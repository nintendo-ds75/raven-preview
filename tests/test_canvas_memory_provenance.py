"""Background memory publication retains the exact observed source revision."""
import json
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import canvas, context_memory as cm, ladder
from bridge.config import Config
from bridge.store import Invalid


REPO = 'background/policy'
QUESTION = 'How long should policy audit records remain?'
ANSWER = 'Keep policy audit records for thirty days.'


class BackgroundMemoryProvenanceTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store('memory-provenance.db')
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.store.add_owner({'name': 'Policy Owner', 'team': 'Policy', 'patterns': 'policy/*'})
        for method in ('complete', 'complete_json'):
            guard = patch('bridge.llm.Client.' + method, side_effect=AssertionError('Provider calls forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def node(self):
        task = self.store.add_run({'title': 'Review audit retention', 'repo': REPO})['id']
        did = self.graph.add_decision(task, QUESTION, 'policy', 'pending', repo=REPO,
                                      owner='Policy Owner', path='policy/retention.py')
        return task, did

    def sign(self, did):
        return canvas.sign_off(self.store, did, {'by': 'Policy Owner',
            'expected_updated_at': self.store.get_decision(did)['updated_at']})

    def source(self, external=False):
        task, did = self.node()
        data = {'answer': ANSWER, 'signed_by': 'Policy Owner'}
        if external:
            data['source_evidence'] = [cm.pin(self.record()['source'])]
        self.store.answer(did, data)
        self.assertTrue(self.store.get_decision(did)['authorized'])
        return task, did

    def record(self, **changes):
        return self.store.add_record({'repo': REPO, 'kind': 'ticket', 'ref': 'POLICY-1',
            'title': 'Audit retention policy', 'body': ANSWER, 'status': 'Done', **changes})

    def model_pass(self, did, after_read=None):
        self.graph.db.execute('UPDATE decisions SET model_pending=1 WHERE id=?', (did,))
        current = self.store.get_decision(did)
        original = ladder.ask
        observed = {}

        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            observed.update(result)
            if after_read:
                after_read()
            return result

        with patch('bridge.ladder.ask', side_effect=read):
            canvas._model_node_pass(self.store, Config(model_api='none'), did, '',
                ['policy/retention.py'], [], '', current['owner_id'], '', current['updated_at'])
        self.assertEqual(observed['source'], 'memory')
        self.assertTrue(observed['source_revision'])
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM decisions WHERE draft=1').fetchone()[0], 0)
        self.assertFalse(self.store.get_decision(did)['model_pending'])
        return observed

    def test_background_reuse_preserves_observed_revision_and_accepts_ordinary_signoff(self):
        _, source = self.source()
        _, did = self.node()
        observed = self.model_pass(did)
        current = canvas.node_view(self.store, did)
        self.assertEqual(current['source_id'], source)
        self.assertEqual(current['source_revision'], observed['source_revision'])
        self.assertEqual(current['source_revision'], self.store.get_decision(source)['updated_at'])
        self.assertFalse(current['authorized'])
        self.assertEqual(current['signoff'], 'required')
        self.assertEqual(current['signatures'], [])
        self.assertTrue(self.sign(did)['authorized'])
        snapshot = json.loads(cm.history(self.graph.db, did)[-1]['snapshot'])
        self.assertEqual(snapshot['decision']['source_revision'], observed['source_revision'])

    def test_source_correction_does_not_repin_or_authorize_reused_answer(self):
        _, source = self.source()
        task, did = self.node()
        observed = self.model_pass(did)
        self.sign(did)
        self.store.answer(source, {'answer': 'Keep policy audit records for seven days.',
            'expected_updated_at': self.store.get_decision(source)['updated_at']})
        current = self.store.get_decision(did)
        self.assertEqual(current['source_revision'], observed['source_revision'])
        self.assertTrue(current['needs_review'])
        self.assertFalse(current['authorized'])
        with self.assertRaises(Invalid):
            self.sign(did)
        self.assertTrue(self.graph.blocking_nodes(task))

    def test_changed_or_inaccessible_external_evidence_invalidates_reused_answer(self):
        for change in ({'body': 'The policy changed to seven days.'}, {'availability': 'inaccessible'}):
            with self.subTest(change=change):
                self.record()
                _, source = self.source(external=True)
                task, did = self.node()
                observed = self.model_pass(did)
                self.sign(did)
                self.record(**change)
                current = self.store.get_decision(did)
                self.assertEqual(current['source_revision'], observed['source_revision'])
                self.assertEqual(current['source_id'], source)
                self.assertTrue(current['needs_review'])
                self.assertFalse(current['authorized'])
                with self.assertRaises(Invalid):
                    self.sign(did)
                self.assertTrue(self.graph.blocking_nodes(task))

    def test_source_changed_during_background_read_is_not_published(self):
        _, source = self.source()
        _, did = self.node()

        def change_source():
            self.store.answer(source, {'answer': 'Keep policy audit records for seven days.',
                'expected_updated_at': self.store.get_decision(source)['updated_at']})

        self.model_pass(did, after_read=change_source)
        current = self.store.get_decision(did)
        self.assertEqual(current['status'], 'pending')
        self.assertFalse(current['source_id'])
        self.assertFalse(current['source_revision'])
        self.assertFalse(current['authorized'])
        self.assertTrue(any(event['kind'] == 'model_read_failed' for event in current['events']))
        self.assertFalse(any(event['kind'] == 'model_read' for event in current['events']))
