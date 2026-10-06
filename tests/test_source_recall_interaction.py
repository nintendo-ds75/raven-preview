"""Provider-free interactions between proposal recall and source-version authority.

The composer deterministically quotes an actual imported source. Approvals,
source publication, rules, updates and finish use production entry points. No
old answer is repaired with a current pin or declared independent by a fixture.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import canvas, context_memory as cm, llm, proof
from bridge.authz import Actor
from bridge.config import Config
from bridge.store import Invalid, Store

REPO = 'synthetic/source-recall'
QUESTION = 'What retention policy governs diagnostic records?'
ANSWER = 'Retain diagnostic records for fourteen days.'
FACTS = {'customer': 'Northstar', 'environment': 'staging', 'review_kind': 'routine'}
CONTEXT = 'Northstar staging diagnostic retention under DOC-107'


class SourceRecallInteractionTests(OfflineCase):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {'BRIDGE_MODEL_API': 'none', 'BRIDGE_DEEP': '0'})
        env.start()
        self.addCleanup(env.stop)
        for method in ('complete', 'complete_json'):
            guard = patch.object(llm.Client, method, side_effect=AssertionError('Provider calls forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        self.store = Store(Path(self.temp.name) / 'interaction.db')
        self.g = self.store.graph
        self.addCleanup(self.g.close)
        self.store.add_owner({'name': 'Source Reviewer', 'team': 'Synthetic', 'patterns': 'policy/*'})
        self.actor = Actor.person(self.g.find_person('Source Reviewer'), kind='synthetic_test')
        self.payload = {'repo': REPO, 'kind': 'doc', 'ref': 'DOC-107', 'title': 'Diagnostic record retention policy',
                        'body': ANSWER, 'status': 'Current', 'resolved': False, 'paths': ['policy/retention.py'],
                        'provider': 'generic', 'namespace': 'synthetic-docs', 'external_id': 'diagnostics-107'}
        self.store.update_settings({'auto_rules': True})

    def compose(self, cfg, question, citation, body, *args, **kwargs):
        return ANSWER if ANSWER in body else None

    def task(self):
        return self.store.add_run({'title': 'Northstar diagnostic retention review', 'repo': REPO})['id']

    def node(self, facts=None):
        task = self.task()
        # A current-task source anchor uses the actual current import, never
        # a guessed pin or a link retroactively assigned to an old answer.
        imported = self.store.add_record({**self.payload, 'task_id': task})
        with patch('bridge.ladder._compose_answer', side_effect=self.compose):
            node = canvas.add_node(self.store, Config(model_api='none'), {'task_id': task, 'question': QUESTION,
                'context': CONTEXT, 'paths': 'policy/retention.py', 'category': 'data-source',
                'facts': FACTS if facts is None else facts})
        return task, node, imported['source']

    def sign(self, did):
        return canvas.sign_off(self.store, did,
            {'expected_updated_at': self.store.get_decision(did)['updated_at']}, actor=self.actor)

    def rule(self, did):
        return self.store.make_rule(did, {
            'conditions': 'customer=Northstar; environment=staging; review_kind=routine', 'scope': 'same',
            'expires': (datetime.now(timezone.utc) + timedelta(days=7)).date().isoformat(),
            'expected_updated_at': self.store.get_decision(did)['updated_at']}, actor=self.actor)

    def source_rule(self):
        task, node, source = self.node()
        did = node['node_id']
        self.assertEqual(self.store.get_decision(did)['status'], 'proposed')
        signed = self.sign(did)
        self.assertEqual(signed['status'], 'resolved')
        self.rule(did)
        return task, did, source

    def test_current_typed_source_requires_explicit_rule_then_reuses_without_notification(self):
        task, proposal, source = self.node()
        did = proposal['node_id']
        self.assertFalse(proposal['authorized'])
        self.sign(did)
        current = self.store.get_decision(did)
        self.assertFalse(current['reusable'])
        self.assertEqual(current['source_provenance'], 'versioned')
        self.assertFalse(current['source_reuse_uncertain'])
        self.assertEqual([(p['record_id'], p['source_version_id'], p['role']) for p in current['sources']],
                         [(source['record_id'], source['source_version_id'], 'support')])
        _, prior, _ = self.node()
        self.assertFalse(prior['authorized'], 'Pure approval alone grants no standing permission')
        self.rule(did)
        with patch.object(self.store, 'notify', wraps=self.store.notify) as notify:
            later_task, later, _ = self.node()
        self.assertEqual(later['signoff'], 'rule')
        self.assertTrue(later['authorized'])
        self.assertEqual(self.store.get_decision(later['node_id'])['source_id'], did)
        self.assertEqual(notify.call_count, 0)
        self.store.update_run(later_task, {'status': 'completed'})
        _, missing, _ = self.node({'customer': 'Northstar'})
        self.assertFalse(missing['authorized'])

    def test_supersession_revokes_automatic_reuse_and_finish_without_rewriting_proof(self):
        source_task, did, source = self.source_rule()
        self.store.update_run(source_task, {'status': 'completed'})
        bundle = proof.create(self.store, source_task,
                             'diff --git a/policy/retention.py b/policy/retention.py\n+retention_days = 14\n')
        frozen = cm.encoded(bundle)
        active_task, active, _ = self.node()
        self.assertTrue(active['authorized'])
        self.payload = {**self.payload, 'status': 'Superseded', 'resolved': False,
                        'body': 'Superseded. This policy no longer governs diagnostic record retention.'}
        update = self.store.add_record(self.payload)
        self.assertNotEqual(update['source']['source_version_id'], source['source_version_id'])
        self.assertEqual(update['source']['record_id'], source['record_id'])
        old = self.store.get_decision(did)
        self.assertTrue(old['needs_review'])
        self.assertFalse(old['authorized'])
        self.assertEqual(old['sources'][0]['source_version_id'], source['source_version_id'])
        self.assertTrue(old['sources'][0]['stale'])
        derived = self.store.get_decision(active['node_id'])
        self.assertTrue(derived['needs_review'])
        self.assertFalse(derived['authorized'])
        new_task, new, _ = self.node()
        self.assertFalse(new['authorized'])
        self.assertNotEqual(new['signoff'], 'rule')
        for task in (active_task, new_task):
            with self.assertRaises(Invalid):
                self.store.update_run(task, {'status': 'completed'})
        exported = proof.export(self.store, {'task_id': source_task})
        self.assertEqual(cm.encoded(exported['bundle']), frozen)
        self.assertTrue(exported['integrity']['valid'])
        self.assertTrue(exported['stale'])
        signed_history = [json.loads(v['snapshot']) for v in cm.history(self.g.db, did)]
        self.assertTrue(any(v['decision']['signoff'] == 'signed' for v in signed_history))

    def test_unknown_legacy_source_is_evidence_only_and_never_receives_todays_pin(self):
        task = self.task()
        self.store.add_record({**self.payload, 'task_id': task})
        # A legacy source-derived answer has citation prose but no record
        # identity/version edge. Its genuine personal approval remains history.
        did = self.g.add_decision(task, QUESTION, 'data-source', 'proposed', repo=REPO,
            owner='Source Reviewer', path='policy/retention.py', context=CONTEXT, source='record',
            answer=ANSWER, kind='prediction', evidence='doc DOC-107: old citation, exact historical version unknown')
        self.g.db.execute('UPDATE decisions SET facts=? WHERE id=?', (json.dumps(FACTS), did))
        self.sign(did)
        self.rule(did)
        before = self.store.get_decision(did)
        self.assertTrue(before['authorized'])
        self.assertTrue(before['source_reuse_uncertain'])
        self.assertEqual(before['sources'], [])
        self.store.update_run(task, {'status': 'completed'})
        frozen = cm.encoded(proof.create(self.store, task, 'diff --git a/a b/a\n+legacy\n'))
        with self.g.source_scope(task):
            hits = self.g.memory_search(QUESTION, repo=REPO)
        self.assertIn(did, {row['id'] for row in hits})
        later_task, later, _ = self.node()
        self.assertFalse(later['authorized'])
        self.assertNotEqual(later['signoff'], 'rule')
        self.assertTrue(later['source_reuse_requires_review'])
        with self.assertRaises(Invalid):
            self.store.update_run(later_task, {'status': 'completed'})
        self.payload = {**self.payload, 'body': 'A newly observed replacement has seven-day retention.'}
        self.store.add_record(self.payload)
        after = self.store.get_decision(did)
        self.assertEqual(after['sources'], [])
        self.assertEqual(after['source_provenance'], 'unknown')
        for field in ('answer', 'signoff', 'signed_by', 'signed_hash', 'signed_revision', 'signatures'):
            self.assertEqual(after[field], before[field], field)
        exported = proof.export(self.store, {'task_id': task})
        self.assertEqual(cm.encoded(exported['bundle']), frozen)
        self.assertTrue(exported['integrity']['valid'])
        self.assertTrue(exported['current_reuse'][did]['requires_source_review'])

    def test_unsigned_and_rule_propagated_rows_do_not_become_personal_memory(self):
        _, proposal, _ = self.node()
        did = proposal['node_id']
        self.assertNotIn(did, {row['id'] for row in self.g.memory_search(QUESTION, repo=REPO)})
        self.sign(did)
        self.rule(did)
        task, derived, _ = self.node()
        self.assertEqual(derived['signoff'], 'rule')
        with self.g.source_scope(task):
            hits = self.g.memory_search(QUESTION, repo=REPO)
        self.assertIn(did, {row['id'] for row in hits})
        self.assertNotIn(derived['node_id'], {row['id'] for row in hits})
