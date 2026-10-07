"""Private, redacted JSONL observations with an externally pinnable hash chain.

This detects edits against a retained head hash. It is not a signature or an
access-control boundary; the evaluator must keep its output outside the host.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from datetime import datetime, timezone


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


_KEY = re.compile(r'^(authorization|cookie|set-cookie|password|api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|client_secret)$', re.I)
_TOKEN = re.compile(r'\b(?:sk-ant-(?:api\d*|svc)-|sk-proj-|ghp_|github_pat_|xox[baprs]-|vcp_|rvn_|brg_)[A-Za-z0-9_-]{10,}')
_BEARER = re.compile(r'\bBearer\s+[^\s"<>]+', re.I)


def redact(value, secrets=()):
    """Known credentials plus common credential forms; not a PII anonymizer."""
    if isinstance(value, dict):
        return {k: '[redacted]' if _KEY.fullmatch(k) else redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        # MCP text content often contains a nested JSON object.
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            return encoded(redact(parsed, secrets))
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[redacted]')
        return _BEARER.sub('Bearer [redacted]', _TOKEN.sub('[redacted]', value))
    return value


def verify(rows, expected_head=None):
    previous = '0' * 64
    for index, row in enumerate(rows, 1):
        body = {k: v for k, v in row.items() if k != 'hash'}
        if row.get('sequence') != index or row.get('previous') != previous or row.get('hash') != digest(body):
            raise ValueError(f'Invalid audit chain at event {index}')
        previous = row['hash']
    if expected_head is not None and previous != expected_head:
        raise ValueError('Audit head differs from the externally retained head')
    return previous


class Ledger:
    def __init__(self, path, *, secrets=()):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.secrets = tuple(secrets)

    def append(self, kind, data, *, actor='evaluator', session=''):
        # A process lock serializes proxy sessions and the human evaluator.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a+', encoding='utf-8') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            rows = [json.loads(line) for line in f if line.strip()]
            previous = verify(rows)
            row = {'sequence': len(rows) + 1, 'previous': previous,
                   'at': datetime.now(timezone.utc).isoformat(), 'kind': kind,
                   'actor': actor, 'session': session, 'data': redact(data, self.secrets)}
            row['hash'] = digest(row)
            f.write(encoded(row) + '\n')
            f.flush()
            os.fsync(f.fileno())
        return row

    def read(self, expected_head=None):
        with self.path.open(encoding='utf-8') as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            rows = [json.loads(line) for line in f if line.strip()]
        verify(rows, expected_head)
        return rows


class ActionLog(list):
    """Preserve the replay runner's old list while recording each intervention now."""
    def __init__(self, ledger):
        super().__init__()
        self.ledger = ledger

    def append(self, value):
        self.ledger.append('evaluator_action', value, actor='simulated-human-or-evaluator')
        super().append(value)


class TaskObserver:
    """Record changed views and existing internal events without inventing history."""
    def __init__(self, ledger):
        self.ledger = ledger
        self.events = set()
        self.snapshots = {}

    def observe(self, tree, trace):
        task = tree.get('task_id') or trace['task_id']
        for event in trace.get('events', []):
            key = (task, event['id'])
            if key not in self.events:
                self.ledger.append('task_event', {'task_id': task, 'event': event}, actor='raven-event-log')
                self.events.add(key)
        state = {'task_id': task, 'nodes': tree.get('nodes', []), 'status': tree.get('status'),
                 'notes': trace.get('notes', []), 'source_anchors': trace.get('source_anchors', []),
                 'notifications': trace.get('notifications', [])}
        checksum = digest(state)
        if self.snapshots.get(task) != checksum:
            self.ledger.append('task_snapshot', state, actor='raven-observer')
            self.snapshots[task] = checksum
