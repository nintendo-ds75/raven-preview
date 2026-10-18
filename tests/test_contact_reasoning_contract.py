"""Contact selections bind exact evidence IDs; failed reasoning never grants authority."""
import json
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import canvas
from bridge.config import Config
from bridge.contact_reasoning import choose
from bridge.llm import LLMError


class ContactReasoningContractTests(OfflineCase):
    repo = 'synthetic/contact-contract'
    question = 'Should batch retries count toward metered usage?'

    def setUp(self):
        super().setUp()
        self.store = self.fresh_store('contacts.db')
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.people = {name: self.graph.add_person(name, email=name.replace(' ', '.') + '@synthetic.invalid')
                       for name in ('Synthetic Maintainer', 'Synthetic Analyst', 'Synthetic Approver')}
        self.ranked = [('Synthetic Maintainer', ['Committed many mechanical changes; on call'], .9),
                       ('Synthetic Analyst', ['Investigated batch retry metering and customer invoices; policy still open'], .4)]
        self.task = self.store.add_run({'title': 'Review synthetic usage', 'repo': self.repo})['id']
        for target in ('socket.socket.connect', 'socket.create_connection', 'bridge.llm.Client.complete'):
            guard = patch(target, side_effect=AssertionError('Live inference/network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def events(self):
        return [json.loads(row['detail']) for row in self.graph.db.execute(
            "SELECT detail FROM events WHERE kind='contact_reasoned' ORDER BY id")]

    def choose(self, results, *, semantic=True, fixed=()):
        details = {}
        with patch('bridge.routing.rank_for_decision', return_value=self.ranked), \
                patch('bridge.signals._authority_matches', return_value=fixed), \
                patch('bridge.semantic_index.sources', return_value=([], {'backend': 'synthetic'})), \
                patch.object(Config, 'semantic_retrieval', property(lambda _: semantic)), \
                patch('bridge.contact_reasoning.Client.complete_json', side_effect=results) as model:
            result = choose(self.graph, Config(model_api='none'), self.repo, self.question,
                            task_id=self.task, details=details)
        return result, details, model

    def test_valid_selection_requires_exact_candidate_and_owned_evidence(self):
        before = self.graph.authority_rows(self.repo)
        result, details, model = self.choose([{'candidate': 'c2', 'evidence': ['c2:e1'],
                                             'why': 'Relevant investigation'}])
        self.assertEqual(result[0][0], 'Synthetic Analyst')
        self.assertIn('confirm or refer', result[0][1][0])
        self.assertEqual(result[0][1][1], self.ranked[1][1][0])
        self.assertEqual(details, {'status': 'selected'})
        self.assertEqual(model.call_count, 1)
        self.assertTrue(model.call_args.kwargs['bounded'])
        self.assertEqual(model.call_args.kwargs['max_tokens'], 700)
        self.assertEqual(self.events()[-1]['validation'], 'selected')
        self.assertEqual(self.events()[-1]['selected'], 'c2')
        self.assertEqual(self.graph.authority_rows(self.repo), before)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM contact_observations').fetchone()[0], 0)

    def test_every_invalid_shape_repairs_once_then_abstains_without_prose_routing(self):
        bad = [
            ([], 'invalid_result_type'),
            ({'why': 'Synthetic Analyst is the right contact'}, 'missing_candidate'),
            ({'candidate': ['c2'], 'evidence': ['c2:e1']}, 'invalid_candidate_type'),
            ({'candidate': {'id': 'c2'}, 'evidence': ['c2:e1']}, 'invalid_candidate_type'),
            ({'candidate': None, 'evidence': ['c2:e1']}, 'invalid_candidate_type'),
            ({'candidate': 'Synthetic Analyst', 'evidence': ['c2:e1']}, 'unknown_candidate'),
            ({'candidate': 'c999', 'evidence': ['c2:e1']}, 'unknown_candidate'),
            ({'candidate': 'c2'}, 'missing_evidence'),
            ({'candidate': 'c2', 'evidence': 'c2:e1'}, 'invalid_evidence_type'),
            ({'candidate': 'c2', 'evidence': []}, 'empty_evidence'),
            ({'candidate': 'c2', 'evidence': [None]}, 'invalid_evidence_id_type'),
            ({'candidate': 'c2', 'evidence': [{'id': 'c2:e1'}]}, 'invalid_evidence_id_type'),
            ({'candidate': 'c2', 'evidence': ['c2:e99']}, 'unknown_evidence'),
            ({'candidate': 'c2', 'evidence': ['c1:e1']}, 'cross_candidate_evidence'),
            ({'candidate': 'c2', 'evidence': ['c2:e1', 'c1:e1']}, 'cross_candidate_evidence'),
            ({'candidate': '', 'evidence': ['c2:e1']}, 'abstention_with_evidence'),
        ]
        for response, reason in bad:
            with self.subTest(reason=reason, response=response):
                result, details, model = self.choose([response, response])
                self.assertEqual(result, [])
                self.assertEqual(details, {'status': 'abstained'})
                self.assertEqual(model.call_count, 2)
                first, repaired = self.events()[-2:]
                self.assertEqual([first['validation'], repaired['validation']], [reason, reason])
                self.assertEqual([first['repair'], repaired['repair']], [False, True])
                self.assertEqual([first['selection_attempt'], repaired['selection_attempt']], [1, 2])
                self.assertEqual([first['selected'], repaired['selected']], ['', ''])
                self.assertEqual([first['evidence'], repaired['evidence']], [[], []])
                original = json.loads(model.call_args_list[0].args[2])
                retry = json.loads(model.call_args_list[1].args[2])
                self.assertEqual(original['candidates'], retry['candidates'])
                self.assertEqual(retry['selection_repair']['validation'], reason)
                self.assertNotIn('previous_response', retry)

    def test_one_bounded_repair_can_bind_a_valid_selection(self):
        result, details, model = self.choose([
            {'candidate': 'Synthetic Analyst', 'evidence': ['c2:e1'], 'why': 'Use analyst'},
            {'candidate': 'c2', 'evidence': ['c2:e1']}])
        self.assertEqual(result[0][0], 'Synthetic Analyst')
        self.assertEqual(details['status'], 'selected')
        self.assertEqual(model.call_count, 2)
        self.assertTrue(all(c.kwargs == {'max_tokens': 700, 'bounded': True} for c in model.call_args_list))
        self.assertEqual([e['validation'] for e in self.events()], ['unknown_candidate', 'selected'])

    def test_explicit_abstention_never_infers_from_why_or_uses_default_contact(self):
        result, details, model = self.choose([{'candidate': '', 'evidence': [],
            'why': 'Synthetic Analyst could know, followed by Synthetic Approver.'}])
        self.assertEqual(result, [])
        self.assertEqual(details['status'], 'abstained')
        self.assertEqual(model.call_count, 1)
        self.assertEqual(self.events()[-1]['validation'], 'explicit_abstention')

    def test_search_after_abstention_is_bounded_and_can_select_owned_evidence(self):
        result, _, model = self.choose([
            {'candidate': '', 'evidence': [], 'search_query': 'retry classification investigation'},
            {'candidate': 'c2', 'evidence': ['c2:e1']}])
        self.assertEqual(result[0][0], 'Synthetic Analyst')
        self.assertEqual(model.call_count, 2)
        self.assertEqual([e['attempt'] for e in self.events()], [1, 2])
        self.assertEqual(self.events()[-1]['query'], 'retry classification investigation')

    def test_retrieval_and_repair_share_one_repair_budget(self):
        invalid = {'candidate': 'Synthetic Analyst', 'evidence': ['c2:e1'], 'search_query': 'more context'}
        result, details, model = self.choose([invalid, invalid, invalid])
        self.assertEqual(result, [])
        self.assertEqual(details['status'], 'abstained')
        self.assertEqual(model.call_count, 3)
        self.assertEqual([e['repair'] for e in self.events()], [False, True, False])
        self.assertEqual([e['attempt'] for e in self.events()], [1, 1, 2])

    def test_malformed_json_is_repaired_and_provider_errors_are_not_logged(self):
        error = LLMError('PRIVATE raw provider data')
        error.__cause__ = json.JSONDecodeError('Incomplete', '{', 1)
        result, details, model = self.choose([error, LLMError('PRIVATE raw provider data')])
        self.assertEqual(result, [])
        self.assertEqual(details['status'], 'abstained')
        self.assertEqual(model.call_count, 2)
        self.assertEqual([e['validation'] for e in self.events()], ['invalid_json', 'model_error'])
        self.assertNotIn('PRIVATE', json.dumps(self.events()))

    def test_disabled_or_unavailable_model_preserves_deterministic_routing(self):
        result, details, model = self.choose([], semantic=False)
        self.assertEqual(result, self.ranked)
        self.assertEqual(details['status'], 'fallback')
        model.assert_not_called()
        result, details, model = self.choose([LLMError('PRIVATE')])
        self.assertEqual(result, self.ranked)
        self.assertEqual(details['status'], 'fallback')
        self.assertEqual(model.call_count, 1)
        self.assertEqual(self.events()[-1]['validation'], 'model_error')

    def test_verified_decider_bypasses_inference(self):
        result, details, model = self.choose([], fixed=[{'strong': True, 'role': 'decides'}])
        self.assertEqual(result, self.ranked)
        self.assertEqual(details['status'], 'fallback')
        model.assert_not_called()

    def model_node(self, results, *, explicit=False, semantic=True):
        did = self.graph.add_decision(self.task, self.question, 'billing', 'pending',
            owner='Synthetic Maintainer', repo=self.repo, path='usage/meter.py')
        required = json.dumps(['Synthetic Approver'])
        self.graph.db.execute('UPDATE decisions SET model_pending=1,required_signers=? WHERE id=?', (required, did))
        current = self.store.get_decision(did)

        def scratch(*args, **kwargs):
            sid = self.graph.add_decision(self.task, self.question, 'billing', 'pending',
                owner='Synthetic Maintainer', repo=self.repo, path='usage/meter.py')
            return {'id': sid, 'drafts': [sid]}

        with patch('bridge.routing.rank_for_decision', return_value=self.ranked), \
                patch('bridge.signals._authority_matches', return_value=[]), \
                patch('bridge.semantic_index.sources', return_value=([], {})), \
                patch.object(Config, 'semantic_retrieval', property(lambda _: semantic)), \
                patch('bridge.contact_reasoning.Client.complete_json', side_effect=results) as model, \
                patch('bridge.ladder.ask', side_effect=scratch), \
                patch('bridge.llm.compose_brief', return_value=''), \
                patch('bridge.canvas._route_signer', return_value=self.ranked) as fallback:
            canvas._model_node_pass(self.store, Config(model_api='none'), did, '', ['usage/meter.py'], [], '',
                current['owner_id'] if explicit else None, '', current['updated_at'])
        view = self.store.get_decision(did)
        self.assertFalse(view['model_pending'])
        self.assertFalse(view['authorized'])
        self.assertEqual(view['required_signers'], required)
        self.assertEqual(json.loads(view['signatures'] or '[]'), [])
        self.assertFalse(self.graph.authority_rows(self.repo))
        self.assertTrue(any(e['kind'] == 'model_read' for e in view['events']), view['events'])
        return view, model, fallback

    def test_canvas_abstention_clears_prior_inferred_owner_without_fallback_or_authority_change(self):
        view, model, fallback = self.model_node([{'candidate': 'c2', 'evidence': ['c1:e1']}] * 2)
        self.assertFalse(view['owner_id'])
        self.assertIn('No evidence-bound first contact', view['owner_evidence'])
        self.assertEqual(model.call_count, 2)
        fallback.assert_not_called()

    def test_canvas_valid_contact_is_pending_and_preserves_required_signers(self):
        view, _, fallback = self.model_node([{'candidate': 'c2', 'evidence': ['c2:e1']}])
        self.assertEqual(view['owner_name'], 'Synthetic Analyst')
        self.assertEqual(view['status'], 'pending')
        fallback.assert_not_called()

    def test_canvas_explicit_owner_and_no_model_paths_keep_existing_route(self):
        for explicit, semantic in ((True, True), (False, False)):
            with self.subTest(explicit=explicit, semantic=semantic):
                view, model, fallback = self.model_node([], explicit=explicit, semantic=semantic)
                self.assertEqual(view['owner_name'], 'Synthetic Maintainer')
                model.assert_not_called()
                fallback.assert_not_called()
