"""Agent REST completion must enforce the same observed-answer gate as MCP."""
import json
from urllib.error import HTTPError

from bridge.authz import Actor
from test_auth import BOOTSTRAP, SharedServer


class AgentRestReadTests(SharedServer):
    def setUp(self):
        super().setUp()
        self.person = self.store.add_person({'name': 'Read Fixture Owner', 'role': 'member'})
        self.addCleanup(self.server.server_close)
        self.agent = self.auth.create_token(self.person['id'], label='read-parity-fixture')['token']

    def task(self, answered=True):
        task = self.store.add_run({'title': 'Read parity fixture', 'repo': 'fixture/read-parity'})['id']
        with self.store.graph.transaction():
            node = self.store.graph.add_decision(task, 'Which value applies?', '', 'pending', owner=self.person['name'])
        if answered:
            self.answer(node, 'Use two units.')
        return task, node

    def answer(self, node, answer):
        row = self.store.get_decision(node)
        self.store.answer(node, {'answer': answer, 'expected_updated_at': row['updated_at']},
                          actor=Actor.person(self.person, kind='session'))

    def assert_unread(self, task, path, data):
        with self.assertRaises(HTTPError) as refused:
            self.post(path, data, token=self.agent)
        self.assertEqual(refused.exception.code, 400)
        self.assertIn('have not seen', json.load(refused.exception)['error'])
        self.assertEqual(self.store.graph.get_task(task)['status'], 'working')

    def test_same_agent_cannot_skip_mcp_read_gate_with_rest_finish(self):
        task, _ = self.task()
        self.assertTrue(self.mcp('bridge_finish_task', {'task_id': task}, self.agent)['isError'])
        self.assert_unread(task, f'/api/tasks/{task}/finish', {})
        read = self.get(f'/api/tasks/{task}/tree', token=self.agent)
        self.assertTrue(read['read_acknowledged'])
        self.assertEqual(read['nodes'][0]['answer'], 'Use two units.')
        self.assertEqual(self.post(f'/api/tasks/{task}/finish', {}, token=self.agent)['status'], 'completed')

    def test_mcp_read_satisfies_rest_finish_and_rest_read_satisfies_mcp(self):
        task, _ = self.task()
        self.mcp('bridge_get_tree', {'task_id': task}, self.agent)
        self.assertEqual(self.post(f'/api/tasks/{task}/finish', {}, token=self.agent)['status'], 'completed')
        task, _ = self.task()
        self.get(f'/api/tasks/{task}/tree', token=self.agent)
        self.assertFalse(self.mcp('bridge_finish_task', {'task_id': task}, self.agent)['isError'])

    def test_correction_requires_a_new_agent_read_not_an_operator_page_view(self):
        task, node = self.task()
        self.get(f'/api/tasks/{task}/tree', token=self.agent)
        self.answer(node, 'Use three units instead.')
        self.get(f'/api/tasks/{task}/tree', token=BOOTSTRAP)
        self.assert_unread(task, f'/api/tasks/{task}/finish', {})
        read = self.get(f'/api/tasks/{task}/tree', token=self.agent)
        self.assertEqual(read['nodes'][0]['answer'], 'Use three units instead.')
        self.assertEqual(self.post(f'/api/tasks/{task}/finish', {}, token=self.agent)['status'], 'completed')

    def test_legacy_agent_status_completion_has_the_same_read_gate(self):
        task, _ = self.task()
        self.assertEqual(self.post(f'/api/runs/{task}/status', {'status': 'working'}, token=self.agent)['status'], 'working')
        self.assert_unread(task, f'/api/runs/{task}/status', {'status': 'completed'})
        self.get(f'/api/tasks/{task}/tree', token=self.agent)
        self.assertEqual(self.post(f'/api/runs/{task}/status', {'status': 'completed'}, token=self.agent)['status'], 'completed')

    def test_operator_completion_does_not_require_an_agent_receipt(self):
        task, _ = self.task()
        self.assertEqual(self.post(f'/api/tasks/{task}/finish', {}, token=BOOTSTRAP)['status'], 'completed')

    def test_reading_pending_answer_does_not_grant_authority(self):
        task, _ = self.task(answered=False)
        self.get(f'/api/tasks/{task}/tree', token=self.agent)
        self.assertEqual(self.status_of('POST', f'/api/tasks/{task}/finish', {}, token=self.agent), 400)
        self.assertEqual(self.store.graph.get_task(task)['status'], 'working')

    def assert_completion_paths_refuse(self, task):
        self.assertTrue(self.mcp('bridge_finish_task', {'task_id': task}, self.agent)['isError'])
        self.assertEqual(self.status_of('POST', f'/api/tasks/{task}/finish', {}, token=self.agent), 400)
        self.assertEqual(self.status_of('POST', f'/api/runs/{task}/status', {'status': 'completed'}, token=self.agent), 400)
        self.assertNotEqual(self.store.graph.get_task(task)['status'], 'completed')

    def test_reading_an_unapproved_proposal_does_not_allow_any_completion_path(self):
        task, node = self.task(answered=False)
        proposed = self.post(f'/api/tasks/{task}/settle', {'node_id': node, 'answer': 'Use seven units.'}, token=self.agent)
        self.assertFalse(proposed['authorized'])
        self.get(f'/api/tasks/{task}/tree', token=self.agent)
        self.assert_completion_paths_refuse(task)

    def test_reading_a_stale_signed_dependency_does_not_restore_authority(self):
        task, parent = self.task()
        with self.store.graph.transaction():
            child = self.store.graph.add_decision(task, 'How does this dependent use the value?', '',
                                                  'pending', owner=self.person['name'])
            self.store.graph.add_link(child, parent, 'depends')
        self.answer(child, 'Use the parent value unchanged.')
        self.get(f'/api/tasks/{task}/tree', token=self.agent)
        self.answer(parent, 'Use four units instead.')
        stale = self.store.get_decision(child)
        self.assertTrue(stale['needs_review'])
        self.assertNotEqual(stale['signoff'], 'signed')
        self.get(f'/api/tasks/{task}/tree', token=self.agent)
        self.assert_completion_paths_refuse(task)

    def test_pending_dependency_still_blocks_after_read_acknowledgment(self):
        task, parent = self.task(answered=False)
        with self.store.graph.transaction():
            child = self.store.graph.add_decision(task, 'How should the signed dependent behave?', '',
                                                  'pending', owner=self.person['name'])
            self.store.graph.add_link(child, parent, 'depends')
        self.answer(child, 'Follow the parent once it is decided.')
        self.get(f'/api/tasks/{task}/tree', token=self.agent)
        self.assert_completion_paths_refuse(task)

    def test_legacy_completion_uses_canonical_pending_discovery_gate(self):
        task = self.store.add_run({'title': 'Pending discovery fixture'})['id']
        self.store.graph.db.execute('UPDATE runs SET discovery=? WHERE id=?',
                                    (json.dumps({'model_pending': 'still-reading'}), task))
        for path in (f'/api/tasks/{task}/finish', f'/api/runs/{task}/status'):
            result = self.post(path, {'status': 'completed'}, token=self.agent)
            self.assertEqual(result['status'], 'working')
            self.assertFalse(result['finished'])
            self.assertTrue(result['model_pending'])
        self.assertEqual(self.store.graph.get_task(task)['status'], 'working')
