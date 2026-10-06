"""Synthetic Slack contract test through Raven's HTTP MCP and signed webhook.

No workspace, real user, model provider, or secret is contacted. The Slack
service is a schema-shaped network double; only the model completion boundary
is stubbed. See docs/slack-contract-testing.md for scope and source references.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEAM = 'T012CONTRACT'
OWNER = 'U012OWNER'
REFERRED = 'U012REFER'
REQUESTER = 'U012REQUEST'
BOT = 'U012RAVEN'
SIGNING_SECRET = 'synthetic-signing-secret-not-a-real-credential'
BOT_TOKEN = 'xoxb-contract-fixture-not-a-real-token'
ACTION_TOKEN = 'contract-transient-action-token-canary'
SOURCE_TEXT = 'Contract source canary: internal load tests are excluded from invoices.'
SOURCE_URL = 'https://contract-fixture.slack.com/archives/C012BILLING/p1700000000000001'
ANSWER = 'Exclude internal load tests from invoices; continue billing real customer traffic.'
DIFF = ('diff --git a/billing/usage.py b/billing/usage.py\n'
        '--- a/billing/usage.py\n+++ b/billing/usage.py\n@@ -1 +1 @@\n'
        '-bill_internal = True\n+bill_internal = False\n')


def member(uid, name, email='', **extra):
    """Optional profile fields can be absent, as in Slack's user object."""
    return {'id': uid, 'team_id': TEAM, 'name': name.lower().replace(' ', '.'),
            'real_name': name, 'deleted': False, 'is_bot': False,
            'profile': {'real_name': name, 'display_name': name,
                        **{f'image_{size}': f'https://avatars.contract.invalid/{uid}-{size}.png'
                           for size in (24, 32, 48, 72, 192, 512)},
                        **({'email': email} if email else {})}, **extra}


def http_json(url, payload, headers=None, raw=None, timeout=15):
    body = raw if raw is not None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, method='POST', headers={
        'Content-Type': 'application/json; charset=utf-8', **(headers or {})})
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        data = response.read()
        return response.status, json.loads(data) if data else {}, dict(response.headers)


def confirmation_text(readback):
    """A synthetic person copies the identity from the actually delivered reading."""
    match = re.search(r'`confirm ([0-9a-f]{12})`', readback['text'])
    assert match, readback
    return 'confirm ' + match[1]


def eventually(predicate, description, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(.025)
    raise AssertionError('Timed out: ' + description)


class SlackContractServer:
    """Subset of the official Web API surface Raven currently consumes.

    Requests go through HTTP JSON/form decoding, auth-header checks, cursor
    checks and shape validation. It intentionally is not a full Slack emulator.
    """
    def __init__(self):
        self.lock = threading.RLock()
        self.requests = []
        self.messages = []
        self.failures = {}
        self.violations = []
        self.users = [member(OWNER, 'Wes Contract', 'wes@contract.example'),
                      member(BOT, 'Raven Contract', is_bot=True),
                      member(REFERRED, 'Marisol Contract', 'marisol@contract.example'),
                      member(REQUESTER, 'Contract Requester', 'requester@contract.example'),
                      member('U012NOEMAIL', 'No Email Contact'),
                      member('U012DELETED', 'Deleted Contact', deleted=True),
                      member('U012FOREIGN', 'Foreign Contact', team_id='TFOREIGN')]
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                try:
                    ctype = self.headers.get('Content-Type', '').split(';')[0]
                    if ctype == 'application/json':
                        payload = json.loads(raw)
                    elif ctype == 'application/x-www-form-urlencoded':
                        payload = {k: v[-1] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
                    else:
                        return self.respond(200, {'ok': False, 'error': 'invalid_post_type'})
                    if not isinstance(payload, dict):
                        raise ValueError('JSON body must be an object')
                    method = self.path.rsplit('/', 1)[-1]
                    if method != 'api.test' and self.headers.get('Authorization') != 'Bearer ' + BOT_TOKEN:
                        return self.respond(200, {'ok': False, 'error': 'invalid_auth'})
                    status, result, extra = owner.dispatch(method, payload)
                    self.respond(status, result, extra)
                except (ValueError, KeyError, TypeError) as error:
                    with owner.lock:
                        owner.violations.append(str(error))
                    self.respond(200, {'ok': False, 'error': 'invalid_arguments'})

            def respond(self, status, result, extra=None):
                body = json.dumps(result).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                for name, value in (extra or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}/api'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def fail_once(self, method, *, status=200, error='missing_scope', retry_after=None):
        with self.lock:
            self.failures.setdefault(method, []).append((status, {'ok': False, 'error': error},
                {'Retry-After': str(retry_after)} if retry_after is not None else {}))

    def dispatch(self, method, payload):
        with self.lock:
            # Never retain Authorization or action_token, even the fake ones.
            self.requests.append({'method': method,
                'payload': {k: ('[redacted]' if k in ('token', 'action_token') else v)
                            for k, v in payload.items()}, 'at': time.monotonic()})
            if self.failures.get(method):
                status, response, headers = self.failures[method].pop(0)
                self.requests[-1]['response_status'] = status
                return status, response, headers
            response = self.answer(method, payload)
            self.requests[-1]['response_status'] = 200
            return 200, response, {}

    def answer(self, method, payload):
        if method == 'api.test':
            return {'ok': not bool(payload.get('error')), **({'error': payload['error']} if payload.get('error') else {}),
                    **({'args': payload} if payload else {})}
        if method == 'auth.test':
            return {'ok': True, 'url': 'https://contract-fixture.slack.com/', 'team': 'Contract Fixture',
                    'user': 'raven', 'team_id': TEAM, 'user_id': BOT, 'bot_id': 'B012RAVEN'}
        if method == 'users.list':
            limit = int(payload.get('limit', 100))
            if not 1 <= limit <= 1000:
                raise ValueError('users.list limit out of range')
            cursor = payload.get('cursor', '')
            pages = {'': (self.users[:2], 'dXNlcjpDMg=='),
                     'dXNlcjpDMg==': (self.users[2:4], 'dXNlcjpDNA=='),
                     'dXNlcjpDNA==': (self.users[4:], '')}
            if cursor not in pages:
                return {'ok': False, 'error': 'invalid_cursor'}
            members, next_cursor = pages[cursor]
            # Deliberately fewer than requested; only an empty cursor ends paging.
            return {'ok': True, 'members': members, 'cache_ts': 1700000000,
                    'response_metadata': {'next_cursor': next_cursor}}
        if method in ('users.info', 'users.lookupByEmail'):
            match = next((u for u in self.users if
                          (u['id'] == payload.get('user') if method == 'users.info' else
                           u['profile'].get('email') == payload.get('email'))), None)
            return {'ok': True, 'user': match} if match else {
                'ok': False, 'error': 'user_not_found' if method == 'users.info' else 'users_not_found'}
        if method == 'conversations.open':
            uid = payload.get('users')
            if not isinstance(uid, str) or not any(u['id'] == uid for u in self.users):
                return {'ok': False, 'error': 'user_not_found'}
            return {'ok': True, 'channel': {'id': 'D' + uid[1:]}}
        if method == 'chat.postMessage':
            if not isinstance(payload.get('channel'), str) or not isinstance(payload.get('text'), str):
                raise ValueError('chat.postMessage requires string channel and text')
            if payload.get('thread_ts') and not isinstance(payload['thread_ts'], str):
                raise ValueError('thread_ts must be a string')
            for block in payload.get('blocks', []):
                if block.get('type') == 'section' and len(block.get('text', {}).get('text', '')) > 3000:
                    raise ValueError('Slack section text exceeds 3000 characters')
            ts = f'1700000000.{len(self.messages) + 1:06d}'
            message = {'type': 'message', 'user': BOT, 'bot_id': 'B012RAVEN', 'text': payload['text'],
                       'ts': ts, **({'blocks': payload['blocks']} if payload.get('blocks') else {}),
                       **({'thread_ts': payload['thread_ts']} if payload.get('thread_ts') else {})}
            self.messages.append({**payload, 'ts': ts, 'at': time.monotonic()})
            return {'ok': True, 'channel': payload['channel'], 'ts': ts, 'message': message}
        if method == 'assistant.search.context':
            if not payload.get('query'):
                return {'ok': False, 'error': 'missing_query'}
            if payload.get('action_token') != ACTION_TOKEN:
                return {'ok': False, 'error': 'invalid_action_token'}
            return {'ok': True, 'results': {'messages': [
                {'author_name': 'Marisol Contract', 'author_user_id': REFERRED, 'team_id': TEAM,
                 'channel_id': 'C012BILLING', 'channel_name': 'billing', 'message_ts': '1700000000.000001',
                 'content': SOURCE_TEXT, 'is_author_bot': False, 'permalink': SOURCE_URL,
                 'context_messages': {'before': [], 'after': []}},
                {'author_name': 'Raven', 'author_user_id': BOT, 'content': 'Bot text must be filtered',
                 'is_author_bot': True, 'permalink': SOURCE_URL + '2'}]},
                'response_metadata': {'next_cursor': ''}}
        return {'ok': False, 'error': 'unknown_method'}

    def matching_messages(self, *, channel=None, contains=None, start=0):
        with self.lock:
            return [m for m in self.messages[start:] if (not channel or m['channel'] == channel)
                    and (not contains or contains in m['text'])]


class FixtureProvider:
    """Scripted completion output, never a claim about natural-language quality."""
    def __init__(self):
        self.calls = []

    def complete(self, purpose, system, prompt, max_tokens=2000):
        if purpose != 'slack_conversation':
            raise AssertionError('Unexpected model use: ' + purpose)
        payload = json.loads(prompt)
        self.calls.append({'purpose': purpose, 'message': payload['message'],
                           'source_authors': [s.get('author') for s in payload['sources']]})
        message = payload['message']
        if message == 'What did the billing team say about internal tests?':
            action = {'kind': 'question', 'reply': ('Marisol Contract said internal load tests are excluded.'
                       if payload['sources'] else 'Let me check the available context.')}
        elif message == 'Could you ask Marisol Contract instead?':
            action = {'kind': 'handoff', 'to': 'Marisol Contract'}
        elif message == 'Please exclude internal load tests, but still bill real customer traffic.':
            action = {'kind': 'answer', 'answer': ANSWER, 'rationale': ''}
        else:
            action = {'kind': 'chat', 'reply': 'Please state the decision you want recorded.'}
        return json.dumps(action)


class RavenContractHarness:
    """Isolated instance; task/decision writes enter exclusively through HTTP."""
    def __init__(self, directory=None, *, model_mode='fixture', request_timeout=15, event_timeout=12):
        if model_mode not in ('fixture', 'live'):
            raise ValueError('model_mode must be fixture or live')
        if model_mode == 'live' and not os.environ.get('ANTHROPIC_API_KEY', '').strip():
            raise ValueError('Live mode requires securely configured Anthropic credentials')
        self.request_timeout = request_timeout
        self.event_timeout = event_timeout
        self.stack = ExitStack()
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='raven-slack-contract-'))
        self.root = Path(directory or self.temp)
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'raven.db'
        self.slack = SlackContractServer()
        self.stack.callback(self.slack.close)
        self.provider = FixtureProvider() if model_mode == 'fixture' else None
        self.stack.enter_context(patch.dict(os.environ, {
            'BRIDGE_MODEL_API': 'none' if model_mode == 'fixture' else 'anthropic',
            'BRIDGE_SEMANTIC': '0' if model_mode == 'fixture' else '1',
            'BRIDGE_LIVE': '0', 'BRIDGE_DEEP': '0', 'BRIDGE_PUBLIC_URL': '', 'GITHUB_TOKEN': ''}))
        if model_mode == 'fixture':
            from bridge.config import Config
            class ChatConfig(Config):
                @property
                def semantic_retrieval(self):
                    return True
            self.stack.enter_context(patch('bridge.slack_chat.load', return_value=ChatConfig(model_api='none')))
            self.stack.enter_context(patch('bridge.llm.Client.complete', side_effect=self.provider.complete))
        self.rpc_id = 0
        self.protocol_version = ''
        self.server = self.store = None
        self.transcript = []
        self.agent_token = ''
        try:
            self.start()
        except BaseException:
            self.close()
            raise

    def start(self, *, workers=True):
        from bridge.auth import Auth
        from bridge.delivery import SlackTransport
        from bridge.server import make_server
        from bridge.store import Store
        self.store = Store(self.database)
        self.store.graph.set_setting('workspace_name', 'Synthetic Slack contract workspace')
        self.transport = SlackTransport(BOT_TOKEN, api_base=self.slack.url)
        delivery = self.store.connect_delivery(self.transport)
        # Installation/bootstrap calls only; no decisions are seeded or signed.
        delivery.sync_directory()
        self.auth = Auth(self.store, enabled=True, public_url='')
        if not self.agent_token:
            requester = self.store.graph.find_person(REQUESTER)
            self.agent_token = self.auth.create_token(requester['id'], label='Contract MCP client')['token']
            self._seed_repository()
        self.server = make_server(self.store, port=0, auth=self.auth, slack_signing_secret=SIGNING_SECRET,
                                  wait_cap=1, github_api=None)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        if workers:
            delivery.start(interval=.05)

    def _seed_repository(self):
        from bridge.ingest import index_repo
        repo = self.root / 'checkout'
        (repo / 'billing').mkdir(parents=True, exist_ok=True)
        (repo / 'billing/usage.py').write_text('bill_internal = True\n')
        (repo / 'CODEOWNERS').write_text('/billing/ wes@contract.example\n')
        env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null',
               'GIT_AUTHOR_NAME': 'Wes Contract', 'GIT_AUTHOR_EMAIL': 'wes@contract.example',
               'GIT_COMMITTER_NAME': 'Wes Contract', 'GIT_COMMITTER_EMAIL': 'wes@contract.example'}
        for args in [('init', '-q'), ('add', '.'), ('commit', '-qm', 'feat: seed contract fixture')]:
            subprocess.run(['git', '-C', str(repo), *args], env=env, check=True, capture_output=True)
        index_repo(self.store.graph, str(repo), repo_name='contract/billing')

    def stop(self):
        if self.server:
            self.server.stop_waits()
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=3)
            self.server = None
        if self.store:
            self.store.graph.close()
            self.store = None

    def restart(self, *, workers=True):
        self.stop()
        self.start(workers=workers)

    def close(self):
        self.stop()
        self.stack.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def rpc(self, method, params=None):
        self.rpc_id += 1
        status, result, _ = http_json(self.url + '/mcp', {'jsonrpc': '2.0', 'id': self.rpc_id,
            'method': method, 'params': params or {}}, self.mcp_headers(), timeout=self.request_timeout)
        if status != 200 or 'error' in result:
            raise AssertionError(f'MCP HTTP/JSON-RPC failure: {status} {result}')
        self.transcript.append({'method': method, 'params': params or {}, 'result': result['result']})
        return result['result']

    def mcp_headers(self):
        return {'Authorization': 'Bearer ' + self.agent_token,
                'Accept': 'application/json, text/event-stream',
                **({'MCP-Protocol-Version': self.protocol_version} if self.protocol_version else {})}

    def notify_initialized(self):
        status, result, _ = http_json(self.url + '/mcp',
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, self.mcp_headers())
        assert status == 202, (status, result)
        self.transcript.append({'method': 'notifications/initialized', 'status': status})

    def call(self, name, **arguments):
        response = self.rpc('tools/call', {'name': name, 'arguments': arguments})
        if response.get('isError'):
            raise AssertionError(response['content'][0]['text'])
        return json.loads(response['content'][0]['text'])

    def event(self, message, text, *, user=OWNER, event_id=None, team=TEAM, action_token='', **extra):
        ts = f'{int(time.time())}.{len(self.transcript) + 1:06d}'
        return {'type': 'event_callback', 'team_id': team, 'api_app_id': 'A012CONTRACT',
                'event_id': event_id or 'Ev' + os.urandom(8).hex(), 'event_time': int(time.time()),
                'authorizations': [{'enterprise_id': None, 'team_id': TEAM, 'user_id': BOT,
                                    'is_bot': True, 'is_enterprise_install': False}],
                'event': {'type': 'message', 'channel_type': 'im', 'channel': message['channel'],
                          'thread_ts': message.get('thread_ts') or message['ts'], 'user': user,
                          'text': text, 'ts': ts, 'event_ts': ts,
                          **({'action_token': action_token} if action_token else {}), **extra}}

    def callback(self, event, *, timestamp=None, bad_signature=False, tamper=False, retry=False):
        raw = json.dumps(event, separators=(',', ':'), ensure_ascii=False).encode()
        stamp = str(int(time.time()) if timestamp is None else timestamp)
        signature = 'v0=' + hmac.new(SIGNING_SECRET.encode(), b'v0:' + stamp.encode() + b':' + raw,
                                   hashlib.sha256).hexdigest()
        status, result, _ = http_json(self.url + '/webhooks/slack', None,
            {'X-Slack-Request-Timestamp': stamp, 'X-Slack-Signature': 'v0=bad' if bad_signature else signature,
             **({'X-Slack-Retry-Num': '1', 'X-Slack-Retry-Reason': 'http_timeout'} if retry else {})},
            raw=raw + (b' ' if tamper else b''))
        self.transcript.append({'callback': event['event_id'] if 'event_id' in event else event['type'],
                                'status': status, 'result': result})
        return status, result

    def say(self, message, text, *, expected, user=OWNER, **kwargs):
        start = len(self.slack.messages)
        event = self.event(message, text, user=user, **kwargs)
        status, result = self.callback(event)
        assert status == 200, (status, result)
        reply = eventually(lambda: self.slack.matching_messages(channel=message['channel'], contains=expected,
                          start=start), 'Slack reply containing ' + expected, timeout=self.event_timeout)[-1]
        assert reply.get('thread_ts') == (message.get('thread_ts') or message['ts']), reply
        return event, reply

    def begin(self):
        initialized = self.rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                              'clientInfo': {'name': 'slack-contract-harness', 'version': '1'}})
        assert initialized['serverInfo']['name']
        self.protocol_version = initialized['protocolVersion']
        self.notify_initialized()
        tools = self.rpc('tools/list')['tools']
        assert {'bridge_start_task', 'bridge_add_node', 'bridge_wait', 'bridge_get_tree',
                'bridge_finish_task', 'bridge_export_proof'} <= {t['name'] for t in tools}
        kickoff = self.call('bridge_start_task', title='Decide billing behavior for internal load tests',
            goal='Change the invoice meter after the appropriate person decides whether internal load tests count.',
            repo='contract/billing', paths='billing/usage.py', client_key='slack-contract-kickoff')
        task = kickoff['task_id']
        node = self.call('bridge_add_node', task_id=task, question='Should internal load tests count on customer invoices?',
                        context='Internal synthetic load test traffic is distinct from real customer usage.',
                        paths='billing/usage.py', category='policy', client_ref='load-test-policy')
        message = eventually(lambda: self.slack.matching_messages(channel='D' + OWNER[1:]), 'owner DM')[0]
        return task, node['node_id'], message


def run_contract():
    """Return a public-safe reproducible result; raise on any failed invariant."""
    checks = []
    with RavenContractHarness() as harness:
        status, body, _ = http_json(harness.slack.url + '/api.test', {'foo': 'bar'})
        assert status == 200 and body == {'ok': True, 'args': {'foo': 'bar'}}
        task, node_id, owner_message = harness.begin()
        owners = harness.call('bridge_list_owners', repo='contract/billing')
        names = {p['name'] for p in owners['people']}
        assert {'Wes Contract', 'Marisol Contract', 'No Email Contact'} <= names, names
        assert not {'Raven Contract', 'Deleted Contact', 'Foreign Contact'} & names
        pages = [r for r in harness.slack.requests if r['method'] == 'users.list']
        assert {'dXNlcjpDMg==', 'dXNlcjpDNA=='} <= {r['payload'].get('cursor') for r in pages}
        checks.append('api.test/auth.test and three-page directory; filtered bots/deleted/foreign members; optional email')
        waiting = harness.call('bridge_wait', task_id=task, timeout='0')
        assert waiting['timed_out'] and any(n['node_id'] == node_id for n in waiting['waiting_on'])
        assert not harness.call('bridge_get_decision', decision_id=node_id)['authorized']
        blocked = harness.rpc('tools/call', {'name': 'bridge_finish_task', 'arguments': {'task_id': task}})
        assert blocked['isError'], blocked
        checks.append('HTTP MCP initialize/list/kickoff/add-node/wait; finish refused before authorization')
        for kwargs in ({'bad_signature': True}, {'tamper': True}, {'timestamp': int(time.time()) - 301}):
            assert harness.callback(harness.event(owner_message, 'yes'), **kwargs)[0] == 401
        foreign = harness.event(owner_message, 'yes', team='TFOREIGN', event_id='EvForeign')
        assert harness.callback(foreign)[1].get('ignored') == 'workspace_mismatch'
        checks.append('invalid signature, tampered raw body and stale timestamp rejected; foreign workspace ignored')
        harness.say(owner_message, 'What did the billing team say about internal tests?',
                    expected='Slack source', action_token=ACTION_TOKEN)
        search_reply = harness.slack.messages[-1]
        assert SOURCE_URL in search_reply['text'] and 'Marisol Contract' in search_reply['text']
        assert any(call['source_authors'] == ['Marisol Contract'] for call in harness.provider.calls)
        for table in ('slack_ingress', 'slack_replies', 'slack_conversation', 'webhook_receipts'):
            persisted = json.dumps([dict(r) for r in harness.store.graph.db.execute('SELECT * FROM ' + table)])
            assert all(secret not in persisted for secret in
                       (ACTION_TOKEN, SOURCE_TEXT, SOURCE_URL, search_reply['text'])), table
        checks.append('assistant.search.context via HTTP; named source and exact citation; transient source/token not retained')
        _, referral = harness.say(owner_message, 'Could you ask Marisol Contract instead?', expected='Pass this question to Marisol Contract')
        harness.say(owner_message, confirmation_text(referral), expected='Marisol Contract')
        referred_message = eventually(lambda: harness.slack.matching_messages(channel='D' + REFERRED[1:]), 'referral DM')[0]
        assert not harness.call('bridge_get_decision', decision_id=node_id)['authorized']
        checks.append('natural referral parsed at mocked provider boundary; confirmed handoff sends a distinct DM')
        _, readback = harness.say(referred_message,
            'Please exclude internal load tests, but still bill real customer traffic.',
            expected='Record your decision as:', user=REFERRED)
        assert ANSWER in readback['text']
        assert not harness.call('bridge_get_decision', decision_id=node_id)['authorized']
        harness.restart(workers=False)
        # Stop only scheduling to reproduce an acknowledged queued callback at shutdown.
        # Receipt insertion/signature/HTTP handling still execute normally.
        confirm = harness.event(referred_message, confirmation_text(readback), user=REFERRED, event_id='EvDurableConfirm')
        with patch('bridge.slack_events.Inbox.start'):
            assert harness.callback(confirm)[0] == 200
        assert harness.store.graph.db.execute('SELECT state FROM slack_ingress WHERE id=?',
                                              ('EvDurableConfirm',)).fetchone()['state'] == 'queued'
        harness.restart()
        decision = eventually(lambda: (d if (d := harness.call('bridge_get_decision', decision_id=node_id))['authorized'] else None),
                              'replayed owner authorization')
        assert decision['signed_by'] == 'Marisol Contract' and decision['answer'] == ANSWER, decision
        eventually(lambda: harness.slack.matching_messages(channel=referred_message['channel'], contains='Recorded'), 'confirmation acknowledgement')
        before = len(harness.slack.messages)
        assert harness.callback(confirm, retry=True)[0] == 200
        time.sleep(.25)
        assert len(harness.slack.messages) == before
        assert harness.call('bridge_get_decision', decision_id=node_id)['signed_revision'] == decision['signed_revision']
        checks.append('readback and queued callback survive server/store restart; retry event applies and replies once')
        harness.call('bridge_get_tree', task_id=task)
        finished = harness.call('bridge_finish_task', task_id=task, diff=DIFF,
                                checks='Synthetic Slack contract harness: HTTP callback and MCP assertions passed.')
        assert finished['status'] == 'completed', finished
        exported = harness.call('bridge_export_proof', task_id=task)
        assert exported['integrity']['valid'] and not exported['integrity']['authenticity_verified']
        assert exported['bundle']['payload']['change']['diff'] == DIFF
        assert exported['bundle']['payload']['decisions'][0]['signed_by'] == 'Marisol Contract'
        harness.restart()
        again = harness.call('bridge_export_proof', task_id=task)
        assert again['bundle']['id'] == exported['bundle']['id'] and again['integrity']['valid']
        checks.append('HTTP MCP finish/proof retains exact diff and attributed signed decision across restart')
        assert not harness.slack.violations, harness.slack.violations
        actions = [block for m in harness.slack.messages for block in m.get('blocks', []) if block.get('type') == 'actions']
        assert not actions, 'New shipped actions need an interactive callback contract test'
        return {'status': 'passed', 'checks': checks,
                'mcp_calls': sum(t.get('method') == 'tools/call' for t in harness.transcript),
                'mcp_tools': sorted({t['params']['name'] for t in harness.transcript
                                     if t.get('method') == 'tools/call'}),
                'slack_http_requests': len(harness.slack.requests),
                'model_completions': len(harness.provider.calls),
                'buttons': 'not shipped: section blocks and textual confirmation only',
                'simulation_boundaries': ['Synthetic Slack HTTP responses and signed callbacks; no live Slack workspace.',
                    'Scripted model completion output; parsing, readback, identity and application code are real.',
                    'Synthetic git repository and installation setup; task lifecycle uses authenticated HTTP MCP.',
                    'Server/store lifecycle restart in one process, not a container/process crash.',
                    'Diff and checks are harness claims; proof integrity is not independent execution verification.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Write the public-safe JSON result here')
    args = parser.parse_args()
    result = run_contract()
    rendered = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end='')


if __name__ == '__main__':
    main()
