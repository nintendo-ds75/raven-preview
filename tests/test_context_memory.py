"""Offline contracts for source history, active reliance, and authority."""
import json
import threading
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import canvas, context_memory as cm, mcp, proof
from bridge.config import Config
from bridge.store import Invalid, Store

REPO = 'synthetic/context'


class ContextMemoryTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'context.db')
        self.g = self.store.graph
        self.addCleanup(self.g.close)
        self.owner = self.store.add_owner({'name': 'Ada Example', 'team': 'Example', 'patterns': 'policy/*'})
        self.task = self.store.add_run({'title': 'Retention migration', 'repo': REPO})['id']

    def source(self, ref='POL-1', **changes):
        return self.store.add_record({'repo': REPO, 'kind': 'jira', 'ref': ref, 'title': 'Retention policy',
            'body': 'Decided: retain records for thirty days.', 'author': 'Source Writer', 'status': 'Done',
            'created_at': '2026-01-01T00:00:00Z', 'url': f'https://example.invalid/browse/{ref}',
            'paths': ['policy/retention.py'], **changes})

    def row(self, source):
        return dict(self.g.db.execute('SELECT i.*,s.id AS record_id,s.head_id AS source_version_id FROM intents i '
                                      'JOIN source_records s ON s.intent_id=i.id WHERE s.id=?', (source['source']['record_id'],)).fetchone())

    def node(self, sources=()):
        did = self.g.add_decision(self.task, 'How many days should records remain?', 'policy', 'pending',
                                  repo=REPO, owner='Ada Example', path='policy/retention.py')
        self.g.publish_evidence(did, [self.row(s) for s in sources], status='resolved', source='record',
                                answer='Retain records for thirty days.', kind='evidence', signoff='required')
        return did

    def sign(self, did):
        return canvas.sign_off(self.store, did, {'by': 'Ada Example', 'expected_updated_at': self.store.get_decision(did)['updated_at']})

    def test_import_revisions_noops_reverts_and_exact_metadata(self):
        a = self.source()
        same = self.source()
        self.assertFalse(same['changed'])
        self.assertEqual(a['source']['source_version_id'], same['source']['source_version_id'])
        b = self.source(body='Retain for seven days.', author='Editor', paths=['policy/new.py'])
        c = self.source()
        record = self.store.get_record(a['source']['record_id'], REPO)
        self.assertEqual([v['sequence'] for v in record['versions']], [1, 2, 3])
        self.assertEqual(record['versions'][0]['fingerprint'], record['versions'][2]['fingerprint'])
        self.assertNotEqual(a['source']['source_version_id'], c['source']['source_version_id'])
        self.assertEqual(record['versions'][1]['snapshot']['author'], 'Editor')
        self.assertEqual(record['versions'][1]['snapshot']['paths'], ['policy/new.py'])
        self.assertEqual(record['versions'][0]['snapshot']['author'], 'Source Writer')

    def test_second_support_update_invalidates_signoff_and_descendants(self):
        a, b = self.source(), self.source('POL-2')
        did = self.node([a, b])
        signed = self.sign(did)
        self.assertTrue(signed['authorized'])
        child = self.node()
        self.g.update_decision(child, source_id=did, source='memory', source_revision=self.store.get_decision(did)['updated_at'])
        self.sign(child)
        self.source('POL-2', body='Changed second premise.')
        self.assertFalse(self.store.get_decision(did)['authorized'])
        self.assertTrue(self.store.get_decision(child)['needs_review'])
        self.assertEqual(len(self.store.get_decision(did)['sources']), 2)
        self.assertTrue(any(e['stale'] for e in self.store.get_decision(did)['sources']))
        history = [json.loads(v['snapshot']) for v in cm.history(self.g.db, did)]
        self.assertTrue(any(h['decision']['signed_by'] == 'Ada Example' for h in history))
        with self.assertRaises(Invalid):
            self.sign(did)
        with self.assertRaises(Invalid):
            self.store.update_run(self.task, {'status': 'completed'})

    def test_context_and_work_item_updates_are_not_blocking_premises(self):
        a = self.source(task_id=self.task)
        did = self.node()
        with self.g.transaction():
            cm.attach(self.g.db, did, [cm.pin(self.row(a), 'context')])
        self.sign(did)
        self.source(body='Context changed.')
        self.assertTrue(self.store.get_decision(did)['authorized'])
        self.assertFalse(self.store.get_decision(did)['needs_review'])
        tree = canvas.get_tree(self.store, self.task)
        self.assertEqual(tree['source_anchors'][0]['record_id'], a['source']['record_id'])
        self.assertFalse(tree['source_anchors'][0]['current'])

    def test_independent_human_action_retires_reliance_even_when_text_identical(self):
        a = self.source()
        did = self.node([a])
        self.sign(did)
        row = self.store.get_decision(did)
        self.store.answer(did, {'answer': row['answer'], 'rationale': 'Independent owner decision',
            'signed_by': 'Ada Example', 'expected_updated_at': row['updated_at'], 'evidence_mode': 'independent'})
        self.assertEqual(self.store.get_decision(did)['sources'], [])
        self.source(body='Source changed after independent replacement.')
        self.assertTrue(self.store.get_decision(did)['authorized'])
        self.assertTrue(any(json.loads(v['snapshot'])['sources'] for v in cm.history(self.g.db, did)))

    def test_human_answer_retains_dependencies_by_default(self):
        a = self.source()
        did = self.node([a])
        self.store.answer(did, {'answer': 'Keep thirty days because the source applies.', 'signed_by': 'Ada Example'})
        self.source(body='Seven days.')
        self.assertTrue(self.store.get_decision(did)['needs_review'])

    def test_reapproval_requires_explicit_current_pins_and_fresh_revision(self):
        a = self.source()
        did = self.node([a])
        self.sign(did)
        stale = self.store.get_decision(did)['updated_at']
        b = self.source(body='Keep seven days.')
        with self.assertRaises(Invalid):
            self.store.answer(did, {'answer': 'Seven days.', 'expected_updated_at': stale})
        row = self.store.get_decision(did)
        self.store.answer(did, {'answer': 'Seven days.', 'signed_by': 'Ada Example',
            'expected_updated_at': row['updated_at'], 'source_evidence': [cm.pin(self.row(b))]})
        self.assertTrue(self.store.get_decision(did)['authorized'])
        self.assertEqual(self.store.get_decision(did)['sources'][0]['source_version_id'], b['source']['source_version_id'])

    def test_atomic_publication_rejects_second_source_changed_during_composition(self):
        a, b = self.source(), self.source('POL-2')
        rows = [self.row(a), self.row(b)]
        did = self.node()
        before = self.store.get_decision(did)
        other = Store(self.store.path)
        self.addCleanup(other.graph.close)
        ready, changed = threading.Event(), threading.Event()
        errors = []
        def writer():
            try:
                ready.wait(5)
                other.add_record({'repo': REPO, 'kind': 'jira', 'ref': 'POL-2', 'title': 'Changed second source'})
            except Exception as error:
                errors.append(error)
            finally:
                changed.set()
        thread = threading.Thread(target=writer)
        thread.start()
        ready.set()
        self.assertTrue(changed.wait(5))
        thread.join()
        self.assertEqual(errors, [])
        with self.assertRaisesRegex(Invalid, 'Source evidence changed'):
            self.g.publish_evidence(did, rows, answer='Racing answer', status='resolved')
        self.assertEqual(self.store.get_decision(did)['answer'], before['answer'])
        self.assertEqual(self.store.get_decision(did)['sources'], [])

    def test_namespaced_same_refs_and_repository_scopes_do_not_merge(self):
        legacy = self.source()
        a = self.source(provider='jira', namespace='site-a')
        b = self.source(provider='jira', namespace='site-b')
        c = self.source(provider='jira', namespace='site-a', repo='other/context')
        self.assertEqual(len({s['source']['record_id'] for s in (legacy, a, b, c)}), 4)
        did = self.node([a])
        self.sign(did)
        self.source(provider='jira', namespace='site-b', body='Unrelated site changes.')
        self.assertTrue(self.store.get_decision(did)['authorized'])
        with self.assertRaises(Invalid):
            self.g.publish_evidence(did, [self.row(c)], answer='Cross-repo')
        self.assertEqual({r['record_id'] for r in self.g.intents_by_ref(['POL-1'], REPO)},
                         {legacy['source']['record_id']})
        with self.g.transaction():
            cm.add_anchor(self.g.db, self.task, a['source']['record_id'], a['source']['source_version_id'])
        with self.g.source_scope(self.task):
            self.assertEqual({r['record_id'] for r in self.g.intents_by_ref(['POL-1'], REPO)},
                             {legacy['source']['record_id'], a['source']['record_id']})
        with self.assertRaises(Invalid):
            self.store.get_record(c['source']['record_id'], REPO)

    def test_out_of_order_rejected_atomically_and_source_time_not_ingestion_time(self):
        a = self.source(source_sequence='2', updated_at='2026-01-02T00:00:00Z')
        with self.assertRaises(Invalid):
            self.source(source_sequence='1', updated_at='2026-01-01T00:00:00Z', body='Older event')
        self.assertEqual(self.row(a)['body'], 'Decided: retain records for thirty days.')
        b = self.source('POL-2', created_at='')
        self.assertEqual(b['source']['source_created_at'], '')
        self.assertTrue(b['source']['observed_at'])

    def test_proof_is_immutable_but_live_export_reports_stale_sources(self):
        a = self.source()
        did = self.node([a])
        self.sign(did)
        self.store.update_run(self.task, {'status': 'completed'})
        bundle = proof.create(self.store, self.task, 'diff --git a/a b/a\n+new\n', checks='synthetic test')
        frozen = json.dumps(bundle, sort_keys=True)
        self.source(body='Source changed after completion.')
        exported = proof.export(self.store, {'task_id': self.task})
        self.assertEqual(json.dumps(exported['bundle'], sort_keys=True), frozen)
        self.assertTrue(exported['integrity']['valid'])
        self.assertTrue(exported['stale'])
        self.assertTrue(exported['current_sources'][did][0]['stale'])

    def test_restart_and_legacy_observation_never_fabricate_old_answer_links(self):
        with self.g.transaction():
            self.g.db.execute("INSERT INTO intents(id,repo,kind,ref,title,body,author,created_at) VALUES('old',?,'jira','OLD-1','Legacy','Old text','Old Author','2020-01-01')", (REPO,))
        did = self.node()
        self.g.update_decision(did, evidence='jira OLD-1 legacy citation', source='record')
        reopened = Store(self.store.path)
        self.addCleanup(reopened.graph.close)
        self.assertEqual(reopened.get_decision(did)['sources'], [])
        self.assertEqual(reopened.get_decision(did)['source_provenance'], 'unknown')
        rec = reopened.graph.db.execute("SELECT * FROM source_records WHERE intent_id='old'").fetchone()
        versions = reopened.get_record(rec['id'], REPO)['versions']
        self.assertEqual(versions[0]['provenance'], 'legacy-observation')
        again = Store(self.store.path)
        self.addCleanup(again.graph.close)
        self.assertEqual(len(again.get_record(rec['id'], REPO)['versions']), 1)

    def test_transient_slack_search_rejected_and_closed_author_never_approves(self):
        with self.assertRaisesRegex(Invalid, 'Transient'):
            self.source(kind='slack', retrieval_mode='slack_realtime_search')
        a = self.source(kind='slack', provider='slack', namespace='TEXAMPLE', external_id='CEXAMPLE:123.456',
                        author='Ada Example', body='I approve everything forever.', status='Approved')
        did = self.node([a])
        self.assertFalse(self.store.get_decision(did)['authorized'])
        self.assertFalse(self.store.get_decision(did)['reusable'])

    def test_mcp_returns_canonical_source_and_exact_version_history(self):
        result = mcp.call_tool(self.store, 'bridge_import_record', {'repo': REPO, 'kind': 'jira', 'ref': 'MCP-1',
            'title': 'MCP source', 'provider': 'jira', 'namespace': 'site-a', 'task_id': self.task,
            'status': 'Done', 'resolved': True, 'paths': []})
        read = mcp.call_tool(self.store, 'bridge_get_record', {'repo': REPO, 'record_id': result['source']['record_id']})
        self.assertEqual(read['source']['source_version_id'], result['source']['source_version_id'])
        self.assertEqual(len(read['versions']), 1)

    def test_noop_event_advances_ordering_watermark_without_revision_churn(self):
        a = self.source(source_sequence='2', updated_at='2026-01-02T00:00:00Z')
        b = self.source(source_sequence='4', updated_at='2026-01-04T00:00:00Z')
        self.assertEqual(a['source']['source_version_id'], b['source']['source_version_id'])
        with self.assertRaises(Invalid):
            self.source(source_sequence='3', updated_at='2026-01-03T00:00:00Z', body='Older changed payload')
        self.assertEqual(len(self.store.get_record(a['source']['record_id'], REPO)['versions']), 1)

    def test_source_update_rollback_keeps_head_paths_and_signoff_together(self):
        a = self.source()
        did = self.node([a])
        self.sign(did)
        with patch.object(cm, 'invalidate', side_effect=RuntimeError('forced rollback')):
            with self.assertRaises(RuntimeError):
                self.source(body='New text', paths=['new/path.py'])
        current = self.store.get_record(a['source']['record_id'], REPO)
        self.assertEqual(current['source']['source_version_id'], a['source']['source_version_id'])
        self.assertEqual(len(current['versions']), 1)
        self.assertEqual(current['versions'][0]['snapshot']['paths'], ['policy/retention.py'])
        self.assertTrue(self.store.get_decision(did)['authorized'])

    def test_duplicate_concurrent_imports_create_one_logical_version(self):
        other = Store(self.store.path)
        self.addCleanup(other.graph.close)
        errors = []
        gate = threading.Barrier(2)
        def worker(store):
            try:
                gate.wait(5)
                store.add_record({'repo': REPO, 'kind': 'jira', 'ref': 'DUPE-1', 'title': 'Same event', 'source_sequence': '1'})
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=worker, args=(s,)) for s in (self.store, other)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        row = self.g.intents_by_ref(['DUPE-1'], REPO)[0]
        self.assertEqual(len(self.store.get_record(row['record_id'], REPO)['versions']), 1)

    def test_signoff_racing_update_is_serialized_and_live_answer_ends_stale(self):
        a = self.source()
        did = self.node([a])
        entered, release = threading.Event(), threading.Event()
        errors = []
        original = cm.check_current
        def check(db, current, seen=None, **kwargs):
            result = original(db, current, seen, **kwargs)
            if threading.current_thread().name == 'signer':
                entered.set()
                self.assertTrue(release.wait(5))
            return result
        def signer():
            try: self.sign(did)
            except Exception as error: errors.append(error)
        def updater():
            try: self.source(body='Updated while sign-off in flight.')
            except Exception as error: errors.append(error)
        with patch.object(cm, 'check_current', side_effect=check):
            first = threading.Thread(target=signer, name='signer')
            first.start()
            self.assertTrue(entered.wait(5))
            second = threading.Thread(target=updater)
            second.start()
            release.set()
            first.join(10)
            second.join(10)
        self.assertEqual(errors, [])
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertFalse(self.store.get_decision(did)['authorized'])
        self.assertTrue(self.store.get_decision(did)['needs_review'])
        self.assertTrue(any(json.loads(h['snapshot'])['decision']['signed_by'] == 'Ada Example' for h in cm.history(self.g.db, did)))

    def test_joint_composer_pins_only_declared_used_input_objects(self):
        from bridge import ladder, llm
        a, b, c = self.source(), self.source('POL-2'), self.source('POL-3')
        rows = [self.row(s) for s in (a, b, c)]
        semantic = patch.object(Config, 'semantic_retrieval', property(lambda _: True))
        semantic.start()
        self.addCleanup(semantic.stop)
        cfg = Config()
        with patch.object(llm.Client, 'complete', return_value='Keep thirty days.\nUSED_SOURCES: r1,r2\nCOVERAGE: FULL'), \
             patch.object(ladder, '_supported', side_effect=lambda cfg, question, source, text, *args: text):
            result = ladder._compose_joint(cfg, 'What retention applies?', rows)
        self.assertIsNotNone(result)
        self.assertEqual([r['record_id'] for r in result[3]], [rows[0]['record_id'], rows[1]['record_id']])
        did = self.node()
        self.g.publish_evidence(did, result[3], answer=result[0], status='resolved')
        self.sign(did)
        self.source('POL-3', body='Rejected neighbor changed.')
        self.assertTrue(self.store.get_decision(did)['authorized'])
        self.source('POL-2', body='Used second source changed.')
        self.assertFalse(self.store.get_decision(did)['authorized'])

    def test_unstructured_joint_cache_output_cannot_acquire_fabricated_links(self):
        from bridge import ladder, llm
        rows = [self.row(self.source()), self.row(self.source('POL-2'))]
        semantic = patch.object(Config, 'semantic_retrieval', property(lambda _: True))
        semantic.start()
        self.addCleanup(semantic.stop)
        with patch.object(llm.Client, 'complete', return_value='Keep thirty days. COVERAGE: FULL'):
            self.assertIsNone(ladder._compose_joint(Config(), 'What retention applies?', rows))

    def test_background_update_does_not_make_completed_proof_inapplicable(self):
        a = self.source()
        did = self.node()
        with self.g.transaction():
            cm.attach(self.g.db, did, [cm.pin(self.row(a), 'context')])
        self.sign(did)
        self.store.update_run(self.task, {'status': 'completed'})
        proof.create(self.store, self.task, 'diff --git a/a b/a\n+new\n')
        self.source(body='Updated background.')
        exported = proof.export(self.store, {'task_id': self.task})
        self.assertFalse(exported['stale'])
        self.assertTrue(exported['current_sources'][did][0]['stale'])

    def test_inaccessible_source_withdraws_reliance_and_source_read(self):
        a = self.source()
        did = self.node([a])
        self.sign(did)
        self.source(availability='inaccessible')
        self.assertFalse(self.store.get_decision(did)['authorized'])
        self.assertEqual(self.g.intents_by_ref(['POL-1'], REPO), [])
        with self.assertRaises(Invalid):
            self.store.get_record(a['source']['record_id'], REPO)

    def test_independent_replacement_retires_decision_derivation_links(self):
        a = self.source()
        upstream = self.node([a])
        self.sign(upstream)
        child = self.node()
        self.g.add_link(child, upstream, 'derived', 'Used upstream answer')
        self.g.update_decision(child, source_id=upstream, source='memory')
        self.store.answer(child, {'answer': 'Independent replacement', 'signed_by': 'Ada Example', 'evidence_mode': 'independent'})
        self.source(body='Changed the original premise.')
        self.assertTrue(self.store.get_decision(child)['authorized'])
        self.assertTrue(any(json.loads(h['snapshot'])['derivations'] for h in cm.history(self.g.db, child)))

    def test_public_mcp_namespace_anchor_disambiguates_the_actual_ladder(self):
        task = mcp.call_tool(self.store, 'bridge_start_task', {'repo': REPO, 'title': 'Apply record retention policy',
                             'goal': 'Apply retention policy from POL-44', 'paths': 'policy/retention.py', 'client_key': 'selected-anchor'})
        base = {'repo': REPO, 'kind': 'jira', 'ref': 'POL-44', 'title': 'How long should records be retained?',
                'status': 'Done', 'provider': 'jira', 'paths': ['policy/retention.py']}
        a = mcp.call_tool(self.store, 'bridge_import_record', {**base, 'namespace': 'site-a', 'external_id': 'opaque-101', 'body': 'Decided: retain records for 30 days.', 'task_id': task['task_id']})
        b = mcp.call_tool(self.store, 'bridge_import_record', {**base, 'namespace': 'site-b', 'external_id': 'opaque-202', 'body': 'Decided: retain records for 99 years.'})
        node = mcp.call_tool(self.store, 'bridge_add_node', {'task_id': task['task_id'],
            'question': 'How long should records be retained under POL-44?', 'paths': 'policy/retention.py', 'category': 'policy'})
        self.assertNotIn('99 years', json.dumps(node))
        self.assertNotIn(b['source']['record_id'], json.dumps(node))
        tree = mcp.call_tool(self.store, 'bridge_get_tree', {'task_id': task['task_id']})
        self.assertEqual(tree['source_anchors'][0]['record_id'], a['source']['record_id'])
        if node['sources']:
            self.assertEqual({s['record_id'] for s in node['sources']}, {a['source']['record_id']})
        else:
            self.assertIn('POL-44', node['evidence'])

    def test_public_mcp_slack_message_ids_isolate_workspaces(self):
        base = {'repo': REPO, 'kind': 'slack', 'ref': 'C1:1700000000.000001', 'external_id': 'C1:1700000000.000001',
                'title': 'Selected conversation', 'body': 'Retention context from the selected message.', 'provider': 'slack'}
        a = mcp.call_tool(self.store, 'bridge_import_record', {**base, 'namespace': 'TA', 'task_id': self.task, 'anchor_role': 'context'})
        b = mcp.call_tool(self.store, 'bridge_import_record', {**base, 'namespace': 'TB'})
        self.assertNotEqual(a['source']['record_id'], b['source']['record_id'])
        self.assertEqual(mcp.call_tool(self.store, 'bridge_get_record', {'repo': REPO, 'record_id': a['source']['record_id']})['source']['namespace'], 'TA')
        self.assertEqual(mcp.call_tool(self.store, 'bridge_get_tree', {'task_id': self.task})['source_anchors'][0]['role'], 'context')

    def test_legacy_citation_reuse_surfaces_unknown_without_revoking_human_authority(self):
        did = self.node()
        self.g.update_decision(did, evidence='jira OLD-1, legacy citation only', source='record')
        before = self.store.get_decision(did)
        self.assertEqual(before['source_provenance'], 'unknown')
        self.assertIn('does not reconstruct', before['source_notice'])
        self.sign(did)
        self.store.make_rule(did, {'by': 'Ada Example'})
        reopened = Store(self.store.path)
        self.addCleanup(reopened.graph.close)
        self.assertTrue(reopened.get_decision(did)['authorized'])
        self.assertTrue(reopened.get_decision(did)['reusable'])
        self.assertEqual(reopened.get_decision(did)['sources'], [])
        self.assertEqual(reopened.get_decision(did)['source_provenance'], 'unknown')
        task = self.store.add_run({'title': 'New reuse request', 'repo': REPO})['id']
        reused = canvas.add_node(self.store, Config(model_api='none'), {'task_id': task,
            'question': 'How many days should records remain?', 'paths': 'policy/retention.py'})
        self.assertEqual(reused['sources'], [])
        self.assertEqual(reused['source_provenance'], 'unknown')
        self.assertIn('Automatic reuse requires deliberate review', reused['source_notice'])
        self.assertFalse(reused['authorized'])  # default requires this new question's sign-off

    def test_source_A_B_A_revert_never_revives_the_signature_on_A(self):
        a = self.source()
        did = self.node([a])
        self.sign(did)
        original = self.store.get_decision(did)['updated_at']
        self.source(body='B changed the premise.')
        reverted = self.source()
        self.assertNotEqual(a['source']['source_version_id'], reverted['source']['source_version_id'])
        self.assertEqual(reverted['source']['sequence'], 3)
        current = self.store.get_decision(did)
        self.assertTrue(current['needs_review'])
        self.assertFalse(current['authorized'])
        with self.assertRaises(Invalid):
            canvas.sign_off(self.store, did, {'by': 'Ada Example', 'expected_updated_at': original})
        with self.assertRaises(Invalid):
            self.sign(did)

    def test_unchanged_snapshot_read_does_not_churn_signed_version(self):
        a = self.source()
        did = self.node([a])
        self.sign(did)
        before = self.store.get_decision(did)
        count = len(before['context_history'])
        with self.g.transaction():
            cm.snapshot_decision(self.g.db, did, reason='used-as-source')
        after = self.store.get_decision(did)
        self.assertEqual(after['sources'], before['sources'])
        self.assertEqual(len(after['context_history']), count)

    def test_joint_second_human_source_correction_invalidates_its_derived_answer(self):
        from bridge.ladder import _as_record
        first, second = self.node(), self.node()
        for did, answer in ((first, 'First human premise'), (second, 'Second human premise')):
            self.store.answer(did, {'answer': answer, 'signed_by': 'Ada Example'})
        joint = self.node()
        self.g.publish_evidence(joint, [_as_record(self.g.get_decision(did)) for did in (first, second)],
                                answer='Composed from both human premises', status='resolved', source='memory')
        self.sign(joint)
        self.store.answer(second, {'answer': 'Corrected second premise', 'signed_by': 'Ada Example'})
        self.assertFalse(self.store.get_decision(joint)['authorized'])
        self.assertTrue(self.store.get_decision(joint)['needs_review'])
        links = self.g.links_for([joint])[joint]
        self.assertEqual(len([link for link in links if link.get('source_version_id')]), 2)

    def test_malformed_joint_source_labels_are_rejected(self):
        from bridge import ladder, llm
        semantic = patch.object(Config, 'semantic_retrieval', property(lambda _: True))
        semantic.start()
        self.addCleanup(semantic.stop)
        rows = [self.row(self.source()), self.row(self.source('POL-2'))]
        with patch.object(llm.Client, 'complete', return_value='Keep thirty days.\nUSED_SOURCES: r1,2\nCOVERAGE: FULL'):
            self.assertIsNone(ladder._compose_joint(Config(), 'What retention applies?', rows))

    def test_repinning_never_attaches_old_signatures_to_new_source_versions_in_history(self):
        a = self.source()
        did = self.node([a])
        self.sign(did)
        signed_revision = self.store.get_decision(did)['signed_revision']
        b = self.source(body='Retain for seven days.')
        self.store.answer(did, {'answer': 'Retain for seven days.', 'signed_by': 'Ada Example',
                               'source_evidence': [cm.pin(self.row(b))]})
        for item in self.store.get_decision(did)['context_history']:
            snapshot = json.loads(item['snapshot'])
            if snapshot['decision']['signed_revision'] == signed_revision:
                self.assertEqual([s['source_version_id'] for s in snapshot['sources']], [a['source']['source_version_id']])

    def test_parent_only_descendant_revalidates_after_material_source_change(self):
        source = self.source()
        parent = self.node([source])
        self.sign(parent)
        child = self.node()
        self.g.update_decision(child, parent_id=parent)
        self.sign(child)
        self.source(body='The parent premise changed.')
        self.assertTrue(self.store.get_decision(child)['needs_review'])
        self.assertFalse(self.store.get_decision(child)['authorized'])
        with self.assertRaises(Invalid):
            self.sign(child)

    def test_independent_human_replacement_retires_inherited_parent_reliance(self):
        source = self.source()
        parent = self.node([source])
        self.sign(parent)
        child = self.node()
        self.g.update_decision(child, parent_id=parent)
        self.store.answer(child, {'answer': 'An independent owner decision.', 'signed_by': 'Ada Example',
                                 'evidence_mode': 'independent'})
        self.source(body='The former parent premise changed.')
        self.assertTrue(self.store.get_decision(child)['authorized'])
        self.assertEqual(self.store.get_decision(child)['parent_id'], parent)

    def test_selected_namespace_filters_direct_and_transitive_signed_memory(self):
        from bridge.ladder import _as_record
        source = self.source(provider='jira', namespace='site-b', body='Retain records for 99 years.')
        prior = self.node([source])
        self.store.answer(prior, {'answer': 'Retain records for 99 years.', 'signed_by': 'Ada Example'})
        derived = self.node()
        self.g.publish_evidence(derived, [_as_record(self.g.get_decision(prior))], answer='Retain records for 99 years.',
                                source='memory', status='resolved')
        self.sign(derived)
        later = self.store.add_run({'title': 'Site A task', 'repo': REPO})['id']
        self.source(provider='jira', namespace='site-a', task_id=later)
        with self.g.source_scope(later):
            self.assertNotIn(prior, {r['id'] for r in self.g._memory_rows(('approved', 'resolved'), REPO)})
            self.assertNotIn(derived, {r['id'] for r in self.g._memory_rows(('approved', 'resolved'), REPO)})
            self.assertNotIn(prior, {r.id for r in self.g.recent_answered(REPO)})
        node = mcp.call_tool(self.store, 'bridge_add_node', {'task_id': later,
            'question': 'How many days should records remain?', 'paths': 'policy/retention.py'})
        self.assertNotIn('99 years', json.dumps(node))

    def test_independent_human_policy_survives_source_namespace_filter(self):
        source = self.source(provider='jira', namespace='site-b')
        prior = self.node([source])
        self.store.answer(prior, {'answer': 'Independent retention policy: thirty days.', 'signed_by': 'Ada Example',
                                 'evidence_mode': 'independent'})
        later = self.store.add_run({'title': 'Site A task', 'repo': REPO})['id']
        self.source(provider='jira', namespace='site-a', task_id=later)
        with self.g.source_scope(later):
            self.assertIn(prior, {r['id'] for r in self.g._memory_rows(('approved', 'resolved'), REPO)})

    def test_source_sequence_rejects_nonintegral_rest_values_without_rounding(self):
        for sequence in (1.2, 1.0, True, '1.2', 'NaN', -1):
            with self.subTest(sequence=sequence), self.assertRaises(Invalid):
                self.source(source_sequence=sequence)
        imported = self.source(source_sequence='2147483648')
        self.assertEqual(self.store.get_record(imported['source']['record_id'], REPO)['versions'][0]['source_sequence'], 2147483648)

    def test_legacy_proof_without_additive_sources_field_is_not_falsely_stale(self):
        did = self.node()
        self.sign(did)
        self.store.update_run(self.task, {'status': 'completed'})
        bundle = proof.create(self.store, self.task, 'diff --git a/a b/a\n+new\n')
        legacy = json.loads(json.dumps(bundle))
        for node in legacy['payload']['decisions']:
            node.pop('sources')
        legacy['id'] = proof._digest(legacy['payload'])
        self.g.append_event('proof_exported', {'task_id': self.task, 'bundle': legacy})
        result = proof.export(self.store, {'task_id': self.task})
        self.assertFalse(result['stale'])
        self.assertEqual(result['bundle'], legacy)
        self.assertTrue(result['integrity']['valid'])
        self.assertNotIn('sources', result['bundle']['payload']['decisions'][0])

    def test_explicit_current_source_readback_revalidates_without_claiming_independence(self):
        a = self.source()
        did = self.node([a])
        self.sign(did)
        self.source(body='Current source: retain records for thirty days after migration.')
        reading = self.store.get_decision(did)
        review = reading['source_revalidation']
        self.assertTrue(review['available'])
        self.assertIn('after migration', review['sources'][0]['snapshot']['body'])
        self.assertFalse(self.store.get_decision(did)['authorized'])  # read is not an action
        signed = canvas.sign_off(self.store, did, {'by': 'Ada Example', 'expected_updated_at': reading['updated_at'],
            'source_evidence': review['pins'], 'source_decision_pins': review['decision_pins']})
        self.assertTrue(signed['authorized'])
        self.assertFalse(self.store.get_decision(did)['independent_source_replacement'])
        self.assertEqual(signed['sources'][0]['source_version_id'], review['pins'][0]['source_version_id'])
        self.source(body='A subsequent material source change.')
        self.assertFalse(self.store.get_decision(did)['authorized'])

    def test_current_source_readback_race_refuses_stale_confirmation(self):
        a = self.source()
        did = self.node([a])
        self.sign(did)
        self.source(body='Revision B')
        reading = self.store.get_decision(did)
        self.source(body='Revision C, after readback')
        with self.assertRaises(Invalid):
            canvas.sign_off(self.store, did, {'by': 'Ada Example', 'expected_updated_at': reading['updated_at'],
                'source_evidence': reading['source_revalidation']['pins']})
        self.assertFalse(self.store.get_decision(did)['authorized'])

    def test_missing_access_or_stale_parent_has_no_available_revalidation(self):
        a = self.source()
        parent = self.node([a])
        self.sign(parent)
        child = self.node()
        self.g.update_decision(child, parent_id=parent)
        self.sign(child)
        self.source(body='Revised premise')
        review = self.store.get_decision(child)['source_revalidation']
        self.assertFalse(review['available'])
        self.assertEqual(review['dependencies'][0]['decision_id'], parent)
        self.assertIn(parent, review['notice'])
        self.assertEqual(review['sources'][0]['snapshot']['body'], 'Revised premise')
        self.source(availability='inaccessible')
        self.assertFalse(self.store.get_decision(parent)['source_revalidation']['available'])
        self.assertEqual(self.store.get_decision(parent)['source_revalidation']['sources'][0]['snapshot']['body'], '')

    def test_source_notifications_queue_after_commit_and_dedupe_noops(self):
        from test_delivery import FakeSlack
        slack = FakeSlack()
        self.store.add_person({'name': 'Ada Example', 'slack_id': 'UADA'})
        delivery = self.store.connect_delivery(slack, fallback_channel='CEXAMPLE')
        a = self.source()
        did = self.node([a])
        self.sign(did)
        before = len(slack.messages)
        changed = self.source(body='Updated policy B')
        self.assertIn(did, changed['affected_decisions'])
        self.assertEqual(len(slack.messages), before)
        self.assertEqual(self.g.db.execute('SELECT state FROM source_review_requests WHERE decision_id=?', (did,)).fetchone()[0], 'pending')
        delivery.deliver_now()
        count = self.g.db.execute("SELECT count(*) FROM notifications WHERE decision_id=? AND kind='review'", (did,)).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(self.source(body='Updated policy B')['affected_decisions'], [])
        delivery.deliver_now()
        self.assertEqual(self.g.db.execute("SELECT count(*) FROM notifications WHERE decision_id=? AND kind='review'", (did,)).fetchone()[0], count)
        self.source(body='Updated policy C')
        delivery.deliver_now()
        self.assertEqual(self.g.db.execute("SELECT count(*) FROM notifications WHERE decision_id=? AND kind='review'", (did,)).fetchone()[0], 2)

    def test_completed_GH104_history_stays_signed_while_active_GH105_revalidates(self):
        from bridge.graph import rule_status
        source_args = {'kind': 'issue', 'provider': 'github', 'namespace': 'github.com',
                       'external_id': REPO + '/issues/104'}
        source = self.source('GH-104', **source_args)
        historical = self.node([source])
        self.sign(historical)
        self.store.make_rule(historical, {'by': 'Ada Example'})
        self.g.set_setting('auto_rules', '1')
        self.store.update_run(self.task, {'status': 'completed'})
        saved = proof.create(self.store, self.task, 'diff --git a/policy/a b/policy/a\n+historical\n')
        before = self.store.get_decision(historical)
        active_task = self.store.add_run({'title': 'GH105 active reuse', 'repo': REPO})['id']
        active = canvas.add_node(self.store, Config(model_api='none'), {'task_id': active_task,
            'question': 'How many days should records remain?', 'paths': 'policy/retention.py'})
        current = self.store.get_decision(active['node_id'])
        self.assertEqual(current['source_id'], historical)
        self.source('GH-104', body='Current premise after the historical work completed.', **source_args)
        past = self.store.get_decision(historical)
        self.assertEqual(past['signoff'], 'signed')
        self.assertEqual(past['signed_revision'], before['signed_revision'])
        self.assertEqual(past['signatures'], before['signatures'])
        self.assertTrue(past['needs_review'])  # current applicability, not a revoked historical signature
        self.assertFalse(rule_status(self.g.get_decision(historical), before['question'])[0])
        self.assertFalse(self.store.get_decision(active['node_id'])['authorized'])
        self.assertTrue(self.store.get_decision(active['node_id'])['needs_review'])
        exported = proof.export(self.store, {'task_id': self.task})
        self.assertEqual(exported['bundle'], saved)
        self.assertTrue(exported['integrity']['valid'])
        self.assertTrue(exported['stale'])
        reading = self.store.get_decision(active['node_id'])
        review = reading['source_revalidation']
        self.assertTrue(review['available'], review['notice'])
        self.assertTrue(any(pin['historical'] for pin in review['decision_pins']))
        # Current work can be revalidated without editing or resigning GH104.
        canvas.sign_off(self.store, active['node_id'], {'by': 'Ada Example', 'expected_updated_at': reading['updated_at'],
            'source_evidence': review['pins'], 'source_decision_pins': review['decision_pins']})
        self.assertTrue(self.store.get_decision(active['node_id'])['authorized'])
        self.assertEqual(self.store.get_decision(historical)['signatures'], before['signatures'])
        self.assertEqual(proof.export(self.store, {'task_id': self.task})['bundle'], saved)

    def test_completed_revalidation_requires_fresh_cosigners_for_new_premises(self):
        source = self.source()
        did = self.node([source])
        self.g.db.execute('UPDATE decisions SET required_signers=? WHERE id=?', (json.dumps(['Ada Example', 'Bo Example']), did))
        self.sign(did)
        canvas.sign_off(self.store, did, {'by': 'Bo Example', 'expected_updated_at': self.store.get_decision(did)['updated_at']})
        before = self.store.get_decision(did)
        self.store.update_run(self.task, {'status': 'completed'})
        self.source(body='New premise requiring another review.')
        reading = self.store.get_decision(did)
        review = reading['source_revalidation']
        canvas.sign_off(self.store, did, {'by': 'Ada Example', 'expected_updated_at': reading['updated_at'],
            'source_evidence': review['pins'], 'source_decision_pins': review['decision_pins']})
        partial = self.store.get_decision(did)
        self.assertFalse(partial['authorized'])
        self.assertEqual([signature['by'] for signature in json.loads(partial['signatures'])], ['Ada Example'])
        self.assertEqual(partial['signed_revision'], '')
        for version in partial['context_history']:
            old = json.loads(version['snapshot'])
            if old['decision']['signed_revision'] == before['signed_revision']:
                self.assertEqual(old['sources'][0]['source_version_id'], source['source']['source_version_id'])
        canvas.sign_off(self.store, did, {'by': 'Bo Example', 'expected_updated_at': partial['updated_at']})
        self.assertTrue(self.store.get_decision(did)['authorized'])
