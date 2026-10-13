from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from fixtures import OfflineCase
from bridge.auth import Identity
from bridge.config import Config
from bridge import host_sessions
from bridge.host_notifications import handle
from bridge.inquiry import ask_task
from bridge.store import Store, Invalid


class HostNotificationTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'notifications.db')
        self.addCleanup(self.store.graph.close)
        self.actor = Identity('employee', 'Employee', 'member', 'agent')
        self.binding = host_sessions.event(self.store, {'event': 'prompt', 'host': 'claude',
            'session_id': 'fixture-session', 'project': '/fixture', 'repo': 'example/service',
            'prompt': 'Test notification delivery', 'tools': ['slack.send_message']}, principal=self.actor)
        self.task = self.binding['task_id']
        self.owner = self.store.add_person({'name': 'Expert Fixture', 'email': 'expert@example.test', 'slack_id': 'UEXPERT'})
        self.node = self.store.graph.add_decision(self.task, 'May we change retry semantics?', '', 'pending', owner=self.owner['name'])
        self.store.delivery.base_url = 'https://raven.example.test'
        self.args = {'action': 'claim', 'binding_id': self.binding['binding_id'], 'task_id': self.task,
            'node_id': self.node, 'revision': self.store.get_decision(self.node)['updated_at'],
            'tool': 'slack.send_message', 'workspace_id': 'TWORKSPACE', 'sender_id': 'UEMPLOYEE',
            'recipient_id': 'UEXPERT', 'recipient_email': 'expert@example.test', 'permission_confirmed': True}

    def call(self, **changes):
        return handle(self.store, {**self.args, **changes}, self.actor)

    def test_persisted_inbox_before_claim_and_no_credentials_or_private_context(self):
        self.assertTrue(self.store.get_decision(self.node))
        result = self.call()
        self.assertTrue(result['send_once'])
        self.assertIn('/#runs/' + self.task + '/' + self.node, result['text'])
        self.assertNotIn('retry semantics', result['text'])
        self.assertNotIn('/brief#', result['text'])
        self.assertFalse(result['receipt_verified'])
        self.assertFalse(self.call()['send_once'])

    def test_concurrent_claims_never_issue_two_sends(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.call(), range(3)))
        self.assertEqual(sum(r['send_once'] for r in results), 1)

    def test_recipient_revision_permission_and_tool_guards(self):
        for change in ({'recipient_email': 'other@example.test'}, {'recipient_id': 'UOTHER'},
                       {'revision': 'stale'}, {'permission_confirmed': False}, {'tool': 'invented.send'}):
            with self.subTest(change=change), self.assertRaises(Invalid):
                self.call(**change)
        self.assertEqual(self.store.graph.db.execute('SELECT count(*) FROM host_notifications').fetchone()[0], 0)

    def test_cross_principal_and_task_are_rejected(self):
        with self.assertRaises(Invalid):
            handle(self.store, self.args, Identity('intruder', 'Other', 'member', 'agent'))
        with self.assertRaises(Invalid):
            self.call(task_id='other-task')

    def test_receipts_are_bound_and_conflicting_terminal_receipt_rejected(self):
        claim = self.call()
        receipt = {'action': 'receipt', 'notification_id': claim['notification_id'],
                   'content_hash': claim['content_hash'], 'state': 'sent', 'external_ref': 'slack:TWORKSPACE:D123:1.1'}
        with self.assertRaises(Invalid):
            self.call(**{**receipt, 'content_hash': 'different'})
        self.assertEqual(self.call(**receipt)['state'], 'sent')
        self.assertEqual(self.call(**receipt)['state'], 'sent')
        with self.assertRaises(Invalid):
            self.call(**{**receipt, 'state': 'unknown'})

    def test_restart_keeps_unknown_and_never_reclaims(self):
        first = self.call()
        recovered = Store(self.store.path)
        try:
            recovered.delivery.base_url = self.store.delivery.base_url
            second = handle(recovered, self.args, self.actor)
            self.assertEqual(second['notification_id'], first['notification_id'])
            self.assertFalse(second['send_once'])
            self.assertEqual(second['state'], 'unknown')
        finally:
            recovered.graph.close()

    def test_closed_task_and_settled_request_do_not_notify(self):
        self.store.graph.db.execute("UPDATE runs SET status='completed' WHERE id=?", (self.task,))
        with self.assertRaisesRegex(Invalid, 'no longer'):
            self.call()
        self.store.graph.db.execute("UPDATE runs SET status='working' WHERE id=?", (self.task,))
        self.store.graph.db.execute("UPDATE decisions SET status='approved',signoff='signed' WHERE id=?", (self.node,))
        with self.assertRaisesRegex(Invalid, 'no longer'):
            self.call()

    def test_server_transport_prevents_duplicate_host_send(self):
        class Transport:
            def post_message(self, *args, **kwargs):
                raise AssertionError('No send from claim')
        self.store.delivery.transport = Transport()
        with self.assertRaisesRegex(Invalid, 'Server notification'):
            self.call()

    def test_no_public_https_url_no_outbound_claim(self):
        self.store.delivery.base_url = 'http://localhost:7331'
        with self.assertRaises(Invalid):
            self.call()

    def test_capability_report_never_claims_backend_can_send(self):
        result = self.call(action='capabilities')
        self.assertFalse(result['backend_can_invoke_host_tools'])
        self.assertFalse(result['inventory_verified'])


class TaskInquiryTests(HostNotificationTests):
    def ask(self, data=None):
        return ask_task(self.store, self.task, data or {'question': 'What decision remains?'}, self.actor,
                        cfg=Config(model_api='none'))

    def test_task_read_does_not_import_neighboring_task_or_create_authority(self):
        other = self.store.add_run({'title': 'Other confidential task', 'repo': 'example/service'})['id']
        hidden = self.store.graph.add_decision(other, 'CONFIDENTIAL_OTHER_TASK', '', 'pending', owner=self.owner['name'])
        before = [self.store.graph.db.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in ('runs', 'decisions', 'host_sessions')]
        result = self.ask()
        self.assertFalse(result['authorized'])
        self.assertNotIn(hidden, [e['id'] for e in result['evidence']])
        self.assertNotIn('CONFIDENTIAL_OTHER_TASK', str(result))
        self.assertEqual(before, [self.store.graph.db.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in ('runs', 'decisions', 'host_sessions')])

    def test_forged_scope_and_mutation_fields_rejected(self):
        for change in ({'task_id': 'other'}, {'focus_id': 'other'}, {'answer': 'Approved'}, {'signoff': True}):
            with self.subTest(change=change), self.assertRaises(Invalid):
                self.ask({'question': 'Explain this', **change})

    def test_revocation_after_model_read_does_not_return_answer(self):
        def revoked():
            raise Invalid('Link revoked')
        with self.assertRaisesRegex(Invalid, 'revoked'):
            ask_task(self.store, self.task, {'question': 'Explain'}, self.actor,
                     cfg=Config(model_api='none'), revalidate=revoked)

from test_auth import SharedServer, BOOTSTRAP  # noqa: E402
from bridge import briefing  # noqa: E402


class TaskInquiryHTTPTests(SharedServer):
    def setUp(self):
        super().setUp()
        self.person = self.post('/api/people', {'name': 'Expert Fixture', 'role': 'viewer'}, token=BOOTSTRAP)
        self.human = self.post('/api/tokens', {'person_id': self.person['id'], 'kind': 'human'}, token=BOOTSTRAP)['token']
        self.task = self.store.add_run({'title': 'Customer fixture task', 'repo': 'example/service'})['id']
        self.node = self.store.graph.add_decision(self.task, 'Which behavior is approved?', '', 'pending')
        with self.store.graph.transaction():
            self.link = briefing.mint(self.store.graph, self.person['id'], self.task, self.node)

    def test_authenticated_viewer_can_ask_without_write_permission(self):
        result = self.post('/api/tasks/' + self.task + '/inquiry', {'question': 'What is pending?', 'focus_id': self.node}, token=self.human)
        self.assertFalse(result['authorized'])
        self.assertEqual(result['task_id'], self.task)

    def test_unauthenticated_task_question_is_rejected(self):
        self.assertEqual(self.status_of('POST', '/api/tasks/' + self.task + '/inquiry', {'question': 'What?'}, token='bad'), 401)

    def brief(self, data, link=None):
        import json
        status, _, body = self.raw('POST', '/api/brief/inquiry',
            {'Content-Type': 'application/json', 'X-Raven-Link': self.link if link is None else link}, json.dumps(data).encode())
        return status, json.loads(body)

    def test_recipient_link_inquiry_and_forged_scope(self):
        status, result = self.brief({'question': 'What is pending?', 'focus_id': self.node})
        self.assertEqual(status, 200)
        self.assertFalse(result['authorized'])
        self.assertEqual(self.brief({'question': 'What?', 'task_id': 'other'})[0], 400)
        self.assertEqual(self.brief({'question': 'What?', 'focus_id': 'other'})[0], 400)
        self.assertEqual(self.brief({'question': 'What?'}, link='rvn_bad')[0], 401)

    def test_revoked_link_cannot_read(self):
        with self.store.graph.transaction():
            self.store.graph.db.execute("UPDATE brief_links SET revoked_at='revoked'")
        self.assertEqual(self.brief({'question': 'What?'})[0], 401)


    def test_workspace_access_revocation_during_read_withholds_result(self):
        from unittest.mock import patch
        from bridge.inquiry import read_evidence
        def revoke(question, evidence, cfg):
            result = read_evidence(question, evidence, cfg)
            self.store.graph.db.execute('UPDATE people SET active=0 WHERE id=?', (self.person['id'],))
            return result
        with patch('bridge.inquiry.read_evidence', side_effect=revoke):
            self.assertEqual(self.status_of('POST', '/api/tasks/' + self.task + '/inquiry',
                                           {'question': 'What is pending?'}, token=self.human), 401)

    def test_decision_change_during_read_withholds_result(self):
        from unittest.mock import patch
        from bridge.inquiry import read_evidence
        def change(question, evidence, cfg):
            result = read_evidence(question, evidence, cfg)
            self.store.graph.db.execute("UPDATE decisions SET answer='Changed concurrently' WHERE id=?", (self.node,))
            return result
        with patch('bridge.inquiry.read_evidence', side_effect=change):
            self.assertEqual(self.status_of('POST', '/api/tasks/' + self.task + '/inquiry',
                                           {'question': 'What is pending?'}, token=self.human), 400)
