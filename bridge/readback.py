"""Bind human consent to one delivered proposal and signed message occurrence.

Transport occurrence is not a callback verification clock, queue creation time,
or processing time. Missing legacy metadata is never synthesized during retry.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone

_SIGNOFF_COMMAND = re.compile(
    r"^(sign(?:ed)?[- ]?off|approve[d]?|lgtm|yes,? sign(?:ed)?(?: off)?|ok(?:ay)?,? sign(?:ed)?(?: off)?)\W*$", re.I)
MAX_PROMPT_BYTES = 12000

_TOKEN = re.compile(r'^\s*(confirm|yes|decline|no)\s+([0-9a-f]{12})\s*[.!]?\s*$', re.I)


def migrate(db):
    have = {r['name'] for r in db.execute('PRAGMA table_info(reply_readings)')}
    for name in ('proposal_id', 'source_occurrence', 'source_event_id', 'delivered_ref', 'prompt', 'source_review'):
        if name not in have:
            db.execute(f"ALTER TABLE reply_readings ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    if 'delivered_at' not in have:
        db.execute("ALTER TABLE reply_readings ADD COLUMN delivered_at BIGINT NOT NULL DEFAULT 0")
    db.executescript('''CREATE TABLE IF NOT EXISTS reply_turns (
        platform TEXT NOT NULL, channel TEXT NOT NULL, thread_ts TEXT NOT NULL, person_id TEXT NOT NULL,
        occurrence_id TEXT NOT NULL, occurred_at BIGINT NOT NULL, callback_id TEXT NOT NULL,
        PRIMARY KEY(platform,channel,thread_ts,person_id));''')


def occurrence_time(occurrence):
    """Conservative microseconds; never infer ordering from float rounding."""
    if not isinstance(occurrence, dict) or not isinstance(occurrence.get('id'), str) or not occurrence['id']:
        return None
    raw = occurrence.get('timestamp')
    if not isinstance(raw, str):
        return None
    if occurrence.get('platform') == 'slack':
        if not re.fullmatch(r'[0-9]{1,12}\.[0-9]{6}', raw):
            return None
        seconds, micros = raw.split('.')
        value = int(seconds) * 1000000 + int(micros)
        return value if value > 0 else None
    if occurrence.get('platform') == 'teams':
        # Teams may use .NET's seven fractional digits. Sub-microsecond
        # differences deliberately tie and cannot establish a safe ordering.
        # Never accept a timezone-free date or manufacture a processing time.
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})', raw):
            return None
        try:
            stamp = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            delta = stamp - datetime(1970, 1, 1, tzinfo=timezone.utc)
            value = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
            return value if value > 0 else None
        except (ValueError, OverflowError):
            return None
    return None


def intent(text, include_signoff=False):
    from .delivery import _CONFIRM_RE, _DECLINE_RE
    token = _TOKEN.fullmatch(text or '')
    if token:
        return ('confirm' if token[1].lower() in ('yes', 'confirm') else 'decline', token[2].lower())
    if _CONFIRM_RE.fullmatch(text or '') or (include_signoff and _SIGNOFF_COMMAND.fullmatch(text or '')):
        return 'confirm', ''
    if _DECLINE_RE.fullmatch(text or ''):
        return 'decline', ''
    return '', ''


def signoff_request(text):
    return bool(_SIGNOFF_COMMAND.fullmatch(text or ''))


def confirming(text):
    return intent(text, include_signoff=True)[0] == 'confirm'


def declining(text):
    return intent(text)[0] == 'decline'


def guidance(held):
    if not held or not held.get('proposal_id'):
        return 'Please restate your answer for a fresh read-back.'
    return f"Review the read-back and reply `confirm {held['proposal_id']}` (or `decline {held['proposal_id']}`)."


def begin(graph, platform, channel, thread, person_id, occurrence, event_id=""):
    """Advance a durable per-speaker cursor. Ties never infer message order."""
    stamp = occurrence_time(occurrence)
    if stamp is None or occurrence.get('platform') != platform:
        return ''  # Read-only conversation and explicit legacy commands still work.
    with graph.transaction():
        old = graph.db.execute('SELECT * FROM reply_turns WHERE platform=? AND channel=? AND thread_ts=? AND person_id=?',
                               (platform, channel, thread, person_id)).fetchone()
        if old and stamp <= old['occurred_at']:
            # Exact retries may resume only their own occurrence; snapshots and
            # receipts separately prevent regenerating or applying a newer proposal.
            if stamp == old['occurred_at'] and occurrence['id'] == old['occurrence_id'] and event_id and event_id == old['callback_id']:
                return ''
            return 'Not recorded: this message is out of order or has an ambiguous timestamp. Please send a fresh reply to the current read-back.'
        graph.db.execute('''INSERT INTO reply_turns(platform,channel,thread_ts,person_id,occurrence_id,occurred_at,callback_id)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(platform,channel,thread_ts,person_id) DO UPDATE SET
            occurrence_id=excluded.occurrence_id,occurred_at=excluded.occurred_at,callback_id=excluded.callback_id''',
            (platform, channel, thread, person_id, occurrence['id'], stamp, event_id))
    return ''


def current(graph, platform, channel, thread, person_id, occurrence):
    stamp = occurrence_time(occurrence)
    if stamp is None or occurrence.get('platform') != platform:
        return False
    row = graph.db.execute('SELECT * FROM reply_turns WHERE platform=? AND channel=? AND thread_ts=? AND person_id=?',
                           (platform, channel, thread, person_id)).fetchone()
    return bool(row and row['occurrence_id'] == occurrence['id'] and row['occurred_at'] == stamp)


def save(delivery, channel, thread, person_id, occurrence, event_id, values, summary, snapshot=None):
    """Called under the writer transaction after semantic/snapshot checks."""
    graph = delivery.store.graph
    if not current(graph, delivery.channel, channel, thread, person_id, occurrence) or not event_id:
        return ('Not recorded: I cannot verify this message occurrence for a read-back. '
                'Please send your answer again in this thread.')
    held = delivery._reading(channel, thread, person_id)
    if held and held.get('source_event_id') == event_id:
        return held['prompt']  # A resumed occurrence cannot create a different proposal.
    from . import source_review
    kind = json.loads(values['answer']).get('kind') if values['kind'] == 'conversation' else values['kind']
    review, summary, error = source_review.prepare(delivery, values['decision_id'], kind, summary, snapshot)
    if error:
        delivery._forget_reading(channel, thread, person_id)
        return error
    proposal = uuid.uuid4().hex[:12]
    from .approval_scope import render, transport_text
    decision = delivery.store.get_decision(values['decision_id'])
    action = json.loads(values['answer']) if values['kind'] == 'conversation' else values
    # An identical semantic answer without a rationale is a co-signature.
    # All other answer proposals use Store.answer's explicit empty default,
    # not an inferred or inherited reusable applicability grant.
    replacement = (action['kind'] == 'answer' and not (
        values['kind'] == 'conversation' and decision['status'] != 'pending'
        and not action.get('rationale') and action['answer'].strip() == (decision.get('answer') or '').strip()))
    effect = ('\nProposed answer applicability: {} (no additional reuse boundaries declared).'
              if replacement else '')
    source_rebind = bool(review and decision['source_revalidation'].get('retires_rule'))
    if (replacement or source_rebind) and decision.get('reusable'):
        effect += ('\nThis approval will retire the existing standing rule. It approves this decision only. '
                   'Future automatic reuse requires a fresh explicit make-rule action.')
    # A source review already contains the complete root and escaped data.
    # Preserve its trusted framing; do not duplicate the root or escape twice.
    displayed = summary if review else transport_text(summary) + '\n' + transport_text(render(decision))
    prompt = displayed + effect + '\nNothing applied yet. ' + guidance({'proposal_id': proposal})
    # Measure the complete escaped prompt, including its confirmation code.
    # A provider-truncated message cannot constitute reviewed consent.
    from .delivery import complete_chat_text
    if not complete_chat_text(prompt):
        delivery._forget_reading(channel, thread, person_id)
        return ('Nothing recorded: the complete answer and decision scope are too large for one chat read-back. '
                'No shortened read-back can be confirmed. Open this decision in Raven to review its full scope and answer.')
    values = {**values, 'channel': channel, 'thread_ts': thread, 'person_id': person_id,
              'proposal_id': proposal, 'source_occurrence': json.dumps(occurrence, sort_keys=True),
              'source_event_id': event_id, 'delivered_ref': '', 'delivered_at': 0, 'prompt': prompt,
              'source_review': review}
    columns = list(values)
    updates = ','.join(f'{key}=excluded.{key}' for key in columns if key not in ('channel', 'thread_ts', 'person_id'))
    graph.db.execute(f"INSERT INTO reply_readings({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) "
                     f'ON CONFLICT(channel,thread_ts,person_id) DO UPDATE SET {updates}', tuple(values.values()))
    return prompt


def mark_delivered(graph, platform, event_id, channel, thread, message_ref):
    if not isinstance(message_ref, str) or not message_ref:
        return
    delivered_at = (occurrence_time({'platform': 'slack', 'id': message_ref, 'timestamp': message_ref})
                    if platform == 'slack' else time.time_ns() // 1000)
    if not delivered_at:
        return
    with graph.transaction():
        # The queue ACK belongs only to the generation created by this event.
        rows = graph.db.execute('SELECT * FROM reply_readings WHERE channel=? AND thread_ts=? AND source_event_id=?',
                                (channel, thread, event_id)).fetchall()
        for row in rows:
            if json.loads(row['source_occurrence'] or '{}').get('platform') != platform:
                continue
            graph.db.execute("UPDATE reply_readings SET delivered_ref=?,delivered_at=? WHERE proposal_id=? AND delivered_ref=''",
                             (message_ref, delivered_at, row['proposal_id']))


def refusal(delivery, channel, thread, person_id, held, text, occurrence):
    """Call inside the same writer transaction as snapshot consumption/action."""
    if not held:
        return 'Not recorded: there is no read-back waiting for confirmation. Tell me the answer you want recorded.'
    try:
        source = json.loads(held.get('source_occurrence') or '{}')
    except (TypeError, ValueError):
        source = {}
    now, then = occurrence_time(occurrence), occurrence_time(source)
    if (now is None or then is None or occurrence.get('platform') != delivery.channel
            or source.get('platform') != delivery.channel or now <= then
            or not current(delivery.store.graph, delivery.channel, channel, thread, person_id, occurrence)):
        return 'Not recorded: this confirmation has missing, stale, or ambiguous message metadata. Please send a fresh reply. ' + guidance(held)
    if not held.get('delivered_ref') or not held.get('delivered_at'):
        return 'Not recorded: the read-back has not been delivered yet. Wait for it before confirming.'
    if now <= held['delivered_at']:
        return 'Not recorded: this reply was written before the read-back was delivered. Please send a fresh confirmation. ' + guidance(held)
    kind, token = intent(text, include_signoff=True)
    if not kind or (token and token != held.get('proposal_id')):
        return 'Not recorded: that confirmation does not identify the current read-back. ' + guidance(held)
    if not token and held.get('source_review'):
        return 'Not recorded: reviewing current sources requires the exact read-back code. ' + guidance(held)
    if not token and occurrence.get('reply_to') != held['delivered_ref']:
        return 'Not recorded: a bare reply could refer to a different read-back. ' + guidance(held)
    return ''


def record_confirmation(delivery, held, person, occurrence):
    """Append exact consent provenance in the transaction that applies it."""
    from .approval_scope import revision
    from .store import Invalid
    graph = delivery.store.graph
    if not graph.db.in_transaction:
        raise Invalid('Read-back confirmation requires its selected writer transaction')
    current = dict(graph.db.execute('SELECT * FROM decisions WHERE id=?', (held['decision_id'],)).fetchone())
    current['events'] = [dict(e) for e in graph.db.execute(
        'SELECT id,kind FROM events WHERE decision_id=? ORDER BY id', (held['decision_id'],))]
    if held['revision'] != revision(current):
        raise Invalid('The decision changed since this read-back. Nothing was recorded; review it again.')
    graph.append_event('readback_confirmed', {
        'decision_id': held['decision_id'], 'task_id': current['run_id'],
        'proposal_id': held['proposal_id'],
        'actor_id': person['id'], 'actor_name': person['name'], 'channel': delivery.channel,
        'thread': held['thread_ts'], 'source_event_id': held['source_event_id'],
        'source_occurrence': json.loads(held['source_occurrence']), 'confirmation_occurrence': occurrence,
        'delivered_ref': held['delivered_ref'], 'delivered_at': held['delivered_at'],
        'proposal': {key: held[key] for key in ('revision', 'kind', 'answer', 'rationale', 'recipient', 'prompt', 'source_review')},
    })
