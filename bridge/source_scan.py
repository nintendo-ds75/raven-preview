"""Suggested context sources for a repository: the Slack channels, Jira
projects and documentation sites the repository points to, checked
against what Raven's credentials can list, and ranked by fixed rules.

Nothing is connected here. An administrator reviews the suggestions and
connects one with the same workspace-shared attestation as `sources add`;
a dismissed suggestion stays dismissed when the repository is scanned
again. The scan reads the checkout at the revision Raven ingested, and
the commit and pull request titles already in the graph. It never
fetches a URL it finds: a documentation site is probed only when an
administrator connects it. Listing reads metadata only: Slack public
channels (never private, shared or archived ones) and the Jira projects
Raven's account can browse. A suggestion can name a project not every
reader may see, so suggestions are for administrators, never agents.

The same inputs always give the same suggestions: no model is asked,
weights are fixed, and ties break on the target. See
docs/context-sources.md.
"""
import json
import re
import tomllib
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import context_memory as cm
from .sources import SourceError, connections
from .store import Invalid, repo_key

SCHEMA = '''
CREATE TABLE IF NOT EXISTS source_scans (
 repo TEXT PRIMARY KEY, scanned_at TEXT NOT NULL, rev TEXT NOT NULL DEFAULT '',
 input_hash TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}', scanned_by TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS source_dismissals (
 repo TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
 dismissed_by TEXT NOT NULL DEFAULT '', dismissed_at TEXT NOT NULL,
 PRIMARY KEY(repo, kind, target));
'''

# How deliberate a pointer is, by where it was written: a service catalog
# entry is a declaration, a README link is a habit, a docs page a mention.
CATALOG_FILES = ('catalog-info.yaml', 'catalog-info.yml', 'cortex.yaml', 'opslevel.yml', 'opslevel.yaml',
                 '.coderabbit.yaml')
PACKAGE_FILES = ('pyproject.toml', 'package.json', 'cargo.toml', 'setup.cfg')
TEMPLATE_FILES = ('.github/issue_template/config.yml', '.github/issue_template/config.yaml')
MCP_FILES = ('.mcp.json', '.cursor/mcp.json', '.vscode/mcp.json', '.codex/config.toml')
AGENT_FILES = ('agents.md', 'claude.md', 'gemini.md', '.github/copilot-instructions.md')
AGENT_DIRS = ('.claude/rules/', '.cursor/rules/', '.windsurf/rules/')
PROSE_NAMES = ('readme', 'contributing', 'security', 'support', 'maintainers', 'codeowners')
PROSE_FILES = ('.github/pull_request_template.md', '.github/codeowners', 'docs/codeowners',
               '.github/contributing.md', '.github/security.md', '.github/support.md')
MAX_FILES = 80
MAX_DOCS_FILES = 30
MAX_FILE_BYTES = 256 * 1024
MAX_TITLES = 500
LIST_PAGES = 10
MAX_PER_KIND = 10

WEIGHTS = {'evidence': 60, 'activity': 25, 'name': 15}
RECOMMEND_AT = 50
FOCUS_PENALTY = 10

SLACK_URL = re.compile(r'https?://(?:[a-z0-9-]+\.)?slack\.com/(?:archives/|client/T[A-Z0-9]+/|app_redirect\?channel=)'
                       r'(C[A-Z0-9]{6,20})\b')
SLACK_NAME = re.compile(r'(?<![\w/#&(\[.-])#([a-z][a-z0-9_-]{1,79})(?![\w-])')
CATALOG_SLACK = re.compile(r'slack[\w./-]*[ \t]*:[ \t]*["\']?#?([a-z][a-z0-9_-]{1,79})["\']?[ \t]*$', re.M | re.I)
JIRA_URL = re.compile(r'https?://([a-z0-9.-]+)/(?:browse/([A-Z][A-Z0-9_]{1,19})-\d+'
                      r'|(?:jira/software/(?:c/)?)?projects/([A-Z][A-Z0-9_]{1,19})\b)')
JIRA_ANNOTATION = re.compile(r'jira/project-key\s*:\s*["\']?([A-Z][A-Z0-9_]{1,19})\b')
CODERABBIT_KEYS = re.compile(r'project_keys\s*:\s*\[([^\]]*)\]')
JIRA_KEY = re.compile(r'(?<![A-Za-z0-9_-])([A-Z][A-Z0-9]{1,9})-\d{1,6}(?![\w-])')
# Standards and identifiers written the way Jira keys are.
NOT_JIRA = frozenset({'CVE', 'CWE', 'GHSA', 'RFC', 'UTF', 'UTF8', 'SHA', 'ISO', 'PEP', 'GCC', 'HTTP', 'TLS',
                      'SSL', 'MD5', 'X86', 'AES', 'RSA', 'ECMA', 'IEEE', 'ASCII', 'GPL', 'LGPL', 'AGPL', 'BSD',
                      'MIT', 'IPV4', 'IPV6', 'ARM', 'ARM64', 'AMD64', 'WCAG', 'CI', 'PR', 'MR', 'ES', 'GH'})
URL = re.compile(r'https?://[^\s<>()\[\]"\'`|]+')
DOCS_HOST = re.compile(r'^(docs?|developers?|wiki|handbook|help)\.|\.readthedocs\.(io|org)$|\.github\.io$'
                       r'|\.gitbook\.io$|\.mintlify\.app$')
NOT_DOCS = ('github.com', 'gitlab.com', 'bitbucket.org', 'pypi.org', 'npmjs.com', 'crates.io', 'shields.io',
            'badge.fury.io', 'codecov.io', 'slack.com', 'atlassian.net', 'docs.google.com', 'drive.google.com',
            'example.com', 'example.org', 'localhost')
GENERIC_WORDS = frozenset({'the', 'and', 'for', 'eng', 'dev', 'devs', 'team', 'general', 'random', 'help', 'app',
                           'api', 'service', 'svc', 'core', 'project', 'main', 'test', 'tests', 'prod', 'staging',
                           'internal', 'alerts', 'notifications', 'bot', 'ops', 'web', 'data', 'platform',
                           'frontend', 'backend', 'client', 'server', 'lib', 'sdk', 'code', 'repo', 'git'})
_FROM_ENV = object()


def migrate(db):
    db.executescript(SCHEMA)


# ---------------- reading the repository ----------------

def _weight(path):
    """How much a pointer in this file counts, or 0 when the file is not read."""
    low = path.lower()
    if low in CATALOG_FILES:
        return 1.0
    if low in TEMPLATE_FILES or low in MCP_FILES or low in PACKAGE_FILES:
        return 0.9
    if low in AGENT_FILES or low.startswith(AGENT_DIRS):
        return 0.8
    if ('/' not in low and low.split('.')[0] in PROSE_NAMES) or low in PROSE_FILES:
        return 0.7
    if low.startswith('docs/') and low.count('/') == 1 and low.endswith(('.md', '.rst', '.txt', '.mdx')):
        return 0.5
    return 0.0


def read_checkout(path, rev=''):
    """The files worth reading, as {path: text}, at `rev` (default HEAD),
    read from the object store so uncommitted edits never count."""
    from .ingest import _git, _git_read, _resolve_commit
    root = Path(path)
    commit = _resolve_commit(root, rev or 'HEAD')
    names = [n for n in _git(root, 'ls-tree', '-r', '--name-only', '-z', commit).split('\0') if n]
    chosen = sorted(n for n in names if _weight(n) and not n.lower().startswith('docs/'))
    chosen += sorted(n for n in names if n.lower().startswith('docs/') and _weight(n))[:MAX_DOCS_FILES]
    files = {}
    for name in chosen[:MAX_FILES]:
        out = _git_read(root, 'show', f'{commit}:{name}', timeout=60, errors='replace')
        if out.returncode == 0:
            files[name] = out.stdout[:MAX_FILE_BYTES]
    return commit, files


def _line(text, index):
    return text.count('\n', 0, index) + 1


def _url_clean(url):
    return url.rstrip('.,;:!?*_\'"')


def _host(url):
    return (urllib.parse.urlsplit(url).hostname or '').lower()


def _package_urls(name, text):
    """Documentation and homepage URLs declared in package metadata, each
    with its weight: a documentation field is a declaration, a homepage a
    likely site."""
    low = name.lower()
    out = {}
    try:
        if low == 'pyproject.toml':
            data = tomllib.loads(text)
            urls = {re.sub(r'[^a-z]', '', k.lower()): v for k, v in ((data.get('project') or {}).get('urls') or {}).items()}
            poetry = (data.get('tool') or {}).get('poetry') or {}
            for key, weight in (('documentation', 0.9), ('docs', 0.9), ('homepage', 0.7), ('home', 0.7)):
                for value in (urls.get(key), poetry.get(key)):
                    if isinstance(value, str):
                        out.setdefault(value, weight)
        elif low == 'package.json':
            data = json.loads(text)
            if isinstance(data.get('homepage'), str):
                out[data['homepage']] = 0.7
        elif low == 'cargo.toml':
            package = tomllib.loads(text).get('package') or {}
            for key, weight in (('documentation', 0.9), ('homepage', 0.7)):
                if isinstance(package.get(key), str):
                    out.setdefault(package[key], weight)
        elif low == 'setup.cfg':
            for label, value in re.findall(r'^\s*(Documentation|Docs|Homepage)\s*=\s*(\S+)', text, re.M | re.I):
                out.setdefault(value, 0.9 if label.lower().startswith('doc') else 0.7)
    except (ValueError, tomllib.TOMLDecodeError, AttributeError):
        pass
    return out


def _docs_target(url):
    from . import source_docs
    host = _host(url)
    if not host or any(host == bad or host.endswith('.' + bad) for bad in NOT_DOCS):
        return ''
    parts = urllib.parse.urlsplit(url)
    try:
        target, _options = source_docs.normalize(urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, '', '')), {})
    except Invalid:
        return ''
    return target


def pointers(files):
    """Every pointer in the files: (kind, target, weight, where, what,
    site). Slack names are kept as 'name:<channel>' until a listing
    resolves them; site is the Jira host a link names."""
    out = []
    for name, text in sorted(files.items()):
        weight = _weight(name)
        low = name.lower()

        def add(kind, target, why, m, w=weight, site=''):
            out.append((kind, target, w, f'{name}:{_line(text, m.start())}', why, site))
        for m in SLACK_URL.finditer(text):
            add('slack', m.group(1), 'links the channel', m)
        if low.endswith(('.md', '.mdx', '.rst', '.txt', '.mdc')) or '/' not in low and low.split('.')[0] in PROSE_NAMES:
            for m in SLACK_NAME.finditer(text):
                add('slack', 'name:' + m.group(1), f'mentions #{m.group(1)}', m)
        if low in CATALOG_FILES:
            for m in CATALOG_SLACK.finditer(text):
                add('slack', 'name:' + m.group(1).lower(), 'declares the channel', m)
            for m in JIRA_ANNOTATION.finditer(text):
                add('jira', m.group(1), 'declares the project', m)
            for m in CODERABBIT_KEYS.finditer(text):
                for key in re.findall(r'[A-Z][A-Z0-9_]{1,19}', m.group(1)):
                    add('jira', key, 'declares the project', m)
        for m in JIRA_URL.finditer(text):
            host = m.group(1).lower()
            if 'atlassian.net' in host or 'jira' in host:
                add('jira', m.group(2) or m.group(3), f'links {host}', m, site=host)
        declared = _package_urls(name, text) if low in PACKAGE_FILES else {}
        for m in URL.finditer(text):
            url = _url_clean(m.group(0))
            target = _docs_target(url)
            if target and url in declared:
                add('docs', target, 'declares the site', m, w=declared[url])
            elif target and DOCS_HOST.search(_host(url)):
                add('docs', target, 'links the site', m)
    return out


def companions(files):
    """MCP servers the repository declares for its coding agents: name and
    endpoint only. Headers, environment and arguments can carry secrets
    and are never read."""
    out = []
    for name in MCP_FILES:
        text = next((t for n, t in files.items() if n.lower() == name), None)
        if not text:
            continue
        try:
            data = tomllib.loads(text) if name.endswith('.toml') else json.loads(text)
        except (ValueError, tomllib.TOMLDecodeError):
            continue
        servers = data.get('mcp_servers') or data.get('mcpServers') or data.get('servers') or {}
        if not isinstance(servers, dict):
            continue
        for server, entry in sorted(servers.items()):
            if not isinstance(entry, dict) or not isinstance(server, str) or len(server) > 100:
                continue
            out.append({'name': server, 'declared_in': name, **_endpoint(entry)})
    return out


def _endpoint(entry):
    url = entry.get('url') or entry.get('serverUrl') or ''
    if isinstance(url, str) and url.startswith(('https://', 'http://')):
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ''
        port = f':{parts.port}' if parts.port else ''
        return {'transport': 'http', 'endpoint': f'{parts.scheme}://{host}{port}{parts.path}'}
    command = entry.get('command')
    if isinstance(command, str) and command:
        program = command.replace('\\', '/').rsplit('/', 1)[-1]
        args = entry.get('args') if isinstance(entry.get('args'), list) else []
        package = next((a for a in args if isinstance(a, str) and not a.startswith('-')), '')
        if program in ('npx', 'uvx', 'pipx', 'bunx', 'pnpx') and package:
            program += ' ' + package
        return {'transport': 'stdio', 'endpoint': program}
    return {'transport': 'unknown', 'endpoint': ''}


def title_keys(titles):
    """Jira-shaped keys in commit and pull request titles: key -> titles."""
    counts = {}
    for title in titles:
        for key in {m.group(1) for m in JIRA_KEY.finditer(title or '')} - NOT_JIRA:
            counts[key] = counts.get(key, 0) + 1
    return counts


def _titles(store, repo):
    db = store.graph.db
    scopes = {repo, store.graph.resolve_repo(repo)} - {''}
    out = []
    for scope in sorted(scopes):
        out += [r['title'] for r in db.execute('SELECT subject AS title FROM changes WHERE repo=? '
                                               'ORDER BY ts DESC, sha LIMIT ?', (scope, MAX_TITLES)).fetchall()]
        out += [r['title'] for r in db.execute("SELECT title FROM intents WHERE repo=? AND kind='pr' "
                                               'ORDER BY created_at DESC, ref LIMIT ?', (scope, MAX_TITLES)).fetchall()]
    return out


# ---------------- listing what the credentials can see ----------------

def list_slack(client):
    """Open public channels, metadata only."""
    team = client.call('auth.test', {})
    channels, cursor = [], ''
    for _ in range(LIST_PAGES):
        page = client.call('conversations.list', {'types': 'public_channel', 'exclude_archived': 'true',
                                                  'limit': 200, 'cursor': cursor})
        channels += [c for c in page.get('channels') or [] if isinstance(c, dict) and c.get('id')]
        cursor = (page.get('response_metadata') or {}).get('next_cursor') or ''
        if not cursor:
            break
    return {'team': team.get('team_id') or '', 'channels': channels}


def list_jira(client):
    """Projects Raven's Jira account can browse, most recently updated first."""
    projects, start = [], 0
    for _ in range(LIST_PAGES):
        page = client.get('/rest/api/3/project/search', {'startAt': start, 'maxResults': 50,
                                                         'orderBy': '-lastIssueUpdatedTime', 'expand': 'insight'})
        values = [p for p in page.get('values') or [] if isinstance(p, dict) and p.get('key')]
        projects += values
        if page.get('isLast', True) or not values:
            break
        start += len(values)
    return {'host': getattr(client, 'host', ''), 'projects': projects}


# ---------------- ranking ----------------

def _words(text):
    return {w for w in re.split(r'[^a-z0-9]+', (text or '').lower()) if len(w) >= 3 and w not in GENERIC_WORDS}


def _name_match(repo, *names):
    mine = _words(repo.rsplit('/', 1)[-1])
    if not mine:
        return 0.0
    theirs = set().union(*(_words(n) for n in names))
    return round(len(mine & theirs) / len(mine), 3)


def _activity(iso, now):
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(iso.replace('Z', '+00:00'))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    days = (now - when).total_seconds() / 86400
    return 1.0 if days <= 7 else 0.7 if days <= 30 else 0.3 if days <= 90 else 0.0


def _score(evidence, activity, name):
    """0-100 over the components that apply: activity only where the
    provider says when the source last changed, the name only where a
    source has one to compare."""
    parts = {'evidence': evidence}
    for key, value in (('activity', activity), ('name', name)):
        if value is not None:
            parts[key] = value
    total = sum(WEIGHTS[k] for k in parts)
    return round(100 * sum(WEIGHTS[k] * v for k, v in parts.items()) / total)


def _evidence(hits):
    """The strongest pointer, plus a little for each further one."""
    if not hits:
        return 0.0
    return min(1.0, max(h[0] for h in hits) + 0.05 * (len(hits) - 1))


def _reasons(hits, extra=()):
    best = sorted(hits, key=lambda h: (-h[0], h[1], h[2]))
    return [f'{h[1]} {h[2]}'.strip() for h in best[:3]] + list(extra)


def _rank(rows):
    rows.sort(key=lambda r: (-r['score'], r['kind'], r['target']))
    return rows[:MAX_PER_KIND]


def _slack_rows(repo, found, listing):
    from .source_slack import _refusal
    rows, refused = [], []
    if listing is None:
        return rows, refused
    channels = listing['channels']
    by_id = {c['id']: c for c in channels}
    by_name = {c.get('name', ''): c for c in channels}
    biggest = max((c.get('num_members') or 0 for c in channels), default=0)
    evidence = {}
    for target, hits in found.items():
        channel = by_name.get(target[5:]) if target.startswith('name:') else by_id.get(target)
        if channel is None:
            if not target.startswith('name:'):
                refused.append({'kind': 'slack', 'target': target, 'label': target, 'reasons': _reasons(hits),
                                'why': 'not an open public channel in this workspace (private, archived, '
                                       'shared or elsewhere)'})
            continue
        evidence.setdefault(channel['id'], []).extend(hits)
    for channel in channels:
        hits = evidence.get(channel['id'], [])
        name = _name_match(repo, channel.get('name', ''))
        if not hits and not name:
            continue
        label = '#' + (channel.get('name') or channel['id'])
        why = _refusal(channel)
        if why:
            if hits:
                refused.append({'kind': 'slack', 'target': channel['id'], 'label': label, 'reasons': _reasons(hits),
                                'why': why})
            continue
        members = channel.get('num_members') or 0
        extra = [f'name matches {repo}'] if name else []
        score = _score(_evidence(hits), None, name)
        if not hits and biggest >= 20 and members >= biggest / 2:
            score = max(0, score - FOCUS_PENALTY)
            extra.append(f'a workspace-wide channel ({members} members)')
        rows.append({'kind': 'slack', 'target': channel['id'], 'label': label, 'score': score,
                     'reasons': _reasons(hits, extra), 'members': members})
    return _rank(rows), refused


def _jira_rows(repo, found, keys, titles, listing, now):
    rows, refused = [], []
    if listing is None:
        return rows, refused
    by_key = {p['key']: p for p in listing['projects']}
    host = (listing.get('host') or '').lower()
    for key, hits in sorted(found.items()):
        other = sorted({h[3] for h in hits if h[3] and host and h[3] != host})
        if key not in by_key or (other and all(h[3] in other for h in hits)):
            refused.append({'kind': 'jira', 'target': key, 'label': key, 'reasons': _reasons(hits),
                            'why': (f'on another Jira site ({other[0]})' if other else
                                    "not a project Raven's Jira account can browse")})
    for project in listing['projects']:
        key = project['key']
        hits = [h for h in found.get(key, []) if not (h[3] and host and h[3] != host)]
        if keys.get(key):
            share = keys[key] / max(1, len(titles))
            hits.append((min(0.9, 0.3 + 3 * share), '',
                         f'{key}-* in {keys[key]} of {len(titles)} recent commit and pull request titles', ''))
        name = _name_match(repo, project.get('name', ''), key)
        if not hits and not name:
            continue
        insight = project.get('insight') or {}
        activity = _activity(insight.get('lastIssueUpdateTime') or '', now)
        extra = [f'name matches {repo}'] if name else []
        if insight.get('lastIssueUpdateTime'):
            extra.append(f"last issue update {insight['lastIssueUpdateTime'][:10]}")
        rows.append({'kind': 'jira', 'target': key, 'label': f"{key} · {project.get('name') or key}",
                     'score': _score(_evidence(hits), activity, name), 'reasons': _reasons(hits, extra)})
    return _rank(rows), refused


def _docs_rows(found):
    by_origin = {}
    for target, hits in found.items():
        parts = urllib.parse.urlsplit(target)
        by_origin.setdefault(f'{parts.scheme}://{parts.netloc}', []).append((target, hits))
    rows = []
    for origin, entries in by_origin.items():
        target = min((t for t, _h in entries), key=lambda t: (len(t), t))
        hits = [h for _t, hs in entries for h in hs]
        rows.append({'kind': 'docs', 'target': target, 'label': origin, 'score': _score(_evidence(hits), None, None),
                     'reasons': _reasons(hits)})
    return _rank(rows)


# ---------------- the scan ----------------

def _client(kind, given):
    if given is not _FROM_ENV:
        return given
    if kind == 'slack':
        from .source_slack import client_from_env
    else:
        from .source_jira import client_from_env
    return client_from_env()


def _checkout(store, repo, checkout, rev):
    if checkout:
        return str(checkout), rev or ''
    for scope in (repo, store.graph.resolve_repo(repo)):
        path = store.graph.get_source(scope, 'git') if scope else ''
        if path and Path(path).is_dir():
            return path, rev or store.graph.get_source(scope, 'git_rev') or ''
    return '', ''


def scan(store, repo, *, checkout=None, rev=None, slack=_FROM_ENV, jira=_FROM_ENV, now=None, by=''):
    """Read the repository and list what the credentials can see, then
    store the ranked suggestions for an administrator. Every read happens
    before the one write."""
    repo = repo_key(str(repo or ''))
    if not repo or repo == 'local':
        raise Invalid('Name the repository (owner/name) to scan')
    now = now or datetime.now(timezone.utc)
    path, rev = _checkout(store, repo, checkout, rev)
    errors, listings = {}, {}
    commit, files = '', {}
    if path:
        try:
            commit, files = read_checkout(path, rev)
        except (RuntimeError, OSError) as error:  # the listings are still worth reporting
            errors['checkout'] = str(error)[:300]
    titles = _titles(store, repo)
    for kind, lister in (('slack', list_slack), ('jira', list_jira)):
        client = _client(kind, slack if kind == 'slack' else jira)
        if client is None:
            continue
        try:
            listings[kind] = lister(client)
        except (SourceError, OSError, ValueError, KeyError, TypeError) as error:
            errors[kind] = str(error)[:300]

    found = {'slack': {}, 'jira': {}, 'docs': {}}
    for kind, target, weight, at, what, site in pointers(files):
        found[kind].setdefault(target, []).append((weight, at, what, site))
    keys = title_keys(titles)
    slack_rows, slack_refused = _slack_rows(repo, found['slack'], listings.get('slack'))
    jira_rows, jira_refused = _jira_rows(repo, found['jira'], keys, titles, listings.get('jira'), now)
    rows = slack_rows + jira_rows + _docs_rows(found['docs'])
    for row in rows:
        row['recommended'] = row['score'] >= RECOMMEND_AT

    awaiting = []
    from .sources import _module
    for kind in ('slack', 'jira'):
        if kind in listings:
            continue
        named = [t for t in found[kind] if not t.startswith('name:')]
        if kind == 'jira':
            named = sorted(set(named) | {k for k, n in keys.items() if n >= 2})
        if named or kind in errors:
            awaiting.append({'kind': kind, 'pointers': sorted(named)[:MAX_PER_KIND],
                             'do': errors.get(kind) or _module(kind).MISSING_CREDENTIALS})
    inputs = {'files': {n: cm.digest(t) for n, t in files.items()}, 'titles': cm.digest(titles),
              'listings': cm.digest(listings), 'errors': errors}
    result = {'repo': repo, 'rev': commit, 'suggestions': rows, 'not_eligible': slack_refused + jira_refused,
              'awaiting_credentials': awaiting, 'companions': companions(files), 'errors': errors,
              'listed': {k: len(v.get('channels') or v.get('projects') or []) for k, v in listings.items()},
              'read': {'checkout': bool(path), 'files': len(files), 'titles': len(titles)}}
    input_hash = cm.digest(inputs)
    at = cm.stamp()
    with store.graph.transaction():
        store.graph.db.execute('DELETE FROM source_scans WHERE repo=?', (repo,))
        store.graph.db.execute('INSERT INTO source_scans(repo,scanned_at,rev,input_hash,result,scanned_by) '
                               'VALUES(?,?,?,?,?,?)', (repo, at, commit, input_hash, cm.encoded(result), by))
        store.graph.append_event('context_scan_completed', {
            'repo': repo, 'rev': commit, 'input_hash': input_hash, 'by': by,
            'suggested': len(rows), 'recommended': sum(r['recommended'] for r in rows)})
    return suggestions(store, repo)


def suggestions(store, repo=''):
    """The latest scan of one repository, or of every scanned repository,
    with each suggestion's state now: suggested, dismissed or connected."""
    db = store.graph.db
    if repo:
        repo = repo_key(repo)
        rows = db.execute('SELECT * FROM source_scans WHERE repo=?', (repo,)).fetchall()
    else:
        rows = db.execute('SELECT * FROM source_scans ORDER BY repo').fetchall()
    connected = {(c['kind'], c['repo'], c['target']) for c in connections(store, enabled_only=True)}
    dismissed = {(r['kind'], r['repo'], r['target']) for r in db.execute(
        'SELECT kind, repo, target FROM source_dismissals').fetchall()}
    scans = []
    for row in rows:
        result = json.loads(row['result'])
        for item in result['suggestions']:
            key = (item['kind'], row['repo'], item['target'])
            item['state'] = 'connected' if key in connected else 'dismissed' if key in dismissed else 'suggested'
        result.update(scanned_at=row['scanned_at'], input_hash=row['input_hash'], scanned_by=row['scanned_by'])
        scans.append(result)
    return {'scans': scans,
            'note': ('Suggestions only: nothing is read until an administrator connects a source and attests '
                     'that it is shared with every reader of this workspace.')}


def dismiss(store, repo, kind, target, *, by='', undo=False):
    repo = repo_key(str(repo or ''))
    if kind not in ('slack', 'jira', 'docs') or not repo or not isinstance(target, str) or not target:
        raise Invalid('Name the repo, kind and target of the suggestion')
    with store.graph.transaction():
        if undo:
            store.graph.db.execute('DELETE FROM source_dismissals WHERE repo=? AND kind=? AND target=?',
                                   (repo, kind, target))
        else:
            store.graph.db.execute(
                'INSERT INTO source_dismissals(repo,kind,target,dismissed_by,dismissed_at) VALUES(?,?,?,?,?) '
                'ON CONFLICT(repo,kind,target) DO NOTHING', (repo, kind, target, by, cm.stamp()))
        store.graph.append_event('context_suggestion_restored' if undo else 'context_suggestion_dismissed',
                                 {'repo': repo, 'kind': kind, 'target': target, 'by': by})
    return suggestions(store, repo)
