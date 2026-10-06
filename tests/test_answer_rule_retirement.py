"""A replacement human answer cannot inherit the preceding standing grant."""
import json
from unittest.mock import patch

from bridge import canvas, proof
from test_rules import RuleCase
import test_rule_invalidation as invalidation_fixtures


class AnswerRuleRetirementTests(RuleCase):
    covered = invalidation_fixtures.RuleInvalidationTests.covered
    save_proof = invalidation_fixtures.RuleInvalidationTests.save_proof

    def replacement(self, source, **data):
        before = self.store.get_decision(source)
        return self.store.answer(source, {'answer': 'Round half even', 'rationale': 'Correct the current decision.',
                                         'expected_updated_at': before['updated_at'], **data})

    def test_replacement_requires_fresh_explicit_regrant(self):
        source, task, active = self.covered()
        changed = self.replacement(source)
        self.assertFalse(changed['reusable'])
        self.assertTrue(changed['rule_ended_at'])
        self.assertTrue(changed['authorized'])
        self.assertFalse(canvas.node_view(self.store, active['node_id'])['authorized'])
        self.assertFalse(self.node(self.task('before-regrant'), ref='before-regrant')['authorized'])
        self.rule(source)
        self.assertTrue(self.store.get_decision(source)['reusable'])
        self.assertFalse(self.store.get_decision(source)['rule_ended_at'])
        self.assertTrue(self.node(self.task('after-regrant'), ref='after-regrant')['authorized'])

    def test_same_text_scope_narrowing_retires_rule_and_invalidates_active_consumer(self):
        source, task, active = self.covered()
        before = self.store.get_decision(source)
        changed = self.replacement(source, answer=before['answer'], applicability={'requires': {'customer': 'one'}})
        self.assertFalse(changed['reusable'])
        self.assertEqual(changed['answer'], before['answer'])
        self.assertFalse(canvas.node_view(self.store, active['node_id'])['authorized'])
        signature = json.loads(changed['signatures'])[0]
        self.assertEqual(signature['scope']['reusable'], 0)
        self.assertEqual(signature['scope']['applicability'], {'requires': {'customer': 'one'}})

    def test_plain_signoff_preserves_existing_grant_and_applicability(self):
        source, task, active = self.covered({'paths': ['billing/']})
        before = self.store.get_decision(source)
        canvas.sign_off(self.store, source, {'by': 'Priya Natarajan', 'expected_updated_at': before['updated_at']})
        after = self.store.get_decision(source)
        for key in ('reusable', 'rule_conditions', 'rule_expires', 'rule_scope', 'rule_ended_at', 'applicability'):
            self.assertEqual(after[key], before[key])
        self.assertTrue(canvas.node_view(self.store, active['node_id'])['authorized'])

    def test_completed_consumer_proof_and_signature_history_are_never_rewritten(self):
        source, task, completed = self.covered()
        old_proof = self.save_proof(task)
        historical = self.store.get_decision(completed['node_id'])
        old_events = [dict(e) for e in self.graph.db.execute('SELECT * FROM events ORDER BY id')]
        self.replacement(source)
        exported = proof.export(self.store, {'task_id': task})
        self.assertEqual(exported['bundle'], old_proof)
        self.assertTrue(exported['integrity']['valid'])
        self.assertTrue(exported['stale'])  # Changed answer is still a correction.
        current = self.store.get_decision(completed['node_id'])
        for key in ('answer', 'signatures', 'signed_hash', 'signed_revision'):
            self.assertEqual(current[key], historical[key])
        for event in old_events:
            self.assertEqual(dict(self.graph.db.execute('SELECT * FROM events WHERE id=?', (event['id'],)).fetchone()), event)

    def test_grant_retirement_and_consumer_invalidation_roll_back_together(self):
        source, task, active = self.covered()
        before = self.store.get_decision(source)
        with patch.object(self.graph, 'flag_dependents', side_effect=RuntimeError('synthetic invalidation failure')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic'):
                self.replacement(source)
        after = self.store.get_decision(source)
        for key in ('answer', 'signatures', 'reusable', 'rule_ended_at', 'updated_at'):
            self.assertEqual(after[key], before[key])
        self.assertTrue(canvas.node_view(self.store, active['node_id'])['authorized'])
        self.assertEqual(self.graph.count_events('rule_ended', decision_id=source), 0)

    def test_same_text_record_on_signed_agent_answer_retires_only_standing_grant(self):
        task_source = self.task('signed-agent-source')
        source = self.node(task_source)['node_id']
        canvas.settle_node(self.store, {'task_id': task_source, 'node_id': source,
                            'answer': 'Round half up to whole cents', 'rationale': 'Agent proposes the existing policy.'})
        row = self.store.get_decision(source)
        canvas.sign_off(self.store, source, {'by': 'Priya Natarajan', 'expected_updated_at': row['updated_at']})
        self.assertEqual(self.store.get_decision(source)['status'], 'resolved')
        self.rule(source)
        canvas.finish_task(self.store, {'task_id': task_source})
        task = self.task('completed-rule-consumer')
        self.assertTrue(self.node(task, ref='completed-rule-consumer')['authorized'])
        old_proof = self.save_proof(task)
        active = self.node(self.task('another-active'), ref='another-active')
        self.assertTrue(active['authorized'])
        before = self.store.get_decision(source)
        changed = self.replacement(source, answer=before['answer'])
        self.assertFalse(changed['reusable'])
        self.assertFalse(canvas.node_view(self.store, active['node_id'])['authorized'])
        exported = proof.export(self.store, {'task_id': task})
        self.assertEqual(exported['bundle'], old_proof)
        self.assertFalse(exported['stale'])  # Grant retirement alone is prospective.
