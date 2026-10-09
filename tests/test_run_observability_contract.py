"""Read-only run provenance from existing rows; no new authority or identity rules."""
import json
from pathlib import Path

from fixtures import OfflineCase
from bridge import canvas
from bridge.store import Store


class RunObservabilityContractTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'run-observability.db')
        self.g = self.store.graph
        self.addCleanup(self.g.close)
        self.owner = self.store.add_owner({'name': 'Synthetic Contact', 'team': 'Synthetic Team', 'patterns': 'docs/*'})
        self.task = self.store.add_run({'title': 'Synthetic run observability',
                                      'repo': 'synthetic/run-view', 'agent': 'Raven UI'})['id']

    def node(self, source='', **extra):
        return self.g.add_decision(self.task, 'What should the synthetic release include?', 'policy',
            'pending', source=source, repo='synthetic/run-view', owner='Synthetic Contact',
            path='docs/release.md', **extra)

    def views(self, did):
        direct = canvas.node_view(self.store, did)
        tree = canvas.get_tree(self.store, self.task)
        return direct, next(n for n in canvas._flatten(tree['nodes']) if n['node_id'] == did)

    def snapshot(self):
        return {
            'decisions': [dict(r) for r in self.g.db.execute('SELECT * FROM decisions ORDER BY id')],
            'events': [dict(r) for r in self.g.db.execute('SELECT * FROM events ORDER BY id')],
            'versions': [dict(r) for r in self.g.db.execute('SELECT * FROM decision_versions ORDER BY id')],
        }

    def test_source_origin_and_exact_revision_are_not_inferred_from_answer_or_owner(self):
        prior = self.node(source='human', answer='Historical answer', answered_by='Historical Respondent')
        for source in ('record', 'memory', 'agent', 'human', ''):
            with self.subTest(source=source):
                did = self.node(source=source, answer='Same text for every origin',
                    answered_by='Recorded respondent', owner_evidence='Unrelated historical routing note')
                self.g.db.execute('UPDATE decisions SET source_id=?,source_revision=?,routing_reason=? WHERE id=?',
                    (prior if source == 'memory' else '', 'exact-saved-revision' if source == 'memory' else '',
                     'Current recorded route', did))
                before = self.snapshot()
                for view in self.views(did):
                    self.assertEqual(view['source'], source)
                    self.assertEqual(view['source_id'], prior if source == 'memory' else '')
                    self.assertEqual(view['source_revision'], 'exact-saved-revision' if source == 'memory' else '')
                    self.assertEqual(view['routing_reason'], 'Current recorded route')
                    self.assertFalse(view['authorized'])
                    self.assertTrue(view['blocking'])
                self.assertEqual(self.snapshot(), before)

    def test_current_route_is_separate_from_older_owner_evidence(self):
        did = self.node(owner_evidence='Earlier candidate was Synthetic Coordinator',
                        routing_reason='Reassigned to Synthetic Contact for this question')
        before = self.snapshot()
        for view in self.views(did):
            self.assertEqual(view['owner'], 'Synthetic Contact')
            self.assertEqual(view['owner_evidence'], 'Earlier candidate was Synthetic Coordinator')
            self.assertEqual(view['routing_reason'], 'Reassigned to Synthetic Contact for this question')
        self.assertEqual(self.snapshot(), before)

    def test_duplicate_reads_canonical_origin_and_route_instead_of_stub_metadata(self):
        prior = self.node(source='human', answer='Earlier synthetic decision')
        canonical = self.node(source='memory', answer='Current synthetic answer',
                              routing_reason='Canonical recorded route')
        self.g.db.execute('UPDATE decisions SET source_id=?,source_revision=? WHERE id=?',
                          (prior, 'canonical-source-revision', canonical))
        duplicate = self.node(source='agent', routing_reason='Obsolete duplicate route')
        self.g.db.execute("UPDATE decisions SET status='duplicate',superseded_by=?,source_revision=? WHERE id=?",
                          (canonical, 'obsolete-stub-revision', duplicate))
        before = self.snapshot()
        for view in self.views(duplicate):
            self.assertEqual(view['duplicate_of'], canonical)
            self.assertEqual(view['source'], 'memory')
            self.assertEqual(view['source_id'], prior)
            self.assertEqual(view['source_revision'], 'canonical-source-revision')
            self.assertEqual(view['routing_reason'], 'Canonical recorded route')
            self.assertFalse(view['authorized'])
        self.assertEqual(self.snapshot(), before)

    def test_review_required_history_keeps_origin_without_current_permission(self):
        did = self.node(source='memory', answer='A historical answer',
                        answered_by='Historical Respondent')
        self.g.db.execute("UPDATE decisions SET needs_review=1,review_reason=?,source_revision=? WHERE id=?",
                          ('The recorded premise needs review', 'retained-historical-revision', did))
        before = self.snapshot()
        for view in self.views(did):
            self.assertEqual(view['source'], 'memory')
            self.assertEqual(view['source_revision'], 'retained-historical-revision')
            self.assertTrue(view['needs_review'])
            self.assertFalse(view['authorized'])
            self.assertTrue(view['blocking'])
        self.assertEqual(self.snapshot(), before)
