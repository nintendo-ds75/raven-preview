"""Exact approved-source coverage for retained external correction premises."""
import unittest
from test_fact_revision_boundaries import FactCorrectionBoundaryCase, REPO, QUESTION
from bridge import context_memory as cm
from bridge.fact_revisions import correct_node_facts


class RetainedExternalPinCorrectionTests(FactCorrectionBoundaryCase):
    def record(self, ref):
        return self.store.add_record({'repo': REPO, 'kind': 'ticket', 'ref': ref,
            'title': 'Auxiliary reference ' + ref, 'body': 'The recorded statement remains current.'})['source']

    def approved_source(self, question, pins=(), dependency=None, rule=False):
        task = self.graph.create_task('Existing approved record policy', repo=REPO)
        source = self.graph.add_decision(task, question, 'policy', 'resolved', source='agent',
            answer='Use the bounded window.', path='src/widget.py', repo=REPO)
        with self.graph.transaction() as db:
            if pins:
                cm.attach(db, source, list(pins))
            if dependency:
                child, version = dependency
                self.graph.add_link(source, child, 'derived', 'Approved exact source premise')
                db.execute('UPDATE decision_links SET source_version_id=? WHERE decision_id=? AND related_id=?',
                           (version, source, child))
            self.graph.update_decision(source, status='approved', source='human',
                answered_by='Reviewer One', signoff='signed', signed_by='Reviewer One')
        if rule:
            self.store.make_rule(source, {'by': 'Reviewer One', 'conditions': 'environment=test', 'scope': 'any'})
        version = cm.snapshot_decision(self.graph.db, source, reason='existing-approved-source')
        return source, version

    def scenario(self, *, retained_role='support', grant_role='support', covered=False,
                 nested=False, node_facts='environment=stage'):
        node = self.node(facts=node_facts)
        record = self.record('REFERENCE-A')
        old_pin = cm.pin(record, retained_role)
        with self.graph.transaction() as db:
            cm.attach(db, node['node_id'], [old_pin])
        grant_pins = [cm.pin(record, grant_role)] if covered else [cm.pin(self.record('REFERENCE-B'))]
        dependency = self.approved_source('Which auxiliary reference supports the queue policy?', grant_pins) if nested else None
        rule, _ = self.approved_source(QUESTION, [] if nested else grant_pins, dependency, rule=True)
        self.graph.set_setting('auto_rules', '1')
        return node, old_pin, rule

    def correct(self, node, pin, rule, facts=None):
        self.assertTrue(all(not edge['stale'] for edge in cm.edges(self.graph.db, node['node_id'])))
        before = cm.history(self.graph.db, node['node_id'])
        source_history = cm.history(self.graph.db, rule)
        request = self.correction(node['node_id'])
        if facts is not None:
            request['facts'] = facts
        result = correct_node_facts(self.store, request)
        self.assertEqual(self.store.get_decision(node['node_id'])['source_id'], rule)
        self.assertEqual(cm.history(self.graph.db, rule), source_history)
        self.assertEqual(cm.history(self.graph.db, node['node_id'])[:len(before)], before)
        self.assertIn(pin, [cm.pin(edge, edge['role']) for edge in cm.edges(self.graph.db, node['node_id'])])
        return result

    def test_extra_fresh_support_pin_requires_complete_source_review(self):
        node, pin, rule = self.scenario()
        result = self.correct(node, pin, rule)
        self.assertFalse(result['authorized'])
        self.assertEqual(result['signoff'], 'required')
        self.assertTrue(result['needs_review'])
        self.assertIn('exact approved source chain', result['review_reason'])
        self.assertFalse(result['facts_correction']['applied_authorized'])

    def test_extra_fresh_contradiction_pin_requires_complete_source_review(self):
        node, pin, rule = self.scenario(retained_role='contradiction')
        result = self.correct(node, pin, rule)
        self.assertFalse(result['authorized'])
        self.assertTrue(result['needs_review'])

    def test_exact_record_version_and_blocking_role_in_approved_rule_are_covered(self):
        node, pin, rule = self.scenario(covered=True)
        result = self.correct(node, pin, rule)
        self.assertTrue(result['authorized'], result)
        self.assertEqual(result['signoff'], 'rule')
        self.assertFalse(result['needs_review'])
        self.assertEqual(result['signatures'], [])

    def test_exact_pin_in_version_pinned_approved_chain_is_covered(self):
        node, pin, rule = self.scenario(covered=True, nested=True)
        result = self.correct(node, pin, rule)
        self.assertTrue(result['authorized'], result)
        self.assertEqual(result['signoff'], 'rule')

    def test_informational_role_on_rule_does_not_cover_a_retained_support_pin(self):
        node, pin, rule = self.scenario(covered=True, grant_role='context')
        result = self.correct(node, pin, rule)
        self.assertFalse(result['authorized'])
        self.assertTrue(result['needs_review'])

    def test_unmatched_context_and_work_item_pins_remain_informational(self):
        node, pin, rule = self.scenario(retained_role='context')
        extra = cm.pin(self.record('WORK-ITEM'), 'work_item')
        with self.graph.transaction() as db:
            cm.attach(db, node['node_id'], [extra])
        result = self.correct(node, pin, rule)
        self.assertTrue(result['authorized'], result)
        self.assertFalse(result['needs_review'])
        self.assertIn(extra, [cm.pin(edge, edge['role']) for edge in cm.edges(self.graph.db, node['node_id'])])

    def test_stale_informational_pin_is_retained_without_blocking_fresh_rule(self):
        node, pin, rule = self.scenario(retained_role='context')
        self.store.add_record({'repo': REPO, 'kind': 'ticket', 'ref': 'REFERENCE-A',
            'title': 'Auxiliary reference REFERENCE-A', 'body': 'Informational context was updated.'})
        self.assertTrue(any(edge['stale'] for edge in cm.edges(self.graph.db, node['node_id'])))
        result = correct_node_facts(self.store, self.correction(node['node_id']))
        self.assertTrue(result['authorized'], result)
        self.assertFalse(result['needs_review'])
        retained = cm.edges(self.graph.db, node['node_id'])
        self.assertIn(pin, [cm.pin(edge, edge['role']) for edge in retained])
        self.assertTrue(any(edge['stale'] and edge['role'] == 'context' for edge in retained))

    def test_canonical_repo_binding_alone_does_not_create_unknown_business_scope(self):
        node, pin, rule = self.scenario(covered=True, node_facts='environment=test')
        result = self.correct(node, pin, rule, facts='environment=test,repo=' + REPO)
        self.assertTrue(result['authorized'], result)
        self.assertFalse(result['needs_review'])
        self.assertEqual(result['facts'], {'environment': 'test', 'repo': REPO})


if __name__ == '__main__':
    unittest.main()
