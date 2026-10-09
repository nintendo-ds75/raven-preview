"""The minimal UI uses the real task ladder, not a synthetic answer or hosted launch."""
import http.client
import json
from pathlib import Path
import threading
from unittest.mock import patch

from fixtures import OfflineCase, ready_server
from bridge.store import Store


class RunAskHTTPTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.environment = patch.dict('os.environ', {'BRIDGE_MODEL_API': 'none', 'BRIDGE_SEMANTIC': '0', 'BRIDGE_LIVE': '0'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.store = Store(Path(self.temp.name) / 'run-ask-http.db')
        self.g = self.store.graph
        self.addCleanup(self.g.close)
        self.repo = 'synthetic/run-ask-http'
        self.store.add_owner({'name': 'Synthetic Reviewer', 'team': 'Policy', 'patterns': 'docs/*'})
        self.server = ready_server(self.store, host='127.0.0.1', port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.csrf = self.request('GET', '/api/state').get('csrf_token', '')

    def request(self, method, path, data=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        headers = {'Content-Type': 'application/json', 'X-Bridge-CSRF': getattr(self, 'csrf', '')}
        try:
            connection.request(method, path, body=json.dumps(data) if data is not None else None, headers=headers)
            response = connection.getresponse()
            body = json.loads(response.read())
            self.assertIn(response.status, (200, 201), body)
            return body
        finally:
            connection.close()

    def start(self, question, key):
        return self.request('POST', '/api/tasks/start', {'title': question, 'goal': question,
            'agent': 'Raven UI', 'repo': self.repo, 'paths': 'docs/audit.md', 'client_key': key})

    def ask(self, task_id, question, key):
        return self.request('POST', '/api/tasks/' + task_id + '/nodes',
            {'question': question, 'context': '', 'paths': 'docs/audit.md', 'client_ref': key})

    def test_ui_shaped_request_retrieves_static_context_without_claiming_an_answer_or_approval(self):
        self.store.add_record({'repo': self.repo, 'kind': 'doc', 'ref': 'DOC-1',
            'title': 'Synthetic release audit retention policy',
            'body': 'Decision: keep release audit records for eleven days.',
            'author': 'Synthetic Writer', 'status': 'Current', 'paths': ['docs/audit.md']})
        question = 'What does DOC-1 say about release audit retention?'
        task = self.start(question, 'ui-static-task')
        node = self.ask(task['task_id'], question, 'ui-static-question')
        self.assertIn('DOC-1', node['evidence'])
        self.assertIn('eleven days', node['evidence'])
        self.assertIn('no answerability check is available without a model', node['evidence'])
        self.assertFalse(node['authorized'])
        self.assertTrue(node['blocking'])
        self.assertEqual(node['answer'], '')
        trace = self.request('GET', '/api/tasks/' + task['task_id'] + '/trace')
        self.assertTrue(any(e['kind'] == 'node_added' for e in trace['events']))
        self.assertFalse(any(n['state'] == 'sent' for n in trace['notifications']))
        self.assertEqual(self.g.db.execute('SELECT COUNT(*) FROM executions').fetchone()[0], 0)

    def test_ui_shaped_request_reuses_prior_answer_with_fresh_signoff_still_required(self):
        question = 'How long should release audit records remain?'
        old = self.store.add_run({'title': 'Earlier synthetic review', 'repo': self.repo})['id']
        prior = self.g.add_decision(old, question, 'policy', 'pending', repo=self.repo,
            owner='Synthetic Reviewer', path='docs/audit.md')
        self.store.answer(prior, {'answer': 'Keep release audit records for eleven days.',
            'by': 'Synthetic Reviewer', 'rationale': 'Synthetic prior task answer.'})
        old_row = dict(self.g.db.execute('SELECT * FROM decisions WHERE id=?', (prior,)).fetchone())
        task = self.start(question, 'ui-memory-task')
        node = self.ask(task['task_id'], question, 'ui-memory-question')
        self.assertEqual(node['source'], 'memory')
        self.assertEqual(node['source_id'], prior)
        self.assertEqual(node['source_revision'], old_row['updated_at'])
        self.assertEqual(node['answer'], old_row['answer'])
        self.assertEqual(node['signoff'], 'required')
        self.assertFalse(node['authorized'])
        self.assertEqual(dict(self.g.db.execute('SELECT * FROM decisions WHERE id=?', (prior,)).fetchone()), old_row)
        tree = self.request('GET', '/api/tasks/' + task['task_id'] + '/tree')
        self.assertEqual(tree['nodes'][0]['source_id'], prior)
        self.assertGreater(tree['counts']['blocking'], 0)

    def test_exact_http_retries_keep_one_task_question_and_observable_ui_origin(self):
        question = 'Which synthetic report should the team prepare?'
        task = self.start(question, 'ui-retry-task')
        repeated_task = self.start(question, 'ui-retry-task')
        self.assertEqual(repeated_task['task_id'], task['task_id'])
        node = self.ask(task['task_id'], question, 'ui-retry-question')
        repeated = self.ask(task['task_id'], question, 'ui-retry-question')
        self.assertEqual(repeated['node_id'], node['node_id'])
        self.assertTrue(repeated['repeated'])
        self.assertEqual(self.g.db.execute('SELECT COUNT(*) FROM runs WHERE client_key=?', ('ui-retry-task',)).fetchone()[0], 1)
        self.assertEqual(self.g.db.execute('SELECT COUNT(*) FROM decisions WHERE run_id=? AND draft=0', (task['task_id'],)).fetchone()[0], 1)
        run = self.g.db.execute('SELECT agent,goal FROM runs WHERE id=?', (task['task_id'],)).fetchone()
        self.assertEqual(run['agent'], 'Raven UI')
        self.assertEqual(run['goal'], question)
        self.assertEqual(self.g.db.execute('SELECT COUNT(*) FROM executions').fetchone()[0], 0)

    def test_scope_clarification_is_a_blocker_not_a_created_question_and_explicit_retry_recovers(self):
        from bridge.authz import Actor
        question = 'How long should release audit records remain?'
        old = self.store.add_run({'title': 'Earlier scoped review', 'repo': self.repo})['id']
        prior = self.g.add_decision(old, question, 'policy', 'pending', repo=self.repo,
            owner='Synthetic Reviewer', path='docs/audit.md')
        facts = {'customer': 'Synthetic Cedar', 'release': '2026-Q4'}
        self.g.db.execute('UPDATE decisions SET facts=? WHERE id=?', (json.dumps(facts), prior))
        person = self.g.find_person('Synthetic Reviewer')
        self.store.answer(prior, {'answer': 'Keep release audit records for eleven days.',
            'rationale': 'Only for the recorded synthetic customer and release.'},
            actor=Actor.person(person))
        task = self.start(question, 'ui-scope-task')['task_id']
        held = self.ask(task, question, 'ui-scope-question')
        self.assertEqual(held['status'], 'needs_scope_clarification')
        self.assertNotIn('node_id', held)
        self.assertFalse(held['authorized'])
        tree = self.request('GET', '/api/tasks/' + task + '/tree')
        self.assertEqual(tree['nodes'], [])
        self.assertEqual(tree['counts']['blocking'], 1)
        self.assertEqual(tree['facts'], {})
        self.assertEqual(held['scope_clarifications'][0]['missing_keys'], ['customer', 'release'])
        payload = {'question': question, 'context': '', 'paths': 'docs/audit.md',
                   'client_ref': 'ui-scope-question', 'facts': facts}
        node = self.request('POST', '/api/tasks/' + task + '/nodes', payload)
        self.assertIn('node_id', node)
        self.assertEqual(node['facts'], facts)
        self.assertFalse(node['authorized'])
        self.assertEqual(self.request('GET', '/api/tasks/' + task + '/tree')['scope_clarifications'], [])
        again = self.request('POST', '/api/tasks/' + task + '/nodes', payload)
        self.assertEqual(again['node_id'], node['node_id'])
        self.assertTrue(again['repeated'])
