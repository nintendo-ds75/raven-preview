"""Stable pseudonyms for evaluations of public repository history.

Call ``pseudonymize(graph)`` after ingest and before routing, model calls,
or writing results. This preserves identity links, not anonymity against
someone who already has the original public history. Never run it on a
customer workspace.
"""
from __future__ import annotations

import hashlib
import json
import re
from bridge import identity
from bridge.graph import is_team_handle

SALT = 'raven-eval'
DOMAIN = 'example.invalid'
_NOREPLY_RE = re.compile(r'^(?:\d+\+)?([^@]+)@users\.noreply\.github\.com$', re.I)
_BOT_RE = re.compile(r'\[bot\]|(?:^|[^a-z])bot$|^(?:dependabot|renovate|github-actions|grafanabot)\b', re.I)
_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')


def _digest(kind, key):
    return hashlib.sha256(f'{SALT}|{kind}|{key}'.encode()).hexdigest()[:6]


def is_service(name='', address=''):
    return bool(_BOT_RE.search(name or '')) or (name or '').strip().lower() in {
        'github', 'github actions', 'github-actions'} or address.lower() == 'noreply@github.com'


def name_key(name):
    tokens = identity.name_tokens(name)
    return (tokens[0] if len(tokens) == 1 else f'{tokens[0]} {tokens[-1]}') if tokens else ''


def label(name):
    if not name or re.fullmatch(r'Engineer [0-9a-f]{6}', name) or is_service(name):
        return name
    return 'Engineer ' + _digest('name', name_key(name))


def handle(login):
    login = (login or '').lstrip('@').lower()
    if not login or re.fullmatch(r'engineer-[0-9a-f]{6}', login) or is_service(login):
        return login
    return 'engineer-' + _digest('handle', login)


def email(address):
    address = (address or '').strip().lower()
    if not address or is_service(address=address) or re.fullmatch(r'engineer-[0-9a-f]{6}@example.invalid', address):
        return address
    match = _NOREPLY_RE.match(address)
    if match:
        return handle(match[1]) + '@users.noreply.github.com'
    return 'engineer-' + _digest('email', address) + '@' + DOMAIN


def listed(value):
    if is_team_handle(value) or is_service(value):
        return value
    if value.startswith('@'):
        return '@' + handle(value)
    return label(value)


def alias(value):
    return email(value) if '@' in value and not value.startswith('@') else listed(value)


def person(name='', address='', login=''):
    if is_service(name, address):
        return name, address, login
    return label(name), email(address), handle(login)


class Scrubber:
    def __init__(self):
        self.mapping = {}
        self.counts = {}
        self._pattern = None

    def learn(self, name='', address='', login=''):
        if is_service(name, address):
            return
        for old, new in zip((name, address, login), person(name, address, login)):
            if old and new and old != new:
                self.mapping[old.lower()] = new
        match = _NOREPLY_RE.match(address or '')
        if match:
            self.mapping[match[1].lower()] = handle(match[1])
        self._pattern = None

    def scrub(self, text):
        if not isinstance(text, str):
            return text
        if self.mapping:
            if self._pattern is None:
                self._pattern = re.compile(r'(?<![\w])(?:' + '|'.join(re.escape(k) for k in
                    sorted(self.mapping, key=len, reverse=True)) + r')(?![\w])', re.I)
            text = self._pattern.sub(lambda m: self.mapping[m[0].lower()], text)
        return text

    def scrub_json(self, value):
        if isinstance(value, dict):
            return {self.scrub(k): self.scrub_json(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub_json(v) for v in value]
        return self.scrub(value)


def learn_graph(graph):
    scrub = Scrubber()
    for table, name, address, login in (
        ('engineers', 'name', 'email', 'github_username'), ('change_people', 'engineer', 'email', None),
        ('blame_lines', 'engineer', 'email', None), ('people', 'name', 'email', 'github_login'),
        ('gh_users', 'name', 'email', 'login')):
        for row in graph.db.execute(f'SELECT * FROM {table}'):
            scrub.learn(row[name], row[address], row[login] if login else '')
    for row in graph.db.execute('SELECT person, email FROM listings'):
        if not is_team_handle(row['person']):
            scrub.learn('' if row['person'].startswith('@') else row['person'], row['email'],
                        row['person'].lstrip('@') if row['person'].startswith('@') else '')
    for row in graph.db.execute('SELECT engineer FROM ownership'):
        scrub.learn(row['engineer'])
    return scrub


def pseudonymize(graph):
    """Rewrite ingested identities, retaining source IDs and graph links.

    Duplicate engineer spelling rows coalesce; their IDs have no inbound links.
    """
    from bridge import context_memory as cm
    scrub = learn_graph(graph)
    tables = ('engineers', 'change_people', 'blame_lines', 'listings', 'ownership', 'gh_users',
              'gh_pulls', 'intents', 'people', 'changes', 'owners')
    with graph.transaction():
        if graph.db.execute('SELECT 1 FROM decisions LIMIT 1').fetchone():
            raise ValueError('Pseudonymize a fresh evaluation graph before creating decisions')
        for table in tables:
            rows = [dict(row) for row in graph.db.execute(f'SELECT * FROM {table}')]
            if not rows:
                scrub.counts[table] = 0
                continue
            # Rebuild tables with natural keys because spelling variants can
            # coalesce to one identity. Foreign links refer to preserved IDs.
            keyed = 'id' in rows[0]
            if not keyed:
                graph.db.execute(f'DELETE FROM {table}')
            engineer_keys = {}
            for row in rows:
                # PostgreSQL maintains this generated FTS column itself.
                row.pop('search_vector', None)
                for col, value in row.items():
                    if isinstance(value, str) and col not in {'id', 'repo', 'sha', 'ref', 'path', 'path_prefix', 'slack_id', 'person_id'}:
                        row[col] = scrub.scrub(value)
                if table == 'engineers':
                    # Spelling/email-case aliases can become one natural key.
                    # Engineer IDs have no inbound foreign keys; history and
                    # ownership join by names/emails, transformed below too.
                    key = (row['name'], row['email'])
                    prior = engineer_keys.get(key)
                    if prior:
                        graph.db.execute('UPDATE engineers SET active=CASE WHEN active=1 OR ?=1 THEN 1 ELSE 0 END, '
                            "github_username=CASE WHEN github_username='' THEN ? ELSE github_username END WHERE id=?",
                            (row['active'], row['github_username'], prior))
                        graph.db.execute('DELETE FROM engineers WHERE id=?', (row['id'],))
                        continue
                    engineer_keys[key] = row['id']
                if table == 'blame_lines':
                    old = graph.db.execute('SELECT lines FROM blame_lines WHERE repo=? AND rev=? AND path=? AND engineer=?',
                                           tuple(row[k] for k in ('repo','rev','path','engineer'))).fetchone()
                    if old:
                        graph.db.execute('UPDATE blame_lines SET lines=lines+? WHERE repo=? AND rev=? AND path=? AND engineer=?',
                                         (row['lines'], *[row[k] for k in ('repo','rev','path','engineer')]))
                        continue
                if keyed:
                    values = {k: v for k, v in row.items() if k != 'id'}
                    graph.db.execute(f'UPDATE {table} SET {",".join(k + "=?" for k in values)} WHERE id=?',
                                     (*values.values(), row['id']))
                else:
                    cols = ','.join(row)
                    graph.db.execute(f'INSERT OR IGNORE INTO {table} ({cols}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
            scrub.counts[table] = len(rows)
        # These are still unconsumed evaluation fixtures, not customer history.
        # Retain source/version identities while transforming their content too.
        for row in graph.db.execute('SELECT id,snapshot FROM source_versions').fetchall():
            snapshot = scrub.scrub_json(json.loads(row['snapshot']))
            fingerprint = cm.digest({k: v for k, v in snapshot.items()
                                     if k not in ('source_updated_at', 'source_version', 'source_sequence')})
            graph.db.execute('UPDATE source_versions SET snapshot=?,fingerprint=? WHERE id=?',
                             (cm.encoded(snapshot), fingerprint, row['id']))
        graph.db.execute('DELETE FROM model_cache')
        graph.set_setting('eval_pseudonyms', '1')
    for repo in graph.known_repos():
        graph._bump(repo)
    return scrub


def members_from_graph(graph, team_id='TLIVE'):
    members = []
    for row in graph.db.execute('SELECT name, email FROM engineers ORDER BY name, email'):
        if is_service(row['name'], row['email']):
            continue
        members.append({'id': 'U' + _digest('slack', row['name']).upper(), 'team_id': team_id,
                        'name': 'engineer-' + _digest('slack', row['name']), 'real_name': row['name'],
                        'deleted': False, 'is_bot': False,
                        'profile': {'real_name': row['name'], 'display_name': row['name'],
                                    'email': 'engineer-' + _digest('email', row['email']) + '@example.invalid'}})
    return members
