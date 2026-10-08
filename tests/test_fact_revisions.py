"""Synthetic local-only contracts for explicit fact corrections."""
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import OfflineCase
from bridge import canvas, context_memory as cm
from bridge.config import Config
from bridge.fact_revisions import correct_node_facts, revision_token
from bridge.mcp import call_tool, dispatch, TOOLS, READ_ONLY_TOOLS
from bridge.store import Store, Invalid

REPO = 'sample/widget'
QUESTION = 'Should widget queue batching use the bounded window?'


class FactRevisionTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'facts.db')
        self.addCleanup(self.store.graph.close)
        self.graph = self.store.graph
        self.graph.upsert_artifact(REPO, 'src/widget.py')
        self.task = canvas.start_task(self.store, Config(model_api='none'), {
            'title': 'Review widget queue batching', 'goal': 'Choose widget batching behavior',
            'repo': REPO, 'paths': 'src/widget.py'})['task_id']

    def node(self, facts='customer=sample,environment=test', **extra):
        return canvas.add_node(self.store, Config(model_api='none'), {
            'task_id': self.task, 'question': QUESTION, 'paths': 'src/widget.py',
            'client_ref': 'batch-choice', 'facts': facts, **extra})

    def request(self, node, facts='customer=sample,environment=test', **extra):
        return {'task_id': self.task, 'node_id': node['node_id'],
                'expected_revision': node['fact_revision'], 'correction_ref': 'repair-1',
                'facts': facts, 'reason': 'The current ticket explicitly supplies these facts', **extra}

    def rule(self, conditions='repo=sample/widget;customer=sample;environment=test'):
        tid = self.graph.create_task('Earlier widget policy', repo=REPO)
        did = self.graph.add_decision(tid, QUESTION, 'policy', 'approved', source='human',
            answer='Use the bounded window.', answered_by='Reviewer One', path='src/widget.py', repo=REPO)
        self.graph.update_decision(did, signoff='signed', signed_by='Reviewer One')
        self.store.make_rule(did, {'by': 'Reviewer One', 'conditions': conditions, 'scope': 'any'})
        self.graph.set_setting('auto_rules', '1')
        cm.snapshot_decision(self.graph.db, did, reason='synthetic-existing-grant')
        return did

    def test_add_retry_leaves_facts_untouched_and_names_supported_correction(self):
        node = self.node('environment=test')
        repeated = self.node('environment=test')
        self.assertTrue(repeated['repeated'])
        self.assertNotIn('facts_applied', repeated)
        changed = self.node()
        self.assertEqual(changed['node_id'], node['node_id'])
        self.assertEqual(changed['facts'], {'environment': 'test'})
        self.assertFalse(changed['facts_applied'])
        self.assertEqual(changed['facts_correction']['tool'], 'bridge_correct_node_facts')
        self.assertEqual(changed['facts_correction']['expected_revision'], changed['fact_revision'])
        self.assertEqual(self.graph.count_events('node_added', task_id=self.task), 1)

    def test_complete_replacement_does_not_inherit_task_or_historical_business_facts(self):
        self.rule()
        self.graph.db.execute('UPDATE runs SET facts=? WHERE id=?',
                              (json.dumps({'customer': 'sample', 'environment': 'production'}), self.task))
        node = self.node('environment=staging')
        result = correct_node_facts(self.store, self.request(node, 'environment=test'))
        self.assertEqual(result['facts'], {'environment': 'test'})
        self.assertFalse(result['authorized'])
        self.assertIn('customer', result['evidence'])
        cleared = correct_node_facts(self.store, self.request(result, '', correction_ref='clear'))
        self.assertEqual(cleared['facts'], {})
        self.assertFalse(cleared['authorized'])

    def test_matching_current_facts_reevaluate_valid_grant_without_source_signature_copy(self):
        source = self.rule()
        source_before = dict(self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (source,)).fetchone())
        source_versions = cm.history(self.graph.db, source)
        node = self.node('environment=test')
        self.assertFalse(node['authorized'])
        result = correct_node_facts(self.store, self.request(node))
        self.assertTrue(result['authorized'], result)
        self.assertEqual(result['signoff'], 'rule')
        self.assertEqual(result['signatures'], [])
        self.assertEqual(result['facts'], {'customer': 'sample', 'environment': 'test'})
        self.assertEqual(dict(self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (source,)).fetchone()), source_before)
        self.assertEqual(cm.history(self.graph.db, source), source_versions)
        event = json.loads(self.graph.db.execute("SELECT detail FROM events WHERE decision_id=? AND kind='node_facts_corrected'", (node['node_id'],)).fetchone()['detail'])
        self.assertEqual(event['before_facts'], {'environment': 'test'})
        self.assertEqual(event['after_facts'], result['facts'])
        self.assertNotEqual(event['before_version_id'], event['after_version_id'])
        self.assertTrue(result['facts_correction']['is_current'])

    def test_rule_recheck_respects_auto_rules_disabled_and_conflicting_repository(self):
        self.rule()
        self.graph.set_setting('auto_rules', '0')
        node = self.node('environment=test')
        result = correct_node_facts(self.store, self.request(node))
        self.assertFalse(result['authorized'])
        self.assertEqual(result['signoff'], 'required')
        before = revision_token(self.graph.db, node['node_id'])
        with self.assertRaisesRegex(Invalid, 'repo'):
            correct_node_facts(self.store, self.request(result, 'repo=other/widget', correction_ref='conflict'))
        self.assertEqual(revision_token(self.graph.db, node['node_id']), before)

    def test_exact_retry_receipt_is_idempotent_and_changed_key_payload_is_rejected(self):
        node = self.node('environment=staging')
        request = self.request(node)
        result = correct_node_facts(self.store, request)
        versions = cm.history(self.graph.db, node['node_id'])
        events_before = self.graph.db.execute('SELECT count(*) n FROM events').fetchone()['n']
        with patch('bridge.ladder.run_task', side_effect=AssertionError('receipt retry must not reevaluate')), \
                patch.object(self.store, 'notify', side_effect=AssertionError('receipt retry must not notify')):
            repeated = correct_node_facts(self.store, request)
        self.assertEqual(self.graph.db.execute('SELECT count(*) n FROM events').fetchone()['n'], events_before)
        self.assertTrue(repeated['facts_correction']['repeated'])
        self.assertEqual(repeated['facts_correction']['after_version_id'], result['facts_correction']['after_version_id'])
        self.assertEqual(cm.history(self.graph.db, node['node_id']), versions)
        self.assertEqual(self.graph.count_events('node_facts_corrected', task_id=self.task), 1)
        with self.assertRaisesRegex(Invalid, 'correction_ref'):
            correct_node_facts(self.store, {**request, 'facts': 'environment=other'})
        newer = correct_node_facts(self.store, self.request(result, 'environment=other', correction_ref='repair-2'))
        stale_receipt = correct_node_facts(self.store, request)
        self.assertFalse(stale_receipt['facts_correction']['is_current'])
        self.assertEqual(stale_receipt['facts'], newer['facts'])

    def test_exact_receipt_retry_after_rule_ended_separates_historical_and_live_authority(self):
        source = self.rule()
        node = self.node('environment=stage')
        request = self.request(node)
        corrected = correct_node_facts(self.store, request)
        self.assertTrue(corrected['authorized'])
        self.store.make_rule(source, {'by': 'Reviewer One', 'end': True})
        before_events = self.graph.db.execute('SELECT count(*) n FROM events').fetchone()['n']
        with patch('bridge.ladder.run_task', side_effect=AssertionError('receipt must not reevaluate')), \
                patch.object(self.store, 'notify', side_effect=AssertionError('receipt must not notify')):
            replayed = correct_node_facts(self.store, request)
        self.assertFalse(replayed['authorized'])
        self.assertTrue(replayed['facts_correction']['applied_authorized'])
        self.assertEqual(replayed['facts_correction']['applied_signoff'], 'rule')
        self.assertFalse(replayed['facts_correction']['is_current'])
        self.assertEqual(replayed['facts_correction']['after_version_id'], corrected['facts_correction']['after_version_id'])
        self.assertEqual(self.graph.db.execute('SELECT count(*) n FROM events').fetchone()['n'], before_events)
        self.assertEqual(self.graph.count_events('node_facts_corrected', task_id=self.task), 1)

    def test_optimistic_token_detects_equal_timestamp_material_change_and_event_aba(self):
        node = self.node()
        request = self.request(node)
        self.graph.db.execute('UPDATE decisions SET facts=? WHERE id=?', ('{"environment":"changed"}', node['node_id']))
        with self.assertRaisesRegex(Invalid, 'expected_revision is stale'):
            correct_node_facts(self.store, request)
        self.graph.db.execute('UPDATE decisions SET facts=? WHERE id=?', (json.dumps(node['facts'], sort_keys=True), node['node_id']))
        self.graph.append_event('node_settled', {'task_id': self.task, 'decision_id': node['node_id']})
        with self.assertRaisesRegex(Invalid, 'expected_revision is stale'):
            correct_node_facts(self.store, request)
        current = canvas.node_view(self.store, node['node_id'])
        with patch('bridge.fact_revisions.now', return_value=current['updated_at']):
            result = correct_node_facts(self.store, self.request(current, 'environment=new'))
        self.assertGreater(result['updated_at'], current['updated_at'])
        self.assertNotEqual(result['fact_revision'], current['fact_revision'])

    def test_notification_is_not_human_action_but_actual_reply_is(self):
        node = self.node()
        self.graph.append_event('notification_queued', {'task_id': self.task, 'decision_id': node['node_id']})
        node = canvas.node_view(self.store, node['node_id'])
        result = correct_node_facts(self.store, self.request(node, 'environment=stage'))
        self.graph.append_event('reply_received', {'task_id': self.task, 'decision_id': node['node_id'], 'by': 'Reviewer One'})
        current = canvas.node_view(self.store, node['node_id'])
        with self.assertRaisesRegex(Invalid, 'person has already acted'):
            correct_node_facts(self.store, self.request(current, correction_ref='reply-race'))
        self.assertEqual(canvas.node_view(self.store, node['node_id'])['facts'], result['facts'])

    def test_signed_rule_authorized_human_origin_and_completed_nodes_are_not_editable(self):
        node = self.node()
        for changes in ({'signoff': 'signed'}, {'signoff': 'rule'}, {'status': 'approved'},
                        {'origin': 'human'}, {'signatures': '[{"by":"Reviewer One"}]'}, {'reusable': 1}):
            with self.subTest(changes=changes):
                old = dict(self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (node['node_id'],)).fetchone())
                self.graph.db.execute('UPDATE decisions SET '+','.join(k+'=?' for k in changes)+' WHERE id=?', (*changes.values(), node['node_id']))
                current = canvas.node_view(self.store, node['node_id'])
                with self.assertRaisesRegex(Invalid, 'unsigned agent-origin'):
                    correct_node_facts(self.store, self.request(current))
                self.graph.db.execute('UPDATE decisions SET '+','.join(k+'=?' for k in changes)+' WHERE id=?', (*[old[k] for k in changes], node['node_id']))
        self.graph.db.execute("UPDATE runs SET status='completed' WHERE id=?", (self.task,))
        with self.assertRaisesRegex(Invalid, 'active task'):
            correct_node_facts(self.store, self.request(canvas.node_view(self.store, node['node_id'])))

    def test_rollback_removes_scratch_snapshots_events_and_live_changes(self):
        node = self.node('environment=staging')
        before = self.graph.db.execute('SELECT count(*) n FROM decisions').fetchone()['n']
        token = node['fact_revision']
        with patch.object(self.graph, 'flag_dependents', side_effect=RuntimeError('synthetic publication failure')):
            with self.assertRaisesRegex(RuntimeError, 'publication failure'):
                correct_node_facts(self.store, self.request(node))
        self.assertEqual(revision_token(self.graph.db, node['node_id']), token)
        self.assertEqual(self.graph.db.execute('SELECT count(*) n FROM decisions').fetchone()['n'], before)
        self.assertEqual(self.graph.count_events('node_facts_corrected', task_id=self.task), 0)
        self.assertEqual(cm.history(self.graph.db, node['node_id']), [])

    def test_dependency_invalidation_and_old_tree_readback_remain_stale(self):
        node = self.node()
        child = self.graph.add_decision(self.task, 'What batch metrics should we record?', 'ops', 'resolved',
            source='agent', answer='Record batch size.', repo=REPO)
        self.graph.add_link(child, node['node_id'], 'depends', 'Dependent on the selected batching behavior')
        tree = canvas.get_tree(self.store, self.task)
        node = canvas.node_view(self.store, node['node_id'])
        result = correct_node_facts(self.store, self.request(node, 'environment=new'))
        self.assertTrue(canvas.node_view(self.store, child)['needs_review'])
        canvas.note_agent_read(self.store, self.task, tree['observed_at'], tree['observed_revision'], {node['node_id'], child})
        self.assertIn(node['node_id'], [r['node_id'] for r in canvas.unread_by_agent(self.store, self.task)])
        changed = canvas.wait(self.store, {'task_id': self.task, 'since': tree['observed_at'], 'timeout': '0'})
        self.assertIn(node['node_id'], [r['node_id'] for r in changed['changed']])
        self.assertEqual(result['dependents'], [child])

    def test_old_background_result_cannot_restore_prior_facts_or_authority(self):
        node = self.node('environment=stage')
        self.graph.db.execute('UPDATE decisions SET model_pending=1 WHERE id=?', (node['node_id'],))
        scheduled = node['updated_at']
        current = canvas.node_view(self.store, node['node_id'])
        result = correct_node_facts(self.store, self.request(current))
        with patch('bridge.ladder.ask', side_effect=AssertionError('old worker must not evaluate')):
            canvas._model_node_pass(self.store, Config(model_api='none'), node['node_id'], '',
                ['src/widget.py'], [], '', '', '', scheduled)
        after = canvas.node_view(self.store, node['node_id'])
        self.assertEqual(after['facts'], result['facts'])
        self.assertFalse(after['model_pending'])
        self.assertEqual(after['updated_at'], result['updated_at'])

    def test_retains_exact_old_source_pin_and_stale_review_blocks_new_rule_authority(self):
        self.rule()
        imported = self.store.add_record({'repo': REPO, 'kind': 'ticket', 'ref': 'CASE-1',
            'title': 'Current widget constraints', 'body': 'The queue is bounded.'})
        node = self.node('environment=stage')
        pin = cm.pin(imported['source'])
        with self.graph.transaction() as db:
            cm.attach(db, node['node_id'], [pin])
        before = cm.history(self.graph.db, node['node_id'])
        self.store.add_record({'repo': REPO, 'kind': 'ticket', 'ref': 'CASE-1',
            'title': 'Current widget constraints', 'body': 'The queue constraint changed.'})
        current = canvas.node_view(self.store, node['node_id'])
        self.assertTrue(current['needs_review'])
        result = correct_node_facts(self.store, self.request(current))
        retained = cm.edges(self.graph.db, node['node_id'])
        self.assertTrue(any(e['source_version_id'] == pin['source_version_id'] and e['stale'] for e in retained))
        self.assertFalse(result['authorized'])
        self.assertTrue(result['needs_review'])
        after = cm.history(self.graph.db, node['node_id'])
        self.assertEqual(after[:len(before)], before)

    def test_legacy_source_revision_is_not_silently_replaced_with_current_rule_authority(self):
        self.rule()
        source_task = self.graph.create_task('Legacy source task', repo=REPO)
        source = self.graph.add_decision(source_task, 'Earlier queue requirement', 'policy', 'approved',
            source='human', answer='Earlier requirement.', answered_by='Reviewer Two', repo=REPO)
        node = self.node('environment=stage')
        self.graph.update_decision(node['node_id'], status='resolved', source='memory',
            answer='Earlier requirement.', source_id=source, source_revision='2020-01-01T00:00:00+00:00')
        current = canvas.node_view(self.store, node['node_id'])
        result = correct_node_facts(self.store, self.request(current))
        self.assertFalse(result['authorized'])
        self.assertTrue(result['needs_review'])
        self.assertIsNotNone(self.graph.db.execute("SELECT 1 FROM decision_links WHERE decision_id=? AND related_id=? AND kind='derived'",
                                                  (node['node_id'], source)).fetchone())

    def test_actual_inflight_background_result_loses_to_explicit_correction(self):
        node = self.node('environment=stage')
        self.graph.db.execute('UPDATE decisions SET model_pending=1 WHERE id=?', (node['node_id'],))
        scheduled = node['updated_at']
        started, release = threading.Event(), threading.Event()
        errors = []
        def old_read(*args, **kwargs):
            did = self.graph.add_decision(self.task, QUESTION, 'policy', 'resolved', source='agent',
                answer='Obsolete background answer', repo=REPO, draft=True)
            started.set()
            if not release.wait(5):
                raise RuntimeError('Synthetic background race did not release')
            return {'id': did, 'drafts': [did]}
        def worker():
            try:
                canvas._model_node_pass(self.store, Config(model_api='none'), node['node_id'], '',
                    ['src/widget.py'], [], '', '', '', scheduled)
            except Exception as error:
                errors.append(error)
            finally:
                self.graph.close_thread()
        with patch('bridge.ladder.ask', side_effect=old_read):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(started.wait(5))
                current = canvas.node_view(self.store, node['node_id'])
                result = correct_node_facts(self.store, self.request(current))
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        after = canvas.node_view(self.store, node['node_id'])
        self.assertEqual(after['facts'], result['facts'])
        self.assertNotEqual(after['answer'], 'Obsolete background answer')
        self.assertEqual(after['updated_at'], result['updated_at'])
        self.assertFalse(after['model_pending'])
        self.assertEqual(self.graph.db.execute('SELECT count(*) n FROM decisions WHERE draft=1').fetchone()['n'], 0)

    def test_concurrent_corrections_with_one_revision_allow_only_one_commit(self):
        node = self.node('environment=stage')
        start = threading.Barrier(3)
        results, failures = [], []
        def worker(label):
            try:
                start.wait(5)
                results.append(correct_node_facts(self.store, self.request(node,
                    'environment=' + label, correction_ref=label)))
            except Invalid as error:
                failures.append(str(error))
            finally:
                self.graph.close_thread()
        threads = [threading.Thread(target=worker, args=(label,)) for label in ('first', 'second')]
        for thread in threads:
            thread.start()
        start.wait(5)
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(len(failures), 1)
        self.assertIn('expected_revision is stale', failures[0])
        self.assertEqual(self.graph.count_events('node_facts_corrected', task_id=self.task), 1)

    def test_repository_contradiction_is_rejected_before_new_task_or_node_publication(self):
        tasks_before = self.graph.db.execute('SELECT count(*) n FROM runs').fetchone()['n']
        with self.assertRaisesRegex(Invalid, 'repo'):
            canvas.start_task(self.store, Config(model_api='none'), {
                'title': 'Other queue change', 'repo': REPO, 'facts': 'repo=other/widget'})
        self.assertEqual(self.graph.db.execute('SELECT count(*) n FROM runs').fetchone()['n'], tasks_before)
        with self.assertRaisesRegex(Invalid, 'repo'):
            self.node('repo=other/widget')
        self.assertEqual(self.graph.db.execute('SELECT count(*) n FROM decisions').fetchone()['n'], 0)
        with self.assertRaisesRegex(Invalid, 'repo'):
            canvas.start_task(self.store, Config(model_api='none'), {
                'task_id': self.task, 'facts': 'repo=other/widget'})
        self.assertEqual(json.loads(self.graph.get_task(self.task)['facts'] or '{}'), {})

    def test_historically_authorized_then_invalidated_consumer_still_requires_human_correction(self):
        source = self.rule()
        node = self.node()
        self.assertTrue(node['authorized'])
        cm.snapshot_decision(self.graph.db, node['node_id'], reason='synthetic-authorized-consumer')
        self.graph.invalidate_rule_dependents(source, 'Synthetic rule invalidation')
        current = canvas.node_view(self.store, node['node_id'])
        self.assertFalse(current['authorized'])
        with self.assertRaisesRegex(Invalid, 'historical signatures or standing authority'):
            correct_node_facts(self.store, self.request(current, 'environment=other'))

    def test_read_response_cannot_pair_old_facts_with_a_newer_usable_revision(self):
        node = self.node('environment=stage')
        original = cm.source_revalidation
        def change_while_reading(db, decision_id):
            response = original(db, decision_id)
            self.graph.db.execute('UPDATE decisions SET facts=? WHERE id=?',
                                  ('{"environment":"new"}', decision_id))
            return response
        with patch('bridge.context_memory.source_revalidation', side_effect=change_while_reading):
            stale = self.store.get_decision(node['node_id'])
        self.assertEqual(json.loads(stale['facts']), {'environment': 'stage'})
        with self.assertRaisesRegex(Invalid, 'expected_revision is stale'):
            correct_node_facts(self.store, self.request(node, expected_revision=stale['fact_revision']))

    def test_malformed_replacement_never_silently_clears_existing_facts(self):
        node = self.node()
        for facts in ('customer', '{broken', 'customer=sample,customer=other', 'environment=',
                      '{"environment": ["test"]}', None):
            with self.subTest(facts=facts), self.assertRaises(Invalid):
                correct_node_facts(self.store, self.request(node, facts))
            self.assertEqual(revision_token(self.graph.db, node['node_id']), node['fact_revision'])

    def test_mcp_requires_opaque_revision_and_remains_write_restricted(self):
        schema = next(t['inputSchema'] for t in TOOLS if t['name'] == 'bridge_correct_node_facts')
        self.assertIn('expected_revision', schema['required'])
        self.assertNotIn('bridge_correct_node_facts', READ_ONLY_TOOLS)
        node = self.node('environment=stage')
        current = call_tool(self.store, 'bridge_get_decision', {'decision_id': node['node_id']})
        self.assertEqual(current['fact_revision'], node['fact_revision'])
        result = call_tool(self.store, 'bridge_correct_node_facts', self.request(node))
        self.assertTrue(result['facts_correction']['applied'])


from test_auth import SharedServer, BOOTSTRAP


class FactRevisionProtocolTests(SharedServer):
    """The shipped HTTP MCP discovery/dispatch and ordinary agent write gate."""
    def test_remote_agent_discovers_and_invokes_fact_correction_while_viewer_is_read_only(self):
        person = self.post('/api/people', {'name': 'Sample Builder', 'role': 'member'}, token=BOOTSTRAP)
        agent = self.post('/api/tokens', {'person_id': person['id']}, token=BOOTSTRAP)['token']
        viewer = self.post('/api/people', {'name': 'Sample Reader', 'role': 'viewer'}, token=BOOTSTRAP)
        reader = self.post('/api/tokens', {'person_id': viewer['id']}, token=BOOTSTRAP)['token']
        listed = self.post('/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, token=agent)
        tool = next(t for t in listed['result']['tools'] if t['name'] == 'bridge_correct_node_facts')
        self.assertIn('expected_revision', tool['inputSchema']['required'])
        task = self.mcp('bridge_start_task', {'title': 'Choose widget batching', 'repo': REPO}, agent)['result']
        node = self.mcp('bridge_add_node', {'task_id': task['task_id'], 'question': QUESTION,
            'client_ref': 'choice', 'facts': 'environment=stage'}, agent)['result']
        request = {'task_id': task['task_id'], 'node_id': node['node_id'],
            'expected_revision': node['fact_revision'], 'correction_ref': 'fixed-facts',
            'facts': 'environment=test', 'reason': 'The current task says test environment'}
        blocked = self.status_of('POST', '/mcp', {'jsonrpc': '2.0', 'id': 2,
            'method': 'tools/call', 'params': {'name': 'bridge_correct_node_facts', 'arguments': request}}, token=reader)
        self.assertEqual(blocked, 403)
        corrected = self.mcp('bridge_correct_node_facts', request, agent)
        self.assertFalse(corrected['isError'], corrected)
        self.assertEqual(corrected['result']['facts'], {'environment': 'test'})
        self.assertFalse(corrected['result']['authorized'])
        repeated = self.mcp('bridge_correct_node_facts', request, agent)
        self.assertFalse(repeated['isError'], repeated)
        self.assertTrue(repeated['result']['facts_correction']['repeated'])
        self.assertEqual(self.store.graph.count_events('node_facts_corrected', task_id=task['task_id']), 1)

    def test_stdio_dispatch_lists_and_validates_the_same_correction_contract(self):
        listed = dispatch(self.store, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.assertIn('bridge_correct_node_facts', [t['name'] for t in listed['result']['tools']])
        invalid = dispatch(self.store, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
            'params': {'name': 'bridge_correct_node_facts', 'arguments': {'task_id': 'unknown'}}})
        self.assertTrue(invalid['result']['isError'])
        self.assertIn('Missing required argument', invalid['result']['content'][0]['text'])


if __name__ == '__main__':
    unittest.main()
