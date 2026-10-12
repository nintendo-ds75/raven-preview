"""Suggested context sources: the scan reads what a repository points to
at the revision Raven ingested, confirms it against what the Slack and
Jira credentials can list, refuses what a connector would refuse, ranks
the rest by fixed rules, never fetches a URL it found, never keeps a
secret from an MCP configuration, and leaves connecting to an
administrator, whose dismissals survive the next scan."""
import contextlib
import io
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase, git_env, run_git
from bridge import source_scan, sources
from bridge.ingest import index_repo
from bridge.sources import SourceError
from bridge.store import Invalid, Store

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
FILES = {
    'README.md': (
        '# Billing service\n\n'
        'Questions go to #billing-eng; incidents in https://acme.slack.com/archives/C0INCIDENT.\n'
        'Roadmap: https://acme.atlassian.net/jira/software/projects/BILL/boards/1\n'
        'User guide: https://docs.billing.example.invalid/guide/start\n'
        'See [the setup](#billing-setup) and use `#include <billing.h>`.\n'
        '![build](https://img.shields.io/badge/build-passing)\n'),
    'CONTRIBUTING.md': ('File bugs as https://acme.atlassian.net/browse/BILL-12 tickets. Platform work lives in '
                        'https://other.atlassian.net/browse/OPS-3.\n'),
    'pyproject.toml': ('[project]\nname = "billing"\n\n[project.urls]\n'
                       'Documentation = "https://billing.readthedocs.io/en/latest/"\n'
                       'Homepage = "https://github.com/acme/billing"\n'),
    '.github/ISSUE_TEMPLATE/config.yml': ('contact_links:\n  - name: Billing team\n'
                                          '    url: https://acme.slack.com/archives/C0PRIVATE1\n'),
    'docs/runbook.md': 'Page the on-call in #billing-oncall.\n',
    'src/billing.py': '# Discussed in #billing-secret and https://acme.slack.com/archives/C0CODEONLY\n',
    '.mcp.json': json.dumps({'mcpServers': {
        'sentry': {'type': 'http', 'url': 'https://mcp.sentry.example.invalid/mcp?token=SECRET-1',
                   'headers': {'Authorization': 'Bearer SECRET-2'}},
        'linear': {'command': 'npx', 'args': ['-y', 'mcp-remote', 'https://mcp.linear.example.invalid/sse'],
                   'env': {'LINEAR_API_KEY': 'SECRET-3'}}}}),
}
SUBJECTS = ['BILL-12: meter usage monthly', 'BILL-14 fix invoice rounding', 'Handle UTF-8 customer names',
            'Fix CVE-2024-1234 in the parser', 'PAY-3 align invoice totals']


def channel(cid, name, members, **flags):
    return {'id': cid, 'name': name, 'is_channel': True, 'is_private': False, 'num_members': members, **flags}


CHANNELS = [
    channel('C0BILLENG1', 'billing-eng', 12),
    channel('C0INCIDENT', 'incidents', 40),
    channel('C0ONCALL01', 'billing-oncall', 9, is_ext_shared=True),
    channel('C0GENERAL1', 'general', 200),
    channel('C0BILLNEWS', 'billing-news', 30),
    channel('C0BILLALL1', 'billing-all', 150),
    channel('C0RANDOM01', 'random', 190),
]


class FakeSlack:
    """conversations.list over two pages, plus what registration probes."""

    def __init__(self, channels=CHANNELS, fail=None):
        self.channels, self.fail, self.calls = list(channels), fail, []

    def call(self, method, params):
        self.calls.append((method, dict(params)))
        if self.fail and method == self.fail:
            raise SourceError(f'Slack {method} failed: missing_scope (add channels:read and channels:history to the app)')
        if method == 'auth.test':
            return {'ok': True, 'team_id': 'T1', 'url': 'https://acme.slack.com/'}
        if method == 'conversations.list':
            first = not params.get('cursor')
            return {'ok': True, 'channels': self.channels[:3] if first else self.channels[3:],
                    'response_metadata': {'next_cursor': 'page-2' if first else ''}}
        if method == 'conversations.info':
            return {'ok': True, 'channel': next(c for c in self.channels if c['id'] == params['channel'])}
        raise AssertionError(method)


class FakeJira:
    host = 'acme.atlassian.net'

    def __init__(self):
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        assert path == '/rest/api/3/project/search'
        recent = (NOW - timedelta(days=2)).strftime('%Y-%m-%dT%H:%M:%S.000+0000')
        old = (NOW - timedelta(days=200)).strftime('%Y-%m-%dT%H:%M:%S.000+0000')
        pages = [[{'key': 'BILL', 'name': 'Billing', 'insight': {'lastIssueUpdateTime': recent}},
                  {'key': 'WEB', 'name': 'Website'}],
                 [{'key': 'BARC', 'name': 'Billing archive', 'insight': {'lastIssueUpdateTime': old}}]]
        page = pages[0] if not params.get('startAt') else pages[1]
        return {'values': page, 'isLast': bool(params.get('startAt'))}


def build_repo(repo):
    repo.mkdir()
    run_git(repo, 'init', '-q', '-b', 'main')
    for rel, text in FILES.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    run_git(repo, 'add', '-A')
    run_git(repo, 'commit', '-q', '-m', 'BILL-10 initial import', env=git_env(('Ana Admin', 'ana@example.com'), 0))
    for day, subject in enumerate(SUBJECTS, start=1):
        (repo / 'CHANGES').write_text(subject + '\n')
        run_git(repo, 'add', '-A')
        run_git(repo, 'commit', '-q', '-m', subject, env=git_env(('Ana Admin', 'ana@example.com'), day))


class SourceScanTests(OfflineCase):
    @classmethod
    def setUpClass(cls):
        # One repository and one ingest for the class; each test gets a copy
        # of the database.
        cls.shared = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.shared.cleanup)
        cls.repo = Path(cls.shared.name) / 'billing'
        build_repo(cls.repo)
        store = Store(Path(cls.shared.name) / 'template.db')
        # Pinned, as a replay or a deliberate ingest is.
        index_repo(store.graph, cls.repo, repo_name='acme/billing', rev='HEAD')
        store.graph.close()

    def setUp(self):
        super().setUp()
        db = Path(self.temp.name) / 'scan.db'
        shutil.copyfile(Path(self.shared.name) / 'template.db', db)
        self.store = Store(db)
        self.addCleanup(self.store.graph.close)

    def scan(self, **kwargs):
        kwargs.setdefault('slack', FakeSlack())
        kwargs.setdefault('jira', FakeJira())
        return source_scan.scan(self.store, 'acme/billing', now=NOW, **kwargs)['scans'][0]

    def rows(self, result, kind):
        return [(r['target'], r['score'], r['recommended']) for r in result['suggestions'] if r['kind'] == kind]

    def test_channels_the_repository_points_to_are_confirmed_and_ranked(self):
        result = self.scan()
        self.assertEqual(self.rows(result, 'slack'), [('C0BILLENG1', 76, True), ('C0INCIDENT', 56, True),
                                                      ('C0BILLNEWS', 20, False), ('C0BILLALL1', 10, False)])
        eng = result['suggestions'][0]
        self.assertEqual(eng['label'], '#billing-eng')
        self.assertEqual(eng['reasons'], ['README.md:3 mentions #billing-eng', 'name matches acme/billing'])
        everywhere = next(r for r in result['suggestions'] if r['target'] == 'C0BILLALL1')
        self.assertIn('a workspace-wide channel (150 members)', everywhere['reasons'])
        self.assertEqual(result['listed']['slack'], len(CHANNELS))

    def test_a_channel_a_connector_would_refuse_is_never_suggested(self):
        refused = {r['target']: r['why'] for r in self.scan()['not_eligible'] if r['kind'] == 'slack'}
        self.assertEqual(refused['C0ONCALL01'], 'it is shared with another organization')
        self.assertIn('not an open public channel', refused['C0PRIVATE1'])
        targets = {r['target'] for r in self.scan()['suggestions']}
        self.assertFalse({'C0ONCALL01', 'C0PRIVATE1', 'C0GENERAL1', 'C0RANDOM01'} & targets)

    def test_jira_projects_are_confirmed_by_the_listing_and_titles(self):
        result = self.scan()
        self.assertEqual(self.rows(result, 'jira'), [('BILL', 100, True), ('BARC', 15, False)])
        bill = next(r for r in result['suggestions'] if r['target'] == 'BILL')
        self.assertIn('BILL-* in 3 of 6 recent commit and pull request titles', bill['reasons'])
        self.assertIn('last issue update 2026-09-29', bill['reasons'])
        refused = {r['target']: r['why'] for r in result['not_eligible'] if r['kind'] == 'jira'}
        self.assertEqual(refused, {'OPS': 'on another Jira site (other.atlassian.net)'})

    def test_documentation_sites_come_from_package_metadata_and_links_and_are_not_fetched(self):
        with patch('bridge.source_docs.HTTPClient.get', side_effect=AssertionError('the scan fetched a URL')):
            result = self.scan()
        self.assertEqual(self.rows(result, 'docs'), [('https://billing.readthedocs.io/en/latest/', 90, True),
                                                     ('https://docs.billing.example.invalid/guide/start', 70, True)])
        self.assertEqual(result['suggestions'][-2]['reasons'], ['pyproject.toml:5 declares the site'])

    def test_mcp_servers_the_repository_declares_are_listed_without_secrets(self):
        result = self.scan()
        self.assertEqual(result['companions'], [
            {'name': 'linear', 'declared_in': '.mcp.json', 'transport': 'stdio', 'endpoint': 'npx mcp-remote'},
            {'name': 'sentry', 'declared_in': '.mcp.json', 'transport': 'http',
             'endpoint': 'https://mcp.sentry.example.invalid/mcp'}])
        stored = self.store.graph.db.execute('SELECT result FROM source_scans').fetchone()['result']
        self.assertNotIn('SECRET', stored + json.dumps(result))

    def test_only_files_people_keep_pointers_in_are_read(self):
        everything = json.dumps(self.scan())
        self.assertNotIn('C0CODEONLY', everything)
        self.assertNotIn('billing-secret', everything)
        self.assertNotIn('billing-setup', everything)

    def test_the_scan_reads_the_ingested_revision_not_later_commits_or_edits(self):
        repo = Path(self.temp.name) / 'billing'
        shutil.copytree(self.repo, repo)
        index_repo(self.store.graph, repo, repo_name='acme/billing', rev='HEAD')
        (repo / 'README.md').write_text(FILES['README.md'] + 'New: https://acme.slack.com/archives/C0LATER01\n')
        run_git(repo, 'commit', '-q', '-am', 'Link the new channel', env=git_env(('Ana Admin', 'ana@example.com'), 9))
        (repo / 'README.md').write_text('Uncommitted: https://acme.slack.com/archives/C0DRAFT01\n')
        result = self.scan(slack=None, jira=None)
        awaiting = {a['kind']: a['pointers'] for a in result['awaiting_credentials']}
        self.assertEqual(awaiting['slack'], ['C0INCIDENT', 'C0PRIVATE1'])
        self.assertEqual(result['rev'], self.store.graph.get_source('acme/billing', 'git_rev'))

    def test_without_credentials_pointers_wait_and_nothing_is_listed(self):
        result = self.scan(slack=None, jira=None)
        awaiting = {a['kind']: a for a in result['awaiting_credentials']}
        self.assertEqual(awaiting['slack']['pointers'], ['C0INCIDENT', 'C0PRIVATE1'])
        self.assertIn('SLACK_BOT_TOKEN', awaiting['slack']['do'])
        self.assertEqual(awaiting['jira']['pointers'], ['BILL', 'OPS'])
        self.assertEqual({r['kind'] for r in result['suggestions']}, {'docs'})
        self.assertEqual(result['listed'], {})

    def test_a_listing_that_fails_is_reported_and_the_rest_of_the_scan_stands(self):
        result = self.scan(slack=FakeSlack(fail='conversations.list'))
        self.assertIn('missing_scope', result['errors']['slack'])
        slack = next(a for a in result['awaiting_credentials'] if a['kind'] == 'slack')
        self.assertIn('missing_scope', slack['do'])
        self.assertEqual(self.rows(result, 'jira')[0][0], 'BILL')

    def test_the_same_inputs_give_the_same_suggestions(self):
        first, second = self.scan(), self.scan()
        for result in (first, second):
            result.pop('scanned_at')
        self.assertEqual(first, second)
        events = self.store.graph.db.execute(
            "SELECT detail FROM events WHERE kind='context_scan_completed'").fetchall()
        self.assertEqual(len(events), 2)
        self.assertEqual(len({json.loads(e['detail'])['input_hash'] for e in events}), 1)

    def test_a_dismissal_survives_a_rescan_and_a_connected_source_shows_as_connected(self):
        self.scan()
        source_scan.dismiss(self.store, 'acme/billing', 'slack', 'C0BILLNEWS', by='ana')
        sources.register(self.store, 'slack', 'acme/billing', 'C0BILLENG1', shared=True, client=FakeSlack())
        states = {r['target']: r['state'] for r in self.scan()['suggestions']}
        self.assertEqual((states['C0BILLNEWS'], states['C0BILLENG1'], states['C0INCIDENT']),
                         ('dismissed', 'connected', 'suggested'))
        source_scan.dismiss(self.store, 'acme/billing', 'slack', 'C0BILLNEWS', by='ana', undo=True)
        restored = source_scan.suggestions(self.store, 'acme/billing')['scans'][0]['suggestions']
        self.assertEqual(next(r['state'] for r in restored if r['target'] == 'C0BILLNEWS'), 'suggested')

    def test_a_repository_is_required(self):
        with self.assertRaises(Invalid):
            source_scan.scan(self.store, '', slack=None, jira=None)
        with self.assertRaises(Invalid):
            source_scan.dismiss(self.store, 'acme/billing', 'teams', 'x')

    def test_the_command_line_scans_lists_and_dismisses(self):
        db = Path(self.temp.name) / 'scan.db'

        def run(*argv):
            out = io.StringIO()
            with patch('sys.argv', ['bridge.sources', *argv, '--db', str(db)]), contextlib.redirect_stdout(out), \
                    patch.dict(os.environ, {'SLACK_BOT_TOKEN': '', 'JIRA_BASE_URL': ''}):
                sources.main()
            return json.loads(out.getvalue())
        scanned = run('scan', '--repo', 'acme/billing', '--checkout', str(self.repo))
        self.assertEqual(scanned['scans'][0]['read']['files'], 6)
        run('dismiss', 'docs', '--repo', 'acme/billing', '--target', 'https://docs.billing.example.invalid/guide/start')
        listed = run('suggestions', '--repo', 'acme/billing')['scans'][0]['suggestions']
        self.assertEqual([r['state'] for r in listed], ['suggested', 'dismissed'])


class PointerTests(OfflineCase):
    def test_title_keys_ignore_standards_that_look_like_keys(self):
        self.assertEqual(source_scan.title_keys(['PAY-12 and PAY-13', 'UTF-8, SHA-256, CVE-2024-1, x86-64',
                                                 'Revert "PAY-9"', 'ISO-8601 dates']), {'PAY': 2})

    def test_slack_names_skip_anchors_entities_and_headings(self):
        found = source_scan.pointers({'README.md': '# Title\n[a](#anchor) &#123; see #team-x, page.html#frag\n'})
        self.assertEqual([(k, t) for k, t, *_ in found], [('slack', 'name:team-x')])
