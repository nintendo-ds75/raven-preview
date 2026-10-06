import io
import json
import threading
import time
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from test_delivery import DeliveryCase
from bridge.delivery import SlackTransport, SlackRateLimited
from bridge.mcp import dispatch
from bridge.server import make_server


class RetryAfterTests(DeliveryCase):
    def test_slack_429_preserves_server_retry_after_without_leaking_token(self):
        headers = Message()
        headers['Retry-After'] = '120'
        error = HTTPError('https://slack.com/api/chat.postMessage', 429, 'rate limited', headers, io.BytesIO())
        with patch('urllib.request.urlopen', side_effect=error), self.assertRaises(SlackRateLimited) as caught:
            SlackTransport('never-log-test-token').post_message('C1', 'hello')
        self.assertEqual(caught.exception.retry_after, 120)
        self.assertNotIn('never-log-test-token', str(caught.exception))

    def test_notification_waits_at_least_as_long_as_slack_requires(self):
        self.node(self.task())
        before = time.time()
        with patch.object(self.slack, 'post_message', side_effect=SlackRateLimited('chat.postMessage', 360)):
            self.assertEqual(self.delivery.deliver_now(), 0)
        row = self.delivery.list()[0]
        self.assertGreaterEqual(row['next_attempt'], before + 360)
        self.assertEqual(row['state'], 'queued')
        self.assertEqual(self.delivery.deliver_now(), 0)

    def test_method_cooldown_survives_transport_restart(self):
        first = SlackTransport('fixture-token')
        first.bind_rate_store(self.graph)
        first._set_cooldown('chat.postMessage', 180)
        restarted = SlackTransport('fixture-token')
        restarted.bind_rate_store(self.graph)
        with patch('urllib.request.urlopen', side_effect=AssertionError('sent during persisted cooldown')):
            with self.assertRaises(SlackRateLimited) as caught:
                restarted.post_message('C1', 'second message')
        self.assertGreater(caught.exception.retry_after, 179)

    def test_reply_acknowledgement_honors_retry_after(self):
        before = time.time()
        with patch.object(self.slack, 'post_message', side_effect=SlackRateLimited('chat.postMessage', 240)):
            self.delivery.inbox.ack('ack-contract', 'C1', '1.0', 'Recorded')
        row = self.graph.db.execute('SELECT * FROM slack_replies').fetchone()
        self.assertGreaterEqual(row['next_attempt'], before + 240)
        self.assertEqual(row['state'], 'queued')


class MCPVersionTests(DeliveryCase):
    def test_http_rejects_unsupported_version_and_accepts_negotiated_version(self):
        server = make_server(self.store, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = 'http://127.0.0.1:' + str(server.server_port) + '/mcp'
            body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).encode()
            headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                       'MCP-Protocol-Version': 'not-supported'}
            with self.assertRaises(HTTPError) as rejected:
                urlopen(Request(url, body, headers))
            self.assertEqual(rejected.exception.code, 400)
            headers['MCP-Protocol-Version'] = '2025-06-18'
            with urlopen(Request(url, body, headers)) as response:
                self.assertEqual(response.status, 200)
                self.assertIn('tools', json.load(response)['result'])
        finally:
            server.shutdown(); server.server_close(); thread.join(3)

    def test_initialize_preserves_supported_version_and_negotiates_unknown_version(self):
        for offered, expected in [('2025-03-26', '2025-03-26'), ('2025-06-18', '2025-06-18'),
                                  ('future-version', '2025-06-18')]:
            reply = dispatch(self.store, {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                          'params': {'protocolVersion': offered}})
            self.assertEqual(reply['result']['protocolVersion'], expected)

    def malformed_calls(self):
        return [
            {'method': 'initialize', 'params': {'protocolVersion': []}},
            {'method': 'initialize', 'params': {'protocolVersion': {}}},
            {'method': 'initialize', 'params': {'protocolVersion': 17}},
            {'method': 'tools/call', 'params': {'name': [], '_meta': {'progressToken': 1}}},
            {'method': 'tools/call', 'params': {'name': {}, '_meta': {'progressToken': 1}}},
            {'method': 'tools/call', 'params': {'name': 'bridge_connection_status', 'arguments': []}},
            {'method': 'tools/call', 'params': {'name': 'bridge_connection_status', 'arguments': None}},
        ]

    def test_malformed_params_preserve_request_id_and_protocol_error(self):
        for call in self.malformed_calls():
            with self.subTest(call=call):
                reply = dispatch(self.store, {'jsonrpc': '2.0', 'id': 'bad-request', **call}, notify=lambda note: None)
                self.assertEqual(reply['id'], 'bad-request')
                self.assertEqual(reply['error']['code'], -32602)
        self.assertEqual(dispatch(self.store, {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'})['result'], {})

    def test_http_malformed_params_match_stdio_without_internal_errors(self):
        server = make_server(self.store, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = 'http://127.0.0.1:' + str(server.server_port) + '/mcp'
            headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
            for call in self.malformed_calls():
                with self.subTest(call=call):
                    body = json.dumps({'jsonrpc': '2.0', 'id': 'bad-request', **call}).encode()
                    with urlopen(Request(url, body, headers)) as response:
                        reply = json.load(response)
                    self.assertEqual(reply['id'], 'bad-request')
                    self.assertEqual(reply['error']['code'], -32602)
            body = json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'ping'}).encode()
            with urlopen(Request(url, body, headers)) as response:
                self.assertEqual(json.load(response)['result'], {})
        finally:
            server.shutdown(); server.server_close(); thread.join(3)
