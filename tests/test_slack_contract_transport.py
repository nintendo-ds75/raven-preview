"""Slack's documented HTTP envelopes exercised by the shipped customer paths."""
import json
import time
import unittest
from unittest.mock import patch

from fixtures import ROOT  # Standard test import/environment isolation.
from evals.slack_contract_harness import (
    ACTION_TOKEN, BOT_TOKEN, OWNER, REFERRED, SOURCE_TEXT, SOURCE_URL,
    RavenContractHarness, SlackContractServer, eventually, http_json, run_contract,
)
from bridge.delivery import SlackTransport
from bridge.llm import LLMError


class SlackWireContractTests(unittest.TestCase):
    def setUp(self):
        self.slack = SlackContractServer()
        self.addCleanup(self.slack.close)
        self.transport = SlackTransport(BOT_TOKEN, api_base=self.slack.url)

    def test_api_test_accepts_json_and_form_and_returns_slack_errors(self):
        for payload, raw, headers in [({'foo': 'bar'}, None, {}),
                (None, b'foo=bar', {'Content-Type': 'application/x-www-form-urlencoded'})]:
            status, body, _ = http_json(self.slack.url + '/api.test', payload, headers, raw=raw)
            self.assertEqual((status, body), (200, {'ok': True, 'args': {'foo': 'bar'}}))
        status, body, _ = http_json(self.slack.url + '/api.test', {'error': 'fixture_error'})
        self.assertEqual(status, 200)
        self.assertFalse(body['ok'])
        self.assertEqual(body['error'], 'fixture_error')

    def test_real_transport_consumes_cursor_directory_dm_thread_and_search(self):
        self.assertEqual(self.transport.workspace_id(), 'T012CONTRACT')
        self.assertEqual(len(self.transport.list_users()), 7)
        pages = [r['payload'] for r in self.slack.requests if r['method'] == 'users.list']
        self.assertEqual([p.get('cursor', '') for p in pages], ['', 'dXNlcjpDMg==', 'dXNlcjpDNA=='])
        channel = self.transport.open_dm(OWNER)
        self.assertEqual(self.transport.open_dm(OWNER), channel)
        self.assertEqual(sum(r['method'] == 'conversations.open' for r in self.slack.requests), 1)
        ts = self.transport.post_message(channel, 'A question')
        reply_ts = self.transport.post_message(channel, 'A reply', thread_ts=ts)
        self.assertNotEqual(ts, reply_ts)
        self.assertEqual(self.slack.messages[-1]['thread_ts'], ts)
        self.assertEqual(self.transport.search_context('invoice tests', ACTION_TOKEN), [
            {'author': REFERRED, 'text': SOURCE_TEXT, 'url': SOURCE_URL}])
        self.assertEqual(self.slack.violations, [])
        self.assertNotIn(ACTION_TOKEN, json.dumps(self.slack.requests))
        self.assertNotIn(BOT_TOKEN, json.dumps(self.slack.requests))

    def test_http_200_ok_false_is_an_error_not_a_delivered_message(self):
        self.slack.fail_once('chat.postMessage', error='channel_not_found')
        with self.assertRaisesRegex(RuntimeError, 'channel_not_found'):
            self.transport.post_message('D012OWNER', 'Must not be delivered')
        self.assertEqual(self.slack.messages, [])
        self.transport.post_message('D012OWNER', 'Recovered next request')
        self.assertEqual(len(self.slack.messages), 1)

    def test_429_preserves_retry_after_over_real_http(self):
        self.slack.fail_once('chat.postMessage', status=429, error='ratelimited', retry_after=5)
        with self.assertRaises(RuntimeError) as failure:
            self.transport.post_message('D012OWNER', 'Rate limited')
        self.assertEqual(getattr(failure.exception, 'retry_after', None), 5)
        self.assertNotIn(BOT_TOKEN, str(failure.exception))
        self.assertEqual(self.slack.messages, [])

    def test_invalid_auth_and_cursor_use_slack_envelopes(self):
        with self.assertRaisesRegex(RuntimeError, 'invalid_auth'):
            SlackTransport('invalid-synthetic-token', self.slack.url).workspace_id()
        status, body, _ = http_json(self.slack.url + '/users.list', {'cursor': 'invalid'},
                                  {'Authorization': 'Bearer ' + BOT_TOKEN})
        self.assertEqual((status, body), (200, {'ok': False, 'error': 'invalid_cursor'}))


class SlackCustomerPathTests(unittest.TestCase):
    def test_authenticated_mcp_full_lifecycle_and_durable_signed_callbacks(self):
        result = run_contract()
        self.assertEqual(result['status'], 'passed')
        self.assertIn('not shipped', result['buttons'])
        self.assertTrue({'bridge_start_task', 'bridge_add_node', 'bridge_wait',
                         'bridge_get_tree', 'bridge_finish_task', 'bridge_export_proof'}
                        <= set(result['mcp_tools']))

    def test_missing_search_scope_is_disclosed_and_does_not_authorize(self):
        with RavenContractHarness() as h:
            _, node, message = h.begin()
            h.slack.fail_once('assistant.search.context', error='missing_scope')
            _, reply = h.say(message, 'What did the billing team say about internal tests?',
                             expected='Slack search was unavailable', action_token=ACTION_TOKEN)
            self.assertNotIn('Slack source', reply['text'])
            self.assertFalse(h.call('bridge_get_decision', decision_id=node)['authorized'])

    def test_provider_failure_does_not_authorize_or_invent_a_reply(self):
        with RavenContractHarness() as h:
            _, node, message = h.begin()
            with patch('bridge.llm.Client.complete', side_effect=LLMError('Synthetic provider outage')):
                h.say(message, 'Please exclude internal load tests, but still bill real customer traffic.',
                      expected='Nothing was changed')
            self.assertFalse(h.call('bridge_get_decision', decision_id=node)['authorized'])

    def test_wrong_person_cannot_confirm_an_owner_readback(self):
        with RavenContractHarness() as h:
            _, node, message = h.begin()
            from evals.slack_contract_harness import confirmation_text
            _, readback = h.say(message, 'Please exclude internal load tests, but still bill real customer traffic.',
                  expected='Use this exact text as the answer only:')
            h.say(message, confirmation_text(readback), user=REFERRED, expected='no read-back')
            self.assertFalse(h.call('bridge_get_decision', decision_id=node)['authorized'])
            h.say(message, confirmation_text(readback), expected='Recorded')
            decision = h.call('bridge_get_decision', decision_id=node)
            self.assertTrue(decision['authorized'])
            self.assertEqual(decision['signed_by'], 'Wes Contract')

    def test_retry_after_delays_real_callback_reply_and_recovers(self):
        with RavenContractHarness() as h:
            _, node, message = h.begin()
            start = len(h.slack.requests)
            h.slack.fail_once('chat.postMessage', status=429, error='ratelimited', retry_after=3)
            before = time.monotonic()
            event = h.event(message, 'Tell me more about this decision.', event_id='EvReply429')
            self.assertEqual(h.callback(event)[0], 200)
            self.assertLess(time.monotonic() - before, 3, 'Webhook must acknowledge before inference/retries')
            reply = eventually(lambda: h.slack.matching_messages(channel=message['channel'],
                                contains='Please state the decision'), 'retried HTTP reply')[0]
            calls = [r for r in h.slack.requests[start:] if r['method'] == 'chat.postMessage']
            self.assertEqual([r['response_status'] for r in calls], [429, 200])
            self.assertGreaterEqual(calls[1]['at'] - calls[0]['at'], 3)
            self.assertEqual(reply['thread_ts'], message['ts'])
            self.assertFalse(h.call('bridge_get_decision', decision_id=node)['authorized'])

    def test_retry_after_applies_to_other_replies_on_the_same_method(self):
        with RavenContractHarness() as h:
            _, _, message = h.begin()
            start = len(h.slack.requests)
            h.slack.fail_once('chat.postMessage', status=429, error='ratelimited', retry_after=3)
            self.assertEqual(h.callback(h.event(message, 'First context question.', event_id='EvCooldown1'))[0], 200)
            eventually(lambda: [r for r in h.slack.requests[start:] if r['method'] == 'chat.postMessage'
                                and r['response_status'] == 429], '429 response')
            self.assertEqual(h.callback(h.event(message, 'Second context question.', event_id='EvCooldown2'))[0], 200)
            eventually(lambda: len(h.slack.matching_messages(channel=message['channel'],
                                contains='Please state the decision')) == 2, 'both deferred replies')
            calls = [r for r in h.slack.requests[start:] if r['method'] == 'chat.postMessage']
            self.assertEqual([r['response_status'] for r in calls], [429, 200, 200])
            self.assertGreaterEqual(min(r['at'] for r in calls[1:]) - calls[0]['at'], 3)

    def test_identity_lookup_429_retries_after_slack_delay(self):
        with RavenContractHarness() as h:
            _, _, message = h.begin()
            start = len(h.slack.requests)
            h.slack.fail_once('users.info', status=429, error='ratelimited', retry_after=3)
            h.say(message, 'Tell me more about this decision.', expected='Please state the decision')
            calls = [r for r in h.slack.requests[start:] if r['method'] == 'users.info']
            self.assertEqual([r['response_status'] for r in calls], [429, 200])
            self.assertGreaterEqual(calls[1]['at'] - calls[0]['at'], 3)

    def test_url_verification_and_bot_callbacks_do_not_create_decisions(self):
        with RavenContractHarness() as h:
            self.assertEqual(h.callback({'type': 'url_verification', 'challenge': 'synthetic-challenge'}),
                             (200, {'challenge': 'synthetic-challenge'}))
            _, node, message = h.begin()
            count = len(h.slack.messages)
            self.assertEqual(h.callback(h.event(message, 'yes', bot_id='B012RAVEN'))[0], 200)
            time.sleep(.3)
            self.assertEqual(len(h.slack.messages), count)
            self.assertFalse(h.call('bridge_get_decision', decision_id=node)['authorized'])


if __name__ == '__main__':
    unittest.main()
