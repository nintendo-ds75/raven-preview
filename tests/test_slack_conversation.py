"""Conversation, durable callbacks and scoped routing learned from real human actions."""
import json
import time
import threading
from unittest.mock import patch
from fixtures import OfflineCase
from test_delivery import DeliveryCase, FakeSlack
from bridge import canvas
from bridge.authz import Actor
from bridge.config import Config
from bridge.delivery import handle_slack_event, SlackTransport
from bridge.llm import LLMError
from bridge.routing import route_ranked
from bridge.store import Store


class ConversationTests(DeliveryCase):
    def setUp(self):
        super().setUp()
        self.n=self.node(self.task())
        self.delivery.deliver_now()
        self.message=self.slack.messages[0]
        # Offline tests patch only model output; no CLI/API access is possible.
        self.cfg=patch('bridge.slack_chat.load',return_value=Config(model_api='none'))
        self.cfg.start();self.addCleanup(self.cfg.stop)
        self.enabled=patch.object(Config,'semantic_retrieval',property(lambda self:True))
        self.enabled.start();self.addCleanup(self.enabled.stop)
        self.addCleanup(self.delivery.close)

    def say(self,text,model=None,who='UWES',event_id=''):
        with patch('bridge.slack_chat.reading',return_value=model or {'kind':'chat','reply':'Tell me more.'}):
            return self.reply(self.message,who,text,event_id)

    def review_source(self, policy):
        task = self.store.get_decision(self.n['node_id'])['run_id']
        with self.graph.transaction():
            parent = self.graph.add_decision(task, 'Which upstream constraint applies?', '', 'pending', owner='Wes Chen')
            self.graph.add_link(self.n['node_id'], parent, 'depends')
        self.store.answer(parent, {'answer': 'Keep the original upstream constraint.'})
        self.say(policy, {'kind': 'answer', 'answer': policy})
        self.say('yes')
        self.assertTrue(self.store.get_decision(self.n['node_id'])['authorized'])
        return parent

    def invalidate_review(self, parent):
        row = self.store.get_decision(parent)
        self.store.answer(parent, {'answer': 'Use the corrected upstream constraint.',
                                  'expected_updated_at': row['updated_at']})
        row = self.store.get_decision(self.n['node_id'])
        self.assertTrue(row['needs_review'])
        self.assertFalse(row['authorized'])
        self.assertEqual(row['signatures'], '[]')

    def test_identical_answer_after_invalidation_gets_fresh_confirmable_readback(self):
        policy = 'Exclude synthetic load tests for this task only. This is not a standing rule.'
        parent = self.review_source(policy)
        self.invalidate_review(parent)
        for repair in ({'kind': 'answer', 'answer': policy}, {'kind': 'signoff'}):
            with self.subTest(kind=repair['kind']):
                with patch('bridge.slack_chat.reading', side_effect=[
                        {'kind': 'chat', 'reply': 'That decision is already recorded and signed.'}, repair]) as model:
                    result = self.reply(self.message, 'UWES', policy)
                self.assertEqual(model.call_count, 2)
                payload = model.call_args.args[1]
                self.assertTrue(payload['needs_review'])
                self.assertFalse(payload['authorized'])
                self.assertEqual(payload['signoff'], 'required')
                self.assertIn(parent, payload['review_reason'])
                self.assertIn('fresh', payload['validation_error'])
                self.assertIn('Reply yes to confirm', result)
                self.assertIn(policy, result)
                self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])
                self.say('yes')
                restored = self.store.get_decision(self.n['node_id'])
                self.assertTrue(restored['authorized'])
                self.assertFalse(restored['needs_review'])
                self.assertEqual(restored['answer'], policy)
                self.assertFalse(restored['reusable'])
                if repair['kind'] == 'answer':
                    # A distinct correction invalidates the next confirmation.
                    row = self.store.get_decision(parent)
                    self.store.answer(parent, {'answer': 'A further corrected constraint.',
                                              'expected_updated_at': row['updated_at']})

    def test_repeated_review_noop_fails_closed_without_reusing_old_approval(self):
        policy = 'Bill two units for this task only.'
        parent = self.review_source(policy)
        self.invalidate_review(parent)
        with patch('bridge.slack_chat.reading', return_value={
                'kind': 'question', 'reply': 'You already have a signed answer.'}) as model:
            result = self.reply(self.message, 'UWES', policy)
        self.assertEqual(model.call_count, 2)
        self.assertIn('Nothing was changed', result)
        self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])
        self.assertIsNone(self.delivery._reading(self.message['channel'], self.message['ts'], self.wes))

    def test_review_question_is_not_forced_into_an_answer(self):
        policy = 'Bill two units.'
        parent = self.review_source(policy)
        self.invalidate_review(parent)
        with patch('bridge.slack_chat.reading', return_value={
                'kind': 'question', 'reply': 'The upstream constraint changed, so fresh review is needed.'}) as model:
            result = self.reply(self.message, 'UWES', 'Why am I being asked to review this again?')
        self.assertEqual(model.call_count, 1)
        self.assertIn('fresh review', result)
        self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])
        self.assertIsNone(self.delivery._reading(self.message['channel'], self.message['ts'], self.wes))

    def test_still_authorized_repeat_does_not_require_new_confirmation(self):
        policy = 'Bill two units.'
        self.review_source(policy)
        with patch('bridge.slack_chat.reading', return_value={
                'kind': 'chat', 'reply': 'The current answer is already signed.'}) as model:
            result = self.reply(self.message, 'UWES', policy)
        self.assertEqual(model.call_count, 1)
        self.assertIn('already signed', result)
        self.assertTrue(self.store.get_decision(self.n['node_id'])['authorized'])
        self.assertIsNone(self.delivery._reading(self.message['channel'], self.message['ts'], self.wes))

    def test_invalidation_retires_a_pending_readback_even_when_answer_text_is_unchanged(self):
        policy = 'Bill two units.'
        parent = self.review_source(policy)
        self.say(policy, {'kind': 'answer', 'answer': policy})
        self.assertIsNotNone(self.delivery._reading(self.message['channel'], self.message['ts'], self.wes))
        self.invalidate_review(parent)
        result = self.say('yes')
        self.assertIn('changed since my read-back', result)
        self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])
        self.assertIsNone(self.delivery._reading(self.message['channel'], self.message['ts'], self.wes))

    def test_old_readback_does_not_revive_after_another_fresh_approval(self):
        policy = 'Bill two units.'
        parent = self.review_source(policy)
        self.say(policy, {'kind': 'answer', 'answer': policy})
        self.invalidate_review(parent)
        row = self.store.get_decision(self.n['node_id'])
        self.store.answer(self.n['node_id'], {'answer': policy, 'expected_updated_at': row['updated_at']})
        before = self.store.get_decision(self.n['node_id'])
        self.assertTrue(before['authorized'])
        result = self.say('yes')
        self.assertIn('changed since my read-back', result)
        after = self.store.get_decision(self.n['node_id'])
        self.assertEqual(after['revision'], before['revision'])
        self.assertEqual(after['signatures'], before['signatures'])

    def test_invalidation_epoch_stays_stable_across_normal_cosignatures(self):
        from bridge.delivery import _what_it_says
        policy = 'Bill two units.'
        parent = self.review_source(policy)
        self.invalidate_review(parent)
        row = self.store.get_decision(self.n['node_id'])
        held_revision = _what_it_says(row)
        self.store.answer(self.n['node_id'], {'answer': policy, 'expected_updated_at': row['updated_at']})
        approved = self.store.get_decision(self.n['node_id'])
        self.assertEqual(_what_it_says(approved), held_revision)
        # An ordinary co-signature changes neither the answer nor the review epoch.
        with self.graph.transaction():
            self.graph.append_event('signature', {'task_id': approved['run_id'],
                                                  'decision_id': approved['id'], 'by': 'Additional approver'})
        self.assertEqual(_what_it_says(self.store.get_decision(approved['id'])), held_revision)

    def test_fresh_readback_cannot_be_issued_by_an_unrelated_person(self):
        policy = 'Bill two units.'
        parent = self.review_source(policy)
        self.invalidate_review(parent)
        with patch('bridge.slack_chat.reading', side_effect=[
                {'kind': 'chat', 'reply': 'It is already signed.'}, {'kind': 'answer', 'answer': policy}]):
            response = self.reply(self.message, 'UMAR', policy)
        self.assertNotIn('Reply yes to confirm', response)
        self.assertIsNone(self.delivery._reading(self.message['channel'], self.message['ts'], self.marisol))
        self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])

    def test_captured_answer_correction_reframe_gets_one_repair(self):
        # Actual Anthropic/MCP Click run: this answer amendment was read back
        # as "Replace the current question with". The old examiner also
        # wrongly confirmed it; that is a separate defect, not authorization.
        policy = ('Export click.DelimitedList as a ParamType subclass. Constructor '
                  'DelimitedList(item_type=click.STRING, *, separator=",", max_items=4). '
                  'separator must be a nonempty string; max_items must be a positive integer excluding bool; '
                  'invalid construction raises ValueError. convert returns a tuple of elements converted by '
                  'click.types.convert_type(item_type). Split strings literally by separator, strip each field, '
                  'and reject empty fields (including an empty input) with BadParameter. Preserve duplicate '
                  'values and order. More than max_items raises BadParameter. An already typed tuple is '
                  'accepted by converting each element, without stringifying it. Any other outer input raises '
                  'BadParameter. No change to Click’s existing parameter types or global parsing. '
                  'This approval is only for this synthetic Northstar local evaluation task, '
                  'not upstream endorsement or a reusable standing rule.')
        old = policy.replace('max_items=4', 'max_items=6')
        self.say(old, {'kind': 'answer', 'answer': old})
        self.say('yes')
        before = self.store.get_decision(self.n['node_id'])
        message = 'I need to correct my earlier answer. The complete replacement decision is: ' + policy
        with patch('bridge.slack_chat.reading', side_effect=[
                {'kind': 'reframe', 'answer': policy}, {'kind': 'answer', 'answer': policy}]) as model:
            offered = self.reply(self.message, 'UWES', message)
        self.assertEqual(model.call_count, 2)
        repair = model.call_args.args[1]
        self.assertIn('not the question', repair['validation_error'])
        self.assertEqual(repair['message'], message)
        self.assertEqual(repair['answer_on_table'], old)
        self.assertEqual(repair['question'], before['question'])
        self.assertIn('Record your decision as:', offered)
        self.assertNotIn('Replace the current question', offered)
        self.assertIn(policy, offered)
        self.assertEqual(self.store.get_decision(self.n['node_id'])['answer'], old)
        held = self.delivery._reading(self.message['channel'], self.message['ts'], self.wes)
        self.assertEqual(json.loads(held['answer'])['kind'], 'answer')
        self.say('yes')
        row = self.store.get_decision(self.n['node_id'])
        self.assertEqual(row['question'], before['question'])
        self.assertEqual(row['answer'], policy)
        self.assertTrue(row['authorized'])
        self.assertEqual(row['signed_by'], 'Wes Chen')
        self.assertFalse(row['reusable'])
        self.assertFalse(any(e['kind'] == 'question_reframed' for e in row['events']))

    def test_reframe_repair_failure_clears_pending_answer(self):
        message = 'Please revise my previous decision: Exclude internal traffic for this task only.'
        invalid = [
            {'kind': 'reframe', 'answer': 'Should we exclude internal traffic?'},
            {'kind': 'signoff'}, {'kind': 'confirm'}, {'kind': 'chat', 'reply': 'OK'},
            {'kind': 'answer', 'answer': ''},
            {'kind': 'answer', 'answer': 'Exclude internal traffic.'},
            LLMError('provider unavailable'),
        ]
        for repair in invalid:
            with self.subTest(repair=repair):
                self.say('Bill everything.', {'kind': 'answer', 'answer': 'Bill everything.'})
                with patch('bridge.slack_chat.reading', side_effect=[
                        {'kind': 'reframe', 'answer': 'Exclude internal traffic.'}, repair]) as model:
                    response = self.reply(self.message, 'UWES', message)
                self.assertEqual(model.call_count, 2)
                self.assertIn('Nothing was changed', response)
                self.assertIsNone(self.delivery._reading(self.message['channel'], self.message['ts'], self.wes))
                self.say('yes', {'kind': 'confirm'})
                self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])
                self.assertFalse(any(e['kind'] == 'question_reframed'
                                     for e in self.store.get_decision(self.n['node_id'])['events']))

    def test_scope_repair_cannot_switch_an_answer_amendment_to_reframe(self):
        self.say('Bill it.', {'kind': 'answer', 'answer': 'Bill it.'})
        with patch('bridge.slack_chat.reading', side_effect=[
                {'kind': 'answer', 'answer': 'Exclude it.'},
                {'kind': 'reframe', 'answer': 'Exclude it for this task only.'}]) as model:
            response = self.reply(self.message, 'UWES',
                'My revised answer is: Exclude it for this task only.')
        self.assertEqual(model.call_count, 2)
        self.assertIn('Nothing was changed', response)
        self.assertIsNone(self.delivery._reading(self.message['channel'], self.message['ts'], self.wes))

    def test_invalid_reframe_repair_preserves_an_already_signed_answer(self):
        self.say('Bill it.', {'kind': 'answer', 'answer': 'Bill it.'})
        self.say('yes')
        before = self.store.get_decision(self.n['node_id'])
        with patch('bridge.slack_chat.reading', return_value={
                'kind': 'reframe', 'answer': 'Exclude it.'}) as model:
            response = self.reply(self.message, 'UWES', 'I want to amend my decision: exclude it.')
        self.assertEqual(model.call_count, 2)
        self.assertIn('Nothing was changed', response)
        self.assertIsNone(self.delivery._reading(self.message['channel'], self.message['ts'], self.wes))
        self.assertIn('no read-back', self.say('yes', {'kind': 'confirm'}))
        row = self.store.get_decision(self.n['node_id'])
        for key in ('question', 'answer', 'authorized', 'signed_by', 'signed_revision', 'signatures'):
            self.assertEqual(row[key], before[key])

    def test_explicit_question_correction_still_reframes_after_confirmation(self):
        self.say('Bill it.', {'kind': 'answer', 'answer': 'Bill it.'})
        self.say('yes')
        question = 'Should only synthetic traffic be excluded?'
        with patch('bridge.slack_chat.reading', return_value={'kind': 'reframe', 'answer': question}) as model:
            response = self.reply(self.message, 'UWES',
                'I need to correct my earlier answer. The question is wrong. Replace the question with: ' + question)
        self.assertEqual(model.call_count, 1)
        self.assertIn('Replace the current question', response)
        self.assertNotEqual(self.store.get_decision(self.n['node_id'])['question'], question)
        self.say('yes')
        row = self.store.get_decision(self.n['node_id'])
        self.assertEqual(row['question'], question)
        self.assertFalse(row['answer'])
        self.assertFalse(row['authorized'])

    def test_answer_amendment_guard_is_narrow_and_message_local(self):
        from bridge.slack_chat import explicit_answer_amendment
        for text in (
                'I want to update my prior decision. Keep retrying, with jitter.',
                'Actually, I amend my answer: retries remain disabled.',
                'My replacement decision is: accept only UTF-8.',
                'Please correct my answer. Do not change the question. Keep the cap.',
                "I need to revise my decision. Don't reframe the question; bill real traffic."):
            with self.subTest(text=text):
                self.assertTrue(explicit_answer_amendment(text))
        for text in (
                'Please correct the question: Which requests count?',
                'My answer was based on the wrong question. Ask which requests count.',
                'The agent quoted: I need to correct my earlier answer.',
                '“I need to correct my earlier answer.” Is this relevant?',
                'Could I amend my answer after seeing the logs?',
                'I do not want to replace my answer.',
                'I need to correct my earlier answer. Reframe the current question: Which requests count?'):
            with self.subTest(text=text):
                self.assertFalse(explicit_answer_amendment(text))
        # An earlier amendment in conversation history must not constrain a
        # later genuine question correction.
        self.say('I need to correct my answer: bill real traffic.', {'kind': 'answer', 'answer': 'Bill real traffic.'})
        with patch('bridge.slack_chat.reading', return_value={
                'kind': 'reframe', 'answer': 'Which traffic is synthetic?'}) as model:
            response = self.reply(self.message, 'UWES', 'Correct the question: Which traffic is synthetic?')
        self.assertEqual(model.call_count, 1)
        self.assertIn('Replace the current question', response)

    def test_faithful_answer_amendment_needs_no_repair(self):
        with patch('bridge.slack_chat.reading', return_value={
                'kind': 'answer', 'answer': 'Bill real traffic only.'}) as model:
            response = self.reply(self.message, 'UWES', 'I amend my earlier answer: bill real traffic only.')
        self.assertEqual(model.call_count, 1)
        self.assertIn('Record your decision as:', response)
        self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])

    def test_repaired_answer_keeps_authority_and_stale_readback_checks(self):
        with patch('bridge.slack_chat.reading', side_effect=[
                {'kind': 'reframe', 'answer': 'Bill real traffic only.'},
                {'kind': 'answer', 'answer': 'Bill real traffic only.'}]):
            response = self.reply(self.message, 'UPRI', 'I amend my answer: bill real traffic only.')
        self.assertIn('Nothing recorded', response)
        self.assertIsNone(self.delivery._reading(self.message['channel'], self.message['ts'], self.priya))
        with patch('bridge.slack_chat.reading', side_effect=[
                {'kind': 'reframe', 'answer': 'Bill real traffic only.'},
                {'kind': 'answer', 'answer': 'Bill real traffic only.'}]):
            self.reply(self.message, 'UWES', 'I amend my answer: bill real traffic only.')
        canvas.settle_node(self.store, {'task_id': self.n['task_id'], 'node_id': self.n['node_id'],
                                      'answer': 'Hold the invoice.', 'rationale': 'New evidence'})
        response = self.say('yes')
        self.assertIn('changed since my read-back', response)
        self.assertEqual(self.store.get_decision(self.n['node_id'])['answer'], 'Hold the invoice.')
        self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])
        self.assertIsNone(self.delivery._reading(self.message['channel'], self.message['ts'], self.wes))

    def test_live_task_only_qualifier_omission_gets_one_repair(self):
        policy = ('For the Northstar edge pilot, use future timestamp tolerance of 17 seconds inclusive only when explicitly configured. '
                  'Never add tolerance to max_age: already-expired tokens stay expired, including when max_age is zero. '
                  'The default is zero and preserves all existing behavior. Timestamps further in the future stay invalid. '
                  'Signature integrity is mandatory. Apply the same checks to rotated/fallback signing keys. '
                  'No internal, test, or staging exemption. This approval is for this task only; it is not a standing rule.')
        omitted = policy.rsplit(' This approval', 1)[0]
        with patch('bridge.slack_chat.reading', side_effect=[
                {'kind': 'answer', 'answer': omitted}, {'kind': 'answer', 'answer': policy}]) as model:
            offered = self.reply(self.message, 'UWES', policy)
        self.assertEqual(model.call_count, 2)
        self.assertIn('task_only', model.call_args.args[1]['validation_error'])
        self.assertIn(policy, offered)
        self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])
        self.say('yes')
        row = self.store.get_decision(self.n['node_id'])
        self.assertEqual(row['answer'], policy)
        self.assertFalse(row['reusable'])

    def test_repeated_scope_omission_discards_old_confirmable_readback(self):
        self.say('Keep the old limit', {'kind': 'answer', 'answer': 'Keep the old limit.'})
        with patch('bridge.slack_chat.reading', return_value={
                'kind': 'answer', 'answer': 'Keep the old limit.'}) as model:
            reply = self.reply(self.message, 'UWES',
                'Keep the old limit for this task only. It is not a reusable rule.')
        self.assertEqual(model.call_count, 2)
        self.assertIn('Nothing was changed', reply)
        self.assertIsNone(self.delivery._reading(self.message['channel'], self.message['ts'], self.wes))
        self.say('yes', {'kind': 'confirm'})
        self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])

    def test_faithful_scope_restrictions_do_not_trigger_a_repair(self):
        text = 'Keep the limit only for this task. This is not a standing rule.'
        answer = 'Keep the limit for this task only. It is not a reusable rule.'
        with patch('bridge.slack_chat.reading', return_value={'kind': 'answer', 'answer': answer}) as model:
            reply = self.reply(self.message, 'UWES', text)
        self.assertEqual(model.call_count, 1)
        self.assertIn(answer, reply)
        self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])

    def test_context_then_answer_amendment_and_confirmation(self):
        with patch('bridge.slack_chat.reading',return_value={'kind':'question','reply':'The task says enterprise-two had an 11x usage spike.'}) as model:
            said=self.reply(self.message,'UWES','Which account was affected?')
            self.assertIn('enterprise-two',said)
            self.assertIn('11x',model.call_args.args[1]['context'])
        self.say('Exclude that one, it was internal testing',{'kind':'answer','answer':'Exclude the spike.','rationale':'internal testing'})
        self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])
        with patch('bridge.slack_chat.reading',return_value={'kind':'answer','answer':'Exclude internal traffic only; bill real customer traffic.'}) as model:
            offered=self.reply(self.message,'UWES','Actually keep the real customer traffic billable')
            self.assertTrue(model.call_args.args[1]['pending_readback'])
            self.assertGreater(len(model.call_args.args[1]['history']),1)
            self.assertIn('real customer',offered)
        self.say('yes',event_id='signed')
        d=self.store.get_decision(self.n['node_id'])
        self.assertTrue(d['authorized']);self.assertIn('bill real',d['answer'])
        self.assertEqual(self.reply(self.message,'UWES','yes',event_id='signed'),'')

    def test_model_cannot_treat_an_amendment_as_confirmation(self):
        self.say('Exclude internal traffic',{'kind':'answer','answer':'Exclude internal traffic.'})
        with patch('bridge.slack_chat.reading',side_effect=[{'kind':'confirm'},
                {'kind':'answer','answer':'Exclude internal traffic for Acme only.'}]) as model:
            ack=self.reply(self.message,'UWES','Actually, only for Acme')
        self.assertEqual(model.call_count,2)
        self.assertIn('Acme only',ack)
        self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])

    def test_ambiguous_amendment_invalidates_the_old_readback(self):
        self.say('Exclude internal traffic',{'kind':'answer','answer':'Exclude internal traffic.'})
        self.say('Actually, only for Acme',{'kind':'confirm'})
        self.assertIsNone(self.delivery._reading(self.message['channel'],self.message['ts'],self.wes))
        self.say('yes',{'kind':'confirm'})
        self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])

    def test_because_in_a_nonanswer_is_not_a_decision(self):
        self.say('I cannot answer because I am away',{'kind':'chat','reply':'No problem. Who could help while you are away?'})
        self.assertEqual(self.store.get_decision(self.n['node_id'])['status'],'pending')

    def test_natural_referral_then_final_answer_learns_only_final_person(self):
        offered=self.say('Marisol is the person for this; I only built the meter',{'kind':'handoff','to':'Marisol Vega'})
        self.assertIn('Marisol Vega',offered)
        self.say('yes')
        self.delivery.deliver_now()
        message=self.slack.messages[-1]
        self.assertEqual(message['channel'],'DUMAR')
        with patch('bridge.slack_chat.reading',return_value={'kind':'answer','answer':'Exclude internal test traffic.'}):
            self.delivery.receive(message['channel'],message['ts'],'UMAR','Leave the internal test traffic out')
        self.delivery.receive(message['channel'],message['ts'],'UMAR','yes')
        feedback=[dict(r) for r in self.graph.db.execute('SELECT * FROM routing_feedback')]
        self.assertEqual({r['person_id']:r['outcome'] for r in feedback}, {self.wes:'declined',self.marisol:'answered'})
        self.assertFalse([a for a in self.graph.authority_rows() if a['person_id']==self.marisol and a['role']=='decides'])

    def test_casual_acknowledgement_does_not_confirm_readback(self):
        self.say('Exclude internal traffic',{'kind':'answer','answer':'Exclude internal traffic.'})
        for text in ['ok','sure','thanks','makes sense']:
            self.say(text,{'kind':'confirm'})
            self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])
        self.say('yes')
        self.assertTrue(self.store.get_decision(self.n['node_id'])['authorized'])

    def test_explicit_full_answer_agreement_requests_readback_before_signing(self):
        canvas.settle_node(self.store, {'task_id':self.n['task_id'],'node_id':self.n['node_id'],
                                      'answer':'Exclude internal traffic.','rationale':'Internal test accounts'})
        self.delivery.deliver_now()
        with patch('bridge.slack_chat.reading',return_value={'kind':'question','reply':'Please clarify.'}) as model:
            ack=self.reply(self.message,'UWES','I confirm that full answer for this task as well.')
        model.assert_not_called()
        self.assertIn('Sign the complete answer',ack)
        self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])
        self.say('yes')
        self.assertTrue(self.store.get_decision(self.n['node_id'])['authorized'])

    def test_agreement_with_a_qualification_still_goes_to_inference(self):
        canvas.settle_node(self.store, {'task_id':self.n['task_id'],'node_id':self.n['node_id'],
                                      'answer':'Exclude internal traffic.','rationale':'Internal test accounts'})
        self.delivery.deliver_now()
        with patch('bridge.slack_chat.reading',return_value={'kind':'answer','answer':'Exclude Acme only.'}) as model:
            ack=self.reply(self.message,'UWES','I confirm that full answer, but for Acme only.')
        model.assert_called_once()
        self.assertIn('Acme only',ack)
        self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])

    def test_stale_readback_and_restart(self):
        self.say('Exclude traffic',{'kind':'answer','answer':'Exclude internal traffic.'})
        other=Store(self.store.path);self.addCleanup(other.graph.close)
        delivery=other.connect_delivery(self.slack)
        with patch('bridge.slack_chat.load',return_value=Config(model_api='none')):
            said=delivery.receive(self.message['channel'],self.message['ts'],'UWES','yes')
        self.assertIn('Recorded',said)

    def test_model_failure_keeps_gate_closed(self):
        with patch('bridge.slack_chat.reading',side_effect=LLMError('offline')):
            said=self.reply(self.message,'UWES','Exclude it please')
        self.assertIn('Nothing was changed',said)
        self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])

    def test_context_is_visible_to_host_and_not_a_signature(self):
        self.say('These are synthetic test accounts',{'kind':'context'})
        tree=canvas.get_tree(self.store,self.store.get_decision(self.n['node_id'])['run_id'])
        self.assertIn('synthetic test accounts',str(tree['notes']))
        self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])

    def test_required_followup_is_explicitly_confirmed(self):
        offered=self.say('Before finishing, find out whether finance signed the exception',
                        {'kind':'followup','answer':'Has finance signed the exception?','required':True})
        self.assertIn('Require an answer',offered)
        self.say('yes')
        child=self.graph.db.execute('SELECT followup_required FROM decisions WHERE parent_id=?',(self.n['node_id'],)).fetchone()
        self.assertEqual(child['followup_required'],1)

    def test_unrelated_person_cannot_confirm_someone_elses_answer(self):
        self.say('Exclude traffic',{'kind':'answer','answer':'Exclude traffic.'})
        said=self.say('yes',{'kind':'signoff'},who='UPRI')
        self.assertIn('Nothing recorded',said)
        self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])

    def test_search_context_is_ephemeral_and_token_not_stored(self):
        self.slack.search_context=lambda query,token:[{'text':'transient-source-canary','url':'https://slack.com/archives/C1/p1','author':'UMAR'}]
        with patch('bridge.slack_chat.reading',return_value={'kind':'question','reply':'transient-source-canary'}):
            handle_slack_event(self.delivery,{'type':'event_callback','team_id':'TTEST','event_id':'search-event','event':{
                'type':'message','channel':self.message['channel'],'thread_ts':self.message['ts'],'user':'UWES',
                'text':'Search Slack for the reason?','action_token':'transient-token-canary'}})
        self.assertIn('transient-source-canary',self.slack.messages[-1]['text'])
        for table in ('intents','slack_conversation','webhook_receipts','slack_replies','routing_feedback'):
            rows=[dict(r) for r in self.graph.db.execute('SELECT * FROM '+table)]
            self.assertNotIn('transient-source-canary',json.dumps(rows))
            self.assertNotIn('transient-token-canary',json.dumps(rows))

    def test_search_replies_name_people_instead_of_slack_ids(self):
        # Measured live: a reply to "what did people say?" read "U0A1B2C3D said ...".
        self.slack.search_context=lambda query,token:[
            {'text':'load tests are excluded','url':'https://slack.com/archives/C1/p2','author':'UMAR'},
            {'text':'agreed','url':'https://slack.com/archives/C1/p3','author':'UOLA'},
            {'text':'no idea','url':'https://slack.com/archives/C1/p4','author':'UZZZZZZZ'}]
        def user_info(uid):
            if uid == 'UWES': return {'id':'UWES','real_name':'Wes Chen','team_id':'TFAKE','profile':{'email':'wes@acme.example'}}
            if uid == 'UOLA': return {'id':'UOLA','real_name':'Ola Berg','team_id':'TFAKE','profile':{'email':'ola@acme.example'}}
            raise RuntimeError('users.info failed: user_not_found')
        self.slack.user_info=user_info
        self.slack.workspace_id=lambda: 'TFAKE'
        with patch('bridge.slack_chat.reading',return_value={'kind':'question',
                'reply':'UMAR said load tests are excluded, UOLA and UZZZZZZZ replied, and <@UWES> agreed.'}) as model:
            handle_slack_event(self.delivery,{'type':'event_callback','team_id':'TFAKE','event_id':'search-names','event':{
                'type':'message','channel':self.message['channel'],'thread_ts':self.message['ts'],'user':'UWES',
                'text':'What did people say about this?','action_token':'short-lived'}})
        posted=self.slack.messages[-1]['text']
        self.assertIn('Marisol Vega said load tests are excluded, Ola Berg and a Slack member replied, and @Wes Chen agreed.',posted)
        self.assertNotIn('UMAR',posted); self.assertNotIn('UZZZZZZZ',posted)
        sources=model.call_args[0][1]['sources']
        self.assertEqual([r['author'] for r in sources],['Marisol Vega','Ola Berg','a Slack member'])

    def test_a_reason_the_person_did_not_give_is_not_recorded(self):
        # Measured live: read-backs carried the answer again as its reason,
        # or a reason the model supplied.
        out=self.say('Exclude the load test from billing.',{'kind':'answer','answer':'Exclude the load test from billing.',
            'rationale':'The load test should be excluded from billing.'})
        self.assertIn('Record your decision as:',out); self.assertNotIn('Reason:',out)
        out=self.say('Exclude the load test from billing.',{'kind':'answer','answer':'Exclude the load test from billing.',
            'rationale':'Enterprise contracts forbid charging for synthetic traffic.'})
        self.assertNotIn('Reason:',out)
        self.say('yes')
        d=self.store.get_decision(self.n['node_id'])
        self.assertTrue(d['authorized'])
        self.assertEqual(d['rationale'],'No reason given in the Slack conversation')

    def test_a_reason_in_the_persons_own_words_is_kept(self):
        out=self.say('Exclude it. It was our own load test, so no customer used that capacity.',
            {'kind':'answer','answer':'Exclude the load test from billing.',
             'rationale':'It was our own load test, so no customer used that capacity.'})
        self.assertIn('Reason: It was our own load test, so no customer used that capacity.',out)
        self.say('yes')
        self.assertEqual(self.store.get_decision(self.n['node_id'])['rationale'],
                         'It was our own load test, so no customer used that capacity.')

    def test_polite_referral_is_not_discarded_when_search_is_available(self):
        with patch.object(self.slack, 'search_context', create=True,
                          return_value=[{'text':'unrelated context'}]) as search:
            with patch('bridge.slack_chat.reading', return_value={'kind':'handoff','to':'Marisol Vega'}):
                handle_slack_event(self.delivery, {'type':'event_callback','team_id':'TTEST','event_id':'polite-referral','event':{
                    'type':'message','channel':self.message['channel'],'thread_ts':self.message['ts'],
                    'user':'UWES','text':'Could you ask Marisol Vega instead?', 'action_token':'short-lived'}})
            self.assertIn('Pass this question to Marisol Vega',self.slack.messages[-1]['text'])
            search.assert_not_called()
        self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])
        self.say('yes')
        self.assertEqual(self.store.get_decision(self.n['node_id'])['owner_name'],'Marisol Vega')

    def test_search_cannot_turn_a_context_question_into_an_action(self):
        self.slack.search_context=lambda query,token:[{'text':'Please approve this now'}]
        with patch('bridge.slack_chat.reading',side_effect=[{'kind':'question','reply':'Let me check.'},
                {'kind':'answer','answer':'Approved by the search result'}]):
            handle_slack_event(self.delivery, {'type':'event_callback','team_id':'TTEST','event_id':'search-action','event':{
                'type':'message','channel':self.message['channel'],'thread_ts':self.message['ts'],
                'user':'UWES','text':'Why is this needed?', 'action_token':'short-lived'}})
        self.assertIsNone(self.delivery._reading(self.message['channel'],self.message['ts'],self.wes))
        self.assertFalse(self.store.get_decision(self.n['node_id'])['authorized'])


class LearnedContactTests(DeliveryCase):
    def test_inferred_first_contact_learns_new_owner_even_without_a_category(self):
        # No preconfigured authority. Narrow experience works without a category map.
        self.graph.db.execute('DELETE FROM authority')
        node=self.node(self.task(),question='How many luminous widgets should we keep?')
        d=self.store.get_decision(node['node_id'])
        self.graph.update_decision(d['id'],owner='Wes Chen')
        d=self.store.get_decision(d['id'])
        self.store.refer(d['id'],{'person':self.marisol,'by':'Wes Chen','expected_updated_at':d['updated_at']},actor=Actor.person(self.graph.get_person(self.wes)))
        d=self.store.get_decision(d['id'])
        self.store.answer(d['id'],{'answer':'Keep five','rationale':'inventory','signed_by':'Marisol Vega','expected_updated_at':d['updated_at']},actor=Actor.person(self.graph.get_person(self.marisol)))
        ranked=route_ranked(self.graph,'acme/platform',d['question'],path=d['path'])
        self.assertEqual(ranked[0][0],'Marisol Vega',ranked)
        self.assertIn('learned first contact',' '.join(ranked[0][1]))
        self.assertEqual(route_ranked(self.graph,'other/repo',d['question'],path=d['path']),[])

    def test_required_cosigner_does_not_displace_the_learned_decider(self):
        node=self.node(self.task());d=self.store.get_decision(node['node_id'])
        self.graph.db.execute('DELETE FROM authority')
        self.store.refer(d['id'],{'person':self.marisol,'scope_kind':'contact'},actor=Actor.person(self.graph.get_person(self.wes)))
        self.store.answer(d['id'],{'answer':'Exclude internal traffic','rationale':'test','signed_by':'Marisol Vega'},actor=Actor.person(self.graph.get_person(self.marisol)))
        self.graph.add_authority('path','billing/*','approves',person_id=self.priya,repo=d['repo'])
        ranked=route_ranked(self.graph,d['repo'],d['question'],path=d['path'])
        self.assertEqual(ranked[0][0],'Marisol Vega')
        sibling=self.node(self.task(title='Follow up on the usage policy'))
        record=self.store.get_decision(sibling['node_id'])
        self.assertEqual(record['owner_name'],'Marisol Vega')
        self.assertIn('Priya Natarajan',record['required_signers'])

    def test_answer_does_not_accept_an_unrelated_repo_referral(self):
        foreign=self.graph.add_authority('repo','','decides',person_id=self.wes,repo='other/repo',source='referral',accepted=False)
        n=self.node(self.task());d=self.store.get_decision(n['node_id'])
        self.store.answer(d['id'],{'answer':'Exclude it','rationale':'internal','signed_by':'Wes Chen','expected_updated_at':d['updated_at']},actor=Actor.person(self.graph.get_person(self.wes)))
        self.assertFalse(next(a for a in self.graph.authority_rows('other/repo') if a['id']==foreign)['accepted'])


class SlackIngressTests(DeliveryCase):
    def test_webhook_enqueue_does_not_wait_for_model_and_survives_restart(self):
        node=self.node(self.task());self.delivery.deliver_now();msg=self.slack.messages[0]
        event={'type':'event_callback','team_id':'TTEST','event_id':'durable','event':{'type':'message','user':'UWES','channel':msg['channel'],
            'thread_ts':msg['ts'],'text':'answer: Exclude it because internal','action_token':'do-not-save-this'}}
        with patch.object(self.delivery.inbox,'start'):
            start=time.monotonic();self.delivery.inbox.enqueue(event)
            self.assertLess(time.monotonic()-start,.2)
            self.delivery.inbox.enqueue(event)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM slack_ingress').fetchone()[0],1)
        self.assertNotIn('do-not-save-this',self.graph.db.execute('SELECT payload FROM slack_ingress').fetchone()[0])
        other=Store(self.store.path);self.addCleanup(other.graph.close)
        worker=other.connect_delivery(self.slack)
        worker.inbox.process()
        self.assertTrue(other.get_decision(node['node_id'])['authorized'])
        count=len(self.slack.messages);worker.inbox.process();self.assertEqual(count,len(self.slack.messages))

    def test_failed_ack_is_retried_without_reapplying_the_answer(self):
        node=self.node(self.task());self.delivery.deliver_now();msg=self.slack.messages[0]
        with patch.object(self.slack,'post_message',side_effect=RuntimeError('network')):
            handle_slack_event(self.delivery,{'type':'event_callback','team_id':'TTEST','event_id':'retry-ack','event':{'type':'message',
                'user':'UWES','channel':msg['channel'],'thread_ts':msg['ts'],'text':'answer: Exclude it because internal'}})
        before=self.store.get_decision(node['node_id'])['updated_at']
        self.graph.db.execute('UPDATE slack_replies SET next_attempt=0')
        self.delivery.inbox.flush()
        self.assertEqual(before,self.store.get_decision(node['node_id'])['updated_at'])
        self.assertEqual(self.graph.db.execute('SELECT state FROM slack_replies').fetchone()[0],'sent')

    def test_bot_search_uses_only_supported_public_scope_and_action_token(self):
        transport=SlackTransport('test-bot')
        with patch.object(transport,'_call',return_value={'results':{'messages':[{'author_user_id':'UWES','content':'hi','permalink':'https://slack.com/archives/C/p1'}]}}) as api:
            self.assertEqual(transport.search_context('why',''),[])
            result=transport.search_context('why','short-lived')
        self.assertEqual(api.call_args.args[0],'assistant.search.context')
        self.assertEqual(api.call_args.args[1]['channel_types'],['public_channel'])
        self.assertEqual(api.call_args.args[1]['action_token'],'short-lived')
        self.assertEqual(result[0]['author'],'UWES')


class DurabilityTests(DeliveryCase):
    def test_search_retry_does_not_post_a_second_response(self):
        from bridge.slack_chat import EphemeralReply
        self.delivery.inbox.ack('search', 'D1', '1.0', EphemeralReply('private-search-canary'))
        self.delivery.inbox.ack('search', 'D1', '1.0', 'Search response not retained')
        self.assertEqual(len(self.slack.messages), 1)
        self.assertNotIn('private-search-canary', str(dict(self.graph.db.execute('SELECT * FROM slack_replies').fetchone())))

    def test_two_reply_workers_do_not_send_the_same_ack(self):
        barrier=threading.Event(); release=threading.Event()
        original=self.slack.post_message
        def delayed(*a,**kw):
            barrier.set(); release.wait(3); return original(*a,**kw)
        self.slack.post_message=delayed
        worker=threading.Thread(target=lambda:self.delivery.inbox.ack('one','D1','1','Saved'))
        worker.start(); self.assertTrue(barrier.wait(2))
        try: self.delivery.inbox.flush()
        finally: release.set(); worker.join(3)
        self.assertEqual(len(self.slack.messages),1)

    def test_expired_event_recovers_and_later_reply_waits(self):
        node=self.node(self.task()); self.delivery.deliver_now(); msg=self.slack.messages[0]
        def event(eid,text):
            return {'type':'event_callback','team_id':'TTEST','event_id':eid,'event':{'type':'message','channel':msg['channel'],
                    'thread_ts':msg['ts'],'user':'UWES','text':text}}
        with patch.object(self.delivery.inbox,'start'):
            self.delivery.inbox.enqueue(event('a','answer: Exclude it because internal'))
            self.delivery.inbox.enqueue(event('b','thanks'))
        self.graph.db.execute("UPDATE slack_ingress SET state='processing',lease_until=? WHERE id='a'",(time.time()+100,))
        self.assertEqual(self.delivery.inbox.process(),0)
        self.graph.db.execute("UPDATE slack_ingress SET lease_until=0 WHERE id='a'")
        self.assertEqual(self.delivery.inbox.process(),2)
        self.assertTrue(self.store.get_decision(node['node_id'])['authorized'])

    def test_exhausted_event_is_visible_and_retryable(self):
        with patch.object(self.delivery.inbox,'start'):
            self.delivery.inbox.enqueue({'type':'event_callback','team_id':'TTEST','event_id':'broken','event':{'type':'message','channel':'D1','text':'hello'}})
        self.graph.db.execute("UPDATE slack_ingress SET state='failed',attempts=5,error='RuntimeError' WHERE id='broken'")
        self.assertEqual(self.delivery.inbound_failed()[0]['id'],'broken')
        with patch.object(self.delivery.inbox,'start'):
            self.assertEqual(self.delivery.retry_inbound('broken')['state'],'queued')


class ScopeLearningTests(DeliveryCase):
    def test_this_question_only_teaches_no_reusable_contact(self):
        n=self.node(self.task());d=self.store.get_decision(n['node_id'])
        self.store.refer(d['id'],{'person':self.marisol,'scope_kind':'none'},actor=Actor.person(self.graph.get_person(self.wes)))
        self.store.answer(d['id'],{'answer':'Exclude it','rationale':'test','signed_by':'Marisol Vega'},actor=Actor.person(self.graph.get_person(self.marisol)))
        self.assertFalse(self.graph.db.execute("SELECT 1 FROM routing_feedback").fetchone())
        self.assertFalse([a for a in self.graph.authority_rows() if a['person_id']==self.marisol])

    def test_learning_respects_customer_and_topic(self):
        from bridge.routing_memory import candidates
        n=self.node(self.task());d=self.store.get_decision(n['node_id'])
        self.graph.db.execute("UPDATE decisions SET facts='customer=Acme' WHERE id=?",(d['id'],))
        self.store.answer(d['id'],{'answer':'Exclude it','rationale':'test','signed_by':'Wes Chen'},actor=Actor.person(self.graph.get_person(self.wes)))
        self.assertTrue(candidates(self.graph,d['repo'],d['question'],context='facts: customer=Acme'))
        self.assertFalse(candidates(self.graph,d['repo'],d['question'],context='facts: customer=Globex'))
        self.assertFalse(candidates(self.graph,d['repo'],d['question']))
        self.assertFalse(candidates(self.graph,d['repo'],'Should we encrypt credit cards?',context='facts: customer=Acme'))

    def test_cosigner_does_not_replace_owner_as_learned_contact(self):
        n=self.node(self.task());d=self.store.get_decision(n['node_id'])
        self.graph.db.execute('UPDATE decisions SET required_signers=? WHERE id=?',(json.dumps(['Priya Natarajan']),d['id']))
        canvas.settle_node(self.store,{'task_id':d['run_id'],'node_id':d['id'],'answer':'Exclude traffic','rationale':'proposal'})
        canvas.sign_off(self.store,d['id'],{'by':'Wes Chen','expected_updated_at':self.store.get_decision(d['id'])['updated_at']},actor=Actor.person(self.graph.get_person(self.wes)))
        canvas.sign_off(self.store,d['id'],{'by':'Priya Natarajan','expected_updated_at':self.store.get_decision(d['id'])['updated_at']},actor=Actor.person(self.graph.get_person(self.priya)))
        rows=self.graph.db.execute("SELECT person_id FROM routing_feedback WHERE outcome='answered'").fetchall()
        self.assertEqual([r['person_id'] for r in rows],[self.wes])


class InferenceIsolationTests(OfflineCase):
    def test_cli_cannot_use_coding_tools_or_keep_search_sessions(self):
        import subprocess
        from bridge.llm import _claude_cli
        with patch('bridge.llm.find_claude',return_value='/fake/claude'), patch('subprocess.run',return_value=subprocess.CompletedProcess([],0,stdout='{}')) as run:
            _claude_cli('system','untrusted Slack text','haiku')
        args=run.call_args.args[0]
        self.assertEqual(args[args.index('--tools')+1],'')
        self.assertIn('--strict-mcp-config',args)
        self.assertIn('--no-session-persistence',args)
        self.assertEqual(run.call_args.kwargs['input'],'untrusted Slack text')

class ReplySchemaTests(OfflineCase):
    def test_unused_null_fields_do_not_discard_a_valid_reply(self):
        from bridge.slack_chat import reading
        with patch('bridge.slack_chat.Client.complete_json',return_value={'kind':'question','reply':'This preserves compatibility.','answer':None,'to':None}):
            got=reading(Config(model_api='none'),{'message':'Why?'})
        self.assertEqual(got['reply'],'This preserves compatibility.')
        self.assertEqual(got['answer'],'')
