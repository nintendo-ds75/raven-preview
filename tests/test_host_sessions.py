import json
import subprocess
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase, run_git
from bridge import host_sessions as hs, host_client as hc
from bridge.auth import Identity
from bridge.setup import install_host_hooks
from bridge.store import Store, Invalid
from test_auth import SharedServer, BOOTSTRAP


class HookDeadlineTests(unittest.TestCase):
    def test_each_request_uses_only_the_remaining_cumulative_budget(self):
        clock = [100.0]
        observed = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit):
                clock[0] += 15
                return b'{"result": {}}'

        class Opener:
            def open(self, request, timeout):
                observed.append(timeout)
                return Response()

        with patch.object(hc.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(hc, 'build_opener', return_value=Opener()):
            config = {'url': 'http://localhost/mcp', 'token': 'synthetic-test'}
            hc.rpc(config, 'initialize', deadline=145)
            hc.rpc(config, 'tools/list', deadline=145)
            with self.assertRaisesRegex(ValueError, 'deadline'):
                hc.rpc(config, 'tools/call', deadline=145)
            with self.assertRaisesRegex(ValueError, 'deadline'):
                hc.rpc(config, 'tools/call', deadline=145)
        self.assertEqual(observed, [45, 30, 15])

    def test_outer_deadline_bounds_a_nonreturning_network_operation(self):
        released = threading.Event()
        began = time.monotonic()
        try:
            with self.assertRaisesRegex(ValueError, 'deadline'):
                hc.bounded_hook(lambda deadline: released.wait(5), budget=.05)
            self.assertLess(time.monotonic() - began, 1)
        finally:
            released.set()

    def test_git_inspection_receives_remaining_budget(self):
        with patch.object(hc.subprocess, 'check_output', return_value=b'/example/checkout\n') as command, \
                patch.object(hc.time, 'monotonic', return_value=100):
            hc.verify_project({'project': '/example/checkout'}, '/example/checkout', deadline=112)
        self.assertEqual(command.call_args.kwargs['timeout'], 12)

    def test_no_operation_starts_with_an_expired_budget(self):
        with patch.object(hc, 'build_opener') as opener:
            with self.assertRaisesRegex(ValueError, 'deadline'):
                hc.rpc({'url': 'http://localhost/mcp', 'token': 'synthetic-test'},
                       'initialize', deadline=time.monotonic() - 1)
        opener.return_value.open.assert_not_called()


class HostCases:
    def event(self, event='prompt', **changes):
        return hs.event(self.store, {**self.args, 'event': event, **changes}, principal=self.actor)

    def changed(self, task, kind='signature'):
        with self.store.graph.transaction():
            self.store.graph.append_event(kind, {'task_id': task, 'by': 'Reviewer Example'})

    def test_first_prompt_registered_once_followups_do_not_create_tasks(self):
        a = self.event()
        b = self.event()
        c = self.event(prompt='Also keep backwards compatibility.')
        self.assertEqual(a['task_id'], b['task_id'])
        self.assertEqual(a['task_id'], c['task_id'])
        task = self.store.graph.get_task(a['task_id'])
        self.assertEqual(task['goal'], self.args['prompt'])
        self.assertEqual(task['requester'], 'Ada Example')
        self.assertEqual(self.store.graph.db.execute('SELECT count(*) n FROM runs').fetchone()['n'], 1)

    def test_overlapping_first_prompt_hooks_create_only_one_task(self):
        with ThreadPoolExecutor(max_workers=3) as workers:
            results = list(workers.map(lambda _: self.event(), range(3)))
        self.assertEqual(len({r['task_id'] for r in results}), 1)
        self.assertEqual(self.store.graph.db.execute('SELECT count(*) n FROM runs').fetchone()['n'], 1)

    def test_interrupted_registration_retry_retains_prompt_in_task_trace(self):
        from bridge import canvas
        with patch.object(canvas, 'start_task', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError): self.event()
        resumed = self.event()
        event = self.store.graph.db.execute("SELECT detail FROM events WHERE run_id=? AND kind='host_task_recovered'",
                                           (resumed['task_id'],)).fetchone()
        self.assertEqual(json.loads(event['detail'])['prompt'], self.args['prompt'])

    def test_explicit_new_task_and_duplicate_hook_share_one_new_generation(self):
        a = self.event()
        b = self.event('new_task', prompt='A separate task', event_key='new-1')
        again = self.event('new_task', prompt='A separate task', event_key='new-1')
        self.assertNotEqual(a['task_id'], b['task_id'])
        self.assertEqual(b['task_id'], again['task_id'])

    def test_compaction_connect_recovers_task_without_new_prompt(self):
        first = self.event()
        reopened = Store(self.store.path)
        self.addCleanup(reopened.graph.close)
        result = hs.event(reopened, {**self.args, 'event': 'connect', 'prompt': ''}, principal=self.actor)
        self.assertEqual(first['task_id'], result['task_id'])

    def test_workspace_and_principal_bindings_cannot_be_replaced(self):
        self.event()
        with self.assertRaises(Invalid): self.event('connect', repo='other/project')
        with self.assertRaises(Invalid): self.event('connect', project='/another/checkout')
        other = Identity('person-b', 'Bea Example', 'member', 'agent')
        with self.assertRaises(Invalid):
            hs.event(self.store, {**self.args, 'event': 'poll', 'worker': 'worker-b'}, principal=other)

    def test_resume_is_durable_leased_and_acknowledged_idempotently(self):
        task = self.event()['task_id']
        self.assertIsNone(self.event('poll', worker='a')['message'])
        self.changed(task)
        message = self.event('poll', worker='a')['message']
        self.assertIsNotNone(message)
        self.assertIsNone(self.event('poll', worker='b')['message'])
        with self.assertRaises(Invalid): self.event('ack', worker='b', message_id=message['id'])
        reopened = Store(self.store.path)
        self.addCleanup(reopened.graph.close)
        reply = hs.event(reopened, {**self.args, 'event': 'ack', 'worker': 'a', 'message_id': message['id']}, principal=self.actor)
        self.assertTrue(reply['acknowledged'])
        self.assertTrue(self.event('ack', worker='a', message_id=message['id'])['duplicate'])
        self.assertIsNone(self.event('poll', worker='a')['message'])
        self.changed(task, 'source_review_required')
        self.assertNotEqual(self.event('poll', worker='a')['message']['id'], message['id'])

    def test_abandoned_worker_lease_can_be_retried_without_losing_event(self):
        task = self.event()['task_id']; self.changed(task)
        message = self.event('poll', worker='a')['message']
        with self.store.graph.transaction():
            self.store.graph.db.execute("UPDATE host_resumes SET lease_until='2000-01-01T00:00:00+00:00'")
        with self.assertRaises(Invalid): self.event('ack', worker='a', message_id=message['id'])
        second = self.event('poll', worker='b')['message']
        self.assertEqual(second['id'], message['id'])
        self.event('release', worker='b', message_id=second['id'])
        self.assertEqual(self.event('poll', worker='c')['message']['id'], message['id'])

    def test_agent_observation_does_not_wake_itself(self):
        task = self.event()['task_id']
        self.changed(task, 'context_searched'); self.changed(task, 'task_started')
        self.assertIsNone(self.event('poll', worker='a')['message'])

    def test_completed_task_wakes_only_for_new_corrections(self):
        task = self.event()['task_id']
        self.changed(task, 'owner_approved')
        self.store.update_run(task, {'status': 'completed'})
        self.assertIsNone(self.event('poll', worker='a')['message'])
        self.changed(task, 'answer_corrected')
        self.assertIsNotNone(self.event('poll', worker='a')['message'])

    def test_late_finish_reading_reaches_completed_host(self):
        task = self.event()['task_id']
        self.store.update_run(task, {'status':'completed'})
        self.changed(task, 'conformance_read')
        self.assertIsNotNone(self.event('poll', worker='a')['message'])

    def test_superseding_task_cancels_pending_resume(self):
        first = self.event()['task_id']; self.changed(first)
        message = self.event('poll', worker='a')['message']
        self.event('new_task', prompt='New task', event_key='next')
        with self.assertRaises(Invalid): self.event('ack', worker='a', message_id=message['id'])
        self.assertIsNone(self.event('poll', worker='b')['message'])


class HostTests(HostCases, OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'hosts.db')
        self.addCleanup(self.store.graph.close)
        self.actor = Identity('person-a', 'Ada Example', 'member', 'agent')
        self.args = {'host': 'claude', 'session_id': 'session-a', 'project': '/example/checkout',
                     'repo': 'example/service', 'prompt': 'Make retry behavior compatible with our API.'}


class HostHTTPTests(SharedServer):
    def setUp(self):
        super().setUp()
        self.project = Path(self.temp.name) / 'project'; self.project.mkdir()
        run_git(self.project, 'init', '-q')
        (self.project / 'a.txt').write_text('before\n')
        run_git(self.project, 'add', 'a.txt')
        run_git(self.project, '-c', 'user.name=Example', '-c', 'user.email=dev@example.invalid', 'commit', '-qm', 'initial')
        self.person = self.post('/api/people', {'name': 'Ada Example', 'role': 'member'}, token=BOOTSTRAP)
        token = self.post('/api/tokens', {'person_id': self.person['id']}, token=BOOTSTRAP)['token']
        self.config = {'url': self.base + '/mcp', 'token': token, 'project': str(self.project), 'repo': 'example/service'}
        self.payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'session-real-http',
                        'cwd': str(self.project), 'prompt': 'Fix the typo without changing behavior.'}

    def deadline_command(self, budget):
        install_host_hooks(self.project, str(self.project), self.config['url'], self.config['token'],
                           ['claude'], repo=self.config['repo'])
        script = self.project / '.raven/host.py'
        # Execute the unchanged installed module with a shorter test-only clock
        # budget. Production setup always uses the fixed 45-second budget.
        bootstrap = ('import runpy,sys; path=sys.argv[1]; budget=float(sys.argv[2]); '
            'loaded=runpy.run_path(path); loaded["main"].__globals__["HOOK_BUDGET_SECONDS"]=budget; '
            'sys.argv=[path,"hook","--host","claude"]; loaded["main"]()')
        return [sys.executable, '-c', bootstrap, str(script), str(budget)]

    def test_installed_hook_deadline_blocks_and_retry_recovers_completed_registration(self):
        from bridge import canvas
        original = canvas.start_task
        created, release = threading.Event(), threading.Event()

        def delayed(*args, **kwargs):
            result = original(*args, **kwargs)
            created.set()
            release.wait(5)
            return result

        command = self.deadline_command(.4)
        began = time.monotonic()
        try:
            with patch.object(canvas, 'start_task', side_effect=delayed):
                result = subprocess.run(command, input=json.dumps(self.payload), capture_output=True,
                                        text=True, cwd=self.project, timeout=3)
                self.assertTrue(created.is_set(), 'The deadline must interrupt the registered-request response')
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stdout)['decision'], 'block')
                self.assertIn('deadline', result.stderr)
                self.assertLess(time.monotonic() - began, 2)
                self.assertNotIn(self.config['token'], result.stdout + result.stderr)
        finally:
            release.set()
        first = self.store.graph.db.execute('SELECT id,goal FROM runs').fetchone()
        retry = hc.hook(self.config, 'claude', self.payload)
        self.assertIn(first['id'], retry['hookSpecificOutput']['additionalContext'])
        self.assertEqual(first['goal'], self.payload['prompt'])
        self.assertEqual(self.store.graph.db.execute('SELECT count(*) n FROM runs').fetchone()['n'], 1)

    def test_default_budget_leaves_time_for_explicit_block_before_host_ceiling(self):
        self.deadline_command(.1)
        settings = json.loads((self.project / '.claude/settings.local.json').read_text())
        for event in ('SessionStart', 'UserPromptSubmit'):
            self.assertLess(hc.HOOK_BUDGET_SECONDS, settings['hooks'][event][0]['hooks'][0]['timeout'])
        self.assertEqual(hc.HOOK_BUDGET_SECONDS, 45)

    def test_installed_command_exits_two_even_if_response_read_never_returns(self):
        command = self.deadline_command(.15)
        prefix = ('import runpy,sys,threading,types; path=sys.argv[1]; '
            'loaded=runpy.run_path(path); scope=loaded["main"].__globals__; '
            'scope["HOOK_BUDGET_SECONDS"]=float(sys.argv[2]); '
            'response=type("SlowBody",(),{"__enter__":lambda self:self,'
            '"__exit__":lambda self,*args:False,'
            '"read":lambda self,limit:threading.Event().wait(10)}); '
            'scope["build_opener"]=lambda *args:types.SimpleNamespace(open=lambda *a,**kw:response()); '
            'sys.argv=[path,"hook","--host","claude"]; loaded["main"]()')
        command[2] = prefix
        began = time.monotonic()
        result = subprocess.run(command, input=json.dumps(self.payload), capture_output=True,
                                text=True, cwd=self.project, timeout=3)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['decision'], 'block')
        self.assertIn('deadline', result.stderr)
        self.assertLess(time.monotonic() - began, 2)
        self.assertNotIn(self.config['token'], result.stdout + result.stderr)
        self.assertEqual(self.store.graph.db.execute('SELECT count(*) n FROM runs').fetchone()['n'], 0)

    def test_actual_hook_http_handshake_registration_and_no_evidence_as_instructions(self):
        response = hc.hook(self.config, 'claude', self.payload)
        context = response['hookSpecificOutput']['additionalContext']
        task = self.store.graph.db.execute('SELECT * FROM runs').fetchone()
        self.assertIn(task['id'], context)
        self.assertEqual(task['goal'], self.payload['prompt'])
        self.assertEqual(task['requester'], 'Ada Example')
        hc.hook(self.config, 'claude', self.payload)
        self.assertEqual(self.store.graph.db.execute('SELECT count(*) n FROM runs').fetchone()['n'], 1)
        self.assertNotIn(self.config['token'], context)
        self.assertNotIn(self.payload['prompt'], context)

    def test_installed_hook_executes_as_a_separate_process_with_private_configuration(self):
        install_host_hooks(self.project, str(self.project), self.config['url'], self.config['token'],
                           ['claude'], repo=self.config['repo'])
        command = [sys.executable, str(self.project / '.raven/host.py'), 'hook', '--host', 'claude']
        start = {**self.payload, 'hook_event_name':'SessionStart'}
        for payload in (start, self.payload, self.payload):
            result = subprocess.run(command, input=json.dumps(payload), capture_output=True, text=True,
                                    cwd=self.project, timeout=30, check=True)
            self.assertEqual(json.loads(result.stdout)['hookSpecificOutput']['hookEventName'], payload['hook_event_name'])
            self.assertNotIn(self.config['token'], result.stdout + result.stderr)
        self.assertEqual(self.store.graph.db.execute('SELECT count(*) n FROM runs').fetchone()['n'], 1)

    def test_real_mailbox_supervisor_releases_failure_and_acks_success(self):
        hc.hook(self.config, 'claude', self.payload)
        task = self.store.graph.db.execute('SELECT id FROM runs').fetchone()['id']
        with self.store.graph.transaction():
            self.store.graph.append_event('signature', {'task_id': task, 'by': 'Reviewer Example'})
        calls = []
        def failed(command, **kw):
            calls.append((command,kw)); return subprocess.CompletedProcess(command, 1)
        first = hc.watch_once(self.config, 'claude', self.payload['session_id'], 'worker-a', failed)
        self.assertFalse(first['resumed'])
        def passed(command, **kw): return subprocess.CompletedProcess(command, 0)
        second = hc.watch_once(self.config, 'claude', self.payload['session_id'], 'worker-b', passed)
        self.assertTrue(second['resumed'])
        self.assertEqual(first['message_id'], second['message_id'])
        self.assertEqual(calls[0][0][:6], ['claude','--print','--resume','session-real-http','--permission-mode','default'])
        self.assertNotIn('shell', calls[0][1])

    def test_finish_captures_exact_diff_and_rejects_untracked_files(self):
        hc.hook(self.config, 'claude', self.payload)
        task = self.store.graph.db.execute('SELECT id FROM runs').fetchone()['id']
        with self.assertRaises(ValueError): hc.finish(self.config, task, None, '')
        (self.project / 'a.txt').write_text('after\n')
        expected = subprocess.check_output(['git','-C',str(self.project),'diff','--binary','--no-ext-diff','--no-textconv','HEAD','--'])
        with patch('os.getcwd',return_value=str(self.project)), patch.object(hc,'call',return_value={}) as call:
            hc.finish(self.config,task,'HEAD','tests claimed by host')
            self.assertEqual(call.call_args.args[2]['diff'].encode(), expected)
            (self.project / 'new.txt').write_text('not staged')
            with self.assertRaisesRegex(ValueError,'untracked'):
                hc.finish(self.config,task,'HEAD','')

    def test_hook_install_is_private_ignored_repeatable_and_preserves_user_hooks(self):
        path = self.project / '.claude/settings.local.json'; path.parent.mkdir()
        original = {'hooks':{'SessionStart':[{'hooks':[{'type':'command','command':'echo existing'}]}]},'permissions':{'deny':['Bash(rm *)']}}
        path.write_text(json.dumps(original))
        kwargs = (self.project,str(self.project),self.config['url'],self.config['token'],['claude','codex'],'example/service')
        install_host_hooks(*kwargs)
        once = path.read_text(); install_host_hooks(*kwargs)
        self.assertEqual(path.read_text(),once)
        self.assertEqual(json.loads(once)['permissions'],original['permissions'])
        credential = self.project / '.raven/host-config.json'
        self.assertEqual(credential.stat().st_mode & 0o777,0o600)
        untracked = subprocess.check_output(['git','-C',str(self.project),'ls-files','--others','--exclude-standard']).decode()
        self.assertEqual(untracked,'')
        self.assertNotIn(self.config['token'],once)
