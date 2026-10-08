"""Local cache reuse precedes contact-only clarification, without outreach."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
import test_rule_candidates as rule_fixtures
from test_rule_candidates import CFG, REPO, QUESTION, FACTS, ANSWER, CONDITIONS
from bridge import canvas, context_memory as cm
from bridge.config import Config
from bridge import llm
from bridge.store import Store


class RuleBeforeContactTests(OfflineCase):
    note = rule_fixtures.RuleCandidateSelectionTests.note
    raw = rule_fixtures.RuleCandidateSelectionTests.raw

    def grant(self, source_evidence=None, **changes):
        source = self.note(QUESTION, facts={**FACTS, 'work_item': 'AUDIT-21'})
        self.store.answer(source, {'answer': ANSWER, 'rationale': 'Scoped current audit policy.',
            **({'source_evidence': source_evidence} if source_evidence is not None else {})})
        self.store.make_rule(source, {'by': 'Morgan Hale', 'scope': 'any', 'conditions': CONDITIONS,
            'expires': (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
            'expected_updated_at': self.store.get_decision(source)['updated_at'], **changes})
        self.assertTrue(self.graph.db.execute("SELECT 1 FROM contact_observations WHERE decision_id=? "
            "AND outcome='answered'", (source,)).fetchone())
        return source

    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'rule-before-contact.db')
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.store.add_owner({'name': 'Morgan Hale', 'team': 'Queue', 'patterns': 'queue/*'})
        self.store.update_settings({'auto_rules': True})
        for method in ('complete', 'complete_json'):
            guard = patch.object(llm.Client, method, side_effect=AssertionError('No providers'))
            guard.start()
            self.addCleanup(guard.stop)

    def task(self):
        return self.graph.create_task('Current audit request', repo=REPO)

    def add(self, task, facts=None, cfg=CFG, **extra):
        return canvas.add_node(self.store, cfg, {'task_id': task, 'question': QUESTION,
            'context': 'Queue audit review', 'category': 'policy', 'paths': 'queue/audit.py',
            'client_ref': 'current-review', 'facts': json.dumps(FACTS if facts is None else facts),
            **extra})

    def persisted(self):
        keys = {'decisions': 'id', 'decision_versions': 'id', 'decision_source_edges': 'id',
                'decision_links': 'decision_id,related_id,kind', 'node_claims': 'run_id,client_ref',
                'notifications': 'id', 'events': 'id', 'contact_observations': 'id',
                'runs': 'id', 'settings': 'key'}
        return {table: [dict(row) for row in self.graph.db.execute(
            'SELECT * FROM ' + table + ' ORDER BY ' + key)] for table, key in keys.items()}

    def test_broad_current_grant_does_not_require_historical_work_item_fact(self):
        source = self.grant()
        before = self.raw(source)
        task = self.task()
        with patch.object(self.store, 'notify', side_effect=AssertionError('No contact needed')):
            result = self.add(task)
        self.assertTrue(result['authorized'], result)
        self.assertEqual(result['signoff'], 'rule')
        self.assertFalse(result['model_pending'])
        self.assertEqual(result['facts'], FACTS)
        self.assertEqual(self.store.get_decision(result['node_id'])['source_id'], source)
        self.assertEqual(self.raw(source), before)
        self.assertFalse(canvas.get_tree(self.store, task)['scope_clarifications'])
        repeated = self.add(task)
        self.assertEqual(repeated['node_id'], result['node_id'])
        self.assertEqual(self.graph.count_events('node_added', task_id=task), 1)

    def test_broad_current_grant_allows_different_declared_work_item(self):
        self.grant()
        result = self.add(self.task(), {**FACTS, 'work_item': 'AUDIT-22'})
        self.assertTrue(result['authorized'], result)
        self.assertEqual(result['signoff'], 'rule')

    def test_missing_required_business_facts_keeps_clarification_without_probe_rows(self):
        self.grant()
        for missing in ('customer', 'environment'):
            with self.subTest(missing=missing):
                task = self.task()
                before = self.persisted()
                with patch.object(self.store, 'notify', side_effect=AssertionError('No contact yet')):
                    result = self.add(task, {k: v for k, v in FACTS.items() if k != missing})
                self.assertEqual(result['status'], 'needs_scope_clarification')
                self.assertFalse(result['authorized'])
                self.assertEqual(self.persisted(), before)
                self.assertEqual(canvas.get_tree(self.store, task)['nodes'], [])

    def test_disabled_auto_rules_keeps_contact_clarification(self):
        self.grant()
        self.store.update_settings({'auto_rules': False})
        task = self.task()
        before = self.persisted()
        result = self.add(task)
        self.assertEqual(result['status'], 'needs_scope_clarification')
        self.assertEqual(self.persisted(), before)

    def test_changed_business_facts_never_skip_to_rule_authority(self):
        self.grant()
        for key in ('customer', 'environment'):
            with self.subTest(changed=key):
                result = self.add(self.task(), {**FACTS, key: 'different'})
                self.assertFalse(result['authorized'])
                self.assertNotEqual(result.get('signoff'), 'rule')

    def test_expired_grant_keeps_clarification(self):
        self.grant()
        task = self.task()
        before = self.persisted()
        future = (datetime.now(timezone.utc) + timedelta(days=11)).timestamp()
        with patch('bridge.graph.time.time', return_value=future):
            result = self.add(task)
        self.assertEqual(result['status'], 'needs_scope_clarification')
        self.assertEqual(self.persisted(), before)

    def test_ended_grant_keeps_clarification(self):
        source = self.grant()
        self.store.make_rule(source, {'by': 'Morgan Hale', 'end': True})
        task = self.task()
        before = self.persisted()
        result = self.add(task)
        self.assertEqual(result['status'], 'needs_scope_clarification')
        self.assertEqual(self.persisted(), before)

    def test_own_scope_grant_keeps_unknown_work_item_clarification(self):
        self.grant(scope='same')
        task = self.task()
        before = self.persisted()
        result = self.add(task)
        self.assertEqual(result['status'], 'needs_scope_clarification')
        self.assertEqual(self.persisted(), before)

    def test_conflicting_namespace_cannot_use_the_other_installations_grant(self):
        support = self.store.add_record({'repo': REPO, 'provider': 'generic', 'namespace': 'tenant-one',
            'kind': 'doc', 'external_id': 'POLICY-1', 'ref': 'POLICY-1', 'title': 'Queue audit policy',
            'body': ANSWER, 'status': 'Current'})['source']
        source = self.grant(source_evidence=[cm.pin(support)])
        task = self.task()
        self.store.add_record({'repo': REPO, 'provider': 'generic', 'namespace': 'tenant-two',
            'kind': 'ticket', 'external_id': 'WORK-22', 'ref': 'WORK-22', 'title': 'Queue audit review',
            'body': 'Open question about queue audit events.', 'status': 'Open',
            'task_id': task, 'anchor_role': 'work_item'})
        before = self.raw(source)
        result = self.add(task)
        self.assertFalse(result['authorized'])
        self.assertNotEqual(result.get('signoff'), 'rule')
        self.assertEqual(self.raw(source), before)

    def test_material_source_change_cannot_skip_to_rule_authority(self):
        payload = {'repo': REPO, 'kind': 'doc', 'ref': 'POLICY-1', 'title': 'Queue audit policy',
                   'body': ANSWER, 'status': 'Current'}
        support = self.store.add_record(payload)['source']
        self.grant(source_evidence=[cm.pin(support)])
        self.store.add_record({**payload, 'status': 'Superseded'})
        result = self.add(self.task())
        self.assertFalse(result['authorized'])
        self.assertNotEqual(result.get('signoff'), 'rule')

    def test_current_source_refusal_keeps_contact_clarification(self):
        source = self.grant()
        task = self.task()
        before = self.persisted()
        with patch('bridge.context_connectors.blocked_decisions', return_value={source}):
            result = self.add(task)
        self.assertEqual(result['status'], 'needs_scope_clarification')
        self.assertEqual(self.persisted(), before)

    def test_existing_approver_refusal_keeps_clarification(self):
        self.grant()
        task = self.task()
        before = self.persisted()
        with patch('bridge.canvas._approvers', return_value=['Existing required reviewer']), \
                patch('bridge.canvas._not_behind_rule', return_value=['Existing required reviewer']), \
                patch.object(self.store, 'notify', side_effect=AssertionError('Probe must not notify')):
            result = self.add(task)
        self.assertEqual(result['status'], 'needs_scope_clarification')
        self.assertEqual(self.persisted(), before)

    def test_nonqualifying_probe_rolls_back_inside_an_existing_writer(self):
        self.grant()
        task = self.task()
        with self.graph.transaction() as db:
            db.execute("INSERT INTO settings(key,value) VALUES('outer-marker','retained')")
            before = self.persisted()
            result = self.add(task, {k: v for k, v in FACTS.items() if k != 'customer'})
            self.assertEqual(result['status'], 'needs_scope_clarification')
            self.assertEqual(self.persisted(), before)
        self.assertEqual(self.graph.get_setting('outer-marker'), 'retained')

    def test_successful_reuse_obeys_outer_rollback(self):
        self.grant()
        task = self.task()
        before = self.persisted()
        with self.assertRaisesRegex(RuntimeError, 'caller rollback'):
            with self.graph.transaction():
                result = self.add(task)
                self.assertTrue(result['authorized'], result)
                raise RuntimeError('caller rollback')
        self.assertEqual(self.persisted(), before)

    def test_unfinished_other_claim_is_not_awaited_inside_probe_writer(self):
        self.grant()
        task = self.task()
        self.graph.claim_node_ref(task, 'current-review')
        before = self.persisted()
        with patch('bridge.canvas._await_node_ref', side_effect=AssertionError('No lock-held wait')):
            result = self.add(task)
        self.assertEqual(result['status'], 'needs_scope_clarification')
        self.assertEqual(self.persisted(), before)

    def test_crashed_stale_claim_recovers_without_a_duplicate_insert(self):
        self.grant()
        task = self.task()
        self.graph.claim_node_ref(task, 'current-review')
        self.graph.db.execute("UPDATE node_claims SET created_at='2000-01-01T00:00:00+00:00' "
                              'WHERE run_id=?', (task,))
        with patch('bridge.canvas._await_node_ref', side_effect=AssertionError('No lock-held wait')):
            result = self.add(task)
        self.assertTrue(result['authorized'], result)
        claim = self.graph.db.execute('SELECT decision_id,created_at FROM node_claims WHERE run_id=?',
                                      (task,)).fetchone()
        self.assertEqual(claim['decision_id'], result['node_id'])
        self.assertGreater(claim['created_at'], '2000-01-01')
        self.assertEqual(self.graph.count_events('node_added', task_id=task), 1)

    def test_unqualified_stale_claim_is_restored_under_outer_writer(self):
        self.grant()
        task = self.task()
        self.graph.claim_node_ref(task, 'current-review')
        self.graph.db.execute("UPDATE node_claims SET created_at='2000-01-01T00:00:00+00:00' "
                              'WHERE run_id=?', (task,))
        with self.graph.transaction():
            before = self.persisted()
            result = self.add(task, {k: v for k, v in FACTS.items() if k != 'customer'})
            self.assertEqual(result['status'], 'needs_scope_clarification')
            self.assertEqual(self.persisted(), before)

    def test_existing_idempotent_node_preserves_its_background_state(self):
        self.grant()
        task = self.task()
        existing = self.add(task, {**FACTS, 'work_item': 'AUDIT-22'})
        self.graph.db.execute('UPDATE decisions SET model_pending=1 WHERE id=?', (existing['node_id'],))
        before = self.persisted()
        result = canvas._rule_before_contact(self.store, {'task_id': task, 'question': QUESTION,
            'context': 'Queue audit review', 'paths': 'queue/audit.py',
            'client_ref': 'current-review', 'facts': json.dumps(FACTS)})
        self.assertTrue(result['repeated'])
        self.assertTrue(result['model_pending'])
        self.assertEqual(self.persisted(), before)

    def test_qualified_local_reuse_never_calls_models_connectors_or_background(self):
        self.grant()
        task = self.task()
        with patch.dict('os.environ', {'BRIDGE_LIVE': '1'}), \
                patch('bridge.context_connectors.search', side_effect=AssertionError('No connector call')), \
                patch('bridge.ingest.live_probe', side_effect=AssertionError('No live lookup')), \
                patch('bridge.canvas._start_background', side_effect=AssertionError('No speculative worker')):
            result = self.add(task, cfg=Config(model_api='anthropic', deterministic=False))
        self.assertTrue(result['authorized'], result)
        self.assertFalse(result['model_pending'])
