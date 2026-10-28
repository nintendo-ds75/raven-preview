"""OpenCode native-driver contracts: scripted server, no live inference."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch

from bridge import opencode_host as host


SPEC = {'name': 'read_file', 'description': 'read', 'input_schema': {'type': 'object'}}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.bridge = host.Bridge([SPEC], self.events.append)
        self.bridge.session_id = 'ses_test'

    def tearDown(self):
        self.bridge.close()

    def test_http_requires_private_token(self):
        request = Request(self.bridge.url, data=b'{}', headers={'Content-Type': 'application/json'})
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 403)

    def test_native_call_id_roundtrips_and_completed_retry_reaches_durable_parent(self):
        args = {'name': 'read_file', 'arguments': {'path': 'a.py', '_raven_context': {'session_id': 'ses_test', 'call_id': 'native-1'}}}
        results = []
        event = threading.Event()
        def emit(value):
            self.events.append(value)
            event.set()
        self.bridge.emit = emit
        worker = threading.Thread(target=lambda: results.append(self.bridge.call(args)))
        worker.start()
        self.assertTrue(event.wait(2))
        with self.assertRaisesRegex(ValueError, 'duplicate_pending'):
            self.bridge.call(args)
        self.bridge.deliver({'id': 'native-1', 'result': {'body': 'source'}})
        worker.join(2)
        self.assertEqual(self.events[0]['call']['input'], {'path': 'a.py'})
        self.assertEqual(json.loads(results[0]['content'][0]['text']), {'body': 'source'})
        event.clear()
        retry = threading.Thread(target=lambda: results.append(self.bridge.call(args)))
        retry.start()
        self.assertTrue(event.wait(2))
        self.bridge.deliver({'id': 'native-1', 'result': {'body': 'source'}})
        retry.join(2)
        self.assertEqual(len(self.events), 2)
        self.assertEqual(self.events[0]['call'], self.events[1]['call'])
        self.assertEqual(results[0], results[1])

    def test_foreign_session_unknown_tool_and_missing_native_id_fail_closed(self):
        for context, name in [({'session_id': 'other', 'call_id': 'x'}, 'read_file'),
                              ({'session_id': 'ses_test'}, 'read_file'),
                              ({'session_id': 'ses_test', 'call_id': 'x'}, 'bash')]:
            with self.subTest(context=context, name=name), self.assertRaises(ValueError):
                self.bridge.call({'name': name, 'arguments': {'_raven_context': context}})
        self.assertFalse(self.events)

    def test_stop_prevents_tools(self):
        self.bridge.stopped.set()
        with self.assertRaises(ValueError):
            self.bridge.call({'name': 'read_file', 'arguments': {'_raven_context': {'session_id': 'ses_test', 'call_id': 'x'}}})


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {'cwd': self.temp.name, 'session_id': '', 'model_provider': 'openai', 'model': 'test',
                       'system': 'safe', 'tools': [SPEC], 'max_turns': 2, 'budget_usd': 1, 'prompt': 'task'}

    def test_security_config_has_no_native_tools_or_discovery(self):
        config = host.configuration(self.config, 'http://127.0.0.1:1/mcp', 'private')
        self.assertEqual(config['permission'], {'*': 'deny', 'raven_*': 'allow'})
        self.assertEqual(config['tools'], {'*': False, 'raven_*': True})
        self.assertEqual(config['enabled_providers'], ['openai'])
        self.assertEqual(config['mcp']['raven']['oauth'], False)
        self.assertEqual(config['plugin'], [str(host.ROOT / 'guard.mjs')])
        self.assertFalse(config['compaction']['auto'])

    def test_unknown_version_or_schema_is_not_compatible(self):
        with self.assertRaisesRegex(ValueError, 'version'):
            host.validate_protocol({'healthy': True, 'version': '2'}, {})
        with self.assertRaisesRegex(ValueError, 'protocol'):
            host.validate_protocol({'healthy': True, 'version': host.VERSION}, {})

    def test_interrupted_start_never_prompts(self):
        events = []
        home = Path(self.temp.name)
        class Server:
            def __init__(self, config, bridge):
                self.home = home
            def ready(self):
                pass
            def close(self):
                pass
            def request(self, route, data=None):
                if route == '/session':
                    return {'id': 'ses_test'}
                raise AssertionError('Interrupted host prompted')
        with patch.object(host, 'Server', Server):
            host.run(self.config, events.append, lambda callback: callback({'kind': 'interrupt'}) or (lambda: None))
        self.assertEqual(events[-1]['status'], 'interrupted')
        self.assertTrue(events[-1]['is_error'])

    @unittest.skipUnless(host.installation() == host.VERSION, 'optional pinned OpenCode dependency not installed')
    def test_installed_server_auth_schema_guard_config_and_abort_without_inference(self):
        events = []
        with patch.dict(os.environ, {'HOME': self.temp.name, 'RAVEN_OPENCODE_API_KEY': 'offline-not-a-credential'}):
            host.run(self.config, events.append, lambda callback: callback({'kind': 'interrupt'}) or (lambda: None))
        self.assertTrue(events[0]['id'].startswith('ses'))
        self.assertEqual(events[-1]['status'], 'interrupted')
        self.assertEqual(events[-1]['cost_usd'], 0)

    @unittest.skipUnless(host.installation() == host.VERSION, 'optional pinned OpenCode dependency not installed')
    def test_installed_native_harness_tool_loop_with_local_scripted_inference(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        requests = []
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                first = len(requests) == 1
                block = ({'type': 'tool_use', 'id': 'native_fixture_call', 'name': 'raven_read_file', 'input': {}}
                         if first else {'type': 'text', 'text': ''})
                frames = [('message_start', {'type': 'message_start', 'message': {'id': 'msg_fixture', 'type': 'message',
                    'role': 'assistant', 'content': [], 'model': 'fixture', 'stop_reason': None, 'stop_sequence': None,
                    'usage': {'input_tokens': 1, 'output_tokens': 0}}}),
                    ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': block}),
                    ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta':
                        {'type': 'input_json_delta', 'partial_json': '{}'} if first else {'type': 'text_delta', 'text': 'Verified fixture'}}),
                    ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
                    ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use' if first else 'end_turn', 'stop_sequence': None},
                                       'usage': {'output_tokens': 1}}), ('message_stop', {'type': 'message_stop'})]
                raw = ''.join('event: ' + name + '\ndata: ' + json.dumps(value) + '\n\n' for name, value in frames).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        thread = threading.Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        original = host.configuration
        def configure(config, url, token):
            value = original(config, url, token)
            value['provider']['anthropic'] = {'options': {'apiKey': 'offline', 'baseURL': f'http://127.0.0.1:{provider.server_port}/v1'},
                'models': {'fixture': {'name': 'fixture', 'limit': {'context': 100000, 'output': 4096}}}}
            return value
        events, callbacks = [], []
        resumed = []
        def emit(value):
            events.append(value)
            if value['kind'] == 'tool':
                callbacks[0]({'kind': 'tool_result', 'id': value['call']['id'], 'result': {'body': 'fixture content'}})
        try:
            with patch.dict(os.environ, {'HOME': self.temp.name, 'RAVEN_OPENCODE_API_KEY': 'offline'}), \
                    patch.object(host, 'configuration', configure):
                host.run({**self.config, 'model_provider': 'anthropic', 'model': 'fixture'}, emit,
                         lambda callback: callbacks.append(callback) or (lambda: None))
                host.run({**self.config, 'session_id': events[0]['id'], 'model_provider': 'anthropic', 'model': 'fixture'},
                         resumed.append, lambda callback: (lambda: None))
        finally:
            provider.shutdown()
            provider.server_close()
            thread.join(2)
        self.assertEqual(len(requests), 3)
        self.assertEqual(resumed[0]['id'], events[0]['id'])
        self.assertEqual(resumed[-1]['status'], 'completed')
        self.assertIn('fixture content', json.dumps(requests[-1]['messages']))
        self.assertEqual([tool['name'] for tool in requests[0].get('tools', [])], ['raven_read_file'])
        calls = [event['call'] for event in events if event['kind'] == 'tool']
        self.assertEqual(calls, [{'id': 'native_fixture_call', 'name': 'read_file', 'input': {}}])
        self.assertEqual(events[-1]['status'], 'completed')
