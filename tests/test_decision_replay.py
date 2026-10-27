"""Signed objective-scope decisions resurface through the ladder as
evidence, never as approval. Six proposals from the labelled corpus are
accepted and signed into one ledger; the same questions on new tasks
come back resolved or predicted with the recorded answer, and every one
of them still needs a signature. No model, no key, no network."""
import json

from fixtures import OfflineCase, ROOT
from bridge import canvas, objective_head as head, objectives as obj
from bridge.auth import Identity
from bridge.config import Config

FIXTURES = ROOT / 'tests' / 'fixtures' / 'proposals'
CORPUS = json.loads((FIXTURES / 'corpus.json').read_text())
LABELS = json.loads((FIXTURES / 'labels.json').read_text())['records']
REPO = CORPUS['repository']
CFG = Config(model_api='none')
SEEDS = 6


class DecisionReplayTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store('decision-replay')
        self.addCleanup(self.store.graph.close)
        self.actor = Identity('', 'Local operator', 'admin', 'operator')
        self.store.add_owner({'name': 'Policy Owner', 'team': 'Policy', 'paths': '*', 'repo': REPO})
        self.records = {r['id']: r for r in CORPUS['records']}

    def proposal(self, fixture_id):
        """One accepted, signed scope decision from a labelled positive."""
        record = self.records[fixture_id]
        label = LABELS[fixture_id]
        source = self.store.add_record({k: v for k, v in record.items() if k != 'id'} | {'repo': REPO})['source']
        pin = {k: source[k] for k in ('record_id', 'source_version_id')}
        payload = {'title': label['title'], 'outcome': 'Prepare ' + label['title'][0].lower() + label['title'][1:] + ' for review',
                   'success_criteria': 'A person can review the prepared change', 'stop_criteria': 'A person declines',
                   'guardrails': 'Private preparation only; nothing is sent', 'repository': REPO, 'provider': 'claude',
                   'why_now': 'The cited record asks for it', 'judgment_kind': 'interpretation_tradeoff',
                   'cases': {'positive': 'Do: ' + label['title'], 'negative': 'Nothing the evidence does not name',
                             'boundary': 'Ask when billing or access would change', 'non_implications': 'No external communication'},
                   'citations': [{**pin, 'quote': label['quote']}]}
        proposal = obj.propose(self.store, payload, [pin])
        review = obj.proposal_action(self.store, proposal['id'], 'accept',
            {'request_key': 'accept-' + fixture_id, 'expected_revision': proposal['revision']}, self.actor)
        decision = self.store.get_decision(review['decision_id'])
        canvas.sign_off(self.store, review['decision_id'], {'by': 'Policy Owner', 'expected_updated_at': decision['updated_at']})
        head.activate_reviews(self.store)
        return self.store.get_decision(review['decision_id'])

    def test_signed_scope_decisions_replay_as_evidence_with_signoff_still_required(self):
        positives = [fid for fid, label in LABELS.items() if label['worth_surfacing']][:SEEDS]
        self.assertEqual(len(positives), SEEDS)
        seeded = [self.proposal(fid) for fid in positives]
        for decision in seeded:
            self.assertEqual((decision['status'], decision['signoff']), ('resolved', 'signed'), decision['question'])
            self.assertTrue(decision['authorized'])
        self.assertEqual(len(obj.overview(self.store)['objectives']), SEEDS)
        # The recorded cases: (question, context) -> the signed answer.
        cases = [(d['question'], d['context'], d['answer'], d['id']) for d in seeded]
        replayed, outcomes = 0, []
        for question, context, answer, source_id in cases:
            task = self.store.add_run({'title': 'Replay: ' + question[:60], 'repo': REPO})['id']
            node = canvas.add_node(self.store, CFG, {'task_id': task, 'question': question, 'context': context,
                                                     'category': 'objective_scope', 'requester': 'Replay'})
            stored = self.store.get_decision(node['node_id'])
            recorded = (node['answer'] == answer or node['prediction'] == answer or stored['source_id'] == source_id)
            outcomes.append((question, node['status'], node['signoff'], recorded))
            # A replay is evidence, never approval: whatever rung answered,
            # nothing is authorized and a signature is still required.
            self.assertFalse(node['authorized'], question)
            self.assertNotEqual(node['signoff'], 'signed', question)
            if recorded:
                self.assertIn(node['status'], ('resolved', 'predicted'), question)
                self.assertEqual(node['signoff'], 'required', question)
                replayed += 1
        print(f"\ndecision replay: {replayed} of {len(cases)} signed scope decisions resurfaced with signoff required")
        self.assertGreaterEqual(replayed, 5, outcomes)
        # No objective was created or activated by the replay itself.
        self.assertEqual(len(obj.overview(self.store)['objectives']), SEEDS)
