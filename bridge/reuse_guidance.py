"""Presentation of a recorded reuse grant, never an applicability decision."""
from __future__ import annotations


def standing_grant_view(row):
    """Keep later grant terms separate from the original answer's rationale.

    This projection neither authorizes a request nor evaluates signatures,
    identity, expiry, source freshness or business facts. Those remain the
    existing scoped-node checks; retrieval alone supplies no new approval.
    """
    d = dict(row)
    declared = bool(d.get('reusable'))
    recorded = bool(declared or d.get('rule_at') or d.get('rule_ended_at'))
    return {
        'recorded': recorded,
        'state': 'declared' if declared else 'ended' if d.get('rule_ended_at') else 'not_declared',
        'scope': d.get('rule_scope') or '',
        'conditions': d.get('rule_conditions') or '',
        'expires_at': d.get('rule_expires') or '',
        'granted_by': d.get('rule_by') or '',
        'granted_at': d.get('rule_at') or '',
        'ended_at': d.get('rule_ended_at') or '',
        'source_needs_review': bool(d.get('needs_review')),
        'source_provenance_uncertain': bool(d.get('source_reuse_uncertain')),
        'request_applicability': 'not_evaluated_by_this_read',
        'notice': (
            'These are the current recorded standing-grant terms, separate from the original '
            'task context and rationale. A later explicit grant can permit reuse within its '
            'conditions, scope and expiry; older request-specific wording does not itself '
            'cancel that grant. This read does not establish that the grant is valid or applies '
            'here. Supply current facts on a scoped node and use its evaluated authorized/signoff '
            'result; source freshness and every other existing guard still apply.'
            if declared else
            'This decision declares no standing reuse grant of its own. Its current authorized/signoff '
            'result is separate and may be based on a source rule. Do not treat this decision as a '
            'new reusable rule; preserve its recorded answer and original context.'),
    }
