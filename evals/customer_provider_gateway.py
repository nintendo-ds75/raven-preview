"""Ephemeral, loopback-only budget gate for real-provider acceptance tests.

The real key stays in this process. Host/backend processes receive a random
placeholder. Responses are forwarded unchanged; this is not a model simulator.
Only metadata/usage is retained. The default transport pins Anthropic, verifies
TLS, ignores inherited proxies, refuses redirects, and never retries.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, HTTPRedirectHandler, build_opener

import httpx


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _provider_target(target):
    """Accept only canonical origin-form paths, never caller-supplied origins."""
    parsed = urlsplit(target)
    suffix = parsed.path + ('?' + parsed.query if parsed.query else '')
    if (parsed.scheme or parsed.netloc or parsed.fragment or target != suffix
            or parsed.path not in ('/v1/messages', '/v1/messages/count_tokens')
            or parsed.query not in ('', 'beta=true')):
        raise ValueError('Destination is outside the acceptance scope')
    return parsed, 'https://api.anthropic.com' + suffix


class _UsageObserver:
    """Keep bounded parsing state, discarding content immediately after a line."""
    MAX_LINE_BYTES = 64 * 1024
    MAX_JSON_BYTES = 1_000_000

    def __init__(self, content_type, record):
        self.stream = 'text/event-stream' in content_type.lower()
        self.record = record
        self._buffer = bytearray()
        self._discarding = False

    def _unavailable(self):
        self.record['usage_unavailable'] = True

    def _value(self, raw):
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            self._unavailable()
            return
        if not isinstance(value, dict):
            self._unavailable()
            return
        usage = value.get('usage')
        if usage is None and 'message' in value:
            message = value['message']
            if not isinstance(message, dict):
                self._unavailable()
                return
            usage = message.get('usage')
        if usage is None:
            return
        if not isinstance(usage, dict):
            self._unavailable()
            return
        for name in ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'):
            if name in usage:
                if type(usage[name]) is not int or usage[name] < 0:
                    self._unavailable()
                    continue
                self.record['usage'][name] = max(self.record['usage'].get(name, 0), usage[name])

    def _line(self):
        line = bytes(self._buffer).rstrip(b'\r')
        self._buffer.clear()
        if line.startswith(b'data:'):
            self._value(line[5:].lstrip(b' '))

    def feed(self, chunk):
        if not self.stream:
            if self._discarding:
                return
            if len(self._buffer) + len(chunk) > self.MAX_JSON_BYTES:
                self._unavailable()
                self._buffer.clear()
                self._discarding = True
            else:
                self._buffer.extend(chunk)
            return
        offset = 0
        while offset < len(chunk):
            newline = chunk.find(b'\n', offset)
            end = len(chunk) if newline < 0 else newline
            if not self._discarding:
                if len(self._buffer) + end - offset > self.MAX_LINE_BYTES:
                    self._unavailable()
                    self._buffer.clear()
                    self._discarding = True
                else:
                    # Check size before slicing: an oversized transport chunk
                    # must not become an oversized retained line or copy.
                    self._buffer.extend(chunk[offset:end])
            if newline < 0:
                break
            if self._discarding:
                self._discarding = False
            else:
                self._line()
            offset = newline + 1

    def finish(self):
        if not self._discarding:
            if self.stream:
                if self._buffer:
                    self._line()
            else:
                self._value(bytes(self._buffer))
        self._buffer.clear()


class Gateway:
    def __init__(self, api_key, workspace_id='', *, max_calls=24,
                 max_input_bytes=1_500_000, max_output_tokens=24_000,
                 model='claude-sonnet-4-6', transport=None):
        if not api_key or any(not isinstance(n, int) or n <= 0 for n in
                              (max_calls, max_input_bytes, max_output_tokens)):
            raise ValueError('A credential and positive budget limits are required')
        if workspace_id and (len(workspace_id) > 256 or not workspace_id.isascii()
                             or not workspace_id.isprintable()):
            raise ValueError('Invalid workspace identifier')
        self._key, self._workspace = api_key, workspace_id
        self.placeholder = 'raven-eval-' + secrets.token_hex(24)
        self.model = model
        self.limits = {'calls': max_calls, 'input_bytes': max_input_bytes,
                       'reserved_output_tokens': max_output_tokens}
        self._lock = threading.Lock()
        self._records = []
        self._input_bytes = self._reserved = self._active = 0
        self._halted = False
        self._client = httpx.Client(transport=transport, trust_env=False,
                                    follow_redirects=False, timeout=60)
        self._server = self._thread = None
        self._local_opener = build_opener(ProxyHandler({}), _NoRedirect())

    def __enter__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                outer._handle(self)

            def do_GET(self):
                outer._error(self, 405, 'Only provider POST requests are accepted')

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._server.daemon_threads = True
        self.url = 'http://127.0.0.1:' + str(self._server.server_port)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args):
        with self._lock:
            self._halted = True
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=3)
        self._client.close()
        self._key = ''

    @staticmethod
    def _error(handler, status, message):
        raw = json.dumps({'type': 'error', 'error': {
            'type': 'invalid_request_error', 'message': message}}).encode()
        handler.send_response(status)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    def _handle(self, handler):
        try:
            path, upstream_url = _provider_target(handler.path)
        except (ValueError, TypeError):
            return self._error(handler, 403, 'Destination is outside the acceptance scope')
        provided = handler.headers.get('x-api-key', '')
        bearer = handler.headers.get('Authorization', '')
        if provided != self.placeholder and bearer != 'Bearer ' + self.placeholder:
            return self._error(handler, 401, 'Invalid local test credential')
        try:
            length = int(handler.headers.get('Content-Length', '-1'))
            if length < 0 or length > self.limits['input_bytes']:
                raise ValueError()
            body = handler.rfile.read(length)
            payload = json.loads(body)
            if not isinstance(payload, dict) or payload.get('model') != self.model:
                raise ValueError()
            maximum = payload.get('max_tokens', 0) if path.path == '/v1/messages' else 0
            if not isinstance(maximum, int) or maximum < 0:
                raise ValueError()
        except (ValueError, TypeError):
            return self._error(handler, 400, 'Invalid or oversized acceptance request')
        with self._lock:
            if (self._halted or len(self._records) >= self.limits['calls']
                    or self._input_bytes + length > self.limits['input_bytes']
                    or self._reserved + maximum > self.limits['reserved_output_tokens']):
                return self._error(handler, 429, 'Acceptance provider budget exhausted')
            self._input_bytes += length
            self._reserved += maximum
            self._active += 1
            record = {'path': path.path, 'status': None, 'input_bytes': length,
                      'reserved_output_tokens': maximum, 'usage': {}, 'error_class': None}
            self._records.append(record)
        headers = {'x-api-key': self._key, 'anthropic-version': '2023-06-01',
                   'Content-Type': 'application/json'}
        if self._workspace:
            headers['anthropic-workspace-id'] = self._workspace
        beta = handler.headers.get('anthropic-beta')
        if beta:
            headers['anthropic-beta'] = beta
        started = time.monotonic()
        try:
            # Never accept a destination or upstream authentication from caller.
            with self._client.stream('POST', upstream_url,
                                     headers=headers, content=body) as response:
                record['status'] = response.status_code
                if response.status_code != 200:
                    with self._lock:
                        self._halted = True
                    return self._error(handler, response.status_code, 'Provider request failed; test stopped')
                handler.send_response(200)
                content_type = response.headers.get('content-type', 'application/json')
                handler.send_header('Content-Type', content_type)
                handler.send_header('Connection', 'close')
                handler.end_headers()
                observer = _UsageObserver(content_type, record)
                for chunk in response.iter_bytes():
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                    observer.feed(chunk)
                observer.finish()
        except Exception as error:
            # Never retain exception text, headers, prompts, or response bodies.
            record['error_class'] = type(error).__name__
            with self._lock:
                self._halted = True
            if record['status'] is None:
                try:
                    self._error(handler, 502, 'Provider transport failed; test stopped')
                except OSError:
                    pass
        finally:
            record['elapsed_seconds'] = round(time.monotonic() - started, 3)
            with self._lock:
                self._active -= 1

    @staticmethod
    def _usage(raw, content_type, record):
        observer = _UsageObserver(content_type, record)
        observer.feed(raw)
        observer.finish()

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps({'physical_requests': len(self._records),
                'active_requests': self._active, 'input_bytes': self._input_bytes,
                'reserved_output_tokens': self._reserved, 'halted': self._halted,
                'limits': self.limits, 'requests': self._records}))

    def urllib_open(self, request, *args, **kwargs):
        """Patch only Raven's transport during a live test; outputs stay real.

        Existing loopback HTTP fixture traffic passes through. The production
        Anthropic URL is routed through this same physical-request budget gate.
        No alternate public host, proxy, redirect, or copied key is accepted.
        """
        url = request.full_url if isinstance(request, Request) else str(request)
        parsed = urlsplit(url)
        if parsed.scheme == 'https' and parsed.netloc == 'api.anthropic.com':
            if not isinstance(request, Request):
                raise ValueError('Provider request must carry an explicit body')
            if '#' in url:
                raise ValueError('Provider request must not contain a fragment')
            suffix = parsed.path + ('?' + parsed.query if parsed.query else '')
            _provider_target(suffix)
            headers = {'Content-Type': 'application/json', 'x-api-key': self.placeholder}
            beta = request.get_header('Anthropic-beta')
            if beta:
                headers['anthropic-beta'] = beta
            local = Request(self.url + suffix, data=request.data, headers=headers, method=request.get_method())
            return self._local_opener.open(local, *args, **kwargs)
        if parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1'):
            return self._local_opener.open(request, *args, **kwargs)
        raise ValueError('External destination refused by acceptance gateway')
