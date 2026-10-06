"""A scoped contact invitation does not confer owner or signer standing.

Opt-in lifecycle for unverified contact routes. Only a reply from the bound,
authenticated Slack recipient, followed by an explicit confirmation of their
claim/referral, can assign this one decision. There are no authority-map writes.
"""
import json
import os
import re

from .graph import now_iso


def revision(row):
    """Bind an invitation to the complete scope, even at an equal timestamp."""
    import hashlib
    fields = ('id', 'run_id', 'repo', 'question', 'context', 'category', 'path', 'scope_paths',
              'facts', 'status', 'answer', 'signoff', 'required_signers', 'owner_id',
              'contact_person_id', 'contact_candidate', 'updated_at')
    return hashlib.sha256(json.dumps({k: row.get(k) for k in fields}, sort_keys=True).encode()).hexdigest()


def enabled():
    return os.environ.get('BRIDGE_CONTACT_CLAIMS') == '1'


def prepare(graph, row, run, paths, requester='', *, explicit_owner=False, reading=None, channel='slack'):
    """Return contact-only fields for an unverified route, without a send.

    Called before publication, and again before the model's first notification.
    The caller owns the revision-checked decision write. Source claims cannot
    mutate a decision somebody already owns through a map or human assignment.
    """
    if (channel != 'slack' or not enabled() or explicit_owner or row.get('signoff') in ('rule', 'signed')
            or row.get('signed_by') or graph.get_setting('require_verified_route') == '1'):
        return {}
    from .contact_claims import protected_contacts
    from .routing import contact_for, route_ranked
    from .store import repo_key
    from .graph import parse_facts
    repo = graph.resolve_repo(repo_key(run['repo']))
    facts = parse_facts(row.get('facts') or '')
    context, category = row.get('context') or '', row.get('category') or ''
    protected = protected_contacts(graph, repo, row['question'], paths, context, category, facts)
    owner = graph.db.execute('SELECT name FROM owners WHERE id=?', (row.get('owner_id') or '',)).fetchone()
    owner_name = owner['name'] if owner else row.get('owner_name') or ''
    evidence = row.get('owner_evidence') or ''
    coordinator = graph.coordinator(repo)
    if ((coordinator and owner_name == coordinator['name'] and evidence.startswith('coordinator fallback:'))
            or owner_name in protected or evidence.startswith('verified:') or
            evidence.startswith('learned first contact:') or 'signed the reused answer in decision' in evidence):
        return {}
    suggestions = (reading or {}).get('suggested_contacts')
    if suggestions is None:
        ranked = route_ranked(graph, repo, row['question'], path=paths[0] if paths else '',
            also_paths=paths[1:], context=context, category=category, facts=facts, requester=requester)
        suggestions = [{'name': n, 'evidence': ev, 'score': score} for n, ev, score in ranked]
    candidate = suggestions[0] if suggestions else None
    # An explicit protected route newly learned during inference is still an
    # owner route. The caller's normal revision guards protect human updates.
    if candidate and candidate['name'] in protected:
        return {}
    person = contact_for(graph, candidate['name']) if candidate else None
    unavailable = set(json.loads(graph.get_setting('slack_unavailable') or '[]'))
    usable = (person and person['active'] and person['role'] != 'viewer' and person['slack_id']
              and person['slack_id'] not in unavailable)
    detail = {'name': person['name'], 'evidence': candidate['evidence'],
              'claims': [c for c in (reading or {}).get('claims', []) if c['person_id'] == person['id']],
              'reading_status': (reading or {}).get('status', 'fallback'),
              'reading_reason': (reading or {}).get('reason', 'plain_authorship_fallback')} if usable else {}
    return {'owner_id': None, 'owner_evidence': '', 'contact_person_id': person['id'] if usable else '',
            'contact_candidate': json.dumps(detail),
            'routing_reason': 'Unverified first contact; explicit human claim or referral required before ownership'}


def notify(store, row):
    if row.get('contact_person_id') and not row.get('owner_id'):
        person = store.graph.get_person(row['contact_person_id'])
        if person and person['active'] and person['role'] != 'viewer':
            store.notify(row['id'], 'contact', to=person['name'])
        return True
    return False


def respond(delivery, note, decision, person, actor, text):
    """Deterministic two-step claim/referral; never interprets an answer as one."""
    graph = delivery.store.graph
    if (actor.kind != 'slack' or note.get('kind') != 'contact' or note.get('state') != 'sent' or
            not note.get('external_ref') or note.get('person_id') != person['id'] or
            decision.get('contact_person_id') != person['id']):
        return 'This contact invitation belongs to its named recipient. Nothing was assigned or approved.'
    channel, _, thread = note['external_ref'].partition(':')
    if decision.get('owner_id'):
        return 'This question already has an owner. Use the current decision thread; nothing changed here.'
    task = graph.db.execute('SELECT status FROM runs WHERE id=?', (decision['run_id'],)).fetchone()
    if (not task or task['status'] in ('abandoned', 'completed') or decision.get('model_pending')
            or decision.get('status') == 'withdrawn'):
        return 'This question is not ready for a contact claim. Nothing was assigned or approved.'
    expected = revision(decision)
    if note.get('content_hash') != expected:
        return 'The question changed, or its scope changed, since this invitation. Nothing was assigned or approved.'
    raw = text.strip()
    held = delivery._reading(channel, thread, person['id'])
    if raw.casefold() == 'confirm contact':
        if not held or held['kind'] != 'contact_claim' or held['revision'] != expected:
            return 'No current contact claim is waiting for confirmation. Reply `claim` or `ask @person` first.'
        target = graph.get_person(held['recipient'])
        if not target or not target['active'] or target['role'] == 'viewer' or not target['slack_id']:
            return 'That contact is no longer available. Nothing was assigned.'
        with graph.transaction():
            current = delivery.store.get_decision(decision['id'])
            invitation = delivery.get(note['id'])
            speaker = graph.get_person(person['id'])
            task = graph.db.execute('SELECT status FROM runs WHERE id=?', (current['run_id'],)).fetchone()
            pending = delivery._reading(channel, thread, person['id'])
            if (not speaker or not speaker['active'] or speaker['role'] == 'viewer' or
                    not task or task['status'] in ('abandoned', 'completed') or
                    revision(current) != expected or current.get('owner_id') or
                    current.get('contact_person_id') != person['id'] or pending != held or
                    not invitation or invitation['state'] != 'sent' or invitation['external_ref'] != note['external_ref']):
                return 'The invitation changed before confirmation. Nothing was assigned.'
            delivery.store.claim_slack_question(decision['id'], target['id'], actor)
            # This human claim/referral applies only to this question, even if
            # the person later answers it. Do not learn standing ownership.
            graph.append_event('route_learning_optout', {'task_id': current['run_id'],
                'decision_id': decision['id'], 'by': person['name'], 'reason': 'contact invitation: this question only'})
            graph.append_event('contact_claim_confirmed', {'task_id': current['run_id'],
                'decision_id': decision['id'], 'by': person['name'], 'to': target['name'],
                'notification_id': note['id']})
            delivery._forget_reading(channel, thread, person['id'])
        return (f"Assigned this question to {target['name']} after your confirmation. They may now answer or sign "
                'this question. No answer, signature, or standing authority was recorded.')
    if raw.casefold() in ('cancel', 'no', 'decline'):
        delivery._forget_reading(channel, thread, person['id'])
        return 'Contact claim canceled. Nothing was assigned or approved.'
    claim = re.fullmatch(r"(?:claim|i['’]ll take this|i can answer|take this)", raw, re.I)
    referral = re.fullmatch(r'(?:ask|refer to|not me)\s+<@([A-Z0-9]+)(?:\|[^>]*)?>[.!]?', raw, re.I)
    target = person if claim else delivery.slack_person(referral[1]) if referral else None
    if not target or not target['active'] or target['role'] == 'viewer':
        return ('This is only a contact check. To take responsibility for this question, reply `claim`; '
                'to suggest someone else, reply `ask @person` using a Slack mention. I will ask you to '
                'confirm before assigning it. An acknowledgment or answer does not claim or approve it.')
    with graph.transaction():
        current = delivery.store.get_decision(decision['id'])
        if revision(current) != expected or current.get('owner_id') or current.get('contact_person_id') != person['id']:
            return 'The invitation changed. Nothing was assigned.'
        graph.db.execute('INSERT INTO reply_readings(channel,thread_ts,person_id,decision_id,revision,kind,recipient,created_at) '
            "VALUES(?,?,?,?,?,'contact_claim',?,?) ON CONFLICT(channel,thread_ts,person_id) DO UPDATE SET "
            'decision_id=excluded.decision_id,revision=excluded.revision,kind=excluded.kind,recipient=excluded.recipient,'
            'created_at=excluded.created_at', (channel, thread, person['id'], decision['id'], expected, target['id'], now_iso()))
    return (f"Assign only this question to {target['name']}? That will let them answer and sign this question, "
            'without approving it or changing standing ownership. Reply `confirm contact` to confirm, or `cancel`.')
