import io
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request

import httpx

from evals.customer_provider_gateway import Gateway, _UsageObserver


class MemoryHandler:
    """A provider request without a socket or any external transport."""
    def __init__(self, gate, path='/v1/messages'):
        self.path = path
        raw = json.dumps({'model': gate.model, 'max_tokens': 10,
                          'messages': [{'role': 'user', 'content': 'synthetic'}]}).encode()
        self.headers = {'x-api-key': gate.placeholder, 'Content-Length': str(len(raw))}
        self.rfile, self.wfile = io.BytesIO(raw), io.BytesIO()
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, *args):
        pass

    def end_headers(self):
        pass


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        return iter(self.chunks)


class GatewayTests(unittest.TestCase):
    def body(self, **changes):
        return {'model': 'claude-sonnet-4-6', 'max_tokens': 10,
                'messages': [{'role': 'user', 'content': 'synthetic'}], **changes}

    def success(self, request):
        self.assertEqual(str(request.url), 'https://api.anthropic.com/v1/messages')
        self.assertEqual(request.headers['x-api-key'], 'fixture-key')
        self.assertNotIn('authorization', request.headers)
        self.assertEqual(request.headers['anthropic-workspace-id'], 'fixture-workspace')
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': 'fixture response'}],
                                        'usage': {'input_tokens': 12, 'output_tokens': 3}})

    def gateway(self, **changes):
        return Gateway('fixture-key', 'fixture-workspace', transport=httpx.MockTransport(self.success), **changes)

    def post(self, gateway, **changes):
        with httpx.Client(trust_env=False) as client:
            return client.post(gateway.url + '/v1/messages', headers={'x-api-key': gateway.placeholder},
                               json=self.body(**changes))

    def test_real_transport_shape_and_usage_without_credentials_in_snapshot(self):
        with self.gateway() as gate:
            response = self.post(gate)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['content'][0]['text'], 'fixture response')
            snap = gate.snapshot()
            self.assertEqual(snap['physical_requests'], 1)
            self.assertEqual(snap['requests'][0]['usage'], {'input_tokens': 12, 'output_tokens': 3})
            for forbidden in ('fixture-key', gate.placeholder, 'synthetic', 'fixture response'):
                self.assertNotIn(forbidden, json.dumps(snap))
        self.assertEqual(gate._key, '')

    def test_no_unexpected_model_or_destination_is_forwarded(self):
        with self.gateway() as gate, httpx.Client(trust_env=False) as client:
            self.assertEqual(self.post(gate, model='unapproved-model').status_code, 400)
            response = client.post(gate.url + '/proxy?url=https://example.test',
                                   headers={'x-api-key': gate.placeholder}, json=self.body())
            self.assertEqual(response.status_code, 403)
            self.assertEqual(gate.snapshot()['physical_requests'], 0)

    def test_random_local_auth_required(self):
        with self.gateway() as gate, httpx.Client(trust_env=False) as client:
            response = client.post(gate.url + '/v1/messages', json=self.body())
            self.assertEqual(response.status_code, 401)
            self.assertEqual(gate.snapshot()['physical_requests'], 0)

    def test_workspace_caller_header_cannot_override_pinned_workspace(self):
        with self.gateway() as gate, httpx.Client(trust_env=False) as client:
            result = client.post(gate.url + '/v1/messages', json=self.body(), headers={
                'x-api-key': gate.placeholder, 'anthropic-workspace-id': 'wrong',
                'Authorization': 'Bearer wrong'})
            self.assertEqual(result.status_code, 200)

    def test_call_and_output_reservation_budgets_are_hard_limits(self):
        for limits in ({'max_calls': 1}, {'max_output_tokens': 10}):
            with self.subTest(limits=limits), self.gateway(**limits) as gate:
                self.assertEqual(self.post(gate).status_code, 200)
                self.assertEqual(self.post(gate).status_code, 429)
                self.assertEqual(gate.snapshot()['physical_requests'], 1)

    def test_input_limit_refuses_without_forwarding(self):
        with self.gateway(max_input_bytes=50) as gate:
            self.assertEqual(self.post(gate).status_code, 400)
            self.assertEqual(gate.snapshot()['physical_requests'], 0)

    def test_concurrent_calls_cannot_overrun_budget(self):
        with self.gateway(max_calls=2) as gate, ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(lambda _: self.post(gate).status_code, range(4)))
            self.assertEqual(statuses.count(200), 2)
            self.assertEqual(statuses.count(429), 2)
            self.assertEqual(gate.snapshot()['physical_requests'], 2)

    def test_provider_error_stops_following_requests_and_scrubs_body(self):
        transport = httpx.MockTransport(lambda _: httpx.Response(401, json={'error': 'sensitive upstream text'}))
        with Gateway('fixture-key', transport=transport) as gate:
            response = self.post(gate)
            self.assertEqual(response.status_code, 401)
            self.assertNotIn('sensitive', response.text)
            self.assertEqual(self.post(gate).status_code, 429)
            self.assertTrue(gate.snapshot()['halted'])

    def test_redirect_is_never_followed(self):
        seen = []
        def redirect(request):
            seen.append(str(request.url))
            return httpx.Response(302, headers={'location': 'https://evil.invalid/'})
        with Gateway('fixture-key', transport=httpx.MockTransport(redirect)) as gate:
            self.assertEqual(self.post(gate).status_code, 302)
            self.assertEqual(seen, ['https://api.anthropic.com/v1/messages'])

    def test_stream_is_forwarded_and_usage_only_retained(self):
        stream = (b'event: message_start\ndata: {"message":{"usage":{"input_tokens":20}}}\n\n'
                  b'event: content_block_delta\ndata: {"delta":{"text":"fixture-text"}}\n\n'
                  b'event: message_delta\ndata: {"usage":{"output_tokens":7}}\n\n')
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=stream,
                                         headers={'content-type': 'text/event-stream'}))
        with Gateway('fixture-key', transport=transport) as gate:
            self.assertEqual(self.post(gate, stream=True).content, stream)
            snapshot = gate.snapshot()
            self.assertEqual(snapshot['requests'][0]['usage'], {'input_tokens': 20, 'output_tokens': 7})
            self.assertNotIn('fixture-text', json.dumps(snapshot))

    def test_production_urllib_adapter_uses_same_budget(self):
        with self.gateway(max_calls=1) as gate:
            request = Request('https://api.anthropic.com/v1/messages', data=json.dumps(self.body()).encode(),
                              headers={'x-api-key': 'not-forwarded'})
            with gate.urllib_open(request, timeout=5) as response:
                self.assertEqual(json.load(response)['usage']['input_tokens'], 12)
            self.assertEqual(gate.snapshot()['physical_requests'], 1)
            with self.assertRaises(ValueError):
                gate.urllib_open('https://unapproved.invalid')

    def test_absolute_network_fragment_and_noncanonical_targets_never_forward(self):
        seen = []
        def upstream(request):
            seen.append(str(request.url))
            return httpx.Response(200, json={'usage': {'output_tokens': 1}})
        gate = Gateway('fixture-key', transport=httpx.MockTransport(upstream))
        self.addCleanup(gate.__exit__)
        targets = ('https://example.test/v1/messages',
                   'https://api.anthropic.com/v1/messages',
                   '//example.test/v1/messages', '///v1/messages',
                   '/v1/messages#fragment', '/v1/messages#', '/v1/messages?',
                   ' /v1/messages', '/v1/mes\tsages', 'http://[/v1/messages')
        for target in targets:
            with self.subTest(target=target):
                handler = MemoryHandler(gate, target)
                gate._handle(handler)
                self.assertEqual(handler.status, 403)
        self.assertEqual(seen, [])
        self.assertEqual(gate.snapshot()['physical_requests'], 0)

    def test_valid_targets_forward_only_to_exact_official_endpoint(self):
        seen = []
        def upstream(request):
            seen.append(str(request.url))
            self.assertEqual(request.url.host, 'api.anthropic.com')
            self.assertEqual(request.headers['x-api-key'], 'fixture-key')
            return httpx.Response(200, json={'usage': {'input_tokens': 1}})
        gate = Gateway('fixture-key', transport=httpx.MockTransport(upstream))
        self.addCleanup(gate.__exit__)
        paths = ('/v1/messages', '/v1/messages?beta=true', '/v1/messages/count_tokens')
        for path in paths:
            handler = MemoryHandler(gate, path)
            gate._handle(handler)
            self.assertEqual(handler.status, 200)
        self.assertEqual(seen, ['https://api.anthropic.com' + path for path in paths])

    def test_large_multichunk_stream_keeps_late_usage_and_forwards_every_byte(self):
        start = b'event: message_start\ndata: {"message":{"usage":{"input_tokens":20}}}\n\n'
        content = b'data: {"delta":{"text":"' + b'x' * 4000 + b'"}}\n\n'
        end = b'event: message_delta\ndata: {"usage":{"output_tokens":7}}\n\n'
        chunks = [start[:11], start[11:], *(content for _ in range(260)), end[:45], end[45:]]
        self.assertGreater(sum(map(len, chunks)), 1_000_000)
        transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=Chunks(chunks),
                                         headers={'content-type': 'text/event-stream'}))
        gate = Gateway('fixture-key', transport=transport)
        self.addCleanup(gate.__exit__)
        handler = MemoryHandler(gate)
        gate._handle(handler)
        self.assertEqual(handler.wfile.getvalue(), b''.join(chunks))
        record = gate.snapshot()['requests'][0]
        self.assertEqual(record['usage'], {'input_tokens': 20, 'output_tokens': 7})
        self.assertFalse(record.get('usage_unavailable', False))
        self.assertNotIn('xxxx', json.dumps(record))

    def test_oversized_sse_line_is_bounded_and_recovers_for_later_usage(self):
        record = {'usage': {}}
        observer = _UsageObserver('text/event-stream', record)
        observer.feed(b'data: ' + b'x' * (observer.MAX_LINE_BYTES - 10))
        self.assertLessEqual(len(observer._buffer), observer.MAX_LINE_BYTES)
        observer.feed(b'x' * 2_000_000)
        self.assertLessEqual(len(observer._buffer), observer.MAX_LINE_BYTES)
        observer.feed(b'\ndata: {"usage":{"output_tokens":9}}\n')
        observer.finish()
        self.assertTrue(record['usage_unavailable'])
        self.assertEqual(record['usage'], {'output_tokens': 9})
        self.assertEqual(observer._buffer, b'')

    def test_malformed_sse_lines_mark_usage_unavailable_and_keep_later_events(self):
        malformed = (b'not-json', b'\xff', b'[]', b'[' * 2000 + b']' * 2000, b'{"usage":"bad"}',
                     b'{"message":null}', b'{"usage":{"output_tokens":true}}')
        for value in malformed:
            with self.subTest(value=value):
                record = {'usage': {}}
                observer = _UsageObserver('text/event-stream', record)
                for chunk in (b'data:' + value + b'\r\n\r\n',
                              b'data: {"usage":{"output_tokens":', b'11}}\r\n'):
                    observer.feed(chunk)
                observer.finish()
                self.assertTrue(record['usage_unavailable'])
                self.assertEqual(record['usage'], {'output_tokens': 11})

    def test_oversized_json_response_has_bounded_retention_and_no_partial_usage(self):
        record = {'usage': {}}
        observer = _UsageObserver('application/json', record)
        observer.feed(b' ' * (observer.MAX_JSON_BYTES + 1))
        observer.feed(b'{"usage":{"output_tokens":9}}')
        observer.finish()
        self.assertEqual(observer._buffer, b'')
        self.assertTrue(record['usage_unavailable'])
        self.assertEqual(record['usage'], {})

    def test_urllib_adapter_rejects_provider_fragments_before_dispatch(self):
        gate = self.gateway()
        self.addCleanup(gate.__exit__)
        request = Request('https://api.anthropic.com/v1/messages#fragment',
                          data=json.dumps(self.body()).encode())
        with self.assertRaises(ValueError):
            gate.urllib_open(request)
        self.assertEqual(gate.snapshot()['physical_requests'], 0)


if __name__ == '__main__':
    unittest.main()
