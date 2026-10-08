"""Historical discovery stays readable without granting present permission.

All records are disposable synthetic fixtures; no providers or real graphs.
"""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import canvas, context_memory as cm, decision_reads
from bridge.config import Config
from bridge.mcp import TOOLS, call_tool, dispatch
from bridge.store import Invalid, Store
from test_auth import BOOTSTRAP, SharedServer

REPO = 'synthetic/history-read'
QUESTION = 'What is the diagnostic retention policy?'


class DecisionReadTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'history.db')
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.store.add_owner({'name': 'Historical Review Owner', 'team': 'Synthetic', 'patterns': 'policy/*'})
        self.task = self.store.add_run({'title': 'Historical source review', 'repo': REPO})['id']

    def signed(self, answer='Retain quartz diagnostic records for fourteen days.', rationale='Saved human retention decision.'):
        node = self.graph.add_decision(self.task, QUESTION, 'policy', 'pending',
                                      owner='Historical Review Owner', repo=REPO, path='policy/retention.py')
        self.store.answer(node, {'answer': answer, 'rationale': rationale,
                                'signed_by': 'Historical Review Owner',
                                'expected_updated_at': self.store.get_decision(node)['updated_at']})
        return node

    def invalidate(self, node, count=1):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for i in range(count):
                db.execute("UPDATE decisions SET needs_review=1,signoff='required',review_reason=? WHERE id=?",
                           ('Synthetic source invalidation ' + str(i), node))
                cm.snapshot_decision(db, node, reason='synthetic-invalidation')

    def frozen(self):
        tables = ('runs', 'decisions', 'decision_versions', 'decision_source_edges', 'decision_links',
                  'events', 'source_records', 'source_versions', 'task_source_anchors', 'authority', 'settings')
        return {table: sorted(json.dumps(dict(row), sort_keys=True, default=str)
                              for row in self.graph.db.execute('SELECT * FROM ' + table)) for table in tables}

    def read(self, node, **args):
        result = dispatch(self.store, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                          'params': {'name': 'bridge_get_decision', 'arguments': {'decision_id': node, **args}}})
        self.assertFalse(result['result']['isError'], result)
        if not args.get('version_id') and args.get('detail') != 'full':
            self.assertLessEqual(len(json.dumps(result['result']['content'][0]['text']).encode()),
                                 decision_reads.MAX_CONTENT_BYTES)
        return json.loads(result['result']['content'][0]['text'])

    def test_old_arguments_current_fields_and_explicit_full_body(self):
        node = self.signed()
        full = self.store.get_decision(node)
        compact = self.read(node)
        for key in ('id', 'answer', 'rationale', 'status', 'signoff', 'authorized', 'approval_pending', 'fact_revision'):
            self.assertEqual(compact[key], full[key], key)
        self.assertEqual(self.read(node, detail='full'), full)
        self.assertEqual(self.store.get_decision(node), full)
        self.assertIn('events', compact['projection']['omitted_fields'])
        self.assertIn('snapshot', full['context_history'][0])
        self.assertNotIn('snapshot', compact['context_history'][0])

    def test_old_human_approval_is_visible_after_105_invalidation_snapshots(self):
        node = self.signed()
        signed_versions = {row['id'] for row in self.store.get_decision(node)['context_history']
                           if json.loads(row['snapshot'])['decision']['signoff'] == 'signed'}
        self.assertTrue(signed_versions)
        self.invalidate(node, 105)
        # Generic event retention is irrelevant to immutable approval history.
        self.graph.db.execute('DELETE FROM events WHERE decision_id=?', (node,))
        before = self.frozen()
        full = self.store.get_decision(node)
        self.assertGreater(len(json.dumps(full)), 64000)
        with patch.object(cm, 'history', side_effect=AssertionError('Do not fetch all historical bodies')):
            compact = self.read(node)
        self.assertFalse(compact['authorized'])
        self.assertTrue(compact['approval_pending'])
        self.assertTrue(signed_versions & {row['id'] for row in compact['historical_approvals']})
        self.assertGreater(compact['pagination']['collections']['context_history']['total'], 100)
        self.assertTrue(compact['pagination']['has_more'])
        saved = self.read(node, version_id=next(iter(signed_versions)))
        self.assertEqual(saved['historical_snapshot']['decision']['signoff'], 'signed')
        self.assertFalse(saved['current_authorization']['authorized'])
        self.assertEqual(self.frozen(), before)

    def test_saved_rule_use_has_its_own_index_after_invalidation(self):
        supplier = self.signed()
        self.store.update_settings({'auto_rules': True})
        self.store.make_rule(supplier, {'by': 'Historical Review Owner', 'scope': 'any',
                                      'expected_updated_at': self.store.get_decision(supplier)['updated_at']})
        task = self.store.add_run({'title': 'Consumer source review', 'repo': REPO})['id']
        result = canvas.add_node(self.store, Config(model_api='none'), {'task_id': task, 'question': QUESTION,
                                 'paths': 'policy/retention.py', 'context': 'Synthetic later use'})
        node = result['node_id']
        self.assertEqual(result['signoff'], 'rule')
        self.invalidate(node, 105)
        compact = self.read(node)
        uses = [r for r in compact['historical_approvals'] if r['signoff_preview'] == 'rule']
        self.assertTrue(uses)
        saved = self.read(node, version_id=uses[0]['id'])
        self.assertEqual(saved['historical_snapshot']['decision']['source_id'], supplier)
        self.assertEqual(saved['historical_snapshot']['decision']['signoff'], 'rule')
        self.assertFalse(saved['current_authorization']['authorized'])

    def test_independent_pagination_is_complete_and_stable(self):
        node = self.signed()
        self.invalidate(node, 13)
        args, seen = {'limit': 3}, {key: [] for key in decision_reads.HISTORY_COLLECTIONS}
        before = self.frozen()
        while True:
            page = self.read(node, **args)
            self.assertEqual(page, self.read(node, **args))
            for key in seen:
                self.assertEqual(page['pagination']['collections'][key]['offset'], len(seen[key]))
                seen[key].extend(row['id'] for row in page[key])
            if not page['pagination']['has_more']:
                break
            args['cursor'] = page['pagination']['next_cursor']
        self.assertEqual(len(seen['context_history']), len(self.store.get_decision(node)['context_history']))
        for key in seen:
            self.assertEqual(len(seen[key]), len(set(seen[key])))
        self.assertEqual(before, self.frozen())

    def test_equal_timestamp_changes_invalidate_cursor(self):
        node = self.signed()
        self.invalidate(node, 3)
        first = self.read(node, limit=1)
        stamp = self.store.get_decision(node)['updated_at']
        self.invalidate(node, 1)
        self.assertEqual(stamp, self.store.get_decision(node)['updated_at'])
        with self.assertRaisesRegex(Invalid, 'results changed'):
            decision_reads.get_decision(self.store, {'decision_id': node, 'limit': 1, 'cursor': first['pagination']['next_cursor']})
        first = self.read(node, limit=1)
        self.graph.db.execute('UPDATE decisions SET answer=? WHERE id=?', ('Changed at the same timestamp', node))
        with self.assertRaisesRegex(Invalid, 'results changed'):
            decision_reads.get_decision(self.store, {'decision_id': node, 'limit': 1, 'cursor': first['pagination']['next_cursor']})

    def test_concurrent_writer_cannot_mix_current_and_history(self):
        if getattr(self.graph.db, 'dialect', '') != 'postgres':
            self.graph.db.execute('PRAGMA journal_mode=WAL')
        node = self.signed()
        original = decision_reads._snapshot
        def concurrent(db, current):
            self.invalidate(node)
            return original(db, current)
        with patch.object(decision_reads, '_snapshot', side_effect=concurrent):
            compact = self.read(node)
        self.assertTrue(compact['authorized'])
        self.assertTrue(all(row['needs_review_preview'] == '0' for row in compact['context_history']))
        self.assertFalse(self.read(node)['authorized'])

    def test_oversized_current_approval_fields_are_explicitly_omitted(self):
        node = self.signed()
        large = 'Complete synthetic approval evidence. ' * 4000
        self.graph.db.execute('UPDATE decisions SET rationale=? WHERE id=?', (large, node))
        compact = self.read(node)
        self.assertNotIn('rationale', compact)
        self.assertIn('rationale_preview', compact)
        self.assertEqual(compact['projection']['omitted_fields']['rationale']['read'],
                         {'decision_id': node, 'detail': 'full'})
        self.assertEqual(self.read(node, detail='full')['rationale'], large)

    def test_valid_large_unicode_signed_revision_keeps_compact_history_readable(self):
        answer, rationale = '雪' * 12000, ('字"\\\n' * 3000).strip()
        node = self.signed(answer=answer, rationale=rationale)
        full = self.store.get_decision(node)
        self.assertEqual(full['revision']['answer'], answer)
        self.assertEqual(full['revision']['rationale'], rationale)
        self.assertGreater(decision_reads._size(full['revision']), decision_reads.MAX_CONTENT_BYTES)
        compact = self.read(node)
        self.assertTrue(compact['historical_approvals'])
        self.assertNotIn('revision', compact)
        self.assertEqual(compact['saved_revision']['id'], full['revision']['id'])
        self.assertEqual(compact['saved_revision']['identifier_kind'], 'decision_revisions')
        self.assertEqual(compact['projection']['omitted_fields']['revision']['read'],
                         {'decision_id': node, 'detail': 'full'})
        self.assertEqual(self.read(node, detail='full'), full)
        exact = self.read(node, version_id=compact['historical_approvals'][0]['id'])
        self.assertEqual(exact['historical_snapshot']['decision']['answer'], answer)
        self.assertEqual(exact['historical_snapshot']['decision']['rationale'], rationale)
        self.assertNotIn('revision', exact['current_authorization'])
        self.assertNotIn('answer', exact['current_authorization'])
        self.assertNotIn('rationale', exact['current_authorization'])

    def test_broad_unicode_current_and_history_fit_with_small_saved_revision(self):
        node = self.signed()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE decisions SET question=?,answer=?,rationale=?,context=? WHERE id=?',
                       ('😀' * 1900, '😀' * 11900, '😀' * 11900, '😀' * 11900, node))
            version = cm.snapshot_decision(db, node, reason='synthetic-unicode-preview')
        full = self.store.get_decision(node)
        self.assertLess(decision_reads._size(full['revision']), 1000)
        before = self.frozen()
        compact = self.read(node)
        self.assertEqual(compact['authorized'], full['authorized'])
        for key in decision_reads.HISTORY_COLLECTIONS:
            self.assertTrue(compact[key])
            self.assertEqual(compact[key][0]['id'], version)
            self.assertEqual(compact[key][0]['read'], {'decision_id': node, 'version_id': version})
            self.assertEqual(compact[key][0]['answer_characters'], 11900)
        self.assertTrue(any(len(row['answer_preview']) < 300 or len(row['question_preview']) < 300
                            for key in decision_reads.HISTORY_COLLECTIONS for row in compact[key]))
        for key in ('answer', 'rationale', 'context'):
            self.assertNotIn(key, compact)
            self.assertEqual(compact['projection']['omitted_fields'][key]['characters'], len(full[key]))
            self.assertEqual(compact['projection']['omitted_fields'][key]['read'],
                             {'decision_id': node, 'detail': 'full'})
        self.assertEqual(self.read(node, detail='full'), full)
        exact = self.read(node, version_id=version)['historical_snapshot']['decision']
        for key in ('question', 'answer', 'rationale', 'context'):
            self.assertEqual(exact[key], full[key])
        seen = {key: [row['id'] for row in compact[key]] for key in decision_reads.HISTORY_COLLECTIONS}
        while compact['pagination']['has_more']:
            compact = self.read(node, cursor=compact['pagination']['next_cursor'])
            for key in seen:
                seen[key].extend(row['id'] for row in compact[key])
        for key in seen:
            self.assertEqual(len(seen[key]), len(set(seen[key])))
            self.assertEqual(len(seen[key]), compact['pagination']['collections'][key]['total'])
        self.assertEqual(before, self.frozen())

    def test_history_search_finds_invalidated_withdrawn_superseded_and_old_text(self):
        node = self.signed()
        self.invalidate(node)
        for status, superseded in (('resolved', ''), ('withdrawn', ''), ('resolved', 'synthetic-replacement')):
            self.graph.db.execute('UPDATE decisions SET status=?,superseded_by=?,answer=? WHERE id=?',
                                  (status, superseded, 'Current value differs.', node))
            self.assertNotIn(node, {r['id'] for r in call_tool(self.store, 'bridge_search_decisions',
                             {'query': 'quartz', 'repo': REPO})['matches']})
            before = self.frozen()
            found = call_tool(self.store, 'bridge_search_decisions', {'query': 'quartz', 'repo': REPO, 'mode': 'history'})
            row = next(r for r in found['matches'] if r['id'] == node)
            self.assertTrue(row['matched_version_id'])
            self.assertEqual(row['current_state']['status'], status)
            self.assertEqual(row['request_applicability'], 'not_evaluated_by_this_read')
            self.assertNotIn('authorized', row)
            self.assertEqual(before, self.frozen())

    def test_history_search_pages_and_rejects_changed_same_timestamp_results(self):
        nodes = [self.signed() for _ in range(4)]
        args = {'query': 'quartz', 'repo': REPO, 'mode': 'history', 'limit': 2}
        first = decision_reads.search(self.store, args)
        self.assertEqual(first['pagination']['total'], 4)
        second = decision_reads.search(self.store, {**args, 'cursor': first['pagination']['next_cursor']})
        self.assertFalse(second['pagination']['has_more'])
        self.assertEqual({r['id'] for r in first['matches'] + second['matches']}, set(nodes))
        self.invalidate(nodes[0])
        with self.assertRaisesRegex(Invalid, 'results changed'):
            decision_reads.search(self.store, {**args, 'cursor': first['pagination']['next_cursor']})

    def test_optional_schema_and_argument_validation(self):
        node = self.signed()
        other = self.signed()
        version = self.store.get_decision(other)['context_history'][0]['id']
        bad = ({'detail': 'unknown'}, {'limit': 0}, {'limit': True}, {'cursor': '!'},
               {'version_id': version}, {'version_id': 'signed-revision'}, {'version_id': ''},
               {'version_id': version, 'limit': 1}, {'detail': 'full', 'cursor': 'x'})
        for extra in bad:
            with self.subTest(extra=extra), self.assertRaises(Invalid):
                call_tool(self.store, 'bridge_get_decision', {'decision_id': node, **extra})
        for name in ('bridge_get_decision', 'bridge_search_decisions'):
            schema = next(tool['inputSchema'] for tool in TOOLS if tool['name'] == name)
            self.assertEqual(schema['required'], ['decision_id'] if name == 'bridge_get_decision' else ['query'])
        with self.assertRaises(Invalid):
            call_tool(self.store, 'bridge_search_decisions', {'query': 'x', 'mode': 'unknown'})

    def test_history_search_calls_unchanged_visibility_helper(self):
        self.signed()
        with patch.object(self.graph, '_memory_scope_sql', return_value=('d.id=?', ['not-visible'])) as helper:
            found = decision_reads.search(self.store, {'query': 'quartz', 'repo': REPO, 'mode': 'history'})
        helper.assert_called_once_with()
        self.assertEqual(found['matches'], [])
        self.assertEqual(found['pagination']['total'], 0)


class DecisionReadHTTPTests(SharedServer):
    def test_human_http_keeps_full_review_and_mcp_full_is_legacy_body(self):
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.store.graph.close)
        task = self.store.add_run({'title': 'Complete human review', 'repo': REPO})['id']
        node = self.store.graph.add_decision(task, QUESTION, 'policy', 'pending', repo=REPO)
        with self.store.connect() as db:
            cm.snapshot_decision(db, node)
        full = self.store.get_decision(node)
        human = self.get('/api/decisions/' + node, token=BOOTSTRAP)
        for key, value in full.items():
            self.assertEqual(human[key], value, key)
        self.assertIn('snapshot', human['context_history'][0])
        self.assertEqual(call_tool(self.store, 'bridge_get_decision', {'decision_id': node, 'detail': 'full'}), full)
        summary = call_tool(self.store, 'bridge_get_decision', {'decision_id': node})
        self.assertTrue(summary['context_history'][0]['snapshot_omitted'])


class DecisionProjectionBudgetTests(unittest.TestCase):
    def test_complete_budget_includes_omission_metadata_and_adapts_previews(self):
        out = {'id': 'synthetic-decision', 'authorized': False, 'approval_pending': True,
               'answer_preview': '😀' * 200}
        omitted = {f'field_{i}': {'characters': 12000,
                   'read': {'decision_id': 'synthetic-decision', 'detail': 'full'}} for i in range(200)}
        row = {'id': 'synthetic-version', 'snapshot_omitted': True,
               'answer_preview': '😀' * 300, 'answer_characters': 12000,
               'read': {'decision_id': 'synthetic-decision', 'version_id': 'synthetic-version'}}
        pages = {'context_history': [dict(row)], 'historical_approvals': [dict(row)]}
        def response():
            return {**out, **pages, 'projection': {'omitted_fields': omitted}}
        self.assertGreater(decision_reads._size(response()), decision_reads.MAX_CONTENT_BYTES)
        self.assertTrue(decision_reads._fit_response(response, out, omitted, pages, 'synthetic-decision'))
        self.assertLessEqual(decision_reads._size(response()), decision_reads.MAX_CONTENT_BYTES)
        self.assertFalse(out['authorized'])
        self.assertTrue(out['approval_pending'])
        self.assertEqual(len(omitted), 200)
        self.assertTrue(any(len(page[0]['answer_preview']) < 300 for page in pages.values()))
        for page in pages.values():
            self.assertEqual(page[0]['answer_characters'], 12000)
            self.assertEqual(page[0]['read'], row['read'])
