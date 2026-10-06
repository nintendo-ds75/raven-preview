"""A kept reading must bind the signed question and context it actually read."""
import copy
from unittest.mock import patch

from test_auth import BOOTSTRAP, SharedServer
from bridge import canvas, proof
from bridge.config import Config


class ReviewInputTests(SharedServer):
    OLD_QUESTION = 'Should we bill synthetic usage?'
    NEW_QUESTION = 'Should we exclude synthetic usage?'
    DIFF = 'diff --git a/billing/usage.py b/billing/usage.py\n+bill_synthetic = True\n'

    def setUp(self):
        super().setUp()
        person = self.post('/api/people', {'name': 'Wes', 'email': 'wes@example.test'}, token=BOOTSTRAP)
        self.post('/api/authority', {'person': person['id'], 'scope_kind': 'path', 'scope': 'billing/*',
                                    'role': 'decides', 'repo': 'acme/platform'}, token=BOOTSTRAP)
        self.human = self.post('/api/tokens', {'person_id': person['id'], 'kind': 'human'}, token=BOOTSTRAP)['token']
        self.task = self.post('/api/tasks/start', {'title': 'Review billing behavior', 'repo': 'acme/platform',
                              'paths': 'billing/usage.py'}, token=self.human)['task_id']
        self.node = canvas.add_node(self.store, Config(model_api='none'), {'task_id': self.task,
                                   'question': self.OLD_QUESTION, 'paths': 'billing/usage.py'})['node_id']
        self.approve(self.node)

    def approve(self, node_id, answer='Yes'):
        path = f'/api/decisions/{node_id}'
        row = self.get(path, token=self.human)
        result = self.post(path + '/answer', {'answer': answer, 'rationale': 'Reviewed by the decider',
                           'expected_updated_at': row['updated_at']}, token=self.human)
        self.assertTrue(result['authorized'])
        self.assertEqual(result['signed_by'], 'Wes')
        return result

    def reframe(self, node_id, question):
        path = f'/api/decisions/{node_id}'
        row = self.get(path, token=self.human)
        result = self.post(path + '/reframe', {'question': question, 'rationale': 'Correct the question',
                           'expected_updated_at': row['updated_at']}, token=self.human)
        self.assertFalse(result['authorized'])
        self.assertEqual(result['answer'], '')
        self.assertEqual(result['signatures'], [])
        return result

    def finish(self):
        return canvas.finish_task(self.store, {'task_id': self.task, 'diff': self.DIFF})

    def test_fresh_approval_of_same_answer_after_reframe_needs_new_reading(self):
        calls = []

        def reader(cfg, question, answer, diff, others):
            calls.append((question, answer, diff, others))
            return {'verdict': 'follows' if question == self.OLD_QUESTION else 'departs',
                    'why': 'Controlled offline reading of ' + question, 'requirements': []}

        with patch('bridge.llm.check_conformance', side_effect=reader):
            first = self.finish()
            saved = copy.deepcopy(proof.export(self.store, {'task_id': self.task})['bundle'])
            self.reframe(self.node, self.NEW_QUESTION)
            self.approve(self.node)
            stale = self.get(f'/api/tasks/{self.task}/tree', token=self.human)['review']
            self.assertEqual(len(calls), 1, 'a tree read must not start inference')
            with self.subTest('read-only current state'):
                self.assertEqual(stale['status'], 'stale')
                self.assertTrue(stale['follows'][0]['stale'])
            again = self.finish()
            with self.subTest('explicit finish cache reuse'):
                self.assertNotEqual(again['review']['id'], first['review']['id'])
                self.assertEqual([item['verdict'] for item in again['follows']], ['departs'])
                self.assertEqual([item[0] for item in calls], [self.OLD_QUESTION, self.NEW_QUESTION])
            event = self.store.graph.db.execute("SELECT detail FROM events WHERE run_id=? AND kind='proof_exported' "
                                                'ORDER BY id LIMIT 1', (self.task,)).fetchone()
            import json
            self.assertEqual(json.loads(event['detail'])['bundle'], saved)
            self.assertTrue(proof.verify(saved)['valid'])

    def test_reframed_secondary_question_stales_the_other_readings_context(self):
        old = 'Should we retain monthly invoice history?'
        new = 'Should we remove monthly invoice history?'
        secondary = canvas.add_node(self.store, Config(model_api='none'), {'task_id': self.task,
                                    'question': old, 'paths': 'billing/history.py'})['node_id']
        self.approve(secondary)
        calls = []

        def reader(cfg, question, answer, diff, others):
            calls.append((question, others))
            return {'verdict': 'follows', 'why': 'Controlled offline context reading', 'requirements': []}

        with patch('bridge.llm.check_conformance', side_effect=reader):
            first = self.finish()
            self.assertIn((self.OLD_QUESTION, ((old, 'Yes'),)), calls)
            self.reframe(secondary, new)
            self.approve(secondary)
            tree = self.get(f'/api/tasks/{self.task}/tree', token=self.human)
            self.assertEqual(len(calls), 2)
            self.assertEqual(tree['review']['status'], 'stale')
            self.assertTrue(all(item.get('stale') for item in tree['review']['follows']))
            self.assertEqual([item['node_id'] for item in tree['review']['stale']], [secondary])
            again = self.finish()
            self.assertNotEqual(first['review']['id'], again['review']['id'])
            self.assertEqual(len(calls), 4)
            self.assertIn((self.OLD_QUESTION, ((new, 'Yes'),)), calls)
            self.assertEqual(canvas.get_tree(self.store, self.task)['review']['status'], 'done')

    def test_cosigning_and_timestamp_touches_keep_completed_reading(self):
        reading = {'verdict': 'follows', 'why': 'Controlled offline reading', 'requirements': []}
        with patch('bridge.llm.check_conformance', return_value=reading) as reader:
            first = self.finish()
            person = self.post('/api/people', {'name': 'Reviewer', 'role': 'admin'}, token=BOOTSTRAP)
            self.post('/api/authority', {'person': person['id'], 'scope_kind': 'path', 'scope': 'billing/*',
                                        'role': 'decides', 'repo': 'acme/platform'}, token=BOOTSTRAP)
            human = self.post('/api/tokens', {'person_id': person['id'], 'kind': 'human'}, token=BOOTSTRAP)['token']
            path = f'/api/decisions/{self.node}'
            before = self.get(path, token=human)
            signed = self.post(path + '/signoff', {'expected_updated_at': before['updated_at']}, token=human)
            self.assertIn('Reviewer', signed['signatures'])
            self.assertNotEqual(signed['updated_at'], before['updated_at'])
            self.assertEqual(canvas.get_tree(self.store, self.task)['review']['status'], 'done')
            self.assertEqual(first['review']['id'], self.finish()['review']['id'])
            self.store.graph.db.execute('UPDATE decisions SET updated_at=?, rationale=? WHERE id=?',
                                        ('2099-01-01T00:00:00+00:00', 'Rationale-only clarification', self.node))
            self.assertEqual(canvas.get_tree(self.store, self.task)['review']['status'], 'done')
            self.assertEqual(first['review']['id'], self.finish()['review']['id'])
            self.assertEqual(reader.call_count, 1)

    def test_answer_only_legacy_record_stays_visible_until_explicit_refresh(self):
        import hashlib
        from bridge.store import answer_hash
        diff_hash = hashlib.sha256(self.DIFF.encode()).hexdigest()[:16]
        legacy_id = hashlib.sha256('\n'.join([self.task, diff_hash,
                                             self.node + ':' + answer_hash('Yes')]).encode()).hexdigest()[:12]
        legacy = {'task_id': self.task, 'review_id': legacy_id, 'diff_hash': diff_hash, 'status': 'done',
                  'revisions': {self.node: answer_hash('Yes')},
                  'follows': [{'node_id': self.node, 'question': self.OLD_QUESTION, 'verdict': 'follows',
                               'why': 'Historical controlled reading', 'requirements': []}]}
        self.store.graph.append_event('conformance_read', legacy)
        original = self.store.graph.db.execute("SELECT detail FROM events WHERE run_id=? AND kind='conformance_read'",
                                               (self.task,)).fetchone()['detail']
        with patch('bridge.llm.check_conformance', return_value={
                'verdict': 'follows', 'why': 'New controlled reading', 'requirements': []}) as reader:
            tree = self.get(f'/api/tasks/{self.task}/tree', token=self.human)
            self.assertEqual((tree['review']['status'], tree['review']['read_status']), ('stale', 'done'))
            self.assertNotIn('review_inputs', tree['review'])
            self.assertIn('did not record its question', tree['review']['stale'][0]['why'])
            self.assertEqual(reader.call_count, 0)
            refreshed = self.finish()
            self.assertNotEqual(refreshed['review']['id'], legacy_id)
            self.assertEqual(reader.call_count, 1)
            current = canvas.get_tree(self.store, self.task)['review']
            self.assertEqual(current['status'], 'done')
            self.assertIn(self.node, current['review_inputs'])
        historical = self.store.graph.db.execute("SELECT detail FROM events WHERE run_id=? AND kind='conformance_read' "
                                                 'ORDER BY id LIMIT 1', (self.task,)).fetchone()['detail']
        self.assertEqual(historical, original)

    def test_nested_tree_uses_the_same_input_order_as_finish(self):
        sibling = canvas.add_node(self.store, Config(model_api='none'), {'task_id': self.task,
                                  'question': 'Should we retain monthly invoice history?',
                                  'paths': 'billing/history.py'})['node_id']
        self.approve(sibling)
        child = canvas.add_node(self.store, Config(model_api='none'), {'task_id': self.task,
                                'question': 'Should synthetic charges appear separately on invoices?',
                                'parent_id': self.node, 'paths': 'billing/invoice.py'})['node_id']
        self.approve(child)
        with patch('bridge.llm.check_conformance', return_value={
                'verdict': 'follows', 'why': 'Controlled offline reading', 'requirements': []}) as reader:
            first = self.finish()
            tree = self.get(f'/api/tasks/{self.task}/tree', token=self.human)
            self.assertEqual(tree['review']['status'], 'done')
            self.assertEqual(first['review']['id'], self.finish()['review']['id'])
            self.assertEqual(reader.call_count, 3)

    def test_reframe_while_old_reader_runs_preserves_its_original_inputs(self):
        import threading
        release, entered = threading.Event(), threading.Event()
        calls = []

        def reader(cfg, question, answer, diff, others):
            calls.append(question)
            if question == self.OLD_QUESTION:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('Controlled reader was not released')
            return {'verdict': 'follows', 'why': 'Controlled reading of ' + question, 'requirements': []}

        try:
            with patch('bridge.llm.check_conformance', side_effect=reader), patch.object(canvas, 'FINISH_WAIT', 0.01):
                first = self.finish()
                self.assertEqual(first['review']['status'], 'running')
                self.assertTrue(entered.wait(5))
                worker = canvas._REVIEWS[first['review']['id']]
                saved = copy.deepcopy(proof.export(self.store, {'task_id': self.task})['bundle'])
                self.reframe(self.node, self.NEW_QUESTION)
                self.approve(self.node)
                stale = canvas.get_tree(self.store, self.task)['review']
                self.assertEqual((stale['status'], stale['read_status']), ('stale', 'running'))
                self.assertEqual(calls, [self.OLD_QUESTION])
                release.set()
                worker.join(5)
                self.assertFalse(worker.is_alive())
                stale = canvas.get_tree(self.store, self.task)['review']
                self.assertEqual((stale['status'], stale['read_status']), ('stale', 'done'))
                self.assertEqual(stale['follows'][0]['question'], self.OLD_QUESTION)
                self.assertTrue(stale['follows'][0]['stale'])
                self.assertEqual(stale['review_inputs'], saved['payload']['review']['review_inputs'])
                with patch.object(canvas, 'FINISH_WAIT', 5):
                    again = self.finish()
                self.assertNotEqual(again['review']['id'], first['review']['id'])
                self.assertEqual(calls, [self.OLD_QUESTION, self.NEW_QUESTION])
                self.assertEqual(canvas.get_tree(self.store, self.task)['review']['status'], 'done')
                self.assertTrue(proof.verify(saved)['valid'])
        finally:
            release.set()

    def test_context_only_questions_beyond_primary_reader_cap_change_key(self):
        signed = [{'node_id': f'controlled-{i}', 'question': f'Question {i}?', 'answer': 'Yes',
                   'authorized': True} for i in range(9)]
        calls = []

        def reader(cfg, question, answer, diff, others):
            calls.append((question, others))
            return {'verdict': 'follows', 'why': 'Controlled reading', 'requirements': []}

        before = canvas._review_key(self.task, self.DIFF, signed)
        with patch('bridge.llm.check_conformance', side_effect=reader):
            canvas._conformance(self.store, self.task, signed, self.DIFF)
        self.assertEqual(len(calls), 8)
        self.assertTrue(all(('Question 8?', 'Yes') in others for _, others in calls))
        signed[-1]['question'] = 'Reframed context-only question?'
        after = canvas._review_key(self.task, self.DIFF, signed)
        self.assertNotEqual(before[0], after[0])
        self.assertEqual(before[1], after[1])
