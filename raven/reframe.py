"""Human correction of a mistaken question, with approval invalidation."""
import json

from . import authz
from .store import Invalid, check_not_abandoned, field, now


def apply(store, decision_id, data, actor=None):
    """Revise the question, not its answer. Old signatures authorize nothing new.

    The existing task/customer/path scope stays fixed. Routing is recalculated
    for the new question; the correcting person does not appoint themselves.
    """
    from . import canvas
    from .routing import rank_for_decision
    graph = store.graph
    question = field(data, 'question', limit=2000)
    rationale = field(data, 'rationale', limit=4000)
    expected = field(data, 'expected_updated_at', limit=100)
    changed = []
    with graph.transaction() as db:
        row = db.execute('''SELECT d.*, o.name AS owner_name FROM decisions d
                            LEFT JOIN owners o ON o.id=d.owner_id WHERE d.id=?''', (decision_id,)).fetchone()
        if row is None:
            raise Invalid('Decision not found')
        decision = dict(row)
        check_not_abandoned(db, decision['run_id'], decision)
        basis = authz.check(graph, actor, decision, 'correct')
        if decision['status'] in ('duplicate', 'suggested', 'adopted', 'withdrawn'):
            raise Invalid('Reframe the active decision rather than its placeholder')
        if decision['updated_at'] != expected:
            raise Invalid('This decision changed while you reviewed it. Reopen it before correcting the question.')
        if question == decision['question']:
            raise Invalid('The corrected question must differ from the existing question')
        paths = json.loads(decision.get('scope_paths') or '[]') or [decision.get('path') or 'unknown']
        facts = json.loads(decision.get('facts') or '{}')
        if canvas._fact_clashes(question, facts):
            raise Invalid('The corrected question conflicts with the task facts; create a separate scoped decision')
        ranked = rank_for_decision(graph, decision['repo'], question, paths,
                                   context=decision.get('context') or '', facts=facts,
                                   task_id=decision['run_id'], decision_id=decision['id'])
        owner = ranked[0][0] if ranked else ''
        evidence = '; '.join(ranked[0][1]) if ranked else 'Reframed question needs a verified contact'
        owner_id = graph.owner_id_for(owner) if owner else None
        required = canvas._approvers(graph, decision['repo'], question, decision.get('context') or '', '', paths)
        stamp = now()
        by = actor.name if actor is not None and actor.id else 'local operator'
        previous = {key: decision.get(key) for key in ('question', 'answer', 'rationale', 'signoff', 'signed_by',
                    'signed_hash', 'signed_revision', 'signatures', 'owner_name', 'updated_at',
                    'source', 'source_id', 'source_revision', 'evidence', 'applicability',
                    'reusable', 'rule_conditions', 'rule_expires', 'rule_by', 'rule_at', 'rule_scope',
                    'actor_id', 'actor_name', 'actor_basis')}
        previous['derived_links'] = [dict(link) for link in db.execute(
            "SELECT * FROM decision_links WHERE decision_id=? AND kind='derived'", (decision_id,))]
        graph.append_event('question_reframed', {'task_id': decision['run_id'], 'decision_id': decision_id,
                    'by': by, 'actor_id': actor.id if actor else '', 'actor_kind': actor.kind if actor else 'operator',
                    'basis': basis, 'question': question, 'rationale': rationale, 'previous': previous})
        db.execute('''UPDATE decisions SET question=?, status='pending', category='', answer=NULL,
            rationale=NULL, answered_by=NULL, prediction=NULL, source_id=NULL, source_revision='',
            source='human-reframing', evidence='', kind='new', signoff='', signed_by='', signed_hash='',
            signed_revision='', signatures='[]', reusable=0, rule_ended_at=?, model_pending=0,
            embedding=NULL, brief='', applicability='{}', rule_conditions='', rule_expires='', rule_by='',
            rule_at='', rule_scope='', actor_id=?, actor_name=?, actor_basis=?,
            owner_id=?, owner_evidence=?, routing_reason=?, required_signers=?, needs_review=0,
            review_reason='', updated_at=? WHERE id=?''',
            (question, stamp, actor.id if actor else '', by, basis,
             owner_id, evidence, evidence, json.dumps(required), stamp, decision_id))
        db.execute('DELETE FROM routing_feedback WHERE decision_id=?', (decision_id,))
        db.execute("DELETE FROM decision_links WHERE decision_id=? AND kind='derived'", (decision_id,))
        db.execute("DELETE FROM reply_readings WHERE decision_id=?", (decision_id,))
        # Source-derived answers and explicit dependency/parent children all
        # become stale. A signed child cannot keep authorizing the old premise.
        queue, seen = [decision_id], {decision_id}
        while queue:
            parent = queue.pop()
            children = db.execute('''SELECT id,run_id,status FROM decisions WHERE source_id=? OR parent_id=? OR id IN
                (SELECT decision_id FROM decision_links WHERE related_id=? AND kind='depends')''',
                (parent, parent, parent)).fetchall()
            for child in children:
                if child['id'] in seen or child['status'] in ('withdrawn', 'adopted'):
                    continue
                seen.add(child['id'])
                queue.append(child['id'])
                reason = 'Parent or dependency question was corrected; review this question and answer again'
                db.execute('''UPDATE decisions SET needs_review=1, review_reason=?,
                    status=CASE WHEN status='approved' THEN 'resolved' ELSE status END,
                    signoff=CASE WHEN signoff IN ('signed','rule') THEN 'required' ELSE signoff END,
                    signatures='[]', signed_by='', signed_hash='', signed_revision='', prediction=NULL,
                    model_pending=0, updated_at=? WHERE id=?''',
                    (reason, stamp, child['id']))
                db.execute('UPDATE runs SET needs_review=1,updated_at=? WHERE id=?', (stamp, child['run_id']))
                graph.append_event('dependent_flagged', {'task_id': child['run_id'], 'decision_id': child['id'],
                                                        'source': decision_id, 'reason': reason})
                changed.append(child['id'])
        db.execute("UPDATE runs SET status='needs_judgment',updated_at=? WHERE id=?", (stamp, decision['run_id']))
        db.execute("UPDATE notifications SET state='superseded' WHERE decision_id=? AND state='queued'", (decision_id,))
    store.notify(decision_id, 'ask')
    for dependent in set(changed):
        store.notify(dependent, 'review')
    return {**canvas.node_view(store, decision_id), 'invalidated': sorted(set(changed)),
            'notice': 'Question corrected. Its previous answer and signatures remain in history only; '
                      'the revised question needs a fresh answer and every required signoff.'}
