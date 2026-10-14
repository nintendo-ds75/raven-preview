"""Opt-in bounded Raven advisory review of a retained synthetic Claude host run.

Each case uses a read-only SQLite backup. Existing advisory cache events,
signed decisions, and source pins are preserved: enabling the backend must
automatically trigger the production cache-identity refresh. This is a separate diagnostic, never a fresh host E2E result or
independent evidence of code correctness. No real Slack/GitHub transport starts.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import difflib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from bridge import canvas, llm
from bridge.auth import Auth
from bridge.server import make_server
from bridge.store import Store
from evals.claude_mcp_acceptance import BASELINE, POLICY, REPO, exception_diagnostic, policy_expression
from evals.live_workflow_acceptance import AcceptanceFailure, check_expression, reuse_scenario
from evals.slack_contract_harness import REQUESTER

MODEL = 'claude-sonnet-4-6'
LIMITS = {'max_calls': 8, 'max_input_bytes': 300000, 'max_output_tokens': 12000}
PIN_TABLES = ('decisions', 'decision_versions', 'decision_source_edges', 'source_records',
              'source_versions', 'source_identities', 'task_source_anchors', 'native_source_access',
              'withheld_source_versions', 'authority', 'source_connections', 'task_source_reads')


def require(value, code):
    if not value:
        raise AcceptanceFailure(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def database_digest(path):
    """A logical digest includes committed WAL contents; no SQL/raw data is saved."""
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        return digest('\n'.join(db.iterdump()).encode())


def snapshot_pins(store):
    tables = {r[0] for r in store.graph.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {table: digest(json.dumps(sorted([list(r) for r in store.graph.db.execute('SELECT * FROM ' + table)],
                          key=repr), sort_keys=True, default=lambda v: {'blob_hex': bytes(v).hex()}).encode()) for table in PIN_TABLES if table in tables}


def policy_diff(source):
    return 'diff --git a/' + POLICY + ' b/' + POLICY + '\n' + ''.join(difflib.unified_diff(
        BASELINE.splitlines(True), source.splitlines(True), fromfile='a/' + POLICY, tofile='b/' + POLICY))


def load_source(source_run):
    root = Path(source_run).resolve()
    saved = json.loads((root / 'result.json').read_text())
    require(saved.get('synthetic_people') is True and saved.get('synthetic_slack') is True,
            'source_not_synthetic_host_fixture')
    require(saved.get('proof_integrity_valid') is True, 'source_proof_not_verified')
    scenario, _ = reuse_scenario(saved)
    source = (root / 'fixture/checkout' / POLICY).read_text()
    expression = policy_expression(source)
    observations = check_expression(expression, scenario)
    actual = policy_diff(source)
    require(actual == saved.get('actual_patch'), 'source_patch_mismatch')
    # Mutate the strict call boundary only. The signed decision remains identical.
    import ast
    tree = ast.parse(source)
    changed = 0
    for node in ast.walk(tree):
        if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == 'calls'
                and len(node.ops) == len(node.comparators) == 1 and isinstance(node.ops[0], ast.Gt)
                and isinstance(node.comparators[0], ast.Constant)
                and node.comparators[0].value == scenario['threshold']):
            node.ops[0] = ast.GtE()
            changed += 1
    require(changed == 1, 'source_strict_boundary_not_unique')
    wrong_source = ast.unparse(tree) + '\n'
    wrong_expression = policy_expression(wrong_source)
    try:
        check_expression(wrong_expression, scenario)
    except AcceptanceFailure as error:
        require(error.code == 'agent_behavior_contradicts_signed_boundary', 'wrong_boundary_oracle_failed')
    else:
        raise AcceptanceFailure('wrong_boundary_not_adversarial')
    return {'task_id': saved['task_id'], 'node_id': saved['node_id'], 'scenario': scenario,
            'answer': scenario['base_answer'] + '\n' + scenario['caveat_message'],
            'database': root / 'fixture/raven.db', 'source_run': root,
            'independent_cases': len(observations), 'correct': actual, 'wrong_boundary': policy_diff(wrong_source),
            'source_result_sha256': digest((root / 'result.json').read_bytes()),
            'source_checkout_sha256': digest(source.encode())}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ReviewFixture:
    """Authenticated agent MCP over a loopback server, production review code."""
    def __init__(self, root, source, gateway, model, deadline, cleanup_deadline):
        self.root, self.source, self.gateway = Path(root), source, gateway
        self.deadline, self.cleanup_deadline = deadline, cleanup_deadline
        self.store = self.server = self.thread = None
        self.stopping, self.serial = False, 0
        self.stack = ExitStack()
        self.trace, self.model_calls = [], []
        self.root.mkdir(parents=True)
        self.database = self.root / 'raven.db'
        with sqlite3.connect(source['database'].resolve().as_uri() + '?mode=ro', uri=True) as original, \
                sqlite3.connect(self.database) as copied:
            original.backup(copied)
        clean = {k: os.environ[k] for k in ('PATH', 'LANG') if k in os.environ}
        clean.update(HOME=str(self.root), BRIDGE_MODEL_API='anthropic', BRIDGE_MODEL=model,
            BRIDGE_FAST_MODEL=model, BRIDGE_SEMANTIC='1', BRIDGE_LIVE='0', BRIDGE_DEEP='0',
            BRIDGE_EMBEDDINGS='disabled', BRIDGE_MCP_CALL_BUDGET='5',
            ANTHROPIC_API_KEY=gateway.placeholder)
        self.stack.enter_context(patch.dict(os.environ, clean, clear=True))
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())
        def transport(request, *args, **kwargs):
            kwargs['timeout'] = min(float(kwargs.get('timeout', 120)), self.remaining())
            return gateway.urllib_open(request, *args, **kwargs)
        self.stack.enter_context(patch('bridge.llm._open_model_request', new=transport))
        original = llm.Client.complete_json
        def observe(client, purpose, system, prompt, *args, **kwargs):
            self.remaining()
            require(purpose in ('conformance', 'counterexample', 'conditions'), 'unexpected_model_purpose')
            record = {'purpose': purpose, 'completed': False}
            self.model_calls.append(record)
            result = original(client, purpose, system, prompt, *args, **kwargs)
            record['completed'] = True
            return result
        self.stack.enter_context(patch.object(llm.Client, 'complete_json', new=observe))
        try:
            self.store = Store(self.database)
            self.pins = snapshot_pins(self.store)
            require(self.store.graph.get_task(source['task_id'])['status'] == 'completed', 'source_task_not_completed')
            require(self.store.graph.get_task(source['task_id'])['repo'] == REPO, 'source_repo_mismatch')
            tree = canvas.get_tree(self.store, source['task_id'])
            nodes = canvas._flatten(tree['nodes'])
            require(len(nodes) == 1 and nodes[0]['node_id'] == source['node_id'], 'source_node_mismatch')
            node = nodes[0]
            require(node.get('authorized') and not node.get('needs_review') and node.get('signoff') == 'signed'
                    and node.get('answer') == source['answer'], 'source_signed_answer_not_current')
            require(json.loads(self.store.get_decision(source['node_id'])['applicability']) ==
                    {'requires': {'environment': 'production'}}, 'source_scope_mismatch')
            self.signed = [node]
            self.source_cache_events = [list(r) for r in self.store.graph.db.execute(
                "SELECT id,kind,detail FROM events WHERE run_id=? AND kind IN "
                "('conformance_started','conformance_read') ORDER BY id", (source['task_id'],))]
            person = self.store.graph.find_person(REQUESTER)
            require(bool(person), 'source_requester_missing')
            self.auth = Auth(self.store, enabled=True, public_url='')
            self.agent = self.auth.create_token(person['id'], 'Isolated review diagnostic', kind='agent')['token']
            self.server = make_server(self.store, port=0, auth=self.auth, github_api=None)
            self.url = f'http://127.0.0.1:{self.server.server_port}'
            self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
            self.thread.start()
            self.rpc('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                                   'clientInfo': {'name': 'isolated-review-evaluator', 'version': '1'}})
        except BaseException:
            require(self.close(), 'review_cleanup_not_quiescent')
            raise

    def remaining(self):
        require(not self.stopping, 'review_fixture_stopping')
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, 'review_deadline')
        return remaining

    def http(self, path, data, *, authenticated=True, expected=200):
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + self.agent
        request = Request(self.url + path, data=json.dumps(data).encode(), headers=headers)
        try:
            response = self.opener.open(request, timeout=min(15, self.remaining()))
        except HTTPError as error:
            response = error
        with response:
            status, result = response.status, json.loads(response.read())
        self.trace.append({'path': path, 'status': status, 'authenticated': authenticated})
        require(status == expected, 'review_http_' + str(status))
        return result

    def rpc(self, method, params):
        self.serial += 1
        result = self.http('/mcp', {'jsonrpc': '2.0', 'id': self.serial, 'method': method, 'params': params})
        require('error' not in result, 'review_mcp_protocol_error')
        return result['result']

    def tool(self, name, **arguments):
        envelope = self.rpc('tools/call', {'name': name, 'arguments': arguments})
        self.trace.append({'tool': name, 'is_error': bool(envelope.get('isError'))})
        require(not envelope.get('isError'), 'review_mcp_refused')
        return json.loads(envelope['content'][0]['text'])

    def access_checks(self):
        self.http('/mcp', {'jsonrpc': '2.0', 'id': 0, 'method': 'tools/list'}, authenticated=False, expected=401)
        self.http('/api/decisions/' + self.source['node_id'] + '/signoff',
            {'expected_updated_at': self.signed[0]['updated_at']}, expected=403)
        require(snapshot_pins(self.store) == self.pins, 'authority_changed_by_access_checks')

    def review(self, diff):
        task, node = self.source['task_id'], self.source['node_id']
        self.tool('bridge_get_tree', task_id=task)
        result = self.tool('bridge_finish_task', task_id=task, diff=diff, diff_sha256=digest(diff.encode()),
            checks='Separate evaluator checks; model review is advisory and not independent correctness evidence.')
        require(result.get('verified') is False, 'finish_claimed_verified')
        expected_id, expected_diff_hash = canvas._review_key(task, diff, self.signed)
        while True:
            tree = self.tool('bridge_get_tree', task_id=task)
            review = tree.get('review') or {}
            if review.get('status') != 'running':
                break
            time.sleep(min(.1, self.remaining()))
        require(review.get('id') == expected_id and review.get('diff_hash') == expected_diff_hash,
                'review_input_identity_mismatch')
        require(review.get('review_inputs') == canvas._review_inputs(self.signed), 'review_signed_inputs_mismatch')
        require(review.get('revisions') == canvas._revisions(self.signed), 'review_signed_revisions_mismatch')
        require(snapshot_pins(self.store) == self.pins, 'review_mutated_signed_state_or_access')
        current_events = {r['id']: [r['id'], r['kind'], r['detail']] for r in self.store.graph.db.execute(
            "SELECT id,kind,detail FROM events WHERE run_id=?", (task,))}
        require(all(current_events.get(r[0]) == r for r in self.source_cache_events), 'source_cache_events_changed')
        require(review.get('review_config') == canvas._review_config(), 'review_backend_identity_mismatch')
        cache_hit = False
        if review.get('status') == 'done':
            before = self.gateway.snapshot()['physical_requests']
            result = self.tool('bridge_finish_task', task_id=task, diff=diff, diff_sha256=digest(diff.encode()),
                checks='Separate evaluator checks; model review is advisory and not independent correctness evidence.')
            require(self.gateway.snapshot()['physical_requests'] == before, 'unchanged_review_cache_miss')
            require(result.get('review', {}).get('id') == expected_id, 'unchanged_review_identity_changed')
            cache_hit = True
        exported = self.tool('bridge_export_proof', task_id=task)
        require(exported['integrity']['valid'] and exported['bundle']['payload']['change']['diff'] == diff,
                'review_proof_diff_mismatch')
        require(snapshot_pins(self.store) == self.pins, 'proof_refresh_mutated_signed_state_or_access')
        if cache_hit:
            require(exported.get('review_snapshot_current') is True, 'terminal_proof_not_refreshed')
        # Do not repeat a failed finish: that would launch an unrequested new attempt.
        return {'review': review, 'verified': result['verified'], 'proof_integrity_valid': True,
                'proof_saved_review': exported['bundle']['payload'].get('review'),
                'proof_review_snapshot_current': exported.get('review_snapshot_current'),
                'pins_unchanged': snapshot_pins(self.store) == self.pins,
                'unchanged_enabled_review_cache_hit': cache_hit,
                'source_advisory_events_preserved': len(self.source_cache_events),
                'model_calls': self.model_calls, 'provider_usage': self.gateway.snapshot(),
                'diff_sha256': digest(diff.encode()), 'node_id': node}

    def close(self):
        self.stopping = True
        # Do not restore ambient credentials/transport beneath a live review worker.
        while time.monotonic() < self.cleanup_deadline:
            with canvas._REVIEWS_LOCK:
                workers = list(canvas._REVIEWS.values())
            if not workers and not self.gateway.snapshot().get('active_requests'):
                break
            for worker in workers:
                worker.join(timeout=.05)
            time.sleep(.01)
        with canvas._REVIEWS_LOCK:
            quiescent = not canvas._REVIEWS
        quiescent = quiescent and not self.gateway.snapshot().get('active_requests')
        if not quiescent:
            return False
        if self.server is not None:
            self.server.stop_waits()
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=3)
            self.server = None
        if self.store is not None:
            self.store.graph.close()
            self.store = None
        self.stack.close()
        return True


def classify(case, result):
    review = result.get('review') or {}
    follows = review.get('follows') or []
    if any(row.get('verdict') == 'follows' for row in follows) and case == 'wrong_boundary':
        return 'failed_false_follows'
    if review.get('status') != 'done' or len(follows) != 1:
        return 'inconclusive'
    row = follows[0]
    if row.get('incomplete') or row.get('unexamined'):
        return 'inconclusive'
    if case == 'correct':
        return 'passed' if row.get('verdict') == 'follows' else 'failed_false_departure' if row.get('verdict') == 'departs' else 'inconclusive'
    located = any((r.get('counterexample') or {}).get('located') for r in row.get('requirements') or [])
    return 'passed' if row.get('verdict') == 'departs' or located else 'inconclusive'


def run(*, source_run=None, output_dir=None, run_live=False, gateway=None, model=MODEL,
        timeout=240, offline_test=False):
    if not (run_live or offline_test) or gateway is None:
        return {'status': 'blocked', 'failure_code': 'explicit_live_gateway_required'}
    if not output_dir or not source_run or not 2 <= timeout <= 240:
        return {'status': 'blocked', 'failure_code': 'review_arguments_invalid'}
    root = Path(output_dir).resolve()
    if root.exists() and any(root.iterdir()):
        return {'status': 'blocked', 'failure_code': 'review_output_not_empty'}
    if root.is_relative_to(Path(source_run).resolve()):
        return {'status': 'blocked', 'failure_code': 'review_output_inside_source'}
    root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    drain = min(65, timeout / 3)
    cleanup_deadline, deadline = started + timeout, started + timeout - drain
    report = {'status': 'failed', 'mode': 'offline_transport_double' if offline_test else 'live_production_review',
        'integrated_host_acceptance': False, 'model': model, 'cases': {}, 'source_database_unchanged': False,
        'cleanup_quiescent': None,
        'limits': gateway.snapshot().get('limits'), 'timeout_seconds': timeout, 'cleanup_reserve_seconds': drain,
        'limitations': ['Model diff review is advisory, never independent correctness verification.',
            'Separate diagnostic on isolated SQLite copies, not a new autonomous-host E2E run.',
            'Existing advisory cache events are preserved; production backend-aware cache refresh is exercised.',
            'Synthetic people and signed policy only; no real Slack, GitHub, or human messages.']}
    source = fixture = None
    try:
        report['source_hashes'] = {p: digest((Path(__file__).resolve().parents[1] / p).read_bytes()) for p in
            ('evals/raven_review_acceptance.py', 'bridge/llm.py', 'bridge/canvas.py', 'evals/customer_provider_gateway.py')}
        source = load_source(source_run)
        source_before = database_digest(source['database'])
        report.update(task_id=source['task_id'], node_id=source['node_id'],
            source_result_sha256=source['source_result_sha256'], source_checkout_sha256=source['source_checkout_sha256'],
            independent_correct_boundary_cases=source['independent_cases'],
            independent_wrong_boundary_check='rejected_at_exact_threshold')
        for case in ('correct', 'wrong_boundary'):
            fixture = ReviewFixture(root / case, source, gateway, model, deadline, cleanup_deadline)
            try:
                report['cases'][case] = {'status': 'inconclusive', 'model_calls': fixture.model_calls}
                fixture.access_checks()
                before_calls = gateway.snapshot()['physical_requests']
                result = fixture.review(source[case])
                require(gateway.snapshot()['physical_requests'] > before_calls, 'review_reused_without_provider')
                result['status'] = classify(case, result)
                report['cases'][case] = result
            finally:
                trace = fixture.trace
                quiescent = fixture.close()
                report['cleanup_quiescent'] = quiescent
                (root / case / 'api-trace.json').write_text(json.dumps(trace, indent=2) + '\n')
                require(quiescent, 'review_cleanup_not_quiescent')
                fixture = None
            if gateway.snapshot().get('halted'):
                break
        statuses = [r['status'] for r in report['cases'].values()]
        report['status'] = ('failed' if any(s.startswith('failed') for s in statuses) else
            'passed' if len(statuses) == 2 and all(s == 'passed' for s in statuses) else 'inconclusive')
    except Exception as error:
        report.update(failure_code=error.code if isinstance(error, AcceptanceFailure) else 'review_diagnostic_failed',
                      runner_diagnostic=exception_diagnostic(error))
        if isinstance(error, AcceptanceFailure) and error.code == 'review_cleanup_not_quiescent':
            report['cleanup_quiescent'] = False
        if isinstance(error, AcceptanceFailure) and error.code in ('review_deadline', 'review_fixture_stopping'):
            report['status'] = 'inconclusive'
    finally:
        if fixture is not None:
            report['cleanup_quiescent'] = fixture.close()
        if source is not None and 'source_before' in locals():
            report['source_database_unchanged'] = database_digest(source['database']) == source_before
            if not report['source_database_unchanged']:
                report.update(status='failed', failure_code='source_database_changed')
        report['provider_usage'] = gateway.snapshot()
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        (root / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live', action='store_true')
    parser.add_argument('--source-run', type=Path)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--credential-file', type=Path)
    parser.add_argument('--workspace-id', default='')
    parser.add_argument('--model', default=MODEL)
    parser.add_argument('--max-output-tokens', type=int, default=LIMITS['max_output_tokens'],
                        help='Reserved provider output ceiling, 1 through 20000; default 12000')
    args = parser.parse_args(argv)
    if not 1 <= args.max_output_tokens <= 20000:
        parser.error('--max-output-tokens must be between 1 and 20000')
    if not args.run_live or not args.source_run or not args.output_dir or not args.credential_file:
        print(json.dumps({'status': 'blocked', 'failure_code': 'explicit_live_source_output_credential_required'}))
        return 1
    from evals.customer_provider_gateway import Gateway
    from evals.customer_test_credentials import read_saved_key
    try:
        key = read_saved_key(args.credential_file)
    except (OSError, ValueError):
        print(json.dumps({'status': 'blocked', 'failure_code': 'saved_credential_unavailable'}))
        return 1
    with Gateway(key, args.workspace_id, model=args.model,
                 **{**LIMITS, 'max_output_tokens': args.max_output_tokens}) as gateway:
        key = ''
        result = run(source_run=args.source_run, output_dir=args.output_dir, run_live=True,
                     gateway=gateway, model=args.model)
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
