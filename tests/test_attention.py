"""The attention page: only what waits on this person and why it reached
them, what Raven settled from their own calls, and their calls that changed."""
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import attention, canvas, judgment, ladder
from bridge.authz import Actor
from bridge.config import Config


REPO = 'background/policy'
QUESTION = 'How long should policy audit records remain?'
ANSWER = 'Keep policy audit records for thirty days.'


class AttentionTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store('attention.db')
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.store.add_owner({'name': 'Policy Owner', 'team': 'Policy', 'patterns': 'policy/*'})
        self.store.add_owner({'name': 'Billing Owner', 'team': 'Billing', 'patterns': 'billing/*'})
        self.owner = Actor(id=self.store.add_person({'name': 'Policy Owner'})['id'], name='Policy Owner')
        self.other = Actor(id=self.store.add_person({'name': 'Billing Owner'})['id'], name='Billing Owner')
        self.graph.set_setting('auto_rules', '1')
        for method in ('complete', 'complete_json'):
            guard = patch('bridge.llm.Client.' + method, side_effect=AssertionError('Provider calls forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def node(self, question=QUESTION, owner='Policy Owner', path='policy/retention.py', title='Review audit retention'):
        task = self.store.add_run({'title': title, 'repo': REPO, 'agent': 'Claude Code'})['id']
        did = self.graph.add_decision(task, question, 'policy', 'pending', repo=REPO, owner=owner, path=path)
        return task, did

    def precedent(self):
        task, did = self.node()
        self.store.answer(did, {'answer': ANSWER, 'signed_by': 'Policy Owner', 'source': 'inbox: Policy Owner'})
        self.store.make_rule(did, {'by': 'Policy Owner', 'scope': 'any',
                                   'expected_updated_at': self.store.get_decision(did)['updated_at']})
        self.store.update_run(task, {'status': 'completed'})
        return task, did

    def settle_from_memory(self, did):
        self.graph.db.execute('UPDATE decisions SET model_pending=1 WHERE id=?', (did,))
        current = self.store.get_decision(did)
        with patch('bridge.ladder.ask', side_effect=ladder.ask):
            canvas._model_node_pass(self.store, Config(model_api='none'), did, '',
                                    ['policy/retention.py'], [], '', current['owner_id'], '', current['updated_at'])

    def test_nothing_waits_on_a_person_with_no_open_calls(self):
        view = attention.overview(self.store, self.owner)
        self.assertEqual(view['needs_you'], [])
        self.assertEqual(view['handled'], [])
        self.assertEqual(view['changed'], [])

    def test_only_the_calls_waiting_on_this_person_with_why_they_reached_them(self):
        task, mine = self.node()
        _, theirs = self.node(question='Which plan gets the discount?', owner='Billing Owner', path='billing/plans.py')
        view = attention.overview(self.store, self.owner)
        self.assertEqual([x['id'] for x in view['needs_you']], [mine])
        item = view['needs_you'][0]
        self.assertEqual(item['task'], 'Review audit retention')
        self.assertIn('No earlier call covers this', item['why'])
        self.assertIn('keeps building', item['meanwhile'])
        self.assertEqual(view['counts']['asked_elsewhere'], 1)
        self.assertEqual([x['id'] for x in attention.overview(self.store, self.other)['needs_you']], [theirs])

    def test_handled_lists_what_raven_settled_from_this_persons_calls(self):
        _, source = self.precedent()
        task, did = self.node()
        self.settle_from_memory(did)
        self.assertTrue(self.store.get_decision(did)['authorized'])
        view = attention.overview(self.store, self.owner)
        self.assertEqual(view['needs_you'], [])
        self.assertEqual(len(view['handled']), 1)
        handled = view['handled'][0]
        self.assertEqual(handled['id'], did)
        self.assertEqual(handled['call']['id'], source)
        self.assertEqual(handled['call']['by'], 'Policy Owner')
        self.assertEqual(handled['call']['answered_via'], 'browser')
        self.assertIn('nobody needed to be asked', handled['fit_reason'])
        # Not the billing owner's judgment: nothing for them.
        self.assertEqual(attention.overview(self.store, self.other)['handled'], [])
        # The finished precedent task itself is not "being built".
        self.assertEqual(view['counts']['live_tasks'], 1)

    def test_a_correction_shows_under_your_calls_that_changed(self):
        _, source = self.precedent()
        task, did = self.node()
        self.settle_from_memory(did)
        judgment.correct(self.store, did, {'answer': 'Keep them for ninety days.', 'why': 'The auditors asked for ninety.',
                                           'scope': 'everywhere', 'expected_updated_at': self.store.get_decision(did)['updated_at']},
                         actor=self.owner)
        view = attention.overview(self.store, self.owner)
        self.assertEqual(view['handled'], [])
        self.assertEqual(len(view['changed']), 1)
        change = view['changed'][0]
        self.assertTrue(change['mine'] and change['was_mine'])
        self.assertEqual(change['replaced']['id'], source)
        self.assertEqual(change['why'], 'The auditors asked for ninety.')
        self.assertTrue(change['now_precedent'])
        self.assertEqual(attention.overview(self.store, self.other)['changed'], [])

    def test_what_the_source_left_open_is_ravens_note_not_the_persons_words(self):
        """Measured on the Harborline dry run: a card quoted "The earlier
        answer does not establish: Why is the cursor-based paging method
        necessary..." as Raj's words. The gaps travel beside the answer."""
        _, source = self.precedent()
        task, did = self.node()
        self.settle_from_memory(did)
        self.graph.publish_evidence(did, [], status='partial', source='memory', kind='evidence',
                                    answer=ANSWER + ' The earlier answer does not establish: Whether the thirty days '
                                    'count from the last read; what happens to records on legal hold.',
                                    answered_by='Policy Owner', signoff='rule', signed_by='Policy Owner',
                                    source_id=source, source_revision=self.store.get_decision(source)['updated_at'])
        handled = attention.overview(self.store, self.owner)['handled'][0]
        self.assertEqual(handled['answer'], ANSWER)
        self.assertNotIn('does not establish', handled['answer'])
        self.assertEqual(handled['gaps'], ['Whether the thirty days count from the last read',
                                           'what happens to records on legal hold'])
        _, open_call = self.node(question='How long should billing audit records remain?')
        self.graph.publish_evidence(open_call, [], status='proposed', source='record', kind='prediction',
                                    answer='Thirty days. The records do not establish: whether billing differs.',
                                    prediction='Thirty days. The records do not establish: whether billing differs.')
        item = next(i for i in attention.overview(self.store, self.owner)['needs_you'] if i['id'] == open_call)
        self.assertEqual(item['guess'], 'Thirty days.')
        self.assertEqual(item['gaps'], ['whether billing differs'])

    def test_an_unsigned_guess_says_raven_is_not_sure(self):
        task, did = self.node()
        self.graph.db.execute("UPDATE decisions SET kind='prediction', prediction=? WHERE id=?", (ANSWER, did))
        item = attention.overview(self.store, self.owner)['needs_you'][0]
        self.assertEqual(item['guess'], ANSWER)
        self.assertIn('likely answer', item['why'])

    def test_the_area_a_person_decides_for_is_said_plainly(self):
        self.assertEqual(attention._area('verified: Priya Shah decides for pricing decisions (this one speaks of pricing) (config): Plans'),
                         'you decide for pricing decisions')
        self.assertEqual(attention._area(''), '')

    def test_a_handed_on_call_says_who_handed_it_and_their_word(self):
        # Parcelwise rehearsal (Nov 2): Priya handed the Bloom & Co call to
        # Sam with a note; Sam's card explained it with the review reason
        # instead of Priya's words.
        task, did = self.node()
        self.store.refer(did, {'person': 'Billing Owner', 'note': 'You know their order form.',
                               'expected_updated_at': self.store.get_decision(did)['updated_at']}, actor=self.owner)
        self.graph.db.execute("UPDATE decisions SET needs_review=1, review_reason='stale' WHERE id=?", (did,))
        item = attention.overview(self.store, self.other)['needs_you'][0]
        self.assertEqual(item['why'], 'Policy Owner handed this to you: “You know their order form.”')
        self.assertEqual(attention._handed_on('handed on by Priya Shah; once you answer, Raven routes customer decisions to you first'),
                         'Priya Shah handed this to you')
        self.assertEqual(attention._handed_on('verified: Priya Shah decides for pricing decisions'), '')

    def test_the_closest_earlier_call_is_dated_the_way_people_say_it(self):
        self.assertEqual(attention._day('2026-10-28T19:05:00+00:00'), 'Oct 28')
        self.assertEqual(attention._day(''), '')
        source = {'answered_by': 'Jordan Okafor', 'signed_by': '', 'created_at': '2026-08-20T16:40:00+00:00'}
        row = {'needs_review': 0, 'kind': 'prediction'}
        self.assertIn('from Aug 20,', attention._why_reached(row, source, 'Priya Shah'))

    def test_a_needs_item_carries_the_source_review_its_answer_binds(self):
        self.node()
        item = attention.overview(self.store, self.owner)['needs_you'][0]
        self.assertEqual(set(item['review']), {'has_reliance', 'available', 'pins', 'decision_pins'})
        self.assertFalse(item['review']['has_reliance'])
