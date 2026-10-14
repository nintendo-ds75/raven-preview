"""Offline driver/containment tests. These never count as live host acceptance."""
import json
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request

from evals import claude_mcp_acceptance as a


class ScriptedHost:
    """Deliberate model double, using real authenticated Raven HTTP."""
    live = False
    def __init__(self, root, *, fail=False):
        self.root, self.fail = root, fail
        self.session_id = 'scripted-persistent-session'
        self.stopped = threading.Event()
        self.thread = None
        self.errors = []
        self.seq = 0

    def call(self, fixture, name, **arguments):
        self.seq += 1
        ident = self.seq
        request = {'jsonrpc': '2.0', 'id': ident, 'method': 'tools/call',
                   'params': {'name': name, 'arguments': arguments}}
        def record(direction, message):
            with (self.root / 'mcp-trace.jsonl').open('a') as out:
                out.write(json.dumps({'time': time.time(), 'direction': direction,
                                      'session': self.session_id, 'message': message}) + '\n')
        record('request', request)
        status, result, _ = a.http_json(fixture.url + '/mcp', request, fixture.mcp_headers(), timeout=120)
        if status != 200:
            raise AssertionError((status, result))
        record('response', result)
        result = result['result']
        if result.get('isError'):
            raise AssertionError(result)
        return json.loads(result['content'][0]['text'])

    def start(self, fixture, phase):
        self.stopped.clear()
        def work():
            try:
                if self.fail:
                    return
                if phase == 'initial':
                    task = self.call(fixture, 'bridge_start_task', title='Production internal load-test invoice policy',
                                     goal=a.PROMPT, repo=a.REPO, client_key='claude-mcp-acceptance', paths=a.POLICY)['task_id']
                    self.call(fixture, 'bridge_search_context', repo=a.REPO, query='Production billing context', task_id=task)
                    self.call(fixture, 'bridge_search_decisions', repo=a.REPO, query='internal load-test traffic billable')
                    self.call(fixture, 'bridge_add_node', task_id=task,
                              question='Should internal load-test traffic count on production customer invoices?',
                              paths=a.POLICY, category='policy', facts='environment=production', client_ref='production-policy')
                    while not self.stopped.is_set():
                        self.call(fixture, 'bridge_wait', task_id=task, timeout='1')
                else:
                    started = self.call(fixture, 'bridge_start_task', title='Production internal load-test invoice policy',
                                        repo=a.REPO, client_key='claude-mcp-acceptance')
                    task = started['task_id']
                    tree = self.call(fixture, 'bridge_get_tree', task_id=task)
                    answer = tree['nodes'][0]['answer']
                    cohort = re.search(r'pilot_[0-9a-f]{8}', answer)[0]
                    threshold = int(re.search(r'more than (\d+)', answer)[1])
                    source = ('def is_billable(is_internal, cohort, calls):\n'
                              f'    return not is_internal or (cohort == {cohort!r} and calls > {threshold})\n')
                    a.fixture_call(fixture.root / 'checkout', 'write_file', {'path': a.POLICY, 'content': source})
                    a.fixture_call(fixture.root / 'checkout', 'run_checks', {})
                    diff = a.fixture_call(fixture.root / 'checkout', 'read_changes', {})['diff']
                    self.call(fixture, 'bridge_get_tree', task_id=task)
                    self.call(fixture, 'bridge_finish_task', task_id=task, diff=diff,
                              checks='Fixture public smoke checks: 2 passed.')
            except Exception as error:
                self.errors.append(error)
        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()

    def alive(self):
        return self.thread is not None and self.thread.is_alive()

    def stop(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=3)


class DriverTests(unittest.TestCase):
    def test_exception_diagnostic_keeps_structure_without_secret_text_or_paths(self):
        secret = 'PRIVATE_API_KEY_AND_PROMPT'
        try:
            try:
                raise FileNotFoundError(2, secret, '/private/' + secret)
            except FileNotFoundError as cause:
                raise RuntimeError(secret) from cause
        except RuntimeError as error:
            diagnostic = a.exception_diagnostic(error)
        serialized = json.dumps(diagnostic)
        self.assertNotIn(secret, serialized)
        self.assertNotIn('/private/', serialized)
        self.assertEqual(diagnostic['class'], 'RuntimeError')
        self.assertEqual(diagnostic['cause']['class'], 'FileNotFoundError')
        self.assertEqual(diagnostic['cause']['errno'], 2)
        self.assertTrue(diagnostic['frames'])
        self.assertEqual(set(diagnostic['frames'][0]), {'module', 'line'})

    def test_host_event_projection_discards_unknown_fields_and_raw_diagnostics(self):
        event = {'kind': 'error', 'code': 'SECRET', 'stderr': 'SECRET', 'diagnostic': {
            'module': 'builtins', 'class': 'RuntimeError', 'message': 'SECRET',
            'frames': [{'module': 'claude_agent_sdk._internal.transport', 'line': 42,
                        'filename': '/private/SECRET.py', 'locals': {'key': 'SECRET'}}],
            'cause': {'module': 'builtins', 'class': 'ModuleNotFoundError', 'message': 'SECRET'},
            'exit_code': 1}}
        result = a.sanitized_host_event(event)
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(result['diagnostic']['frames'][0]['line'], 42)
        self.assertEqual(result['diagnostic']['cause']['class'], 'ModuleNotFoundError')
        self.assertEqual(result['diagnostic']['exit_code'], 1)

    def test_sdk_host_persists_sanitized_returncode_and_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = a.SDKHost(SimpleNamespace(), Path(tmp), 'fixture-model')
            host.process = SimpleNamespace(poll=lambda: 7, stdin=io.StringIO(), stdout=io.StringIO())
            host.phases = [{'phase': 'initial', 'event_start': 0, 'returncode': None}]
            host.events = [a.sanitized_host_event({'kind': 'error', 'code': 'SECRET'})]
            host.stop()
            saved = (Path(tmp) / 'host-diagnostics.json').read_text()
        self.assertNotIn('SECRET', saved)
        self.assertEqual(json.loads(saved)['phases'][0]['returncode'], 7)
        self.assertFalse(json.loads(saved)['raw_stderr_retained'])

    def test_empty_home_does_not_expand_inherited_pythonpath(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / 'empty-home'
            home.mkdir()
            gateway = SimpleNamespace(url='http://127.0.0.1:1', placeholder='public-placeholder')
            with patch.dict(os.environ, {'PYTHONPATH': '/private/untrusted-python-path'}):
                env = a.host_environment(gateway, home)
            self.assertNotIn('/private/untrusted', env['PYTHONPATH'])
            self.assertEqual(env['PYTHONPATH'].split(os.pathsep)[0], str(a.ROOT))

    def test_isolated_child_uses_exact_distribution_and_constructs_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdk_root = Path(tmp) / 'pinned-site'
            package = sdk_root / 'claude_agent_sdk'
            package.mkdir(parents=True)
            (package / '__init__.py').write_text(
                'class ClaudeAgentOptions:\n    def __init__(self, **kw): self.__dict__.update(kw)\n'
                'class HookMatcher:\n    def __init__(self, **kw): self.__dict__.update(kw)\n'
                'class PermissionResultDeny:\n    def __init__(self, **kw): self.__dict__.update(kw)\n')
            info = sdk_root / 'claude_agent_sdk-0.2.165.dist-info'
            info.mkdir()
            (info / 'METADATA').write_text('Metadata-Version: 2.1\nName: claude-agent-sdk\nVersion: 0.2.165\n')
            distribution = SimpleNamespace(locate_file=lambda _: sdk_root)
            with patch.object(a.importlib.metadata, 'distribution', return_value=distribution):
                result = a.sdk_child_preflight('fixture-model')
            self.assertEqual(result, {'status': 'passed', 'version': a.SDK_VERSION, 'options_constructed': True})

    def test_wrong_isolated_sdk_version_blocks_before_key_prompt(self):
        with patch.object(a.importlib.metadata, 'version', return_value=a.SDK_VERSION), \
                patch.object(a, 'sdk_child_preflight', return_value={'status': 'blocked', 'code': 'wrong_version'}), \
                patch.object(a.getpass, 'getpass', side_effect=AssertionError('must not ask')):
            self.assertEqual(a.main(['--run-live', '--model', 'fixture-model', '--output-dir', '/tmp/uncreated-child-check']), 1)

    def test_offline_sdk_transcript_emits_sse_tool_calls_and_uses_real_result_identity(self):
        import httpx
        transcript = a.OfflineSDKTranscript()
        first = transcript(httpx.Request('POST', 'https://api.anthropic.com/v1/messages',
            json={'model': 'fixture-model', 'stream': True, 'messages': []}))
        self.assertEqual(first.status_code, 200)
        self.assertIn('mcp__raven__bridge_start_task', first.text)
        self.assertIn('event: content_block_delta', first.text)
        result = {'role': 'user', 'content': [{'type': 'tool_result', 'content': [
            {'type': 'text', 'text': json.dumps({'task_id': 'actual-task-from-mcp'})}]}]}
        second = transcript(httpx.Request('POST', 'https://api.anthropic.com/v1/messages',
            json={'model': 'fixture-model', 'stream': False, 'messages': [result]}))
        block = second.json()['content'][0]
        self.assertEqual(block['name'], 'mcp__raven__bridge_search_context')
        self.assertEqual(block['input']['task_id'], 'actual-task-from-mcp')
        denied = transcript(httpx.Request('POST', 'https://other.invalid/v1/messages', json={}))
        self.assertEqual(denied.status_code, 403)

    def test_default_has_no_harness_provider_or_filesystem_effect(self):
        with patch.object(a, 'Fixture', side_effect=AssertionError('must not construct')), \
                patch.object(a, 'freshness_checks', side_effect=AssertionError('must not run')):
            self.assertEqual(a.run()['status'], 'blocked')

    def test_live_missing_gateway_has_no_effect(self):
        with patch.object(a, 'Fixture', side_effect=AssertionError('must not construct')):
            self.assertEqual(a.run(run_live=True, model='fixture')['status'], 'blocked')

    def test_live_missing_sdk_does_not_prompt_for_key_or_construct_gateway(self):
        with patch.object(a.importlib.metadata, 'version', side_effect=a.importlib.metadata.PackageNotFoundError), \
                patch.object(a.getpass, 'getpass', side_effect=AssertionError('must not ask')):
            self.assertEqual(a.main(['--run-live', '--model', 'fixture', '--output-dir', '/tmp/uncreated-fixture']), 1)

    def test_direct_smoke_uses_production_messages_without_transport_replacement(self):
        from bridge import llm
        payload = {'content': [{'type': 'text', 'text': 'RAVEN_DIRECT_OK'}],
                   'usage': {'input_tokens': 12, 'output_tokens': 6}}
        with patch.object(llm.Client, '_post_json', return_value=payload) as post:
            result = a.direct_inference_smoke('synthetic-smoke-key', 'workspace-fixture')
        self.assertEqual(post.call_args.args[0], 'https://api.anthropic.com/v1/messages')
        self.assertEqual(post.call_args.args[1]['x-api-key'], 'synthetic-smoke-key')
        self.assertEqual(post.call_args.args[2]['max_tokens'], 32)
        self.assertEqual(result['maximum_physical_attempts'], 4)
        self.assertNotIn('synthetic-smoke-key', json.dumps(result))

    def test_direct_smoke_drops_inherited_proxy_and_tls_overrides(self):
        from bridge import llm
        def post(*_):
            self.assertNotIn('HTTPS_PROXY', os.environ)
            self.assertNotIn('https_proxy', os.environ)
            self.assertNotIn('SSL_CERT_FILE', os.environ)
            return {'content': [{'type': 'text', 'text': 'RAVEN_DIRECT_OK'}]}
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://bad', 'https_proxy': 'http://bad', 'SSL_CERT_FILE': '/untrusted'}), \
                patch.object(llm.Client, '_post_json', side_effect=post):
            a.direct_inference_smoke('synthetic-key')

    def test_official_sdk_options_have_only_two_bounded_external_mcp_servers(self):
        fake = SimpleNamespace(ClaudeAgentOptions=lambda **kw: SimpleNamespace(**kw),
                               HookMatcher=lambda **kw: kw, PermissionResultDeny=lambda **kw: kw)
        config = {'raven_env': {'BRIDGE_TOKEN': 'synthetic'}, 'checkout': '/tmp/fixture', 'home': '/tmp/home',
                  'model': 'fixture-model', 'max_turns': 20, 'budget_usd': 1, 'session_id': 'saved'}
        with patch.dict(sys.modules, {'claude_agent_sdk': fake}):
            options = a.sdk_options(config)
        self.assertEqual(options.tools, [])
        self.assertEqual(set(options.mcp_servers), {'raven', 'fixture'})
        self.assertTrue(options.strict_mcp_config)
        self.assertEqual(options.resume, 'saved')
        self.assertEqual(options.setting_sources, [])

    def test_direct_failure_is_saved_without_raw_exception_or_credential(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(a.importlib.metadata, 'version', return_value=a.SDK_VERSION), \
                patch.object(a, 'sdk_child_preflight', return_value={'status': 'passed'}), \
                patch.object(a.getpass, 'getpass', return_value='synthetic-credential'), \
                patch.object(a, 'direct_inference_smoke', side_effect=RuntimeError('synthetic-credential PRIVATE_BODY')):
            output = Path(tmp) / 'result'
            self.assertEqual(a.main(['--run-live', '--model', 'fixture-model', '--output-dir', str(output)]), 1)
            saved = (output / 'result.json').read_text()
        self.assertNotIn('synthetic-credential', saved)
        self.assertNotIn('PRIVATE_BODY', saved)
        self.assertEqual(json.loads(saved)['failure_code'], 'direct_provider_failed')

    def test_long_wait_really_remains_pending_beyond_old_one_second_cap(self):
        with a.Fixture() as fixture:
            fixture.prepare()
            task = fixture.call('bridge_start_task', title='Production policy', repo=a.REPO, paths=a.POLICY)['task_id']
            fixture.call('bridge_add_node', task_id=task, question='Which production billing policy applies?', paths=a.POLICY)
            fixture.call('bridge_get_tree', task_id=task)
            ended = threading.Event()
            def waiting():
                fixture.call('bridge_wait', task_id=task, timeout='2')
                ended.set()
            thread = threading.Thread(target=waiting)
            thread.start()
            self.assertFalse(ended.wait(1.2), 'Old one-second cap must not silently return')
            thread.join(5)
            self.assertTrue(ended.is_set())

    def test_authenticated_inquiry_live_path_with_explicit_offline_model_double(self):
        from bridge import llm
        with a.Fixture() as fixture:
            fixture.prepare()
            task = fixture.call('bridge_start_task', title='Production billing policy', repo=a.REPO, paths=a.POLICY)['task_id']
            node = fixture.call('bridge_add_node', task_id=task, question='Is customer traffic billable?', paths=a.POLICY)['node_id']
            fixture.human('/api/decisions/' + node + '/signoff', {
                'answer': 'Real customer traffic is billable.',
                'expected_updated_at': fixture.store.get_decision(node)['updated_at']})
            gateway = SimpleNamespace(placeholder='public-placeholder', urllib_open=urllib.request.urlopen)
            readings = [{'outcome': 'supported_answer', 'answer': 'Real customer traffic is billable.',
                         'citations': [{'id': node, 'quote': 'Real customer traffic is billable.'}], 'open': []},
                        {'claims': [{'claim': 'Real customer traffic is billable.', 'support': 'stated'}]}]
            with patch.object(llm.Client, 'complete_json', side_effect=readings) as mocked:
                result = a.live_inquiry(fixture, task, node, gateway, 'fixture-model')
            self.assertEqual(mocked.call_count, 2)
            self.assertTrue(result['model_used'])
            self.assertTrue(result['counts_unchanged'])

    def test_host_environment_never_inherits_external_credentials_or_settings(self):
        gateway = SimpleNamespace(url='http://127.0.0.1:1234', placeholder='public-placeholder')
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'secret', 'SLACK_BOT_TOKEN': 'secret',
                                    'HTTP_PROXY': 'http://secret', 'CLAUDE_CONFIG_DIR': '/private'}):
            env = a.host_environment(gateway, Path('/tmp/isolated'))
        self.assertEqual(env['ANTHROPIC_API_KEY'], gateway.placeholder)
        self.assertNotIn('secret', json.dumps(env))
        self.assertNotIn('HTTP_PROXY', env)
        self.assertEqual(env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'], '1024')

    def test_fixture_rejects_escape_and_executable_code_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'billing').mkdir()
            (root / a.POLICY).write_text(a.BASELINE)
            for path in ('../secret', '/etc/passwd', 'billing/../billing/usage.py'):
                with self.assertRaises(a.AcceptanceFailure):
                    a.fixture_call(root, 'read_file', {'path': path})
            for source in ('import os\n', 'def is_billable(is_internal, cohort, calls):\n    return __import__("os")\n',
                           'def is_billable(is_internal, cohort, calls):\n    return cohort.__class__\n'):
                with self.assertRaises(a.AcceptanceFailure):
                    a.fixture_call(root, 'write_file', {'path': a.POLICY, 'content': source})
            self.assertEqual((root / a.POLICY).read_text(), a.BASELINE)

    def test_external_fixture_stdio_is_real_and_contains_no_shell_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05'}},
                        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}]
            env = {'PATH': os.environ.get('PATH', ''), 'RAVEN_ACCEPTANCE_CHECKOUT': tmp}
            proc = subprocess.run([sys.executable, str(Path(a.__file__)), '--fixture-mcp'], env=env,
                input='\n'.join(json.dumps(m) for m in messages) + '\n', text=True, capture_output=True, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout.splitlines()[1])['result']
            self.assertEqual({s['name'] for s in result['tools']}, a.FIXTURE_TOOLS)

    def test_scripted_full_driver_is_explicitly_not_live(self):
        hosts = []
        def factory(root):
            host = ScriptedHost(root)
            hosts.append(host)
            return host
        report = a.run(host_factory=factory, idle_seconds=.01, timeout=20,
                       freshness_runner=lambda: {'mode': 'test-double-not-executed'})
        self.assertEqual(report['status'], 'scripted_passed', (report, hosts[0].errors if hosts else []))
        self.assertFalse(report['host_live'])
        self.assertEqual(report['behavioral_cases'], 16)
        self.assertTrue(report['same_session_resumed'])
        self.assertFalse(report['scoped_inquiry']['authorized'])

    def test_failed_host_stops_without_signoff_or_pass(self):
        hosts = []
        def factory(root):
            host = ScriptedHost(root, fail=True)
            hosts.append(host)
            return host
        report = a.run(host_factory=factory, idle_seconds=.01, timeout=2,
                       freshness_runner=lambda: {'mode': 'test-double-not-executed'})
        self.assertEqual(report['status'], 'blocked', report)
        self.assertIn('host_exited', report['failure_code'])
        self.assertNotIn('same_session_resumed', report)
        self.assertFalse(hosts[0].alive())


if __name__ == '__main__':
    unittest.main()
