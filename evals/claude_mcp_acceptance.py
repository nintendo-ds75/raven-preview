"""Opt-in official Claude Agent SDK -> stdio -> authenticated Raven MCP acceptance.

People, repository, Slack transport, and the host's Raven ladder are deterministic
fixtures. Live mode additionally requires a production-model task inquiry. The
live host is never replaced with canned model output. No native
host tools are enabled; a second MCP server exposes three bounded fixture files
and a validated, expression-only Python policy. Scripted mode tests the driver,
and is explicitly not a live-host acceptance result.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
from contextlib import ExitStack
import difflib
import getpass
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.live_workflow_acceptance import AcceptanceFailure, check_expression, novel_scenario
from evals.real_oss_remote.run_host import read_trace, trace_state
from evals.slack_contract_harness import OWNER, RavenContractHarness, SlackContractServer, http_json

SDK_VERSION = '0.2.165'
REPO = 'contract/billing'
POLICY = 'billing/usage.py'
BASELINE = 'def is_billable(is_internal, cohort, calls):\n    return True\n'
READABLE = {POLICY, 'README.md', 'CODEOWNERS'}
RAVEN_TOOLS = {'bridge_start_task', 'bridge_add_node', 'bridge_get_tree', 'bridge_get_decision',
               'bridge_wait', 'bridge_settle_node', 'bridge_finish_task', 'bridge_export_proof',
               'bridge_search_decisions', 'bridge_search_context', 'bridge_get_record',
               'bridge_lookup_record', 'bridge_list_owners', 'bridge_context_sources',
               'bridge_review_discovery', 'bridge_connection_status'}
FIXTURE_TOOLS = {'read_file', 'write_file', 'run_checks', 'read_changes'}
PROMPT = '''Implement the current production invoice policy for internal load-test traffic, preserving
real customer billing. Inspect the tiny checkout with fixture tools and the recorded Raven
context/prior decisions. Discover the unresolved policy judgment yourself; do not assume
historical or another environment's answer is approval. Start one Raven task with
repo=contract/billing, client_key=claude-mcp-acceptance, and the complete request as goal.
Keep all work on that task. Use current facts environment=production on policy nodes.
Read source evidence and prior decisions before asking the routed owner. Use bridge_wait
with timeout=60 when blocked. The evaluator will simulate a human in Raven only after
you wait. Slack is notification-only. Never attempt to act as a human or self-authorize.
Your first host process may stop while waiting; resume the existing task and read its tree.
Implement the final explicitly signed answer in billing/usage.py as one function:
is_billable(is_internal, cohort, calls), containing only a single return expression of
booleans/comparisons/constants. No calls, imports, attributes, helpers, or side effects.
Use fixture run_checks and read_changes. Finish via bridge_finish_task with the exact
diff returned by read_changes and honest checks. Do not invent hidden test results.
'''


def require(condition, code):
    if not condition:
        raise AcceptanceFailure(code)


DIAGNOSTIC_MODULES = ('claude_agent_sdk', 'anyio', 'asyncio', 'httpx', 'httpcore', 'mcp',
                      'pydantic', 'typing', 'typing_extensions', 'sniffio', 'exceptiongroup',
                      'contextlib', 'subprocess', 'json', 'collections', 'enum', 'pathlib',
                      'os', 'importlib', 'sys', 'builtins', 'evals.claude_mcp_acceptance',
                      'evals.live_workflow_acceptance', 'evals.customer_provider_gateway')


def diagnostic_module(name):
    if name == '__main__':
        return 'evals.claude_mcp_acceptance'
    if isinstance(name, str) and len(name) <= 160 and any(name == p or name.startswith(p + '.') for p in DIAGNOSTIC_MODULES):
        return name
    return 'other'


def exception_diagnostic(error, *, depth=0, seen=None):
    """Never stringify exceptions, source lines, filenames, arguments or locals."""
    seen = set() if seen is None else seen
    if id(error) in seen or depth >= 4:
        return {'truncated': True}
    seen.add(id(error))
    module = diagnostic_module(type(error).__module__)
    name = type(error).__name__
    result = {'class': name if module != 'other' and name.isidentifier() and len(name) <= 100 else 'ExternalException',
              'module': module, 'frames': []}
    trace = error.__traceback__
    while trace is not None:
        result['frames'].append({'module': diagnostic_module(trace.tb_frame.f_globals.get('__name__')),
                                 'line': trace.tb_lineno})
        trace = trace.tb_next
    result['frames'] = result['frames'][-12:]
    for attr in ('exit_code', 'returncode', 'errno'):
        value = getattr(error, attr, None)
        if type(value) is int and -1000000 <= value <= 1000000:
            result[attr] = value
    cause = error.__cause__ if error.__cause__ is not None else error.__context__
    if cause is not None:
        result['cause'] = exception_diagnostic(cause, depth=depth + 1, seen=seen)
    if isinstance(error, BaseExceptionGroup):
        result['children'] = [exception_diagnostic(child, depth=depth + 1, seen=seen) for child in error.exceptions[:4]]
        result['omitted_children'] = max(0, len(error.exceptions) - 4)
    return result


def sanitized_host_event(event):
    """Persist only known structural fields, never arbitrary subprocess output."""
    if not isinstance(event, dict):
        return {'kind': 'protocol_error', 'code': 'non_object_event'}
    kind = event.get('kind')
    if kind == 'error':
        result = {'kind': 'error', 'code': 'sdk_worker_failed'}
        # Diagnostics originate in our worker; still project every field again.
        def project(value, depth=0):
            if not isinstance(value, dict) or depth >= 4:
                return {'truncated': True}
            module = diagnostic_module(value.get('module'))
            name = value.get('class', '')
            output = {'module': module, 'class': name if module != 'other' and isinstance(name, str)
                      and name.isidentifier() and len(name) <= 100 else 'ExternalException'}
            output['frames'] = [{'module': diagnostic_module(f.get('module')), 'line': f['line']}
                                for f in value.get('frames', [])[-12:] if isinstance(f, dict)
                                and type(f.get('line')) is int and f['line'] >= 0]
            for key in ('exit_code', 'returncode', 'errno', 'omitted_children'):
                if type(value.get(key)) is int:
                    output[key] = value[key]
            if 'cause' in value:
                output['cause'] = project(value['cause'], depth + 1)
            if isinstance(value.get('children'), list):
                output['children'] = [project(c, depth + 1) for c in value['children'][:4]]
            return output
        if 'diagnostic' in event:
            result['diagnostic'] = project(event['diagnostic'])
        return result
    if kind in ('session', 'result'):
        result = {'kind': kind}
        for key in ('id', 'session_id'):
            value = event.get(key)
            if isinstance(value, str) and re.fullmatch(r'[0-9a-fA-F-]{32,36}', value):
                result[key] = value
        if kind == 'result':
            statuses = {'success', 'error_during_execution', 'error_max_turns', 'error_max_budget_usd',
                        'error_max_structured_output_retries'}
            result['status'] = event.get('status') if event.get('status') in statuses else 'other'
            result['is_error'] = event.get('is_error') is True
            for key in ('cost_usd', 'turns'):
                if type(event.get(key)) in (float, int) and 0 <= event[key] < 1e9:
                    result[key] = event[key]
            usage = event.get('usage') or {}
            result['usage'] = {key: usage[key] for key in ('input_tokens', 'output_tokens',
                'cache_creation_input_tokens', 'cache_read_input_tokens') if isinstance(usage, dict)
                and type(usage.get(key)) is int and usage[key] >= 0}
        return result
    return {'kind': 'protocol_error', 'code': 'unknown_event_kind'}


def policy_expression(source):
    """Validate actual host-written bytes before any evaluation or write."""
    require(isinstance(source, str) and len(source) <= 12000, 'policy_size')
    try:
        module = ast.parse(source)
    except SyntaxError:
        raise AcceptanceFailure('policy_syntax') from None
    require(len(module.body) == 1 and isinstance(module.body[0], ast.FunctionDef), 'policy_function')
    func = module.body[0]
    args = func.args
    require(func.name == 'is_billable' and not func.decorator_list and not func.returns
            and not args.posonlyargs and not args.kwonlyargs and not args.vararg and not args.kwarg
            and not args.defaults and not args.kw_defaults
            and [a.arg for a in args.args] == ['is_internal', 'cohort', 'calls']
            and not any(a.annotation for a in args.args)
            and len(func.body) == 1 and isinstance(func.body[0], ast.Return)
            and func.body[0].value is not None, 'policy_contract')
    expression = ast.unparse(func.body[0].value)
    allowed = (ast.Expression, ast.BoolOp, ast.UnaryOp, ast.Compare, ast.Name, ast.Load,
               ast.Constant, ast.And, ast.Or, ast.Not, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
               ast.Gt, ast.GtE, ast.IfExp)
    for item in ast.walk(ast.parse(expression, mode='eval')):
        require(isinstance(item, allowed), 'policy_operation')
        if isinstance(item, ast.Name):
            require(item.id in {'is_internal', 'cohort', 'calls'}, 'policy_name')
        if isinstance(item, ast.Constant):
            require(type(item.value) in (bool, int, str), 'policy_literal')
    return expression


def fixture_call(root, name, args):
    root = Path(root).resolve()
    if name in ('read_file', 'write_file'):
        path = args.get('path')
        require(path in READABLE, 'fixture_path_denied')
        target = root / path
        require(not target.is_symlink() and target.resolve().is_relative_to(root), 'fixture_path_denied')
        if name == 'read_file':
            return {'path': path, 'content': target.read_text()}
        require(path == POLICY, 'fixture_write_denied')
        policy_expression(args.get('content'))
        target.write_text(args['content'])
        return {'written': path}
    if name == 'read_changes':
        source = (root / POLICY).read_text()
        diff = ''.join(difflib.unified_diff(BASELINE.splitlines(True), source.splitlines(True),
                                          fromfile='a/' + POLICY, tofile='b/' + POLICY))
        return {'diff': ('diff --git a/' + POLICY + ' b/' + POLICY + '\n' + diff) if diff else ''}
    if name == 'run_checks':
        expression = policy_expression((root / POLICY).read_text())
        code = compile(expression, '<fixture-policy>', 'eval')
        for internal in (False, True):
            actual = eval(code, {'__builtins__': {}}, {'is_internal': internal, 'cohort': 'ordinary', 'calls': 0})
            require(type(actual) is bool, 'public_check_not_boolean')
            if not internal:
                require(actual, 'public_check_customer_traffic')
        return {'passed': 2, 'scope': 'Public interface and real-customer smoke checks; policy boundaries are hidden.'}
    raise AcceptanceFailure('fixture_tool_denied')


def fixture_mcp():
    """Minimal external stdio MCP server, with no generic code/shell facility."""
    root = os.environ['RAVEN_ACCEPTANCE_CHECKOUT']
    specs = [
        ('read_file', 'Read a fixture file: billing/usage.py, README.md, CODEOWNERS.', {'path': {'type': 'string'}}, ['path']),
        ('write_file', 'Replace billing/usage.py with one validated expression-only policy function.',
         {'path': {'type': 'string'}, 'content': {'type': 'string'}}, ['path', 'content']),
        ('run_checks', 'Run public smoke checks against the actual policy file.', {}, []),
        ('read_changes', 'Read the exact patch to submit to Raven.', {}, []),
    ]
    for line in sys.stdin:
        request = json.loads(line)
        if 'id' not in request:
            continue
        method = request.get('method')
        if method == 'initialize':
            result = {'protocolVersion': request.get('params', {}).get('protocolVersion', '2024-11-05'),
                      'capabilities': {'tools': {}}, 'serverInfo': {'name': 'bounded-fixture', 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': [{'name': n, 'description': d, 'inputSchema': {'type': 'object',
                       'properties': p, 'required': r, 'additionalProperties': False}} for n, d, p, r in specs]}
        elif method == 'ping':
            result = {}
        elif method == 'tools/call':
            try:
                value = fixture_call(root, request['params']['name'], request['params'].get('arguments', {}))
                result = {'content': [{'type': 'text', 'text': json.dumps(value)}]}
            except Exception as error:
                result = {'isError': True, 'content': [{'type': 'text', 'text':
                          error.code if isinstance(error, AcceptanceFailure) else 'fixture_operation_failed'}]}
        else:
            print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32601, 'message': 'Unknown method'}}), flush=True)
            continue
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)


def host_environment(gateway, home):
    env = {k: os.environ[k] for k in ('PATH', 'LANG') if k in os.environ}
    python_paths = [str(ROOT)]
    try:
        distribution = importlib.metadata.distribution('claude-agent-sdk')
        sdk_root = Path(distribution.locate_file('')).resolve()
        if (sdk_root / 'claude_agent_sdk').is_dir() and str(sdk_root) not in python_paths:
            # Preserve precisely the installed SDK's site directory ahead of
            # global packages. Empty HOME can otherwise select an older SDK.
            python_paths.append(str(sdk_root))
    except importlib.metadata.PackageNotFoundError:
        pass
    env.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(home / 'claude'), PYTHONPATH=os.pathsep.join(python_paths),
               PYTHONUNBUFFERED='1', ANTHROPIC_BASE_URL=gateway.url,
               ANTHROPIC_API_KEY=gateway.placeholder, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
               CLAUDE_CODE_MAX_OUTPUT_TOKENS='1024')
    return env


def sdk_child_preflight(model):
    """Check the actual isolated child's package/version/options without inference."""
    from types import SimpleNamespace
    with tempfile.TemporaryDirectory(prefix='raven-sdk-preflight-') as tmp:
        home = Path(tmp)
        config = {'raven_env': {}, 'checkout': str(home), 'home': str(home), 'model': model,
                  'max_turns': 1, 'budget_usd': .01, 'session_id': ''}
        dummy = SimpleNamespace(url='http://127.0.0.1:1', placeholder='offline-preflight-placeholder')
        process = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--sdk-preflight'],
            input=json.dumps(config) + '\n', text=True, capture_output=True, timeout=20,
            cwd=home, env=host_environment(dummy, home))
        try:
            result = json.loads(process.stdout)
        except ValueError:
            return {'status': 'blocked', 'code': 'sdk_child_preflight_no_receipt', 'returncode': process.returncode}
        if (process.returncode == 0 and result.get('version') == SDK_VERSION
                and result.get('options_constructed') is True):
            return {'status': 'passed', 'version': SDK_VERSION, 'options_constructed': True}
        observed = result.get('version')
        observed = observed if isinstance(observed, str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', observed) else 'unavailable'
        return {'status': 'blocked', 'code': 'sdk_child_preflight_failed', 'returncode': process.returncode,
                'observed_version': observed, 'required_version': SDK_VERSION,
                'event': sanitized_host_event(result)}


class OfflineSDKTranscript:
    """Synthetic Anthropic wire replies for the real SDK, never live acceptance."""
    def __init__(self):
        self.calls = 0
        self.task_id = ''
        self.errors = []
        self.inquiry_calls = 0
        self.current_answer = ''
        self.actual_diff = ''

    @staticmethod
    def objects(value, depth=0):
        if depth > 24:
            return []
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return []
        if isinstance(value, dict):
            return [value] + [item for child in value.values() for item in OfflineSDKTranscript.objects(child, depth + 1)]
        if isinstance(value, list):
            return [item for child in value for item in OfflineSDKTranscript.objects(child, depth + 1)]
        return []

    @staticmethod
    def task_ids(value, depth=0):
        if depth > 24:
            return []
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return []
        if isinstance(value, dict):
            own = [value['task_id']] if isinstance(value.get('task_id'), str) else []
            return own + [item for child in value.values() for item in OfflineSDKTranscript.task_ids(child, depth + 1)]
        if isinstance(value, list):
            return [item for child in value for item in OfflineSDKTranscript.task_ids(child, depth + 1)]
        return []

    def __call__(self, request):
        import httpx
        if request.url.host != 'api.anthropic.com' or request.url.path not in ('/v1/messages', '/v1/messages/count_tokens'):
            self.errors.append('unexpected_destination')
            return httpx.Response(403, json={'error': {'type': 'invalid_request_error', 'message': 'Offline destination refused'}})
        if request.url.path.endswith('/count_tokens'):
            return httpx.Response(200, json={'input_tokens': 1})
        payload = json.loads(request.content)
        if not payload.get('tools') and not payload.get('stream'):
            # Raven's question-only production client has no tools. The exact
            # reader and support-check outputs are synthetic, clearly reported.
            prompts = [m.get('content', '') for m in payload.get('messages', []) if m.get('role') == 'user']
            packets = self.objects(prompts)
            evidence = next((p['evidence'] for p in packets if isinstance(p.get('evidence'), list)), None)
            if evidence is not None:
                node = next((e for e in evidence if e.get('kind') == 'task_decision' and 'Current answer:' in e.get('text', '')), None)
                require(node is not None, 'offline_inquiry_missing_decision_evidence')
                raw = {'outcome': 'supported_answer', 'answer': node['text'],
                       'citations': [{'id': node['id'], 'quote': node['text']}], 'open': []}
            else:
                prompt = next((p for p in prompts if isinstance(p, str) and '\n\nANSWER: ' in p), '')
                require(bool(prompt), 'offline_inquiry_unexpected_prompt')
                raw = {'claims': [{'claim': prompt.split('\n\nANSWER: ', 1)[1], 'support': 'stated'}]}
            self.inquiry_calls += 1
            return httpx.Response(200, json={'id': 'msg_offline_inquiry_' + str(self.inquiry_calls),
                'type': 'message', 'role': 'assistant', 'model': payload['model'],
                'content': [{'type': 'text', 'text': json.dumps(raw)}], 'stop_reason': 'end_turn',
                'usage': {'input_tokens': 1, 'output_tokens': 1}})
        packets = self.objects(payload.get('messages', []))
        for packet in packets:
            if packet.get('authorized') is True and packet.get('signed_by') and isinstance(packet.get('answer'), str):
                answer = packet['answer']
                if re.search(r'\bpilot_[0-9a-f]{8}\b', answer) and re.search(r'more than \d+', answer):
                    self.current_answer = answer
            if isinstance(packet.get('diff'), str) and packet['diff'].startswith('diff --git a/' + POLICY):
                self.actual_diff = packet['diff']
        if not self.task_id:
            ids = self.task_ids(payload.get('messages', []))
            if ids:
                self.task_id = ids[0]
        steps = [
            ('bridge_start_task', {'title': 'Production internal load-test invoice policy', 'goal': PROMPT,
                                  'repo': REPO, 'paths': POLICY, 'client_key': 'claude-mcp-acceptance'}),
            ('bridge_search_context', {'repo': REPO, 'task_id': self.task_id, 'query': 'Production billing context'}),
            ('bridge_search_decisions', {'repo': REPO, 'query': 'internal load-test traffic billable'}),
            ('bridge_add_node', {'task_id': self.task_id, 'question': 'Should internal load-test traffic count on production customer invoices?',
                                 'paths': POLICY, 'category': 'policy', 'facts': 'environment=production', 'client_ref': 'production-policy'}),
            ('bridge_get_tree', {'task_id': self.task_id}),
            ('bridge_wait', {'task_id': self.task_id, 'timeout': '60'}),
            ('bridge_get_tree', {'task_id': self.task_id}),
            ('fixture:write_file', {}),
            ('fixture:run_checks', {}),
            ('fixture:read_changes', {}),
            ('bridge_get_tree', {'task_id': self.task_id}),
            ('bridge_finish_task', {'task_id': self.task_id, 'diff': self.actual_diff,
                                    'checks': 'Actual fixture run_checks passed 2 public smoke checks. Hidden boundary checks are evaluator-owned.'}),
            ('final', {}),
        ]
        if self.calls >= len(steps) or (self.calls > 0 and not self.task_id):
            self.errors.append('unexpected_model_turn' if self.task_id else 'missing_task_tool_result')
            return httpx.Response(400, json={'error': {'type': 'invalid_request_error', 'message': 'Offline transcript exhausted'}})
        name, args = steps[self.calls]
        if name == 'fixture:write_file':
            require(bool(self.current_answer), 'offline_resume_missing_signed_tool_answer')
            cohort = re.search(r'\bpilot_[0-9a-f]{8}\b', self.current_answer)[0]
            threshold = int(re.search(r'more than (\d+)', self.current_answer)[1])
            args = {'path': POLICY, 'content': 'def is_billable(is_internal, cohort, calls):\n'
                    f'    return not is_internal or (cohort == {cohort!r} and calls > {threshold})\n'}
        if name == 'bridge_finish_task':
            require(bool(self.actual_diff), 'offline_finish_missing_actual_tool_diff')
        self.calls += 1
        if name == 'final':
            block = {'type': 'text', 'text': 'The synthetic fixture policy is implemented and recorded with the human decision. Public smoke checks passed; independent boundary verification belongs to the evaluator.'}
            stop_reason = 'end_turn'
            delta = {'type': 'text_delta', 'text': block['text']}
            start_block = {'type': 'text', 'text': ''}
        else:
            tool_name = ('mcp__fixture__' + name.split(':', 1)[1]) if name.startswith('fixture:') else 'mcp__raven__' + name
            block = {'type': 'tool_use', 'id': 'toolu_offline_' + str(self.calls), 'name': tool_name, 'input': args}
            stop_reason = 'tool_use'
            delta = {'type': 'input_json_delta', 'partial_json': json.dumps(args)}
            start_block = {**block, 'input': {}}
        message = {'id': 'msg_offline_' + str(self.calls), 'type': 'message', 'role': 'assistant',
                   'model': payload['model'], 'content': [block], 'stop_reason': stop_reason,
                   'stop_sequence': None, 'usage': {'input_tokens': 1, 'output_tokens': 1}}
        if not payload.get('stream'):
            return httpx.Response(200, json=message)
        events = [
            ('message_start', {'type': 'message_start', 'message': {**message, 'content': [], 'stop_reason': None,
                                                                   'usage': {'input_tokens': 1, 'output_tokens': 0}}}),
            ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': start_block}),
            ('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                                     'delta': delta}),
            ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
            ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None},
                               'usage': {'output_tokens': 1}}),
            ('message_stop', {'type': 'message_stop'}),
        ]
        content = ''.join('event: ' + name + '\ndata: ' + json.dumps(value) + '\n\n' for name, value in events)
        return httpx.Response(200, content=content.encode(), headers={'Content-Type': 'text/event-stream'})


def offline_sdk_preflight(*, output_dir=None, model='claude-sonnet-4-6', timeout=60):
    """Real SDK process and real MCP; all provider responses are MockTransport."""
    import httpx
    from evals.customer_provider_gateway import Gateway
    checked = sdk_child_preflight(model)
    if checked['status'] != 'passed':
        return {**checked, 'mode': 'offline_sdk_fixture', 'live_provider': False}
    report = {'status': 'failed', 'mode': 'offline_sdk_fixture', 'live_provider': False,
              'provider_responses': 'httpx.MockTransport synthetic SSE', 'sdk_child_preflight': checked}
    with ExitStack() as stack:
        root = Path(output_dir) if output_dir else Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='raven-offline-sdk-')))
        if root.exists() and any(root.iterdir()):
            return {**report, 'status': 'blocked', 'code': 'output_directory_not_empty'}
        root.mkdir(parents=True, exist_ok=True)
        transcript = OfflineSDKTranscript()
        host = None
        gateway = None
        try:
            with Gateway('synthetic-offline-key', model=model, max_calls=12, max_input_bytes=4000000,
                         max_output_tokens=20000, transport=httpx.MockTransport(transcript)) as gateway:
                clean = {k: v for k, v in os.environ.items() if not any(x in k for x in ('TOKEN', 'SECRET', 'API_KEY'))
                         and not k.startswith(('BRIDGE_', 'SLACK_', 'TEAMS_', 'GITHUB_', 'RAVEN_'))}
                with patch.dict(os.environ, clean, clear=True), Fixture(directory=root / 'fixture') as fixture:
                    fixture.prepare()
                    host = SDKHost(gateway, root, model, max_turns=12)
                    try:
                        host.start(fixture, 'initial')
                        deadline = time.monotonic() + timeout
                        completed, waits = wait_until(lambda: (
                            state if (state := trace_state(read_trace(root / 'mcp-trace.jsonl')))[1] else None),
                            host, deadline, 'offline_sdk_pending_wait')
                        calls = [r['message']['params']['name'] for r, _, _ in completed]
                        require(all(name in calls for name in ('bridge_start_task', 'bridge_search_context',
                            'bridge_search_decisions', 'bridge_add_node', 'bridge_get_tree')), 'offline_sdk_missing_mcp_stage')
                        require(bool(host.session_id), 'offline_sdk_missing_session')
                        time.sleep(1.2)
                        require(any(w in trace_state(read_trace(root / 'mcp-trace.jsonl'))[1] for w in waits),
                                'offline_sdk_wait_not_pending')
                        require(not transcript.errors, 'offline_sdk_transcript_error')
                        report.update(status='offline_passed', sdk_process_executed=True,
                                      authenticated_mcp_pending_wait=True, pending_seconds=1.2,
                                      completed_mcp_tools=calls, synthetic_model_turns=transcript.calls)
                    finally:
                        host.stop()
                    report['gateway'] = gateway.snapshot()
        except Exception as error:
            report.update(status='blocked', failure_code=error.code if isinstance(error, AcceptanceFailure) else 'offline_sdk_error',
                          diagnostic=exception_diagnostic(error), synthetic_transcript_errors=transcript.errors)
        finally:
            if host:
                host.stop()
                report['host_diagnostics'] = host.diagnostics()
            if gateway is not None:
                report['gateway'] = gateway.snapshot()
        if output_dir:
            (root / 'offline-sdk-result.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def sdk_options(config):
    """Construct pinned official options without starting a host or inference."""
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, PermissionResultDeny
    allowed = {'mcp__raven__' + n for n in RAVEN_TOOLS} | {'mcp__fixture__' + n for n in FIXTURE_TOOLS}
    async def before_tool(payload, *_):
        if payload.get('tool_name') not in allowed:
            return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                          'permissionDecisionReason': 'Only bounded evaluation MCP tools are enabled.'}}
        return {}
    async def deny(*_):
        return PermissionResultDeny(message='Native tools are unavailable.')
    return ClaudeAgentOptions(tools=[], allowed_tools=sorted(allowed),
        mcp_servers={'raven': {'command': sys.executable, 'args': [str(ROOT / 'evals/real_oss_remote/mcp_proxy.py')],
                              'env': config['raven_env']},
                     'fixture': {'command': sys.executable, 'args': [str(Path(__file__).resolve()), '--fixture-mcp'],
                                 'env': {'RAVEN_ACCEPTANCE_CHECKOUT': config['checkout']}}},
        strict_mcp_config=True, permission_mode='dontAsk', can_use_tool=deny,
        hooks={'PreToolUse': [HookMatcher(hooks=[before_tool])]}, setting_sources=[], plugins=[], agents={}, skills=[],
        cwd=config['home'], model=config['model'], max_turns=config['max_turns'],
        max_budget_usd=config['budget_usd'], resume=config.get('session_id') or None)


async def sdk_worker(config):
    require(importlib.metadata.version('claude-agent-sdk') == SDK_VERSION, 'sdk_worker_version_changed')
    from claude_agent_sdk import ClaudeSDKClient, SystemMessage, ResultMessage
    options = sdk_options(config)
    async with ClaudeSDKClient(options=options) as client:
        await client.query(config['prompt'])
        async for message in client.receive_response():
            if isinstance(message, SystemMessage) and message.subtype == 'init':
                print(json.dumps({'kind': 'session', 'id': message.data['session_id']}), flush=True)
            elif isinstance(message, ResultMessage):
                print(json.dumps({'kind': 'result', 'session_id': message.session_id,
                                  'status': message.subtype, 'is_error': message.is_error,
                                  'cost_usd': message.total_cost_usd, 'usage': message.usage,
                                  'turns': message.num_turns}), flush=True)


class SDKHost:
    live = True
    def __init__(self, gateway, root, model, budget_usd=2.0, max_turns=30):
        self.gateway, self.root, self.model = gateway, root, model
        self.budget_usd, self.max_turns = budget_usd, max_turns
        self.session_id = ''
        self.process = None
        self.events = []
        self.thread = None
        self.phases = []

    def start(self, fixture, phase):
        self.phases.append({'phase': phase if phase in ('initial', 'resume') else 'other',
                            'event_start': len(self.events), 'returncode': None})
        home = self.root / 'host-home'
        home.mkdir(mode=0o700, exist_ok=True)
        config = {'checkout': str(fixture.root / 'checkout'), 'home': str(home), 'model': self.model,
                  'max_turns': self.max_turns, 'budget_usd': self.budget_usd, 'session_id': self.session_id,
                  'prompt': PROMPT if phase == 'initial' else 'Resume the same task/client key. Read bridge_get_tree now, then implement only the current signed answer. ' + PROMPT,
                  'raven_env': {'BRIDGE_TOKEN': fixture.agent_token, 'BRIDGE_EVAL_URL': fixture.url,
                                'BRIDGE_EVAL_TRACE': str(self.root / 'mcp-trace.jsonl')}}
        self.process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--sdk-worker'],
            cwd=home, env=host_environment(self.gateway, home), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        self.process.stdin.write(json.dumps(config) + '\n')
        self.process.stdin.flush()
        def read():
            for line in self.process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                event = sanitized_host_event(event)
                self.events.append(event)
                if event.get('kind') == 'session' and event.get('id'):
                    self.session_id = event['id']
        self.thread = threading.Thread(target=read, daemon=True)
        self.thread.start()

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def stop(self):
        if self.alive():
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        if self.thread:
            self.thread.join(timeout=3)
        if self.process:
            for stream in (self.process.stdin, self.process.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
        if self.phases:
            self.phases[-1]['returncode'] = self.process.poll() if self.process is not None else None
            self.phases[-1]['event_end'] = len(self.events)
        self.persist_diagnostics()

    def diagnostics(self):
        return {'phases': self.phases, 'events': self.events[-200:],
                'omitted_events': max(0, len(self.events) - 200), 'raw_stderr_retained': False}

    def persist_diagnostics(self):
        (self.root / 'host-diagnostics.json').write_text(json.dumps(self.diagnostics(), indent=2) + '\n')


class Fixture(RavenContractHarness):
    def __init__(self, directory=None, *, model_mode='fixture'):
        require(model_mode == 'fixture', 'fixture_mode_unknown')
        self.request_timeout, self.event_timeout = 120, 15
        self.stack = ExitStack()
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='raven-claude-contract-'))
        self.root = Path(directory or self.temp)
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'raven.db'
        self.slack = SlackContractServer()
        self.stack.callback(self.slack.close)
        self.stack.enter_context(patch.dict(os.environ, {'BRIDGE_MODEL_API': 'none', 'BRIDGE_SEMANTIC': '0',
            'BRIDGE_LIVE': '0', 'BRIDGE_DEEP': '0', 'BRIDGE_PUBLIC_URL': '', 'GITHUB_TOKEN': ''}))
        self.rpc_id, self.protocol_version, self.transcript = 0, '', []
        self.server = self.store = None
        self.agent_token = ''
        try:
            self.start()
        except BaseException:
            self.close()
            raise

    def start(self, *, workers=True):
        from bridge import server
        original = server.make_server
        def with_wait_cap(*args, **kwargs):
            return original(*args, **{**kwargs, 'wait_cap': 65})
        with patch.object(server, 'make_server', side_effect=with_wait_cap):
            super().start(workers=False)
        self.store.graph.set_setting('slack_interaction_mode', '')
        self.store.delivery.base_url = 'https://raven.acceptance.invalid'
        # A real long poll is necessary: returning every second wakes the host.

    def stop(self):
        if self.store:
            self.store.delivery.close()
            inbox = self.store.delivery.inbox.thread
            if inbox is not None:
                inbox.join(timeout=5)
        super().stop()

    def prepare(self):
        (self.root / 'checkout' / POLICY).write_text(BASELINE)
        (self.root / 'checkout/README.md').write_text('Production billing policy. Internal-load exceptions require current owner judgment.\n')
        person = self.store.graph.find_person(OWNER)
        self.owner_name = person['name']
        self.human_token = self.auth.create_token(person['id'], 'Synthetic owner', kind='human')['token']
        self.store.graph.add_authority('path', 'billing/*', 'decides', person_id=person['id'],
                                      repo=REPO, source='config', asserted_by='acceptance fixture', accepted=True)
        self.source = self.store.add_record({'repo': REPO, 'kind': 'doc', 'ref': 'BILLING-CONTEXT',
            'title': 'Production billing context', 'body': 'Real customer traffic must remain billable. Internal-load exceptions need current owner judgment.',
            'author': self.owner_name, 'status': 'Open', 'resolved': False, 'paths': [POLICY]})['source']
        old = self.store.add_run({'title': 'Historical sandbox-only billing decision', 'repo': REPO})['id']
        node = self.store.graph.add_decision(old, 'Should internal load-test traffic be billable in sandbox?',
            'policy', 'pending', repo=REPO, owner=self.owner_name, path=POLICY)
        self.human('/api/decisions/' + node + '/answer', {
            'answer': 'All sandbox internal-load traffic is excluded. Production is not decided here.',
            'applicability': {'requires': {'environment': 'sandbox'}},
            'expected_updated_at': self.store.get_decision(node)['updated_at']})
        self.prior_id = node
        self.store.delivery.deliver_now()
        self.slack.messages.clear()

    def human(self, path, data):
        status, value, _ = http_json(self.url + path, data, {'Authorization': 'Bearer ' + self.human_token})
        require(status == 200, 'synthetic_human_http_refused')
        return value


def provider_snapshot(gateway):
    return gateway.snapshot() if gateway is not None else {'scripted': True}


def direct_inference_smoke(api_key, workspace_id='', model='claude-sonnet-4-6'):
    """One production logical call; its built-in retry policy permits four attempts."""
    from bridge import llm
    from bridge.config import Config
    require(callable(getattr(llm, '_open_model_request', None)), 'production_transport_hardening_missing')
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith('_proxy')
           and k not in ('SSL_CERT_FILE', 'SSL_CERT_DIR', 'CURL_CA_BUNDLE', 'REQUESTS_CA_BUNDLE')}
    env.update(ANTHROPIC_API_KEY=api_key, ANTHROPIC_WORKSPACE_ID=workspace_id)
    with patch.dict(os.environ, env, clear=True):
        payload = llm.Client(Config(model_api='anthropic', model=model, fast_model=model))._messages(
            'Reply exactly RAVEN_DIRECT_OK.', 'Reply with the marker.', 32)
    text = ''.join(b.get('text', '') for b in payload.get('content', []) if b.get('type') == 'text')
    require(text.strip() == 'RAVEN_DIRECT_OK', 'direct_production_inference_marker')
    return {'status': 'passed', 'transport': 'unmodified bridge.llm.Client._messages',
            'source_sha256': hashlib.sha256((ROOT / 'bridge/llm.py').read_bytes()).hexdigest(),
            'model': model, 'logical_calls': 1, 'maximum_physical_attempts': 4,
            'max_tokens_per_attempt': 32, 'usage': {k: int(payload.get('usage', {}).get(k, 0))
            for k in ('input_tokens', 'output_tokens')}, 'marker_verified': True}


def live_inquiry(fixture, task, node, gateway, model):
    tables = ('runs', 'decisions', 'host_sessions')
    def counts():
        result = {t: fixture.store.graph.db.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in tables}
        result['signatures'] = [tuple(r) for r in fixture.store.graph.db.execute(
            'SELECT id,signed_by,signed_hash,signatures FROM decisions ORDER BY id')]
        return result
    before = counts()
    env = {'BRIDGE_MODEL_API': 'anthropic', 'BRIDGE_MODEL': model, 'BRIDGE_FAST_MODEL': model,
           'BRIDGE_SEMANTIC': '1', 'ANTHROPIC_API_KEY': gateway.placeholder}
    with patch.dict(os.environ, env), patch('bridge.llm._open_model_request', new=gateway.urllib_open):
        result = fixture.human('/api/tasks/' + task + '/inquiry', {
            'question': 'What is the signed production invoice policy, including the exact cohort and call-count boundary?',
            'focus_id': node})
    require(counts() == before, 'live_inquiry_mutated_authority')
    require(result.get('model_used') is True and result.get('authorized') is False,
            'live_inquiry_not_verified_model_reading')
    require(bool(result.get('citations')) and all(c['id'] in (task, node) for c in result['citations']),
            'live_inquiry_missing_task_citations')
    require(fixture.prior_id not in {e['id'] for e in result['evidence']}, 'live_inquiry_neighbor_leak')
    return {'status': 'passed', 'mode': 'real_production_inference_authenticated_http',
            'model_used': True, 'authorized': False, 'counts_unchanged': True,
            'answer': result['answer'], 'citations': result['citations']}


def freshness_checks():
    """Existing deterministic source-version contracts, never live coverage."""
    env = {k: os.environ[k] for k in ('PATH', 'LANG') if k in os.environ}
    env.update(PYTHONPATH=str(ROOT / 'tests') + os.pathsep + str(ROOT), BRIDGE_MODEL_API='none', BRIDGE_SEMANTIC='0')
    tests = ['test_context_memory.ContextMemoryTests.test_second_support_update_invalidates_signoff_and_descendants',
             'test_context_memory.ContextMemoryTests.test_reapproval_requires_explicit_current_pins_and_fresh_revision',
             'test_context_memory.ContextMemoryTests.test_source_A_B_A_revert_never_revives_the_signature_on_A',
             'test_context_connectors.ConnectorTests.test_expiry_fails_closed_even_when_worker_is_not_running']
    proc = subprocess.run([sys.executable, '-m', 'unittest', *tests], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=90)
    require(proc.returncode == 0, 'deterministic_freshness_regressions_failed')
    return {'mode': 'deterministic_regression_tests', 'passed': len(tests), 'tests': tests}


def wait_until(predicate, host, deadline, code):
    while time.monotonic() < deadline:
        if getattr(host, 'gateway', None) is not None:
            require(not host.gateway.snapshot().get('halted'), 'provider_budget_halted')
        value = predicate()
        if value:
            return value
        require(host.alive(), 'host_exited_before_' + code)
        time.sleep(.05)
    raise AcceptanceFailure('timeout_' + code)


def exercise(fixture, host, gateway, report, *, timeout=180, idle_seconds=2):
    scenario = novel_scenario()
    report['scenario'] = scenario
    trace_path = host.root / 'mcp-trace.jsonl'
    deadline = time.monotonic() + timeout
    report['stage'] = 'initial_host'
    host.start(fixture, 'initial')
    def blocked():
        completed, waits = trace_state(read_trace(trace_path))
        nodes = [(req, value) for req, _, value in completed
                 if req['message']['params']['name'] == 'bridge_add_node' and isinstance(value, dict) and value.get('node_id')]
        return (nodes, waits) if nodes and waits else None
    nodes, waits = wait_until(blocked, host, deadline, 'pending_wait')
    require(len(nodes) == 1, 'unexpected_decision_count')
    _, first = nodes[0]
    task, node = first['task_id'], first['node_id']
    report.update(task_id=task, node_id=node, first_owner=first.get('owner'))
    require(first.get('owner') == fixture.owner_name, 'first_route_wrong_owner')
    require(not fixture.store.get_decision(node)['authorized'], 'authority_before_human')
    names = {r['message']['params']['name'] for r, _, _ in trace_state(read_trace(trace_path))[0]}
    require('bridge_search_decisions' in names and ('bridge_search_context' in names or 'bridge_get_record' in names),
            'host_did_not_read_prior_and_context')
    completed = trace_state(read_trace(trace_path))[0]
    prior_reads = [value for req, _, value in completed
                   if req['message']['params']['name'] in ('bridge_search_decisions', 'bridge_get_decision')]
    context_reads = [value for req, _, value in completed
                     if req['message']['params']['name'] in ('bridge_search_context', 'bridge_get_record')]
    require(any(fixture.prior_id in json.dumps(value) for value in prior_reads), 'host_missing_actual_prior_evidence')
    require(any(fixture.source['record_id'] in json.dumps(value) for value in context_reads), 'host_missing_actual_context_evidence')
    report['evidence_reads'] = {'prior_decision_id': fixture.prior_id, 'record_id': fixture.source['record_id'],
                               'source_version_id': fixture.source['source_version_id']}
    fixture.store.delivery.deliver_now()
    notices = [m for m in fixture.slack.messages if '/#runs/' + task + '/' + node in m['text']]
    require(bool(notices), 'notification_missing')
    require(all(m.get('blocks') is None and m['text'].startswith('Your judgment is requested in Raven.') for m in notices),
            'notification_not_link_only')
    before = fixture.store.get_decision(node)
    slack_event = fixture.event(notices[-1], 'Approved. Ship it.', user=OWNER)
    status, queued = fixture.callback(slack_event)
    require(status == 200 and queued.get('queued') is True, 'slack_reply_not_queued')
    wait_until(lambda: fixture.store.graph.db.execute('SELECT state FROM slack_ingress WHERE id=?',
               (slack_event['event_id'],)).fetchone()['state'] == 'done', host, deadline, 'slack_reply_processed')
    require(fixture.store.get_decision(node)['updated_at'] == before['updated_at'], 'slack_reply_changed_decision')
    try:
        fixture.call('bridge_finish_task', task_id=task)
    except AssertionError:
        pass
    else:
        raise AcceptanceFailure('finish_allowed_before_human')
    while gateway is not None and provider_snapshot(gateway).get('active_requests'):
        require(time.monotonic() < deadline, 'provider_not_quiescent_during_wait')
        time.sleep(.05)
    pending_usage = provider_snapshot(gateway)
    pending_waits = trace_state(read_trace(trace_path))[1]
    require(bool(pending_waits), 'host_not_still_waiting')
    time.sleep(idle_seconds)
    require(provider_snapshot(gateway) == pending_usage, 'inference_during_pending_wait')
    current_waits = trace_state(read_trace(trace_path))[1]
    require(any(w in current_waits for w in pending_waits), 'wait_returned_during_idle_window')
    report['stage'] = 'interrupt_and_idle'
    require(bool(host.session_id), 'host_session_receipt_missing')
    old_session = host.session_id
    host.stop()
    require(not host.alive(), 'host_process_not_stopped')
    while gateway is not None and provider_snapshot(gateway).get('active_requests'):
        require(time.monotonic() < deadline, 'provider_not_quiescent_after_interrupt')
        time.sleep(.05)
    before_usage = provider_snapshot(gateway)
    time.sleep(idle_seconds)
    require(provider_snapshot(gateway) == before_usage, 'inference_during_stopped_wait')
    report['zero_inference_wait'] = {'seconds': idle_seconds, 'host_process_stopped': True,
                                    'pending_tool_window_also_checked': True,
                                    'provider_snapshot_unchanged': True, 'raven_inference': 'disabled'}
    fixture.restart(workers=False)
    require(fixture.store.get_decision(node)['run_id'] == task, 'restart_lost_node')
    report['stage'] = 'explicit_human_signoff'
    answer = scenario['base_answer'] + '\n' + scenario['caveat_message']
    payload = {'answer': answer, 'rationale': 'Synthetic production-only policy chosen by the test-controlled owner.',
               'expected_updated_at': fixture.store.get_decision(node)['updated_at'],
               'applicability': {'requires': {'environment': 'production'}}, 'evidence_mode': 'independent'}
    status, _, _ = http_json(fixture.url + '/api/decisions/' + node + '/signoff', payload,
                            {'Authorization': 'Bearer ' + fixture.agent_token})
    require(status == 403, 'agent_can_sign_as_human')
    signed = fixture.human('/api/decisions/' + node + '/signoff', payload)
    require(signed.get('authorized') and signed.get('signed_by') == fixture.owner_name, 'human_signature_missing')
    inquiry = fixture.human('/api/tasks/' + task + '/inquiry', {'question': 'Who signed the current policy and what scope applies?', 'focus_id': node})
    require(inquiry.get('authorized') is False and fixture.prior_id not in {e['id'] for e in inquiry['evidence']},
            'inquiry_scope_or_authority')
    report['scoped_inquiry'] = {'authorized': False, 'neighboring_decision_excluded': True, 'model_used': inquiry['model_used']}
    report['stage'] = 'resume_host'
    offset = len(read_trace(trace_path))
    host.start(fixture, 'resume')
    wait_until(lambda: fixture.store.graph.get_task(task)['status'] == 'completed', host, deadline, 'completed_task')
    if host.live:
        receipt = wait_until(lambda: next((e for e in reversed(host.events) if e.get('kind') == 'result'), None),
                             host, deadline, 'host_final_receipt')
        require(not receipt.get('is_error') and receipt.get('session_id') == old_session, 'host_final_receipt_invalid')
        report['host_final_receipt'] = receipt
    report['stage'] = 'independent_patch_checks'
    calls = trace_state(read_trace(trace_path)[offset:])[0]
    require(any(r['message']['params']['name'] == 'bridge_get_tree' for r, _, _ in calls), 'resume_missing_tree_read')
    require(host.session_id == old_session, 'host_session_changed')
    require(len(fixture.store.graph.decisions_for_task(task)) == 1, 'resume_duplicated_nodes')
    expression = policy_expression((fixture.root / 'checkout' / POLICY).read_text())
    checks = check_expression(expression, scenario)
    actual = fixture_call(fixture.root / 'checkout', 'read_changes', {})['diff']
    proof = fixture.call('bridge_export_proof', task_id=task)
    require(bool(actual) and proof['integrity']['valid'] and proof['bundle']['payload']['change']['diff'] == actual,
            'proof_diff_not_actual_patch')
    report['advisory_review'] = {'status': proof.get('review', {}).get('status', 'not_reported'),
                                  'notice': 'Not independent correctness verification; Raven model review disabled for this host phase.'}
    host.stop()
    if host.live:
        report['stage'] = 'live_scoped_inquiry'
        report['live_inquiry'] = live_inquiry(fixture, task, node, gateway, report['model'])
        report['raven_inference'] = 'live_task_inquiry_only; host_ladder_deterministic'
        require(gateway.snapshot().get('physical_requests', 0) > 0, 'no_physical_provider_requests')
    else:
        report['live_inquiry'] = {'mode': 'fixture_only', 'status': 'not_executed'}
    report.update(stage='complete', status='passed' if host.live else 'scripted_passed',
                  behavioral_cases=len(checks), same_session_resumed=True, durable_task_recovered=True,
                  actual_patch=actual, proof_integrity_valid=True)
    return report


def run(*, run_live=False, gateway=None, model='', output_dir=None, host_factory=None,
        timeout=180, idle_seconds=2, freshness_runner=freshness_checks):
    if not run_live and host_factory is None:
        return {'status': 'blocked', 'reason': 'Explicit --run-live and approved provider gateway are required.'}
    if run_live and (gateway is None or not model):
        return {'status': 'blocked', 'reason': 'Configured provider gateway and explicit host model are required.'}
    if run_live:
        try:
            version = importlib.metadata.version('claude-agent-sdk')
        except importlib.metadata.PackageNotFoundError:
            version = ''
        if version != SDK_VERSION:
            return {'status': 'blocked', 'reason': 'Install the pinned requirements-hosts.txt before live acceptance.'}
    report = {'status': 'failed', 'stage': 'setup', 'host_live': bool(run_live),
              'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'host': 'official Claude Agent SDK' if run_live else 'scripted driver fixture',
              'sdk_version': SDK_VERSION if run_live else None, 'model': model,
              'synthetic_people': True, 'synthetic_slack': True, 'raven_inference': 'deterministic_disabled',
              'limitations': ['No real Slack or real human approval.', 'Raven live discovery/ladder inference is not covered; live task inquiry is a separate required live stage.',
                              'Source freshness is a separately labeled deterministic regression stage.',
                              'Coding is deliberately restricted to an expression-only policy function.']}
    host = None
    with ExitStack() as stack:
        root = Path(output_dir) if output_dir else Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='claude-mcp-acceptance-')))
        if output_dir and root.exists() and any(root.iterdir()):
            return {'status': 'blocked', 'reason': 'Use a new empty output directory; existing evidence is never overwritten.'}
        root.mkdir(parents=True, exist_ok=True)
        try:
            report['freshness'] = freshness_runner()
            # Prevent inherited production connector configuration from entering fixtures.
            clean = {k: v for k, v in os.environ.items() if not any(x in k for x in ('TOKEN', 'SECRET', 'API_KEY'))
                     and not k.startswith(('BRIDGE_', 'SLACK_', 'TEAMS_', 'GITHUB_', 'RAVEN_'))}
            with patch.dict(os.environ, clean, clear=True):
                with Fixture(directory=root / 'fixture', model_mode='fixture') as fixture:
                    fixture.prepare()
                    host = host_factory(root) if host_factory else SDKHost(gateway, root, model)
                    require(bool(host.live) == bool(run_live), 'host_mode_mismatch')
                    try:
                        exercise(fixture, host, gateway, report, timeout=timeout, idle_seconds=idle_seconds)
                    finally:
                        host.stop()
        except Exception as error:
            report.update(status='blocked' if isinstance(error, AcceptanceFailure) and error.code.startswith(('host_exited', 'timeout_')) else 'failed',
                          failure_code=error.code if isinstance(error, AcceptanceFailure) else 'runner_' + type(error).__name__,
                          runner_diagnostic=exception_diagnostic(error))
        finally:
            if host:
                host.stop()
                if hasattr(host, 'diagnostics'):
                    report['host_diagnostics'] = host.diagnostics()
            report['provider_usage'] = provider_snapshot(gateway)
        if output_dir:
            (root / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture-mcp', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--sdk-worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--sdk-preflight', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--offline-sdk-preflight', action='store_true', help='Run real SDK/MCP against synthetic provider SSE; never asks for a key')
    parser.add_argument('--run-live', action='store_true')
    parser.add_argument('--model', default='')
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--max-calls', type=int, default=24)
    parser.add_argument('--max-input-bytes', type=int, default=1500000)
    parser.add_argument('--max-output-tokens', type=int, default=24000)
    parser.add_argument('--workspace-id', default='')
    parser.add_argument('--credential-file', type=Path, help='Use an explicitly installed private test credential; invalid files never trigger a prompt')
    parser.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args(argv)
    if args.offline_sdk_preflight:
        result = offline_sdk_preflight(output_dir=args.output_dir, model=args.model or 'claude-sonnet-4-6',
                                       timeout=min(args.timeout, 90))
        print(json.dumps(result, indent=2))
        return 0 if result['status'] == 'offline_passed' else 1
    if args.fixture_mcp:
        fixture_mcp()
        return 0
    if args.sdk_preflight:
        try:
            version = importlib.metadata.version('claude-agent-sdk')
            if version != SDK_VERSION:
                shown = version if re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version) else 'unavailable'
                print(json.dumps({'kind': 'error', 'version': shown, 'code': 'isolated_sdk_version_mismatch'}), flush=True)
                return 1
            sdk_options(json.loads(sys.stdin.readline()))
            print(json.dumps({'version': version, 'options_constructed': True}), flush=True)
            return 0
        except Exception as error:
            print(json.dumps({'kind': 'error', 'diagnostic': exception_diagnostic(error)}), flush=True)
            return 1
    if args.sdk_worker:
        try:
            asyncio.run(sdk_worker(json.loads(sys.stdin.readline())))
            sys.stdout.flush()
            os._exit(0)
        except Exception as error:
            print(json.dumps({'kind': 'error', 'code': 'sdk_worker_failed',
                              'diagnostic': exception_diagnostic(error)}), flush=True)
            sys.stdout.flush()
            os._exit(1)
    if not args.run_live:
        result = run()
    else:
        if not args.model or not args.output_dir or min(args.max_calls, args.max_input_bytes, args.max_output_tokens) <= 0:
            parser.error('Live mode requires --model, --output-dir and positive approved budget limits.')
        try:
            version = importlib.metadata.version('claude-agent-sdk')
        except importlib.metadata.PackageNotFoundError:
            version = ''
        if version != SDK_VERSION:
            print(json.dumps({'status': 'blocked', 'reason': 'Install pinned requirements-hosts.txt before supplying a key.'}))
            return 1
        if args.output_dir.exists() and any(args.output_dir.iterdir()):
            print(json.dumps({'status': 'blocked', 'reason': 'Use a new empty output directory before supplying a key.'}))
            return 1
        child_preflight = sdk_child_preflight(args.model)
        if child_preflight['status'] != 'passed':
            print(json.dumps(child_preflight))
            return 1
        from evals.customer_provider_gateway import Gateway
        if args.credential_file is not None:
            try:
                from evals.customer_test_credentials import read_saved_key
                key = read_saved_key(args.credential_file)
            except Exception:
                result = {'status': 'blocked', 'stage': 'credential_setup',
                          'failure_code': 'saved_credential_unavailable',
                          'reason': 'The saved test credential could not be read safely. No prompt or provider call was made.'}
                args.output_dir.mkdir(parents=True, exist_ok=True)
                (args.output_dir / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
                print(json.dumps(result, indent=2))
                return 1
        else:
            key = getpass.getpass('Approved Anthropic API key (not stored): ').strip()
        if not key:
            result = {'status': 'blocked', 'reason': 'No provider key supplied.'}
        else:
            try:
                smoke = direct_inference_smoke(key, args.workspace_id, args.model)
            except Exception as error:
                key = ''
                result = {'status': 'failed', 'stage': 'direct_production_inference',
                          'failure_code': error.code if isinstance(error, AcceptanceFailure) else 'direct_provider_failed'}
            else:
                with Gateway(api_key=key, workspace_id=args.workspace_id, max_calls=args.max_calls,
                             max_input_bytes=args.max_input_bytes, max_output_tokens=args.max_output_tokens,
                             model=args.model) as gateway:
                    key = ''
                    result = run(run_live=True, gateway=gateway, model=args.model,
                                 output_dir=args.output_dir, timeout=args.timeout)
                    result['direct_production_inference'] = smoke
                    result['sdk_child_preflight'] = child_preflight
                    (args.output_dir / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    if args.run_live and args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
