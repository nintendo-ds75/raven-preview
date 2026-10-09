"""Human wording survives real parsing, readback and confirmation boundaries.

Only model output is replaced. No provider or live messages are used.
"""
import json
from unittest.mock import patch

from bridge import canvas, llm
from bridge.approval_scope import transport_text
from bridge.config import Config
from bridge.human_answers import (ANSWER_FORM_GUIDANCE, ANSWER_INTERPRETATION_REQUEST,
                                  COMPLETE_ANSWER_REQUEST, INLINE_RATIONALE,
                                  CompleteAnswerRequired, preserve_complete_answer)
from fixtures import OfflineCase
from test_delivery import DeliveryCase


# The observed human wording is evidence for a regression, never a product prompt.
A006_ORIGINAL = (
    'For Orion’s G1 staging canary, hold rollout until G1 preserves trustworthy actual written counts. '
    'No v1 fallback or compatibility exception is authorized for this header-evidence case.\n'
    'This policy protects acceptance evidence for this canary and is limited to the missing-header condition. '
    'It grants no production-change or data-replay approval.'
)
A006_LOSSY = (
    'Hold rollout through G1 until G1 reliably emits all three '
    'X-Prometheus-Remote-Write-*-Written headers on 204. '
    'No v1 fallback or compatibility exception is authorized for this header-evidence case. '
    'This policy is limited to the missing-header condition and grants no production-change or data-replay approval.'
)


class AuthoredAnswerTests(DeliveryCase):
    def setUp(self):
        super().setUp()
        self.node_id = self.node(self.task(), category='compat')['node_id']
        self.graph.publish_evidence(self.node_id, [], status='assumed', source='assumption',
                                    answer='Preserve existing behavior until a human decides.',
                                    kind='evidence', signoff='required')
        self.delivery.deliver_now()
        self.message = self.slack.messages[0]
        self.addCleanup(self.delivery.close)
        enabled = patch.object(Config, 'semantic_retrieval', property(lambda self: True))
        enabled.start(); self.addCleanup(enabled.stop)
        cfg = patch('bridge.slack_chat.load', return_value=Config(model_api='none'))
        cfg.start(); self.addCleanup(cfg.stop)

    def row(self):
        return self.store.get_decision(self.node_id)

    def held(self):
        return self.delivery._reading(self.message['channel'], self.message['ts'], self.wes)

    def offer(self, text, action=None, event_id=''):
        action = action or {'kind': 'answer', 'answer_form': 'complete'}
        with patch('bridge.slack_chat.Client.complete_json', return_value=action) as model:
            result = self.reply(self.message, 'UWES', text, event_id)
        return result, model

    def confirm(self, held=None, event_id=''):
        held = held or self.held()
        return self.reply(self.message, 'UWES', 'confirm ' + held['proposal_id'], event_id)

    def test_observed_condition_and_reason_survive_lossy_model_answer_on_assumed_node(self):
        before = self.row()
        self.assertEqual(before['status'], 'assumed')
        self.assertFalse(before['authorized'])
        result, model = self.offer(A006_ORIGINAL, {'kind': 'answer', 'answer_form': 'complete',
                                                   'answer': A006_LOSSY, 'rationale': ''})
        model.assert_called_once()
        self.assertIn(transport_text(A006_ORIGINAL), result)
        self.assertNotIn('reliably emits all three', result)
        held = self.held()
        action = json.loads(held['answer'])
        self.assertEqual(action['answer'], A006_ORIGINAL)
        self.assertEqual(action['original'], A006_ORIGINAL)
        self.assertTrue(action['authored_verbatim'])
        self.assertEqual(self.row()['answer'], before['answer'])
        self.assertFalse(self.row()['authorized'])
        self.confirm(held)
        after = self.row()
        self.assertEqual(after['answer'], A006_ORIGINAL)
        self.assertEqual(after['rationale'], INLINE_RATIONALE)
        self.assertEqual(after['signed_by'], 'Wes Chen')
        self.assertTrue(after['authorized'])
        self.assertFalse(after['reusable'])
        event = next(e for e in after['events'] if e['kind'] == 'readback_confirmed')
        proof = json.loads(event['detail'])
        self.assertEqual(json.loads(proof['proposal']['answer'])['original'], A006_ORIGINAL)

    def test_complete_answer_keeps_negation_exception_and_inline_reason_without_model_extraction(self):
        text = ('Do not charge for rejected requests, except retries that completed successfully.\n'
                'This preserves accurate invoices. No future exception or production change is authorized.')
        result, _ = self.offer(text, {'kind': 'answer', 'answer_form': 'complete',
                                     'answer': 'Charge retries.', 'rationale': 'Invented contractual obligation.'})
        self.assertIn(transport_text(text), result)
        self.assertNotIn('Invented contractual', result)
        self.confirm()
        self.assertEqual(self.row()['answer'], text)
        self.assertEqual(self.row()['rationale'], INLINE_RATIONALE)

    def test_partial_amendment_cannot_replace_complete_answer_or_confirm_old_proposal(self):
        self.offer('Exclude rejected requests; bill completed customer requests.')
        original = self.row()['answer']
        result, model = self.offer('Actually keep that exception, except for trial accounts.',
                                  {'kind': 'answer', 'answer_form': 'partial', 'answer': 'A synthesized replacement.'})
        model.assert_called_once()
        self.assertEqual(result, COMPLETE_ANSWER_REQUEST)
        self.assertIsNone(self.held())
        self.reply(self.message, 'UWES', 'yes')
        self.assertEqual(self.row()['answer'], original)
        self.assertFalse(self.row()['authorized'])
        replacement = 'Exclude rejected requests and all trial traffic; bill other completed customer requests.'
        self.offer(replacement)
        self.confirm()
        self.assertEqual(self.row()['answer'], replacement)

    def test_repair_for_partial_amendment_does_not_request_model_composition(self):
        self.offer('Exclude rejected requests; bill completed customer requests.')
        message = 'Please amend my answer: except for trial accounts.'
        with patch('bridge.slack_chat.Client.complete_json', side_effect=[
                {'kind': 'reframe', 'answer': 'Which accounts are trials?'},
                {'kind': 'answer', 'answer_form': 'partial'}]) as model:
            result = self.reply(self.message, 'UWES', message)
        self.assertEqual(model.call_count, 2)
        repair = json.loads(model.call_args.args[2])
        self.assertIn('answer_form=partial', repair['validation_error'])
        self.assertIn('Do not compose one', repair['validation_error'])
        self.assertEqual(repair['message'], message)
        self.assertEqual(result, COMPLETE_ANSWER_REQUEST)
        self.assertIsNone(self.held())
        self.assertFalse(self.row()['authorized'])

    def test_mixed_answer_referral_and_private_aside_are_not_signed_as_one_message(self):
        before = self.row()
        for text in ('Hold this rollout. Ask Marisol to decide the next one.',
                     'Exclude rejected requests. Privately, what did the customer tell you?',
                     'My colleague wrote "ship everything"; I am not adopting that suggestion.'):
            with self.subTest(text=text):
                result, _ = self.offer(text, {'kind': 'answer', 'answer_form': 'mixed'})
                self.assertEqual(result, COMPLETE_ANSWER_REQUEST)
                self.assertIsNone(self.held())
                self.assertEqual(self.row()['answer'], before['answer'])
                self.assertFalse(self.row()['authorized'])
                self.assertEqual(self.row()['owner_id'], before['owner_id'])

    def test_contradictory_routing_fields_cannot_mark_a_mixed_message_complete(self):
        for field, value in (('to', 'Marisol Vega'), ('conditions', 'customer=Acme'),
                             ('scope_kind', 'any'), ('expires', '2099-01-01'), ('required', True)):
            with self.subTest(field=field):
                result, _ = self.offer('Hold rollout and ask someone else about future reuse.',
                                      {'kind': 'answer', 'answer_form': 'complete', field: value})
                self.assertEqual(result, ANSWER_INTERPRETATION_REQUEST)
                self.assertIsNone(self.held())
                self.assertFalse(self.row()['authorized'])

    def test_question_and_private_context_keep_their_nonanswer_paths(self):
        for text, kind in (('Why was the request rejected?', 'question'),
                           ('I cannot decide yet; keep this discussion private.', 'chat')):
            with self.subTest(kind=kind):
                result, _ = self.offer(text, {'kind': kind, 'reply': 'Let us review the current evidence.'})
                self.assertIn('No decision or sign-off recorded', result)
                self.assertIsNone(self.held())
                self.assertFalse(self.row()['authorized'])
        self.reply(self.message, 'UWES', 'context: The rejection log needs checking.')
        self.assertIsNone(self.held())
        self.assertFalse(self.row()['authorized'])

    def test_rule_request_does_not_become_an_answer_or_create_unsigned_authority(self):
        result, _ = self.offer('Make this the rule for later requests.',
                              {'kind': 'rule', 'conditions': 'customer=Acme', 'scope_kind': 'none'})
        self.assertFalse(self.row()['authorized'])
        self.assertFalse(self.row()['reusable'])
        if self.held():
            self.assertEqual(json.loads(self.held()['answer'])['kind'], 'rule')
        self.assertNotIn('Record your decision as:', result)

    def test_missing_answer_classification_gets_one_repair_without_paraphrase_fallback(self):
        text = 'Hold the rollout until measured error counts are trustworthy. This preserves audit evidence.'
        with patch('bridge.slack_chat.Client.complete_json', side_effect=[
                {'kind': 'answer', 'answer': 'Emit the error counter.'},
                {'kind': 'answer', 'answer_form': 'complete', 'answer': 'Emit the error counter.'}]) as model:
            result = self.reply(self.message, 'UWES', text)
        self.assertEqual(model.call_count, 2)
        self.assertIn('answer_form', json.loads(model.call_args.args[2])['validation_error'])
        self.assertIn(transport_text(text), result)
        self.assertEqual(json.loads(self.held()['answer'])['answer'], text)

    def test_oversized_verbatim_answer_never_becomes_a_short_confirmable_summary(self):
        text = 'Preserve these literal delimiters: ' + ('<>&' * 3900)
        result, _ = self.offer(text, {'kind': 'answer', 'answer_form': 'complete', 'answer': 'Preserve exceptions.'})
        self.assertIn('too large', result)
        self.assertIsNone(self.held())
        self.assertFalse(self.row()['authorized'])

    def test_current_revision_and_duplicate_callback_gates_cover_verbatim_answers(self):
        text = 'Exclude rejected requests for this task only.'
        result, _ = self.offer(text, event_id='same-human-answer')
        held = self.held()
        retry, model = self.offer(text, event_id='same-human-answer')
        model.assert_not_called()
        self.assertEqual(self.held(), held)
        canvas.settle_node(self.store, {'task_id': self.row()['run_id'], 'node_id': self.node_id,
                                       'answer': 'A newer proposal.', 'rationale': 'Changed evidence'})
        refused = self.confirm(held)
        self.assertIn('changed', refused)
        self.assertEqual(self.row()['answer'], 'A newer proposal.')
        self.assertFalse(self.row()['authorized'])

    def test_compatibility_readback_and_confirmation_store_complete_original(self):
        text = 'Hold until actual counts are trustworthy. This protects acceptance evidence; never infer counts.'
        with patch('bridge.slack_chat.respond', return_value=None), \
                patch('bridge.llm.Client.complete_json', return_value={
                    'kind': 'answer', 'answer_form': 'complete', 'confident': True,
                    'answer': 'Emit a count field.'}):
            result = self.reply(self.message, 'UWES', text)
            self.assertIn(transport_text(text), result)
            self.assertEqual(self.held()['answer'], text)
            self.assertFalse(self.row()['authorized'])
            self.confirm()
        self.assertEqual(self.row()['answer'], text)
        self.assertTrue(self.row()['authorized'])
        self.assertFalse(self.row()['reusable'])

    def test_repeated_missing_form_clears_old_readback_without_applying_model_text(self):
        self.offer('Hold rollout for this task only.')
        with patch('bridge.slack_chat.Client.complete_json', return_value={
                'kind': 'answer', 'answer': 'A model-written replacement.'}) as model:
            result = self.reply(self.message, 'UWES', 'Keep complete trustworthy counts; no exceptions.')
        self.assertEqual(model.call_count, 2)
        self.assertIn('Nothing was changed', result)
        self.assertIsNone(self.held())
        self.assertFalse(self.row()['authorized'])

    def test_long_allowed_answer_preserves_internal_whitespace_unicode_and_late_limit(self):
        text = ('Preserve trustworthy counts for café usage.\n\n' + 'Recorded line.\t' * 300
                + '\nDo not ship an exception 🧪. This applies to this task only.')
        result, _ = self.offer(text)
        self.assertIn(transport_text(text), result)
        self.confirm()
        self.assertEqual(self.row()['answer'], text)
        self.assertTrue(self.row()['authorized'])

    def refusals(self):
        return [json.loads(event['detail']) for event in self.row()['events']
                if event['kind'] == 'answer_readback_refused']

    def test_primary_prompt_keeps_related_constraints_in_one_complete_replacement(self):
        text = ('For this maintenance window, retain rejected rows until the recorded counts reconcile.\n'
                'This preserves auditability. Permit no exception for retries. '
                'This answer does not authorize deleting customer data or changing production access.')
        result, model = self.offer(text)
        system = model.call_args.args[1]
        self.assertIn(ANSWER_FORM_GUIDANCE, system)
        self.assertIn('INDEPENDENT request', system)
        self.assertIn('For kind=rule, use scope_kind=none', system)
        self.assertIn('and required=false', system)
        self.assertNotIn('An amendment is an answer with answer_form=partial', system)
        self.assertEqual(json.loads(model.call_args.args[2])['message'], text)
        self.assertIn(transport_text(text), result)
        self.assertEqual(json.loads(self.held()['answer'])['answer'], text)
        self.assertFalse(self.row()['authorized'])
        self.assertEqual(self.refusals(), [])
        self.confirm()
        self.assertEqual(self.row()['answer'], text)
        self.assertFalse(self.row()['reusable'])

    def test_complete_control_conflict_has_bounded_diagnostic_without_private_values(self):
        before = self.row()
        secret = 'model-private-control-value-73ad'
        result, model = self.offer('Hold this change for this task only.', {
            'kind': 'answer', 'answer_form': 'complete', 'scope_kind': 'none',
            'conditions': secret, 'explanation': secret, 'rationale': secret})
        model.assert_called_once()
        self.assertEqual(result, ANSWER_INTERPRETATION_REQUEST)
        self.assertNotIn('partial answer', result)
        self.assertIsNone(self.held())
        after = self.row()
        for key in ('answer', 'status', 'signoff', 'signed_by', 'authorized', 'reusable', 'signatures', 'context_history'):
            self.assertEqual(after.get(key), before.get(key), key)
        details = self.refusals()
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0], {'decision_id': self.node_id, 'task_id': before['run_id'],
            'reason': 'conflicting_control_fields', 'answer_form': 'complete',
            'conflicting_fields': ['conditions', 'scope_kind']})
        self.assertNotIn(secret, json.dumps(details))
        self.assertNotIn('Hold this change', json.dumps(details))

    def test_partial_and_independent_requests_have_distinct_non_authorizing_diagnostics(self):
        messages = [('partial', 'Keep that part, except for trial accounts.'),
                    ('mixed', 'Hold this change. Ask Marisol to decide the next rollout.'),
                    ('mixed', 'Hold this change. Make my answer reusable for later customers.'),
                    ('mixed', 'Hold this change. Privately, what did the customer disclose?')]
        for form, text in messages:
            with self.subTest(text=text):
                result, _ = self.offer(text, {'kind': 'answer', 'answer_form': form})
                self.assertEqual(result, COMPLETE_ANSWER_REQUEST)
                self.assertIsNone(self.held())
                detail = self.refusals()[-1]
                self.assertEqual(detail['reason'], form)
                self.assertEqual(detail['answer_form'], form)
                self.assertEqual(detail['conflicting_fields'], [])
                self.assertNotIn(text, json.dumps(detail))
                self.assertFalse(self.row()['authorized'])
                self.assertFalse(self.row()['reusable'])

    def test_duplicate_refusal_callback_does_not_add_diagnostic_or_confirmation(self):
        action = {'kind': 'answer', 'answer_form': 'complete', 'required': True}
        self.offer('Hold this change.', action, event_id='one-refused-reply')
        before = self.refusals()
        _, model = self.offer('Hold this change.', action, event_id='one-refused-reply')
        model.assert_not_called()
        self.assertEqual(self.refusals(), before)
        self.assertIsNone(self.held())
        self.assertFalse(self.row()['authorized'])

    def test_compatibility_conflict_records_same_bounded_reason(self):
        text = 'Retain these rows only for the current task. This protects the audit record.'
        with patch('bridge.slack_chat.respond', return_value=None), \
                patch('bridge.llm.Client.complete_json', return_value={
                    'kind': 'answer', 'answer_form': 'complete', 'confident': True,
                    'conditions': 'private-model-condition-value'}):
            result = self.reply(self.message, 'UWES', text)
        self.assertEqual(result, ANSWER_INTERPRETATION_REQUEST)
        self.assertIsNone(self.held())
        detail = self.refusals()[-1]
        self.assertEqual(detail['reason'], 'conflicting_control_fields')
        self.assertEqual(detail['conflicting_fields'], ['conditions'])
        self.assertNotIn('private-model-condition-value', json.dumps(detail))
        self.assertFalse(self.row()['authorized'])

    def test_rule_control_fields_remain_on_the_rule_path(self):
        from bridge.slack_chat import reading
        action = {'kind': 'rule', 'conditions': 'customer=Acme', 'scope_kind': 'none',
                  'expires': '2099-01-01'}
        with patch('bridge.slack_chat.Client.complete_json', return_value=action):
            result = reading(Config(model_api='none'), {'message': 'Make this my rule only for this task.'})
        self.assertEqual(result['kind'], 'rule')
        for key in ('conditions', 'scope_kind', 'expires'):
            self.assertEqual(result[key], action[key])
        self.assertFalse(self.row()['authorized'])
        self.assertFalse(self.row()['reusable'])


class CompatibilityAuthoredAnswerTests(OfflineCase):
    def read(self, text, action):
        with patch.object(Config, 'semantic_retrieval', property(lambda self: True)), \
                patch('bridge.llm.Client.complete_json', return_value=action) as model:
            result = llm.read_reply(Config(model_api='none'), 'May this proceed?', 'Old proposal.', text)
        return result, model

    def test_compatibility_classifier_cannot_rewrite_or_truncate_human_answer(self):
        text = ('Keep complete measured counts, including zero counts. ' * 35
                + 'Never infer a successful write from a response status. This preserves acceptance evidence.')
        result, model = self.read(text, {'kind': 'answer', 'confident': True, 'answer_form': 'complete',
                                        'answer': 'Require a success status.', 'rationale': ''})
        self.assertEqual(result['answer'], text)
        self.assertEqual(result['original'], text)
        self.assertIn(text, model.call_args.args[2])

    def test_complete_result_normalizes_kind_and_discards_unused_model_fields(self):
        text = 'Hold until the measurement is trustworthy.'
        result, _ = self.read(text, {'kind': ' Answer ', 'answer_form': 'complete', 'confident': True,
                                    'to': None, 'rationale': None, 'unsupported_model_field': 'discard me'})
        self.assertEqual(result, {'kind': 'answer', 'answer_form': 'complete', 'answer': text,
                                  'rationale': '', 'original': text, 'authored_verbatim': True})

    def test_compatibility_partial_or_mixed_answer_is_not_confirmable(self):
        for form in ('partial', 'mixed'):
            with self.subTest(form=form):
                result, _ = self.read('Keep that part but ask Marisol about the rest.',
                                      {'kind': 'answer', 'confident': True, 'answer_form': form})
                self.assertEqual(result, {'kind': 'clarify_answer', 'refusal': {
                    'reason': form, 'answer_form': form, 'conflicting_fields': []}})

    def test_compatibility_missing_form_and_overlimit_input_fail_closed(self):
        result, _ = self.read('Hold rollout.', {'kind': 'answer', 'confident': True, 'answer': 'Ship.'})
        self.assertEqual(result, {})
        result, model = self.read('x' * 12001, {'kind': 'answer', 'confident': True, 'answer_form': 'complete'})
        self.assertEqual(result, {'kind': 'clarify_answer', 'refusal': {
            'reason': 'oversized_message', 'answer_form': '', 'conflicting_fields': []}})
        model.assert_not_called()

    def test_compatibility_prompt_uses_same_complete_related_clause_contract(self):
        text = ('Do not bill failed requests, including retries. Bill completed requests only.\n'
                'This preserves accurate invoices and grants no refund or access-policy exception.')
        result, model = self.read(text, {'kind': 'answer', 'confident': True, 'answer_form': 'complete'})
        self.assertIn(ANSWER_FORM_GUIDANCE, model.call_args.args[1])
        self.assertIn('Related conditions and reasons do not themselves make a message mixed', model.call_args.args[1])
        self.assertIn(text, model.call_args.args[2])
        self.assertEqual(result['answer'], text)
        self.assertEqual(result['rationale'], '')

    def test_refusal_metadata_never_copies_unknown_values_or_fields(self):
        refusal = CompleteAnswerRequired('private-unrecognized-reason', answer_form='private-form',
                                          fields=['unknown-secret-field', 'to', 'to', 'scope', {'secret': 'value'}])
        self.assertEqual(refusal.diagnostic(), {'reason': 'unclassified', 'answer_form': '',
                                                'conflicting_fields': ['to', 'scope']})
        self.assertEqual(str(refusal), ANSWER_INTERPRETATION_REQUEST)
        malformed = CompleteAnswerRequired(['private'], answer_form={'private': 1}, fields='private')
        self.assertEqual(malformed.diagnostic(), {'reason': 'unclassified', 'answer_form': '', 'conflicting_fields': []})

    def test_invalid_and_oversized_messages_have_separate_bounded_reasons(self):
        for text, reason in ((None, 'invalid_message'), ('  ', 'invalid_message'),
                             (['private-text'], 'invalid_message'), ('x' * 12001, 'oversized_message')):
            with self.subTest(reason=reason, text_type=type(text).__name__):
                with self.assertRaises(CompleteAnswerRequired) as caught:
                    preserve_complete_answer({'kind': 'answer', 'answer_form': 'complete'}, text)
                self.assertEqual(caught.exception.diagnostic(), {
                    'reason': reason, 'answer_form': 'complete', 'conflicting_fields': []})
                self.assertEqual(str(caught.exception), ANSWER_INTERPRETATION_REQUEST)
