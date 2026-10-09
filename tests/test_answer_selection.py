"""Answer-purpose consent uses real synthetic callbacks, never a live provider.

The two captured classifier mistakes below are regression evidence. They do not
teach a classifier or grant authority; only a person's explicit selection of a
fully displayed answer can consume the held proposal.
"""
import copy
import json
from unittest.mock import patch

from bridge import canvas, readback
from bridge.approval_scope import transport_text
from bridge.config import Config
from bridge.delivery import handle_slack_event
from bridge.human_answers import INLINE_RATIONALE
from test_delivery import DeliveryCase


# Exact boundary_outputs[0].action and native_result.original from
# product-classifier-003-results/report.json, mixed_reusable_grant and
# mixed_separate_followup. Both observed outputs incorrectly said "complete".
CAPTURED_COMPLETE_ACTION = {
    'answer': '', 'answer_form': 'complete', 'conditions': '', 'contact_outcome': '',
    'expires': '', 'kind': 'answer', 'rationale': '', 'reply': '', 'required': False,
    'scope': '', 'scope_kind': '', 'to': '',
}
CAPTURED_BASE = (
    'For Orion’s G1 staging canary, hold rollout until G1 preserves trustworthy actual written counts. '
    'No v1 fallback or compatibility exception is authorized for this header-evidence case.\n'
    'This policy protects acceptance evidence for this canary and is limited to the missing-header condition. '
    'It grants no production-change or data-replay approval.'
)
CAPTURED_MIXED = {
    'mixed_reusable_grant': CAPTURED_BASE + '\n\nAlso create a reusable rule from this answer for future Orion '
        'staging canaries with missing written-count headers.',
    'mixed_separate_followup': CAPTURED_BASE + '\n\nSeparately, ask the coding agent to investigate whether '
        'gateway G2 can support a different customer’s production migration. Open a separate follow-up for that question.',
}


class AnswerSelectionTests(DeliveryCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.delivery.close)
        self.node_id = self.node(self.task())['node_id']
        self.graph.publish_evidence(self.node_id, [], status='assumed', source='assumption',
            answer='Preserve existing behavior until a human decides.', kind='evidence', signoff='required')
        self.delivery.deliver_now()
        self.message = self.slack.messages[0]
        self.counter = 0
        for replacement in (
            patch.object(Config, 'semantic_retrieval', property(lambda self: True)),
            patch('bridge.slack_chat.load', return_value=Config(model_api='none')),
            patch.object(self.delivery.inbox, 'start'),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def row(self, node_id=None):
        return self.store.get_decision(node_id or self.node_id)

    def held(self, message=None, person=None):
        message = message or self.message
        return self.delivery._reading(message['channel'], message['ts'], person or self.wes)

    def message_for(self, node_id):
        note = self.graph.db.execute("SELECT external_ref FROM notifications WHERE decision_id=? "
            "AND person_id=? AND state='sent' ORDER BY sent_at DESC,created_at DESC LIMIT 1",
            (node_id, self.wes)).fetchone()
        self.assertIsNotNone(note)
        channel, thread = note['external_ref'].split(':', 1)
        return {'channel': channel, 'ts': thread}

    def another_node(self, title, question):
        with patch.object(Config, 'semantic_retrieval', property(lambda self: False)):
            node_id = self.node(self.task(title), question=question)['node_id']
            canvas.wait_for_background(10)
        self.store.notify(node_id, 'ask')
        self.delivery.deliver_now()
        return node_id, self.message_for(node_id)

    def event(self, text, *, message=None, user='UWES', stamp=None):
        self.counter += 1
        message = message or self.message
        return {'type': 'event_callback', 'team_id': 'TTEST', 'event_id': f'answer-selection-{self.counter}',
            'event': {'type': 'message', 'user': user, 'text': text, 'channel': message['channel'],
                'thread_ts': message['ts'], 'ts': stamp or f'2000000000.{self.counter:06d}'}}

    def send(self, text, **options):
        event = self.event(text, **options)
        handle_slack_event(self.delivery, event)
        self.last_event = event
        return self.slack.messages[-1]['text']

    def direct(self, text, occurrence, *, user='UWES'):
        self.counter += 1
        return self.delivery.receive(self.message['channel'], self.message['ts'], user, text,
            event_id=f'answer-selection-direct-{self.counter}', occurrence=occurrence)

    def offer(self, text='Exclude internal load tests for this task only.', action=None, **options):
        with patch('bridge.slack_chat.Client.complete_json', return_value=copy.deepcopy(
                action if action is not None else {'kind': 'answer', 'answer_form': 'complete'})) as model:
            result = self.send(text, **options)
        model.assert_called_once()
        held = self.held(options.get('message'))
        self.assertIsNotNone(held, result)
        self.assertTrue(held['delivered_ref'])
        return held

    def select(self, held=None, **options):
        held = held or self.held(options.get('message'))
        return self.send('confirm answer ' + held['proposal_id'], **options)

    def confirmations(self):
        return [json.loads(row['detail']) for row in self.graph.db.execute(
            "SELECT detail FROM events WHERE kind='readback_confirmed' ORDER BY id")]

    def assert_unapplied(self, before, node_id=None):
        after = self.row(node_id)
        for key in ('answer', 'rationale', 'status', 'signoff', 'signed_by', 'authorized',
                    'reusable', 'owner_id', 'signatures'):
            self.assertEqual(after.get(key), before.get(key), key)

    def assert_answer_prompt(self, held, text):
        self.assertEqual(held['selection_kind'], 'answer_only')
        self.assertIn(transport_text(text), held['prompt'])
        self.assertIn('answer only', held['prompt'])
        self.assertIn('does not create a reusable rule', held['prompt'])
        self.assertIn('refer the question', held['prompt'])
        self.assertIn('follow-up', held['prompt'])
        self.assertIn('confirm answer ' + held['proposal_id'], held['prompt'])
        self.assertIn('answer:', held['prompt'])
        self.assertIn('review page', held['prompt'])

    def test_captured_mixed_clean_complete_outputs_need_exact_answer_only_selection(self):
        for case_id, text in CAPTURED_MIXED.items():
            with self.subTest(case=case_id):
                before = self.row()
                count = self.graph.db.execute('SELECT count(*) FROM decisions').fetchone()[0]
                held = self.offer(text, CAPTURED_COMPLETE_ACTION)
                self.assert_answer_prompt(held, text)
                payload = json.loads(held['answer'])
                self.assertEqual(payload['answer'], text)
                self.assertEqual(payload['original'], text)
                self.assertTrue(payload['authored_verbatim'])
                prior_confirmations = self.confirmations()
                for generic in ('yes', 'confirm', 'confirm ' + held['proposal_id'], 'yes ' + held['proposal_id']):
                    with self.subTest(generic=generic):
                        response = self.send(generic)
                        self.assertIn('confirm answer ' + held['proposal_id'], response)
                        self.assert_unapplied(before)
                        self.assertEqual(self.held(), held)
                        self.assertEqual(self.confirmations(), prior_confirmations)
                self.select(held)
                after = self.row()
                self.assertEqual(after['answer'], text)
                self.assertEqual(after['rationale'], INLINE_RATIONALE)
                self.assertTrue(after['authorized'])
                self.assertFalse(after['reusable'])
                self.assertEqual(after['owner_id'], before['owner_id'])
                self.assertEqual(self.graph.db.execute('SELECT count(*) FROM decisions').fetchone()[0], count)
                self.assertFalse(self.graph.db.execute('SELECT 1 FROM decisions WHERE parent_id=?',
                    (self.node_id,)).fetchone())
                proof = self.confirmations()[-1]
                self.assertEqual(proof['selection_kind'], 'answer_only')
                self.assertEqual(json.loads(proof['proposal']['answer'])['original'], text)
                self.assertEqual(proof['proposal']['prompt'], held['prompt'])
                # Start the next capture from a fresh, delivered current review.
                self.store.notify(self.node_id, 'signoff', to='Wes Chen')
                self.delivery.deliver_now()
                self.message = self.message_for(self.node_id)

    def test_simple_complete_answer_needs_one_readback_and_one_selection(self):
        text = 'Do not bill rejected requests. This preserves accurate invoices.'
        before_messages = len(self.slack.messages)
        held = self.offer(text, {'kind': 'answer', 'answer_form': 'complete',
            'answer': 'Invented model answer.', 'rationale': 'Invented model rationale.'})
        self.assert_answer_prompt(held, text)
        self.assertNotIn('Invented model', held['prompt'])
        self.assertEqual(len(self.slack.messages), before_messages + 1)
        self.assertFalse(self.row()['authorized'])
        self.select(held)
        self.assertTrue(self.row()['authorized'])
        self.assertEqual(self.row()['answer'], text)
        self.assertIsNone(self.held())
        self.assertEqual(len(self.confirmations()), 1)
        self.assertEqual(len(self.slack.messages), before_messages + 2)

    def test_compatibility_answer_is_marked_and_keeps_full_human_text(self):
        text = CAPTURED_MIXED['mixed_reusable_grant']
        with patch('bridge.slack_chat.respond', return_value=None), \
                patch('bridge.llm.Client.complete_json', return_value={**CAPTURED_COMPLETE_ACTION, 'confident': True}):
            self.send(text)
            held = self.held()
            self.assert_answer_prompt(held, text)
            self.assertEqual(held['answer'], text)
            self.send('confirm ' + held['proposal_id'])
            self.assertFalse(self.row()['authorized'])
            self.select(held)
        self.assertEqual(self.row()['answer'], text)
        self.assertTrue(self.row()['authorized'])
        self.assertFalse(self.row()['reusable'])
        self.assertEqual(self.confirmations()[-1]['proposal']['answer'], text)

    def test_bound_bare_confirmation_and_signoff_aliases_do_not_select_an_answer(self):
        held = self.offer()
        before = self.row()
        for alias in ('yes', 'confirm', 'sign off', 'approve', 'lgtm'):
            self.counter += 1
            stamp = f'2000000000.{self.counter:06d}'
            response = self.direct(alias, {'platform': 'slack', 'id': stamp, 'timestamp': stamp,
                'reply_to': held['delivered_ref']})
            self.assertIn('confirm answer ' + held['proposal_id'], response)
            self.assert_unapplied(before)
            self.assertEqual(self.held(), held)
        self.assertEqual(self.confirmations(), [])

    def test_wrong_person_and_wrong_code_cannot_select_owner_answer(self):
        held = self.offer()
        before = self.row()
        self.select(held, user='UMAR')
        self.assert_unapplied(before)
        wrong = '0' * 12 if held['proposal_id'] != '0' * 12 else '1' * 12
        self.send('confirm answer ' + wrong)
        self.assert_unapplied(before)
        self.assertEqual(self.held(), held)
        self.assertEqual(self.confirmations(), [])
        self.select(held)
        self.assertTrue(self.row()['authorized'])

    def test_selection_code_cannot_cross_decisions(self):
        held = self.offer()
        other, other_message = self.another_node('Second independent invoice', 'Exclude another load test?')
        other_before = self.row(other)
        other_held = self.offer('Exclude only the second internal load test.', message=other_message)
        self.select(held, message=other_message)
        self.assert_unapplied(other_before, other)
        self.assertEqual(self.held(other_message), other_held)
        self.assertFalse(self.row()['authorized'])
        self.assertEqual(self.confirmations(), [])

    def test_invalid_and_stale_occurrences_cannot_select_current_answer(self):
        held = self.offer()
        before = self.row()
        source = json.loads(held['source_occurrence'])
        for occurrence in (None, {}, {**source, 'timestamp': 'not-a-time'}, source,
                           {**source, 'id': 'older', 'timestamp': '1900000000.000001'}):
            with self.subTest(occurrence=occurrence):
                self.direct('confirm answer ' + held['proposal_id'], occurrence)
                self.assert_unapplied(before)
                self.assertEqual(self.held(), held)
        self.assertEqual(self.confirmations(), [])

    def test_changed_revision_refuses_old_selection(self):
        held = self.offer()
        canvas.settle_node(self.store, {'task_id': self.row()['run_id'], 'node_id': self.node_id,
            'answer': 'A newer unsigned proposal.', 'rationale': 'Changed evidence.'})
        before = self.row()
        response = self.select(held)
        self.assertIn('changed', response.lower())
        self.assert_unapplied(before)
        self.assertEqual(self.confirmations(), [])

    def test_duplicate_callbacks_record_one_selection_and_cannot_replay_into_replacement(self):
        held = self.offer()
        original = copy.deepcopy(self.last_event)
        with patch('bridge.slack_chat.Client.complete_json') as model:
            handle_slack_event(self.delivery, original)
        model.assert_not_called()
        self.assertEqual(self.held(), held)
        self.select(held)
        selected = copy.deepcopy(self.last_event)
        proof = self.confirmations()
        handle_slack_event(self.delivery, selected)
        self.assertEqual(self.confirmations(), proof)
        self.store.notify(self.node_id, 'signoff', to='Wes Chen')
        self.delivery.deliver_now()
        self.message = self.message_for(self.node_id)
        replacement = self.offer('Do not bill any requests for this one task.')
        handle_slack_event(self.delivery, selected)
        self.assertEqual(self.held(), replacement)
        self.assertEqual(self.confirmations(), proof)
        self.assertEqual(self.row()['answer'], 'Exclude internal load tests for this task only.')

    def test_decline_and_replacement_retire_previous_selection_codes(self):
        first = self.offer()
        self.send('decline ' + first['proposal_id'])
        self.assertIsNone(self.held())
        self.select(first)
        self.assertFalse(self.row()['authorized'])
        second = self.offer('Bill completed customer requests for this task only.')
        self.assertNotEqual(first['proposal_id'], second['proposal_id'])
        third = self.offer('Exclude trial traffic; bill other completed requests.')
        self.select(second)
        self.assertEqual(self.held(), third)
        self.assertFalse(self.row()['authorized'])
        self.select(third)
        self.assertEqual(self.row()['answer'], 'Exclude trial traffic; bill other completed requests.')
        self.assertEqual(len(self.confirmations()), 1)

    def test_long_answer_falls_back_without_a_truncated_confirmable_hold(self):
        text = 'Preserve the complete customer wording: ' + '<>&' * 3900
        with patch('bridge.slack_chat.Client.complete_json', return_value={
                'kind': 'answer', 'answer_form': 'complete', 'answer': 'Short model summary.'}):
            response = self.send(text)
        self.assertIn('too large', response)
        self.assertIn('Raven', response)
        self.assertIsNone(self.held())
        self.assertFalse(self.row()['authorized'])
        self.assertEqual(self.confirmations(), [])

    def test_failed_delivery_and_pre_delivery_confirmation_cannot_select_answer(self):
        self.slack.fail = True
        with patch('bridge.slack_chat.Client.complete_json', return_value={
                'kind': 'answer', 'answer_form': 'complete'}):
            self.send('Exclude internal load tests.', stamp='2000000001.000001')
        held = self.held()
        self.assertFalse(held['delivered_ref'])
        self.select(held, stamp='2000000002.000001')
        self.assertFalse(self.row()['authorized'])
        self.slack.fail = False
        original = self.slack.post_message
        def delivered_later(*args, **kwargs):
            original(*args, **kwargs)
            self.slack.messages[-1]['ts'] = '2000000004.000001'
            return '2000000004.000001'
        self.graph.db.execute('UPDATE slack_replies SET next_attempt=0')
        with patch.object(self.slack, 'post_message', side_effect=delivered_later):
            self.delivery.inbox.flush()
        self.select(held, stamp='2000000003.000001')
        self.assertFalse(self.row()['authorized'])
        self.assertEqual(self.confirmations(), [])
        self.select(held, stamp='2000000005.000001')
        self.assertTrue(self.row()['authorized'])

    def test_nonanswer_followup_keeps_generic_confirmation_and_rejects_answer_mode(self):
        held = self.offer('Please ask the agent whether a synthetic ceiling would help.', {
            'kind': 'followup', 'answer': 'Would a synthetic usage ceiling help?', 'required': False})
        self.assertEqual(held['selection_kind'], '')
        self.assertIn('confirm ' + held['proposal_id'], held['prompt'])
        self.select(held)
        self.assertEqual(self.held(), held)
        self.assertEqual(self.confirmations(), [])
        self.assertFalse(self.graph.db.execute('SELECT 1 FROM decisions WHERE parent_id=?', (self.node_id,)).fetchone())
        self.send('confirm ' + held['proposal_id'])
        child = self.graph.db.execute('SELECT * FROM decisions WHERE parent_id=?', (self.node_id,)).fetchone()
        self.assertIsNotNone(child)
        self.assertEqual(child['question'], 'Would a synthetic usage ceiling help?')
        self.assertFalse(self.row()['authorized'])
        self.assertEqual(self.confirmations()[-1]['selection_kind'], '')

    def test_nonanswer_signoff_keeps_generic_confirmation(self):
        held = self.offer('I approve the complete answer already shown.', {'kind': 'signoff'})
        self.assertEqual(held['selection_kind'], '')
        self.select(held)
        self.assertFalse(self.row()['authorized'])
        self.send('confirm ' + held['proposal_id'])
        self.assertTrue(self.row()['authorized'])
        self.assertEqual(self.row()['answer'], 'Preserve existing behavior until a human decides.')

    def test_legacy_unmarked_answer_requires_fresh_purpose_readback(self):
        held = self.offer()
        self.graph.db.execute("UPDATE reply_readings SET selection_kind='' WHERE proposal_id=?", (held['proposal_id'],))
        legacy = self.held()
        readback.migrate(self.graph.db)
        readback.migrate(self.graph.db)
        self.assertEqual(self.held(), legacy)
        for command in ('confirm ', 'confirm answer '):
            response = self.send(command + held['proposal_id'])
            self.assertIn('fresh review', response)
            self.assertFalse(self.row()['authorized'])
            self.assertEqual(self.held(), legacy)
        fresh = self.offer()
        self.assertNotEqual(fresh['proposal_id'], held['proposal_id'])
        self.select(fresh)
        self.assertTrue(self.row()['authorized'])

    def test_old_schema_migration_is_idempotent_and_preserves_other_holds(self):
        answer = self.offer()
        other, other_message = self.another_node('Another independent task', 'Which currency should invoices use?')
        followup = self.offer('Please investigate invoice rounding separately.', {
            'kind': 'followup', 'answer': 'How should invoice rounding work?', 'required': False}, message=other_message)
        before = {row['proposal_id']: dict(row) for row in self.graph.db.execute('SELECT * FROM reply_readings')}
        self.graph.db.execute('ALTER TABLE reply_readings DROP COLUMN selection_kind')
        # Upgrade starts on a fresh connection, without the old process's prepared queries.
        self.graph.close()
        readback.migrate(self.graph.db)
        readback.migrate(self.graph.db)
        after = {row['proposal_id']: dict(row) for row in self.graph.db.execute('SELECT * FROM reply_readings')}
        for proposal, prior in before.items():
            self.assertEqual(after[proposal], {**prior, 'selection_kind': ''})
        self.select(answer)
        self.assertFalse(self.row()['authorized'])
        self.send('confirm ' + followup['proposal_id'], message=other_message)
        self.assertIsNotNone(self.graph.db.execute('SELECT 1 FROM decisions WHERE parent_id=?', (other,)).fetchone())

    def test_queued_preupgrade_generic_confirmation_cannot_authorize_old_answer(self):
        held = self.offer()
        self.graph.db.execute("UPDATE reply_readings SET selection_kind='' WHERE proposal_id=?", (held['proposal_id'],))
        event = self.event('confirm ' + held['proposal_id'])
        self.delivery.inbox.enqueue(event)
        readback.migrate(self.graph.db)
        readback.migrate(self.graph.db)
        self.assertEqual(self.delivery.inbox.process(), 1)
        queued = self.graph.db.execute('SELECT state FROM slack_ingress WHERE id=?', (event['event_id'],)).fetchone()
        self.assertEqual(queued['state'], 'done')
        self.assertFalse(self.row()['authorized'])
        self.assertEqual(self.confirmations(), [])
        self.assertEqual(self.held()['selection_kind'], '')

    def test_queued_selection_after_revision_change_does_not_rebind(self):
        held = self.offer()
        event = self.event('confirm answer ' + held['proposal_id'])
        self.delivery.inbox.enqueue(event)
        canvas.settle_node(self.store, {'task_id': self.row()['run_id'], 'node_id': self.node_id,
            'answer': 'A newer unsigned proposal.', 'rationale': 'Changed while queued.'})
        before = self.row()
        self.assertEqual(self.delivery.inbox.process(), 1)
        self.assert_unapplied(before)
        self.assertEqual(self.confirmations(), [])

    def test_preupgrade_queued_answer_gets_fresh_explicit_selection_when_delivered(self):
        text = 'Exclude only internal load tests for this one invoice.'
        event = self.event(text)
        self.delivery.inbox.enqueue(event)
        self.graph.db.execute('ALTER TABLE reply_readings DROP COLUMN selection_kind')
        # A queued pre-upgrade callback is processed after the service restarts.
        self.graph.close()
        readback.migrate(self.graph.db)
        readback.migrate(self.graph.db)
        with patch('bridge.slack_chat.Client.complete_json', return_value={
                'kind': 'answer', 'answer_form': 'complete'}) as model:
            self.assertEqual(self.delivery.inbox.process(), 1)
        model.assert_called_once()
        held = self.held()
        self.assert_answer_prompt(held, text)
        self.assertEqual(held['source_event_id'], event['event_id'])
        self.assertEqual(json.loads(held['source_occurrence'])['timestamp'], event['event']['ts'])
        self.send('confirm ' + held['proposal_id'])
        self.assertFalse(self.row()['authorized'])
        self.select(held)
        self.assertTrue(self.row()['authorized'])
        self.assertEqual(self.row()['answer'], text)

    def source(self, body='Exclude internal load tests.', **extra):
        return self.store.add_record({'repo': 'acme/platform', 'kind': 'jira', 'ref': 'BILL-1',
            'title': 'Billing policy', 'body': body, 'author': 'Source Writer', 'status': 'Done',
            'url': 'https://example.invalid/BILL-1', 'paths': ['billing/usage.py'], **extra})

    def setup_source(self):
        original = self.source()
        source = dict(self.graph.db.execute('SELECT i.*,s.id AS record_id,s.head_id AS source_version_id '
            'FROM intents i JOIN source_records s ON s.intent_id=i.id WHERE s.id=?',
            (original['source']['record_id'],)).fetchone())
        self.graph.publish_evidence(self.node_id, [source], status='resolved', source='record',
            answer='Exclude internal load tests.', kind='evidence', signoff='required')
        canvas.sign_off(self.store, self.node_id, {'by': 'Wes Chen', 'expected_updated_at': self.row()['updated_at']})
        changed = self.source('Bill customer traffic; exclude only internal load tests.', source_version='external-v2')
        self.delivery.deliver_now()
        return changed

    def test_source_answer_selection_displays_and_consumes_complete_current_review(self):
        changed = self.setup_source()
        text = 'Exclude only internal load tests for this task.'
        self.send('answer: ' + text)
        held = self.held()
        self.assert_answer_prompt(held, text)
        self.assertTrue(held['source_review'])
        displayed = json.loads(held['source_review'])
        self.assertEqual(displayed['source_evidence'], self.row()['source_revalidation']['pins'])
        self.assertIn('Bill customer traffic; exclude only internal load tests.', held['prompt'])
        self.assertIn(changed['source']['source_version_id'], held['prompt'])
        self.assertIn(self.row()['question'], held['prompt'])
        self.send('confirm ' + held['proposal_id'])
        self.assertFalse(self.row()['authorized'])
        self.select(held)
        self.assertTrue(self.row()['authorized'])
        self.assertEqual(self.row()['answer'], text)
        self.assertEqual(self.confirmations()[-1]['proposal']['source_review'], held['source_review'])

    def test_source_change_after_readback_refuses_exact_answer_selection(self):
        self.setup_source()
        self.send('answer: Exclude only internal load tests for this task.')
        held = self.held()
        self.source('The source changed again after the review was displayed.', source_version='external-v3')
        response = self.select(held)
        self.assertIn('changed', response.lower())
        self.assertFalse(self.row()['authorized'])
        self.assertEqual(self.confirmations(), [])

    def test_explicit_source_answer_edit_replaces_hold_without_model_composition(self):
        self.setup_source()
        self.send('answer: Exclude internal load tests for this task.')
        old = self.held()
        replacement = 'Exclude internal load tests and rejected customer requests for this task.'
        with patch('bridge.slack_chat.Client.complete_json') as model:
            self.send('answer: ' + replacement)
        model.assert_not_called()
        current = self.held()
        self.assertNotEqual(current['proposal_id'], old['proposal_id'])
        self.assertEqual(current['answer'], replacement)
        self.assert_answer_prompt(current, replacement)
        self.assertFalse(self.row()['authorized'])
        self.select(old)
        self.assertEqual(self.held(), current)
        self.assertFalse(self.row()['authorized'])
        self.select(current)
        self.assertTrue(self.row()['authorized'])
        self.assertEqual(self.row()['answer'], replacement)

    def test_oversized_current_source_review_has_no_confirmable_answer_hold(self):
        self.setup_source()
        self.source('The complete source must remain visible. ' + 'source detail ' * 1000,
            source_version='external-long')
        response = self.send('answer: Exclude internal load tests for this task.')
        self.assertIn('too large', response)
        self.assertIn('/#runs/', response)
        self.assertIsNone(self.held())
        self.assertFalse(self.row()['authorized'])
        self.assertEqual(self.confirmations(), [])

    def test_incomplete_legacy_source_scope_is_not_upgraded_by_answer_selection(self):
        self.setup_source()
        self.send('answer: Exclude only internal load tests for this task.')
        held = self.held()
        incomplete = json.loads(held['source_review'])
        incomplete['decision'].pop('facts')
        self.graph.db.execute('UPDATE reply_readings SET source_review=? WHERE proposal_id=?',
            (json.dumps(incomplete), held['proposal_id']))
        response = self.select(held)
        self.assertIn('complete decision scope', response)
        self.assertFalse(self.row()['authorized'])
        self.assertEqual(self.confirmations(), [])

