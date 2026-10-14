"""Run focused, credential-free customer contract fixtures, never live acceptance.

From the repository root::

    python3 evals/customer_acceptance_preflight.py
    python3 evals/customer_acceptance_preflight.py --output /tmp/raven-preflight.json

Each check runs explicit unittest targets with PYTHONPATH=tests:<repository>.
Tests use synthetic stores, actors and transports; HTTP tests use loopback.
No Claude host, browser, external provider, or real Slack delivery is exercised.
A passing report is only a prerequisite to separately authorized live acceptance.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Check:
    name: str
    coverage: str
    targets: tuple[str, ...]


# Explicit classes avoid unittest collecting imported base classes twice. The
# three inquiry methods avoid repeating the inherited host notification tests.
CHECKS = (
    Check('notification_only_inbox', 'Notification-only delivery and authenticated inbox', (
        'test_notification_only.NotificationOnlyTests',
        'test_auth.IdentityAndRoleTests.test_writes_are_attributed_to_the_signed_in_person',
        'test_auth.IdentityAndRoleTests.test_task_overview_is_private_and_viewer_cannot_add_context',
    )),
    Check('host_notification_security', 'Recipient binding, single-send claims, receipts and restart', (
        'test_host_notifications.HostNotificationTests',
    )),
    Check('task_inquiry_acl', 'Task-scoped read-only inquiry, ACL and revocation', (
        'test_host_notifications.TaskInquiryTests.test_task_read_does_not_import_neighboring_task_or_create_authority',
        'test_host_notifications.TaskInquiryTests.test_forged_scope_and_mutation_fields_rejected',
        'test_host_notifications.TaskInquiryTests.test_revocation_after_model_read_does_not_return_answer',
        'test_host_notifications.TaskInquiryHTTPTests',
    )),
    Check('signoff_authority', 'Human authority and revision-bound signatures', (
        'test_trust.WhoMayAct',
        'test_trust.WhatASignatureCovers',
        'test_trust.WhoMustApprove',
        'test_security_isolation.AuthorityFreshnessTests',
        'test_security_isolation.CredentialIsolationTests',
    )),
    Check('evidence_freshness_scope_cache', 'Evidence freshness, namespace scope and cache invalidation', (
        'test_fact_revision_freshness.FactCorrectionFreshnessTests',
        'test_cache_namespace_applicability.CacheNamespaceApplicabilityTests',
        'test_rule_invalidation.RuleInvalidationTests',
        'test_security_isolation.RouteAuthorityFreshnessTests',
    )),
    Check('durable_wait_restart', 'Durable host mailbox, wait bounds, resume and restart', (
        'test_host_sessions.HostTests',
        'test_minimal_e2e_regressions.MinimalHostTests',
        'test_host_wake.HostWakeTests.test_stop_is_held_with_a_wait_while_a_person_has_not_answered',
        'test_host_wake.HostWakeTests.test_a_stopped_session_is_resumed_in_auto_mode_and_reported_to_slack',
    )),
)

# Do not copy os.environ and then remove credentials: that reads their values.
# Only these OS/runtime variables may be read from the caller's environment.
ENV_ALLOWLIST = frozenset({
    'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT',
    'TMP', 'TEMP', 'TMPDIR', 'LANG', 'LC_ALL', 'LC_CTYPE',
})
COUNT_NAMES = ('tests_run', 'tests_passed', 'failures', 'errors', 'skipped',
               'expected_failures', 'unexpected_successes')
SUMMARY = re.compile(r'^Ran (\d+) tests? in [0-9.]+s$', re.MULTILINE)
OUTCOME = re.compile(r'^(OK|FAILED)(?: \(([^\n]*)\))?$', re.MULTILINE)
DETAIL = re.compile(r'(failures|errors|skipped|expected failures|unexpected successes)=(\d+)')


def fixture_environment(source: Mapping[str, str], home: Path, root: Path = ROOT) -> dict[str, str]:
    """Filter names first; never retrieve an unapproved environment value."""
    env = {name: source[name] for name in source if name in ENV_ALLOWLIST}
    env.update({
        'HOME': str(home), 'USERPROFILE': str(home),
        'XDG_CONFIG_HOME': str(home / 'config'),
        'XDG_CACHE_HOME': str(home / 'cache'),
        'XDG_DATA_HOME': str(home / 'data'),
        'PYTHONPATH': os.pathsep.join((str(root / 'tests'), str(root))),
        'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONHASHSEED': '0', 'NO_COLOR': '1',
        'BRIDGE_MODEL_API': 'none', 'BRIDGE_SEMANTIC': '0',
        'BRIDGE_LIVE': '0', 'BRIDGE_DEEP': '0',
        'BRIDGE_CLAUDE_BIN': str(home / 'no-claude-host'),
        'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull,
        'GIT_TERMINAL_PROMPT': '0',
    })
    return env


def parse_result(output: str, returncode: int) -> tuple[dict[str, int], str]:
    """Require an actual, nonempty unittest result and a successful process."""
    counts = dict.fromkeys(COUNT_NAMES, 0)
    summaries, outcomes = list(SUMMARY.finditer(output)), list(OUTCOME.finditer(output))
    if len(summaries) != 1 or len(outcomes) != 1 or outcomes[0].start() < summaries[0].end():
        return counts, 'missing_or_ambiguous_unittest_summary'
    counts['tests_run'] = int(summaries[0][1])
    details = outcomes[0][2] or ''
    for key, value in DETAIL.findall(details):
        counts[key.replace(' ', '_')] = int(value)
    counts['tests_passed'] = max(0, counts['tests_run'] - sum(
        counts[name] for name in COUNT_NAMES if name not in ('tests_run', 'tests_passed')))
    if returncode:
        return counts, 'nonzero_exit'
    if outcomes[0][1] != 'OK' or counts['failures'] or counts['errors'] or counts['unexpected_successes']:
        return counts, 'test_failure'
    if not counts['tests_run']:
        return counts, 'no_tests_run'
    if counts['skipped'] or counts['expected_failures']:
        return counts, 'incomplete_coverage'
    if details:
        # Unknown result annotations cannot silently weaken the pass criteria.
        return counts, 'unrecognized_unittest_summary'
    return counts, ''


def run_preflight(checks: Sequence[Check] = CHECKS, *, timeout: float = 25,
                  total_timeout: float = 150, environ: Mapping[str, str] | None = None,
                  runner=None, root: Path = ROOT) -> dict:
    """Run the bounded fixture manifest and return a JSON-serializable report."""
    if not all(math.isfinite(value) and value > 0 for value in (timeout, total_timeout)):
        raise ValueError('Timeouts must be finite and positive')
    runner = runner or subprocess.run
    started = time.monotonic()
    results = []
    with tempfile.TemporaryDirectory(prefix='raven-preflight-') as directory:
        env = fixture_environment(os.environ if environ is None else environ, Path(directory), root)
        for check in checks:
            command = [sys.executable, '-m', 'unittest', '-q', *check.targets]
            result = {
                'name': check.name, 'coverage': check.coverage,
                'command': command, 'cwd': str(root),
                'status': 'failed', 'returncode': None,
                'counts': dict.fromkeys(COUNT_NAMES, 0),
            }
            remaining = total_timeout - (time.monotonic() - started)
            if remaining <= 0:
                result.update(reason='total_timeout_exhausted', elapsed_seconds=0.0)
                results.append(result)
                continue
            allowance = min(timeout, remaining)
            result['timeout_seconds'] = round(allowance, 3)
            check_started = time.monotonic()
            try:
                completed = runner(command, cwd=root, env=env, capture_output=True,
                                   text=True, timeout=allowance, check=False)
                result['returncode'] = completed.returncode
                output = completed.stdout + '\n' + completed.stderr
                result['counts'], reason = parse_result(output, completed.returncode)
                result['reason'] = reason
                if reason:
                    result['diagnostic'] = output[-12000:]
                else:
                    result['status'] = 'passed'
            except subprocess.TimeoutExpired:
                result['reason'] = 'command_timeout'
            except OSError as error:
                result.update(reason='runner_error', error_type=type(error).__name__)
            result['elapsed_seconds'] = round(time.monotonic() - check_started, 3)
            results.append(result)
    passed = sum(result['status'] == 'passed' for result in results)
    totals = {name: sum(result['counts'][name] for result in results) for name in COUNT_NAMES}
    totals.update(commands_run=sum(result['reason'] != 'total_timeout_exhausted' for result in results),
                  commands_passed=passed, commands_failed=len(results) - passed,
                  commands_total=len(results))
    return {
        'schema_version': 1,
        'fixture_only': True,
        'live_acceptance': False,
        'status': 'passed' if results and passed == len(results) else 'failed',
        'scope': 'Focused synthetic contract preflight; no real Claude host, browser, provider or Slack delivery.',
        'live_acceptance_remaining': [
            'Actual Claude host tool invocation and permission boundary',
            'Real notification receipt in the intended workspace and recipient inbox',
            'Browser sign-in, human inquiry and signoff, and actual host resume',
        ],
        'environment': {
            'policy': 'OS/runtime allowlist; caller credential values are never read',
            'provider_credentials_inherited': False,
            'isolated_home': True,
            'model_api': 'none',
            'network_scope': 'Synthetic fixtures and local loopback HTTP only; not an OS network sandbox',
        },
        'reproduce': shlex.join([sys.executable, 'evals/customer_acceptance_preflight.py',
                                 '--timeout', str(timeout), '--total-timeout', str(total_timeout)]),
        'limits': {'command_timeout_seconds': timeout, 'total_timeout_seconds': total_timeout},
        'elapsed_seconds': round(time.monotonic() - started, 3),
        'summary': totals,
        'checks': results,
    }


def positive_seconds(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be finite and greater than zero')
    return number


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Also save the JSON report to this file')
    parser.add_argument('--timeout', type=positive_seconds, default=25, help='Per-command seconds (default: 25)')
    parser.add_argument('--total-timeout', type=positive_seconds, default=150,
                        help='Total fixture budget in seconds (default: 150)')
    args = parser.parse_args(argv)
    report = run_preflight(timeout=args.timeout, total_timeout=args.total_timeout)
    serialized = json.dumps(report, indent=2, sort_keys=True) + '\n'
    if args.output:
        args.output.write_text(serialized, encoding='utf-8')
    print(serialized, end='')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
