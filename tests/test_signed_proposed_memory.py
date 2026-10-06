"""Pure human approval of static proposals enters memory without creating a rule.

Legacy-status proposals use genuine signatures made through the sign-off API
before restoring only the old status in disposable fixtures. Their supporting
versions come from real imports at creation, not retrospective pins. Unknown
legacy provenance is covered separately by the source/recall interaction tests.
No observed host fixture is rewritten, and no inference provider is used.
"""
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from fixtures import OfflineCase
from bridge import canvas, graph as graph_mod, llm
from bridge.authz import Actor
from bridge.config import Config
from bridge.store import Invalid, Store

REPO = 'synthetic/approval-control'
QUESTION = 'What is the current maximum retention period for diagnostic records?'
CONTEXT = 'Northstar staging diagnostic retention'
FACTS = {'customer': 'Northstar', 'environment': 'staging', 'review_kind': 'routine'}
ANSWER = 'Maximum retention is 14 days for Northstar staging.'
CFG = Config(model_api='none')


class SignedProposedMemoryTests(OfflineCase):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {'BRIDGE_MODEL_API': 'none', 'BRIDGE_DEEP': '0'})
        env.start()
        self.addCleanup(env.stop)
        for name in ('complete', 'complete_json'):
            guard = patch.object(llm.Client, name, side_effect=AssertionError('Provider calls forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        self.store = Store(Path(self.temp.name) / 'proposal.db')
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.store.add_owner({'name': 'Nisha Bell', 'team': 'Synthetic', 'patterns': 'src/packaging/*'})
        self.owner = Actor.person(self.graph.find_person('Nisha Bell'), kind='synthetic_test')
        self.task = self.store.add_run({'title': 'Synthetic static proposal', 'repo': REPO})['id']

    def proposal(self, *, required=(), answer=ANSWER, repo=REPO):
        imported = self.store.add_record({'repo': repo, 'kind': 'doc',
            'ref': 'PROPOSAL-' + uuid.uuid4().hex[:12], 'title': QUESTION,
            'body': answer, 'status': 'Current', 'resolved': False, 'paths': ['src/packaging/']})
        record = self.graph.db.execute(
            'SELECT i.*,s.id AS record_id,s.head_id AS source_version_id FROM intents i '
            'JOIN source_records s ON s.intent_id=i.id WHERE s.id=?',
            (imported['source']['record_id'],)).fetchone()
        did = self.graph.add_decision(self.task, QUESTION, 'data-source', 'pending',
                                      owner='Nisha Bell', repo=repo, path='src/packaging/', context=CONTEXT)
        self.graph.publish_evidence(did, [record], status='proposed', source='record', answer=answer,
                                    evidence='Imported synthetic diagnostic-retention source',
                                    kind='prediction', signoff='required')
        self.graph.db.execute('UPDATE decisions SET facts=?, required_signers=?, signoff=? WHERE id=?',
                              (json.dumps(FACTS), json.dumps(list(required)), 'required', did))
        return did

    def sign(self, did, actor=None):
        row = self.store.get_decision(did)
        actor = actor or self.owner
        return canvas.sign_off(self.store, did, {'by': actor.name, 'expected_updated_at': row['updated_at']},
                               actor=actor)

    def signed(self, *, legacy=False, **kwargs):
        did = self.proposal(**kwargs)
        self.sign(did)
        if legacy:
            # The old pure-approval writer left exactly this status behind.
            # Preserve the real signature, its revision and provenance.
            self.graph.db.execute("UPDATE decisions SET status='proposed' WHERE id=?", (did,))
        return did

    def make_rule(self, did):
        row = self.store.get_decision(did)
        expires = (datetime.now(timezone.utc) + timedelta(days=7)).date().isoformat()
        return self.store.make_rule(did, {'conditions': 'customer=Northstar; environment=staging; review_kind=routine',
                                          'scope': 'same', 'expires': expires,
                                          'expected_updated_at': row['updated_at']}, actor=self.owner)

    def later_node(self, facts=None):
        task = self.store.add_run({'title': 'Later retention review', 'repo': REPO})['id']
        return canvas.add_node(self.store, CFG, {'task_id': task, 'question': QUESTION, 'context': CONTEXT,
                                                'paths': 'src/packaging/', 'facts': facts if facts is not None else FACTS})

    def assert_in_memory(self, did, present=True):
        checks = {
            'hybrid': {row['id'] for row in self.graph.memory_search(QUESTION, repo=REPO)},
            'recent': {row.id for row in self.graph.recent_answered(REPO)},
            'bounded': {row['id'] for row in self.graph._bounded_rows(graph_mod.MEMORY_STATUSES, REPO, QUESTION)},
            'similar': {row.id for _, row in self.graph.similar_answered(llm.embed(QUESTION), repo=REPO, query=QUESTION)},
        }
        for label, ids in checks.items():
            with self.subTest(reader=label):
                self.assertEqual(did in ids, present)

    def test_pure_static_approval_resolves_proposal_without_making_a_rule(self):
        did = self.proposal()
        before = self.store.get_decision(did)
        self.assertFalse(before['authorized'])
        signed = self.sign(did)
        self.assertEqual((signed['status'], signed['signoff']), ('resolved', 'signed'))
        self.assertTrue(signed['authorized'])
        self.assertFalse(signed['reusable'])
        after = self.store.get_decision(did)
        for field in ('answer', 'source', 'context', 'evidence', 'facts'):
            self.assertEqual(after[field], before[field], field)
        self.assert_in_memory(did)
        later = self.later_node()
        self.assertEqual(self.store.get_decision(later['node_id'])['source_id'], did)
        self.assertEqual(later['signoff'], 'required')
        self.assertFalse(later['authorized'])

    def test_explicit_rule_opt_in_enables_matching_automatic_reuse(self):
        did = self.signed()
        self.store.update_settings({'auto_rules': True})
        before = self.later_node()
        self.assertFalse(before['authorized'], 'A personal signature alone grants no standing permission')
        self.assertFalse(self.store.get_decision(did)['reusable'])
        self.make_rule(did)
        later = self.later_node()
        self.assertEqual(self.store.get_decision(later['node_id'])['source_id'], did)
        self.assertEqual(later['signoff'], 'rule')
        self.assertTrue(later['authorized'])

    def test_legacy_signed_proposal_recall_does_not_rewrite_history(self):
        did = self.signed(legacy=True)
        self.graph.db.execute("UPDATE runs SET status='completed' WHERE id=?", (self.task,))
        before = dict(self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone())
        events = [dict(row) for row in self.graph.db.execute('SELECT * FROM events WHERE decision_id=?', (did,))]
        self.assert_in_memory(did)
        self.assertEqual(before, dict(self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()))
        self.assertEqual(events, [dict(row) for row in self.graph.db.execute('SELECT * FROM events WHERE decision_id=?', (did,))])
        self.assertEqual(self.store.get_decision(did)['status'], 'proposed')

    def test_legacy_signed_proposal_rule_can_authorize_matching_facts_only(self):
        did = self.signed(legacy=True)
        self.make_rule(did)
        self.store.update_settings({'auto_rules': True})
        later = self.later_node()
        self.assertEqual(self.store.get_decision(later['node_id'])['source_id'], did)
        self.assertEqual(later['signoff'], 'rule')
        self.assertTrue(later['authorized'])
        missing = self.later_node({'customer': 'Northstar'})
        self.assertFalse(missing['authorized'])
        other = self.later_node({**FACTS, 'customer': 'Bluebird'})
        self.assertFalse(other['authorized'])
        self.assertEqual(self.store.get_decision(did)['status'], 'proposed')

    def test_proposals_stay_provisional_until_every_required_person_signs(self):
        for name in ('Uma North', 'Tess West'):
            self.graph.add_person(name)
        uma = Actor.person(self.graph.find_person('Uma North'), kind='synthetic_test')
        tess = Actor.person(self.graph.find_person('Tess West'), kind='synthetic_test')
        did = self.proposal(required=('Nisha Bell', 'Uma North'))
        first = self.sign(did)
        self.assertEqual(self.store.get_decision(did)['status'], 'proposed')
        self.assertFalse(first['authorized'])
        self.assertEqual(first['signatures'], ['Nisha Bell'])
        self.assert_in_memory(did, False)
        # Adding another required person while co-signing never discards a
        # still-current signature or promotes the unfinished proposal.
        self.graph.db.execute('UPDATE decisions SET required_signers=? WHERE id=?',
                              (json.dumps(['Nisha Bell', 'Uma North', 'Tess West']), did))
        second = self.sign(did, uma)
        self.assertEqual(self.store.get_decision(did)['status'], 'proposed')
        self.assertFalse(second['authorized'])
        self.assertEqual(set(second['signatures']), {'Nisha Bell', 'Uma North'})
        self.assert_in_memory(did, False)
        final = self.sign(did, tess)
        self.assertEqual((final['status'], final['signoff']), ('resolved', 'signed'))
        self.assertTrue(final['authorized'])
        self.assertEqual(set(final['signatures']), {'Nisha Bell', 'Uma North', 'Tess West'})
        self.assert_in_memory(did)

    def test_unsigned_and_rule_propagated_proposals_are_not_personal_memory(self):
        unsigned = self.proposal()
        self.assert_in_memory(unsigned, False)
        source = self.signed()
        self.make_rule(source)
        self.store.update_settings({'auto_rules': True})
        propagated = self.later_node()
        self.assertEqual(propagated['signoff'], 'rule')
        self.assertTrue(propagated['authorized'])
        self.graph.db.execute("UPDATE decisions SET status='proposed' WHERE id=?", (propagated['node_id'],))
        self.assert_in_memory(propagated['node_id'], False)
        self.assertEqual(self.graph._memory_count(graph_mod.MEMORY_STATUSES, REPO), 1)

    def test_legacy_proposals_require_live_personal_signature_metadata(self):
        variants = ({'needs_review': 1}, {'superseded_by': 'replacement'}, {'status': 'withdrawn'}, {'draft': 1},
                    {'signoff': 'required'}, {'signoff': 'rule'}, {'signed_by': ''}, {'signed_revision': ''},
                    {'signed_hash': ''}, {'signatures': '[]'}, {'answer': ''})
        for fields in variants:
            with self.subTest(fields=fields):
                did = self.signed(legacy=True)
                self.graph.db.execute('UPDATE decisions SET ' + ','.join(f'{key}=?' for key in fields) + ' WHERE id=?',
                                      [*fields.values(), did])
                self.assert_in_memory(did, False)
        self.assertEqual(self.graph._memory_count(graph_mod.MEMORY_STATUSES, REPO), 0)

    def test_a_stale_signature_request_does_not_promote_a_proposal(self):
        did = self.proposal()
        row = self.store.get_decision(did)
        self.graph.update_decision(did, answer='Changed proposal requires another look.')
        with self.assertRaises(Invalid):
            canvas.sign_off(self.store, did, {'by': self.owner.name, 'expected_updated_at': row['updated_at']},
                            actor=self.owner)
        current = self.store.get_decision(did)
        self.assertEqual(current['status'], 'proposed')
        self.assertFalse(current['authorized'])
        self.assert_in_memory(did, False)

    def test_legacy_proposals_keep_repository_and_model_exclusion_guards(self):
        foreign = self.signed(legacy=True, repo='different/approval-control')
        wanted = self.signed(legacy=True, answer=ANSWER + ' unrelated background' * 100)
        self.assert_in_memory(foreign, False)
        self.assert_in_memory(wanted)
        self.assertEqual(self.graph._memory_count(graph_mod.MEMORY_STATUSES, REPO), 1)
        if self.graph.has_fts:
            self.assertEqual(self.graph._fts_ids('decisions_fts', ['diagnostic', 'retention'], limit=1,
                                                repo=REPO, statuses=graph_mod.MEMORY_STATUSES), {wanted})
        with patch.object(self.graph._local, 'model_exclude', wanted, create=True):
            self.assert_in_memory(wanted, False)
            self.assertEqual(self.graph._memory_count(graph_mod.MEMORY_STATUSES, REPO), 0)

    def test_legacy_proposal_eligibility_precedes_large_memory_budgets(self):
        if not self.graph.has_fts:
            self.skipTest('Backend has no FTS')
        wanted = self.signed(legacy=True, answer=ANSWER + ' unrelated historical prose' * 200)
        with self.graph.transaction():
            for i in range(2001):
                self.graph.add_decision(self.task, f'Colour and font selection {i}', 'ux', 'resolved', repo=REPO,
                                        answer='Use blue and serif.', source='agent')
            for _ in range(650):
                self.proposal()
                self.graph.add_decision(self.task, QUESTION, 'policy', 'resolved', repo='different/approval-control',
                                        answer='Diagnostic retention is 90 days.', source='agent')
        self.assertEqual(self.graph._memory_count(graph_mod.MEMORY_STATUSES, REPO), 2002)
        connection = Mock(wraps=self.graph.db)
        with patch.object(self.graph._local, 'db', connection), \
                patch.object(self.graph, '_row_embedding', wraps=self.graph._row_embedding) as score, \
                patch.object(self.graph, '_memory_rows', wraps=self.graph._memory_rows) as fetch:
            hits = self.graph.memory_search(QUESTION, repo=REPO)
        self.assertIn(wanted, {hit['id'] for hit in hits})
        self.assertTrue(all(hit['repo'] == REPO for hit in hits))
        budget = graph_mod.CANDIDATE_FTS + graph_mod.CANDIDATE_RECENT
        self.assertLessEqual(score.call_count, budget)
        self.assertLessEqual(connection.execute.call_count, 4 + (budget + 399) // 400)
        for call in fetch.call_args_list:
            self.assertIsNotNone(call.kwargs.get('ids'))
            self.assertLessEqual(len(call.kwargs['ids']), budget)
        # Open-question retrieval must not expand to signed proposals.
        self.assertEqual(self.graph._memory_count(('pending',), REPO), 0)
