"""Deliberate current-human-source review binds exact immutable decisions."""
import json
import threading
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
from test_auth import SharedServer, BOOTSTRAP
from bridge import canvas, context_memory as cm, ladder, proof
from bridge.graph import ts_to_iso
from bridge.config import Config
from bridge.store import Invalid, Store

REPO = 'synthetic/human-pins'


class HumanSourceRevalidationTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'human-pins.db')
        self.g = self.store.graph
        self.addCleanup(self.g.close)
        self.store.add_owner({'name': 'Policy Owner', 'team': 'Policy', 'patterns': 'policy/*'})
        for method in ('complete', 'complete_json'):
            guard = patch('bridge.llm.Client.' + method, side_effect=AssertionError('Provider calls forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def node(self, question='How long should records remain?', **fields):
        task = self.store.add_run({'title': question, 'repo': REPO})['id']
        return task, self.g.add_decision(task, question, 'policy', 'pending', repo=REPO,
                                        owner='Policy Owner', path='policy/retention.py', **fields)

    def parent(self, external=False):
        task, did = self.node()
        if external:
            source = self.store.add_record({'repo': REPO, 'kind': 'jira', 'provider': 'jira',
                'namespace': 'policy.example', 'external_id': 'POL-1', 'ref': 'POL-1',
                'title': 'Current retention premise', 'body': 'The owner decides the retention period.', 'status': 'Done'})
            row = dict(self.g.db.execute('SELECT i.*,s.id AS record_id,s.head_id AS source_version_id FROM intents i JOIN source_records s ON s.intent_id=i.id WHERE s.id=?', (source['source']['record_id'],)).fetchone())
            self.g.publish_evidence(did, [row], answer='Thirty days.', status='resolved', source='record', kind='evidence', signoff='required')
            self.sign(did)
        else:
            self.store.answer(did, {'answer': 'Thirty days.'})
        return task, did

    def child(self, parent, extra_sources=()):
        task, did = self.node('What period should the archive apply?')
        source = self.g.get_decision(parent)
        records = [ladder._as_record(self.g.get_decision(p)) for p in (parent, *extra_sources)]
        self.g.publish_evidence(did, records, answer=source.answer, status='resolved', source='memory',
            source_id=parent, source_revision=ts_to_iso(source.updated_at), kind='evidence', signoff='required')
        self.sign(did)
        return task, did

    def sign(self, did, **fields):
        return canvas.sign_off(self.store, did, {'by': 'Policy Owner',
            'expected_updated_at': self.store.get_decision(did)['updated_at'], **fields})

    def correction(self, did, answer='Seven days.'):
        return self.store.answer(did, {'answer': answer, 'expected_updated_at': self.store.get_decision(did)['updated_at']})

    def review(self, did):
        current = self.store.get_decision(did)
        review = current['source_revalidation']
        self.assertTrue(review['available'], review['notice'])
        return {'expected_updated_at': current['updated_at'], 'source_evidence': review['pins'],
                'source_decision_pins': review['decision_pins']}

    def link(self, did, parent):
        return self.g.db.execute("SELECT source_version_id FROM decision_links WHERE decision_id=? AND related_id=? AND kind='derived'", (did, parent)).fetchone()[0]

    def test_fresh_complete_review_rebinds_current_human_version_and_preserves_history(self):
        _, parent = self.parent(external=True)
        _, child = self.child(parent)
        old_id = self.link(child, parent)
        old_source = dict(self.g.db.execute('SELECT * FROM decision_versions WHERE id=?', (old_id,)).fetchone())
        old_child = cm.history(self.g.db, child)
        self.correction(parent)
        payload = self.review(child)
        displayed = payload['source_decision_pins'][0]['source_version_id']
        self.assertNotEqual(displayed, old_id)
        self.store.answer(child, {'answer': 'Seven days.', **payload})
        self.assertTrue(self.store.get_decision(child)['authorized'])
        self.assertEqual(self.link(child, parent), displayed)
        new = json.loads(self.g.db.execute('SELECT snapshot FROM decision_versions WHERE id=?', (displayed,)).fetchone()[0])
        self.assertEqual(new['decision']['answer'], 'Seven days.')
        self.assertEqual(dict(self.g.db.execute('SELECT * FROM decision_versions WHERE id=?', (old_id,)).fetchone()), old_source)
        self.assertEqual(cm.history(self.g.db, child)[:len(old_child)], old_child)
        self.assertTrue(any(any(link['source_version_id'] == old_id for link in json.loads(v['snapshot'])['derivations']) for v in old_child))

    def test_complete_human_review_is_exactly_json_round_trip_stable(self):
        _, parent = self.parent(external=True)
        _, child = self.child(parent)
        review = self.store.get_decision(child)['source_revalidation']
        self.assertTrue(review['available'])
        self.assertEqual(review, json.loads(json.dumps(review)))
        binding = review['dependencies'][0]['reviewed_snapshot']
        self.assertTrue(binding['sources'])
        self.assertEqual(binding, json.loads(json.dumps(binding)))

    def test_pure_human_chain_can_review_without_external_sources(self):
        _, parent = self.parent()
        _, child = self.child(parent)
        self.correction(parent)
        payload = self.review(child)
        self.assertEqual(payload['source_evidence'], [])
        self.assertEqual(len(payload['source_decision_pins']), 1)
        self.store.answer(child, {'answer': 'Seven days.', **payload})
        self.assertTrue(self.store.get_decision(child)['authorized'])
        self.assertEqual(self.link(child, parent), payload['source_decision_pins'][0]['source_version_id'])

    def test_omitted_incomplete_and_unversioned_human_pins_are_rejected_atomically(self):
        _, parent = self.parent()
        _, second = self.parent()
        _, child = self.child(parent, [second])
        self.correction(parent)
        full = self.review(child)
        before = self.store.get_decision(child)
        link = self.link(child, parent)
        variants = [{}, {'source_evidence': full['source_evidence']},
            {**full, 'source_decision_pins': full['source_decision_pins'][:1]},
            {**full, 'source_decision_pins': [{k: v for k, v in p.items() if k != 'source_snapshot_sha256'} for p in full['source_decision_pins']]}]
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaisesRegex(Invalid, 'review|pins|revalidation'):
                self.store.answer(child, {'answer': 'Seven days.', 'expected_updated_at': before['updated_at'], **payload})
            after = self.store.get_decision(child)
            self.assertEqual((after['updated_at'], after['signatures'], after['answer']), (before['updated_at'], before['signatures'], before['answer']))
            self.assertEqual(self.link(child, parent), link)

    def test_same_text_human_revision_changes_version_and_rejects_old_review(self):
        _, parent = self.parent()
        _, child = self.child(parent)
        old_review = self.review(child)
        old_id = old_review['source_decision_pins'][0]['source_version_id']
        self.correction(parent, 'Thirty days.')
        current = self.review(child)
        self.assertNotEqual(current['source_decision_pins'][0]['source_version_id'], old_id)
        with self.assertRaisesRegex(Invalid, 'source decision changed|immutable'):
            self.store.answer(child, {'answer': 'Thirty days.', **old_review,
                'expected_updated_at': self.store.get_decision(child)['updated_at']})
        self.store.answer(child, {'answer': 'Thirty days.', **current})
        self.assertTrue(self.store.get_decision(child)['authorized'])

    def test_dependency_readback_binds_scope_rationale_and_authority_not_only_timestamp(self):
        _, parent = self.parent()
        _, child = self.child(parent)
        self.g.db.execute('UPDATE decisions SET context=?,facts=?,scope_paths=?,rationale=?,applicability=?,required_signers=?,rule_conditions=?,rule_scope=? WHERE id=?',
            ('Release 4 only', '{"customer":"Northstar"}', '["policy/northstar.py"]', 'Contract limitation', '{"customer":"Northstar"}', '["Policy Owner"]', 'customer=Northstar', 'scope', parent))
        review = self.store.get_decision(child)['source_revalidation']
        dep = review['dependencies'][0]
        for key in ('context', 'facts', 'scope_paths', 'rationale', 'applicability', 'required_signers', 'category', 'options', 'rule_conditions', 'rule_scope', 'rule_expires'):
            self.assertEqual(dep[key], dep['reviewed_snapshot']['decision'][key])
        self.assertIn('Northstar', dep['facts'])
        self.assertEqual(dep['rationale'], 'Contract limitation')
        payload = self.review(child)
        # Adversarial same-timestamp mutation: the content digest must reject it.
        self.g.db.execute('UPDATE decisions SET facts=? WHERE id=?', ('{"customer":"Bluebird"}', parent))
        with self.assertRaisesRegex(Invalid, 'immutable|scope'):
            self.sign(child, **payload)
        self.assertTrue(self.g.blocking_nodes(self.g.get_decision(child).task_id))

    def test_read_only_preview_freezes_exact_unsnapshotted_observation_on_confirmation(self):
        _, parent = self.parent()
        _, child = self.child(parent)
        self.g.db.execute('UPDATE decisions SET rationale=? WHERE id=?', ('New limitation without timestamp rewrite', parent))
        before = cm.history(self.g.db, parent)
        payload = self.review(child)
        self.assertEqual(payload['source_decision_pins'][0]['source_version_id'], '')
        self.assertEqual(cm.history(self.g.db, parent), before)
        self.sign(child, **payload)
        linked = self.link(child, parent)
        self.assertTrue(linked)
        saved = json.loads(self.g.db.execute('SELECT snapshot FROM decision_versions WHERE id=?', (linked,)).fetchone()[0])
        self.assertEqual(saved['decision']['rationale'], 'New limitation without timestamp rewrite')
        self.assertEqual(cm.digest(cm._binding_projection(saved)), payload['source_decision_pins'][0]['source_snapshot_sha256'])

    def test_source_change_after_readback_refuses_signoff_and_finish(self):
        _, parent = self.parent()
        task, child = self.child(parent)
        reading = self.review(child)
        other = Store(self.store.path)
        self.addCleanup(other.graph.close)
        errors = []
        def writer():
            try:
                other.answer(parent, {'answer': 'Seven days.', 'expected_updated_at': other.get_decision(parent)['updated_at']})
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=writer)
        thread.start(); thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        with self.assertRaises(Invalid):
            self.sign(child, **reading)
        with self.assertRaises(Invalid):
            self.store.update_run(task, {'status': 'completed'})
        self.assertFalse(self.store.get_decision(child)['authorized'])

    def test_repeated_complete_review_keeps_new_cosignatures(self):
        _, parent = self.parent()
        _, child = self.child(parent)
        self.g.db.execute('UPDATE decisions SET required_signers=? WHERE id=?', ('["Policy Owner", "Second Owner"]', child))
        self.correction(parent)
        self.sign(child, **self.review(child))
        partial = self.store.get_decision(child)
        self.assertFalse(partial['authorized'])
        self.assertEqual([s['by'] for s in json.loads(partial['signatures'])], ['Policy Owner'])
        self.sign(child, by='Second Owner', **self.review(child))
        final = self.store.get_decision(child)
        self.assertTrue(final['authorized'])
        self.assertEqual({s['by'] for s in json.loads(final['signatures'])}, {'Policy Owner', 'Second Owner'})

    def test_previously_delivered_cosigner_review_requires_reopen_after_signature_progress(self):
        _, parent = self.parent()
        _, child = self.child(parent)
        self.g.db.execute('UPDATE decisions SET required_signers=? WHERE id=?', ('["Policy Owner", "Second Owner"]', child))
        self.correction(parent)
        held_by_b = self.review(child)
        self.sign(child, **held_by_b)
        with self.assertRaisesRegex(Invalid, 'changed|revision|review'):
            self.sign(child, by='Second Owner', **held_by_b)
        partial = self.store.get_decision(child)
        self.assertEqual([s['by'] for s in json.loads(partial['signatures'])], ['Policy Owner'])
        fresh = self.review(child)
        self.assertEqual(held_by_b['source_decision_pins'], fresh['source_decision_pins'])
        self.sign(child, by='Second Owner', **fresh)
        self.assertTrue(self.store.get_decision(child)['authorized'])

    def test_parent_only_and_explicit_depends_links_receive_exact_reviewed_versions(self):
        _, parent = self.parent()
        task, child = self.node('Dependent policy?')
        self.g.update_decision(child, parent_id=parent)
        self.store.answer(child, {'answer': 'Thirty days.'})
        _, dependency = self.parent()
        self.g.db.execute("INSERT INTO decision_links(decision_id,related_id,kind,note,created_at) VALUES(?,?,'depends','',?)", (child, dependency, cm.stamp()))
        self.correction(parent)
        payload = self.review(child)
        self.store.answer(child, {'answer': 'Seven days.', **payload})
        versions = {p['decision_id']: p['source_version_id'] for p in payload['source_decision_pins']}
        actual = {r['related_id']: r['source_version_id'] for r in self.g.db.execute("SELECT related_id,source_version_id FROM decision_links WHERE decision_id=? AND kind IN ('derived','depends')", (child,))}
        self.assertEqual(actual, versions)
        self.assertTrue(self.store.get_decision(child)['authorized'])

    def test_unaccepted_inbox_hint_can_be_replaced_but_typed_pending_reliance_cannot(self):
        _, parent = self.parent()
        _, hint = self.node('Unanswered hint')
        self.g.update_decision(hint, kind='prediction', source_id=parent, source_revision='', answer=None)
        self.assertFalse(self.store.get_decision(hint)['source_revalidation']['available'])
        self.store.answer(hint, {'answer': 'A first human answer unrelated to the hint.'})
        self.assertTrue(self.store.get_decision(hint)['authorized'])
        self.assertFalse(self.store.get_decision(hint)['source_id'])
        _, real = self.child(parent)
        self.g.update_decision(real, status='pending', kind='prediction', answer=None, signoff='required')
        self.correction(parent)
        before = self.link(real, parent)
        with self.assertRaisesRegex(Invalid, 'review|revalidation'):
            self.store.answer(real, {'answer': 'Cannot discard the actual premise.'})
        self.assertEqual(self.link(real, parent), before)
        self.assertFalse(self.store.get_decision(real)['authorized'])

    def test_rebinding_uses_caller_writer_and_rolls_back_every_changed_pointer(self):
        _, parent = self.parent()
        _, child = self.child(parent)
        self.correction(parent)
        payload = self.review(child)
        link_before = self.link(child, parent)
        before = cm.history(self.g.db, child)
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.g.transaction():
                self.store.answer(child, {'answer': 'Seven days.', **payload}, transaction_db=self.g.db)
                self.assertEqual(self.link(child, parent), payload['source_decision_pins'][0]['source_version_id'])
                raise RuntimeError('rollback')
        self.assertEqual(self.link(child, parent), link_before)
        self.assertEqual(cm.history(self.g.db, child), before)
        self.assertFalse(self.store.get_decision(child)['authorized'])

    def test_fresh_inbox_hint_records_its_observed_revision_with_unrelated_dependencies(self):
        _, source = self.parent()
        task, hint = self.node('A pending inbox suggestion')
        source_row = self.store.get_decision(source)
        row = self.store.get_decision(hint)
        run = dict(self.g.db.execute('SELECT * FROM runs WHERE id=?', (task,)).fetchone())
        with patch.object(self.store, 'candidates', return_value=[{**source_row, 'similarity': .99}]):
            ladder._inbox_route(self.store, self.g, row, row['owner_id'], run, REPO)
        hint_row = self.store.get_decision(hint)
        self.assertEqual(hint_row['source_id'], source)
        self.assertEqual(hint_row['source_revision'], source_row['updated_at'])
        self.assertEqual(hint_row['kind'], 'prediction')
        _, real_dependency = self.parent()
        self.g.add_link(hint, real_dependency, 'depends')
        self.store.answer(hint, {'answer': 'The first human answer keeps the actual dependency.'})
        self.assertTrue(self.store.get_decision(hint)['authorized'])
        self.assertTrue(self.g.db.execute("SELECT 1 FROM decision_links WHERE decision_id=? AND related_id=? AND kind='depends'", (hint, real_dependency)).fetchone())

    def test_direct_request_records_fresh_hint_revision_without_granting_authority(self):
        _, source = self.parent()
        task = self.store.add_run({'title': 'An owner inbox request', 'repo': REPO})['id']
        source_row = self.store.get_decision(source)
        with patch.object(self.store, 'candidates', return_value=[{**source_row, 'similarity': .99}]):
            hint = self.store.request({'run_id': task, 'question': 'How should this archive behave?',
                                       'context': 'Current archive task', 'path': 'policy/retention.py'})
        self.assertEqual(hint['source_id'], source)
        self.assertEqual(hint['source_revision'], source_row['updated_at'])
        self.assertFalse(hint['authorized'])
        self.assertFalse(hint['source_revalidation']['has_reliance'])
        self.assertFalse(self.g.db.execute('SELECT 1 FROM decision_links WHERE decision_id=?', (hint['id'],)).fetchone())

    def test_composed_hint_cannot_pin_a_new_revision_it_did_not_observe(self):
        _, source = self.parent()
        task = self.store.add_run({'title': 'Write archive receipt wording', 'repo': REPO})['id']
        observed = self.store.get_decision(source)['updated_at']
        def raced(*args, observations, **kwargs):
            observations[source] = observed
            self.correction(source)
            return 'Thirty days.', source, 'Observed the old policy before composition.'
        with patch('bridge.ladder._how_they_decide', side_effect=raced) as compose:
            node = canvas.add_node(self.store, Config(model_api='none'), {'task_id': task,
                'question': 'Which phrase belongs on the archive receipt?', 'paths': 'policy/receipt.py'})
        self.assertEqual(compose.call_count, 1)
        row = self.store.get_decision(node['node_id'])
        self.assertFalse(row['prediction'])
        self.assertFalse(row['source_id'])
        self.assertFalse(row['authorized'])
        self.assertFalse(self.g.db.execute("SELECT 1 FROM decision_links WHERE decision_id=? AND kind='how-they-decide'", (row['id'],)).fetchone())


class AuthenticatedSourceLifecycleTests(SharedServer):
    def test_typed_import_pure_signoff_superseded_rejects_mcp_finish_and_preserves_completed_proof(self):
        for method in ('complete', 'complete_json'):
            guard = patch('bridge.llm.Client.' + method, side_effect=AssertionError('Provider calls forbidden'))
            guard.start(); self.addCleanup(guard.stop)
        person = self.post('/api/people', {'name': 'Policy Owner', 'role': 'member'}, token=BOOTSTRAP)
        agent = self.post('/api/tokens', {'person_id': person['id']}, token=BOOTSTRAP)['token']
        human = self.post('/api/tokens', {'person_id': person['id'], 'kind': 'human'}, token=BOOTSTRAP)['token']
        self.store.add_owner({'name': 'Policy Owner', 'team': 'Policy', 'patterns': 'policy/*', 'person_id': person['id']})
        data = {'repo': REPO, 'kind': 'jira', 'provider': 'jira', 'namespace': 'policy.example',
            'external_id': 'POL-104', 'ref': 'POL-104', 'title': 'Retention period',
            'body': 'Decided: retain policy records for thirty days.', 'status': 'Done', 'paths': ['policy/retention.py']}
        imported = self.mcp('bridge_import_record', data, agent)
        self.assertFalse(imported['isError'], imported)
        record = imported['result']['source']
        g = self.store.graph
        row = dict(g.db.execute('SELECT i.*,s.id AS record_id,s.head_id AS source_version_id FROM intents i JOIN source_records s ON s.intent_id=i.id WHERE s.id=?', (record['record_id'],)).fetchone())
        def candidate(title):
            task = self.store.add_run({'title': title, 'repo': REPO})['id']
            did = g.add_decision(task, 'How many days should policy records remain?', 'policy', 'pending', repo=REPO, owner='Policy Owner', path='policy/retention.py')
            g.publish_evidence(did, [row], answer='Thirty days.', status='resolved', source='record', kind='evidence', signoff='required')
            self.assertFalse(self.store.get_decision(did)['authorized'])
            # Pure authenticated human signoff retains the candidate-created pins.
            self.post('/api/decisions/' + did + '/signoff', {'expected_updated_at': self.store.get_decision(did)['updated_at']}, token=human)
            self.assertTrue(self.store.get_decision(did)['authorized'])
            self.assertEqual(g.blocking_nodes(task), [])
            tree = self.mcp('bridge_get_tree', {'task_id': task}, agent)
            self.assertFalse(tree['isError'])
            self.assertEqual(canvas.unread_by_agent(self.store, task), [])
            return task, did
        historical_task, historical = candidate('GH104 completed')
        finished = self.mcp('bridge_finish_task', {'task_id': historical_task, 'checks': 'Synthetic provider-free lifecycle', 'diff': 'diff --git a/policy/retention.py b/policy/retention.py\n+retention = 30\n'}, agent)
        self.assertFalse(finished['isError'], finished)
        saved = self.mcp('bridge_export_proof', {'task_id': historical_task}, agent)['result']['bundle']
        historical_before = self.store.get_decision(historical)
        task, did = candidate('GH105 active')
        changed = self.mcp('bridge_import_record', {**data, 'status': 'Superseded'}, agent)
        self.assertFalse(changed['isError'], changed)
        self.assertIn(did, changed['result']['affected_decisions'])
        tree = self.mcp('bridge_get_tree', {'task_id': task}, agent)
        self.assertFalse(tree['isError'], tree)
        node = next(n for n in canvas._flatten(tree['result']['nodes']) if n['node_id'] == did)
        self.assertFalse(node['authorized'])
        self.assertTrue(node['needs_review'])
        self.assertEqual(canvas.unread_by_agent(self.store, task), [])
        blockers = g.blocking_nodes(task)
        self.assertEqual([n['id'] for n in blockers], [did])
        self.assertIn('POL-104', blockers[0]['why'])
        refused = self.mcp('bridge_finish_task', {'task_id': task}, agent)
        self.assertTrue(refused['isError'], refused)
        self.assertIn(did, refused['result'])
        self.assertIn('POL-104', refused['result'])
        self.assertNotIn('after you last read', refused['result'])
        after = self.store.get_decision(historical)
        for key in ('signatures', 'signed_by', 'signed_revision', 'signed_hash'):
            self.assertEqual(after[key], historical_before[key])
        exported = self.mcp('bridge_export_proof', {'task_id': historical_task}, agent)['result']
        self.assertEqual(exported['bundle'], saved)
        self.assertTrue(exported['integrity']['valid'])
        self.assertTrue(exported['stale'])
