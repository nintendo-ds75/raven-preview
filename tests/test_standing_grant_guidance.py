"""Recorded reuse terms are presentation, never new-request authorization."""
import copy
import json
import unittest

from bridge.reuse_guidance import standing_grant_view
from test_rules import RuleCase, QUESTION, REPO


class StandingGrantProjectionTests(unittest.TestCase):
    def test_later_grant_is_distinct_from_unchanged_original_rationale(self):
        row = {'reusable': 1, 'rule_scope': 'any', 'rule_conditions': 'customer=Cedar; environment=preview',
               'rule_expires': '2099-01-01T00:00:00Z', 'rule_by': 'Synthetic Reviewer',
               'rule_at': '2026-01-02T00:00:00Z', 'context': 'Original item only.',
               'rationale': 'This original request needs a fresh review.'}
        before = copy.deepcopy(row)
        grant = standing_grant_view(row)
        self.assertEqual(row, before)
        self.assertEqual(grant['scope'], 'any')
        self.assertEqual(grant['conditions'], row['rule_conditions'])
        self.assertEqual(grant['expires_at'], row['rule_expires'])
        self.assertEqual(grant['granted_at'], row['rule_at'])
        self.assertEqual(grant['request_applicability'], 'not_evaluated_by_this_read')
        self.assertIn('older request-specific wording does not itself cancel', grant['notice'])
        self.assertNotIn('authorized', grant)

    def test_display_does_not_turn_expired_or_uncertain_terms_into_a_valid_grant(self):
        grant = standing_grant_view({'reusable': 1, 'rule_expires': '2000-01-01',
                                    'needs_review': 1, 'source_reuse_uncertain': 1})
        self.assertEqual(grant['state'], 'declared')
        self.assertTrue(grant['source_needs_review'])
        self.assertTrue(grant['source_provenance_uncertain'])
        self.assertEqual(grant['expires_at'], '2000-01-01')
        self.assertEqual(grant['request_applicability'], 'not_evaluated_by_this_read')
        self.assertIn('does not establish that the grant is valid', grant['notice'])

    def test_no_grant_and_ended_grant_remain_distinct(self):
        self.assertEqual(standing_grant_view({})['state'], 'not_declared')
        self.assertFalse(standing_grant_view({})['recorded'])
        ended = standing_grant_view({'reusable': 0, 'rule_at': '2026-01-01',
                                    'rule_ended_at': '2026-01-02', 'rule_scope': 'any'})
        self.assertEqual(ended['state'], 'ended')
        self.assertTrue(ended['recorded'])
        self.assertEqual(ended['ended_at'], '2026-01-02')
        self.assertIn('no standing reuse grant of its own', ended['notice'])


class StandingGrantDisplayTests(RuleCase):
    def snapshot(self, source):
        row = dict(self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (source,)).fetchone())
        row.pop('embedding', None)  # Existing retrieval may cache an embedding.
        versions = [dict(r) for r in self.graph.db.execute(
            'SELECT * FROM decision_versions WHERE decision_id=? ORDER BY id', (source,))]
        return row, versions

    def test_search_and_direct_read_label_grant_without_changing_signed_history(self):
        source = self.signed_answer()
        self.rule(source, conditions='plan=enterprise', scope='any', expires='2099-01-01')
        before = self.snapshot(source)
        result = self.store.search(QUESTION, REPO)
        found = next(row for row in result['matches'] if row['id'] == source)
        direct = self.store.get_decision(source)
        self.assertEqual(found['standing_grant'], direct['standing_grant'])
        self.assertEqual(found['standing_grant']['state'], 'declared')
        self.assertEqual(found['standing_grant']['scope'], 'any')
        self.assertEqual(found['standing_grant']['conditions'], 'plan=enterprise')
        self.assertEqual(found['rationale'], before[0]['rationale'])
        self.assertNotIn('New requests still require review.', result['notice'])
        self.assertIn('valid reusable grant may cover', result['notice'])
        self.assertEqual(self.snapshot(source), before)

    def test_ended_grant_stays_visible_as_history_without_reuse_claim(self):
        source = self.signed_answer()
        self.rule(source, conditions='plan=enterprise', scope='any', expires='2099-01-01')
        self.rule(source, end=True)
        before = self.snapshot(source)
        found = next(row for row in self.store.search(QUESTION, REPO)['matches'] if row['id'] == source)
        self.assertEqual(found['standing_grant']['state'], 'ended')
        self.assertFalse(found['reusable'])
        self.assertEqual(found['standing_grant']['request_applicability'], 'not_evaluated_by_this_read')
        self.assertEqual(self.snapshot(source), before)
