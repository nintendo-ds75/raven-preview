import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase, run_git
from bridge import host_sessions as hs, host_client as hc
from bridge.auth import Identity
from bridge.setup import install_host_hooks
from bridge.store import Store, Invalid
from test_auth import SharedServer, BOOTSTRAP


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
