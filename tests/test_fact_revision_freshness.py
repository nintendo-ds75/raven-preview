"""Correction-local parity with the existing opaque observation guard."""
import json
import unittest
from unittest.mock import patch
from test_fact_revision_boundaries import FactCorrectionBoundaryCase, QUESTION
from bridge.fact_revisions import correct_node_facts


class FactCorrectionFreshnessTests(FactCorrectionBoundaryCase):
    def setup_rule(self):
        source, _ = self.source(QUESTION, conditions='environment=test')
        self.graph.set_setting('auto_rules', '1')
        node = self.node()
        return source, node, self.correction(node['node_id'])

    def receipt(self, node_id):
        return json.loads(self.graph.db.execute("SELECT detail FROM events WHERE decision_id=? AND kind='node_facts_corrected'", (node_id,)).fetchone()['detail'])

    def test_blocked_publication_and_receipt_match_authoritative_guarded_read(self):
        source, node, request = self.setup_rule()
        checks = []
        def blocked(db, repo):
            checks.append(db)
            self.assertIs(db, self.graph.db)
            self.assertTrue(db.in_transaction)
            return {source, node['node_id']}
        with patch('bridge.context_connectors.blocked_decisions', side_effect=blocked):
            corrected = correct_node_facts(self.store, request)
        self.assertGreaterEqual(len(checks), 2)
        with patch('bridge.context_connectors.blocked_decisions', return_value={source, node['node_id']}):
            authoritative = self.store.get_decision(node['node_id'])
        self.assertFalse(corrected['authorized'])
        self.assertTrue(corrected['blocking'])
        self.assertTrue(corrected['source_refresh_required'])
        self.assertFalse(corrected['facts_correction']['applied_authorized'])
        self.assertTrue(corrected['facts_correction']['applied_source_refresh_required'])
        self.assertTrue(corrected['facts_correction']['is_current'])
        self.assertIn('successful refresh', corrected['next'])
        self.assertEqual(corrected['authorized'], authoritative['authorized'])
        self.assertEqual(corrected['source_refresh_required'], authoritative['source_refresh_required'])
        stored = self.receipt(node['node_id'])
        self.assertFalse(stored['published_authorized'])
        self.assertTrue(stored['published_source_refresh_required'])

    def test_later_guarded_replay_keeps_successful_receipt_historical(self):
        source, node, request = self.setup_rule()
        with patch('bridge.context_connectors.blocked_decisions', return_value=set()):
            corrected = correct_node_facts(self.store, request)
        self.assertTrue(corrected['authorized'])
        before = self.receipt(node['node_id'])
        events = self.graph.db.execute('SELECT count(*) FROM events').fetchone()[0]
        with patch('bridge.context_connectors.blocked_decisions', return_value={source, node['node_id']}), \
                patch('bridge.ladder.run_task', side_effect=AssertionError('replay must not reevaluate')), \
                patch.object(self.store, 'notify', side_effect=AssertionError('replay must not notify')):
            replay = correct_node_facts(self.store, request)
        self.assertFalse(replay['authorized'])
        self.assertTrue(replay['source_refresh_required'])
        self.assertTrue(replay['facts_correction']['applied_authorized'])
        self.assertFalse(replay['facts_correction']['applied_source_refresh_required'])
        self.assertFalse(replay['facts_correction']['is_current'])
        self.assertEqual(self.receipt(node['node_id']), before)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM events').fetchone()[0], events)

    def test_recovery_exposes_current_authority_without_rewriting_blocked_receipt(self):
        source, node, request = self.setup_rule()
        with patch('bridge.context_connectors.blocked_decisions', return_value={source, node['node_id']}):
            corrected = correct_node_facts(self.store, request)
        self.assertFalse(corrected['authorized'])
        before = self.receipt(node['node_id'])
        with patch('bridge.context_connectors.blocked_decisions', return_value=set()), \
                patch('bridge.ladder.run_task', side_effect=AssertionError('replay must not reevaluate')), \
                patch.object(self.store, 'notify', side_effect=AssertionError('replay must not notify')):
            recovered = correct_node_facts(self.store, request)
            authoritative = self.store.get_decision(node['node_id'])
        self.assertTrue(recovered['authorized'])
        self.assertFalse(recovered['source_refresh_required'])
        self.assertFalse(recovered['facts_correction']['applied_authorized'])
        self.assertTrue(recovered['facts_correction']['applied_source_refresh_required'])
        self.assertFalse(recovered['facts_correction']['is_current'])
        self.assertEqual(recovered['authorized'], authoritative['authorized'])
        self.assertEqual(self.receipt(node['node_id']), before)
        self.assertEqual(self.graph.count_events('node_facts_corrected', decision_id=node['node_id']), 1)


if __name__ == '__main__':
    unittest.main()
