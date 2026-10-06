"""Opt-in live Anthropic workflow acceptance against synthetic Slack HTTP.

Default invocation and missing credentials refuse before starting a harness or
provider request. Live mode uses production inference; nothing returns canned
model answers. Physical provider HTTP attempts, including retries, are bounded.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import redirect_stderr, redirect_stdout
import difflib
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from bridge import llm
from bridge.config import load
from evals.slack_contract_harness import (
    OWNER, REFERRED, RavenContractHarness, eventually, http_json,
)

PROVIDER_URL = 'https://api.anthropic.com/v1/messages'


class RequestBudget:
    """Transparent network observer/gate. Does not replace any model output."""
    def __init__(self, maximum=30, opener=None):
        if not isinstance(maximum, int) or not 1 <= maximum <= 100:
            raise ValueError('max_calls must be between 1 and 100')
        self.maximum = maximum
        self.opener = opener or urllib.request.urlopen
        self.lock = threading.Lock()
        self.requests = []
        self.cancelled = False

    def open(self, request, *args, **kwargs):
        url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
        if url != PROVIDER_URL:
            if urllib.parse.urlsplit(url).hostname not in ('127.0.0.1', 'localhost', '::1'):
                raise llm.LLMError('Acceptance run refused an unexpected external destination')
            return self.opener(request, *args, **kwargs)
        with self.lock:
            if self.cancelled:
                raise llm.LLMError('Live workflow provider requests have stopped')
            if len(self.requests) >= self.maximum:
                raise llm.LLMError('Live workflow physical HTTP request budget exhausted')
            record = {'status': None, 'input_tokens': 0, 'output_tokens': 0,
                      'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}
            self.requests.append(record)
        try:
            kwargs['timeout'] = min(float(kwargs.get('timeout', 45)), 45)
            response = self.opener(request, *args, **kwargs)
        except urllib.error.HTTPError as error:
            record['status'] = error.code
            raise
        record['status'] = getattr(response, 'status', 200)
        budget = self

        class ObservedResponse:
            def __init__(self):
                self.body = b''
                self.counted = False

            def read(self, *read_args, **read_kwargs):
                data = response.read(*read_args, **read_kwargs)
                self.body += data
                if not self.counted:
                    try:
                        payload = json.loads(self.body)
                    except (ValueError, UnicodeDecodeError):
                        return data
                    usage = payload.get('usage') or {}
                    with budget.lock:
                        for field in ('input_tokens', 'output_tokens', 'cache_creation_input_tokens',
                                      'cache_read_input_tokens'):
                            record[field] = int(usage.get(field) or 0)
                    self.counted = True
                return data

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return response.__exit__(*exc)

            def __getattr__(self, name):
                return getattr(response, name)

        return ObservedResponse()

    def stop(self):
        with self.lock:
            self.cancelled = True

    def summary(self):
        with self.lock:
            return {'physical_http_requests': len(self.requests), 'max_physical_http_requests': self.maximum,
                    'successful_http_responses': sum(r['status'] == 200 for r in self.requests),
                    'http_statuses': [r['status'] for r in self.requests],
                    **{field: sum(r[field] for r in self.requests) for field in
                       ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')}}


def novel_scenario():
    """These are synthetic human inputs/oracles, never simulated model outputs."""
    threshold = 113 + secrets.randbelow(787)
    cohort = 'pilot_' + secrets.token_hex(4)
    staging = 'staging_' + secrets.token_hex(4)
    base = 'Please exclude internal load-test traffic from invoices and continue billing real customer traffic.'
    caveat = (f'Actually, one exception: for cohort {cohort}, internal load-test traffic with more than '
              f'{threshold} calls must remain billable. At or below {threshold} calls it is excluded. '
              'Internal traffic for other cohorts is excluded. Keep real customer traffic billable.')
    interview = (base + '\n' + caveat + f'\nThis exception is decided for production only. '
                 f'I have not decided whether the {staging} environment should use that exception. '
                 'Ask me about this unresolved environment before proposing its policy.')
    return {'threshold': threshold, 'cohort': cohort, 'staging': staging,
            'base_answer': base, 'caveat_message': caveat, 'interview_response': interview}


def check_expression(expression, scenario):
    """Execute a small, validated expression; never arbitrary provider code."""
    if not isinstance(expression, str) or not 1 <= len(expression) <= 2000:
        raise AssertionError('Agent did not supply a bounded boolean expression')
    tree = ast.parse(expression, mode='eval')
    permitted = (ast.Expression, ast.BoolOp, ast.UnaryOp, ast.Compare, ast.Name, ast.Load,
                 ast.Constant, ast.And, ast.Or, ast.Not, ast.Eq, ast.NotEq, ast.Lt,
                 ast.LtE, ast.Gt, ast.GtE, ast.IfExp)
    for node in ast.walk(tree):
        if not isinstance(node, permitted):
            raise AssertionError('Agent expression contains an unsupported operation')
        if isinstance(node, ast.Name) and node.id not in {'is_internal', 'cohort', 'calls'}:
            raise AssertionError('Agent expression uses an unknown input')
        if isinstance(node, ast.Constant) and not isinstance(node.value, (str, int, bool)):
            raise AssertionError('Agent expression contains an unsupported literal')
    compiled = compile(tree, '<live-agent-billing-expression>', 'eval')
    observations = []
    for internal in (False, True):
        for cohort in (scenario['cohort'], 'ordinary_' + scenario['cohort']):
            for calls in (0, scenario['threshold'] - 1, scenario['threshold'], scenario['threshold'] + 1):
                expected = not internal or (cohort == scenario['cohort'] and calls > scenario['threshold'])
                actual = eval(compiled, {'__builtins__': {}},
                              {'is_internal': internal, 'cohort': cohort, 'calls': calls})
                if type(actual) is not bool or actual != expected:
                    raise AssertionError('Generated behavior contradicts the novel signed boundary condition')
                observations.append({'is_internal': internal, 'cohort': cohort, 'calls': calls, 'billable': actual})
    return observations


def check_guidance(guidance, scenario):
    if guidance.get('mode') != 'model-assisted' or guidance.get('status') != 'unapproved':
        raise AssertionError('Adaptive interview did not produce a validated unapproved live-model draft')
    quote, question = guidance.get('question_quote', ''), guidance.get('question', '')
    response = scenario['interview_response']
    if not quote or quote not in response or not question:
        raise AssertionError('Adaptive follow-up lacks exact grounding')
    if not any(term in quote.lower() for term in (scenario['staging'], 'environment', 'production')):
        raise AssertionError('Follow-up grounding missed the novel unresolved environment')
    if not any(term in question.lower() for term in (scenario['staging'], 'staging', 'environment')):
        raise AssertionError('Follow-up did not ask about the unresolved environment')
    for caveat in guidance.get('caveats', []):
        if caveat.get('quote', '') not in response:
            raise AssertionError('Interview introduced an ungrounded caveat')
    if not guidance.get('caveats'):
        raise AssertionError('Interview did not retain any human caveat')


def human_post(harness, token, path, data):
    status, result, _ = http_json(harness.url + path, data, {'Authorization': 'Bearer ' + token},
                                  timeout=harness.request_timeout)
    if status != 200:
        raise AssertionError('Attributed interview HTTP operation was refused')
    return result


def run_workflow(h, agent, scenario, report, timeout=180):
    """Injectable driver; live callers pass the real bridge.llm.Client."""
    def stage(name):
        report['stage'] = name

    stage('rough_task_discovery')
    initialized = h.rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                       'clientInfo': {'name': 'live-workflow-acceptance', 'version': '1'}})
    h.protocol_version = initialized['protocolVersion']
    h.notify_initialized()
    h.rpc('tools/list')
    rough = 'Make billing treat our sandbox load tests correctly without changing normal customer charges.'
    started = h.call('bridge_start_task', title=rough, goal=rough, repo='contract/billing',
                     client_key='live-workflow-' + scenario['cohort'])
    task = started['task_id']
    settled = eventually(lambda: (r if not (r := h.call('bridge_start_task', task_id=task)).get('model_pending') else None),
                         'live kickoff model discovery', timeout=timeout)
    discovery = settled.get('discovery') or {}
    if not discovery.get('named_decisions') or discovery.get('model_error'):
        raise AssertionError('Raven did not discover candidate decisions through live inference')
    files = {str(p.relative_to(h.root / 'checkout')): p.read_text() for p in (h.root / 'checkout').rglob('*.py')}
    proposed = agent.complete_json('acceptance_agent_discovery',
        'You are the coding host. Discover one unresolved billing policy decision before editing code. '
        'Read the rough task, actual repository files and Raven kickoff. Choose an existing source path. '
        'Do not invent a human answer or owner. Return JSON with path, question, context.',
        json.dumps({'rough_task': rough, 'files': files, 'raven_kickoff': settled}), max_tokens=1800)
    if not isinstance(proposed, dict) or proposed.get('path') not in files or not proposed.get('question', '').strip():
        raise AssertionError('Live coding host did not discover a valid repository decision')
    if proposed['path'] != 'billing/usage.py':
        raise AssertionError('Live coding host chose a non-billing fixture path')
    report['discovery'] = {'question': proposed['question'], 'path': proposed['path'],
                           'raven_candidates': discovery['named_decisions']}
    node = h.call('bridge_add_node', task_id=task, question=proposed['question'],
                 context=proposed.get('context', ''), paths=proposed['path'], client_ref='live-policy')
    node_id = node['node_id']
    eventually(lambda: not h.call('bridge_get_decision', decision_id=node_id).get('model_pending'),
               'live node inference', timeout=timeout)
    message = eventually(lambda: h.slack.matching_messages(channel='D' + OWNER[1:]), 'inferred owner DM', timeout=timeout)[0]
    stage('natural_referral')
    h.say(message, 'I built the meter, but Marisol Contract handles invoice policy. Could you bring this to her?',
          expected='Pass this question to Marisol Contract')
    if h.call('bridge_get_decision', decision_id=node_id)['authorized']:
        raise AssertionError('Referral proposal authorized the decision')
    h.say(message, 'yes', expected='Marisol Contract')
    referred = eventually(lambda: h.slack.matching_messages(channel='D' + REFERRED[1:]),
                         'referred owner DM', timeout=timeout)[0]
    stage('natural_answer_and_novel_amendment')
    h.say(referred, scenario['base_answer'], user=REFERRED, expected='Record your decision as:')
    _, readback = h.say(referred, scenario['caveat_message'], user=REFERRED, expected='Record your decision as:')
    if scenario['cohort'] not in readback['text'] or str(scenario['threshold']) not in readback['text']:
        raise AssertionError('Live readback omitted the randomized cohort or threshold')
    before = h.call('bridge_get_decision', decision_id=node_id)
    if before['authorized']:
        raise AssertionError('Unconfirmed natural amendment authorized the decision')
    report['readback'] = readback['text']
    stage('adaptive_interview')
    # Synthetic installation credential for the identified human role, never a real account token.
    person = h.store.graph.find_person(REFERRED)
    human_token = h.auth.create_token(person['id'], label='Synthetic interview client', kind='human')['token']
    path = f'/api/tasks/{task}/interviews'
    draft = human_post(h, human_token, path, {'decision_id': node_id, 'client_key': 'novel-interview'})
    draft = human_post(h, human_token, path + '/' + draft['id'] + '/draft',
                       {'expected_version': draft['version'], 'transcript': scenario['interview_response'],
                        'turns': [{'prompt_id': 0, 'response': scenario['interview_response']}],
                        'answer': '', 'rationale': ''})
    guided = human_post(h, human_token, path + '/' + draft['id'] + '/advance', {'expected_version': draft['version']})
    check_guidance(guided['guidance'], scenario)
    if h.call('bridge_get_decision', decision_id=node_id)['authorized']:
        raise AssertionError('Unapproved interview draft authorized the decision')
    report['interview'] = guided['guidance']
    stage('explicit_confirmation_and_agent_resume')
    h.say(referred, 'yes', user=REFERRED, expected='Recorded')
    h.call('bridge_wait', task_id=task, timeout='0')
    tree = h.call('bridge_get_tree', task_id=task)
    signed = h.call('bridge_get_decision', decision_id=node_id)
    if not signed['authorized'] or signed['signed_by'] != 'Marisol Contract':
        raise AssertionError('Explicit confirmation did not produce the attributed signed answer')
    report['signed_answer'] = signed['answer']
    stage('agent_behavior_from_signed_answer')
    implementation = agent.complete_json('acceptance_agent_implementation',
        'You are the coding host resuming from the MCP tree and signed decision. Implement the authorized billing '
        'policy as one Python boolean expression with inputs is_internal (bool), cohort (str), calls (int). '
        'Use only boolean operations, comparisons and constants; no calls, attributes or imports. '
        'Return JSON with billable_expression and rationale. Preserve every exception and exact boundary. '
        'The expression returns True when that usage is billable. The supplied signed answer is the sole policy source.',
        json.dumps({'mcp_tree': tree, 'signed_decision': signed, 'source': files[proposed['path']]}), max_tokens=2000)
    expression = implementation.get('billable_expression') if isinstance(implementation, dict) else None
    cases = check_expression(expression, scenario)
    report['agent_implementation'] = {'expression': expression, 'behavioral_cases_passed': len(cases),
                                     'boundary_cases': cases}
    source = files[proposed['path']]
    changed = 'def is_billable(is_internal, cohort, calls):\n    return ' + expression + '\n'
    (h.root / 'checkout' / proposed['path']).write_text(changed)
    diff = ''.join(difflib.unified_diff(source.splitlines(True), changed.splitlines(True),
                    fromfile='a/' + proposed['path'], tofile='b/' + proposed['path']))
    diff = 'diff --git a/' + proposed['path'] + ' b/' + proposed['path'] + '\n' + diff
    stage('finish_and_proof')
    h.call('bridge_get_tree', task_id=task)
    checks = f'Live generated policy: {len(cases)} boundary cases executed and passed by acceptance host.'
    finished = h.call('bridge_finish_task', task_id=task, diff=diff, checks=checks)
    if finished.get('status') != 'completed':
        raise AssertionError('Approved task did not finish')
    if (finished.get('review') or {}).get('status') == 'running':
        eventually(lambda: (t if (t := h.call('bridge_get_tree', task_id=task)).get('review', {}).get('status') != 'running' else None),
                   'live advisory diff review', timeout=timeout)
        # Same diff/checks reuse the cached review and refresh the proof snapshot.
        finished = h.call('bridge_finish_task', task_id=task, diff=diff, checks=checks)
    report['advisory_review'] = {'status': (finished.get('review') or {}).get('status'),
                                 'follows': finished.get('follows') or []}
    proof = h.call('bridge_export_proof', task_id=task)
    if not proof['integrity']['valid'] or proof['bundle']['payload']['change']['diff'] != diff:
        raise AssertionError('MCP proof did not retain the actual generated change')
    report['proof'] = {'id': proof['bundle']['id'], 'integrity_valid': True,
                       'authenticity_verified': proof['integrity']['authenticity_verified'],
                       'diff_sha256': hashlib.sha256(diff.encode()).hexdigest()}
    report['mcp_tools'] = sorted({t['params']['name'] for t in h.transcript if t.get('method') == 'tools/call'})
    report['status'] = 'passed'
    report['stage'] = 'complete'
    return report


def run(*, run_live=False, max_calls=30, timeout=180, harness_factory=RavenContractHarness, workflow=run_workflow):
    if not run_live:
        return {'status': 'blocked', 'reason': 'Pass --run-live only after secure provider setup and approval.'}
    cfg = load()
    if cfg.model_api != 'anthropic' or not cfg.api_key or not cfg.semantic_retrieval:
        return {'status': 'blocked', 'reason': 'Configure Anthropic securely and enable inference before this run.'}
    budget = RequestBudget(max_calls)
    report = {'status': 'failed', 'stage': 'setup', 'provider': 'anthropic',
              'model': cfg.model, 'fast_model': cfg.fast_model, 'synthetic_slack': True,
              'model_outputs_mocked': False, 'human_messages_synthetic': True,
              'limitations': ['No live Slack workspace or actual human approval.',
                  'Synthetic repository and human inputs; actual live provider inference and authenticated HTTP workflow.',
                  'Behavioral code is a restricted model-generated expression, not a general-purpose autonomous coding run.',
                  'Interview draft is tested for adaptivity and grounding; it is not confirmed as a separate approval.',
                  'Proof integrity and advisory review do not independently verify host test execution.']}
    # Production diagnostics may include provider error text. Keep them out of the public report.
    diagnostics = io.StringIO()
    try:
        with redirect_stdout(diagnostics), redirect_stderr(diagnostics), patch('urllib.request.urlopen', side_effect=budget.open):
            with harness_factory(model_mode='live', request_timeout=timeout, event_timeout=timeout) as h:
                try:
                    workflow(h, llm.Client(load().fast()), novel_scenario(), report, timeout=timeout)
                finally:
                    # Stop further provider attempts before teardown, including retry/background paths.
                    budget.stop()
                    if hasattr(h, 'store'):
                        from bridge import canvas
                        h.store.delivery.close()
                        inbox = h.store.delivery.inbox.thread
                        if inbox is not None:
                            inbox.join(timeout=120)
                        if not canvas.wait_for_background(timeout=120) or (inbox is not None and inbox.is_alive()):
                            raise AssertionError('Inference workers did not stop within the bounded teardown window')
    except Exception as error:
        report.update(status='failed', reason=type(error).__name__)
    finally:
        report.update(budget.summary())
    if report['status'] == 'passed' and not report['physical_http_requests']:
        report.update(status='failed', reason='No actual provider HTTP request was observed')
    # Defensive final scrub; no credentials or environment dump belongs in artifacts.
    return json.loads(json.dumps(report).replace(cfg.api_key, '[redacted]'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live', action='store_true')
    parser.add_argument('--max-calls', type=int, default=30, choices=range(1, 101), metavar='1..100')
    parser.add_argument('--timeout', type=int, default=180, choices=range(15, 601), metavar='15..600')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    result = run(run_live=args.run_live, max_calls=args.max_calls, timeout=args.timeout)
    rendered = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end='')
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
