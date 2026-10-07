"""Provider-free cache classification, using labelled direct human actors.

Task links are context, never supporting premises or signatures. These tests
exercise new reuse only; no authentication service or external delivery runs.
"""
import json
import os
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import canvas, context_memory as cm, github, mcp, proof
from bridge.authz import Actor
from bridge.config import Config
from bridge.store import Store


class CacheNamespaceApplicabilityTests(OfflineCase):
    repo = 'synthetic/cache-classification'
    question = 'Should synthetic preview requests count toward metered usage?'
    answer = 'Exclude synthetic preview requests from metered usage.'
    facts = {'org': 'Synthetic Cedar', 'customer': 'Synthetic Bay', 'domain': 'metering'}

    def setUp(self):
        super().setUp()
        self.cfg = Config(model_api='none')
        for target in ('bridge.llm.Client.complete', 'bridge.llm.Client.complete_json',
                       'bridge.github.GitHubAPI.get', 'bridge.mcp.index_repo',
                       'socket.socket.connect', 'socket.create_connection'):
            guard = patch(target, side_effect=AssertionError('No provider or network calls'))
            guard.start()
            self.addCleanup(guard.stop)
        env = patch.dict(os.environ, {'BRIDGE_MODEL_API': 'none', 'BRIDGE_DEEP': '0'})
        env.start()
        self.addCleanup(env.stop)
        self.store = Store(Path(self.temp.name) / 'cache.db')
        self.g = self.store.graph
        self.addCleanup(self.g.close)
        person = self.g.add_person('Synthetic Reviewer', email='reviewer@synthetic.invalid')
        self.owner = self.g.owner_id_for_person(person)
        self.actor = Actor.person(self.g.find_person(person), kind='synthetic_test')

    def task(self, namespace='synthetic-north', ref='SYN-1', role='work_item', facts=None):
        task = mcp.call_tool(self.store, 'bridge_start_task', {
            'repo': self.repo, 'title': 'Review synthetic metering', 'paths': 'usage/meter.py',
            'facts': json.dumps(self.facts if facts is None else facts)})['task_id']
        if namespace:
            self.anchor(task, namespace, ref, role)
        return task

    def anchor(self, task, namespace, ref, role='work_item'):
        source = mcp.call_tool(self.store, 'bridge_import_record', {
            'repo': self.repo, 'kind': 'jira', 'provider': 'jira', 'namespace': namespace,
            'external_id': 'synthetic-' + ref, 'ref': ref, 'title': 'Open synthetic investigation',
            'body': 'No policy answer or approval is recorded here.', 'status': 'Open'})['source']
        return self.link_source(task, source, role)

    def link_source(self, task, source, role='work_item'):
        return mcp.call_tool(self.store, 'bridge_link_work_item', {
            'repo': self.repo, 'task_id': task, 'record_id': source['record_id'],
            'source_version_id': source['source_version_id'], 'provider': source['provider'],
            'namespace': source['namespace'], 'object_kind': source['kind'],
            'external_id': source['external_id'], 'role': role})

    def native_pull(self, alias=True):
        # The native local import followed by GitHub enrichment intentionally
        # keeps one backing record with two real canonical identities.
        self.g.upsert_intent(self.repo, 'pr', '42', 'Open synthetic investigation',
            'No policy answer or approval is recorded here.', '', '', metadata={
                'provider': 'git', 'namespace': self.repo, 'object_kind': 'pr',
                'external_id': '42', 'display_ref': '42'})
        found = mcp.call_tool(self.store, 'bridge_lookup_record', {'repo': self.repo,
            'provider': 'git', 'namespace': self.repo, 'object_kind': 'pr', 'external_id': '42'})
        record_id = found['source']['record_id']
        if alias:
            self.alias_pull()
        return mcp.call_tool(self.store, 'bridge_get_record', {
            'repo': self.repo, 'record_id': record_id})['source']

    def alias_pull(self):
        github.apply_pull(self.g, self.repo, {'repo': self.repo, 'number': 42,
            'title': 'Open synthetic investigation', 'body': 'No policy answer or approval is recorded here.',
            'author': '', 'merged_by': '', 'merged_at': '', 'updated_at': '', 'merge_sha': '',
            'files': [], 'reviews': []})

    def imported_context(self, provider, namespace):
        return mcp.call_tool(self.store, 'bridge_import_record', {
            'repo': self.repo, 'kind': 'note', 'provider': provider, 'namespace': namespace,
            'external_id': 'synthetic-context-77', 'ref': '77', 'title': 'Open synthetic investigation',
            'body': 'No policy answer or approval is recorded here.', 'status': 'Open'})['source']

    def answer_node(self, task):
        node = self.node(task)
        self.store.answer(node['node_id'], {'answer': self.answer,
            'rationale': 'Direct synthetic human response, independent of the task link.',
            'expected_updated_at': node['updated_at']}, actor=self.actor)
        return node['node_id']

    def multi_namespace_task(self, namespaces):
        task = self.task(namespace='')
        for namespace in namespaces:
            self.store.add_record({'repo': self.repo, 'kind': 'jira', 'provider': 'jira',
                'namespace': namespace, 'external_id': 'synthetic-context-' + namespace,
                'ref': 'CTX-' + namespace, 'title': 'Open synthetic investigation',
                'body': 'No policy answer or approval is recorded here.', 'status': 'Open',
                'task_id': task, 'anchor_role': 'context'})
        return task

    def node(self, task):
        return canvas.add_node(self.store, self.cfg, {'task_id': task,
            'question': self.question, 'paths': 'usage/meter.py', 'category': 'billing',
            'owner_id': self.owner})

    def history(self, namespace='synthetic-north', role='work_item'):
        task = self.task(namespace, role=role)
        return task, self.answer_node(task)

    def assert_prediction(self, node, source):
        self.assertEqual((node['status'], node['kind']), ('predicted', 'prediction'), node['evidence'])
        self.assertEqual(node['answer'], self.answer)
        self.assertFalse(node['authorized'])
        self.assertEqual(node['sources'], [])
        self.assertEqual(self.store.get_decision(node['node_id'])['source_id'], source)
        self.assertIn('namespace_conflict', node['evidence'])
        self.assertIn('synthetic-north', node['evidence'])
        self.assertIn('synthetic-south', node['evidence'])
        self.assertIn('historical context', node['evidence'])
        self.assertEqual(node['answered_by'], '')

    def test_conflicting_task_namespace_is_history_not_matching_evidence(self):
        _, source = self.history()
        before = self.store.get_decision(source)
        history = cm.history(self.g.db, source)
        current = self.node(self.task('synthetic-south', 'SYN-2'))
        self.assert_prediction(current, source)
        after = self.store.get_decision(source)
        for key in ('answer', 'facts', 'signatures', 'updated_at', 'authorized', 'sources'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(cm.history(self.g.db, source), history)
        self.assertFalse(self.store.delivery.enabled)
        self.assertEqual(self.g.db.execute('SELECT COUNT(*) AS n FROM notifications').fetchone()['n'], 0)

    def test_context_role_does_not_hide_conflicting_namespace(self):
        _, source = self.history(role='context')
        self.assert_prediction(self.node(self.task('synthetic-south', 'SYN-2', role='context')), source)

    def test_shared_namespace_does_not_hide_other_changed_context(self):
        old_task = self.multi_namespace_task(('shared', 'historical-only'))
        old = self.answer_node(old_task)
        history = cm.history(self.g.db, old)
        current = self.node(self.multi_namespace_task(('shared', 'current-only')))
        self.assertEqual((current['status'], current['kind']), ('predicted', 'prediction'), current['evidence'])
        for namespace in ('shared', 'historical-only', 'current-only'):
            self.assertIn('jira:' + namespace, current['evidence'])
        self.assertIn('namespace_conflict', current['evidence'])
        self.assertEqual(current['answer'], self.answer)
        self.assertEqual(self.store.get_decision(current['node_id'])['source_id'], old)
        self.assertEqual(current['sources'], [])
        self.assertFalse(current['authorized'])
        self.assertEqual(cm.history(self.g.db, old), history)

    def test_equal_namespace_sets_remain_matching_evidence(self):
        self.answer_node(self.multi_namespace_task(('shared', 'second')))
        current = self.node(self.multi_namespace_task(('second', 'shared')))
        self.assertEqual((current['status'], current['kind']), ('resolved', 'evidence'), current['evidence'])
        self.assertNotIn('namespace_conflict', current['evidence'])
        self.assertFalse(current['authorized'])

    def test_source_alias_uses_anchored_canonical_identity(self):
        old_task = self.task(namespace='')
        source = self.native_pull()
        backing = self.g.db.execute('SELECT provider,namespace FROM source_records WHERE id=?',
                                   (source['record_id'],)).fetchone()
        self.assertEqual((backing['provider'], backing['namespace']), ('git', self.repo))
        self.assertEqual((source['provider'], source['namespace']), ('github', 'github.com'))
        self.link_source(old_task, source)
        old = self.answer_node(old_task)
        current_task = self.task(namespace='')
        self.link_source(current_task, self.imported_context('github', 'github.synthetic.invalid'))
        node = self.node(current_task)
        self.assertEqual((node['status'], node['kind']), ('predicted', 'prediction'), node['evidence'])
        self.assertIn('github:github.com', node['evidence'])
        self.assertIn('github:github.synthetic.invalid', node['evidence'])
        self.assertFalse(node['authorized'])
        self.assertEqual(node['sources'], [])
        self.assertEqual(self.store.get_decision(node['node_id'])['source_id'], old)

    def test_current_alias_uses_anchored_canonical_identity(self):
        old_task = self.task(namespace='')
        self.link_source(old_task, self.imported_context('github', 'github.synthetic.invalid'))
        self.answer_node(old_task)
        current_task = self.task(namespace='')
        self.link_source(current_task, self.native_pull())
        node = self.node(current_task)
        self.assertEqual((node['status'], node['kind']), ('predicted', 'prediction'), node['evidence'])
        self.assertIn('github:github.com', node['evidence'])
        self.assertIn('github:github.synthetic.invalid', node['evidence'])
        self.assertFalse(node['authorized'])

    def test_alias_does_not_replace_an_older_anchors_identity(self):
        old_task = self.task(namespace='')
        source = self.native_pull(alias=False)
        self.link_source(old_task, source)
        self.answer_node(old_task)
        self.alias_pull()
        current_task = self.task(namespace='')
        self.link_source(current_task, self.imported_context('github', 'github.synthetic.invalid'))
        node = self.node(current_task)
        self.assertEqual((node['status'], node['kind']), ('resolved', 'evidence'), node['evidence'])
        self.assertNotIn('namespace_conflict', node['evidence'])
        anchor = cm.anchors(self.g.db, old_task)[0]
        self.assertEqual((anchor['provider'], anchor['namespace']), ('git', self.repo))
        self.assertEqual(anchor['source_version_id'], source['source_version_id'])
        self.assertFalse(node['authorized'])

    def test_different_item_in_same_namespace_remains_evidence(self):
        self.history()
        node = self.node(self.task(ref='SYN-2'))
        self.assertEqual((node['status'], node['kind']), ('resolved', 'evidence'))
        self.assertFalse(node['authorized'])

    def test_unlinked_legacy_history_does_not_invent_a_namespace(self):
        self.history(namespace='')
        node = self.node(self.task('synthetic-south'))
        self.assertEqual((node['status'], node['kind']), ('resolved', 'evidence'))
        self.assertNotIn('namespace_conflict', node['evidence'])

    def test_link_added_after_answer_does_not_rewrite_its_context(self):
        task, _ = self.history(namespace='')
        self.anchor(task, 'synthetic-north', 'SYN-1')
        node = self.node(self.task('synthetic-south', 'SYN-2'))
        self.assertEqual((node['status'], node['kind']), ('resolved', 'evidence'))
        self.assertNotIn('namespace_conflict', node['evidence'])

    def test_explicit_broad_independent_rule_keeps_declared_scope(self):
        _, source = self.history()
        self.store.update_settings({'auto_rules': True})
        current = self.store.get_decision(source)
        self.store.make_rule(source, {'scope': 'any', 'conditions': 'domain=metering',
            'expected_updated_at': current['updated_at']}, actor=self.actor)
        before = self.store.get_decision(source)
        node = self.node(self.task('synthetic-south', 'SYN-2'))
        self.assertEqual((node['status'], node['kind'], node['signoff']), ('resolved', 'evidence', 'rule'))
        self.assertTrue(node['authorized'], node['evidence'])
        self.assertEqual(self.store.get_decision(source)['signatures'], before['signatures'])
        self.assertEqual(node['sources'], [])

    def test_broad_rule_with_automatic_rules_off_is_still_matching_evidence(self):
        _, source = self.history()
        current = self.store.get_decision(source)
        self.store.make_rule(source, {'scope': 'any', 'conditions': 'domain=metering',
            'expected_updated_at': current['updated_at']}, actor=self.actor)
        node = self.node(self.task('synthetic-south', 'SYN-2'))
        self.assertEqual((node['status'], node['kind']), ('resolved', 'evidence'))
        self.assertFalse(node['authorized'])
        self.assertIn('automatic rule authorization is off', node['evidence'])

    def test_unmet_rule_keeps_namespace_prediction_explanation(self):
        _, source = self.history()
        self.store.update_settings({'auto_rules': True})
        current = self.store.get_decision(source)
        self.store.make_rule(source, {'scope': 'any', 'conditions': 'environment=production',
            'expected_updated_at': current['updated_at']}, actor=self.actor)
        node = self.node(self.task('synthetic-south', 'SYN-2'))
        self.assert_prediction(node, source)
        self.assertNotIn('the answer is evidence to sign', node['evidence'])

    def test_task_context_does_not_expand_declared_rule_restrictions(self):
        _, source = self.history()
        self.store.update_settings({'auto_rules': True})
        current = self.store.get_decision(source)
        self.store.make_rule(source, {'scope': 'same', 'conditions': 'domain=metering',
            'expected_updated_at': current['updated_at']}, actor=self.actor)
        node = self.node(self.task('synthetic-south', 'SYN-2'))
        self.assertEqual(node['signoff'], 'rule', node['evidence'])
        self.assertTrue(node['authorized'])

    def test_completed_proof_and_fresh_signoff_survive_classification(self):
        task, source = self.history()
        self.store.update_run(task, {'status': 'completed'})
        bundle = proof.create(self.store, task, 'diff --git a/a b/a\n+synthetic\n')
        frozen = cm.encoded(bundle)
        current = self.node(self.task('synthetic-south', 'SYN-2'))
        self.assert_prediction(current, source)
        # A historical answer does not select its signer for this scope.
        # The synthetic local operator independently assigns this request
        # before its selected reviewer deliberately approves the answer.
        assigned = self.store.assign(current['node_id'], {'owner_id': self.owner},
            actor=Actor(name='Synthetic local operator', kind='operator'))
        self.assertFalse(assigned['authorized'])
        signed = canvas.sign_off(self.store, current['node_id'], {
            'expected_updated_at': assigned['updated_at']}, actor=self.actor)
        self.assertTrue(signed['authorized'])
        self.assertEqual(signed['sources'], [])
        exported = proof.export(self.store, {'task_id': task})
        self.assertEqual(cm.encoded(exported['bundle']), frozen)
        self.assertTrue(exported['integrity']['valid'])
        self.assertTrue(self.store.get_decision(source)['authorized'])
