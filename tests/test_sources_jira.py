"""Jira projects as a native context source, over real HTTP against a
Jira double: credentials, the enhanced search endpoint and its paging,
the updated cursor with overlap, security levels and restricted comments
refused, ADF to text, idempotent re-reads and failures that keep state."""
import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fixtures import OfflineCase
from bridge import source_jira, sources
from bridge.store import Invalid, Store

AUTH = 'Basic ' + base64.b64encode(b'bot@example.invalid:secret-token').decode()


def adf(text):
    return {'type': 'doc', 'version': 1, 'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': text}]}]}


def issue(n, updated, summary='Meter usage monthly', security=None, comments=(), status='In Progress'):
    return {'id': str(10000 + n), 'key': f'BILL-{n}', 'fields': {
        'summary': summary, 'description': adf(f'Description of BILL-{n}.'), 'updated': updated,
        'created': '2026-09-01T10:00:00.000+0000', 'status': {'name': status}, 'resolution': None,
        'issuetype': {'name': 'Story'}, 'reporter': {'displayName': 'Wes Chen'}, 'security': security,
        'comment': {'total': len(comments), 'comments': list(comments)}}}


def comment(text, restricted=False):
    out = {'author': {'displayName': 'Marisol Vega'}, 'created': '2026-09-02T10:00:00.000+0000', 'body': adf(text)}
    if restricted:
        out['visibility'] = {'type': 'role', 'value': 'Administrators'}
    return out


class JiraDouble:
    def __init__(self):
        self.issues, self.searches, self.fail, self.page_size = [], [], None, 50
        jira = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body, headers=None):
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_GET(self):
                if self.headers.get('Authorization') != AUTH:
                    return self.reply(401, {'errorMessages': ['unauthorized']})
                url = urlsplit(self.path)
                if jira.fail:
                    return self.reply(*jira.fail)
                if url.path == '/rest/api/3/project/BILL':
                    return self.reply(200, {'key': 'BILL', 'name': 'Billing'})
                if url.path == '/rest/api/3/search/jql':
                    q = {k: v[0] for k, v in parse_qs(url.query).items()}
                    jira.searches.append(q)
                    start = int(q.get('nextPageToken') or 0)
                    page = jira.issues[start:start + jira.page_size]
                    nxt = start + jira.page_size
                    last = nxt >= len(jira.issues)
                    return self.reply(200, {'issues': page, 'isLast': last, **({} if last else {'nextPageToken': str(nxt)})})
                if url.path == '/rest/api/3/search':
                    return self.reply(410, {'errorMessages': ['removed']})
                return self.reply(404, {'errorMessages': ['not found']})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class JiraSourceTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'jira.db')
        self.addCleanup(self.store.graph.close)
        self.jira = JiraDouble()
        self.addCleanup(self.jira.close)
        env = patch.dict(os.environ, {'JIRA_BASE_URL': self.jira.base, 'JIRA_EMAIL': 'bot@example.invalid',
                                      'JIRA_API_TOKEN': 'secret-token'})
        env.start()
        self.addCleanup(env.stop)

    def add(self):
        return sources.register(self.store, 'jira', 'example/service', 'bill', shared=True)

    def records(self):
        return {r['external_id']: json.loads(r['snapshot']) for r in self.store.graph.db.execute(
            "SELECT s.external_id, v.snapshot FROM source_records s JOIN source_versions v ON v.id=s.head_id "
            "WHERE s.provider='jira'").fetchall()}

    def test_credentials_are_required_and_never_stored(self):
        with patch.dict(os.environ, {'JIRA_API_TOKEN': ''}):
            with self.assertRaisesRegex(Invalid, 'JIRA_API_TOKEN'):
                self.add()
        connection = self.add()
        self.assertEqual((connection['target'], connection['label']), ('BILL', 'BILL · Billing'))
        dump = json.dumps(sources.connections(self.store)) + json.dumps(sources.report(self.store))
        self.assertNotIn('secret-token', dump)

    def test_wrong_credentials_fail_registration(self):
        with patch.dict(os.environ, {'JIRA_API_TOKEN': 'wrong'}):
            with self.assertRaisesRegex(sources.SourceError, 'refused the credentials'):
                self.add()

    def test_issues_and_unrestricted_comments_become_records(self):
        self.jira.issues = [issue(1, '2026-10-01T10:00:00.000+0000',
                                  comments=[comment('Cap overage at 2x.'), comment('Legal hold note', restricted=True)]),
                            issue(2, '2026-10-01T11:00:00.000+0000', security={'name': 'Finance only'})]
        connection = self.add()
        result = sources.sync(self.store, connection['id'])
        self.assertEqual(result['refused'], {'restricted_comment': 1, 'issue_security_level': 1})
        records = self.records()
        self.assertEqual(list(records), ['10001'])
        body = records['10001']['body']
        self.assertIn('Description of BILL-1.', body)
        self.assertIn('Cap overage at 2x.', body)
        self.assertNotIn('Legal hold', body)
        search = self.jira.searches[0]
        self.assertIn('project = "BILL"', search['jql'])
        self.assertIn('ORDER BY updated ASC, key ASC', search['jql'])
        self.assertIn('security', search['fields'])

    def test_the_cursor_reads_back_an_overlap_and_re_reads_are_idempotent(self):
        self.jira.issues = [issue(1, '2026-10-01T10:00:00.000+0000')]
        connection = self.add()
        sources.sync(self.store, connection['id'])
        cursor = int(sources._row(self.store, connection['id'])['cursor'])
        self.assertEqual(cursor, source_jira._ms('2026-10-01T10:00:00.000+0000'))
        versions = self.store.graph.db.execute('SELECT count(*) n FROM source_versions').fetchone()['n']
        again = sources.sync(self.store, connection['id'])
        self.assertEqual((again['records'], again['changed']), (1, 0))
        self.assertIn(f'updated >= {cursor - source_jira.OVERLAP_MS}', self.jira.searches[-1]['jql'])
        self.assertEqual(self.store.graph.db.execute('SELECT count(*) n FROM source_versions').fetchone()['n'], versions)

    def test_an_edited_issue_is_a_new_version(self):
        self.jira.issues = [issue(1, '2026-10-01T10:00:00.000+0000')]
        connection = self.add()
        sources.sync(self.store, connection['id'])
        self.jira.issues = [issue(1, '2026-10-02T10:00:00.000+0000', summary='Meter usage weekly', status='Done')]
        result = sources.sync(self.store, connection['id'])
        self.assertEqual(result['changed'], 1)
        record = self.records()['10001']
        self.assertIn('Meter usage weekly', record['title'])
        self.assertEqual(self.store.graph.db.execute(
            "SELECT count(*) n FROM source_versions v JOIN source_records s ON s.id=v.record_id WHERE s.provider='jira'"
        ).fetchone()['n'], 2)

    def test_pages_are_followed_with_next_page_token(self):
        self.jira.page_size = 2
        self.jira.issues = [issue(n, f'2026-10-01T1{n}:00:00.000+0000') for n in range(1, 6)]
        connection = self.add()
        result = sources.sync(self.store, connection['id'])
        self.assertEqual(result['records'], 5)
        self.assertEqual([s.get('nextPageToken') for s in self.jira.searches], [None, '2', '4'])

    def test_long_comment_threads_keep_the_newest_and_say_what_was_left_out(self):
        many = [comment(f'Comment {n} ' + 'x' * 3000) for n in range(10)]
        self.jira.issues = [issue(1, '2026-10-01T10:00:00.000+0000', comments=many)]
        sources.sync(self.store, self.add()['id'])
        body = self.records()['10001']['body']
        self.assertLessEqual(len(body), source_jira.BODY_LIMIT)
        self.assertIn('Comment 9', body)
        self.assertNotIn('Comment 0 ', body)
        self.assertRegex(body, r'\(\d+ earlier comment\(s\) not included in this record\.\)')

    def test_a_failure_keeps_the_cursor_and_rate_limits_carry_retry_after(self):
        self.jira.issues = [issue(1, '2026-10-01T10:00:00.000+0000')]
        connection = self.add()
        sources.sync(self.store, connection['id'])
        cursor = sources._row(self.store, connection['id'])['cursor']
        self.jira.fail = (429, {'errorMessages': ['slow down']}, {'Retry-After': '30'})
        with self.assertRaises(sources.SourceError) as caught:
            sources.sync(self.store, connection['id'])
        self.assertEqual(caught.exception.retry_after, 30.0)
        row = sources._row(self.store, connection['id'])
        self.assertEqual(row['cursor'], cursor)
        self.assertIn('rate limit', row['last_error'])

    def test_adf_text_keeps_lists_code_and_mentions(self):
        doc = {'type': 'doc', 'content': [
            {'type': 'bulletList', 'content': [{'type': 'listItem', 'content': [
                {'type': 'paragraph', 'content': [{'type': 'text', 'text': 'first'}]}]}]},
            {'type': 'codeBlock', 'content': [{'type': 'text', 'text': 'CAP = 2'}]},
            {'type': 'paragraph', 'content': [{'type': 'mention', 'attrs': {'text': '@Wes Chen'}},
                                              {'type': 'text', 'text': ' approved'}]}]}
        text = source_jira.adf_text(doc)
        self.assertIn('- first', text)
        self.assertIn('```\nCAP = 2\n```', text)
        self.assertIn('@Wes Chen approved', text)

    def test_project_keys_are_validated(self):
        for bad in ('', 'b', 'BILL-1', 'DROP TABLE'):
            with self.assertRaises(Invalid):
                sources.register(self.store, 'jira', 'example/service', bad, shared=True)
