"""A configured decider keeps the call in the model pass, however weakly
the category matched.

Measured live on the Parcelwise demo (main aaa0858): the ladder routed "If a
batch would take the merchant over their monthly label allowance, do we
refuse the whole batch before buying anything and show how many labels are
left?" to Priya Shah, who decides pricing ("this one only mentions pricing",
from the authority map). The canvas model pass then ran the contact
reasoning, whose guard protected only a strong decides match, and the
model picked Marcus Lee for having written all of plans.py and its last pull
request. Code authorship is not business authority: a person the map itself
names as deciding for the call keeps it. A decider learned from a referral is
not the map's own word and does not.
"""
import json
from unittest.mock import patch

from fixtures import OfflineCase
from bridge.config import Config
from bridge.contact_reasoning import choose
from bridge.routing import rank_for_decision

REPO = 'parcelwise/parcelwise-app'
PATH = 'app/billing/plans.py'
QUESTION = ('If a batch would take the merchant over their monthly label allowance, do we refuse the whole '
            'batch before buying anything and show how many labels are left?')
# Nothing in the question's own words names pricing; the context does, so
# the pricing decider's row is a weak match ("this one only mentions pricing").
CONTEXT = 'Pricing: the monthly label allowance per plan is read from plans.py.'


class ConfiguredDeciderKeepsTheCall(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store('decider.db')
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        with self.graph.transaction():
            self.priya = self.graph.add_person('Priya Shah', email='priya@parcelwise.example')
            self.marcus = self.graph.add_person('Marcus Lee', email='marcus@parcelwise.example')
            self.graph.upsert_artifact(REPO, PATH)
        self.task = self.store.add_run({'title': 'Batch label purchase', 'repo': REPO})['id']
        for target in ('socket.socket.connect', 'socket.create_connection', 'bridge.llm.Client.complete'):
            guard = patch(target, side_effect=AssertionError('Live inference/network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def decider(self, source):
        with self.graph.transaction():
            self.graph.add_authority('category', 'pricing', 'decides', person_id=self.priya, repo=REPO,
                                     source=source, asserted_by='Sam Ortiz' if source == 'referral' else '')

    def authored(self):
        # The PR that touched the file, as semantic retrieval surfaces it.
        return [{'author': 'Marcus Lee', 'kind': 'pr', 'ref': '#101', 'status': '',
                 'title': 'Add batch label purchasing and the allowance check in plans.py',
                 'body': 'Marcus wrote the whole of plans.py and the batch path.'}]

    def choose(self):
        details = {}
        marcus_first = {'candidate': 'c2', 'evidence': ['c2:e1'], 'why': 'Wrote plans.py and PR #101'}
        with patch('bridge.semantic_index.sources', return_value=(self.authored(), {'backend': 'synthetic'})), \
                patch.object(Config, 'semantic_retrieval', property(lambda _: True)), \
                patch('bridge.contact_reasoning.Client.complete_json', return_value=marcus_first) as model:
            result = choose(self.graph, Config(model_api='none'), REPO, QUESTION, paths=(PATH,), context=CONTEXT,
                            task_id=self.task, details=details)
        return result, details, model

    def test_the_ladder_places_the_weakly_matched_pricing_decider_first(self):
        self.decider('config')
        ranked = rank_for_decision(self.graph, REPO, QUESTION, (PATH,), context=CONTEXT)
        self.assertEqual(ranked[0][0], 'Priya Shah', ranked)
        self.assertIn('verified: Priya Shah decides for pricing decisions (this one only mentions pricing) (config)',
                      ranked[0][1])

    def test_code_authorship_does_not_take_the_call_from_a_configured_decider(self):
        self.decider('config')
        result, details, model = self.choose()
        self.assertEqual(result[0][0], 'Priya Shah', result)
        self.assertTrue(any(line.startswith('verified: Priya Shah decides') for line in result[0][1]), result[0][1])
        self.assertNotIn('Marcus Lee', [r[0] for r in result])
        self.assertEqual(details, {'status': 'fallback'})
        model.assert_not_called()
        self.assertEqual(self.graph.db.execute("SELECT count(*) FROM events WHERE kind='contact_reasoned'").fetchone()[0], 0)

    def test_a_decider_learned_from_a_referral_is_still_weighed_against_the_record(self):
        self.decider('referral')
        ranked = rank_for_decision(self.graph, REPO, QUESTION, (PATH,), context=CONTEXT)
        self.assertEqual(ranked[0][0], 'Priya Shah', ranked)
        result, details, model = self.choose()
        self.assertEqual(result[0][0], 'Marcus Lee', result)
        self.assertEqual(details, {'status': 'selected'})
        self.assertEqual(model.call_count, 1)
        candidates = json.loads(model.call_args.args[2])['candidates']
        self.assertEqual([c['name'] for c in candidates], ['Priya Shah', 'Marcus Lee'])
