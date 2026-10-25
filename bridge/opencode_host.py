"""Pinned OpenCode native session driver with a private parent-mediated MCP bridge.

No model loop, credentials discovery, public listener or external action tools.
A permission-denied native tool cannot turn into an implicit approval here.
"""
import base64
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote
from urllib.request import Request, build_opener, ProxyHandler

VERSION = '1.19.4'
ROOT = Path(__file__).resolve().parents[1] / 'hosts' / 'opencode'
MAX_BODY = 1024 * 1024


def binary():
    configured = os.environ.get('RAVEN_OPENCODE_BIN', '')
    if configured:
        return configured
    platform = {'linux': 'linux', 'darwin': 'darwin'}.get(sys.platform)
    import platform as machine
    arch = {'x86_64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(machine.machine())
    return str(ROOT / 'node_modules' / f'opencode-{platform}-{arch}' / 'bin' / 'opencode')


def installation():
    try:
        package = json.loads((ROOT / 'node_modules' / 'opencode-ai' / 'package.json').read_text())
        return package.get('version', '') if Path(binary()).is_file() else ''
    except (OSError, ValueError):
        return ''


def configuration(config, url, token):
    provider = config['model_provider']
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,80}', provider):
        raise ValueError('invalid_provider')
    permissions = {'*': 'deny', 'raven_*': 'allow'}
    return {'autoupdate': False, 'share': 'disabled', 'snapshot': False,
            'enabled_providers': [provider], 'model': provider + '/' + config['model'],
            'small_model': provider + '/' + config['model'],
            'provider': {provider: {'options': {'apiKey': '{env:RAVEN_OPENCODE_API_KEY}'}}},
            'plugin': [str(ROOT / 'guard.mjs')], 'instructions': [], 'lsp': False, 'formatter': False,
            'permission': permissions, 'tools': {'*': False, 'raven_*': True},
            'compaction': {'auto': False, 'prune': False},
            'agent': {'raven': {'mode': 'primary', 'prompt': config['system'], 'permission': permissions,
                                'steps': config['max_turns']}}, 'default_agent': 'raven',
            'mcp': {'raven': {'type': 'remote', 'url': url, 'headers': {'Authorization': 'Bearer ' + token},
                              'oauth': False, 'enabled': True}}}


def validate_protocol(health, schema):
    if health != {'healthy': True, 'version': VERSION}:
        raise ValueError('unsupported_opencode_version')
    paths = schema.get('paths', {})
    for path, method, operation in [('/session', 'post', 'session.create'),
            ('/session/{sessionID}/message', 'post', 'session.prompt'),
            ('/session/{sessionID}/abort', 'post', 'session.abort'),
            ('/config', 'get', 'config.get')]:
        if paths.get(path, {}).get(method, {}).get('operationId') != operation:
            raise ValueError('unsupported_opencode_protocol')
    prompt = paths['/session/{sessionID}/message']['post']
    response = prompt.get('responses', {}).get('200', {}).get('content', {}).get('application/json', {}).get('schema', {})
    if not {'info', 'parts'} <= set(response.get('required', [])):
        raise ValueError('unsupported_opencode_prompt_receipt')


class Bridge:
    def __init__(self, specs, emit):
        self.specs = {spec['name']: spec for spec in specs}
        self.emit, self.session_id = emit, ''
        self.token = secrets.token_urlsafe(32)
        self.pending = {}
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, code, body=None):
                raw = json.dumps(body).encode() if body is not None else b''
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self.reply(405)

            def do_POST(self):
                if self.path != '/mcp' or not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + bridge.token):
                    return self.reply(403)
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= MAX_BODY:
                        return self.reply(413)
                    request = json.loads(self.rfile.read(size))
                    if not isinstance(request, dict):
                        raise ValueError('invalid_request')
                    method, params = request.get('method'), request.get('params') or {}
                    if method == 'notifications/initialized':
                        return self.reply(202)
                    if method == 'initialize':
                        result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                                  'serverInfo': {'name': 'raven-task', 'version': '1'}}
                    elif method == 'tools/list':
                        result = {'tools': [{'name': s['name'], 'description': s['description'], 'inputSchema': s['input_schema']}
                                            for s in bridge.specs.values()]}
                    elif method == 'tools/call':
                        result = bridge.call(params)
                    elif method == 'ping':
                        result = {}
                    else:
                        raise ValueError('unknown_method')
                    return self.reply(200, {'jsonrpc': '2.0', 'id': request.get('id'), 'result': result})
                except (ValueError, TypeError, KeyError):
                    return self.reply(400, {'error': 'invalid_or_out_of_scope_request'})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}/mcp'

    def call(self, params):
        name, args = params['name'], dict(params.get('arguments') or {})
        context = args.pop('_raven_context', {})
        ident = context.get('call_id')
        if (name not in self.specs or not self.session_id or context.get('session_id') != self.session_id
                or not isinstance(ident, str) or not ident or len(ident) > 200 or self.stopped.is_set()):
            raise ValueError('out_of_scope_tool')
        with self.lock:
            if ident in self.pending:
                raise ValueError('duplicate_pending_tool')
            # Completed retries retain the native ID and go back through the
            # parent durable ledger. It validates the argument fingerprint and
            # replays the committed receipt without repeating the effect.
            inbox = self.pending[ident] = queue.Queue()
        try:
            self.emit({'kind': 'tool', 'call': {'id': ident, 'name': name, 'input': args}})
            while not self.stopped.is_set():
                try:
                    result = inbox.get(timeout=.2)
                    return {'content': [{'type': 'text', 'text': json.dumps(result)}], 'isError': 'error' in result}
                except queue.Empty:
                    continue
            return {'content': [{'type': 'text', 'text': 'Task interrupted'}], 'isError': True}
        finally:
            with self.lock:
                self.pending.pop(ident, None)

    def deliver(self, value):
        with self.lock:
            inbox = self.pending.get(value.get('id'))
            if inbox:
                inbox.put(value['result'])

    def close(self):
        self.stopped.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class Server:
    def __init__(self, config, bridge):
        self.config = config
        home = Path(os.environ['HOME']) / 'opencode'
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.home = home
        # OpenCode bootstraps its plugin package on first config read. Reuse
        # the explicitly installed, lockfile-pinned dependency graph instead
        # of letting a task trigger an implicit network install.
        plugin_dir = home / 'config' / 'opencode'
        plugin_dir.mkdir(parents=True, exist_ok=True)
        for name in ('package.json', 'package-lock.json'):
            shutil.copyfile(ROOT / name, plugin_dir / name)
        modules = plugin_dir / 'node_modules'
        if not modules.exists():
            modules.symlink_to(ROOT / 'node_modules', target_is_directory=True)
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'RAVEN_OPENCODE_API_KEY') if key in os.environ}
        env['HOME'] = str(home)
        for key, name in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_CACHE_HOME', 'cache'), ('XDG_STATE_HOME', 'state')]:
            env[key] = str(home / name)
        for flag in ('AUTOUPDATE', 'PROJECT_CONFIG', 'MODELS_FETCH', 'DEFAULT_PLUGINS', 'EXTERNAL_SKILLS', 'CLAUDE_CODE', 'LSP_DOWNLOAD'):
            env['OPENCODE_DISABLE_' + flag] = '1'
        self.password = secrets.token_urlsafe(32)
        env['OPENCODE_SERVER_PASSWORD'] = self.password
        env['OPENCODE_CONFIG_CONTENT'] = json.dumps(configuration(config, bridge.url, bridge.token))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.base = f'http://127.0.0.1:{port}'
        self.opener = build_opener(ProxyHandler({}))
        self.process = subprocess.Popen([binary(), 'serve', '--hostname', '127.0.0.1', '--port', str(port)],
                                        cwd=config['cwd'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def request(self, path, data=None, timeout=30):
        auth = base64.b64encode(('opencode:' + self.password).encode()).decode()
        request = Request(self.base + path, data=json.dumps(data).encode() if data is not None else None,
                          headers={'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=timeout) as response:
            return json.load(response)

    def ready(self):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError('opencode_start_failed')
            try:
                health = self.request('/global/health', timeout=1)
                break
            except OSError:
                time.sleep(.1)
        else:
            raise RuntimeError('opencode_start_timeout')
        validate_protocol(health, self.request('/doc'))
        active = self.request('/config')
        if (active.get('permission') != {'*': 'deny', 'raven_*': 'allow'}
                or active.get('plugin') != [str(ROOT / 'guard.mjs')]
                or set(active.get('mcp', {})) != {'raven'} or active.get('instructions')):
            raise RuntimeError('opencode_ambient_configuration_refused')

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def run(config, emit, commands):
    """commands registers a callback and returns an unsubscribe callback."""
    bridge = Bridge(config['tools'], emit)
    server = None
    interrupted = threading.Event()
    release = lambda: None
    try:
        server = Server(config, bridge)
        def command(value):
            if value.get('kind') == 'tool_result':
                bridge.deliver(value)
            elif value.get('kind') == 'interrupt':
                interrupted.set()
                bridge.stopped.set()
                if bridge.session_id:
                    try:
                        server.request('/session/' + quote(bridge.session_id, safe='') + '/abort', {})
                    except OSError:
                        pass
        release = commands(command)
        server.ready()
        session = config.get('session_id')
        if session:
            if not re.fullmatch(r'ses[a-zA-Z0-9_-]+', session):
                raise ValueError('invalid_session')
            selected = server.request('/session/' + session)
            if selected.get('id') != session or Path(selected.get('directory', '')).resolve() != Path(config['cwd']).resolve():
                raise ValueError('session_scope')
        else:
            selected = server.request('/session', {'title': 'Raven task'})
            session = selected['id']
        binding = server.home / 'raven-session.json'
        identity = {'session_id': session, 'model': config['model'], 'provider': config['model_provider']}
        if config.get('session_id'):
            if not binding.is_file() or json.loads(binding.read_text()) != identity:
                raise ValueError('session_model_binding')
        else:
            binding.write_text(json.dumps(identity))
        bridge.session_id = session
        emit({'kind': 'session', 'id': session})
        if interrupted.is_set():
            emit({'kind': 'result', 'status': 'interrupted', 'is_error': True, 'cost_usd': 0})
            return
        history = server.request('/session/' + quote(session, safe='') + '/message')
        previous = {message.get('info', {}).get('id') for message in history}
        result = server.request('/session/' + quote(session, safe='') + '/message', {
            'agent': 'raven', 'model': {'providerID': config['model_provider'], 'modelID': config['model']},
            'system': config['system'], 'parts': [{'type': 'text', 'text': config['prompt']}]}, timeout=900)
        info = result.get('info', {})
        if (info.get('sessionID') != session or info.get('role') != 'assistant'
                or info.get('providerID') != config['model_provider'] or info.get('modelID') != config['model']):
            raise ValueError('invalid_final_receipt')
        for part in result.get('parts', []):
            if part.get('type') == 'text' and part.get('sessionID') == session:
                emit({'kind': 'text', 'text': part.get('text', '')})
        success = (not interrupted.is_set() and not info.get('error') and bool(info.get('time', {}).get('completed'))
                   and info.get('finish') in ('stop', 'end-turn'))
        cost = 0.0
        delivery = False
        for message in server.request('/session/' + quote(session, safe='') + '/message'):
            receipt = message.get('info', {})
            if receipt.get('role') == 'user' and receipt.get('id') not in previous and receipt.get('sessionID') == session:
                delivery = True
            if receipt.get('role') == 'assistant' and receipt.get('id') not in previous:
                value = receipt.get('cost', 0)
                if receipt.get('sessionID') != session or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError('invalid_usage_receipt')
                cost += value
        if delivery:
            emit({'kind': 'delivery', 'accepted': True})
        emit({'kind': 'result', 'status': 'completed' if success else 'interrupted' if interrupted.is_set() else 'incomplete',
              'is_error': not success, 'cost_usd': cost})
    finally:
        release()
        bridge.close()
        if server:
            server.close()


def main():
    def terminate(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    config = json.loads(sys.stdin.readline())
    output_lock = threading.Lock()
    def emit(value):
        with output_lock:
            print(json.dumps(value), flush=True)
    def commands(callback):
        def read():
            for line in sys.stdin:
                try:
                    callback(json.loads(line))
                except (ValueError, KeyError):
                    callback({'kind': 'interrupt'})
                    return
            callback({'kind': 'interrupt'})
        threading.Thread(target=read, daemon=True).start()
        return lambda: None
    try:
        run(config, emit, commands)
    except Exception:
        emit({'kind': 'error', 'code': 'opencode_host_failed'})


if __name__ == '__main__':
    main()
