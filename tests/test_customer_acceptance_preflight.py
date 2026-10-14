"""The fixture preflight must never pass a failed or credential-bearing runner."""
from collections.abc import Mapping
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals import customer_acceptance_preflight as preflight  # noqa: E402


class NamesOnlyCredentials(Mapping):
    """Any attempt to read even a synthetic credential value is an error."""
    def __iter__(self):
        return iter(('PATH', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'RAVEN_HOST_CODEX_API_KEY',
                     'CLAUDE_CODE_OAUTH_TOKEN', 'AWS_SESSION_TOKEN', 'GITHUB_TOKEN',
                     'SLACK_BOT_TOKEN', 'custom_token', 'CUSTOM_SECRET', 'HOME', 'PYTHONPATH'))

    def __len__(self):
        return 12

    def __getitem__(self, name):
        if name == 'PATH':
            return '/usr/bin:/bin'
        raise AssertionError('Unapproved environment value was read')


class CustomerAcceptancePreflightTests(unittest.TestCase):
    check = preflight.Check('fixture', 'Unit fixture', ('test_fixture.Example',))

    @staticmethod
    def completed(code=0, summary='Ran 3 tests in 0.001s\n\nOK\n'):
        return subprocess.CompletedProcess([], code, '', summary)

    def run_report(self, completed=None, **kwargs):
        def runner(*args, **options):
            return self.completed() if completed is None else completed
        return preflight.run_preflight((self.check,), environ={}, runner=runner, **kwargs)

    def test_report_schema_and_fixture_only_claim(self):
        report = self.run_report()
        self.assertEqual(json.loads(json.dumps(report)), report)
        self.assertEqual(report['schema_version'], 1)
        self.assertTrue(report['fixture_only'])
        self.assertFalse(report['live_acceptance'])
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['summary']['commands_passed'], 1)
        self.assertEqual(report['summary']['commands_failed'], 0)
        self.assertEqual(report['summary']['tests_run'], 3)
        self.assertEqual(report['summary']['tests_passed'], 3)
        self.assertEqual(report['checks'][0]['command'],
                         [sys.executable, '-m', 'unittest', '-q', 'test_fixture.Example'])
        self.assertIn('actual host resume', report['live_acceptance_remaining'][-1])

    def test_nonzero_runner_never_passes_even_with_ok_summary(self):
        report = self.run_report(self.completed(7))
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['checks'][0]['reason'], 'nonzero_exit')
        self.assertEqual(report['checks'][0]['returncode'], 7)
        self.assertEqual(report['summary']['commands_failed'], 1)

    def test_failure_and_error_counts_are_preserved(self):
        report = self.run_report(self.completed(1, 'Ran 4 tests in 0.001s\n\nFAILED (failures=1, errors=1)\n'))
        counts = report['summary']
        self.assertEqual((counts['tests_run'], counts['tests_passed'], counts['failures'], counts['errors']),
                         (4, 2, 1, 1))
        self.assertEqual(report['status'], 'failed')

    def test_zero_exit_with_failed_summary_fails(self):
        report = self.run_report(self.completed(0, 'Ran 1 test in 0.001s\n\nFAILED (failures=1)\n'))
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['checks'][0]['reason'], 'test_failure')

    def test_missing_empty_or_ambiguous_result_cannot_pass(self):
        outputs = ('', 'OK\n', 'Ran 0 tests in 0.001s\n\nOK\n',
                   'Ran 1 test in 0.001s\n\nOK\nRan 1 test in 0.001s\n\nOK\n')
        for output in outputs:
            with self.subTest(output=output):
                self.assertEqual(self.run_report(self.completed(0, output))['status'], 'failed')

    def test_skips_expected_failures_and_unknown_annotations_do_not_complete_coverage(self):
        for annotation in ('skipped=1', 'expected failures=1', 'unexpected successes=1', 'unknown=1'):
            with self.subTest(annotation=annotation):
                report = self.run_report(self.completed(0, f'Ran 2 tests in 0.001s\n\nOK ({annotation})\n'))
                self.assertEqual(report['status'], 'failed')

    def test_environment_filters_names_before_reading_values(self):
        env = preflight.fixture_environment(NamesOnlyCredentials(), Path('/fixture/home'))
        self.assertEqual(env['PATH'], '/usr/bin:/bin')
        self.assertEqual(env['HOME'], '/fixture/home')
        self.assertEqual(env['BRIDGE_MODEL_API'], 'none')
        self.assertEqual(env['PYTHONHASHSEED'], '0')
        self.assertEqual(env['PYTHONPATH'], str(ROOT / 'tests') + preflight.os.pathsep + str(ROOT))
        self.assertFalse(any(marker in name.upper() for name in env
                             for marker in ('TOKEN', 'API_KEY', 'SECRET', 'PASSWORD', 'CREDENTIAL')))

    def test_child_gets_only_scrubbed_environment_and_isolated_home(self):
        calls = []
        def runner(command, **options):
            calls.append((command, options))
            self.assertTrue(Path(options['env']['HOME']).is_dir())
            self.assertFalse(options['check'])
            self.assertLessEqual(options['timeout'], 25)
            self.assertNotIn('shell', options)
            return self.completed()
        report = preflight.run_preflight((self.check,), environ=NamesOnlyCredentials(), runner=runner)
        self.assertEqual(report['status'], 'passed')
        self.assertFalse(Path(calls[0][1]['env']['HOME']).exists())

    def test_timeout_and_spawn_error_are_reported_as_failures(self):
        for error, reason in ((subprocess.TimeoutExpired(['fixture'], 1), 'command_timeout'),
                              (FileNotFoundError('synthetic executable missing'), 'runner_error')):
            with self.subTest(reason=reason):
                def runner(*args, **kwargs):
                    raise error
                report = preflight.run_preflight((self.check,), environ={}, runner=runner)
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(report['checks'][0]['reason'], reason)

    def test_exhausted_budget_is_not_success_and_does_not_launch(self):
        with patch.object(preflight.time, 'monotonic', side_effect=(0, 2, 2)), \
                patch.object(preflight.subprocess, 'run') as runner:
            report = preflight.run_preflight((self.check,), total_timeout=1, environ={})
        runner.assert_not_called()
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['checks'][0]['reason'], 'total_timeout_exhausted')
        self.assertEqual(report['summary']['commands_run'], 0)
        self.assertEqual(report['summary']['commands_failed'], 1)

    def test_later_checks_still_run_after_a_failure(self):
        outcomes = iter((self.completed(1), self.completed()))
        report = preflight.run_preflight((self.check, self.check), environ={},
                                         runner=lambda *args, **kwargs: next(outcomes))
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['summary']['commands_passed'], 1)
        self.assertEqual(report['summary']['commands_failed'], 1)

    def test_empty_manifest_cannot_pass(self):
        self.assertEqual(preflight.run_preflight((), environ={})['status'], 'failed')

    def test_invalid_timeouts_are_rejected(self):
        for value in (0, -1, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.run_report(timeout=value)

    def test_cli_exit_code_and_saved_json_follow_result(self):
        for successful in (True, False):
            with self.subTest(successful=successful), tempfile.TemporaryDirectory() as directory:
                report = self.run_report(self.completed(0 if successful else 1))
                target = Path(directory) / 'result.json'
                with patch.object(preflight, 'run_preflight', return_value=report), \
                        patch('sys.stdout', new_callable=io.StringIO) as output:
                    code = preflight.main(['--output', str(target)])
                self.assertEqual(code, 0 if successful else 1)
                self.assertEqual(json.loads(output.getvalue()), report)
                self.assertEqual(json.loads(target.read_text()), report)

    def test_selected_targets_exist_without_loading_them_or_accessing_credentials(self):
        # Resolve the explicit manifest syntactically: importing all fixtures in
        # this unit test would mutate the test runner's own environment.
        import ast
        for check in preflight.CHECKS:
            self.assertTrue(check.targets)
            for target in check.targets:
                parts = target.split('.')
                module = ast.parse((ROOT / 'tests' / (parts[0] + '.py')).read_text())
                classes = {node.name: node for node in module.body if isinstance(node, ast.ClassDef)}
                self.assertIn(parts[1], classes, target)
                if len(parts) == 3:
                    methods = {node.name for node in classes[parts[1]].body if isinstance(node, ast.FunctionDef)}
                    self.assertIn(parts[2], methods, target)

    def test_real_subprocess_unittest_summary_and_import_failure(self):
        # Exercise actual unittest stdout/stderr conventions without Raven,
        # providers, networking, credentials or a recursive preflight run.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tests').mkdir()
            (root / 'tests' / 'test_probe.py').write_text(
                'import os, unittest\nclass Probe(unittest.TestCase):\n'
                '    def test_ok(self):\n'
                '        self.assertNotIn("ANTHROPIC_API_KEY", os.environ)\n'
                '        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", os.environ)\n'
                '        self.assertEqual(os.environ["BRIDGE_MODEL_API"], "none")\n')
            checks = (preflight.Check('pass', 'Fixture', ('test_probe.Probe',)),
                      preflight.Check('missing', 'Fixture', ('test_nonexistent_preflight_module',)))
            report = preflight.run_preflight(checks, root=root, environ=NamesOnlyCredentials())
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['summary']['commands_passed'], 1)
        self.assertEqual(report['summary']['commands_failed'], 1)
        self.assertEqual(report['summary']['errors'], 1)


if __name__ == '__main__':
    unittest.main()
