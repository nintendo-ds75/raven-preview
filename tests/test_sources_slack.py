"""Slack public channels as an opt-in native context source, over real HTTP
against a Slack Web API double: public-only registration, the per-pass
public check that fails closed, history with overlap, thread replies
followed after their parent leaves the window, skipped bot and system
messages, edits as new versions, and rate limits."""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

from fixtures import OfflineCase
from bridge import source_slack, sources
from bridge.store import Invalid, Store

TOKEN = 'xoxb-test-token'


class SlackDouble:
    def __init__(self):
        now = time.time()
        self.channel = {'id': 'C0BILLING1', 'name': 'billing', 'is_channel': True, 'is_private': False}
        self.history = [
            {'type': 'message', 'user': 'U1', 'text': 'We bill overage at 2x.', 'ts': f'{now - 3600:.6f}'},
            {'type': 'message', 'subtype': 'channel_join', 'user': 'U2', 'text': 'joined', 'ts': f'{now - 3500:.6f}'},
            {'type': 'message', 'bot_id': 'B1', 'text': 'Raven asked a question', 'ts': f'{now - 3400:.6f}'},
        ]
        self.replies = {}
        self.calls, self.rate_limit = [], False
        slack = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body, status=200, headers=None):
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_POST(self):
                method = self.path.strip('/')
                raw = self.rfile.read(int(self.headers.get('Content-Length') or 0)).decode()
                params = {k: v[0] for k, v in parse_qs(raw).items()}
                slack.calls.append((method, params, self.headers.get('Content-Type')))
                if self.headers.get('Authorization') != f'Bearer {TOKEN}':
                    return self.reply({'ok': False, 'error': 'invalid_auth'})
                if slack.rate_limit:
                    return self.reply({'ok': False, 'error': 'ratelimited'}, 429, {'Retry-After': '20'})
                if method == 'auth.test':
                    return self.reply({'ok': True, 'team_id': 'T1', 'url': 'https://acme.slack.com/'})
                if method == 'conversations.info':
                    return self.reply({'ok': True, 'channel': slack.channel})
                if method == 'conversations.history':
                    oldest = float(params.get('oldest') or 0)
                    found = [m for m in slack.history if float(m['ts']) >= oldest]
                    return self.reply({'ok': True, 'messages': sorted(found, key=lambda m: -float(m['ts'])),
                                       'has_more': False})
                if method == 'conversations.replies':
                    thread = slack.replies.get(params['ts'], [])
                    oldest = float(params.get('oldest') or 0)
                    return self.reply({'ok': True, 'has_more': False,
                                       'messages': [m for m in thread if float(m['ts']) >= oldest]})
                return self.reply({'ok': False, 'error': 'unknown_method'})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class SlackSourceTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'slack.db')
        self.addCleanup(self.store.graph.close)
        self.slack = SlackDouble()
        self.addCleanup(self.slack.close)
        env = patch.dict(os.environ, {'SLACK_BOT_TOKEN': TOKEN, 'SLACK_API_BASE': self.slack.base})
        env.start()
        self.addCleanup(env.stop)

    def add(self):
        return sources.register(self.store, 'slack', 'example/service', 'C0BILLING1', shared=True)

    def records(self):
        return {r['external_id']: (r['availability'], json.loads(r['snapshot'])) for r in self.store.graph.db.execute(
            "SELECT s.external_id, s.availability, v.snapshot FROM source_records s JOIN source_versions v "
            "ON v.id=s.head_id WHERE s.provider='slack'").fetchall()}

    def test_only_public_channels_can_be_registered(self):
        for flags, why in (({'is_private': True}, 'private'), ({'is_ext_shared': True}, 'another organization'),
                           ({'is_archived': True}, 'archived'), ({'is_im': True}, 'direct message')):
            self.slack.channel = {'id': 'C0BILLING1', 'name': 'billing', **flags}
            with self.assertRaisesRegex(Invalid, why):
                self.add()
        self.assertEqual(sources.connections(self.store), [])
        with self.assertRaisesRegex(Invalid, 'channel ID'):
            sources.register(self.store, 'slack', 'example/service', 'D0DIRECT1', shared=True)

    def test_person_messages_are_imported_and_bots_and_system_messages_skipped(self):
        connection = self.add()
        self.assertEqual(connection['label'], '#billing')
        result = sources.sync(self.store, connection['id'])
        self.assertEqual(result['records'], 1)
        self.assertEqual(result['refused'], {'not_a_person_message': 2})
        [(external_id, (availability, snapshot))] = self.records().items()
        self.assertTrue(external_id.startswith('history:C0BILLING1:'))
        self.assertEqual(availability, 'available')
        self.assertIn('2x', snapshot['body'])
        self.assertTrue(snapshot['url'].startswith('https://acme.slack.com/archives/C0BILLING1/p'))
        self.assertEqual(json.loads(snapshot['access_scope'])['visibility'], 'public_channel')
        history = [p for m, p, _ in self.slack.calls if m == 'conversations.history'][0]
        self.assertEqual(history['inclusive'], 'true')
        self.assertTrue(all(ct == 'application/x-www-form-urlencoded' for _, _, ct in self.slack.calls))

    def test_a_channel_that_turns_private_fails_closed_and_imports_nothing(self):
        connection = self.add()
        sources.sync(self.store, connection['id'])
        self.slack.history.append({'type': 'message', 'user': 'U1', 'text': 'secret plan', 'ts': f'{time.time():.6f}'})
        self.slack.channel = {**self.slack.channel, 'is_private': True}
        with self.assertRaisesRegex(sources.SourceError, 'private'):
            sources.sync(self.store, connection['id'])
        self.assertNotIn('secret plan', json.dumps(self.records()))
        self.assertEqual(sources.report(self.store)['sources'][0]['state'], 'needs_attention')

    def test_the_cursor_reads_back_an_overlap_and_edits_become_new_versions(self):
        connection = self.add()
        sources.sync(self.store, connection['id'])
        cursor = float(sources._row(self.store, connection['id'])['cursor'])
        message = self.slack.history[0]
        message.update(text='We bill overage at 3x.', edited={'ts': f'{time.time():.6f}'})
        result = sources.sync(self.store, connection['id'])
        history = [p for m, p, _ in self.slack.calls if m == 'conversations.history'][-1]
        self.assertAlmostEqual(float(history['oldest']), cursor - source_slack.OVERLAP_SECONDS, places=3)
        self.assertEqual(result['changed'], 1)
        [(_, snapshot)] = self.records().values()
        self.assertIn('3x', snapshot['body'])
        again = sources.sync(self.store, connection['id'])
        self.assertEqual(again['changed'], 0)

    def test_new_replies_to_an_old_thread_are_followed_after_the_parent_leaves_the_window(self):
        parent = self.slack.history[0]
        first = f'{float(parent["ts"]) + 60:.6f}'
        parent.update(reply_count=1, latest_reply=first, thread_ts=parent['ts'])
        self.slack.replies[parent['ts']] = [parent, {'type': 'message', 'user': 'U2', 'text': 'Agreed, 2x.',
                                                     'ts': first, 'thread_ts': parent['ts']}]
        connection = self.add()
        sources.sync(self.store, connection['id'])
        self.assertEqual(len(self.records()), 2)
        # Move the cursor past the parent, then a late reply arrives.
        with self.store.graph.transaction():
            self.store.graph.db.execute('UPDATE source_connections SET cursor=? WHERE id=?',
                                        (f'{time.time() + 1000:.6f}', connection['id']))
        late = f'{time.time():.6f}'
        self.slack.replies[parent['ts']].append({'type': 'message', 'user': 'U3', 'text': 'Late: cap it at 2x.',
                                                 'ts': late, 'thread_ts': parent['ts']})
        result = sources.sync(self.store, connection['id'])
        # The last seen reply is re-read (inclusive) and unchanged; only the late one is new.
        self.assertEqual(result['changed'], 1)
        self.assertEqual(len(self.records()), 3)
        bodies = [s['body'] for _, s in self.records().values()]
        self.assertTrue(any('Late: cap it at 2x.' in b and b.startswith('Reply in thread') for b in bodies))

    def test_rate_limits_keep_the_cursor_and_carry_retry_after(self):
        connection = self.add()
        sources.sync(self.store, connection['id'])
        cursor = sources._row(self.store, connection['id'])['cursor']
        self.slack.rate_limit = True
        with self.assertRaises(sources.SourceError) as caught:
            sources.sync(self.store, connection['id'])
        self.assertEqual(caught.exception.retry_after, 20.0)
        self.assertEqual(sources._row(self.store, connection['id'])['cursor'], cursor)

    def test_a_token_from_another_workspace_is_refused(self):
        connection = self.add()
        with self.store.graph.transaction():
            options = json.dumps({**connection['options'], 'team': 'T-OTHER'})
            self.store.graph.db.execute('UPDATE source_connections SET options=? WHERE id=?', (options, connection['id']))
        with self.assertRaisesRegex(sources.SourceError, 'another workspace'):
            sources.sync(self.store, connection['id'])
        self.assertEqual(self.records(), {})

    def test_history_and_captured_decisions_never_share_an_identity(self):
        connection = self.add()
        sources.sync(self.store, connection['id'])
        message = self.slack.history[0]
        captured = self.store.add_record({'repo': 'example/service', 'kind': 'slack', 'ref': f"C0BILLING1:{message['ts']}",
                                          'provider': 'slack', 'namespace': 'T1',
                                          'external_id': f"C0BILLING1:{message['ts']}", 'title': 'decision',
                                          'body': message['text']})
        self.assertNotEqual(captured['source']['record_id'],
                            next(iter(self.records())))
        self.assertEqual(len(self.records()), 2)
