"""Deterministic fake app-server contracts, never a live-provider claim."""
import asyncio
import io
import json
import subprocess
import unittest
from unittest.mock import patch

from bridge.harness_worker import _codex_transport as codex


class Process:
    def __init__(self, events=(), disconnect=False, resume_id='thread', effective_model='fake', effective_provider='openai'):
        self.stdout = asyncio.StreamReader()
        self.stdin = self
        self.returncode = None
        self.sent = []
        self.events = events
        self.disconnect = disconnect
        self.resume_id = resume_id
        self.effective_model, self.effective_provider = effective_model, effective_provider

    def frame(self, value):
        self.stdout.feed_data((json.dumps(value) + '\n').encode())

    def write(self, data):
        value = json.loads(data)
        self.sent.append(value)
        method = value.get('method')
        if self.disconnect:
            self.stdout.feed_eof()
            return
        if method == 'initialize':
            result = {}
        elif method in ('thread/start', 'thread/resume'):
            result = {'thread': {'id': self.resume_id}, 'model':self.effective_model, 'modelProvider':self.effective_provider}
        elif method == 'turn/start':
            self.frame({'method': 'turn/started', 'params': {'threadId': 'thread', 'turn': {'id': 'turn'}}})
            for event in self.events:
                self.frame(event)
            result = {'turn': {'id': 'turn'}}
        else:
            return
        self.frame({'id': value['id'], 'result': result})

    async def drain(self):
        pass

    def terminate(self):
        self.returncode = -15

    async def wait(self):
        return self.returncode


def event(method, **params):
    return {'method': method, 'params': {'threadId': 'thread', 'turnId': 'turn', **params}}


def finish(status='completed'):
    return event('turn/completed', turn={'id': 'turn', 'status': status})


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def run_host(self, process, session='', command_lines=None):
        config = {'session_id': session, 'model': 'fake', 'cwd': '/empty', 'system': 'safe',
                  'prompt': 'task', 'tools': [{'name': 'read_file', 'description': 'read', 'input_schema': {}}]}
        async def no_commands(*args):
            if command_lines:
                return command_lines.pop(0)
            await asyncio.Future()
        with patch('bridge.harness_worker.asyncio.to_thread', side_effect=no_commands), \
                patch('bridge.harness_worker.asyncio.create_subprocess_exec', return_value=process), \
                patch('bridge.harness_worker.sys.stdin', io.StringIO('')), \
                patch.dict('os.environ', {'RAVEN_CODEX_BIN': 'fake'}), \
                patch('bridge.harness_worker.emit') as emit:
            await asyncio.wait_for(codex(config), 1)
            return [call.args[0] for call in emit.call_args_list]

    async def test_early_start_notification_and_completion_are_preserved(self):
        values = await self.run_host(Process([finish()]))
        self.assertEqual(values[-1]['status'], 'completed')
        self.assertFalse(values[-1]['is_error'])

    async def test_foreign_thread_cannot_hijack_turn_or_publish_text(self):
        frames = [event('turn/started', threadId='foreign', turn={'id': 'other'}),
                  event('item/completed', threadId='foreign', item={'type': 'agentMessage', 'text': 'secret'}),
                  event('turn/completed', threadId='foreign', turn={'id': 'other', 'status': 'completed'}), finish()]
        values = await self.run_host(Process(frames))
        self.assertFalse(any(x['kind'] == 'text' for x in values))
        self.assertEqual(values[-1]['turn_id'], 'turn')

    async def test_duplicate_and_unknown_tool_calls_are_refused(self):
        tool = event('item/tool/call', callId='one', tool='read_file', arguments={})
        tool['id'] = 90
        unknown = event('item/tool/call', callId='two', tool='shell', arguments={})
        unknown['id'] = 91
        process = Process([tool, tool, unknown, finish()])
        values = await self.run_host(process)
        self.assertEqual(len([x for x in values if x['kind'] == 'tool']), 1)
        refusals = [x for x in process.sent if x.get('result', {}).get('success') is False]
        self.assertEqual(len(refusals), 2)

    async def test_permission_requests_are_denied_without_parent_tools(self):
        frames = []
        for ident, method in enumerate(('item/commandExecution/requestApproval', 'item/tool/requestPermissions', 'unsupported/request'), 100):
            value = event(method)
            value['id'] = ident
            frames.append(value)
        process = Process([*frames, finish()])
        values = await self.run_host(process)
        replies = {value['id']: value for value in process.sent if value.get('id') in (100, 101, 102)}
        self.assertEqual(replies[100]['result'], {'decision': 'decline'})
        self.assertEqual(replies[101]['result'], {'permissions': {}, 'scope': 'turn'})
        self.assertEqual(replies[102]['error']['code'], -32601)
        self.assertFalse(any(value['kind'] == 'tool' for value in values))

    async def test_effective_model_or_provider_change_is_refused_on_start_and_resume(self):
        for session in ('', 'thread'):
            for kwargs in ({'effective_model':'different'}, {'effective_provider':'different'}):
                with self.subTest(session=session, kwargs=kwargs):
                    process = Process(**kwargs)
                    with self.assertRaisesRegex(RuntimeError, 'model_identity_changed'):
                        await self.run_host(process, session=session)
                    self.assertFalse(any(value.get('method') == 'turn/start' for value in process.sent))

    async def test_pre_start_interrupt_never_submits_a_prompt(self):
        process = Process([finish()])
        values = await self.run_host(process, command_lines=['{"kind":"interrupt"}\n'])
        self.assertFalse(any(value.get('method') == 'turn/start' for value in process.sent))
        self.assertFalse(any(value['kind'] == 'delivery' for value in values))
        self.assertEqual(values[-1]['status'], 'interrupted')

    async def test_interrupted_is_not_success(self):
        values = await self.run_host(Process([finish('interrupted')]))
        self.assertTrue(values[-1]['is_error'])

    async def test_eof_rejects_pending_request_immediately(self):
        with self.assertRaisesRegex(RuntimeError, 'disconnected'):
            await self.run_host(Process(disconnect=True))

    async def test_resume_cannot_silently_select_another_session(self):
        with self.assertRaisesRegex(RuntimeError, 'resume_identity_changed'):
            await self.run_host(Process(resume_id='different'), session='saved')


class CapabilityTests(unittest.TestCase):
    def test_codex_worker_and_capability_require_exact_qualified_model_and_binary(self):
        from bridge import harness_worker, workbench_hosts
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        pin = workbench_hosts.CODEX_VERSION
        model = workbench_hosts.CODEX_MODEL
        with patch.dict('os.environ', {'RAVEN_HOST_CODEX_API_KEY':'fixture', 'RAVEN_HOST_CODEX_MODEL':model,
                                       'RAVEN_CODEX_BIN':'fixture', 'BRIDGE_HOST_PROBE_SECONDS':'0'}):
            for version, returncode, ready in ((pin,0,True), ('different',0,False), (pin,1,False)):
                result = SimpleNamespace(stdout='codex-cli ' + version, returncode=returncode)
                with patch('bridge.workbench_hosts.subprocess.run', return_value=result), \
                     patch('bridge.harness_worker._codex_transport', new_callable=AsyncMock) as transport:
                    # Capability additionally checks native probe exit status.
                    self.assertEqual(workbench_hosts.capability('codex')['ready'], ready)
                    if ready:
                        asyncio.run(harness_worker.codex({'model':model}))
                        transport.assert_awaited_once_with({'model':model}, observer=None)
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'unqualified_binary'):
                            asyncio.run(harness_worker.codex({'model':model}))
                        transport.assert_not_awaited()
            for error in (OSError('missing'), subprocess.TimeoutExpired('fixture', 5)):
                with patch('bridge.harness_worker.subprocess.run', side_effect=error), \
                     patch('bridge.harness_worker._codex_transport', new_callable=AsyncMock) as transport:
                    with self.assertRaisesRegex(RuntimeError, 'unqualified_binary'):
                        asyncio.run(harness_worker.codex({'model':model}))
                    transport.assert_not_awaited()
            with patch('bridge.harness_worker.subprocess.run', side_effect=AssertionError('No binary call')):
                with self.assertRaisesRegex(RuntimeError, 'unqualified_model'):
                    asyncio.run(harness_worker.codex({'model':'different'}))
            with patch.dict('os.environ', {'RAVEN_HOST_CODEX_MODEL':'different'}), \
                 patch('bridge.workbench_hosts.subprocess.run', return_value=SimpleNamespace(stdout='codex-cli '+pin,returncode=0)):
                self.assertFalse(workbench_hosts.capability('codex')['ready'])

    def test_pi_requires_explicit_credential_model_and_provider(self):
        from bridge.workbench_hosts import capability
        with patch.dict('os.environ', {'RAVEN_HOST_PI_API_KEY': '', 'RAVEN_HOST_PI_MODEL': '',
                                       'RAVEN_HOST_PI_PROVIDER': '', 'OPENAI_API_KEY': 'do-not-borrow'}):
            result = capability('pi')
        self.assertFalse(result['ready'])
        self.assertFalse(result['live_verified'])
        self.assertIn('RAVEN_HOST_PI_API_KEY', ' '.join(result['blockers']))
        self.assertIn('RAVEN_HOST_PI_PROVIDER', ' '.join(result['blockers']))

    def test_unknown_harness_never_selects_codex(self):
        from bridge.workbench_hosts import capability
        from bridge.store import Invalid
        with self.assertRaises(Invalid):
            capability('unknown')

    def test_pi_custom_endpoint_rejects_insecure_remote_and_embedded_credentials(self):
        from bridge.workbench_hosts import capability
        for endpoint in ('https://[malformed', 'http://remote.example/v1', 'https://key:secret@example.com/v1', 'https://example.com/v1?key=x'):
            with self.subTest(endpoint=endpoint), patch.dict('os.environ', {'RAVEN_HOST_PI_BASE_URL': endpoint}):
                self.assertIn('without credentials', ' '.join(capability('pi')['blockers']))


class ProcessGroupTests(unittest.TestCase):
    def test_cleanup_targets_dedicated_group_even_if_leader_already_exited(self):
        import signal
        from types import SimpleNamespace
        from bridge.workbench_hosts import stop_process_group
        process = SimpleNamespace(pid=123456, wait=lambda **kwargs: 0)
        with patch('bridge.workbench_hosts.os.killpg') as kill:
            stop_process_group(process)
        self.assertEqual(kill.call_args_list[0].args, (123456, signal.SIGTERM))
        self.assertEqual(kill.call_args_list[1].args, (123456, signal.SIGKILL))

    def test_already_gone_group_is_harmless(self):
        from types import SimpleNamespace
        from bridge.workbench_hosts import stop_process_group
        with patch('bridge.workbench_hosts.os.killpg', side_effect=ProcessLookupError):
            stop_process_group(SimpleNamespace(pid=123456, wait=lambda **kwargs: 0))


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        import test_workbench_hosts as fixtures
        self.fixture = fixtures.IntentTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.task = self.fixture.submit()
        self.service = self.fixture.service
        self.service.update(self.task, status='running')
        self.service.store.graph.db.execute('INSERT INTO browser_hosts(task_id,provider,model,updated_at) VALUES(?,?,?,?)',
                                            (self.task, 'pi', 'fixture', 'now'))

    def process(self, output='', broken=False):
        from types import SimpleNamespace
        class Input(io.StringIO):
            def write(self, value):
                if broken:
                    raise BrokenPipeError('worker exited before config')
                return super().write(value)
        return SimpleNamespace(stdin=Input(), stdout=io.StringIO(output), pid=123456)

    def run_host(self, process):
        from bridge.workbench_hosts import Harness
        return Harness().run(self.service, self.task, 'Fixture task')

    def test_startup_broken_pipe_still_stops_worker_group(self):
        from bridge import workbench_hosts as hosts
        process = self.process(broken=True)
        with patch.object(hosts, 'capability', return_value={'ready': True, 'version': '1.4.0'}), \
                patch.object(hosts.subprocess, 'Popen', return_value=process) as spawn, \
                patch.object(hosts, 'stop_process_group') as stop, self.assertRaises(BrokenPipeError):
            self.run_host(process)
        stop.assert_called_once_with(process)
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])
        state = self.service.store.graph.db.execute('SELECT state FROM browser_hosts WHERE task_id=?', (self.task,)).fetchone()[0]
        self.assertEqual(state, 'stopped')

    def test_receipt_and_pi_cost_are_recorded_together(self):
        from bridge import workbench_hosts as hosts
        process = self.process(json.dumps({'kind':'result', 'status':'completed', 'is_error':False, 'cost_usd':0.25})+'\n')
        with patch.object(hosts, 'capability', return_value={'ready': True, 'version': '1.4.0'}), \
                patch.object(hosts.subprocess, 'Popen', return_value=process), patch.object(hosts, 'stop_process_group'):
            self.run_host(process)
        row = self.service.store.graph.db.execute('SELECT * FROM browser_hosts WHERE task_id=?', (self.task,)).fetchone()
        self.assertEqual(row['state'], 'completed')
        self.assertEqual(row['cost_usd'], 0.25)
        self.assertEqual(row['turns'], 1)

    def test_opencode_uses_native_driver_and_only_explicit_host_key(self):
        from bridge import workbench_hosts as hosts
        self.service.store.graph.db.execute("UPDATE browser_hosts SET provider='opencode' WHERE task_id=?", (self.task,))
        process = self.process(json.dumps({'kind':'result', 'status':'completed', 'is_error':False, 'cost_usd':0.5})+'\n')
        with patch.object(hosts, 'capability', return_value={'ready': True, 'version': '1.19.4'}), \
                patch.object(hosts.subprocess, 'Popen', return_value=process) as spawn, patch.object(hosts, 'stop_process_group'), \
                patch.dict('os.environ', {'OPENAI_API_KEY':'internal-do-not-borrow',
                                         'RAVEN_HOST_OPENCODE_API_KEY':'dedicated-fixture', 'RAVEN_HOST_OPENCODE_PROVIDER':'anthropic'}):
            self.run_host(process)
        self.assertEqual(spawn.call_args.args[0][-1], 'bridge.opencode_host')
        environment = spawn.call_args.kwargs['env']
        self.assertEqual(environment['RAVEN_OPENCODE_API_KEY'], 'dedicated-fixture')
        self.assertNotIn('OPENAI_API_KEY', environment)
        self.assertNotIn('ANTHROPIC_API_KEY', environment)
        row = self.service.store.graph.db.execute('SELECT cost_usd FROM browser_hosts WHERE task_id=?', (self.task,)).fetchone()
        self.assertEqual(row['cost_usd'], 0.5)

    def test_queued_tools_are_not_dispatched_after_wait_or_timeout_interrupt(self):
        from bridge import workbench_hosts as hosts
        for status, timeout in (('waiting','900'), ('running','0.000000001')):
            with self.subTest(status=status):
                self.service.update(self.task, status=status)
                process = self.process('\n'.join(json.dumps(value) for value in (
                    {'kind':'tool','call':{'id':'queued','name':'edit_file','input':{}}},
                    {'kind':'result','status':'interrupted','is_error':True,'cost_usd':0}))+'\n')
                with patch.object(hosts, 'capability', return_value={'ready':True,'version':'1.4.0'}), \
                        patch.object(hosts.subprocess, 'Popen', return_value=process), patch.object(hosts, 'stop_process_group'), \
                        patch.object(hosts, 'execute_once', side_effect=AssertionError('Dispatched after interrupt')), \
                        patch.dict('os.environ', {'RAVEN_HOST_TURN_TIMEOUT':timeout}):
                    receipt = self.run_host(process)
                self.assertFalse(receipt['prompt_delivered'])
