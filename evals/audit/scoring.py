"""Explicit, evaluator-owned labels. Unknown is never silently a pass."""
from .fixture import timestamp
from .ledger import digest


QUALITY = ('necessary', 'answerable', 'context_sufficient', 'neutral', 'readable', 'scope_clear')


def validate_rubric(rubric, cutoff):
    if not rubric.get('reviewer') or not isinstance(rubric.get('decisions'), list):
        raise ValueError('Rubric needs a reviewer and an explicit list of expected decisions')
    ids = set()
    for d in rubric['decisions']:
        if not d.get('id') or d['id'] in ids or not d.get('intent'):
            raise ValueError('Each expected decision needs a unique ID and intent')
        ids.add(d['id'])
        for contact in d.get('acceptable_contacts', []):
            if not contact.get('person_id') or not contact.get('evidence') or contact.get('role') not in ('decider', 'knowledgeable', 'referrer'):
                raise ValueError('Contact labels need identity, role and evidence')
            if timestamp(contact['evidence_at']) > timestamp(cutoff):
                raise ValueError('Contact justification must be available before the cutoff')
    return digest(rubric)


def score(rubric, observations):
    expected = {d['id']: d for d in rubric['decisions']}
    if len({o['decision_id'] for o in observations}) != len(observations):
        raise ValueError('One adjudicated observation is required per expected decision')
    rows = []
    for o in observations:
        if o['decision_id'] not in expected:
            raise ValueError('Observation references an unknown rubric decision')
        d = expected[o['decision_id']]
        if not isinstance(o.get('discovered'), bool):
            raise ValueError('Discovery must be an explicit observed boolean')
        if o.get('authorization', 'unknown') not in ('pass', 'fail', 'unknown'):
            raise ValueError('Authorization is a separate pass/fail/unknown check')
        contacts = {c['person_id']: c for c in d.get('acceptable_contacts', [])}
        route = o.get('first_contact')
        quality = o.get('question_quality', {})
        if any(v not in ('pass', 'fail', 'unknown') for v in quality.values()) or set(quality) - set(QUALITY):
            raise ValueError('Question scores must use the six named axes and pass/fail/unknown')
        if quality and (not o.get('judge') or not o.get('evidence_events')):
            raise ValueError('Question judgments require an attributed judge and trace evidence')
        rows.append({'decision_id': d['id'], 'discovered': bool(o.get('discovered')),
                     'first_contact': 'unknown' if not contacts or not route else 'pass' if route in contacts else 'fail',
                     'contact_role': contacts.get(route, {}).get('role', ''),
                     'authorization': o.get('authorization', 'unknown'),
                     'question_quality': {k: quality.get(k, 'unknown') for k in QUALITY},
                     'handoffs': o.get('handoffs'), 'judge': o.get('judge', ''),
                     'evidence_events': o.get('evidence_events', [])})
    present = {r['decision_id'] for r in rows}
    missing = sorted(set(expected) - present)
    return {'rubric_sha256': digest(rubric), 'expected': len(expected),
            'discovered': sum(r['discovered'] for r in rows), 'unobserved_decisions': missing,
            'first_contact_pass': sum(r['first_contact'] == 'pass' for r in rows),
            'first_contact_scored': sum(r['first_contact'] != 'unknown' for r in rows),
            'rows': rows,
            'notice': 'Knowledgeable/referrer contact is not authorization. No overall pass is inferred.'}


def pin_rubric(ledger, rubric, cutoff):
    checksum = validate_rubric(rubric, cutoff)
    rows = ledger.read() if ledger.path.exists() else []
    if any(e['kind'].startswith('mcp_') or e['kind'] == 'rubric_frozen' for e in rows):
        raise ValueError('Freeze the rubric once, before any host calls')
    return ledger.append('rubric_frozen', {'sha256': checksum, 'cutoff': cutoff, 'rubric': rubric})


def score_run(ledger, rubric, observations):
    rows = ledger.read()
    pins = [e for e in rows if e['kind'] == 'rubric_frozen']
    if len(pins) != 1 or pins[0]['data']['sha256'] != digest(rubric):
        raise ValueError('Rubric is missing or differs from the frozen rubric')
    available = {e['sequence'] for e in rows}
    for o in observations:
        if any(i not in available for i in o.get('evidence_events', [])):
            raise ValueError('Judgment refers to an absent trace event')
    return score(rubric, observations)
