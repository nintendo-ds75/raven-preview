"""Jira Cloud projects as context: issues and their unrestricted comments,
polled by `updated` like GitHub pull requests.

Standard library only, API-token authentication (JIRA_BASE_URL,
JIRA_EMAIL, JIRA_API_TOKEN). Reads use the enhanced search endpoint
(/rest/api/3/search/jql, paged with nextPageToken), ordered by updated
then key, from the cursor less an overlap window. An issue with a
security level is refused, and so is a comment restricted to a role or
group: Raven has one audience, so content narrower than the project is
left out and counted, never imported with a narrower label.
See docs/context-sources.md.
"""
import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .sources import Batch, SourceError
from .store import Invalid

MISSING_CREDENTIALS = 'Set JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN where Raven runs'
ENABLE = 'python -m bridge.sources add jira --repo owner/name --target PROJ --workspace-shared'
LIMITS = ('Issues with a security level and restricted comments are not imported',
          'Deleted issues are not visible to polling; their last version is kept')
FIELDS = 'summary,description,status,resolution,reporter,created,updated,security,comment,issuetype'
OVERLAP_MS = 5 * 60 * 1000
PAGE_SIZE = 50
MAX_PAGES = 40
BODY_LIMIT = 20000
MAX_BYTES = 8 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, 'Jira redirected; check JIRA_BASE_URL', headers, fp)


class JiraClient:
    def __init__(self, base_url, email, token, timeout=30):
        self.base = base_url.rstrip('/')
        self.auth = 'Basic ' + base64.b64encode(f'{email}:{token}'.encode()).decode()
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_NoRedirect)
        self.host = urllib.parse.urlsplit(self.base).netloc.lower()

    def get(self, path, params=None):
        url = self.base + path + ('?' + urllib.parse.urlencode(params) if params else '')
        req = urllib.request.Request(url, headers={'Authorization': self.auth, 'Accept': 'application/json'})
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                body = resp.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as error:
            if error.code == 429:
                retry = (error.headers or {}).get('Retry-After') or ''
                raise SourceError('Jira rate limit reached', retry_after=float(retry) if retry.isdigit() else 60) from error
            if error.code in (401, 403):
                raise SourceError(f'Jira refused the credentials (HTTP {error.code}); check JIRA_EMAIL and '
                                  'JIRA_API_TOKEN and the account\'s project access') from error
            if error.code == 404:
                raise SourceError(f'Jira has no {path} (HTTP 404), or this account cannot see it') from error
            raise SourceError(f'Jira {path} failed: HTTP {error.code}') from error
        except (urllib.error.URLError, OSError) as error:
            raise SourceError(f'Jira is unreachable: {getattr(error, "reason", error)}') from error
        if len(body) > MAX_BYTES:
            raise SourceError('Jira response exceeded the size limit')
        return json.loads(body)


def client_from_env():
    base, email, token = (os.environ.get(k, '').strip() for k in ('JIRA_BASE_URL', 'JIRA_EMAIL', 'JIRA_API_TOKEN'))
    if not (base and email and token):
        return None
    parts = urllib.parse.urlsplit(base)
    if parts.scheme != 'https' and parts.hostname not in ('localhost', '127.0.0.1'):
        return None
    return JiraClient(base, email, token)


def identity(connection):
    return 'jira', connection['options'].get('host', '')


def normalize(target, options):
    key = target.strip().upper()
    if not re.fullmatch(r'[A-Z][A-Z0-9_]{1,19}', key):
        raise Invalid('A Jira source is a project key such as PROJ')
    days = options.get('backfill_days', int(os.environ.get('BRIDGE_JIRA_BACKFILL_DAYS', '90') or 90))
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 3650:
        raise Invalid('backfill_days must be between 1 and 3650')
    return key, {**options, 'backfill_days': days}


def probe(client, target, options):
    project = client.get(f'/rest/api/3/project/{urllib.parse.quote(target)}')
    options['host'] = client.host
    return f"{target} · {project.get('name') or target}"


# ---------------- text ----------------

def adf_text(node):
    """Atlassian Document Format to plain text: paragraphs and list items
    on their own lines, mentions and links kept as their visible text."""
    if node is None:
        return ''
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return ''.join(adf_text(n) for n in node)
    if not isinstance(node, dict):
        return ''
    kind = node.get('type')
    if kind == 'text':
        return node.get('text', '')
    if kind == 'hardBreak':
        return '\n'
    if kind in ('mention', 'emoji', 'status', 'date'):
        attrs = node.get('attrs') or {}
        return str(attrs.get('text') or attrs.get('shortName') or attrs.get('timestamp') or '')
    inner = adf_text(node.get('content'))
    if kind == 'codeBlock':
        return '\n```\n' + inner + '\n```\n'
    if kind == 'listItem':
        return '- ' + inner.strip() + '\n'
    if kind in ('paragraph', 'heading', 'blockquote', 'rule', 'panel', 'tableRow'):
        return inner.strip() + '\n'
    return inner


def _iso(value):
    """Jira's 2026-10-01T12:00:00.000+0000 as ISO 8601 with a colon offset."""
    if not value:
        return ''
    return re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', value)


def _ms(value):
    return int(datetime.fromisoformat(_iso(value).replace('Z', '+00:00')).timestamp() * 1000)


def _body(fields, batch):
    description = adf_text(fields.get('description')).strip()
    head = []
    status = (fields.get('status') or {}).get('name') or ''
    resolution = (fields.get('resolution') or {}).get('name') or ''
    kind = (fields.get('issuetype') or {}).get('name') or ''
    if kind:
        head.append(f'Type: {kind}')
    if status:
        head.append(f'Status: {status}')
    if resolution:
        head.append(f'Resolution: {resolution}')
    parts = ['\n'.join(head), description] if head else [description]
    block = fields.get('comment') or {}
    comments = [c for c in block.get('comments') or [] if isinstance(c, dict)]
    shown = []
    for comment in comments:
        if comment.get('visibility'):
            batch.refuse('restricted_comment')
            continue
        who = (comment.get('author') or {}).get('displayName') or 'Someone'
        shown.append(f"Comment by {who} ({_iso(comment.get('created') or '')}):\n{adf_text(comment.get('body')).strip()}")
    unfetched = max(0, int(block.get('total') or 0) - len(comments))
    text = '\n\n'.join(p for p in parts if p)
    # Newest comments first when they do not all fit; the record says so.
    kept = []
    budget = BODY_LIMIT - len(text) - 200
    for comment in reversed(shown):
        if len(comment) + 2 > budget:
            break
        kept.insert(0, comment)
        budget -= len(comment) + 2
    omitted = len(shown) - len(kept) + unfetched
    if kept:
        text += '\n\n' + '\n\n'.join(kept)
    if omitted:
        text += f'\n\n({omitted} earlier comment(s) not included in this record.)'
    return text[:BODY_LIMIT], status, resolution


def collect(connection, client, items):
    key, options = connection['target'], connection['options']
    host = options.get('host') or client.host
    batch = Batch(label=connection['label'])
    if connection['cursor']:
        since = int(connection['cursor']) - OVERLAP_MS
    else:
        since = int((time.time() - options.get('backfill_days', 90) * 86400) * 1000)
    jql = f'project = "{key}" AND updated >= {since} ORDER BY updated ASC, key ASC'
    newest = int(connection['cursor'] or 0)
    token, seen_tokens = '', set()
    for _ in range(MAX_PAGES):
        params = {'jql': jql, 'fields': FIELDS, 'maxResults': PAGE_SIZE}
        if token:
            params['nextPageToken'] = token
        page = client.get('/rest/api/3/search/jql', params)
        for issue in page.get('issues') or []:
            fields = issue.get('fields') or {}
            if not issue.get('id') or not issue.get('key'):
                batch.refuse('malformed_issue')
                continue
            updated = fields.get('updated') or ''
            if updated:
                newest = max(newest, _ms(updated))
            if fields.get('security'):
                batch.refuse('issue_security_level')
                continue
            body, status, resolution = _body(fields, batch)
            summary = fields.get('summary') or ''
            batch.records.append({
                'kind': 'ticket', 'provider': 'jira', 'namespace': host, 'external_id': str(issue['id']),
                'ref': issue['key'], 'title': f"{issue['key']}: {summary}"[:300], 'body': body,
                'url': f"{client.base}/browse/{issue['key']}",
                'author': ((fields.get('reporter') or {}).get('displayName') or '')[:100],
                'created_at': _iso(fields.get('created') or ''), 'updated_at': _iso(updated),
                'status': status[:40], 'source_version': _iso(updated) or summary,
                'availability': 'available', 'paths': [],
                'access_scope': json.dumps({'jira_project': key, 'audience': 'workspace-shared'}, sort_keys=True)})
        token = page.get('nextPageToken') or ''
        if page.get('isLast', not token) or not token:
            break
        if token in seen_tokens:
            raise SourceError('Jira pagination repeated a page token')
        seen_tokens.add(token)
    # Ordered by updated: a pass that stops at MAX_PAGES resumes from here.
    batch.cursor = str(newest) if newest else connection['cursor']
    return batch
