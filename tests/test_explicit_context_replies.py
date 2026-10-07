"""Offline synthetic replies, after transport authentication; no provider calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from unittest.mock import patch

from bridge import canvas, source_review
from bridge.config import Config
from bridge.delivery import handle_slack_event
from bridge.store import Invalid, Store
from bridge.teams import TeamsConfig, TeamsDelivery
from test_delivery import DeliveryCase, FakeSlack


class ContextReplyContract:
    def setUp(self):
        super().setUp()
        self.delivery.base_url = ''
        self.configure_transport()
        self.addCleanup(self.delivery.close)
        self.task_id = self.task()
        self.node_id = self.node(self.task_id)['node_id']
        self.delivery.deliver_now()
        self.message = self.slack.messages[0]
        self.counter = 0

    def configure_transport(self):
        pass

    def occurrence(self, n):
        if self.delivery.channel == 'teams':
            return {'platform': 'teams', 'id': f'synthetic-activity-{n}',
                    'timestamp': f'2033-05-18T03:33:20.{n:06d}Z', 'reply_to': self.message['ts']}
        return {'platform': 'slack', 'id': f'2000000000.{n:06d}',
                'timestamp': f'2000000000.{n:06d}', 'reply_to': self.message['ts']}

    def send(self, text, *, n=None, event_id=None):
        self.counter += 1
        n = self.counter if n is None else n
        return self.delivery.receive(self.message['channel'], self.message['ts'],
                                    self.wes if self.delivery.channel == 'teams' else 'UWES', text,
                                    event_id=event_id or f'synthetic-context-{n}', occurrence=self.occurrence(n))

    def notes(self):
        return canvas.get_tree(self.store, self.task_id)['notes']

    def decision_state(self):
        # Compare raw persistent state, including owner, signatures, rules and
        # timestamps, rather than merely checking an approval summary flag.
        return dict(self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (self.node_id,)).fetchone())

    def test_ordinary_context_is_visible_before_semantic_interpretation(self):
        text = 'The synthetic rollout checklist still needs a current review.'
        with patch('bridge.slack_chat.respond', return_value='Would you like me to ask the agent?') as infer:
            self.assertIn('Context added', self.send('context: ' + text))
        self.assertEqual([note['text'] for note in self.notes()], [text])
        infer.assert_not_called()

    def test_notification_kinds_accept_context_without_state_changes(self):
        for kind in ('ask', 'reassigned', 'signoff', 'review', 'overdue', 'escalation'):
            with self.subTest(kind=kind):
                if kind != 'ask':
                    self.assertIsNotNone(self.store.notify(self.node_id, kind))
                    self.delivery.deliver_now()
                self.assertEqual(self.delivery.notification_for_thread(
                    self.message['channel'], self.message['ts'])['kind'], kind)
                before = self.decision_state()
                authorities = self.graph.authority_rows()
                text = f'{kind}: Approve this later because the audit example is incomplete; rule if ready.'
                with patch('bridge.slack_chat.respond', side_effect=AssertionError('semantic interpretation')):
                    self.assertIn('Context added', self.send('CoNtExT : ' + text))
                self.assertEqual(self.notes()[-1]['text'], text)
                self.assertEqual(self.decision_state(), before)
                self.assertEqual(self.graph.authority_rows(), authorities)
                self.assertEqual(self.graph.db.execute('SELECT count(*) FROM routing_feedback').fetchone()[0], 0)

    def test_because_and_approval_words_bypass_real_source_review(self):
        with self.graph.transaction():
            upstream = self.graph.add_decision(self.task_id, 'Which synthetic constraint applies?', '',
                                               'pending', owner='Wes Chen', repo='acme/platform')
            self.graph.add_link(self.node_id, upstream, 'depends')
        self.store.answer(upstream, {'answer': 'Retain the audit record.'})
        self.assertTrue(source_review.has_sources(self.store.get_decision(self.node_id)))
        before = self.decision_state()
        body = 'I have not signed because the synthetic record needs inspection. approve; answer: hold; rule if audited.'
        with patch('bridge.source_review.offer_command', side_effect=AssertionError('source interpretation')), \
                patch('bridge.slack_chat.respond', side_effect=AssertionError('semantic interpretation')):
            self.assertIn('Context added', self.send('context: ' + body))
        self.assertEqual(self.notes()[0]['text'], body)
        self.assertEqual(self.decision_state(), before)
        self.assertIsNone(self.delivery._reading(self.message['channel'], self.message['ts'], self.wes))

    def test_context_in_a_real_signoff_thread_does_not_sign_the_answer(self):
        canvas.settle_node(self.store, {'task_id': self.task_id, 'node_id': self.node_id,
                                       'answer': 'Retain two synthetic records.', 'rationale': 'Test proposal.'})
        self.delivery.deliver_now()
        self.assertEqual(self.delivery.notification_for_thread(self.message['channel'], self.message['ts'])['kind'],
                         'signoff')
        before = self.decision_state()
        self.assertEqual(before['signoff'], 'required')
        self.send('context: Approve only after checking the synthetic record because the review is incomplete.')
        self.assertEqual(self.decision_state(), before)
        self.assertEqual(len(self.notes()), 1)

    def test_original_text_actor_and_transport_provenance_survive_restart(self):
        original = '  CoNtExT :  Keep this exact paragraph.\n  Another line: <@UOTHER>, because “quoted”.  '
        self.send(original, n=1)
        reopened = Store(self.store.path)
        self.addCleanup(reopened.graph.close)
        note = canvas.get_tree(reopened, self.task_id)['notes'][0]
        self.assertEqual(note['text'], 'Keep this exact paragraph.\n  Another line: <@UOTHER>, because “quoted”.')
        self.assertEqual((note['by'], note['actor_id'], note['actor_kind']), ('Wes Chen', self.wes, self.delivery.channel))
        self.assertEqual(note['source'], f'{self.delivery.channel}: Wes Chen')
        self.assertEqual(note['reply_source'], {
            'platform': self.delivery.channel, 'channel': self.message['channel'],
            'thread_ts': self.message['ts'], 'decision_id': self.node_id,
            'event_id': 'synthetic-context-1', 'occurrence': self.occurrence(1), 'text': original})
        events = canvas.trace(reopened, self.task_id)['events']
        event = next(e for e in events if e['kind'] == 'task_note')
        self.assertEqual(event['detail']['reply_source'], note['reply_source'])

    def test_note_visible_before_during_and_between_wait_calls(self):
        cursor = canvas.get_tree(self.store, self.task_id)['observed_at']
        empty = canvas.wait(self.store, {'task_id': self.task_id, 'since': cursor, 'timeout': 0})
        self.assertTrue(empty['timed_out'])
        self.assertEqual(empty['notes'], [])
        self.send('context: Arrived before the next wait.')
        before = canvas.wait(self.store, {'task_id': self.task_id, 'since': cursor, 'timeout': 0})
        self.assertFalse(before['timed_out'])
        self.assertEqual([n['text'] for n in before['notes']], ['Arrived before the next wait.'])
        self.assertEqual(before['changed'], [])
        ticks = [0.0]

        def receive_while_waiting(seconds):
            ticks[0] += seconds
            self.send('context: Arrived while waiting.')

        during = canvas.wait(self.store, {'task_id': self.task_id, 'since': before['observed_at'], 'timeout': 1},
                             sleep=receive_while_waiting, clock=lambda: ticks[0], interval=0.1)
        self.assertFalse(during['timed_out'])
        self.assertEqual([n['text'] for n in during['notes']], ['Arrived while waiting.'])
        quiet = canvas.wait(self.store, {'task_id': self.task_id, 'since': during['observed_at'], 'timeout': 0})
        self.assertTrue(quiet['timed_out'])
        self.assertEqual(quiet['notes'], [])
        self.send('context: Arrived between wait calls.')
        between = canvas.wait(self.store, {'task_id': self.task_id, 'since': quiet['observed_at'], 'timeout': 0})
        self.assertFalse(between['timed_out'])
        self.assertEqual([n['text'] for n in between['notes']], ['Arrived between wait calls.'])
        self.assertEqual(len(self.notes()), 3)
        # Existing notes are always on the tree. A cursor-free wait watches
        # new changes and does not replay notes that predate that call.
        self.assertEqual(canvas.wait(self.store, {'task_id': self.task_id, 'timeout': 0})['notes'], [])

    def test_same_event_retry_is_idempotent_and_mutated_payload_is_refused(self):
        self.send('context: A synthetic retry should leave one note.', n=1)
        saved = self.notes()
        self.assertEqual(self.send('context: A synthetic retry should leave one note.', n=1), '')
        self.assertEqual(self.notes(), saved)
        with self.assertRaisesRegex(Invalid, 'reused with different content'):
            self.send('context: Different content.', n=1)
        self.assertEqual(self.notes(), saved)

    def test_concurrent_note_retries_across_two_stores_append_once(self):
        from bridge.authz import Actor
        from bridge.delivery import _context_reply_source
        other = Store(self.store.path)
        self.addCleanup(other.graph.close)
        actor = Actor.person(self.graph.get_person(self.wes), kind=self.delivery.channel)
        source = _context_reply_source(self.delivery.channel, self.message['channel'], self.message['ts'],
                                       self.node_id, 'synthetic-concurrent-note', self.occurrence(1),
                                       'context: One note across connections.')
        ready = Barrier(2)

        def add(store):
            try:
                ready.wait(timeout=10)
                return canvas.add_note(store, self.task_id, {'text': 'One note across connections.'},
                                       actor=actor, reply_source=source)
            finally:
                store.graph.close_thread()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(add, store) for store in (self.store, other)]
            self.assertEqual([len(result.result(timeout=15)['notes']) for result in results], [1, 1])
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(self.notes()[0]['reply_source'], source)

    def test_out_of_order_context_cannot_append_a_note(self):
        self.send('context: Newer context.', n=2)
        self.assertIn('out of order', self.send('context: Older context.', n=1))
        self.assertEqual([note['text'] for note in self.notes()], ['Newer context.'])

    def test_private_question_and_chat_stay_private_until_explicit_context(self):
        cursor = canvas.get_tree(self.store, self.task_id)['observed_at']
        before = self.decision_state()
        empty = canvas.wait(self.store, {'task_id': self.task_id, 'since': cursor, 'timeout': 0})
        for kind in ('question', 'chat'):
            with self.subTest(kind=kind), \
                    patch.object(Config, 'semantic_retrieval', property(lambda _: True)), \
                    patch('bridge.slack_chat.load', return_value=Config(model_api='none')), \
                    patch('bridge.slack_chat.reading', return_value={'kind': kind, 'reply': 'A private clarification.'}):
                self.assertIn('A private clarification.', self.send('Could you explain the synthetic checklist?'))
            self.assertEqual(self.notes(), [])
            self.assertEqual(self.decision_state(), before)
        quiet = canvas.wait(self.store, {'task_id': self.task_id, 'since': cursor, 'timeout': 0})
        self.assertEqual(quiet['notes'], [])
        self.assertTrue(quiet['timed_out'])
        self.assertIn('no new decision or forwarded task note is observable', quiet['next'])
        self.assertIn('this does not prove that nobody replied privately', quiet['next'])
        self.assertEqual(quiet['next'], empty['next'], 'Timeout guidance must not reveal whether a private reply exists')
        self.assertNotIn('A private clarification.', quiet['next'])
        self.send('context: Please inspect the synthetic checklist before implementation.')
        self.assertEqual(len(self.notes()), 1)

    def test_context_leaves_existing_pending_readback_unchanged(self):
        with patch.object(Config, 'semantic_retrieval', property(lambda _: True)), \
                patch('bridge.slack_chat.load', return_value=Config(model_api='none')), \
                patch('bridge.slack_chat.reading', return_value={'kind': 'answer', 'answer': 'Retain two synthetic records.'}):
            self.send('Retain two synthetic records.')
        held = self.delivery._reading(self.message['channel'], self.message['ts'], self.wes)
        self.assertIsNotNone(held)
        before = self.decision_state()
        self.send('context: This separate note supplies the audit checklist.')
        self.assertEqual(self.delivery._reading(self.message['channel'], self.message['ts'], self.wes), held)
        self.assertEqual(self.decision_state(), before)
        self.assertEqual(len(self.notes()), 1)

    def test_empty_or_oversized_context_is_not_reinterpreted(self):
        for text in ('context: ', 'context: ' + 'x' * 4001,
                     ' ' * 4096 + 'context: Small body.', 'context:' + ' ' * 4096 + 'Small body.'):
            with self.subTest(length=len(text)), \
                    patch('bridge.slack_chat.respond', side_effect=AssertionError('semantic interpretation')):
                self.assertIn('Nothing recorded', self.send(text))
        self.assertEqual(self.notes(), [])

    def test_maximum_note_body_is_preserved(self):
        body = 'x' * 4000
        self.assertIn('Context added', self.send('context: ' + body))
        self.assertEqual(self.notes()[0]['text'], body)

    def test_legacy_missing_occurrence_is_not_fabricated(self):
        person = self.wes if self.delivery.channel == 'teams' else 'UWES'
        self.delivery.receive(self.message['channel'], self.message['ts'], person,
                              'context: Legacy explicit note.', event_id='synthetic-legacy')
        self.assertIsNone(self.notes()[0]['reply_source']['occurrence'])

    def test_occurrence_metadata_is_bounded_and_normalized(self):
        person = self.wes if self.delivery.channel == 'teams' else 'UWES'
        occurrence = {**self.occurrence(1), 'unrelated': {'untrusted': 'not retained'}}
        self.delivery.receive(self.message['channel'], self.message['ts'], person,
                              'context: Known occurrence fields only.', event_id='synthetic-normalized', occurrence=occurrence)
        self.assertEqual(self.notes()[0]['reply_source']['occurrence'], self.occurrence(1))
        reply = self.delivery.receive(self.message['channel'], self.message['ts'], person,
                                      'context: Oversized occurrence.', event_id='synthetic-oversized',
                                      occurrence={**self.occurrence(2), 'reply_to': 'x' * 2049})
        self.assertIn('Nothing recorded', reply)
        self.assertEqual(len(self.notes()), 1)

    def test_unknown_thread_or_person_cannot_create_a_note(self):
        self.assertEqual(self.delivery.receive('unknown-channel', 'unknown-thread', 'unknown-person',
                                              'context: Not associated with any task.'), '')
        self.delivery.receive(self.message['channel'], self.message['ts'], 'unknown-person',
                              'context: This sender is not authenticated to a person.')
        self.assertEqual(self.notes(), [])

    def test_note_data_cannot_claim_transport_provenance(self):
        from bridge.authz import Actor
        canvas.add_note(self.store, self.task_id,
                        {'text': 'An ordinary note.', 'by': 'Fabricated Author',
                         'actor_id': 'fabricated-id', 'reply_source': {'event_id': 'fabricated-event'}},
                        actor=Actor.person(self.graph.get_person(self.wes), kind=self.delivery.channel))
        note = self.notes()[0]
        self.assertEqual(note['by'], 'Wes Chen')
        self.assertNotIn('reply_source', note)


class ExplicitContextRepliesTests(ContextReplyContract, DeliveryCase):
    def test_actual_reassigned_recipient_can_forward_context(self):
        self.store.refer(self.node_id, {'person': self.marisol, 'by': 'Wes Chen'})
        self.delivery.deliver_now()
        message = self.slack.messages[-1]
        before = self.decision_state()
        self.assertEqual(self.delivery.notification_for_thread(message['channel'], message['ts'])['kind'], 'reassigned')
        self.assertIn('Context added', self.reply(message, 'UMAR',
                                                'context: The replacement reviewer needs the synthetic checklist.'))
        self.assertEqual(self.notes()[0]['by'], 'Marisol Vega')
        self.assertEqual(self.notes()[0]['actor_id'], self.marisol)
        self.assertEqual(self.notes()[0]['reply_source']['decision_id'], self.node_id)
        self.assertEqual(self.decision_state(), before)

    def test_retry_after_note_commit_does_not_duplicate_it(self):
        text = 'context: Preserve this synthetic note across a failed acknowledgement.'
        with patch('bridge.slack_chat.remember', side_effect=RuntimeError('synthetic acknowledgement failure')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic acknowledgement failure'):
                self.send(text, n=1)
        saved = self.notes()
        self.assertEqual(len(saved), 1)
        self.assertIn('Context added', self.send(text, n=1))
        self.assertEqual(self.notes(), saved)
        self.assertEqual(self.graph.db.execute("SELECT state FROM webhook_receipts WHERE id='synthetic-context-1'").fetchone()[0],
                         'applied')

    def test_slack_adapter_keeps_exact_occurrence_and_task_association(self):
        payload = {'type': 'event_callback', 'team_id': 'TTEST', 'event_id': 'synthetic-slack-event',
                   'event': {'type': 'message', 'channel': self.message['channel'], 'thread_ts': self.message['ts'],
                             'user': 'UWES', 'ts': '2000000000.000001', 'text': 'context: Adapter context.'}}
        with patch.object(self.delivery.inbox, 'start'):
            handle_slack_event(self.delivery, payload)
            handle_slack_event(self.delivery, payload)
        self.assertEqual(len(self.notes()), 1)
        source = self.notes()[0]['reply_source']
        self.assertEqual(source['event_id'], payload['event_id'])
        self.assertEqual(source['occurrence'], self.occurrence(1))
        self.assertEqual(source['decision_id'], self.node_id)


class FakeTeams(FakeSlack):
    name = 'teams'


class TeamsExplicitContextRepliesTests(ContextReplyContract, DeliveryCase):
    def configure_transport(self):
        self.delivery.close()
        self.slack = FakeTeams()
        self.oid = '33333333-3333-4333-8333-333333333333'
        self.teams_config = TeamsConfig('11111111-1111-4111-8111-111111111111',
                                       '22222222-2222-4222-8222-222222222222', 'synthetic-channel',
                                       'unused-synthetic-secret', {self.oid: self.wes})
        # Authentication is outside this contract. No credentials, JWTs or
        # provider requests are created to exercise the shared reply handler.
        self.delivery = TeamsDelivery(self.store, self.teams_config, transport=self.slack, auth=object())
        self.store._delivery = self.delivery

    def test_teams_adapter_keeps_exact_occurrence_and_task_association(self):
        self.graph.db.execute('INSERT INTO teams_threads(id,destination,conversation_id,root_activity_id) VALUES(?,?,?,?)',
                              (self.message['ts'], self.message['channel'], 'synthetic-conversation', 'synthetic-root'))
        config = self.teams_config
        stamp = datetime.now(timezone.utc).isoformat()
        activity = {'type': 'message', 'id': 'synthetic-teams-activity', 'channelId': 'msteams',
                    'timestamp': stamp, 'replyToId': 'synthetic-root', 'text': 'context: Adapter context.',
                    'conversation': {'id': 'synthetic-conversation', 'conversationType': 'channel', 'tenantId': config.tenant_id},
                    'channelData': {'tenant': {'id': config.tenant_id}, 'channel': {'id': config.channel_id}},
                    'from': {'id': 'synthetic-member', 'aadObjectId': self.oid, 'name': 'Untrusted display name'},
                    'recipient': {'id': config.bot_id}}
        with patch.object(self.delivery.inbox, 'start'):
            self.delivery.inbox.enqueue(activity)
            self.delivery.inbox.enqueue(activity)
            self.delivery.inbox.process()
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(self.notes()[0]['by'], 'Wes Chen')
        source = self.notes()[0]['reply_source']
        self.assertEqual(source['event_id'], self.graph.db.execute('SELECT id FROM teams_ingress').fetchone()[0])
        self.assertEqual(source['occurrence'], {'platform': 'teams', 'id': activity['id'], 'timestamp': stamp,
                                               'reply_to': activity['replyToId']})
        self.assertEqual(source['decision_id'], self.node_id)
