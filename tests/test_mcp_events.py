"""Signed wake events: durable delivery, bounded subscriptions and no authority."""
import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import mcp_events as events
from bridge.auth import Auth
from bridge.store import Store, Invalid
from test_auth import SharedServer, BOOTSTRAP


class EventTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'events.db')
        self.g = self.store.graph
        self.addCleanup(self.g.close)
        self.person = self.g.add_person('Ari Example')
        self.auth = Auth(self.store, enabled=True)
        self.token = self.auth.create_token(self.person)
        self.task = self.store.add_run({'title': 'Add bounded retries', 'repo': 'example/service'})['id']
        self.secret = 'whsec_' + base64.b64encode(b'e' * 32).decode()
        self.params = {'name': events.NAME, 'arguments': {'task_id': self.task},
                       'delivery': {'mode': 'webhook', 'url': 'https://receiver.example/callback', 'secret': self.secret}}
        self.calls = []

    def sender(self, url, body, headers):
        self.calls.append((url, body, headers))
        value = json.loads(body)
        if value.get('type') == 'verification':
            return 200, json.dumps({'challenge': value['challenge']}).encode()
        return 202, b'{}'

    def subscribe(self, **extra):
        return events.subscription(self.store, {**self.params, **extra}, self.person, self.token['id'], sender=self.sender)

    def test_signed_delivery_is_durable_idempotent_and_contains_no_answer_or_secret(self):
        sub = self.subscribe()
        self.assertEqual(self.subscribe()['id'], sub['id'])
        self.assertEqual(len(self.calls), 1)  # short-lived verification cache
        self.g.append_event('answer_corrected', {'task_id': self.task, 'answer': 'PRIVATE ANSWER'})
        self.assertEqual(events.deliver(self.store, self.sender), 1)
        self.assertEqual(events.deliver(self.store, self.sender), 0)

        _, body, headers = self.calls[-1]
        payload = json.loads(body)
        self.assertEqual(payload['data']['task_id'], self.task)
        signed = headers['webhook-id'].encode() + b'.' + headers['webhook-timestamp'].encode() + b'.' + body
        signature = 'v1,' + base64.b64encode(hmac.new(b'e' * 32, signed, hashlib.sha256).digest()).decode()
        self.assertEqual(headers['webhook-signature'], signature)
        self.assertNotIn('PRIVATE ANSWER', body.decode())
        self.assertNotIn(self.secret, json.dumps(self.store.state(full_history=True)))

    def test_retry_preserves_event_id_and_revocation_stops_delivery(self):
        self.subscribe()
        self.g.append_event('signature', {'task_id': self.task})
        failed = []
        def fail(url, body, headers):
            failed.append(json.loads(body)['eventId'])
            return 503, b'{}'
        self.assertEqual(events.deliver(self.store, fail), 0)
        self.g.db.execute('UPDATE mcp_event_deliveries SET next_attempt=0')
        self.assertEqual(events.deliver(self.store, self.sender), 1)
        self.assertEqual(json.loads(self.calls[-1][1])['eventId'], failed[0])
        self.auth.revoke_token(self.token['id'])
        self.g.append_event('signature', {'task_id': self.task})
        self.assertEqual(events.deliver(self.store, self.sender), 0)

    def test_failed_challenge_does_not_store_subscription(self):
        with self.assertRaises(events.CallbackError):
            events.subscription(self.store, self.params, self.person, self.token['id'], sender=lambda *a: (200, b'{"challenge":"wrong"}'))
        self.assertFalse(self.g.db.execute('SELECT 1 FROM mcp_event_subscriptions').fetchone())

    def test_expired_claim_cannot_overwrite_new_delivery_or_disable_subscription(self):
        sub = self.subscribe()
        self.g.append_event('signature', {'task_id': self.task})
        def delayed(url, body, headers):
            self.g.db.execute('UPDATE mcp_event_deliveries SET lease_until=0')
            self.assertEqual(events.deliver(self.store, self.sender), 1)
            return 410, b'{}'
        self.assertEqual(events.deliver(self.store, delayed), 0)
        row = self.g.db.execute("SELECT state,attempts FROM mcp_event_deliveries WHERE state<>'baseline'").fetchone()
        self.assertEqual((row['state'], row['attempts']), ('sent', 2))
        self.assertEqual(self.g.db.execute('SELECT active FROM mcp_event_subscriptions WHERE id=?', (sub['id'],)).fetchone()[0], 1)

    def test_expiry_unsubscribe_and_rotation(self):
        sub = self.subscribe()
        self.params['delivery']['secret'] = 'whsec_' + base64.b64encode(b'n' * 32).decode()
        self.subscribe()
        self.g.append_event('signature', {'task_id': self.task})
        events.deliver(self.store, self.sender)
        self.assertEqual(len(self.calls[-1][2]['webhook-signature'].split()), 2)
        events.subscription(self.store, self.params, self.person, self.token['id'], remove=True)
        self.g.append_event('signature', {'task_id': self.task})
        self.assertEqual(events.deliver(self.store, self.sender), 0)
        self.subscribe()
        self.g.db.execute('UPDATE mcp_event_subscriptions SET expires=? WHERE id=?', (time.time() - 1, sub['id']))
        self.assertEqual(events.deliver(self.store, self.sender), 0)

    def test_no_callback_to_private_or_mixed_dns_answers(self):
        for url in ('http://receiver.example/a', 'https://user:pass@receiver.example/a', 'https://receiver.example:8080/a'):
            with self.subTest(url=url), self.assertRaises(events.CallbackError):
                events.destination(url)
        for addresses in (['127.0.0.1'], ['169.254.169.254'], ['::1'], ['8.8.8.8', '10.0.0.1']):
            with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', (a, 443)) for a in addresses]), self.assertRaises(events.CallbackError):
                events.destination('https://receiver.example/a')

    def test_wrong_principal_and_invalid_secret_are_refused(self):
        other = self.g.add_person('Bo Example')
        with self.assertRaises(Invalid):
            events.subscription(self.store, self.params, other, self.token['id'], sender=self.sender)
        self.params['delivery']['secret'] = 'whsec_c2hvcnQ='
        with self.assertRaises(Invalid):
            self.subscribe()

    def test_terminal_delivery_failure_is_retained_without_retry(self):
        self.subscribe()
        self.g.append_event('source_review_required', {'task_id': self.task})
        events.deliver(self.store, lambda *a: (413, b''))
        row = self.g.db.execute('SELECT state,last_error FROM mcp_event_deliveries').fetchone()
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(row['last_error'], 'HTTP 413')
        self.assertEqual(events.deliver(self.store, self.sender), 0)

    def test_late_lower_id_event_is_not_lost_and_visible_history_is_not_replayed(self):
        self.g.append_event('signature', {'task_id': self.task})
        self.subscribe()
        self.assertEqual(events.deliver(self.store, self.sender), 0)
        self.g.append_event('answer_corrected', {'task_id': self.task})
        delayed = dict(self.g.db.execute("SELECT * FROM events WHERE kind='answer_corrected'").fetchone())
        self.g.db.execute('DELETE FROM events WHERE id=?', (delayed['id'],))
        self.g.append_event('signature', {'task_id': self.task})
        self.assertEqual(events.deliver(self.store, self.sender), 1)
        # Reproduce a row becoming visible after a higher allocated ID, as
        # independent PostgreSQL transactions can do when they commit.
        columns = list(delayed)
        self.g.db.execute('INSERT INTO events(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')',
                          [delayed[c] for c in columns])
        self.assertEqual(events.deliver(self.store, self.sender), 1)
        self.assertEqual(json.loads(self.calls[-1][1])['data']['event_kind'], 'answer_corrected')


class EventHTTPTests(SharedServer):
    def setUp(self):
        super().setUp()
        person = self.post('/api/people', {'name': 'Ari Example'}, token=BOOTSTRAP)
        self.token = self.post('/api/tokens', {'person_id': person['id']}, token=BOOTSTRAP)['token']
        self.task = self.mcp('bridge_start_task', {'title': 'Bound retries', 'repo': 'example/service'}, self.token)['result']['task_id']

    def rpc(self, method, params):
        return self.post('/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}, token=self.token)

    def test_modern_claude_discovery_exposes_callable_tools_and_legacy_stays_compatible(self):
        meta = {'io.modelcontextprotocol/protocolVersion': events.VERSION,
                'io.modelcontextprotocol/clientInfo': {'name': 'claude-code', 'version': '2.1.296'},
                'io.modelcontextprotocol/clientCapabilities': {'roots': {'listChanged': True}}}
        for method in ('server/discover', 'tools/list', 'events/list'):
            result = self.rpc(method, {'_meta': meta})['result']
            self.assertEqual(result['resultType'], 'complete')
            self.assertEqual(result['ttlMs'], 0)
            self.assertEqual(result['cacheScope'], 'private')
            self.assertIn('io.modelcontextprotocol/serverInfo', result['_meta'])
            if method == 'tools/list':
                self.assertIn('bridge_get_tree', [t['name'] for t in result['tools']])
            if method == 'server/discover':
                self.assertTrue(result['instructions'])
        for task, failed in ((self.task, False), ('missing-task', True)):
            result = self.rpc('tools/call', {'_meta': meta, 'name': 'bridge_get_tree',
                                           'arguments': {'task_id': task}})['result']
            self.assertEqual(result['resultType'], 'complete')
            self.assertEqual(result['isError'], failed)
            self.assertTrue(result['content'])
        self.assertNotIn('resultType', self.rpc('tools/list', {})['result'])
        self.assertNotIn('resultType', self.rpc('tools/call', {'name': 'bridge_get_tree',
                         'arguments': {'task_id': self.task}})['result'])

    def test_discover_subscribe_callback_and_unsubscribe_over_authenticated_http(self):
        discovery = self.rpc('server/discover', {})['result']
        self.assertIn(events.VERSION, discovery['supportedVersions'])
        self.assertIn('events', discovery['capabilities'])
        self.assertEqual(self.rpc('events/list', {})['result']['events'][0]['name'], events.NAME)
        calls = []
        def receiver(url, body, headers):
            value = json.loads(body)
            calls.append((value, headers))
            return 200, json.dumps({'challenge': value.get('challenge', '')}).encode()
        params = {'name': events.NAME, 'arguments': {'task_id': self.task}, 'delivery': {
            'mode': 'webhook', 'url': 'https://receiver.example/events',
            'secret': 'whsec_' + base64.b64encode(b'h' * 32).decode()}}
        with patch('bridge.mcp_events.post', side_effect=receiver):
            result = self.rpc('events/subscribe', params)['result']
            self.store.graph.append_event('discovery_reviewed', {'task_id': self.task})
            self.assertEqual(events.deliver(self.store), 1)
        self.assertEqual(calls[1][0]['data']['event_kind'], 'discovery_reviewed')
        self.assertEqual(calls[1][1]['X-MCP-Subscription-Id'], result['id'])
        self.assertEqual(self.rpc('events/unsubscribe', params)['result'], {})

    def test_callback_errors_have_protocol_code_and_old_clients_still_work(self):
        legacy = self.rpc('initialize', {'protocolVersion': '2025-06-18'})['result']
        self.assertEqual(legacy['protocolVersion'], '2025-06-18')
        params = {'name': events.NAME, 'arguments': {'task_id': self.task}, 'delivery': {
            'mode': 'webhook', 'url': 'https://receiver.example/events',
            'secret': 'whsec_' + base64.b64encode(b'h' * 32).decode()}}
        with patch('bridge.mcp_events.post', return_value=(200, b'{"challenge":"wrong"}')):
            result = self.rpc('events/subscribe', params)
        self.assertEqual(result['error']['code'], -32015)
        self.assertEqual(result['error']['data']['reason'], 'challenge_failed')
