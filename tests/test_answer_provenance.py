"""Synthetic, provider-free answer provenance through authenticated transports.

Source labels remain descriptive. Identity comes from the verified Actor; old
history and saved proof bundles are read through without transport backfills.
"""
import hashlib
import hmac
import json
import re
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from fixtures import ready_server
from test_delivery import DeliveryCase
import test_teams
import test_readback_generations
from bridge import canvas, proof
from bridge.authz import Actor, Refused
from bridge.config import Config
from bridge.mcp import call_tool
from bridge.store import Store, answer_hash


class ProvenanceAssertions:
    def event(self, kind):
        events = self.store.get_decision(self.node_id)['events']
        return json.loads(next(e['detail'] for e in reversed(events) if e['kind'] == kind))

    def assert_origin(self, kind, label):
        detail = self.event(kind)
        self.assertEqual(detail['source'], label)
        self.assertEqual(detail['actor_id'], self.wes)
        self.assertEqual(detail['actor_kind'], self.channel)
        return detail

    def settle_evidence(self):
        canvas.settle_node(self.store, {'task_id': self.task_id, 'node_id': self.node_id,
                                      'answer': 'Bill all traffic.', 'rationale': 'Existing evidence'})
        self.assertFalse(self.store.get_decision(self.node_id)['authorized'])

    def test_signed_explicit_new_answer_and_correction(self):
        self.say('answer: Exclude internal tests because they are synthetic')
        self.assert_origin('owner_approved', f'{self.channel}: Wes Chen')
        self.say('answer: Bill only customer traffic because it reflects usage')
        self.assert_origin('answer_corrected', f'{self.channel}: Wes Chen')
        row = self.store.get_decision(self.node_id)
        self.assertEqual(row['answer'], 'Bill only customer traffic')
        self.assertEqual(row['signed_by'], 'Wes Chen')
        self.assertEqual(row['signed_hash'], answer_hash(row['answer']))
        self.assertTrue(row['authorized'])

    def test_signed_explicit_correction_of_existing_evidence(self):
        self.settle_evidence()
        self.say('answer: Exclude internal tests because they are synthetic')
        self.assert_origin('owner_approved', f'{self.channel}: Wes Chen')
        self.assertEqual(self.event('previous_answer')['answer'], 'Bill all traffic.')
        self.assertTrue(self.store.get_decision(self.node_id)['authorized'])

    def confirm_conversational_answer(self):
        action = {'kind': 'answer', 'answer': 'Exclude only internal load tests.', 'rationale': ''}
        with patch('bridge.slack_chat.load', return_value=Config(model_api='none')), \
                patch.object(Config, 'semantic_retrieval', property(lambda _: True)), \
                patch('bridge.slack_chat.reading', return_value=action):
            source = self.say(action['answer'])
            self.assertFalse(self.store.get_decision(self.node_id)['authorized'])
            held, visible = self.readback()
            self.assertIsNotNone(held)
            self.assertIn(action['answer'], visible)
            code = re.search(r'confirm ([0-9a-f]{12})', visible)
            self.assertIsNotNone(code)
            self.assertEqual(code[1], held['proposal_id'])
            self.assertTrue(held['delivered_ref'])
            self.assertEqual(json.loads(held['source_occurrence']), source)
            confirmation = self.say('confirm ' + code[1])
        self.assert_origin('owner_approved', f'{self.channel}: Wes Chen (read back and confirmed)')
        self.assertTrue(self.store.get_decision(self.node_id)['authorized'])
        consent = self.event('readback_confirmed')
        self.assertEqual(consent['source_occurrence'], source)
        self.assertEqual(consent['confirmation_occurrence'], confirmation)
        self.assertEqual(consent['actor_id'], self.wes)

    def test_signed_conversational_new_answer(self):
        self.confirm_conversational_answer()

    def test_signed_conversational_correction_preserves_source(self):
        self.settle_evidence()
        self.confirm_conversational_answer()
        self.assertEqual(self.event('previous_answer')['answer'], 'Bill all traffic.')

    def test_signed_plain_signoff_records_transport(self):
        self.settle_evidence()
        self.say('sign off')
        self.assert_origin('signoff', f'{self.channel}: Wes Chen')
        self.assertTrue(self.store.get_decision(self.node_id)['authorized'])


class SlackProvenanceTests(ProvenanceAssertions, DeliveryCase):
    channel = 'slack'

    def setUp(self):
        super().setUp()
        self.task_id = self.task()
        self.node_id = self.node(self.task_id)['node_id']
        self.delivery.deliver_now()
        self.message = self.slack.messages[0]
        self.counter = 0
        self.addCleanup(self.delivery.close)
        worker = patch.object(self.delivery.inbox, 'start')
        worker.start()
        self.addCleanup(worker.stop)
        self.secret = 'synthetic-provenance-signing-secret'
        self.server = ready_server(self.store, port=0, slack_signing_secret=self.secret)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def say(self, text):
        self.counter += 1
        # Original synthetic message occurrence, distinct from callback receipt
        # time and newer than FakeSlack's delivered read-back timestamps.
        occurrence = {'platform': 'slack', 'id': f'1700000001.{self.counter:06d}',
                      'timestamp': f'1700000001.{self.counter:06d}', 'reply_to': self.message['ts']}
        body = json.dumps({'type': 'event_callback', 'team_id': 'TTEST',
            'event_id': f'EvProvenance{self.counter}', 'event': {'type': 'message',
            'channel': self.message['channel'], 'thread_ts': self.message['ts'],
            'user': 'UWES', 'text': text, 'ts': occurrence['timestamp']}}).encode()
        stamp = str(int(time.time()))
        signature = 'v0=' + hmac.new(self.secret.encode(), b'v0:' + stamp.encode() + b':' + body,
                                   hashlib.sha256).hexdigest()
        request = Request(f'http://127.0.0.1:{self.server.server_port}/webhooks/slack', body,
            {'Content-Type': 'application/json', 'X-Slack-Request-Timestamp': stamp,
             'X-Slack-Signature': signature})
        with urlopen(request) as response:
            self.assertEqual(response.status, 200)
        self.delivery.inbox.process()
        return occurrence

    def readback(self):
        return (self.delivery._reading(self.message['channel'], self.message['ts'], self.wes),
                self.slack.messages[-1]['text'])


@unittest.skipUnless(test_teams.jwt, 'Install requirements-teams.txt to test verified Teams replies')
class TeamsProvenanceTests(ProvenanceAssertions, unittest.TestCase):
    channel = 'teams'

    def setUp(self):
        # Reuse only fixture setup/helpers, without inheriting and rerunning
        # the complete Teams contract suite under a second test class.
        self.fixture = test_readback_generations.TeamsReadbackGenerationTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.store, self.wes = self.fixture.store, self.fixture.wes
        self.node_id, self.task_id = self.fixture.node, self.fixture.task

    def say(self, text):
        # The activity keeps its original ID/time through authenticated ingress;
        # the synthetic clock advances beyond both the source and delivery ACK.
        activity = self.fixture.stamped(text, seconds=max(
            self.fixture.counter, time.time() - self.fixture.epoch + 1))
        self.fixture.submit(activity)
        return {'platform': 'teams', 'id': activity['id'], 'timestamp': activity['timestamp'],
                'reply_to': activity.get('replyToId', '')}

    def readback(self):
        return self.fixture.held(), self.fixture.microsoft.messages[-1]['text']


class AnswerEventProvenanceTests(DeliveryCase):
    def setUp(self):
        super().setUp()
        self.task_id = self.task()
        self.node_id = self.node(self.task_id)['node_id']
        self.addCleanup(self.delivery.close)
        self.actor = Actor.person(self.graph.get_person(self.wes), kind='slack')

    def details(self, kind):
        return [json.loads(e['detail']) for e in self.store.get_decision(self.node_id)['events'] if e['kind'] == kind]

    def test_local_operator_and_internal_fallback_remain_explicit(self):
        self.store.answer(self.node_id, {'answer': 'Local answer.'})
        detail = self.details('owner_approved')[-1]
        self.assertEqual((detail['source'], detail['actor_id'], detail['actor_kind']),
                         ('local operator', '', 'internal'))
        self.store.answer(self.node_id, {'answer': 'Operator correction.'}, actor=Actor(kind='operator'))
        detail = self.details('answer_corrected')[-1]
        self.assertEqual((detail['source'], detail['actor_id'], detail['actor_kind']),
                         ('local operator', '', 'operator'))

    def test_source_label_and_client_fields_cannot_replace_authenticated_identity(self):
        payload = {'answer': 'Keep current behavior.', 'source': 'teams: Another Person',
                   'actor_id': self.marisol, 'actor_kind': 'operator', 'signed_by': 'Another Person',
                   'by': 'Another Person'}
        self.store.answer(self.node_id, payload, actor=self.actor)
        detail = self.details('owner_approved')[-1]
        self.assertEqual(detail['source'], payload['source'])
        self.assertEqual((detail['actor_id'], detail['actor_kind'], detail['actor'], detail['basis']),
                         (self.wes, 'slack', 'Wes Chen', 'owner'))
        payload.update(answer='Correct current behavior.', expected_updated_at=self.store.get_decision(self.node_id)['updated_at'])
        canvas.sign_off(self.store, self.node_id, payload, actor=self.actor)
        detail = self.details('answer_corrected')[-1]
        self.assertEqual(detail['source'], payload['source'])
        self.assertEqual((detail['actor_id'], detail['actor_kind'], detail['actor']),
                         (self.wes, 'slack', 'Wes Chen'))
        row = self.store.get_decision(self.node_id)
        self.assertEqual((row['actor_id'], row['signed_by'], row['source']), (self.wes, 'Wes Chen', 'human'))

    def test_forged_source_does_not_grant_authority(self):
        outsider = Actor.person(self.graph.get_person(self.marisol), kind='teams')
        before = self.store.get_decision(self.node_id)['events']
        with self.assertRaises(Refused):
            self.store.answer(self.node_id, {'answer': 'Forged answer.', 'source': 'slack: Wes Chen',
                                            'actor_id': self.wes, 'actor_kind': 'operator'}, actor=outsider)
        self.assertEqual(self.store.get_decision(self.node_id)['events'], before)

    def test_answer_and_cosignature_events_keep_each_signers_identity(self):
        with self.graph.transaction():
            self.graph.db.execute('UPDATE decisions SET required_signers=? WHERE id=?',
                                 (json.dumps(['Wes Chen', 'Marisol Vega', 'Priya Natarajan']), self.node_id))
        self.store.answer(self.node_id, {'answer': 'Require three signatures.'}, actor=self.actor)
        for person_id, kind in ((self.marisol, 'teams'), (self.priya, 'session')):
            actor = Actor.person(self.graph.get_person(person_id), kind=kind)
            canvas.sign_off(self.store, self.node_id, {
                'expected_updated_at': self.store.get_decision(self.node_id)['updated_at'],
                'source': 'Reviewed together', 'actor_id': 'client-supplied', 'actor_kind': 'operator'}, actor=actor)
        signatures = self.details('signature')
        self.assertEqual([(d['actor_id'], d['actor_kind']) for d in signatures],
                         [(self.wes, 'slack'), (self.marisol, 'teams')])
        signoff = self.details('signoff')[-1]
        self.assertEqual((signoff['actor_id'], signoff['actor_kind'], signoff['source']),
                         (self.priya, 'session', 'Reviewed together'))
        row = self.store.get_decision(self.node_id)
        self.assertTrue(row['authorized'])
        self.assertEqual(len(json.loads(row['signatures'])), 3)

    def test_history_and_proof_preserve_attribution_and_legacy_bytes(self):
        legacy = '{"answer": "Historical fixture", "source": "local operator"}'
        with self.store.connect() as db:
            self.store.event(db, 'owner_approved', legacy, self.node_id, self.task_id)
        self.store.answer(self.node_id, {'answer': 'Bill at two cents.', 'source': 'Slack owner readback'}, actor=self.actor)
        detail = self.details('owner_approved')[-1]
        history = canvas.trace(self.store, self.task_id)
        self.assertIn(detail, [e['detail'] for e in history['events']])
        self.assertIn(json.loads(legacy), [e['detail'] for e in history['events']])
        decision = call_tool(self.store, 'bridge_get_decision', {'decision_id': self.node_id, 'detail': 'full'})
        self.assertIn(detail, [json.loads(e['detail']) for e in decision['events']
                               if e['kind'] == 'owner_approved'])
        canvas.get_tree(self.store, self.task_id)
        diff = 'diff --git a/billing/usage.py b/billing/usage.py\n--- a/billing/usage.py\n+++ b/billing/usage.py\n@@ -1 +1 @@\n-rate = 1\n+rate = 2\n'
        with patch('bridge.llm.check_conformance', return_value=None):
            canvas.finish_task(self.store, {'task_id': self.task_id, 'diff': diff})
        exported = proof.export(self.store, {'task_id': self.task_id})
        saved = exported['bundle']
        attribution = saved['payload']['decisions'][0]['attribution']
        self.assertEqual((attribution['actor_id'], attribution['actor_name'], attribution['actor_basis']),
                         (self.wes, 'Wes Chen', 'owner'))
        self.assertTrue(exported['integrity']['valid'])
        self.assertFalse(exported['integrity']['authenticity_verified'])
        self.store.answer(self.node_id, {'answer': 'A later correction.'}, actor=self.actor)
        reopened = Store(self.store.path)
        self.addCleanup(reopened.graph.close)
        again = proof.export(reopened, {'task_id': self.task_id})
        self.assertEqual(again['bundle'], saved)
        self.assertTrue(again['stale'])
        self.assertTrue(again['integrity']['valid'])
        stored = reopened.get_decision(self.node_id)['events']
        self.assertIn(legacy, [e['detail'] for e in stored])
        legacy_history = [e['detail'] for e in canvas.trace(reopened, self.task_id)['events']
                          if e['detail'] == json.loads(legacy)]
        self.assertEqual(legacy_history, [json.loads(legacy)])
        self.assertNotIn('actor_kind', legacy_history[0])
