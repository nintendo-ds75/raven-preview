"""Exact installed app-server schemas; opt-in native loop with local scripted SSE.

No live provider inference. Full native acceptance is explicit because nested
Codex sandboxes can reject thread creation before the fixture is ever called.
"""
import base64
import gzip
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shlex
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bridge.workbench_hosts import CODEX_ROOT, CODEX_VERSION, stop_process_group

ROOT = Path(__file__).resolve().parents[1]
BINARY = CODEX_ROOT / 'node_modules' / '.bin' / 'codex'


def assert_denial_receipt(case, request, call_id, tool_name):
    """Require an explicit refusal for this exact attempted native call.

    A command crash, empty output, or merely absent canary is not containment.
    Inputs are native provider wire receipts, not worker-generated assertions.
    """
    receipts = [item for item in request.get('input', [])
                if item.get('type') in ('function_call_output', 'custom_tool_call_output') and item.get('call_id') == call_id]
    case.assertEqual(len(receipts), 1, 'Missing/ambiguous native refusal receipt for ' + call_id)
    output = receipts[0].get('output')
    case.assertIsInstance(output, str, 'Native refusal must be a textual receipt')
    # Match the whole native dispatch refusal, never a command's stderr or
    # filesystem/network PermissionError after native execution has occurred.
    name = re.escape(tool_name)
    grammar = (rf'(?:unknown tool[: ]+{name}|unsupported (?:tool|call): {name}|'
               rf'tool {name} is (?:not found|not available|not allowed|disabled))\.?')
    if tool_name == 'request_user_input':
        grammar = r'(?:request_user_input is unavailable in default mode|unavailable in raven host)\.?'
    case.assertIsNotNone(re.fullmatch(grammar, output.lower().strip()),
                         'Native call did not explicitly refuse dispatch: ' + call_id + ': ' + repr(output))



def assert_no_native_execution(case, events):
    # Exact pinned ThreadItem types from native generated JSON schema. Unknown
    # types fail closed, not just the current list of known dangerous types.
    inert = {'userMessage', 'agentMessage', 'reasoning', 'functionCallOutput', 'dynamicToolCall'}
    lifecycle = [event for event in events if event.get('kind') == 'native_event'
                 and event['method'] in ('item/started', 'item/completed')]
    case.assertTrue(lifecycle, 'No native lifecycle telemetry; cannot establish negative execution')
    for event in lifecycle:
        case.assertIn(event.get('item_type'), inert, 'Forbidden or unknown native execution lifecycle')
    for event in events:
        if event.get('kind') == 'native_event':
            case.assertFalse(event['method'].startswith(('item/commandExecution/', 'item/fileChange/',
                                                        'item/mcpToolCall/', 'item/webSearch/')),
                             'Native execution emitted a delta even without a start/complete event')


class DenialReceiptTests(unittest.TestCase):
    def test_native_execution_telemetry_is_required_and_fail_closed(self):
        safe = {'kind':'native_event','method':'item/started','item_type':'agentMessage'}
        assert_no_native_execution(self, [safe])
        for kind in ('commandExecution','fileChange','mcpToolCall','webSearch','imageView','subAgentActivity','newUnknownTool'):
            for method in ('item/started','item/completed'):
                with self.assertRaises(AssertionError):
                    assert_no_native_execution(self, [safe, {'kind':'native_event','method':method,'item_type':kind}])
        with self.assertRaises(AssertionError):
            assert_no_native_execution(self, [])
        with self.assertRaises(AssertionError):
            assert_no_native_execution(self, [safe, {'kind':'native_event','method':'item/commandExecution/outputDelta'}])

    def test_requires_matching_explicit_native_refusal(self):
        for text, name in (('unknown tool exec_command','exec_command'), ('unsupported call: view_image','view_image'),
                           ('tool exec_command is disabled','exec_command'),
                           ('request_user_input is unavailable in Default mode','request_user_input'),
                           ('Unavailable in Raven host','request_user_input')):
            assert_denial_receipt(self, {'input':[{'type':'function_call_output','call_id':'attempt','output':text}]}, 'attempt', name)

    def test_absence_success_crash_or_unrelated_refusal_is_not_containment(self):
        for body in ({'input':[]}, {'input':[{'type':'function_call_output','call_id':'other','output':'unknown tool'}]},
                     *({'input':[{'type':'function_call_output','call_id':'attempt','output':text}]}
                       for text in ('', 'exit code 1', 'Traceback: file missing', 'Permission denied', 'Access denied', 'Process exited with code 1\nOutput: Permission denied',
                                    'Process exited with code 0\nOutput: unknown tool exec_command', 'unknown tool different_tool', 'success'))):
            with self.assertRaises(AssertionError):
                assert_denial_receipt(self, body, 'attempt', 'exec_command')


@unittest.skipUnless(BINARY.is_file(), 'optional exact Codex npm dependency not installed')
class NativeSchemaTests(unittest.TestCase):
    def test_installed_pin_initializes_native_stdio_without_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            process = subprocess.Popen([str(BINARY), 'app-server', '-c', 'analytics.enabled=false'],
                env={'PATH':os.environ['PATH'], 'HOME':str(home), 'CODEX_HOME':str(home)},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, start_new_session=True)
            inbox = queue.Queue()
            def read():
                for line in process.stdout:
                    inbox.put(json.loads(line))
                inbox.put(None)
            threading.Thread(target=read, daemon=True).start()
            try:
                process.stdin.write(json.dumps({'id':1,'method':'initialize','params':{
                    'clientInfo':{'name':'raven_offline_fixture','version':'1'},
                    'capabilities':{'experimentalApi':True}}})+'\n')
                process.stdin.flush()
                response = inbox.get(timeout=10)
                self.assertEqual(response.get('id'),1)
                self.assertIn(CODEX_VERSION,response['result']['userAgent'])
            finally:
                stop_process_group(process)
                process.stdin.close()
                process.stdout.close()

    def test_installed_pin_exposes_actual_start_resume_and_dynamic_tool_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {'PATH': os.environ['PATH'], 'HOME': str(home), 'CODEX_HOME': str(home)}
            version = subprocess.run([str(BINARY), '--version'], env=env, capture_output=True, text=True, check=True, timeout=10)
            self.assertEqual(version.stdout.strip(), 'codex-cli ' + CODEX_VERSION)
            subprocess.run([str(BINARY), 'app-server', 'generate-json-schema', '--experimental', '--out', str(home / 'schema')],
                           env=env, capture_output=True, text=True, check=True, timeout=20)
            start = json.loads((home / 'schema/v2/ThreadStartParams.json').read_text())
            self.assertIn('dynamicTools', start['properties'])
            self.assertIn('allowProviderModelFallback', start['properties'])
            resume = json.loads((home / 'schema/v2/ThreadResumeParams.json').read_text())
            self.assertIn('model', resume['properties'])
            self.assertIn('modelProvider', resume['properties'])
            # Resume has no fallback flag in this exact pin. Verify the returned
            # effective model/provider instead, not an invented request field.
            self.assertNotIn('allowProviderModelFallback', resume['properties'])
            for name in ('ThreadStartResponse', 'ThreadResumeResponse'):
                response = json.loads((home / ('schema/v2/' + name + '.json')).read_text())
                self.assertTrue({'model', 'modelProvider', 'thread'} <= set(response['required']))


@unittest.skipUnless(os.environ.get('BRIDGE_TEST_CODEX') == '1', 'full native Codex transport acceptance is opt-in')
class NativeLoopTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(BINARY.is_file(), 'Run npm ci --prefix hosts/codex --ignore-scripts')
        # Codex intentionally refuses installing helper aliases below /tmp.
        # Keep its private test HOME inside this ignored, operator-owned checkout.
        fixture_root = ROOT / '.bridge' / 'native-codex-fixtures'
        fixture_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=fixture_root)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'codex').mkdir(mode=0o700)
        (self.home / 'empty').mkdir()
        self.requests, self.provider_errors, self.network_hits = [], [], []
        self.attack_mode = False
        self.attack_count = 9
        self.canary = 'RAVEN_PRIVATE_FIXTURE_' + secrets.token_hex(16)
        (self.home / 'private-canary.txt').write_text(self.canary)
        def png_chunk(kind, data):
            return struct.pack('!I',len(data)) + kind + data + struct.pack('!I',zlib.crc32(kind + data))
        self.image_bytes = (b'\x89PNG\r\n\x1a\n' + png_chunk(b'IHDR',struct.pack('!2I5B',1,1,8,6,0,0,0))
                            + png_chunk(b'IDAT',zlib.compress(b'\x00\x12\x34\x56\xff')) + png_chunk(b'IEND',b''))
        (self.home / 'private-canary.png').write_bytes(self.image_bytes)
        fixture = self

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path.startswith('/forbidden-native-network'):
                    fixture.network_hits.append(self.path)
                    self.send_error(403)
                    return
                # Native clients can negotiate WebSockets. Refuse the upgrade
                # so the documented HTTP/SSE fallback stays local as well.
                if self.headers.get('Upgrade', '').lower() == 'websocket':
                    self.send_error(426)
                    return
                raw = json.dumps({'models': []}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                if not self.path.startswith('/v1/responses'):
                    fixture.network_hits.append(self.path)
                    self.send_error(403)
                    return
                try:
                    raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                    encoding = self.headers.get('Content-Encoding', '')
                    if encoding == 'gzip':
                        raw = gzip.decompress(raw)
                    elif encoding == 'zstd':
                        import zstandard
                        raw = zstandard.ZstdDecompressor().decompress(raw, max_output_size=8 * 1024 * 1024)
                    elif encoding:
                        raise ValueError('Unexpected request encoding')
                    body = json.loads(raw)
                    fixture.requests.append(body)
                    first = len(fixture.requests) == 1
                    item = ({'type': 'function_call', 'call_id': 'native-fixture-call', 'name': 'read_file', 'arguments': '{}'}
                            if first else {'type': 'message', 'role': 'assistant', 'id': 'fixture-message',
                                           'content': [{'type': 'output_text', 'text': 'Native fixture complete'}]})
                    if fixture.attack_mode:
                        position = (len(fixture.requests) - 1) % (fixture.attack_count + 1)
                        if position < fixture.attack_count:
                            item = fixture.attack_item(position, body.get('tools', []))
                    rid = 'fixture-response-' + str(len(fixture.requests))
                    frames = [{'type': 'response.created', 'response': {'id': rid}},
                              {'type': 'response.output_item.done', 'item': item},
                              {'type': 'response.completed', 'response': {'id': rid,
                               'usage': {'input_tokens': 2, 'output_tokens': 2, 'total_tokens': 4,
                                         'input_tokens_details': None, 'output_tokens_details': None}}}]
                    payload = ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except Exception as error:
                    fixture.provider_errors.append(type(error).__name__)
                    self.send_error(400)

        self.provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        threading.Thread(target=self.provider.serve_forever, daemon=True).start()
        self.addCleanup(self.provider.server_close)
        self.addCleanup(self.provider.shutdown)
        (self.home / 'codex/config.toml').write_text(
            f'openai_base_url = "http://127.0.0.1:{self.provider.server_port}/v1"\n'
            '[analytics]\nenabled = false\n')

    @staticmethod
    def advertised(tools, namespace=None):
        result = []
        for tool in tools:
            if tool.get('type') == 'namespace':
                result.extend(NativeLoopTests.advertised(tool.get('tools', []), tool.get('name')))
            else:
                result.append((namespace, tool.get('name'), tool.get('type')))
        return result

    def attack_item(self, position, tools):
        # Attack synthetic files/key and a loopback sink only, never real host
        # secrets or an external service. Exercise on both fresh and resumed
        # native sessions, even when a capability is not advertised.
        if position == 0:
            script = ("from pathlib import Path; import os; "
                      f"print(Path({str(self.home / 'private-canary.txt')!r}).read_text()); "
                      "print(os.environ.get('OPENAI_API_KEY',''))")
            name, arguments = 'exec_command', {'cmd': 'python3 -c ' + shlex.quote(script)}
        elif position == 1:
            name, arguments = 'view_image', {'path':str(self.home / 'private-canary.png')}
        elif position == 2:
            url = f'http://127.0.0.1:{self.provider.server_port}/forbidden-native-network'
            script = f"import urllib.request; urllib.request.urlopen({url!r},timeout=2).read()"
            name, arguments = 'exec_command', {'cmd':'python3 -c ' + shlex.quote(script)}
        elif position == 3:
            # apply_patch is a freeform custom tool, not a JSON function. A
            # function_call here would test the wrong native dispatch path.
            patch = ('*** Begin Patch\n*** Add File: ' + str(self.home / 'native-write.txt')
                     + '\n+NATIVE_WRITE_FORBIDDEN\n*** End Patch\n')
            return {'type':'custom_tool_call', 'name':'apply_patch', 'input':patch,
                    'call_id':'attack-' + str(len(self.requests))}
        elif position == 4:
            url = f'http://127.0.0.1:{self.provider.server_port}/forbidden-native-network'
            script = f"import urllib.request; urllib.request.urlopen({url!r},timeout=2).read()"
            name, arguments = 'shell_command', {'command':'python3 -c ' + shlex.quote(script)}
        elif position == 5:
            name, arguments = 'request_user_input', {'questions':[{
                'header':'Approval', 'id':'fixture_approval', 'question':'Authorize this synthetic fixture action?',
                'options':[{'label':'Yes','description':'Approve the synthetic action.'},
                           {'label':'No','description':'Do not approve the synthetic action.'}]}]}
        elif position == 6:
            name, arguments = 'create_goal', {'objective':'Forbidden native automatic continuation', 'token_budget':10}
        elif position == 7:
            name, arguments = 'update_goal', {'status':'complete'}
        else:
            name, arguments = 'read_file', {}
        match = next((entry for entry in self.advertised(tools) if entry[1] == name), (None,name,'function'))
        item = {'type':'function_call','name':name,'arguments':json.dumps(arguments),
                'call_id':'attack-' + str(len(self.requests))}
        if match[0]:
            item['namespace'] = match[0]
        return item

    def run_native(self, session_id='', interrupt_on_tool=False):
        env = {'PATH': os.environ['PATH'], 'HOME': str(self.home), 'CODEX_HOME': str(self.home / 'codex'),
               'OPENAI_API_KEY': 'offline-scripted-fixture', 'RAVEN_CODEX_BIN': str(BINARY),
               'PYTHONPATH': str(ROOT), 'PYTHONUNBUFFERED': '1'}
        process = subprocess.Popen([sys.executable, str(ROOT / 'tests/codex_transport_fixture.py')], cwd=self.home / 'empty', env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        events, inbox = [], queue.Queue()
        def send(value):
            process.stdin.write(json.dumps(value) + '\n')
            process.stdin.flush()
        def read():
            for line in process.stdout:
                inbox.put(json.loads(line))
            inbox.put(None)
        threading.Thread(target=read, daemon=True).start()
        try:
            send({'provider': 'codex', 'session_id': session_id, 'model': 'gpt-5.3-codex',
                  'cwd': str(self.home / 'empty'), 'system': 'Use only the Raven read_file tool.',
                  'prompt': 'Read the fixture' if not session_id else 'Continue the existing fixture session',
                  'tools': [{'name': 'read_file', 'description': 'Read the fixture through Raven',
                             'input_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}]})
            while True:
                event = inbox.get(timeout=45)
                if event is None:
                    break
                events.append(event)
                if event['kind'] == 'tool' and interrupt_on_tool:
                    send({'kind':'interrupt'})
                elif event['kind'] == 'tool':
                    send({'kind': 'tool_result', 'id': event['call']['id'], 'result': {'body': 'RAVEN_FIXTURE_VALUE'}})
            assert_no_native_execution(self, events)
            self.assertFalse(self.provider_errors, self.provider_errors)
            self.assertFalse(any(event['kind'] == 'error' for event in events),
                'Native app-server failed before a terminal receipt. Nested Codex sandbox setup may block thread creation; do not disable sandbox controls. Events: ' + repr(events))
            results = [event for event in events if event['kind'] == 'result']
            self.assertTrue(results, 'No native final receipt')
            self.assertEqual(results[-1]['status'], 'interrupted' if interrupt_on_tool else 'completed', results)
            self.assertTrue(any(event['kind'] == 'delivery' for event in events))
            return events
        finally:
            stop_process_group(process)
            process.stdin.close()
            process.stdout.close()

    def test_native_dynamic_tool_and_persistent_resume_with_scripted_inference(self):
        events = self.run_native()
        calls = [event['call'] for event in events if event['kind'] == 'tool']
        self.assertEqual(calls, [{'id': 'native-fixture-call', 'name': 'read_file', 'input': {}}])
        session = next(event['id'] for event in events if event['kind'] == 'session')
        resumed = self.run_native(session)
        self.assertEqual(next(event['id'] for event in resumed if event['kind'] == 'session'), session)
        self.assertEqual(len(self.requests), 3)
        self.assertIn('RAVEN_FIXTURE_VALUE', json.dumps(self.requests[-1]['input']))
        for request in self.requests:
            self.assert_native_tool_schema(request['tools'])

    def test_native_interruption_has_terminal_receipt_and_resumes_same_session(self):
        first = self.run_native(interrupt_on_tool=True)
        session = next(event['id'] for event in first if event['kind'] == 'session')
        self.assertEqual(len([event for event in first if event['kind'] == 'tool']), 1)
        resumed = self.run_native(session)
        self.assertEqual(next(event['id'] for event in resumed if event['kind'] == 'session'), session)
        self.assertNotIn('RAVEN_FIXTURE_VALUE', json.dumps(self.requests))
        for request in self.requests:
            self.assert_native_tool_schema(request['tools'])

    def assert_native_tool_schema(self, tools):
        expected = json.loads((ROOT / 'tests/fixtures/codex-native-tool-schema.json').read_text())
        # Exact names, namespaces, types, schemas, and descriptions from the
        # pinned native wire snapshot. The sole native tool is denied below;
        # its advertisement is not an authority grant or Raven judgment.
        self.assertEqual(tools, expected)

    def test_native_shell_read_image_and_network_are_denied_before_and_after_resume(self):
        self.attack_mode = True
        first = self.run_native()
        session = next(event['id'] for event in first if event['kind'] == 'session')
        second = self.run_native(session)
        self.assertEqual(next(event['id'] for event in second if event['kind'] == 'session'), session)
        cycle = self.attack_count + 1
        self.assertEqual(len(self.requests), cycle * 2, 'All adversarial stages must run fresh and resumed')
        self.assertFalse((self.home / 'native-write.txt').exists(), 'Native apply_patch wrote outside Raven')
        self.assertEqual((self.home / 'private-canary.txt').read_text(), self.canary)
        self.assertFalse(self.network_hits, 'A native builtin reached the loopback exfiltration sink')
        bodies = json.dumps(self.requests)
        self.assertNotIn(self.canary, bodies, 'Native builtin disclosed a file outside Raven task tools')
        self.assertNotIn('offline-scripted-fixture', bodies, 'Native builtin disclosed the synthetic provider key')
        self.assertNotIn(base64.b64encode(self.image_bytes).decode(), bodies, 'Native builtin disclosed an image outside task tools')
        for request in self.requests:
            self.assertNotIn('data:image/', json.dumps(request.get('input',[])), 'Native builtin transmitted a re-encoded image')
        for offset in (0, cycle):
            for position in range(self.attack_count - 1):
                # The next inference request must carry the native result for
                # the preceding attempted shell/file, image, or network call.
                assert_denial_receipt(self, self.requests[offset + position + 1],
                                     'attack-' + str(offset + position + 1),
                                     ('exec_command','view_image','exec_command','apply_patch','shell_command',
                                      'request_user_input','create_goal','update_goal')[position])
        for request in self.requests:
            self.assert_native_tool_schema(request['tools'])
        for events in (first, second):
            self.assertEqual([event['call']['name'] for event in events if event['kind'] == 'tool'], ['read_file'])
