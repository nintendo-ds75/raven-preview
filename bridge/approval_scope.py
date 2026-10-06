"""The stored decision scope a human reviews, without inferring facts from prose.

One field inventory drives display, revision binding and new signature
provenance. Co-signatures and timestamps are deliberately not decision scope.
Historical signatures are never upgraded into attestations they did not make.
"""
from __future__ import annotations

import hashlib
import json

# JSON columns have empty legacy spellings; normalize only their encoding,
# never their values or the whitespace inside fact strings.
_FIELDS = (
    ('id', 'Decision', None), ('run_id', 'Task', None), ('repo', 'Repository', None),
    ('question', 'Question', None), ('context', 'Context', None),
    ('path', 'Path', None), ('scope_paths', 'Scope paths', []), ('scope_key', 'Scope key', None),
    ('category', 'Category', None), ('facts', 'Facts', {}), ('options', 'Options', []),
    ('applicability', 'Applicability', {}), ('required_signers', 'Required signers', []),
    ('followup_required', 'Required follow-up', 0), ('reusable', 'Reusable rule', 0),
    ('rule_conditions', 'Rule conditions', None), ('rule_scope', 'Rule scope', None),
    ('rule_expires', 'Rule expiry', None), ('rule_ended_at', 'Rule ended', None),
)


def snapshot(decision):
    row = dict(decision)
    result = {}
    for key, _label, default in _FIELDS:
        value = row.get(key)
        if isinstance(default, (dict, list)):
            if value in (None, ''):
                value = default.copy()
            elif isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    pass  # Preserve malformed legacy text; never silently erase it.
        elif value is None:
            value = '' if default is None else default
        result[key] = value
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def revision(decision):
    row = dict(decision)
    return 'c:' + digest({'scope': snapshot(row), 'answer': row.get('answer') or '',
                         'owner_id': row.get('owner_id') or '',
                         'state': 'open' if row.get('status') == 'pending' else 'settled',
                         'invalidations': [str(e['id']) for e in row.get('events', [])
                                           if e.get('kind') in ('dependent_flagged', 'prediction_withdrawn', 'source_review_required')]})


def render(decision, *, exclude=()):
    scope = snapshot(decision)
    lines = ['Decision scope (recorded fields; no facts inferred from prose):']
    for key, label, _default in _FIELDS:
        if key in exclude:
            continue
        value = scope[key]
        # Facts/applicability are explicit even when absent. No silent clipping:
        # the delivered proposal and its persisted prompt show the same scope.
        if value in ('', [], {}, 0, None) and key not in ('facts', 'applicability'):
            continue
        lines.append(label + ': ' + (json.dumps(value, ensure_ascii=False, sort_keys=True)
                                    if not isinstance(value, str) else value))
    return '\n'.join(lines)


def transport_text(text):
    """Display stored data literally, without Slack mentions or Teams links.

    Literal Unicode escapes keep the exact value reviewable without relying
    on the two providers having the same Markdown/HTML escape semantics.
    This representation is display-only; snapshot and revision keep raw data.
    """
    for char in ('\\', '&', '<', '>', '`', '*', '_', '~', '[', ']', '(', ')'):
        text = text.replace(char, '\\u%04x' % ord(char))
    return text.replace('://', '\\u003a//')


def scope_hash(decision):
    return digest(snapshot(decision))


def review_epoch(decision):
    return [str(e['id']) for e in dict(decision).get('events', [])
            if e.get('kind') in ('dependent_flagged', 'prediction_withdrawn', 'source_review_required')]
