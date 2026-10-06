"""Provider-free scope/eligibility regressions for bounded decision retrieval.

Unsigned synthetic notes exercise recall, not permission to reuse an answer.
The same tests are registered in test_postgres for its isolated real-PG run.
"""
import json
from pathlib import Path
from unittest.mock import Mock, patch

from fixtures import OfflineCase
from bridge import graph as graph_mod, llm
from bridge.store import Store

REPO = 'synthetic/solaris'
OTHER_REPO = 'other/solaris'
QUESTION = 'What retention policy governs Solaris archives?'
QUERIES = (
    QUESTION,
    'For Northstar staging, what retention policy governs Solaris archives?',
    'How long are Solaris archive records kept under the retention policy?',
    'Should Solaris archives be kept for ninety days rather than thirty days?',
)


class ScopedMemoryRetrievalTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'memory.db')
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.task = self.store.add_run({'title': 'Synthetic retrieval', 'repo': REPO})['id']
        for method in ('complete', 'complete_json'):
            guard = patch.object(llm.Client, method, side_effect=AssertionError('No provider calls allowed'))
            guard.start()
            self.addCleanup(guard.stop)

    def note(self, *, question=QUESTION, repo=REPO, answer='Retain archives for thirty days.',
             context='', facts=None, scope_key='', stamp='2026-10-01T00:00:00+00:00', **fields):
        did = self.graph.add_decision(self.task, question, 'policy', 'resolved', repo=repo,
                                      answer=answer, context=context, source='agent')
        values = {'facts': json.dumps(facts or {}), 'scope_key': scope_key, 'updated_at': stamp, **fields}
        self.graph.db.execute('UPDATE decisions SET ' + ','.join(f'{key}=?' for key in values) +
                              ' WHERE id=?', [*values.values(), did])
        return did

    def seed_context_pair(self):
        tail = ' unrelated historical prose' * 200
        northstar = self.note(answer='Northstar archives use thirty days, not ninety days.' + tail,
                              context='customer=northstar; environment=staging',
                              facts={'customer': 'northstar', 'environment': 'staging'},
                              scope_key='synthetic-northstar-staging')
        bluebird = self.note(answer='Bluebird archives use ninety days rather than thirty days.' + tail,
                             context='customer=bluebird; environment=production',
                             facts={'customer': 'bluebird', 'environment': 'production'},
                             scope_key='synthetic-bluebird-production', stamp='2026-10-02T00:00:00+00:00')
        return northstar, bluebird

    def fillers(self, count):
        for i in range(count):
            self.note(question=f'Colour and font selection {i}', answer='Use blue and serif.',
                      stamp='2026-10-03T00:00:00+00:00')

    def assert_visible_pair(self, pair):
        for query in QUERIES:
            with self.subTest(query=query):
                rows = self.graph._bounded_rows(graph_mod.MEMORY_STATUSES, REPO, query)
                self.assertTrue(set(pair).issubset({row['id'] for row in rows}))
                self.assertTrue(all(row['repo'] == REPO for row in rows))
                connection = Mock(wraps=self.graph.db)
                with patch.object(self.graph._local, 'db', connection), \
                        patch.object(self.graph, '_row_embedding', wraps=self.graph._row_embedding) as score:
                    hits = self.graph.memory_search(query, repo=REPO)
                budget = graph_mod.CANDIDATE_FTS + graph_mod.CANDIDATE_RECENT
                self.assertLessEqual(score.call_count, budget)
                # Boost, count, FTS candidates, recent candidates, then at
                # most ceil(budget/400) bounded row reads; no per-row queries.
                self.assertLessEqual(connection.execute.call_count, 4 + (budget + 399) // 400)
                self.assertTrue(set(pair).issubset({hit['id'] for hit in hits}))
                self.assertTrue(all(hit['repo'] == REPO for hit in hits))
                self.assertTrue(all(not hit['signed'] for hit in hits))
                self.assertTrue(all(not hit['demoted_by'] for hit in hits))
                self.assertLessEqual(len(rows), graph_mod.CANDIDATE_FTS + graph_mod.CANDIDATE_RECENT)

    def test_small_memory_preserves_same_question_in_distinct_contexts(self):
        pair = self.seed_context_pair()
        self.fillers(20)
        self.assert_visible_pair(pair)

    def test_large_memory_fts_budget_is_repository_local(self):
        if not self.graph.has_fts:
            self.skipTest('Backend has no FTS')
        with self.graph.transaction():
            for _ in range(650):
                self.note(repo=OTHER_REPO, answer='Solaris archives use ninety days.')
            pair = self.seed_context_pair()
            self.fillers(2001)
        # Both desired rows are older than the recent window and their long
        # text ranks below 650 stronger foreign FTS matches on the old code.
        with patch.object(self.graph, '_memory_rows', wraps=self.graph._memory_rows) as fetch:
            self.assert_visible_pair(pair)
        self.assertTrue(fetch.call_args_list)
        for call in fetch.call_args_list:
            self.assertIn('ids', call.kwargs, 'Large retrieval must not fall back to an unbounded row scan')
            self.assertLessEqual(len(call.kwargs['ids']), graph_mod.CANDIDATE_FTS + graph_mod.CANDIDATE_RECENT)

    def test_ineligible_rows_do_not_consume_fts_or_recent_budgets(self):
        if not self.graph.has_fts:
            self.skipTest('Backend has no FTS')
        wanted = self.note(answer='Retain Solaris archives thirty days.' + ' background' * 100)
        self.fillers(12)
        excluded = set()
        variants = ({'status': 'pending'}, {'superseded_by': 'retired'}, {'draft': 1}, {'answer': ''},
                    {'source': 'memory'}, {'source': 'record'}, {'source': 'human'},
                    {'source': 'memory', 'signoff': 'rule'}, {'repo': OTHER_REPO})
        with self.graph.transaction():
            for variant in variants:
                for _ in range(8):
                    excluded.add(self.note(stamp='2026-10-05T00:00:00+00:00', **variant))
        with patch.multiple(graph_mod, FULL_SCAN_MAX=10, CANDIDATE_FTS=4, CANDIDATE_RECENT=3):
            hits = self.graph._fts_ids('decisions_fts', ['solaris', 'archives'], 4,
                                        repo=REPO, statuses=graph_mod.MEMORY_STATUSES)
            self.assertEqual(hits, {wanted})
            rows = self.graph._bounded_rows(graph_mod.MEMORY_STATUSES, REPO, QUESTION)
            self.assertIn(wanted, {row['id'] for row in rows})
            self.assertFalse(excluded & {row['id'] for row in rows})
            self.assertEqual(len(rows), 4, 'Recent fallback must fill its budget with eligible rows')
            self.assertEqual(self.graph._memory_count(graph_mod.MEMORY_STATUSES, REPO), 13)

    def test_pending_candidates_have_their_own_scoped_budget(self):
        if not self.graph.has_fts:
            self.skipTest('Backend has no FTS')
        wanted = self.note(status='pending', answer='', context=' background' * 100)
        with self.graph.transaction():
            for i in range(12):
                self.note(question=f'Colour and font selection {i}', status='pending', answer='',
                          stamp='2026-10-03T00:00:00+00:00')
            for _ in range(10):
                self.note()
                self.note(repo=OTHER_REPO, status='pending', answer='')
        with patch.multiple(graph_mod, FULL_SCAN_MAX=10, CANDIDATE_FTS=4, CANDIDATE_RECENT=3):
            rows = self.graph._bounded_rows(('pending',), REPO, QUESTION)
        self.assertIn(wanted, {row['id'] for row in rows})
        self.assertTrue(all(row['status'] == 'pending' and row['repo'] == REPO for row in rows))
        self.assertLessEqual(len(rows), 7)

    def test_global_legacy_scope_remains_eligible_but_foreign_repo_does_not(self):
        global_id = self.note(repo='', context='Legacy unscoped policy')
        local = self.note(context='Local policy')
        foreign = self.note(repo=OTHER_REPO)
        for query in QUERIES[:2]:
            hits = self.graph.memory_search(query, repo=REPO)
            self.assertEqual({hit['id'] for hit in hits}, {global_id, local})
            self.assertNotIn(foreign, {hit['id'] for hit in hits})
        # An omitted repository deliberately remains a global search.
        self.assertIn(foreign, {hit['id'] for hit in self.graph.memory_search(QUESTION)})

    def test_model_exclusion_applies_before_the_fts_limit(self):
        if not self.graph.has_fts:
            self.skipTest('Backend has no FTS')
        excluded = self.note()
        wanted = self.note(answer='Solaris archives thirty days.' + ' background' * 100)
        with patch.object(self.graph._local, 'model_exclude', excluded, create=True):
            self.assertEqual(self.graph._fts_ids('decisions_fts', ['solaris', 'archives'], 1,
                                                repo=REPO, statuses=graph_mod.MEMORY_STATUSES), {wanted})
            self.assertEqual(self.graph._memory_count(graph_mod.MEMORY_STATUSES, REPO), 1)

    def test_without_fts_or_query_only_recent_eligible_rows_are_scored(self):
        self.fillers(12)
        for _ in range(6):
            self.note(source='memory', stamp='2026-10-05T00:00:00+00:00')
        with patch.multiple(graph_mod, FULL_SCAN_MAX=10, CANDIDATE_FTS=4, CANDIDATE_RECENT=3):
            for query in ('', QUESTION):
                with self.subTest(query=query), patch.object(self.graph, 'has_fts', False):
                    rows = self.graph._bounded_rows(graph_mod.MEMORY_STATUSES, REPO, query)
                    self.assertEqual(len(rows), 3)
                    self.assertTrue(all(row['source'] == 'agent' for row in rows))

    def test_scope_differences_prevent_dedup_and_newest_demotion(self):
        for scope in ('facts', 'context', 'scope_key', 'applicability', 'scope_paths', 'path'):
            with self.subTest(scope=scope):
                old = self.note()
                values = {
                    'facts': {'customer': 'bluebird'},
                    'context': 'customer=bluebird; environment=production',
                    'scope_key': 'separate-scope',
                    'applicability': json.dumps({'requires': {'customer': 'bluebird'}}),
                    'scope_paths': json.dumps(['archives/production.py']),
                    'path': 'archives/production.py',
                }
                new = self.note(stamp='2026-10-02T00:00:00+00:00', **{scope: values[scope]})
                rows = [self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()
                        for did in (old, new)]
                with patch.object(self.graph, '_bounded_rows', return_value=rows):
                    hits = self.graph.memory_search(QUESTION, repo=REPO)
                self.assertEqual({hit['id'] for hit in hits}, {old, new})
                self.assertTrue(all(not hit['demoted_by'] for hit in hits))

    def test_equal_scope_key_does_not_override_conflicting_or_missing_facts(self):
        for other_facts in ({'customer': 'bluebird'}, {}):
            with self.subTest(facts=other_facts):
                old = self.note(scope_key='same-key', facts={'customer': 'northstar'})
                new = self.note(scope_key='same-key', facts=other_facts, stamp='2026-10-02T00:00:00+00:00')
                rows = [self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()
                        for did in (old, new)]
                with patch.object(self.graph, '_bounded_rows', return_value=rows):
                    hits = self.graph.memory_search(QUESTION, repo=REPO)
                self.assertEqual({hit['id'] for hit in hits}, {old, new})

    def test_same_scope_keeps_newest_and_explicit_supersession_stays_hidden(self):
        old = self.note(context='Customer northstar', facts={'customer': 'Northstar'})
        new = self.note(context=' customer  NORTHSTAR ', facts={'customer': 'northstar'},
                        stamp='2026-10-02T00:00:00+00:00')
        hits = self.graph.memory_search(QUESTION, repo=REPO)
        self.assertEqual([hit['id'] for hit in hits], [new])
        self.graph.update_decision(new, superseded_by=old)
        self.assertEqual([hit['id'] for hit in self.graph.memory_search(QUESTION, repo=REPO)], [old])
        # An explicit replacement remains authoritative even when its scope
        # differs. Dedup compatibility never resurrects a superseded row.
        self.graph.update_decision(old, superseded_by='', supersedes=new)
        self.graph.update_decision(new, superseded_by='', scope_key='other')
        self.assertEqual([hit['id'] for hit in self.graph.memory_search(QUESTION, repo=REPO)], [old])

    def test_query_prose_does_not_create_or_inherit_current_facts(self):
        pair = self.seed_context_pair()
        before = {did: dict(self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone())
                  for did in pair}
        hits = self.graph.memory_search(QUERIES[1], repo=REPO)
        self.assertEqual({hit['id'] for hit in hits}, set(pair))
        self.assertTrue(all(not hit['signed'] for hit in hits))
        after = {did: dict(self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone())
                 for did in pair}
        self.assertEqual(before, after)
        self.assertEqual(self.graph.db.execute('SELECT count(*) c FROM decisions').fetchone()['c'], 2)
