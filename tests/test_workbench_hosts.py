"""Offline host contracts. These are not live-provider acceptance results."""
from types import SimpleNamespace
from unittest.mock import patch

from fixtures import OfflineCase
import test_workbench as base
from test_workbench import call
from bridge import workbench_hosts as hosts
from bridge.store import Invalid


class IntentTests(OfflineCase):
    def setUp(self):
        super().setUp()
        # Share the actual git fixture without inheriting or recounting its tests.
        self.root = base.Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'policy.py').write_text('limit = 1\n')
        base.fs.git(self.source, 'init', '--template=', '-b', 'main')
        base.fs.git(self.source, 'add', '.')
        base.fs.git(self.source, '-c', 'user.name=Fixture', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'baseline')
        self.store = base.Store(self.root / 'state.db')
        self.actor = base.Identity('requester', 'Requester', 'member', 'session')
        self.agent = base.ScriptedAgent([call('read_file', {'path': 'policy.py'})], [{'type': 'text', 'text': 'What limit?'}])
        self.service = base.Workbench(self.store, [{'id': 'sample/project', 'path': str(self.source)}], self.root / 'copies',
                                     cfg=base.Config(model_api='none'), agent=self.agent)

    submit = base.WorkbenchTests.submit

    def test_resumed_session_cost_is_not_added_twice_and_restart_repairs_aggregate(self):
        task = self.submit()
        self.store.graph.db.execute('INSERT INTO browser_hosts(task_id,provider,model,cost_usd,updated_at) VALUES(?,?,?,?,?)',
                                   (task, 'claude', 'test', 5.44, 'today'))
        for total in (0.25, 1.16, 1.79, 2.24, 2.24):
            self.service.record(task, 'host', 'Host connected', {'session_id': 'saved-session'})
            self.service.record(task, 'host', 'Host turn ended', {'cost_usd': total})
        hosts.reconcile_cost(self.service, task)
        hosts.reconcile_cost(self.service, task)
        row = self.store.graph.db.execute('SELECT cost_usd FROM browser_hosts WHERE task_id=?', (task,)).fetchone()
        self.assertAlmostEqual(row['cost_usd'], 2.24)
        self.service.record(task, 'host', 'Host connected', {'session_id': 'another-session'})
        self.service.record(task, 'host', 'Host turn ended', {'cost_usd': 0.5})
        hosts.reconcile_cost(self.service, task)
        row = self.store.graph.db.execute('SELECT cost_usd FROM browser_hosts WHERE task_id=?', (task,)).fetchone()
        self.assertAlmostEqual(row['cost_usd'], 2.74)

    def test_completed_intent_is_replayed_without_repeating_effect(self):
        task = self.submit()
        self.service.step(task)
        action = call('edit_file', {'path': 'policy.py', 'old': '1', 'new': '2'}, 'write')
        self.service.update(task, status='running')
        first = hosts.execute_once(self.service, task, action)
        with patch.object(self.service, 'execute', side_effect=AssertionError('replayed effect')):
            self.assertEqual(first, hosts.execute_once(self.service, task, action))

    def test_crash_after_effect_leaves_unknown_and_never_retries(self):
        task = self.submit()
        self.service.step(task)
        action = call('edit_file', {'path': 'policy.py', 'old': '1', 'new': '2'}, 'uncertain')
        def crash(row, operation):
            (self.service.root / task / 'policy.py').write_text('limit = 2\n')
            raise OSError('crash after file write')
        with patch.object(self.service, 'execute', side_effect=crash), self.assertRaises(OSError):
            hosts.execute_once(self.service, task, action)
        with patch.object(self.service, 'execute', side_effect=AssertionError('must not retry')):
            with self.assertRaisesRegex(Invalid, 'unresolved intent'):
                hosts.execute_once(self.service, task, action)
        self.assertEqual((self.service.root / task / 'policy.py').read_text(), 'limit = 2\n')

    def test_call_identity_cannot_be_reused_with_new_arguments(self):
        task = self.submit()
        self.service.step(task)
        with self.assertRaisesRegex(Invalid, 'different arguments'):
            hosts.execute_once(self.service, task, call('read_file', {'path': 'other.py'}))

    def test_call_identity_is_scoped_to_task(self):
        task = self.submit()
        self.service.step(task)
        other = self.submit(request_key='another')
        self.service.agent.turns.insert(0, [call('read_file', {'path': 'policy.py'})])
        self.service.step(other)
        self.assertEqual(self.store.graph.db.execute('SELECT count(*) FROM browser_intents WHERE call_id=?', ('call-1',)).fetchone()[0], 2)

    def test_cancel_prevents_further_effects(self):
        task = self.submit()
        self.service.control(task, 'cancel', self.actor)
        with patch.object(self.service, 'execute', side_effect=AssertionError('cancelled task executed')):
            result = hosts.execute_once(self.service, task, call('run_command', {'command': 'false'}))
        self.assertIn('did not run', result['error'])

    def test_unavailable_executor_pauses_before_the_host_can_retry_another_command(self):
        task = self.submit()
        self.service.step(task)
        self.service.update(task, status='running')
        self.service.sandbox = SimpleNamespace(run=lambda *_: (_ for _ in ()).throw(Invalid('Executor stopped')))
        first = hosts.execute_once(self.service, task, call('run_command', {'command': 'pytest'}, 'first'))
        self.assertIn('Executor stopped', first['error'])
        self.assertEqual(self.service.row(task)['status'], 'paused')
        with patch.object(self.service.sandbox, 'run', side_effect=AssertionError('repeat')):
            second = hosts.execute_once(self.service, task, call('run_command', {'command': 'pytest -v'}, 'second'))
        self.assertIn('did not run', second['error'])

    def test_missing_host_never_falls_back_to_internal_inference(self):
        self.service.agent = None
        with patch.dict('os.environ', {'RAVEN_HOST_CLAUDE_API_KEY': '', 'ANTHROPIC_API_KEY': 'internal-canary'}):
            with self.assertRaisesRegex(Invalid, 'RAVEN_HOST_CLAUDE_API_KEY'):
                self.submit()
        self.assertFalse(self.service.list())

    def test_host_choice_is_bound_to_request_key(self):
        self.service.agent = None
        with patch.object(hosts, 'capability', return_value={'ready': True, 'model': 'model', 'blockers': []}):
            self.submit(host='claude')
            with self.assertRaisesRegex(Invalid, 'different coding host'):
                self.submit(host='codex')

    def test_failed_host_delivery_keeps_user_input_for_retry(self):
        task = self.submit()
        self.service.step(task)
        self.store.graph.db.execute('INSERT INTO browser_hosts(task_id,provider,model,session_id,updated_at) VALUES(?,?,?,?,?)',
                                   (task, 'claude', 'test', 'existing', 'today'))
        self.service.agent = None
        self.service.message(task, {'text': 'Use the second option.', 'request_key': 'reply'}, self.actor)
        with patch.object(self.service.harness, 'run', side_effect=Invalid('Disconnected')):
            self.service.step(task)
        self.assertEqual(self.service.get(task)['pending_messages'][0]['body'], 'Use the second option.')
        self.service.control(task, 'resume', self.actor)
        with patch.object(self.service.harness, 'run', return_value={'prompt_delivered': True}) as run:
            self.service.step(task)
        self.assertIn('Use the second option.', run.call_args.args[2])
        self.assertFalse(self.service.get(task)['pending_messages'])

    def test_migration_is_repeatable_and_preserves_pending_intents(self):
        task = self.submit()
        self.store.graph.db.execute('INSERT INTO browser_intents VALUES(?,?,?,?,?,?,?)',
                                   (task, 'call', 'fingerprint', 'unknown', '{}', 'then', 'then'))
        hosts.migrate(self.store.graph.db)
        hosts.migrate(self.store.graph.db)
        self.assertEqual(self.store.graph.db.execute('SELECT state FROM browser_intents').fetchone()[0], 'unknown')

    def test_reconciliation_requires_stopped_host_admin_and_evidence_and_never_replays(self):
        task = self.submit()
        self.service.step(task)
        action = call('edit_file', {'path': 'policy.py', 'old': '1', 'new': '2'}, 'unknown-write')
        with patch.object(self.service, 'execute', side_effect=OSError('uncertain')), self.assertRaises(OSError):
            hosts.execute_once(self.service, task, action)
        self.assertEqual(self.service.get(task)['unresolved_operations'][0]['arguments'], action['input'])
        admin = base.Identity('admin', 'Operator', 'admin', 'session')
        data = {'call_id': action['id'], 'outcome': 'not_run', 'note': 'The task file and isolated command receipt confirm no write ran.'}
        with self.assertRaisesRegex(Invalid, 'admin'):
            hosts.reconcile(self.service, task, data, self.actor)
        with self.assertRaisesRegex(Invalid, 'Stop the host'):
            hosts.reconcile(self.service, task, data, admin)
        self.service.update(task, status='paused')
        with self.assertRaisesRegex(Invalid, 'explain the evidence'):
            hosts.reconcile(self.service, task, {**data, 'note': ''}, admin)
        hosts.reconcile(self.service, task, data, admin)
        self.assertEqual(self.service.row(task)['status'], 'paused')
        with patch.object(self.service, 'execute', side_effect=AssertionError('must not execute')):
            replay = hosts.execute_once(self.service, task, action)
        self.assertTrue(replay['reconciled'])
        self.assertEqual(replay['operator_id'], admin.id)
        self.assertEqual((self.service.root / task / 'policy.py').read_text(), 'limit = 1\n')
        with self.assertRaisesRegex(Invalid, 'no unresolved'):
            hosts.reconcile(self.service, task, data, admin)

    def test_host_ending_without_wait_tool_does_not_strand_questions(self):
        task = self.submit()
        self.service.step(task)
        node = self.store.graph.add_decision(task, 'Which limit should apply?', 'policy', 'pending', repo='sample/project')
        self.store.graph.db.execute('INSERT INTO browser_hosts(task_id,provider,model,updated_at) VALUES(?,?,?,?)',
                                   (task, 'claude', 'test', 'today'))
        self.service.agent = None
        self.service.update(task, status='queued')
        with patch.object(self.service.harness, 'run', return_value={'prompt_delivered': True}) as run:
            self.service.step(task)
            self.assertEqual(self.service.row(task)['status'], 'waiting')
            for _ in range(3):
                self.service.tick()
            self.assertEqual(run.call_count, 1, 'Idle waiting must not poll the model')
            self.store.graph.db.execute("UPDATE decisions SET answer='Use 2',status='resolved',signoff='not_required',updated_at='later' WHERE id=?", (node,))
            self.service.tick()
            self.assertEqual(run.call_count, 2, 'A changed decision must wake the same host')

    def test_pre_prompt_interruption_preserves_queued_user_input_until_confirmed_delivery(self):
        task = self.submit()
        self.service.step(task)
        self.store.graph.db.execute('INSERT INTO browser_hosts(task_id,provider,model,session_id,updated_at) VALUES(?,?,?,?,?)',
                                   (task, 'pi', 'test', 'existing', 'today'))
        self.service.agent = None
        self.service.message(task, {'text': 'Keep the strict limit.', 'request_key': 'stop-race'}, self.actor)
        with patch.object(self.service.harness, 'run', return_value={'prompt_delivered': False}):
            self.service.step(task)
        self.assertEqual(self.service.row(task)['status'], 'paused')
        self.assertEqual(self.service.get(task)['pending_messages'][0]['body'], 'Keep the strict limit.')
        self.service.control(task, 'resume', self.actor)
        with patch.object(self.service.harness, 'run', return_value={'prompt_delivered': True}) as run:
            self.service.step(task)
        self.assertIn('Keep the strict limit.', run.call_args.args[2])
        self.assertFalse(self.service.get(task)['pending_messages'])

    def test_completed_receipt_replay_still_requires_current_source_access(self):
        task = self.submit()
        self.service.step(task)
        with patch('bridge.visibility.require_task', side_effect=Invalid('Source access revoked')):
            with self.assertRaisesRegex(Invalid, 'Source access revoked'):
                hosts.execute_once(self.service, task, call('read_file', {'path':'policy.py'}))
