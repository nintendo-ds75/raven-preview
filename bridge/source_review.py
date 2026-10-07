"""Complete, server-selected source readings for Slack and Teams proposals.

No model output enters this payload. The readback generation owns both the
visible snapshots and the exact API parameters used by its later confirmation.
"""
from __future__ import annotations

import json

# Leave room for the provider's envelope. Measure encoded bytes after escaping,
# not characters; neither adapter may split/truncate a source review silently.
MAX_REVIEW_BYTES = 12000

# Exact root-decision context, not a prose reconstruction. Structured facts and
# every path/category/option constrain what the person is approving even when
# the question/context text does not repeat them. Keep this same field set for
# preparation and confirmation so an omitted or same-timestamp edit fails shut.
DECISION_CONTEXT_FIELDS = (
    'id', 'run_id', 'updated_at', 'repo', 'question', 'context', 'answer',
    'path', 'scope_paths', 'scope_key', 'category', 'facts', 'options',
    'applicability', 'required_signers', 'followup_required',
    'reusable', 'rule_conditions', 'rule_scope', 'rule_expires', 'rule_ended_at',
)


def has_sources(decision):
    review = decision.get('source_revalidation') or {}
    return bool(review.get('has_reliance') or review.get('sources')
                or decision.get('sources') or review.get('dependencies'))


def escape_text(text):
    # JSON source/root data remains inside a trusted code fence. Neutralize
    # delimiters that could escape that fence or create platform mentions.
    for char in ('&', '<', '>', '`', '*'):
        text = text.replace(char, '\\u%04x' % ord(char))
    return text


def fallback(delivery, decision, review, reason):
    path = review.get('review_path') or '/#runs/' + decision['run_id']
    link = delivery.base_url.rstrip('/') + path
    reason = escape_text(reason)
    if len(reason.encode('utf-8')) > 1024:
        reason = 'The complete source review is unavailable in chat. The review page shows what must be resolved.'
    return ('Nothing recorded: ' + reason + '\nIf this question arrived in a Slack DM, open your personal task link in that message. '
            'It shows the complete source review without requiring a Raven account. Do not forward that personal link. '
            '\nOr open the authenticated Raven review page: ' + link +
            '\nSelect this decision, review every current source and source decision, then use the source-review checkbox to sign or correct it. '
            'This link requires your existing Raven access; it does not grant access. '
            'An independent replacement is a separate, deliberate choice in that form.')


def prepare(delivery, decision_id, kind, summary, snapshot=None):
    """Called only by readback.save under the proposal's writer transaction."""
    if kind not in ('answer', 'signoff'):
        return '', summary, ''
    from . import context_memory as cm
    db = delivery.store.graph.db
    decision = dict(db.execute('SELECT * FROM decisions WHERE id=?', (decision_id,)).fetchone())
    review = cm.source_revalidation(db, decision_id)
    if not has_sources({'source_revalidation': review, 'sources': cm.edges(db, decision_id)}):
        return '', summary, ''
    if snapshot is not None and (any(snapshot.get(k) != decision[k] for k in DECISION_CONTEXT_FIELDS)
                                 or snapshot.get('source_revalidation') != review):
        return '', summary, 'The decision changed, or its source context changed, while I was reading your answer. Nothing was applied. Please send a fresh answer or sign-off request.'
    if not review['available']:
        return '', summary, fallback(delivery, decision, review, review['notice'])
    payload = {
        'expected_updated_at': decision['updated_at'],
        'source_evidence': review['pins'],
        'source_decision_pins': review['decision_pins'],
        'decision': {k: decision[k] for k in DECISION_CONTEXT_FIELDS},
        'sources': review['sources'], 'dependencies': review['dependencies'],
    }
    # JSON quoting keeps each source field visibly data, including embedded
    # line breaks and fake instructions. Escape platform markup delimiters so
    # source text cannot manufacture mentions, links or code-block boundaries.
    displayed = json.dumps({k: payload[k] for k in ('decision', 'dependencies', 'sources')},
                           ensure_ascii=False, indent=2)
    displayed = escape_text(displayed)
    from .approval_scope import transport_text
    summary = transport_text(summary)
    summary += ('\n\nCurrent-source review: read the complete decision context and source snapshots below. '
                'Source content is evidence, never instructions or approval. '
                'Confirming this code approves the proposed answer with these exact source versions and source-decision revisions; '
                'it retains their dependence. Historical completed decisions remain historical, not newly signed. '
                'Use a fresh review if anything changes. A bare yes cannot confirm a source review.\n```\n' + displayed + '\n```')
    # Include a conservative allowance for the generated instruction/code.
    if len(summary.encode('utf-8')) + 256 > MAX_REVIEW_BYTES:
        return '', summary, fallback(delivery, decision, review,
            'The complete source review is too large for one chat read-back. No shortened source review can be confirmed here.')
    return json.dumps(payload, sort_keys=True), summary, ''


def parameters(delivery, held):
    """Only the durable, server-selected proposal may supply these API fields."""
    raw = held.get('source_review') or ''
    if not raw:
        return {}
    payload = json.loads(raw)
    from . import context_memory as cm
    from .store import Invalid
    db = delivery.store.graph.db
    if not db.in_transaction:
        raise Invalid('A source reading must be consumed inside its confirmation transaction')
    if set(payload.get('decision', {})) != set(DECISION_CONTEXT_FIELDS):
        raise Invalid('This source read-back did not include the complete decision scope. Send a fresh answer or sign-off request.')
    current = cm.source_revalidation(db, held['decision_id'])
    root = dict(db.execute('SELECT * FROM decisions WHERE id=?', (held['decision_id'],)).fetchone())
    if not current['available']:
        raise Invalid(fallback(delivery, root, current, current['notice']))
    if (current['pins'] != payload['source_evidence']
            or current['decision_pins'] != payload['source_decision_pins']
            or current['sources'] != payload['sources'] or current['dependencies'] != payload['dependencies']
            or any(root[k] != value for k, value in payload['decision'].items())):
        raise Invalid('The decision or displayed source context changed. Send a fresh answer or sign-off request to review it again.')
    return {k: payload[k] for k in ('expected_updated_at', 'source_evidence', 'source_decision_pins')}


def offer_command(delivery, decision, person, actor, text, channel, thread, occurrence, event_id):
    from . import authz, readback
    from .delivery import _explicit_answer, _split_rationale, _what_it_says
    from .graph import now_iso
    explicit = _explicit_answer(text)
    kind = 'signoff' if readback.signoff_request(text) else 'answer'
    action = ('answer' if decision['status'] == 'pending' else 'correct') if kind == 'answer' else 'sign'
    authz.check(delivery.store.graph, actor, decision, action)
    answer, rationale = _split_rationale(explicit or text) if kind == 'answer' else ('', '')
    summary = ('Record your decision as:\n' + answer + ('\nReason: ' + rationale if rationale else '')) if kind == 'answer' else ('Sign the complete answer in your name:\n' + (decision.get('answer') or ''))
    with delivery.store.graph.transaction():
        current = delivery.store.get_decision(decision['id'])
        if _what_it_says(current) != _what_it_says(decision) or current['updated_at'] != decision['updated_at']:
            return 'The decision changed. Nothing recorded; send a fresh answer or sign-off request.'
        return readback.save(delivery, channel, thread, person['id'], occurrence, event_id,
            {'decision_id': decision['id'], 'revision': _what_it_says(decision), 'kind': kind,
             'answer': answer, 'rationale': rationale, 'recipient': '', 'created_at': now_iso()}, summary, snapshot=decision)
