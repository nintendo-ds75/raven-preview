"""Synthetic transport regressions for occurrence-bound human read-back approval.

Only local fixture transports and mocked interpretation are used. A callback's
identity, source ordering, proposal generation, and delivered message must all
agree before a human confirmation can authorize the held interpretation.
"""
import copy
import json
import time
import threading
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
import test_delivery as slack_fixtures
import test_teams as teams_fixtures
from bridge import canvas
from bridge.config import Config
from bridge.delivery import handle_slack_event
from bridge.store import Store
from bridge.teams import ConnectorAuth, TeamsAuthError, TeamsBotTransport, TeamsConfig, TeamsDelivery


def interpretation(answer, semantic=False):
    """Keep real receive/queue paths while replacing only model output."""
    stack = ExitStack()
    action = {'kind': 'answer', 'answer': answer, 'rationale': '', 'to': ''}
    stack.enter_context(patch.object(Config, 'semantic_retrieval', property(lambda self: semantic)))
    stack.enter_context(patch('bridge.slack_chat.load', return_value=Config(model_api='none')))
    stack.enter_context(patch('bridge.slack_chat.reading', return_value=action))
    stack.enter_context(patch('bridge.llm.read_reply', return_value=action))
    return stack


class SlackReadbackGenerationTests(slack_fixtures.DeliveryCase):
    def setUp(self):
        super().setUp()
        self.node_id = self.node(self.task())['node_id']
        self.delivery.deliver_now()
        self.message = self.slack.messages[0]
        self.counter = 0
        self.addCleanup(self.delivery.close)

    def event(self, text, *, stamp=None, message=None, user='UWES', event_id=None):
        self.counter += 1
        message = message or self.message
        return {'type': 'event_callback', 'team_id': 'TTEST',
                'event_id': event_id or f'Ev-readback-{self.counter}',
                'event': {'type': 'message', 'user': user, 'text': text,
                          'channel': message['channel'], 'thread_ts': message['ts'],
                          'ts': stamp or f'1700000001.{self.counter:06d}'}}

    def send(self, text, **kwargs):
        event = self.event(text, **kwargs)
        handle_slack_event(self.delivery, event)
        return event

    def held(self, message=None, person=None):
        message = message or self.message
        return self.delivery._reading(message['channel'], message['ts'], person or self.wes)

    def offer(self, answer='Exclude the load test.', *, semantic=False, **kwargs):
        with interpretation(answer, semantic):
            event = self.send(answer, **kwargs)
        held = self.held(kwargs.get('message'))
        self.assertIsNotNone(held)
        self.assertRegex(held['proposal_id'], r'^[0-9a-f]{12}$')
        self.assertIn(held['proposal_id'], held['prompt'])
        self.assertIn(held['proposal_id'], self.slack.messages[-1]['text'])
        occurrence = json.loads(held['source_occurrence'])
        self.assertEqual(occurrence['timestamp'], event['event']['ts'])
        self.assertEqual(occurrence['platform'], 'slack')
        self.assertTrue(occurrence['id'])
        self.assertEqual(held['source_event_id'], event['event_id'])
        self.assertTrue(held['delivered_ref'])
        self.assertEqual(held['delivered_ref'], self.slack.messages[-1]['ts'])
        return held

    def assert_pending(self, node_id=None):
        row = self.store.get_decision(node_id or self.node_id)
        self.assertFalse(row['authorized'])
        self.assertFalse(row['answer'])
        self.assertEqual(row['status'], 'pending')

    def direct(self, text, occurrence, *, event_id=None):
        self.counter += 1
        return self.delivery.receive(self.message['channel'], self.message['ts'], 'UWES', text,
            event_id=event_id or f'direct-{self.counter}', occurrence=occurrence)

    def occurrence(self, stamp='1700000002.000001', **changes):
        return {'platform': 'slack', 'id': 'source-' + str(self.counter + 1),
                'timestamp': stamp, 'reply_to': self.message['ts'], **changes}

    def restart(self):
        self.delivery.close()
        self.graph.close()
        self.store = Store(Path(self.temp.name) / 'delivery.db')
        self.graph = self.store.graph
        self.delivery = self.store.connect_delivery(self.slack, base_url='https://bridge.acme.test')
        self.addCleanup(self.delivery.close)

    def test_delayed_yes_for_a_never_authorizes_newer_b(self):
        a = self.offer('Exclude the load test.', stamp='1700000001.000010')
        b = self.offer('Bill the load test.', stamp='1700000001.000030')
        self.assertNotEqual(a['proposal_id'], b['proposal_id'])
        self.send('yes', stamp='1700000001.000020')
        self.assert_pending()
        self.assertEqual(self.held(), b)
        self.send('confirm ' + a['proposal_id'], stamp='1700000001.000040')
        self.assert_pending()
        self.assertEqual(self.held(), b)
        self.send('confirm ' + b['proposal_id'], stamp='1700000001.000050')
        self.assertEqual(self.store.get_decision(self.node_id)['answer'], 'Bill the load test.')

    def test_new_root_thread_yes_requires_the_visible_proposal_code(self):
        held = self.offer()
        self.send('yes')
        self.assert_pending()
        self.assertEqual(self.held(), held)
        self.assertIn(held['proposal_id'], self.slack.messages[-1]['text'])
        self.send('yes ' + held['proposal_id'])
        self.assertTrue(self.store.get_decision(self.node_id)['authorized'])

    def test_bare_yes_can_bind_an_exact_delivered_reply_reference(self):
        held = self.offer()
        self.direct('yes', self.occurrence(reply_to=held['delivered_ref']))
        self.assertTrue(self.store.get_decision(self.node_id)['authorized'])
        self.assertIsNone(self.held())

    def test_bare_yes_cannot_bind_a_retired_delivered_reference(self):
        a = self.offer()
        b = self.offer('Bill the load test.')
        self.direct('yes', self.occurrence(reply_to=a['delivered_ref']))
        self.assert_pending()
        self.assertEqual(self.held(), b)

    def test_identical_text_still_gets_a_distinct_proposal_generation(self):
        a = self.offer()
        b = self.offer()
        self.assertNotEqual(a['proposal_id'], b['proposal_id'])
        self.assertNotEqual(a['source_occurrence'], b['source_occurrence'])
        self.send('yes ' + a['proposal_id'])
        self.assert_pending()
        self.assertEqual(self.held(), b)
        self.send('yes ' + b['proposal_id'])
        self.assertTrue(self.store.get_decision(self.node_id)['authorized'])

    def test_semantic_readback_obeys_the_same_occurrence_and_generation_checks(self):
        a = self.offer('Exclude the load test.', semantic=True, stamp='1700000001.000010')
        b = self.offer('Bill the load test.', semantic=True, stamp='1700000001.000030')
        self.assertEqual(b['kind'], 'conversation')
        with interpretation('Bill the load test.', semantic=True):
            self.send('yes', stamp='1700000001.000020')
            self.send('confirm ' + a['proposal_id'], stamp='1700000001.000040')
            self.assert_pending()
            self.assertEqual(self.held(), b)
            self.send('confirm ' + b['proposal_id'], stamp='1700000001.000050')
        self.assertEqual(self.store.get_decision(self.node_id)['answer'], 'Bill the load test.')

    def test_missing_or_invalid_occurrence_cannot_create_a_readback(self):
        invalid = [None, {}, {'platform': 'slack', 'id': 'missing-time'},
                   {'platform': 'slack', 'timestamp': '1700000002.000001'},
                   self.occurrence(timestamp=''), self.occurrence(timestamp='not-a-time'),
                   self.occurrence(timestamp='NaN'), self.occurrence(timestamp='Infinity'),
                   self.occurrence(timestamp=True), self.occurrence(platform='teams')]
        for occurrence in invalid:
            with self.subTest(occurrence=occurrence), interpretation('Exclude the load test.'):
                self.direct('Exclude the load test.', occurrence)
                self.assertIsNone(self.held())
                self.assert_pending()

    def test_invalid_and_non_newer_confirmation_occurrences_preserve_held_proposal(self):
        held = self.offer(stamp='1700000001.000010')
        invalid = [None, {}, self.occurrence(timestamp=''), self.occurrence(timestamp='NaN'),
                   self.occurrence(timestamp='not-a-time'), self.occurrence(timestamp=True),
                   self.occurrence(timestamp='1700000001.000009'),
                   self.occurrence(timestamp='1700000001.000010'),
                   self.occurrence(id=''), self.occurrence(platform='teams')]
        for occurrence in invalid:
            with self.subTest(occurrence=occurrence):
                self.direct('confirm ' + held['proposal_id'], occurrence)
                self.assert_pending()
                self.assertEqual(self.held(), held)
        self.direct('confirm ' + held['proposal_id'], self.occurrence())
        self.assertTrue(self.store.get_decision(self.node_id)['authorized'])

    def test_duplicate_equal_and_older_sources_cannot_replace_newer_readback(self):
        held = self.offer(stamp='1700000001.000010')
        with interpretation('An obsolete interpretation.'):
            for stamp in ('1700000001.000009', '1700000001.000010'):
                self.send('An obsolete interpretation.', stamp=stamp)
                self.assertEqual(self.held(), held)
        self.assert_pending()
        self.send('confirm ' + held['proposal_id'], stamp='1700000001.000011')
        self.assertEqual(self.store.get_decision(self.node_id)['answer'], 'Exclude the load test.')

    def test_duplicate_callback_does_not_reissue_or_apply_twice(self):
        with interpretation('Exclude the load test.'):
            event = self.send('Exclude the load test.')
            held = self.held()
            before = len(self.slack.messages)
            handle_slack_event(self.delivery, copy.deepcopy(event))
        self.assertEqual(self.held(), held)
        self.assertEqual(len(self.slack.messages), before)
        confirmation = self.send('confirm ' + held['proposal_id'])
        signature_events = self.graph.db.execute("SELECT count(*) FROM events WHERE kind='owner_approved'").fetchone()[0]
        handle_slack_event(self.delivery, copy.deepcopy(confirmation))
        self.assertEqual(self.graph.db.execute("SELECT count(*) FROM events WHERE kind='owner_approved'").fetchone()[0], signature_events)

    def test_wrong_person_and_workspace_cannot_confirm_owner_proposal(self):
        held = self.offer()
        self.send('confirm ' + held['proposal_id'], user='UMAR')
        self.assert_pending()
        self.assertEqual(self.held(), held)
        wrong_workspace = self.event('confirm ' + held['proposal_id'])
        wrong_workspace['team_id'] = 'TOTHER'
        handle_slack_event(self.delivery, wrong_workspace)
        self.assert_pending()
        self.assertEqual(self.held(), held)
        self.send('confirm ' + held['proposal_id'])
        self.assertTrue(self.store.get_decision(self.node_id)['authorized'])

    def test_proposal_codes_cannot_cross_threads_or_tasks(self):
        first = self.offer()
        second_id = self.node(self.task('A separate billing task'),
            question='Which currency should invoices use for the separate regional account?')['node_id']
        self.delivery.deliver_now()
        second_message = self.slack.messages[-1]
        second = self.offer('Bill the separate account.', message=second_message)
        self.send('confirm ' + first['proposal_id'], message=second_message)
        self.send('confirm ' + second['proposal_id'])
        self.assert_pending()
        self.assert_pending(second_id)
        self.assertEqual(self.held(), first)
        self.assertEqual(self.held(second_message), second)
        self.send('confirm ' + second['proposal_id'], message=second_message)
        self.send('confirm ' + first['proposal_id'])
        self.assertEqual(self.store.get_decision(second_id)['answer'], 'Bill the separate account.')
        self.assertEqual(self.store.get_decision(self.node_id)['answer'], 'Exclude the load test.')

    def test_cancelled_task_cannot_be_revived_by_its_proposal_code(self):
        held = self.offer()
        task = self.store.get_decision(self.node_id)['run_id']
        canvas.finish_task(self.store, {'task_id': task, 'status': 'abandoned', 'reason': 'Synthetic cancellation'})
        self.send('confirm ' + held['proposal_id'])
        row = self.store.get_decision(self.node_id)
        self.assertEqual(row['status'], 'withdrawn')
        self.assertFalse(row['authorized'])
        self.assertFalse(row['answer'])

    def test_decline_retires_code_and_new_same_text_requires_new_code(self):
        a = self.offer()
        self.send('decline ' + a['proposal_id'])
        self.assertIsNone(self.held())
        b = self.offer()
        self.assertNotEqual(a['proposal_id'], b['proposal_id'])
        self.send('confirm ' + a['proposal_id'])
        self.assert_pending()
        self.assertEqual(self.held(), b)
        self.send('confirm ' + b['proposal_id'])
        self.assertTrue(self.store.get_decision(self.node_id)['authorized'])

    def test_delivery_proof_and_source_occurrence_survive_store_restart(self):
        held = self.offer(stamp='1700000001.000020')
        self.restart()
        self.assertEqual(self.held(), held)
        self.send('confirm ' + held['proposal_id'], stamp='1700000001.000019')
        self.assert_pending()
        self.assertEqual(self.held(), held)
        self.send('confirm ' + held['proposal_id'], stamp='1700000001.000021')
        self.assertTrue(self.store.get_decision(self.node_id)['authorized'])

    def test_failed_delivery_cannot_confirm_until_durable_reply_is_sent(self):
        self.slack.fail = True
        with interpretation('Exclude the load test.'):
            event = self.send('Exclude the load test.')
        held = self.held()
        self.assertTrue(held['proposal_id'])
        self.assertFalse(held['delivered_ref'])
        self.send('confirm ' + held['proposal_id'])
        self.assert_pending()
        self.assertEqual(self.held(), held)
        self.restart()
        self.slack.fail = False
        self.graph.db.execute('UPDATE slack_replies SET next_attempt=0')
        self.delivery.inbox.flush()
        delivered = self.held()
        self.assertEqual(delivered['proposal_id'], held['proposal_id'])
        self.assertEqual(delivered['source_occurrence'], held['source_occurrence'])
        self.assertTrue(delivered['delivered_ref'])
        self.assertEqual(self.graph.db.execute('SELECT state FROM slack_replies WHERE id=?', (event['event_id'],)).fetchone()[0], 'sent')
        self.send('confirm ' + held['proposal_id'])
        self.assertTrue(self.store.get_decision(self.node_id)['authorized'])

    def test_retrying_old_prompt_never_rebinds_newer_delivered_proposal(self):
        self.slack.fail = True
        with interpretation('Exclude the load test.'):
            first_event = self.send('Exclude the load test.')
        first = self.held()
        self.slack.fail = False
        current = self.offer('Bill the load test.')
        self.graph.db.execute('UPDATE slack_replies SET next_attempt=0 WHERE id=?', (first_event['event_id'],))
        self.delivery.inbox.flush()
        self.assertEqual(self.held(), current)
        self.send('confirm ' + first['proposal_id'])
        self.assert_pending()
        self.assertEqual(self.held(), current)
        self.send('confirm ' + current['proposal_id'])
        self.assertEqual(self.store.get_decision(self.node_id)['answer'], 'Bill the load test.')

    def test_missing_slack_timestamp_never_becomes_a_proposal_or_confirmation(self):
        event = self.event('Exclude the load test.')
        event['event'].pop('ts')
        with interpretation('Exclude the load test.'):
            handle_slack_event(self.delivery, event)
        self.assertIsNone(self.held())
        held = self.offer()
        event = self.event('confirm ' + held['proposal_id'])
        event['event'].pop('ts')
        handle_slack_event(self.delivery, event)
        self.assert_pending()
        self.assertEqual(self.held(), held)

    def test_signoff_aliases_do_not_switch_from_a_held_correction_to_the_old_answer(self):
        self.send('answer: Existing proposed answer because synthetic baseline')
        original = self.store.get_decision(self.node_id)
        held = self.offer('Replace it with a stricter task-only answer.')
        for alias in ('sign off', 'approve', 'approved', 'lgtm', 'yes, sign off', 'okay, sign off'):
            with self.subTest(alias=alias):
                before = [dict(r) for r in self.graph.db.execute('SELECT * FROM events ORDER BY id')]
                self.send(alias)
                self.assertEqual(self.held(), held)
                self.assertEqual(self.store.get_decision(self.node_id)['answer'], original['answer'])
                self.assertEqual([dict(r) for r in self.graph.db.execute(
                    "SELECT * FROM events WHERE kind IN ('readback_confirmed','owner_approved','answer_corrected','signoff') ORDER BY id")],
                    [r for r in before if r['kind'] in ('readback_confirmed','owner_approved','answer_corrected','signoff')])
        self.send('confirm ' + held['proposal_id'])
        self.assertEqual(self.store.get_decision(self.node_id)['answer'], 'Replace it with a stricter task-only answer.')

    def test_confirmation_authored_before_delivery_is_not_retimed_by_processing(self):
        self.slack.fail = True
        with interpretation('Exclude the load test.'):
            source = self.send('Exclude the load test.', stamp='1700000001.000001')
        held = self.held()
        self.slack.fail = False
        original = self.slack.post_message
        def delivered_later(*args, **kwargs):
            original(*args, **kwargs)
            self.slack.messages[-1]['ts'] = '1700000003.000001'
            return '1700000003.000001'
        self.graph.db.execute('UPDATE slack_replies SET next_attempt=0')
        with patch.object(self.slack, 'post_message', side_effect=delivered_later):
            self.delivery.inbox.flush()
        self.send('confirm ' + held['proposal_id'], stamp='1700000002.000001')
        self.assert_pending()
        self.send('confirm ' + held['proposal_id'], stamp='1700000004.000001')
        self.assertTrue(self.store.get_decision(self.node_id)['authorized'])

    def test_legacy_readback_metadata_is_not_upgraded_into_consent(self):
        held = self.offer()
        self.graph.db.execute("UPDATE reply_readings SET source_occurrence='',proposal_id='',delivered_ref='',delivered_at=0")
        self.send('confirm ' + held['proposal_id'])
        self.assert_pending()
        self.send('yes')
        self.assert_pending()

    def test_failed_receipt_retry_uses_original_occurrence_not_retry_time(self):
        event = self.event('Exclude the load test.', stamp='1700000001.000001')
        with patch.object(self.delivery, '_apply_reply', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):
                handle_slack_event(self.delivery, event)
        receipt = dict(self.graph.db.execute('SELECT * FROM webhook_receipts WHERE id=?', (event['event_id'],)).fetchone())
        payload = json.loads(receipt['payload'])
        self.assertEqual(payload['occurrence']['timestamp'], event['event']['ts'])
        newer = self.offer('Bill the load test.', stamp='1700000003.000001')
        with interpretation('Exclude the load test.'):
            self.delivery.retry_inbound(event['event_id'])
        self.assertEqual(self.held(), newer)
        self.assert_pending()

    def test_replayed_event_cannot_alter_its_signed_occurrence(self):
        held = self.offer()
        payload = json.loads(self.graph.db.execute('SELECT payload FROM webhook_receipts WHERE id=?',
                            (held['source_event_id'],)).fetchone()[0])
        from bridge.store import Invalid
        with self.assertRaises(Invalid):
            self.delivery.receive(payload['channel'], payload['thread_ts'], payload['user'], payload['text'],
                event_id=held['source_event_id'], occurrence={**payload['occurrence'], 'timestamp':'1700000010.000001'})
        self.assertEqual(self.held(), held)
        self.assert_pending()

    def test_exact_confirmation_provenance_is_appended_once(self):
        held = self.offer()
        before = [dict(r) for r in self.graph.db.execute('SELECT * FROM events ORDER BY id')]
        event = self.send('confirm ' + held['proposal_id'])
        after = [dict(r) for r in self.graph.db.execute('SELECT * FROM events ORDER BY id')]
        self.assertEqual(after[:len(before)], before)
        proof = json.loads(next(r['detail'] for r in after if r['kind'] == 'readback_confirmed'))
        self.assertEqual(proof['proposal_id'], held['proposal_id'])
        self.assertEqual(proof['proposal']['answer'], held['answer'])
        self.assertEqual(proof['source_occurrence'], json.loads(held['source_occurrence']))
        self.assertEqual(proof['confirmation_occurrence']['timestamp'], event['event']['ts'])
        self.assertEqual(proof['actor_id'], self.wes)
        self.assertEqual(proof['delivered_ref'], held['delivered_ref'])
        handle_slack_event(self.delivery, event)
        self.assertEqual([dict(r) for r in self.graph.db.execute('SELECT * FROM events ORDER BY id')], after)


    # Independently authored action and two-connection transaction regressions.
    def event_count(self,kind):
        return self.graph.db.execute('SELECT count(*) FROM events WHERE decision_id=? AND kind=?',(self.node_id,kind)).fetchone()[0]
    def thread(self,fn,errors,name):
        def run():
            try:fn()
            except Exception as e:errors.append(repr(e))
            finally:self.graph.close_thread()
        w=threading.Thread(target=run,name=name);w.start();return w
    def action_context(self,action):
        stack=interpretation('unused',True)
        stack.enter_context(patch('bridge.slack_chat.reading',return_value=action))
        return stack
    def action_offer(self,action,text):
        with self.action_context(action):self.send(text)
        held=self.held()
        self.assertIsNotNone(held)
        self.assertEqual(json.loads(held['answer'])['kind'],action['kind'])
        return held

    def test_two_connection_confirmations_consume_exactly_once(self):
        held=self.offer()
        events=[self.event('confirm '+held['proposal_id'],stamp='1700000002.000001'),
                self.event('confirm '+held['proposal_id'],stamp='1700000002.000002')]
        barrier=threading.Barrier(2);first_read=threading.Event();local=threading.local();original=self.delivery._reading;errors=[];connections=set()
        def reading(*args):
            result=original(*args)
            if not getattr(local,'seen',False):
                local.seen=True
                connections.add(id(self.graph.db))
                if threading.current_thread().name=='confirm-0':first_read.set()
                barrier.wait(5)
            return result
        with patch.object(self.delivery,'_reading',side_effect=reading):
            ws=[self.thread(lambda:handle_slack_event(self.delivery,events[0]),errors,'confirm-0')]
            self.assertTrue(first_read.wait(5))
            ws.append(self.thread(lambda:handle_slack_event(self.delivery,events[1]),errors,'confirm-1'))
            for w in ws:w.join(10)
        self.assertFalse(any(w.is_alive() for w in ws));self.assertEqual(errors,[])
        self.assertTrue(self.store.get_decision(self.node_id)['authorized'])
        self.assertEqual(self.event_count('readback_confirmed'),1)
        self.assertEqual(self.event_count('owner_approved'),1)
        self.assertEqual(len(connections),2)

    def test_newer_proposal_wins_during_old_interpretation(self):
        entered,release=threading.Event(),threading.Event();errors=[]
        old=self.event('Old answer',stamp='1700000002.000001')
        newer=self.event('New answer',stamp='1700000002.000002')
        def interpret(*args,**kwargs):
            old_thread=threading.current_thread().name=='old-interpreter'
            if old_thread:
                entered.set()
                if not release.wait(5):raise AssertionError('timeout')
            return {'kind':'answer','answer':'Old answer' if old_thread else 'New answer','rationale':'','to':''}
        with patch('bridge.llm.read_reply',side_effect=interpret):
            w=self.thread(lambda:handle_slack_event(self.delivery,old),errors,'old-interpreter')
            self.assertTrue(entered.wait(5))
            try:handle_slack_event(self.delivery,newer)
            finally:release.set();w.join(10)
        self.assertFalse(w.is_alive());self.assertEqual(errors,[])
        self.assertEqual(self.held()['answer'],'New answer');self.assert_pending()

    def test_later_explicit_answer_defeats_paused_confirmation(self):
        held=self.offer();entered,release=threading.Event(),threading.Event();errors=[];original=self.delivery._reading
        confirm=self.event('confirm '+held['proposal_id'],stamp='1700000002.000001')
        newer=self.event('answer: Bill the full spike because the fixture changed',stamp='1700000002.000002')
        def reading(*args):
            result=original(*args)
            if threading.current_thread().name=='paused-confirmer' and not entered.is_set():
                entered.set()
                if not release.wait(5):raise AssertionError('timeout')
            return result
        with patch.object(self.delivery,'_reading',side_effect=reading):
            w=self.thread(lambda:handle_slack_event(self.delivery,confirm),errors,'paused-confirmer')
            self.assertTrue(entered.wait(5))
            try:handle_slack_event(self.delivery,newer)
            finally:release.set();w.join(10)
        self.assertFalse(w.is_alive());self.assertEqual(errors,[])
        self.assertEqual(self.store.get_decision(self.node_id)['answer'],'Bill the full spike')
        self.assertEqual(self.event_count('readback_confirmed'),0)

    def test_writer_failure_rolls_back_consumption_and_provenance(self):
        held=self.offer();original=self.store.answer
        def failed(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError('synthetic failure after answer mutation')
        with patch.object(self.store,'answer',side_effect=failed):
            with self.assertRaisesRegex(RuntimeError,'synthetic failure'):
                self.send('confirm '+held['proposal_id'])
        self.assert_pending();self.assertEqual(self.held(),held)
        self.assertEqual(self.event_count('readback_confirmed'),0);self.assertEqual(self.event_count('owner_approved'),0)
        self.send('confirm '+held['proposal_id']);self.assertTrue(self.store.get_decision(self.node_id)['authorized'])

    def test_semantic_followup_is_atomic_without_nested_writer(self):
        held=self.action_offer({'kind':'followup','answer':'Should we add a synthetic usage ceiling?','required':True},'Please ask whether to add a synthetic usage ceiling before finishing')
        self.send('confirm '+held['proposal_id'])
        self.assertIsNone(self.held());self.assertEqual(self.event_count('readback_confirmed'),1)
        row=self.graph.db.execute('SELECT * FROM decisions WHERE parent_id=?',(self.node_id,)).fetchone()
        self.assertTrue(row);self.assertEqual(row['followup_required'],1)

    def test_semantic_reframe_is_atomic_without_nested_writer(self):
        held=self.action_offer({'kind':'reframe','answer':'Should synthetic usage be excluded from this invoice?','rationale':'fixture scope'},'Please replace the mistaken question with whether synthetic usage is excluded')
        self.send('confirm '+held['proposal_id'])
        self.assertEqual(self.store.get_decision(self.node_id)['question'],'Should synthetic usage be excluded from this invoice?')
        self.assertEqual(self.event_count('readback_confirmed'),1)

    def test_semantic_rule_is_atomic_without_nested_writer(self):
        self.send('answer: Exclude it because it was synthetic')
        held=self.action_offer({'kind':'rule','conditions':'fixture=synthetic','expires':''},'Make that a rule for fixture=synthetic')
        self.send('confirm '+held['proposal_id'])
        self.assertTrue(self.store.get_decision(self.node_id)['reusable'])
        self.assertEqual(self.event_count('readback_confirmed'),1)

    def test_semantic_handoff_keeps_question_only_scope(self):
        held=self.action_offer({'kind':'handoff','to':'<@UMAR>','scope_kind':'none','scope':''},'Please ask <@UMAR> for this question only')
        self.send('confirm '+held['proposal_id'])
        row=self.store.get_decision(self.node_id)
        owner=self.graph.db.execute('SELECT person_id FROM owners WHERE id=?',(row['owner_id'],)).fetchone()
        self.assertEqual(owner['person_id'],self.marisol)
        self.assertEqual(self.event_count('readback_confirmed'),1)
        self.assertTrue(self.event_count('route_learning_optout'))

    def test_semantic_handoff_never_fetches_remote_identity_under_writer(self):
        calls=[]
        def user_info(uid):
            calls.append((uid,self.graph.db.in_transaction))
            person=self.graph.find_person(uid)
            return {'id':uid,'team_id':'TTEST','profile':{'real_name':person['name'],'email':person['email']}}
        with patch.object(self.slack,'user_info',side_effect=user_info,create=True):
            held=self.action_offer({'kind':'handoff','to':'<@UMAR>','scope_kind':'none','scope':''},'Please ask <@UMAR> for this question only')
            self.send('confirm '+held['proposal_id'])
        self.assertFalse(any(locked for _,locked in calls),'Remote user_info must not run while the global writer transaction is held')

    def test_semantic_handoff_confirmation_preserves_displayed_target(self):
        held=self.action_offer({'kind':'handoff','to':'<@UMAR>','scope_kind':'none','scope':''},'Please ask <@UMAR> for this question only')
        saved=json.loads(held['answer'])
        self.assertEqual(saved['to'],self.marisol)
        self.assertIn('Marisol Vega',held['prompt'])
        with self.graph.transaction():
            self.graph.db.execute('UPDATE people SET slack_id=? WHERE id=?',('UMARNEW',self.marisol))
            replacement=self.graph.add_person('Synthetic Other Contact',email='other-contact@example.test',slack_id='UMAR',merge=False)
        self.send('confirm '+held['proposal_id'])
        row=self.store.get_decision(self.node_id)
        actual=self.graph.db.execute('SELECT person_id FROM owners WHERE id=?',(row['owner_id'],)).fetchone()['person_id']
        self.assertNotEqual(actual,replacement,'Exact displayed proposal must not re-resolve its original mention into another person')

    def test_semantic_handoff_uses_displayed_target_among_multiple_mentions(self):
        held=self.action_offer({'kind':'handoff','to':'<@UPRI>','scope_kind':'none','scope':''},'<@UMAR> is away. Please ask <@UPRI> for this question only')
        self.assertEqual(json.loads(held['answer'])['to'],self.priya)
        self.assertIn('Priya Natarajan',held['prompt'])
        self.send('confirm '+held['proposal_id'])
        row=self.store.get_decision(self.node_id)
        actual=self.graph.db.execute('SELECT person_id FROM owners WHERE id=?',(row['owner_id'],)).fetchone()['person_id']
        self.assertEqual(actual,self.priya,'The first original mention is not the displayed target')

    def test_semantic_renamed_target_and_namesake_keep_original_identity(self):
        held=self.action_offer({'kind':'handoff','to':'<@UMAR>','scope_kind':'none','scope':''},'Please ask <@UMAR> for this question only')
        self.graph.add_person('Marisol Renamed',email='marisol@acme.example')
        other=self.graph.add_person('Marisol Vega',email='namesake@example.test',slack_id='UMAROTHER',merge=False)
        self.send('confirm '+held['proposal_id'])
        row=self.store.get_decision(self.node_id)
        actual=self.graph.db.execute('SELECT person_id FROM owners WHERE id=?',(row['owner_id'],)).fetchone()['person_id']
        self.assertEqual(actual,self.marisol);self.assertNotEqual(actual,other)

    def test_semantic_deactivated_target_refuses_and_preserves_proposal(self):
        held=self.action_offer({'kind':'handoff','to':'<@UMAR>','scope_kind':'none','scope':''},'Please ask <@UMAR> for this question only')
        with self.graph.transaction():self.graph.db.execute('UPDATE people SET active=0 WHERE id=?',(self.marisol,))
        self.send('confirm '+held['proposal_id'])
        self.assertEqual(self.held(),held);self.assertEqual(self.event_count('readback_confirmed'),0)
        row=self.store.get_decision(self.node_id)
        self.assertEqual(self.graph.db.execute('SELECT person_id FROM owners WHERE id=?',(row['owner_id'],)).fetchone()['person_id'],self.wes)

    def test_compatibility_handoff_pins_displayed_person_before_rename(self):
        action={'kind':'handoff','to':'Marisol Vega','answer':'','rationale':''}
        with interpretation('unused'),patch('bridge.llm.read_reply',return_value=action):
            self.send('Can someone pass this to Marisol Vega?')
        held=self.held()
        self.assertEqual(held['kind'],'handoff')
        self.assertIn('Marisol Vega',held['prompt'])
        self.graph.add_person('Marisol Renamed',email='marisol@acme.example')
        other=self.graph.add_person('Marisol Vega',email='namesake@example.test',slack_id='UMAROTHER',merge=False)
        self.send('confirm '+held['proposal_id'])
        row=self.store.get_decision(self.node_id)
        actual=self.graph.db.execute('SELECT person_id FROM owners WHERE id=?',(row['owner_id'],)).fetchone()['person_id']
        self.assertNotEqual(actual,other,'A name rebound after readback must not select a different contact')

    def test_semantic_claim_is_atomic_without_nested_writer(self):
        self.delivery.fallback_channel='CTRIAGE'
        self.node_id=self.graph.add_decision(self.task('Unassigned synthetic question'),'Choose the fixture route','definition','pending')
        self.store.notify(self.node_id,'ask');self.delivery.deliver_now();self.message=self.slack.messages[-1]
        self.assertEqual(self.message['channel'],'CTRIAGE')
        held=self.action_offer({'kind':'claim'},'I can take responsibility for this question')
        self.send('confirm '+held['proposal_id'])
        row=self.store.get_decision(self.node_id)
        actual=self.graph.db.execute('SELECT person_id FROM owners WHERE id=?',(row['owner_id'],)).fetchone()['person_id']
        self.assertEqual(actual,self.wes);self.assertFalse(row['authorized'])
        self.assertEqual(self.event_count('readback_confirmed'),1)

    def compatibility_offer(self):
        action={'kind':'handoff','to':'Marisol Vega','answer':'','rationale':''}
        with interpretation('unused'),patch('bridge.llm.read_reply',return_value=action):self.send('Could Marisol Vega take this one?')
        return self.held()

    def test_compatibility_handoff_normal_uses_bound_stable_identity(self):
        held=self.compatibility_offer();self.assertEqual(held['recipient'],self.marisol)
        self.send('confirm '+held['proposal_id'])
        row=self.store.get_decision(self.node_id)
        actual=self.graph.db.execute('SELECT person_id FROM owners WHERE id=?',(row['owner_id'],)).fetchone()['person_id']
        self.assertEqual(actual,self.marisol);self.assertEqual(self.event_count('readback_confirmed'),1)

    def test_compatibility_handoff_changed_binding_keeps_original_identity(self):
        held=self.compatibility_offer();self.assertEqual(held['recipient'],self.marisol)
        with self.graph.transaction():
            self.graph.db.execute('UPDATE people SET slack_id=? WHERE id=?',('UMARNEW',self.marisol))
            other=self.graph.add_person('Synthetic Other Contact',email='other-contact@example.test',slack_id='UMAR',merge=False)
        self.send('confirm '+held['proposal_id'])
        row=self.store.get_decision(self.node_id)
        actual=self.graph.db.execute('SELECT person_id FROM owners WHERE id=?',(row['owner_id'],)).fetchone()['person_id']
        self.assertEqual(actual,self.marisol);self.assertNotEqual(actual,other)

    def test_compatibility_deactivated_target_refuses_and_preserves_proposal(self):
        held=self.compatibility_offer()
        with self.graph.transaction():self.graph.db.execute('UPDATE people SET active=0 WHERE id=?',(self.marisol,))
        self.send('confirm '+held['proposal_id'])
        self.assertEqual(self.held(),held);self.assertEqual(self.event_count('readback_confirmed'),0)
        row=self.store.get_decision(self.node_id)
        actual=self.graph.db.execute('SELECT person_id FROM owners WHERE id=?',(row['owner_id'],)).fetchone()['person_id']
        self.assertEqual(actual,self.wes)

class UniqueReplyMicrosoftFixture(teams_fixtures.MicrosoftFixture):
    """The production connector returns distinct IDs for distinct replies."""
    def __call__(self, request):
        response = super().__call__(request)
        if response.get('id') == 'opaque-reply':
            return {'id': f'opaque-reply-{len(self.messages)}'}
        return response


@unittest.skipUnless(teams_fixtures.jwt, 'Install requirements-teams.txt for verified Teams callbacks')
class TeamsReadbackGenerationTests(OfflineCase):
    activity = teams_fixtures.TeamsTests.activity
    token = teams_fixtures.TeamsTests.token
    submit = teams_fixtures.TeamsTests.submit
    decision = teams_fixtures.TeamsTests.decision

    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'teams-generations.db')
        self.graph = self.store.graph
        with self.graph.transaction():
            self.wes = self.graph.add_person('Wes Chen', email='wes@example.test')
            self.other = self.graph.add_person('Other Person', email='other@example.test')
            self.graph.add_authority('path', 'billing/*', 'decides', person_id=self.wes)
        self.config = TeamsConfig(teams_fixtures.APP, teams_fixtures.TENANT, teams_fixtures.CHANNEL,
            'fixture-secret', {teams_fixtures.OWNER: self.wes, teams_fixtures.OTHER: self.other})
        self.microsoft = UniqueReplyMicrosoftFixture()
        self.transport = TeamsBotTransport(self.graph, self.config, http=self.microsoft)
        self.auth = ConnectorAuth(self.config, http=self.microsoft)
        self.delivery = TeamsDelivery(self.store, self.config, self.transport, self.auth)
        self.store._delivery = self.delivery
        self.no_worker = patch.object(self.delivery.inbox, 'start')
        self.no_worker.start()
        self.addCleanup(self.no_worker.stop)
        self.addCleanup(self.delivery.close)
        self.task = canvas.start_task(self.store, Config(model_api='none'),
            {'title': 'Usage billing', 'repo': 'acme/platform', 'paths': 'billing/usage.py'})['task_id']
        self.node = canvas.add_node(self.store, Config(model_api='none'),
            {'task_id': self.task, 'question': 'Bill the usage spike?', 'paths': 'billing/usage.py',
             'context': 'Enterprise-two load test', 'options': 'Bill | Exclude'})['node_id']
        self.delivery.deliver_now()
        self.thread = dict(self.graph.db.execute('SELECT * FROM teams_threads').fetchone())
        self.counter = 0
        self.epoch = time.time() + 1  # Synthetic transport clock; all replies follow ACK time.

    def held(self):
        return self.delivery._reading(self.config.destination, self.thread['id'], self.wes)

    def stamped(self, text, seconds=0, **changes):
        activity = self.activity(text)
        activity['timestamp'] = datetime.fromtimestamp(self.epoch + seconds, timezone.utc).isoformat()
        activity.update(changes)
        return activity

    def offer(self, answer='Exclude the load test.', seconds=0, semantic=False):
        source = self.stamped(answer, seconds)
        with interpretation(answer, semantic):
            self.submit(source)
        held = self.held()
        self.assertIsNotNone(held)
        self.assertRegex(held['proposal_id'], r'^[0-9a-f]{12}$')
        self.assertIn(held['proposal_id'], self.microsoft.messages[-1]['text'])
        self.assertEqual(json.loads(held['source_occurrence'])['timestamp'], source['timestamp'])
        self.assertEqual(json.loads(held['source_occurrence'])['id'], source['id'])
        self.assertEqual(json.loads(held['source_occurrence'])['platform'], 'teams')
        self.assertEqual(held['delivered_ref'], f'opaque-reply-{len(self.microsoft.messages)}')
        return held

    def assert_pending(self):
        self.assertFalse(self.decision()['authorized'])
        self.assertFalse(self.decision()['answer'])
        self.assertEqual(self.decision()['status'], 'pending')

    def test_verified_delayed_and_unbound_yes_do_not_authorize_latest_readback(self):
        a = self.offer(seconds=0)
        b = self.offer('Bill the load test.', seconds=10)
        self.submit(self.stamped('yes', 5))
        self.submit(self.stamped('yes', 11))
        self.submit(self.stamped('confirm ' + a['proposal_id'], 12))
        self.assert_pending()
        self.assertEqual(self.held(), b)
        self.submit(self.stamped('confirm ' + b['proposal_id'], 13))
        self.assertEqual(self.decision()['answer'], 'Bill the load test.')

    def test_verified_exact_reply_to_current_readback_can_confirm_without_code(self):
        a = self.offer(seconds=0)
        b = self.offer('Bill the load test.', seconds=10)
        self.assertNotEqual(a['delivered_ref'], b['delivered_ref'])
        self.submit(self.stamped('yes', 11, replyToId=a['delivered_ref']))
        self.assert_pending()
        self.assertEqual(self.held(), b)
        self.submit(self.stamped('yes', 12, replyToId=b['delivered_ref']))
        self.assertEqual(self.decision()['answer'], 'Bill the load test.')

    def test_same_text_and_equal_timestamp_are_not_the_same_generation(self):
        a = self.offer(seconds=0)
        with interpretation('A conflicting reading.'):
            self.submit(self.stamped('A conflicting reading.', 0))
        self.assertEqual(self.held(), a)
        b = self.offer(seconds=1)
        self.assertNotEqual(a['proposal_id'], b['proposal_id'])
        self.submit(self.stamped('confirm ' + a['proposal_id'], 2))
        self.assert_pending()
        self.assertEqual(self.held(), b)
        self.submit(self.stamped('confirm ' + b['proposal_id'], 3))
        self.assertTrue(self.decision()['authorized'])

    def test_verified_other_person_cannot_use_owner_code(self):
        held = self.offer()
        activity = self.stamped('confirm ' + held['proposal_id'], 1)
        activity['from']['aadObjectId'] = teams_fixtures.OTHER
        self.submit(activity)
        self.assert_pending()
        self.assertEqual(self.held(), held)
        self.submit(self.stamped('confirm ' + held['proposal_id'], 2))
        self.assertTrue(self.decision()['authorized'])

    def test_missing_or_invalid_signed_timestamp_never_changes_held_readback(self):
        held = self.offer()
        for timestamp in (None, '', 'not-a-time', '2026-10-06T12:00:00'):
            activity = self.stamped('confirm ' + held['proposal_id'], 1)
            if timestamp is None:
                activity.pop('timestamp')
            else:
                activity['timestamp'] = timestamp
            with self.subTest(timestamp=timestamp), self.assertRaises(TeamsAuthError):
                self.submit(activity)
            self.assert_pending()
            self.assertEqual(self.held(), held)

    def test_queue_preserves_verified_occurrence_through_adapter_restart(self):
        held = self.offer()
        activity = self.stamped('yes', 1, replyToId=held['delivered_ref'])
        self.delivery.handle(self.token(), activity)
        queued = dict(self.graph.db.execute("SELECT * FROM teams_ingress WHERE state='queued'").fetchone())
        occurrence = json.loads(queued['payload'])['occurrence']
        self.assertEqual(occurrence['id'], activity['id'])
        self.assertEqual(occurrence['timestamp'], activity['timestamp'])
        self.assertEqual(occurrence['reply_to'], held['delivered_ref'])
        self.delivery.close()
        replacement = TeamsDelivery(self.store, self.config, self.transport, self.auth)
        self.addCleanup(replacement.close)
        replacement.inbox.process()
        self.assertTrue(self.decision()['authorized'])
        self.assertEqual(self.graph.db.execute('SELECT state FROM teams_ingress WHERE id=?', (queued['id'],)).fetchone()[0], 'done')

    def test_semantic_teams_readback_also_requires_fresh_generation_bound_confirmation(self):
        a = self.offer(seconds=0, semantic=True)
        b = self.offer('Bill the load test.', seconds=2, semantic=True)
        with interpretation('Bill the load test.', semantic=True):
            self.submit(self.stamped('confirm ' + a['proposal_id'], 3))
            self.assert_pending()
            self.assertEqual(self.held(), b)
            self.submit(self.stamped('confirm ' + b['proposal_id'], 4))
        self.assertEqual(self.decision()['answer'], 'Bill the load test.')

    def test_delivery_retry_preserves_code_and_adds_exact_reply_binding(self):
        self.microsoft.fail = True
        with interpretation('Exclude the load test.'):
            self.submit(self.stamped('Exclude the load test.'))
        held = self.held()
        self.assertTrue(held['proposal_id'])
        self.assertFalse(held['delivered_ref'])
        self.assert_pending()
        self.delivery.close()
        replacement = TeamsDelivery(self.store, self.config, self.transport, self.auth)
        self.addCleanup(replacement.close)
        self.microsoft.fail = False
        self.graph.db.execute('UPDATE teams_replies SET next_attempt=0')
        replacement.inbox.flush()
        delivered = self.held()
        self.assertEqual(delivered['proposal_id'], held['proposal_id'])
        self.assertEqual(delivered['source_occurrence'], held['source_occurrence'])
        self.assertTrue(delivered['delivered_ref'])
        with patch.object(replacement.inbox, 'start'):
            replacement.handle(self.token(), self.stamped('yes', 2, replyToId=delivered['delivered_ref']))
        replacement.inbox.process()
        self.assertTrue(self.decision()['authorized'])

    def test_fresh_jwt_does_not_retime_an_old_confirmation(self):
        self.epoch = time.time() - 20
        held = self.offer(seconds=0)
        old = self.stamped('confirm ' + held['proposal_id'], 1)
        self.submit(old)  # JWT is verified now; the activity was before delivery.
        self.assert_pending()
        self.submit(self.activity('confirm ' + held['proposal_id']))
        self.assertTrue(self.decision()['authorized'])

    def test_submicrosecond_occurrences_tie_instead_of_inventing_order(self):
        from bridge.readback import occurrence_time
        a = {'platform':'teams', 'id':'a', 'timestamp':'2026-10-06T12:00:00.1234561Z'}
        b = {'platform':'teams', 'id':'b', 'timestamp':'2026-10-06T12:00:00.1234569Z'}
        self.assertIsNotNone(occurrence_time(a))
        self.assertEqual(occurrence_time(a), occurrence_time(b))
        self.assertIsNone(occurrence_time({**a, 'timestamp':'2026-10-06T12:00:00.1234561'}))

    def test_identical_activity_id_with_different_timestamp_is_rejected(self):
        source = self.stamped('Exclude the load test.')
        with interpretation('Exclude the load test.'):
            self.submit(source)
        held = self.held()
        changed = copy.deepcopy(source)
        changed['timestamp'] = datetime.fromtimestamp(self.epoch + 1, timezone.utc).isoformat()
        with self.assertRaises(TeamsAuthError):
            self.submit(changed)
        self.assertEqual(self.held(), held)
        self.assert_pending()


if __name__ == '__main__':
    unittest.main()
