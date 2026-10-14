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


class EvidenceReadTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SimpleNamespace(prior_id='seeded-prior', source={
            'record_id': 'seeded-record', 'source_version_id': 'seeded-current-version'})
        self.prior = {'id': self.fixture.prior_id, 'answer': a.PRIOR_ANSWER,
                      'applicability': a.PRIOR_APPLICABILITY}
        self.context = {**self.fixture.source, 'body': a.SOURCE_BODY}

    def observed(self, *responses, name='bridge_start_task', arguments=None):
        request = {'message': {'params': {'name': name, 'arguments': arguments or {}}}}
        return a.observed_evidence_reads(self.fixture, [(request, {}, response) for response in responses])

    def test_bundled_start_response_counts_complete_prior_scope_and_source_content(self):
        result = self.observed({'discovery': {'prior_decisions': [self.prior], 'context': [self.context]}})
        self.assertTrue(result['prior_content_read'])
        self.assertTrue(result['context_content_read'])
        self.assertEqual(result['prior_response_tools'], ['bridge_start_task'])
        self.assertEqual(result['context_response_tools'], ['bridge_start_task'])

    def test_any_raven_response_can_deliver_equivalent_nested_json_evidence(self):
        result = self.observed({'content': json.dumps({'prior': self.prior, 'source': self.context})},
                               name='bridge_get_tree')
        self.assertTrue(result['prior_content_read'])
        self.assertTrue(result['context_content_read'])

    def test_pointer_title_and_request_arguments_are_not_evidence_reads(self):
        result = self.observed({'prior': {'id': self.fixture.prior_id},
                                'source': {**self.fixture.source, 'title': 'Production billing context'}},
                               arguments={'prior': self.prior, 'context': self.context})
        self.assertFalse(result['prior_content_read'])
        self.assertFalse(result['context_content_read'])

    def test_prior_requires_complete_answer_and_matching_recorded_scope(self):
        for prior in ({'id': self.fixture.prior_id, 'answer': a.PRIOR_ANSWER},
                      {**self.prior, 'answer': a.PRIOR_ANSWER[:30]},
                      {**self.prior, 'applicability': {'requires': {'environment': 'production'}}}):
            with self.subTest(prior=prior):
                self.assertFalse(self.observed(prior)['prior_content_read'])
        encoded = {**self.prior, 'applicability': json.dumps(a.PRIOR_APPLICABILITY)}
        self.assertTrue(self.observed(encoded)['prior_content_read'])

    def test_source_requires_exact_current_version_and_complete_body(self):
        for context in ({**self.context, 'source_version_id': 'old-version'},
                        {**self.context, 'body': a.SOURCE_BODY[:40]},
                        {**self.context, 'record_id': 'other-record'},
                        {**self.context, 'source_version_id': 'old-version', 'id': 'seeded-current-version'}):
            with self.subTest(context=context):
                self.assertFalse(self.observed(context)['context_content_read'])

    def test_contents_cannot_be_stitched_across_unrelated_objects_or_responses(self):
        pointer = {'id': self.fixture.prior_id}
        other_prior = {**self.prior, 'id': 'other-prior'}
        context_pointer = dict(self.fixture.source)
        other_context = {**self.context, 'record_id': 'other-record'}
        for result in (self.observed({'items': [pointer, other_prior, context_pointer, other_context]}),
                       self.observed(pointer, other_prior, context_pointer, other_context)):
            self.assertFalse(result['prior_content_read'])
            self.assertFalse(result['context_content_read'])

    def test_fixture_file_response_is_not_a_raven_evidence_read(self):
        result = self.observed({'prior': self.prior, 'context': self.context}, name='read_file')
        self.assertFalse(result['prior_content_read'])
        self.assertFalse(result['context_content_read'])

    def test_real_dedicated_responses_and_bundled_equivalent_both_count(self):
        with a.Fixture() as fixture:
            fixture.prepare()
            prior = fixture.call('bridge_get_decision', decision_id=fixture.prior_id)
            source = fixture.call('bridge_get_record', record_id=fixture.source['record_id'], repo=a.REPO)
            search = fixture.call('bridge_search_context', repo=a.REPO, query='Production billing context')
            for name, value in [('bridge_start_task', {'discovery': [prior, source]}),
                                ('bridge_get_tree', {'context': search, 'prior': prior})]:
                request = {'message': {'params': {'name': name}}}
                result = a.observed_evidence_reads(fixture, [(request, {}, value)])
                self.assertTrue(result['prior_content_read'])
                self.assertTrue(result['context_content_read'])


class DriverTests(unittest.TestCase):
    def test_private_synthetic_support_diagnostic_is_bounded_and_drops_unknown_fields(self):
        raw = {'claims': [{'claim': 'x' * 1200, 'support': 'not_in_source', 'unknown': 'omit'}] * 35,
               'unknown': 'omit'}
        result = a.inquiry_model_diagnostic('support', raw, '')
        self.assertEqual(result['category'], 'unsupported_or_malformed_claims')
        self.assertEqual(len(result['synthetic_claims']), 30)
        self.assertEqual(len(result['synthetic_claims'][0]['claim']), 1000)
        self.assertEqual(result['synthetic_claims'][0]['support'], 'not_in_source')
        self.assertNotIn('omit', json.dumps(result))

    def test_human_inquiry_uses_configured_timeout_and_remaining_deadline(self):
        fixture = SimpleNamespace(url='http://127.0.0.1:1', human_token='synthetic', request_timeout=120)
        with patch.object(a, 'http_json', return_value=(200, {'ok': True}, {})) as request:
            self.assertEqual(a.Fixture.human(fixture, '/api/tasks/t/inquiry', {}), {'ok': True})
            self.assertEqual(request.call_args.kwargs['timeout'], 120)
            fixture.request_deadline = 125
            with patch.object(a.time, 'monotonic', return_value=100):
                a.Fixture.human(fixture, '/api/tasks/t/inquiry', {})
            self.assertEqual(request.call_args.kwargs['timeout'], 25)
            request.reset_mock()
            with patch.object(a.time, 'monotonic', return_value=126):
                with self.assertRaisesRegex(a.AcceptanceFailure, 'timeout_human_request'):
                    a.Fixture.human(fixture, '/api/tasks/t/inquiry', {})
            request.assert_not_called()

    def test_inquiry_only_cli_uses_saved_key_and_fixed_budget_without_sdk_or_smoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / 'existing', Path(tmp) / 'new'
            (source / 'fixture').mkdir(parents=True)
            (source / 'result.json').write_text('{}')
            (source / 'fixture/raven.db').write_bytes(b'synthetic-placeholder')
            with patch('evals.customer_test_credentials.read_saved_key', return_value='synthetic-key'), \
                    patch('evals.customer_provider_gateway.Gateway') as gateway, \
                    patch.object(a, 'run_inquiry_only', return_value={'status': 'passed', 'integrated_acceptance': False}), \
                    patch.object(a, 'sdk_child_preflight', side_effect=AssertionError('no SDK')), \
                    patch.object(a, 'direct_inference_smoke', side_effect=AssertionError('no smoke')), \
                    patch.object(a.getpass, 'getpass', side_effect=AssertionError('no prompt')):
                status = a.main(['--run-live', '--inquiry-only', str(source), '--output-dir', str(output),
                                 '--credential-file', '/synthetic/private/key', '--model', 'fixture-model',
                                 '--max-calls', '999', '--max-input-bytes', '99999999', '--max-output-tokens', '999999'])
            self.assertEqual(status, 0)
            self.assertEqual(gateway.call_args.kwargs['max_calls'], 4)
            self.assertEqual(gateway.call_args.kwargs['max_input_bytes'], 100000)
            self.assertEqual(gateway.call_args.kwargs['max_output_tokens'], 6000)
            self.assertNotIn('synthetic-key', (output / 'result.json').read_text())

    def test_invalid_credential_file_never_prompts_or_calls_provider(self):
        reader = SimpleNamespace(read_saved_key=lambda _: (_ for _ in ()).throw(ValueError('SECRET private path')))
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(a.importlib.metadata, 'version', return_value=a.SDK_VERSION), \
                patch.object(a, 'sdk_child_preflight', return_value={'status': 'passed'}), \
                patch.dict(sys.modules, {'evals.customer_test_credentials': reader}), \
                patch.object(a.getpass, 'getpass', side_effect=AssertionError('must not prompt')), \
                patch.object(a, 'direct_inference_smoke', side_effect=AssertionError('must not call')):
            output = Path(tmp) / 'output'
            result = a.main(['--run-live', '--model', 'fixture-model', '--output-dir', str(output),
                             '--credential-file', str(Path(tmp) / 'missing')])
            saved = (output / 'result.json').read_text()
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(saved)['failure_code'], 'saved_credential_unavailable')
        self.assertNotIn('SECRET', saved)

    def test_saved_credential_uses_reader_and_never_prompts(self):
        reader = SimpleNamespace(read_saved_key=lambda _: 'synthetic-saved-key')
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(a.importlib.metadata, 'version', return_value=a.SDK_VERSION), \
                patch.object(a, 'sdk_child_preflight', return_value={'status': 'passed'}), \
                patch.dict(sys.modules, {'evals.customer_test_credentials': reader}), \
                patch.object(a.getpass, 'getpass', side_effect=AssertionError('must not prompt')), \
                patch.object(a, 'direct_inference_smoke', side_effect=RuntimeError('synthetic-saved-key')) as smoke:
            output = Path(tmp) / 'output'
            result = a.main(['--run-live', '--model', 'fixture-model', '--output-dir', str(output),
                             '--credential-file', str(Path(tmp) / 'installed')])
            saved = (output / 'result.json').read_text()
        self.assertEqual(result, 1)
        self.assertEqual(smoke.call_args.args[0], 'synthetic-saved-key')
        self.assertNotIn('synthetic-saved-key', saved)

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
            json={'model': 'fixture-model', 'stream': False, 'messages': [result], 'tools': [{}]}))
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
            gateway = SimpleNamespace(placeholder='public-placeholder', urllib_open=urllib.request.urlopen,
                                      snapshot=lambda: {'physical_requests': 0, 'fixture_only': True})
            readings = [{'outcome': 'supported_answer', 'answer': 'Real customer traffic is billable.',
                         'citations': [{'id': node, 'quote': 'Real customer traffic is billable.'}], 'open': []},
                        {'claims': [{'claim': 'Real customer traffic is billable.', 'support': 'stated'}]}]
            with patch.object(llm.Client, 'complete_json', side_effect=readings) as mocked:
                result = a.live_inquiry(fixture, task, node, gateway, 'fixture-model')
            self.assertEqual(mocked.call_count, 2)
            self.assertTrue(result['model_used'])
            self.assertTrue(result['counts_unchanged'])

    def test_inquiry_fallback_preserves_response_and_exact_citation_failure(self):
        from bridge import llm
        with a.Fixture() as fixture:
            fixture.prepare()
            task = fixture.call('bridge_start_task', title='Production billing policy', repo=a.REPO)['task_id']
            node = fixture.call('bridge_add_node', task_id=task, question='Is traffic billable?', paths=a.POLICY)['node_id']
            raw = {'outcome': 'supported_answer', 'answer': 'Unverified answer',
                   'citations': [{'id': node, 'quote': 'This quote is not in the source.'}], 'open': []}
            gateway = SimpleNamespace(placeholder='placeholder', urllib_open=urllib.request.urlopen,
                                      snapshot=lambda: {'physical_requests': 1, 'fixture_only': True})
            diagnostic = {}
            with patch.object(llm.Client, 'complete', return_value=json.dumps(raw)) as complete:
                with self.assertRaisesRegex(a.AcceptanceFailure, 'live_inquiry_not_verified_model_reading'):
                    a.live_inquiry(fixture, task, node, gateway, 'fixture-model', diagnostics=diagnostic)
            self.assertEqual(complete.call_count, 2, 'Invalid citations allow one bounded repair, then fail without support')
            self.assertEqual([call['purpose'] for call in diagnostic['model_calls']], ['inquiry', 'inquiry'])
            self.assertEqual([call['category'] for call in diagnostic['model_calls']],
                             ['unsupported_citation', 'unsupported_citation'])
            self.assertFalse(diagnostic['response']['model_used'])
            self.assertTrue(diagnostic['counts_unchanged'])
            self.assertEqual(diagnostic['provider_before'], diagnostic['provider_after'])
            self.assertEqual(diagnostic['status'], 'unverified')
            self.assertNotIn(raw['answer'], json.dumps(diagnostic['response']))
            self.assertEqual(diagnostic['model_calls'][0]['synthetic_answer'], raw['answer'])

    def test_inquiry_model_diagnostics_classify_bounded_private_synthetic_text(self):
        prompt = json.dumps({'evidence': [{'id': 'known', 'text': 'Actual source.'}]})
        malformed = a.inquiry_model_diagnostic('inquiry', {'answer': 'PRIVATE'}, prompt)
        self.assertEqual(malformed['category'], 'malformed_reading')
        raw = {'outcome': 'supported_answer', 'answer': 'PRIVATE', 'citations': [], 'open': []}
        self.assertEqual(a.inquiry_model_diagnostic('inquiry', raw, prompt)['category'], 'missing_required_citation')
        raw['citations'] = [{'id': 'known', 'quote': 'Actual source.'}]
        good = a.inquiry_model_diagnostic('inquiry', raw, prompt)
        self.assertEqual(good['category'], 'schema_valid')
        self.assertEqual(good['synthetic_answer'], 'PRIVATE')
        response = a.inquiry_response_diagnostic({'answer': 'Synthetic answer', 'model_used': False,
            'headers': {'key': 'PRIVATE'}, 'citations': [{'id': 'known', 'quote': 'Actual source.', 'token': 'PRIVATE'}]})
        self.assertNotIn('PRIVATE', json.dumps(response))
        self.assertEqual(response['answer'], 'Synthetic answer')

    def test_late_inquiry_failure_retains_verified_host_patch_and_proof_stages(self):
        class ReceiptHost(ScriptedHost):
            live = True
            def __init__(self, root):
                super().__init__(root)
                self.events = [{'kind': 'result', 'session_id': self.session_id, 'is_error': False}]
        def failed_inquiry(*args, diagnostics, **kwargs):
            diagnostics.update(status='unverified', response={'model_used': False, 'authorized': False})
            raise a.AcceptanceFailure('live_inquiry_not_verified_model_reading')
        gateway = SimpleNamespace(snapshot=lambda: {'physical_requests': 1})
        with patch.object(a.importlib.metadata, 'version', return_value=a.SDK_VERSION), \
                patch.object(a, 'live_inquiry', side_effect=failed_inquiry):
            report = a.run(run_live=True, gateway=gateway, model='fixture-model', host_factory=ReceiptHost,
                           idle_seconds=.01, timeout=20, freshness_runner=lambda: {'mode': 'test-double'})
        self.assertEqual(report['failure_code'], 'live_inquiry_not_verified_model_reading')
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['behavioral_cases'], 16)
        self.assertTrue(report['same_session_resumed'])
        self.assertTrue(report['durable_task_recovered'])
        self.assertTrue(report['proof_integrity_valid'])
        self.assertTrue(report['actual_patch'])
        self.assertIn('proof_matches_actual_patch', report['passed_stages'])
        self.assertNotIn('live_scoped_inquiry', report['passed_stages'])
        self.assertFalse(report['live_inquiry']['response']['model_used'])

    def test_inquiry_only_uses_authenticated_copy_without_reseeding_original(self):
        from bridge import llm
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / 'source', Path(tmp) / 'diagnostic'
            run = a.run(output_dir=source, host_factory=ScriptedHost, idle_seconds=.01, timeout=20,
                        freshness_runner=lambda: {'mode': 'test-double'})
            self.assertEqual(run['status'], 'scripted_passed')
            before_db = (source / 'fixture/raven.db').read_bytes()
            before_code = (source / 'fixture/checkout' / a.POLICY).read_bytes()
            def complete(purpose, system, prompt, **kwargs):
                if purpose == 'inquiry':
                    evidence = json.loads(prompt)['evidence']
                    decision = next(e for e in evidence if e['kind'] == 'task_decision')
                    return json.dumps({'outcome': 'supported_answer', 'answer': decision['text'],
                                       'citations': [{'id': decision['id'], 'quote': decision['text']}], 'open': []})
                return json.dumps({'claims': [{'claim': 'Synthetic recorded policy', 'support': 'stated'}]})
            gateway = SimpleNamespace(placeholder='placeholder', urllib_open=urllib.request.urlopen,
                                      snapshot=lambda: {'physical_requests': 0, 'fixture_only': True})
            with patch.object(llm.Client, 'complete', side_effect=complete) as mocked:
                result = a.run_inquiry_only(run_dir=source, output_dir=output, gateway=gateway, model='fixture-model')
            self.assertEqual(result['status'], 'passed', result)
            self.assertFalse(result['integrated_acceptance'])
            self.assertEqual(mocked.call_count, 2)
            self.assertEqual((source / 'fixture/raven.db').read_bytes(), before_db)
            self.assertEqual((source / 'fixture/checkout' / a.POLICY).read_bytes(), before_code)
            self.assertEqual(result['task_id'], run['task_id'])
            self.assertEqual(result['live_inquiry']['model_calls'][1]['category'], 'support_valid')
            self.assertTrue((output / 'result.json').is_file())

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

    def test_missing_static_content_still_fails_after_actual_prior_read(self):
        class MissingStaticHost(ScriptedHost):
            def call(self, fixture, name, **arguments):
                if name == 'bridge_search_context':
                    return {}  # No response delivered; the prior still uses real Raven.
                return super().call(fixture, name, **arguments)
        report = a.run(host_factory=MissingStaticHost, idle_seconds=.01, timeout=5,
                       freshness_runner=lambda: {'mode': 'test-double-not-executed'})
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['failure_code'], 'host_missing_actual_context_evidence')
        self.assertTrue(report['evidence_reads']['prior_content_read'])
        self.assertFalse(report['evidence_reads']['context_content_read'])
        self.assertNotIn('same_session_resumed', report)


if __name__ == '__main__':
    unittest.main()
