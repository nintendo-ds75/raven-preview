"""Slack public channels as context: an opt-in backfill of the channels an
administrator selects, kept current like GitHub.

Raven runs as each customer's own internal Slack app; Slack's API terms
forbid persistent copies for commercially distributed apps, not for an
app built for one organization's own use. Only public channels are
read, and every pass first confirms the channel is still public, not
archived and not shared with another organization; otherwise the pass
fails closed and nothing new is written. History is read with
conversations.history from the cursor less an overlap window, and
threads are followed with conversations.replies while they stay active,
because a new reply does not bring its parent back into the window.
Deletions are not visible to polling. See docs/context-sources.md.
"""
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

MISSING_CREDENTIALS = 'Set SLACK_BOT_TOKEN (scopes channels:read and channels:history) where Raven runs'
ENABLE = ('python -m bridge.sources add slack --repo owner/name --target C0123456789 --workspace-shared '
          '(public channels only)')
LIMITS = ('Public channels only; private channels, DMs and Slack Connect channels are never read',
          'Deleted messages are not visible to polling; their last version is kept')
OVERLAP_SECONDS = 300
PAGE_SIZE = 200
MAX_PAGES = 25
MAX_THREADS = 50
SKIP_SUBTYPES = {'channel_join', 'channel_leave', 'channel_topic', 'channel_purpose', 'channel_name',
                 'channel_archive', 'channel_unarchive', 'pinned_item', 'unpinned_item', 'bot_message',
                 'bot_add', 'bot_remove', 'reminder_add', 'group_join', 'tombstone', 'message_deleted'}


class SlackHistoryClient:
    """Form-encoded Web API calls (supported by every read method)."""

    def __init__(self, token, api_base='https://slack.com/api', timeout=30):
        self.token, self.api_base, self.timeout = token, api_base.rstrip('/'), timeout

    def call(self, method, params):
        data = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, '')}).encode()
        req = urllib.request.Request(f'{self.api_base}/{method}', data=data, method='POST', headers={
            'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read(8 * 1024 * 1024).decode())
        except urllib.error.HTTPError as error:
            if error.code == 429:
                retry = (error.headers or {}).get('Retry-After') or ''
                raise SourceError(f'Slack rate limit on {method}',
                                  retry_after=float(retry) if retry.isdigit() else 60) from error
            raise SourceError(f'Slack {method} failed: HTTP {error.code}') from error
        except (urllib.error.URLError, OSError) as error:
            raise SourceError(f'Slack is unreachable: {getattr(error, "reason", error)}') from error
        if not body.get('ok'):
            hint = {'missing_scope': ' (add channels:read and channels:history to the app)',
                    'not_in_channel': ' (invite the Raven app to the channel)',
                    'channel_not_found': ' (check the channel ID)'}.get(body.get('error'), '')
            raise SourceError(f"Slack {method} failed: {body.get('error', 'unknown error')}{hint}")
        return body


def client_from_env():
    token = os.environ.get('SLACK_BOT_TOKEN', '').strip()
    if not token:
        return None
    return SlackHistoryClient(token, os.environ.get('SLACK_API_BASE', '').strip() or 'https://slack.com/api')


def identity(connection):
    return 'slack', connection['options'].get('team', '')


def normalize(target, options):
    channel = target.strip().upper()
    if not re.fullmatch(r'C[A-Z0-9]{6,20}', channel):
        raise Invalid('A Slack source is a public channel ID such as C0123456789 (from the channel details)')
    out = dict(options)
    for name, env, default, top in (('backfill_days', 'BRIDGE_SLACK_BACKFILL_DAYS', 30, 365),
                                    ('thread_days', 'BRIDGE_SLACK_THREAD_DAYS', 14, 90)):
        value = out.get(name, int(os.environ.get(env, str(default)) or default))
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= top:
            raise Invalid(f'{name} must be between 1 and {top}')
        out[name] = value
    return channel, out


def _refusal(channel):
    """Why a channel may not be read, or '' when it is an ordinary public channel."""
    if channel.get('is_im') or channel.get('is_mpim'):
        return 'it is a direct message'
    if channel.get('is_private') or channel.get('is_group'):
        return 'it is private'
    if channel.get('is_ext_shared') or channel.get('is_shared') or channel.get('is_org_shared'):
        return 'it is shared with another organization'
    if channel.get('is_archived'):
        return 'it is archived'
    return ''


def probe(client, target, options):
    team = client.call('auth.test', {})
    channel = client.call('conversations.info', {'channel': target})['channel']
    why = _refusal(channel)
    if why:
        raise Invalid(f'Slack channel {target} cannot be a context source: {why}. Only public channels are read')
    options['team'] = team.get('team_id') or ''
    options['workspace_url'] = (team.get('url') or '').rstrip('/')
    return '#' + (channel.get('name') or target)


def _iso(ts):
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def _link(options, channel, ts, thread_ts=''):
    base = options.get('workspace_url') or ''
    if not base:
        return ''
    url = f"{base}/archives/{channel}/p{ts.replace('.', '')}"
    if thread_ts and thread_ts != ts:
        url += '?' + urllib.parse.urlencode({'thread_ts': thread_ts, 'cid': channel})
    return url


def _keep(message):
    return (message.get('type', 'message') == 'message' and not message.get('bot_id')
            and message.get('subtype') not in SKIP_SUBTYPES and (message.get('text') or '').strip()
            and message.get('ts'))


def _record(connection, label, message, thread_ts=''):
    options, channel, ts = connection['options'], connection['target'], message['ts']
    text = message['text'].strip()
    edited = (message.get('edited') or {}).get('ts') or ''
    reply = bool(thread_ts and thread_ts != ts)
    return {'kind': 'slack', 'provider': 'slack', 'namespace': options.get('team', ''),
            'external_id': f'history:{channel}:{ts}', 'ref': f'history:{channel}:{ts}',
            'title': (f"{label} {'reply' if reply else 'message'}: " + re.sub(r'\s+', ' ', text))[:300],
            'body': (f'Reply in thread {thread_ts}.\n' if reply else '') + text[:19900],
            'author': (message.get('user') or '')[:100], 'created_at': _iso(ts),
            'updated_at': _iso(edited) if edited else _iso(ts), 'source_version': edited or ts,
            'url': _link(options, channel, ts, thread_ts), 'status': '', 'resolved': False,
            'availability': 'available', 'paths': [],
            'access_scope': json.dumps({'slack_channel': channel, 'visibility': 'public_channel',
                                        'audience': 'workspace-shared'}, sort_keys=True)}


def _pages(client, method, params, batch):
    cursor, seen = '', set()
    for _ in range(MAX_PAGES):
        page = client.call(method, {**params, 'limit': PAGE_SIZE, 'cursor': cursor})
        yield page.get('messages') or []
        cursor = ((page.get('response_metadata') or {}).get('next_cursor') or '').strip()
        if not cursor or not page.get('has_more', True):
            return
        if cursor in seen:
            raise SourceError(f'Slack {method} pagination repeated a cursor')
        seen.add(cursor)


def collect(connection, client, items):
    options, channel = connection['options'], connection['target']
    team = client.call('auth.test', {}).get('team_id') or ''
    if options.get('team') and team != options['team']:
        raise SourceError('The Slack token now belongs to another workspace; register the source again')
    info = client.call('conversations.info', {'channel': channel})['channel']
    why = _refusal(info)
    if why:
        raise SourceError(f'Slack channel {channel} is no longer readable as context: {why}. Nothing new was imported')
    label = '#' + (info.get('name') or channel)
    batch = Batch(label=label)
    now = time.time()
    if connection['cursor']:
        oldest = max(0.0, float(connection['cursor']) - OVERLAP_SECONDS)
    else:
        oldest = now - options.get('backfill_days', 30) * 86400
    newest = float(connection['cursor'] or 0)
    threads = {k[len('thread:'):]: v for k, v in items.items() if k.startswith('thread:')}
    recorded = set()
    for messages in _pages(client, 'conversations.history',
                           {'channel': channel, 'oldest': f'{oldest:.6f}', 'inclusive': 'true'}, batch):
        for message in messages:
            if not message.get('ts'):
                continue
            newest = max(newest, float(message['ts']))
            if message.get('reply_count') and message.get('latest_reply'):
                known = threads.get(message['ts']) or {}
                if message['latest_reply'] != known.get('latest_reply'):
                    threads[message['ts']] = {'latest_reply': known.get('latest_reply') or message['ts'],
                                              'due': True}
            if not _keep(message):
                batch.refuse('not_a_person_message')
                continue
            batch.records.append(_record(connection, label, message, message.get('thread_ts') or ''))
            recorded.add(message['ts'])
    cutoff = now - options.get('thread_days', 14) * 86400
    active = sorted(((ts, state) for ts, state in threads.items()
                     if float(state.get('latest_reply') or ts) >= cutoff or state.get('due')),
                    key=lambda pair: float(pair[1].get('latest_reply') or pair[0]), reverse=True)
    for ts, state in active[:MAX_THREADS]:
        latest = state.get('latest_reply') or ts
        for messages in _pages(client, 'conversations.replies',
                               {'channel': channel, 'ts': ts, 'oldest': latest, 'inclusive': 'true'}, batch):
            for message in messages:
                if not message.get('ts') or message['ts'] in recorded:
                    continue
                latest = max(latest, message['ts'], key=float)
                if not _keep(message):
                    continue
                batch.records.append(_record(connection, label, message, ts))
                recorded.add(message['ts'])
        batch.items['thread:' + ts] = {'latest_reply': latest}
    if len(active) > MAX_THREADS:
        for ts, state in active[MAX_THREADS:]:
            batch.items['thread:' + ts] = {**state, 'due': True}
    for ts, state in threads.items():
        if float(state.get('latest_reply') or ts) < cutoff and not state.get('due'):
            batch.items['thread:' + ts] = None
    batch.cursor = f'{newest:.6f}' if newest else connection['cursor']
    return batch
