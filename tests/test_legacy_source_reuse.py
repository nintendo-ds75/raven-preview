"""Synthetic migration/auto-rule contracts; no external services or providers."""
import json
from pathlib import Path

from fixtures import OfflineCase
from bridge import canvas, context_memory as cm, proof
from bridge.config import Config
from bridge.graph import rule_status
from bridge.store import Invalid, Store

REPO = 'synthetic/legacy-source'
QUESTION = 'How many days should records remain?'
ANSWER = 'Retain records for thirty days.'


class LegacySourceReuseTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'legacy-reuse.db')
        self.g = self.store.graph
        self.addCleanup(self.g.close)
        self.store.add_owner({'name': 'Ada Example', 'team': 'Example', 'patterns': 'policy/*'})
        self.g.set_setting('auto_rules', '1')

    def task(self):
        return self.store.add_run({'title': 'Synthetic retention policy', 'repo': REPO})['id']

    def rule(self, source='record', completed=False):
        task = self.task()
        did = self.g.add_decision(task, QUESTION, 'policy', 'resolved', repo=REPO,
                                  owner='Ada Example', path='policy/retention.py', source=source,
                                  answer=ANSWER, kind='evidence')
        self.g.update_decision(did, evidence='jira OLD-1: legacy citation, not a source identity')
        if source == 'human':
            self.store.answer(did, {'answer': ANSWER, 'signed_by': 'Ada Example'})
        else:
            canvas.sign_off(self.store, did, {'by': 'Ada Example', 'expected_updated_at': self.store.get_decision(did)['updated_at']})
        self.store.make_rule(did, {'by': 'Ada Example'})
        if completed:
            self.store.update_run(task, {'status': 'completed'})
        return did, task

    def restart(self):
        reopened = Store(self.store.path)
        self.addCleanup(reopened.graph.close)
        self.store, self.g = reopened, reopened.graph
        return reopened

    def reuse(self):
        return canvas.add_node(self.store, Config(model_api='none'), {'task_id': self.task(),
            'question': QUESTION, 'paths': 'policy/retention.py', 'category': 'policy'})

    def source(self, **kwargs):
        return self.store.add_record({'repo': REPO, 'kind': 'jira', 'ref': 'OLD-1',
            'title': 'Retention policy', 'body': ANSWER, 'status': 'Done', **kwargs})['source']

    def review(self, did, source, **kwargs):
        return canvas.sign_off(self.store, did, {'by': 'Ada Example',
            'expected_updated_at': self.store.get_decision(did)['updated_at'],
            'source_evidence': [cm.pin(source)], **kwargs})

    def test_migration_restart_auto_rules_preserves_completed_proof_and_signature_facts(self):
        did, task = self.rule(completed=True)
        saved = proof.create(self.store, task, 'diff --git a/a b/a\n+new\n')
        frozen = cm.encoded(saved)
        columns = 'status,signoff,signatures,signed_by,signed_hash,signed_revision,updated_at,needs_review'
        facts = tuple(self.g.db.execute('SELECT '+columns+' FROM decisions WHERE id=?', (did,)).fetchone())
        self.g.db.execute('ALTER TABLE decisions DROP COLUMN source_reuse_state')
        self.restart()
        after = tuple(self.g.db.execute('SELECT '+columns+' FROM decisions WHERE id=?', (did,)).fetchone())
        self.assertEqual(after, facts)
        self.assertFalse(rule_status(self.g.get_decision(did), QUESTION)[0])
        result = proof.export(self.store, {'task_id': task})
        self.assertEqual(cm.encoded(result['bundle']), frozen)
        self.assertTrue(result['integrity']['valid'])
        self.assertFalse(result['stale'])  # historical account did not change
        self.assertTrue(result['current_reuse'][did]['requires_source_review'])
        self.assertIn('unknown', result['current_reuse'][did]['notice'])
        fresh = self.reuse()
        self.assertFalse(fresh['authorized'])
        self.assertNotEqual(fresh['signoff'], 'rule')
        self.assertTrue(fresh['source_reuse_requires_review'])
        self.assertIn('Automatic reuse requires deliberate review', fresh['evidence'])
        self.assertEqual(self.store.get_decision(did)['sources'], [])
        self.restart()
        self.assertFalse(self.reuse()['authorized'])

    def test_matching_import_noop_and_new_revision_never_pin_old_answer(self):
        did, _ = self.rule()
        old_history = cm.history(self.g.db, did)
        a = self.source()
        b = self.source()
        self.assertEqual(a['source_version_id'], b['source_version_id'])
        self.source(body='Retain for seven days.')
        self.restart()
        old = self.store.get_decision(did)
        self.assertEqual(old['sources'], [])
        self.assertEqual(cm.history(self.g.db, did), old_history)
        self.assertEqual(old['source_provenance'], 'unknown')
        self.assertFalse(old['source_revalidation']['available'])
        self.assertFalse(self.reuse()['authorized'])

    def test_typed_record_history_survives_human_correction_and_rule_renewal(self):
        did, _ = self.rule(source='human')
        self.g.append_event('resolve', {'task_id': self.store.get_decision(did)['run_id'],
            'decision_id': did, 'source': 'record', 'cited': 'unparseable free prose'})
        self.g.db.execute("UPDATE decisions SET source_reuse_state='' WHERE id=?", (did,))
        self.restart()
        self.assertTrue(self.store.get_decision(did)['source_reuse_uncertain'])
        self.store.answer(did, {'answer': ANSWER, 'signed_by': 'Ada Example'})
        self.store.make_rule(did, {'by': 'Ada Example'})
        self.assertEqual(self.store.get_decision(did)['source'], 'human')
        self.assertFalse(self.reuse()['authorized'])

    def test_human_only_standing_rule_ignores_citation_prose_and_unrelated_events(self):
        did, _ = self.rule(source='human')
        self.g.append_event('comment', {'decision_id': did, 'source': 'record', 'text': 'jira OLD-1'})
        self.g.db.execute("UPDATE decisions SET source_reuse_state='' WHERE id=?", (did,))
        self.restart()
        fresh = self.reuse()
        self.assertTrue(fresh['authorized'])
        self.assertEqual(fresh['signoff'], 'rule')
        self.assertFalse(fresh['source_reuse_requires_review'])

    def test_malformed_missing_and_partial_typed_provenance_fail_closed(self):
        for detail in ('{not json', '[]', '{}', '{"source": 5}', '{"source": "unrecognized"}'):
            with self.subTest(detail=detail):
                did, _ = self.rule(source='human')
                self.g.db.execute('INSERT INTO events(decision_id,kind,detail,created_at) VALUES(?,?,?,?)',
                    (did, 'resolve', detail, cm.stamp()))
                self.g.db.execute("UPDATE decisions SET source_reuse_state='' WHERE id=?", (did,))
                self.restart()
                self.assertFalse(rule_status(self.g.get_decision(did), QUESTION)[0])
        for source in ('record', 'memory'):
            did, _ = self.rule(source=source)
            self.g.db.execute("UPDATE decisions SET source_reuse_state='' WHERE id=?", (did,))
            self.restart()
            self.assertFalse(rule_status(self.g.get_decision(did), QUESTION)[0])
        # A known current pin is not evidence that every old unknown premise
        # was reviewed. Attaching a context pin or partial support is insufficient.
        did, _ = self.rule()
        with self.g.transaction():
            cm.attach(self.g.db, did, [cm.pin(self.source())])
        self.assertTrue(self.store.get_decision(did)['source_reuse_uncertain'])
        self.assertEqual(self.store.get_decision(did)['source_provenance'], 'unknown')
        self.assertFalse(self.store.get_decision(did)['source_revalidation']['available'])
        self.restart()
        self.assertFalse(rule_status(self.g.get_decision(did), QUESTION)[0])

    def test_explicit_independent_replacement_clears_gate_even_with_identical_text(self):
        did, _ = self.rule()
        old = self.store.get_decision(did)
        self.store.answer(did, {'answer': ANSWER, 'signed_by': 'Ada Example',
            'expected_updated_at': old['updated_at'], 'evidence_mode': 'independent'})
        self.restart()
        current = self.store.get_decision(did)
        self.assertFalse(current['source_reuse_uncertain'])
        self.assertFalse(current['reusable'])
        self.assertTrue(current['rule_ended_at'])
        self.assertFalse(self.reuse()['authorized'])
        self.assertEqual(current['sources'], [])
        # Clearing legacy provenance is not a new standing authorization.
        self.store.make_rule(did, {'by': 'Ada Example', 'expected_updated_at': current['updated_at']})
        self.restart()
        self.assertTrue(self.reuse()['authorized'])
        self.assertEqual(self.store.get_decision(did)['sources'], [])

    def test_current_source_review_clears_gate_and_retains_real_current_pin(self):
        did, _ = self.rule()
        source = self.source()
        self.review(did, source)
        self.restart()
        current = self.store.get_decision(did)
        self.assertFalse(current['reusable'])
        self.assertTrue(current['rule_ended_at'])
        self.assertFalse(self.reuse()['authorized'])
        # First current-source review replaces the unknown old derivation;
        # only explicit regrant permits automatic reuse of these real pins.
        self.store.make_rule(did, {'by': 'Ada Example', 'expected_updated_at': current['updated_at']})
        self.restart()
        self.assertTrue(self.reuse()['authorized'])
        current = self.store.get_decision(did)
        self.assertFalse(current['source_reuse_uncertain'])
        self.assertFalse(current['independent_source_replacement'])
        self.assertEqual(current['sources'][0]['source_version_id'], source['source_version_id'])
        self.source(body='Current policy changed again.')
        self.assertTrue(self.store.get_decision(did)['needs_review'])
        self.assertFalse(self.store.get_decision(did)['authorized'])

    def test_review_rejects_stale_context_only_and_wrong_namespace_pins(self):
        did, task = self.rule()
        a = self.source(provider='jira', namespace='a')
        b = self.source(provider='jira', namespace='b')
        with self.g.transaction():
            cm.add_anchor(self.g.db, task, a['record_id'], a['source_version_id'])
        with self.assertRaisesRegex(Invalid, 'namespace'):
            self.review(did, b)
        with self.assertRaisesRegex(Invalid, 'supporting'):
            canvas.sign_off(self.store, did, {'by': 'Ada Example', 'expected_updated_at': self.store.get_decision(did)['updated_at'], 'source_evidence': [cm.pin(a, 'context')]})
        self.source(provider='jira', namespace='a', body='Seven days.')
        with self.assertRaisesRegex(Invalid, 'changed'):
            self.review(did, a)
        self.assertTrue(self.store.get_decision(did)['source_reuse_uncertain'])

    def test_migration_withdraws_only_active_automatic_chains_and_traverses_completed_nodes(self):
        did, _ = self.rule(completed=True)
        active_task, history_task = self.task(), self.task()
        def dependent(task, parent, signoff='rule', source='memory'):
            node = self.g.add_decision(task, QUESTION, 'policy', 'resolved', source=source,
                repo=REPO, owner='Ada Example', answer=ANSWER)
            self.g.update_decision(node, source_id=parent, signoff=signoff, signed_by='Ada Example')
            return node
        history = dependent(history_task, did)
        self.g.db.execute("UPDATE runs SET status='completed' WHERE id=?", (history_task,))
        live = dependent(active_task, history)
        child = dependent(active_task, live, signoff='signed')
        unrelated, _ = self.rule(source='human')
        unrelated_live = dependent(active_task, unrelated)
        direct_signed = dependent(active_task, did, signoff='signed')
        original = tuple(self.g.db.execute('SELECT signoff,signed_by,updated_at FROM decisions WHERE id=?', (history,)).fetchone())
        self.g.db.execute("UPDATE decisions SET source_reuse_state='' ")
        self.restart()
        self.assertTrue(self.store.get_decision(live)['needs_review'])
        self.assertTrue(self.store.get_decision(child)['needs_review'])
        self.assertFalse(self.store.get_decision(unrelated_live)['needs_review'])
        self.assertTrue(self.store.get_decision(unrelated_live)['authorized'])
        self.assertFalse(self.store.get_decision(direct_signed)['needs_review'])
        self.assertTrue(self.store.get_decision(direct_signed)['authorized'])
        after = tuple(self.g.db.execute('SELECT signoff,signed_by,updated_at FROM decisions WHERE id=?', (history,)).fetchone())
        self.assertEqual(after, original)
        events = self.g.db.execute("SELECT count(*) FROM events WHERE kind='legacy_source_reuse_review'").fetchone()[0]
        self.restart()
        self.assertEqual(self.g.db.execute("SELECT count(*) FROM events WHERE kind='legacy_source_reuse_review'").fetchone()[0], events)

    def test_completed_legacy_precedent_can_be_revalidated_on_new_task_with_exact_decision_pins(self):
        did, old_task = self.rule(completed=True)
        frozen = proof.create(self.store, old_task, 'diff --git a/a b/a\n+old\n')
        fresh = self.reuse()
        nid = fresh['node_id']
        source = self.source()
        reading = self.store.get_decision(nid)
        self.assertFalse(reading['source_revalidation']['available'])
        self.assertTrue(reading['source_revalidation']['decision_pins'][0]['historical'])
        with self.assertRaisesRegex(Invalid, 'source_decision_pins'):
            self.review(nid, source)
        signed = self.review(nid, source, source_decision_pins=reading['source_revalidation']['decision_pins'])
        self.assertTrue(signed['authorized'])
        self.assertFalse(signed['source_reuse_requires_review'])
        self.assertTrue(self.store.get_decision(did)['source_reuse_uncertain'])
        self.assertEqual(self.store.get_decision(did)['sources'], [])
        self.assertEqual(proof.export(self.store, {'task_id': old_task})['bundle'], frozen)
        self.source(body='Seven days after revalidation.')
        self.assertTrue(self.store.get_decision(nid)['needs_review'])

    def test_ordinary_fresh_signoff_is_local_authority_not_legacy_source_review(self):
        self.rule(completed=True)
        fresh = self.reuse()
        nid = fresh['node_id']
        canvas.sign_off(self.store, nid, {'by': 'Ada Example',
            'expected_updated_at': self.store.get_decision(nid)['updated_at']})
        self.assertTrue(self.store.get_decision(nid)['authorized'])
        self.assertTrue(self.store.get_decision(nid)['source_reuse_uncertain'])
        self.store.make_rule(nid, {'by': 'Ada Example'})
        self.assertFalse(rule_status(self.g.get_decision(nid), QUESTION)[0])

    def test_current_source_review_requires_fresh_revision_and_fresh_cosigners(self):
        did, _ = self.rule()
        source = self.source()
        with self.assertRaisesRegex(Invalid, 'expected_updated_at'):
            self.store.answer(did, {'answer': ANSWER, 'signed_by': 'Ada Example', 'source_evidence': [cm.pin(source)]})
        # A partial pin attached before review is not an owner review. Even if
        # the submitted pin is identical, old co-signatures must not carry over.
        with self.g.transaction():
            cm.attach(self.g.db, did, [cm.pin(source)])
        self.g.db.execute("UPDATE decisions SET required_signers='[\"Ada Example\",\"Bea Example\"]',signatures=? WHERE id=?",
            (json.dumps([{'by': 'Bea Example', 'hash': self.store.get_decision(did)['signed_hash']}]), did))
        signed = self.review(did, source)
        self.assertFalse(signed['authorized'])
        self.assertEqual(signed['signatures'], ['Ada Example'])
        self.assertFalse(signed['source_reuse_requires_review'])

    def test_unknown_state_and_missing_typed_links_do_not_authorize(self):
        did, _ = self.rule(source='human')
        self.g.db.execute("UPDATE decisions SET source_reuse_state='invalid-state' WHERE id=?", (did,))
        self.assertFalse(rule_status(self.g.get_decision(did), QUESTION)[0])
        self.g.db.execute("UPDATE decisions SET source_reuse_state='tracked',source_id='missing-source' WHERE id=?", (did,))
        self.assertFalse(rule_status(self.g.get_decision(did), QUESTION)[0])
        # A cycle does not hang the lineage read, and cannot hide an unknown source.
        other, _ = self.rule()
        self.g.db.execute('UPDATE decisions SET source_id=? WHERE id=?', (other, did))
        self.g.db.execute('UPDATE decisions SET source_id=? WHERE id=?', (did, other))
        self.assertFalse(rule_status(self.g.get_decision(did), QUESTION)[0])

    def test_publication_rechecks_uncertainty_inside_the_rule_write_boundary(self):
        from bridge.ladder import _as_record
        did, _ = self.rule(source='human')
        selected = _as_record(self.g.get_decision(did))
        scratch = self.g.add_decision(self.task(), QUESTION, 'policy', 'pending', repo=REPO, owner='Ada Example')
        self.g.publish_evidence(scratch, [selected], source='memory', answer=ANSWER, signoff='rule')
        self.g.db.execute("UPDATE decisions SET source_reuse_state='unknown' WHERE id=?", (did,))
        target = self.g.add_decision(self.task(), QUESTION, 'policy', 'pending', repo=REPO, owner='Ada Example')
        with self.assertRaisesRegex(Invalid, 'Automatic reuse'):
            self.g.publish_evidence(target, [selected], source='memory', answer=ANSWER, signoff='rule')
        self.assertFalse(self.store.get_decision(target)['authorized'])
        self.assertEqual(self.store.get_decision(target)['sources'], [])
        with self.assertRaisesRegex(Invalid, 'Automatic reuse'):
            cm.validate_derivations(self.g.db, scratch)

    def test_malformed_snapshot_and_independence_marker_cannot_clear_unknown_derivation(self):
        did, _ = self.rule(source='human')
        self.g.db.execute("UPDATE decisions SET source_reuse_state='' WHERE id=?", (did,))
        value = {'decision': {'source': 'record'}, 'sources': [{'record_id': 'partial'}]}
        self.g.db.execute('INSERT INTO decision_versions(id,decision_id,sequence,snapshot,fingerprint,recorded_at) VALUES(?,?,?,?,?,?)',
                         ('malformed-snapshot', did, 999, json.dumps(value), 'synthetic', cm.stamp()))
        self.restart()
        self.assertFalse(rule_status(self.g.get_decision(did), QUESTION)[0])
        # INTEGER 2 is not the explicit API marker (1), on either backend.
        self.g.db.execute("UPDATE decisions SET source_reuse_state='',independent_source_replacement=2 WHERE id=?", (did,))
        self.restart()
        self.assertFalse(rule_status(self.g.get_decision(did), QUESTION)[0])

    def test_unrecognized_row_origin_is_not_independent_human_provenance(self):
        for origin in ('unrecognized', 'jira'):
            with self.subTest(origin=origin):
                did, _ = self.rule(source=origin)
                self.g.db.execute("UPDATE decisions SET source_reuse_state='' WHERE id=?", (did,))
                self.restart()
                self.assertFalse(rule_status(self.g.get_decision(did), QUESTION)[0])
                self.assertTrue(self.store.get_decision(did)['source_reuse_requires_review'])
        did, _ = self.rule(source='human')
        self.g.update_decision(did, source='unknown-external')
        self.assertFalse(rule_status(self.g.get_decision(did), QUESTION)[0])
