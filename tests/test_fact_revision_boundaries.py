"""Synthetic retained-premise and accepted-callback correction boundaries."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import OfflineCase
from test_delivery import FakeSlack
from bridge import canvas, context_memory as cm
from bridge.config import Config
from bridge.fact_revisions import correct_node_facts
from bridge.store import Invalid, Store

REPO = 'sample/widget'
QUESTION = 'Should widget queue batching use the bounded window?'


class FactCorrectionBoundaryCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'boundary.db')
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.graph.upsert_artifact(REPO, 'src/widget.py')
        self.task = canvas.start_task(self.store, Config(model_api='none'), {
            'title': 'Review widget queue batching', 'repo': REPO,
            'paths': 'src/widget.py'})['task_id']

    def node(self, **extra):
        return canvas.add_node(self.store, Config(model_api='none'), {
            'task_id': self.task, 'question': QUESTION, 'paths': 'src/widget.py',
            'client_ref': 'batching', 'facts': 'environment=stage', **extra})

    def correction(self, node_id):
        current = canvas.node_view(self.store, node_id)
        return {'task_id': self.task, 'node_id': node_id,
                'expected_revision': current['fact_revision'], 'correction_ref': 'repair',
                'facts': 'environment=test', 'reason': 'The current task explicitly targets test'}

    def source(self, question, *, applicability=None, conditions='', facts=None):
        task = self.graph.create_task('Existing source policy', repo=REPO)
        source = self.graph.add_decision(task, question, 'policy', 'approved', source='human',
            answer='Use the bounded window.', answered_by='Reviewer One', repo=REPO, path='src/widget.py')
        self.graph.update_decision(source, signoff='signed', signed_by='Reviewer One')
        if applicability is not None:
            self.graph.db.execute('UPDATE decisions SET applicability=? WHERE id=?', (json.dumps(applicability), source))
        if facts is not None:
            self.graph.db.execute('UPDATE decisions SET facts=? WHERE id=?', (json.dumps(facts), source))
        if conditions:
            self.store.make_rule(source, {'by': 'Reviewer One', 'conditions': conditions, 'scope': 'any'})
        version = cm.snapshot_decision(self.graph.db, source, reason='existing-synthetic-policy')
        return source, version


class RetainedPremiseCorrectionTests(FactCorrectionBoundaryCase):
    def scenario(self, constraint, *, value='stage', nested=False):
        node = self.node()
        params = ({'applicability': {'requires': {'environment': value}}} if constraint == 'structured'
                  else {'conditions': 'environment=' + value} if constraint == 'rule'
                  else {'facts': {'environment': value}})
        old, version = self.source('What transport prerequisite applies to queue buffering?', **params)
        target = node['node_id']
        if nested:
            intermediate, intermediate_version = self.source('What current buffer premise supports the batching choice?')
            self.graph.add_link(intermediate, old, 'derived', 'Existing nested premise')
            self.graph.db.execute("UPDATE decision_links SET source_version_id=? WHERE decision_id=? AND related_id=?", (version, intermediate, old))
            intermediate_version = cm.snapshot_decision(self.graph.db, intermediate, reason='existing-nested-premise')
            self.graph.add_link(target, intermediate, 'derived', 'Existing direct premise')
            self.graph.db.execute("UPDATE decision_links SET source_version_id=? WHERE decision_id=? AND related_id=?", (intermediate_version, target, intermediate))
        else:
            self.graph.add_link(target, old, 'derived', 'Existing exact retained premise')
            self.graph.db.execute("UPDATE decision_links SET source_version_id=? WHERE decision_id=? AND related_id=?", (version, target, old))
        cm.snapshot_decision(self.graph.db, target, reason='unsigned-proposal-with-premise')
        current_rule, _ = self.source(QUESTION, conditions='environment=test')
        self.graph.set_setting('auto_rules', '1')
        return node, old, current_rule

    def assert_scope_blocked(self, constraint, nested=False):
        node, old, current_rule = self.scenario(constraint, nested=nested)
        before_history = cm.history(self.graph.db, old)
        before_links = [dict(r) for r in self.graph.db.execute('SELECT * FROM decision_links WHERE decision_id=?', (node['node_id'],))]
        result = correct_node_facts(self.store, self.correction(node['node_id']))
        self.assertEqual(self.store.get_decision(node['node_id'])['source_id'], current_rule)
        self.assertFalse(result['authorized'], result)
        self.assertEqual(result['signoff'], 'required')
        self.assertTrue(result['needs_review'])
        self.assertIn('Retained premise', result['review_reason'])
        self.assertEqual(cm.history(self.graph.db, old), before_history)
        for link in before_links:
            retained = self.graph.db.execute('SELECT source_version_id FROM decision_links WHERE decision_id=? AND related_id=? AND kind=?',
                (link['decision_id'], link['related_id'], link['kind'])).fetchone()
            self.assertEqual(retained['source_version_id'], link['source_version_id'])
        self.assertFalse(result['facts_correction']['applied_authorized'])

    def test_fresh_structured_stage_premise_blocks_new_test_grant(self):
        self.assert_scope_blocked('structured')

    def test_fresh_rule_only_stage_premise_blocks_new_test_grant(self):
        self.assert_scope_blocked('rule')

    def test_retained_explicit_source_scope_is_rechecked(self):
        self.assert_scope_blocked('facts')

    def test_nested_retained_premise_is_rechecked(self):
        self.assert_scope_blocked('structured', nested=True)

    def test_fresh_applicable_retained_premise_allows_new_test_grant(self):
        node, old, current_rule = self.scenario('structured', value='test')
        result = correct_node_facts(self.store, self.correction(node['node_id']))
        self.assertTrue(result['authorized'], result)
        self.assertEqual(result['signoff'], 'rule')
        self.assertFalse(result['needs_review'])
        self.assertEqual(result['signatures'], [])


class AcceptedInteractionCorrectionTests(FactCorrectionBoundaryCase):
    def setUp(self):
        super().setUp()
        self.transport = FakeSlack()
        self.delivery = self.store.connect_delivery(self.transport, base_url='https://example.invalid')
        self.addCleanup(self.delivery.close)
        self.person = self.graph.add_person('Sample Reviewer', email='reviewer@example.invalid', slack_id='USAMPLE')
        self.graph.add_authority('path', 'src/*', 'decides', person_id=self.person, repo=REPO)
        self.current = self.node(owner_id=self.graph.owner_id_for('Sample Reviewer'))
        self.delivery.deliver_now()
        self.message = self.transport.messages[0]

    def receive(self, text, *, occurrence=True):
        metadata = ({'platform': 'slack', 'id': '2000000000.000001', 'timestamp': '2000000000.000001',
                     'reply_to': self.message['ts']} if occurrence else None)
        return self.delivery.receive(self.message['channel'], self.message['ts'], 'USAMPLE', text,
                                     event_id='sample-callback', occurrence=metadata)

    def assert_generic_refusal(self):
        node_id = self.current['node_id']
        self.assertEqual(self.graph.count_events('reply_received', decision_id=node_id), 0)
        before = self.store.get_decision(node_id)['facts']
        with self.assertRaisesRegex(Invalid, 'not eligible for agent fact correction') as refused:
            correct_node_facts(self.store, self.correction(node_id))
        self.assertNotIn('PRIVATE_SAMPLE_TEXT', str(refused.exception))
        self.assertNotIn('private', str(refused.exception).lower())
        self.assertEqual(self.store.get_decision(node_id)['facts'], before)
        self.assertEqual(self.graph.count_events('node_facts_corrected', decision_id=node_id), 0)

    def test_notification_without_a_reply_remains_eligible(self):
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM reply_turns').fetchone()[0], 0)
        result = correct_node_facts(self.store, self.correction(self.current['node_id']))
        self.assertEqual(result['facts'], {'environment': 'test'})
        self.assertTrue(result['facts_correction']['applied'])

    def test_private_conversation_user_turn_blocks_correction_without_reply_event(self):
        with patch('bridge.slack_chat.load', return_value=Config(model_api='none')), \
                patch.object(Config, 'semantic_retrieval', property(lambda self: True)), \
                patch('bridge.slack_chat.reading', return_value={'kind': 'chat', 'reply': 'Please take your time.'}):
            self.receive('PRIVATE_SAMPLE_TEXT: I am still considering the batching behavior.', occurrence=False)
        self.assertIsNotNone(self.graph.db.execute("SELECT 1 FROM slack_conversation WHERE decision_id=? AND role='user'", (self.current['node_id'],)).fetchone())
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM reply_turns').fetchone()[0], 0)
        self.assert_generic_refusal()

    def test_source_readback_blocks_correction_before_any_confirmed_answer(self):
        source = self.store.add_record({'repo': REPO, 'kind': 'ticket', 'ref': 'SAMPLE-1',
            'title': 'Queue context', 'body': 'A bounded window is proposed.'})
        with self.graph.transaction() as db:
            cm.attach(db, self.current['node_id'], [cm.pin(source['source'])])
        self.receive('answer: Use the bounded window for this task only.')
        self.assertIsNotNone(self.delivery._reading(self.message['channel'], self.message['ts'], self.person))
        self.assert_generic_refusal()

    def test_accepted_reply_turn_association_blocks_before_other_reply_paths(self):
        self.receive('yes')
        self.assertIsNotNone(self.graph.db.execute('SELECT 1 FROM reply_turns').fetchone())
        self.assertIsNone(self.delivery._reading(self.message['channel'], self.message['ts'], self.person))
        self.assertIsNone(self.graph.db.execute("SELECT 1 FROM slack_conversation WHERE role='user'").fetchone())
        self.assert_generic_refusal()

    def test_explicit_context_provenance_blocks_correction_without_reply_event(self):
        self.receive('context: The current queue task still needs review.', occurrence=False)
        self.assertEqual(self.graph.count_events('task_note', task_id=self.task), 1)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM reply_turns').fetchone()[0], 0)
        self.assert_generic_refusal()


if __name__ == '__main__':
    unittest.main()
