"""Learn first contacts from completed human conversations, not inferred authority."""
import hashlib
from datetime import datetime, timezone
import json
import re
from .graph import now_iso, parse_facts


def migrate(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS routing_feedback (
        decision_id TEXT NOT NULL, person_id TEXT NOT NULL, repo TEXT NOT NULL,
        question TEXT NOT NULL, category TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
        facts TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(decision_id, person_id));
        CREATE INDEX IF NOT EXISTS routing_feedback_repo ON routing_feedback(repo,updated_at);''')
    db.executescript('''CREATE TABLE IF NOT EXISTS contact_observations (
        id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, sequence INTEGER NOT NULL,
        from_person_id TEXT NOT NULL, to_person_id TEXT NOT NULL, outcome TEXT NOT NULL,
        reason TEXT NOT NULL, repo TEXT NOT NULL, question TEXT NOT NULL,
        category TEXT NOT NULL, path TEXT NOT NULL, facts TEXT NOT NULL, created_at TEXT NOT NULL,
        source_revision TEXT NOT NULL, observed_at TEXT NOT NULL, source_context TEXT NOT NULL,
        UNIQUE(decision_id,sequence));
        CREATE INDEX IF NOT EXISTS contact_observations_repo ON contact_observations(repo,created_at);''')
    db.executescript('''CREATE TABLE IF NOT EXISTS scope_clarifications (
        task_id TEXT NOT NULL, request_key TEXT NOT NULL, question TEXT NOT NULL,
        detail TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(task_id, request_key));''')


def record(graph, decision_id, person_id, outcome):
    d = graph.db.execute('SELECT * FROM decisions WHERE id=?', (decision_id,)).fetchone()
    if not d or not person_id:
        return
    if outcome == 'answered' and graph.db.execute(
            "SELECT 1 FROM events WHERE decision_id=? AND kind='route_learning_optout'", (decision_id,)).fetchone():
        return
    graph.db.execute('''INSERT INTO routing_feedback(decision_id,person_id,repo,question,category,path,facts,outcome,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(decision_id,person_id) DO UPDATE SET
        outcome=excluded.outcome, question=excluded.question, facts=excluded.facts, updated_at=excluded.updated_at
        WHERE routing_feedback.outcome<>excluded.outcome OR routing_feedback.question<>excluded.question
           OR routing_feedback.facts<>excluded.facts''',
        (decision_id, person_id, d['repo'], d['question'], d['category'] or '', d['path'] or '',
         d['facts'] or '', outcome, now_iso()))
    graph._bump('')


# A contact hint is reconsidered after six months. This is not approval expiry.
CONTACT_FRESH_DAYS = 180
CONTACT_RESPONSES = ('answered', 'owner_confirmed')


def _version_identity(snapshot):
    """Identity and declaration label from one immutable source version."""
    snapshot = _context_dict(snapshot)
    return {key: snapshot.get(source_key, '') if isinstance(snapshot.get(source_key, ''), str) else ''
            for key, source_key in (('provider', 'provider'), ('namespace', 'namespace'),
                ('object_kind', 'object_kind'), ('external_id', 'external_id'), ('ref', 'display_ref'))}


def _source_context(db, decision):
    """Keep typed task/work-item context as observed, without parsing prose."""
    rows = db.execute('''SELECT b.binding,b.record_id,b.source_version_id,b.role,
            v.snapshot,v.source_updated_at,v.source_sequence,v.observed_at
        FROM (SELECT 'task' AS binding,record_id,source_version_id,role FROM task_source_anchors WHERE task_id=?
              UNION ALL SELECT 'decision' AS binding,record_id,source_version_id,role
              FROM decision_source_edges WHERE decision_id=? AND active=1 AND role IN ('work_item','context')) b
        JOIN source_versions v ON v.id=b.source_version_id AND v.record_id=b.record_id
        ORDER BY b.binding,b.record_id,b.source_version_id,b.role''', (decision['run_id'], decision['id']))
    anchors = []
    for row in rows:
        anchor = dict(row)
        anchor.update(_version_identity(anchor.pop('snapshot')))
        anchors.append(anchor)
    run = db.execute('SELECT facts FROM runs WHERE id=?', (decision['run_id'],)).fetchone()
    return {'task_id': decision['run_id'], 'client_ref': decision['client_ref'] or '',
            'anchors': anchors, 'facts': parse_facts(run['facts'] if run else '')}


def _facts(raw):
    """Only explicit facts: org and organization are the same material key."""
    normalized, conflict = {}, False
    for key, value in parse_facts(raw).items():
        key = 'org' if key == 'organization' else key
        if key in normalized and normalized[key].casefold() != value.casefold():
            conflict = True
        normalized[key] = value
    return normalized, conflict


def routing_context(graph, task_id='', decision_id='', contact_context=None):
    """Read existing typed anchors, or accept canonical identities without IDs."""
    decision = graph.db.execute('SELECT * FROM decisions WHERE id=?', (decision_id,)).fetchone() if decision_id else None
    task_id = decision['run_id'] if decision else task_id
    run = graph.db.execute('SELECT facts FROM runs WHERE id=?', (task_id,)).fetchone() if task_id else None
    inherited, inherited_conflict = _facts(run['facts'] if run else '')
    if decision:
        own_facts, own_conflict = _facts(decision['facts'])
        inherited.update(own_facts)
        inherited_conflict |= own_conflict
    result = _source_context(graph.db, dict(decision) if decision else
                             {'run_id': task_id, 'id': '', 'client_ref': ''})
    supplied = contact_context if isinstance(contact_context, dict) else {}
    anchors = supplied.get('anchors') or []
    result['anchors'] += anchors if isinstance(anchors, list) else []
    result['inherited_facts'] = inherited
    result['facts_conflict'] = inherited_conflict
    result['facts'] = supplied.get('facts') or {}
    return result


def _context_dict(context):
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except (ValueError, TypeError):
            context = {}
    return context if isinstance(context, dict) else {}


def _pinned_context(db, context, versions):
    """Resolve only existing version pins, without rewriting observations.

    Older contact snapshots omitted display refs and could carry the original
    backing-row identity after a canonical alias import. The pinned version,
    never its mutable head or display row, supplies the observed identity.
    """
    context = _context_dict(context)
    anchors, unavailable = [], False
    values = context.get('anchors') or []
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, dict):
            continue
        anchor = dict(value)
        if anchor.get('record_id') or anchor.get('source_version_id'):
            pin = (anchor.get('record_id'), anchor.get('source_version_id'))
            if not all(isinstance(k, str) and k for k in pin):
                anchor.update(_version_identity({}))
            else:
                if pin not in versions:
                    row = db.execute('SELECT snapshot FROM source_versions WHERE record_id=? AND id=?', pin).fetchone()
                    versions[pin] = _version_identity(row['snapshot'] if row else {})
                anchor.update(versions[pin])
            unavailable |= not all(anchor.get(k) for k in ('provider', 'namespace', 'object_kind', 'external_id'))
        anchors.append(anchor)
    return {**context, 'anchors': anchors, 'unavailable_source_pin': unavailable}


def _declared_work_item(db, context, declared, bindings):
    """Bind an exact declaration only to a persisted, pinned task anchor."""
    if not declared:
        return 'missing', None
    identities = set()
    for anchor in context.get('anchors') or []:
        if anchor.get('binding') != 'task' or anchor.get('role') != 'work_item':
            continue
        identity = tuple(anchor.get(k) for k in ('provider', 'namespace', 'object_kind', 'external_id'))
        if (not all(isinstance(k, str) and k for k in identity) or 'legacy' in identity[:2]
                or declared not in (anchor.get('ref'), anchor.get('external_id'))):
            continue
        pin = (context.get('task_id'), anchor.get('record_id'), anchor.get('source_version_id'))
        if not all(isinstance(k, str) and k for k in pin):
            continue
        if pin not in bindings:
            bindings[pin] = bool(db.execute("SELECT 1 FROM task_source_anchors WHERE task_id=? "
                "AND record_id=? AND source_version_id=? AND role='work_item'", pin).fetchone())
        if bindings[pin]:
            identities.add(identity)
    return ('linked', next(iter(identities))) if len(identities) == 1 else (
        'ambiguous' if identities else 'unlinked', None)


def _identities(context):
    """Compare canonical fields only; never parse URLs, titles, or prose."""
    context = _context_dict(context)
    namespaces, identities = {}, {}
    anchors = context.get('anchors') or []
    for item in anchors if isinstance(anchors, list) else []:
        if not isinstance(item, dict):
            continue
        provider, namespace = item.get('provider'), item.get('namespace')
        if not isinstance(provider, str) or not isinstance(namespace, str) or not provider or not namespace:
            continue
        # Legacy is explicitly unnamespaced historical evidence, not a tenant.
        if provider == 'legacy' or namespace == 'legacy':
            continue
        namespaces.setdefault(provider, set()).add(namespace)
        kind, external_id = item.get('object_kind') or item.get('kind'), item.get('external_id')
        if isinstance(kind, str) and isinstance(external_id, str) and kind and external_id:
            identity = (provider, namespace, kind, external_id)
            role = item.get('role')
            identities.setdefault(identity, set()).add(role if isinstance(role, str) and role else 'context')
    return namespaces, identities


def _typed_match(old_context, current, old_facts, facts):
    old_ns, old_ids = _identities(old_context)
    new_ns, new_ids = _identities(current)
    extra, extra_conflict = _facts(current.get('facts'))
    known = {**facts}
    conflict = extra_conflict or any(k in known and known[k].casefold() != v.casefold() for k, v in extra.items())
    known.update(extra)
    material = ('org', 'customer', 'domain')
    conflict |= any(k in old_facts and k in known and old_facts[k].casefold() != known[k].casefold()
                    for k in material)
    reasons, shared, missing = [], [], sorted(set(old_ns) - set(new_ns))
    if conflict:
        reasons.append('material_fact_conflict')
    if any(old_ns[p] != new_ns[p] for p in old_ns.keys() & new_ns.keys()):
        reasons.append('namespace_conflict')
    if reasons:
        return {'conflict': True, 'reasons': reasons, 'missing_source_namespaces': [], 'factor': 0, 'bonus': 0}
    if missing:
        reasons.append('missing_source_namespace: ' + ', '.join(missing))
    missing_facts = sorted(set(old_facts) - set(known))
    missing_material = sorted(set(missing_facts) & set(material))
    other_missing = sorted(set(missing_facts) - set(material))
    if missing_material:
        reasons.append('missing_material_facts: ' + ', '.join(missing_material))
    if other_missing:
        reasons.append('missing_scope_facts: ' + ', '.join(other_missing))
    if missing or missing_facts:
        # A relationship remains audit context until every required scope
        # check passes. Clarification must not imply relevance was credited.
        return {'conflict': False, 'reasons': reasons, 'missing_source_namespaces': missing,
                'missing_facts': missing_facts, 'factor': 0, 'bonus': 0}
    if old_ns:
        reasons.append('compatible_namespace: ' + ', '.join(f"{p}:{n}" for p in sorted(old_ns) for n in sorted(old_ns[p])))
    unknown = not old_ns or bool(set(new_ns) - set(old_ns)) or any(k in known and k not in old_facts for k in material)
    if unknown:
        reasons.append('typed_context_unknown: confirm the unestablished source or material scope')
    bonus = 0
    for identity in sorted(old_ids.keys() & new_ids.keys()):
        kind = 'shared_work_item' if 'work_item' in old_ids[identity] & new_ids[identity] else 'shared_source'
        shared.append(kind + ': ' + ':'.join(identity))
        bonus = max(bonus, .15 if kind == 'shared_work_item' else .08)
    reasons.extend(shared[:3])
    return {'conflict': False, 'reasons': reasons, 'missing_source_namespaces': missing,
            'factor': .65 if unknown else 1, 'bonus': bonus,
            'uncertain_negative': unknown and bool(new_ns or any(k in known and k not in old_facts for k in material))}


def observe(graph, decision, outcome, from_person_id='', to_person_id='', reason='', *, db=None,
            source_revision=None, occurred_at=None, source_context=None, identity_salt=None):
    """Append contact evidence inside its source writer; never authorize an action.

    Revision identity makes callback/replay retries idempotent. No name/prose
    backfill: these identities must come from the committed product boundary.
    """
    db = graph.db if db is None else db
    if outcome not in ('referred', 'declined', *CONTACT_RESPONSES):
        raise ValueError('Unknown contact observation outcome')
    if not to_person_id or (outcome not in CONTACT_RESPONSES and (not from_person_id or from_person_id == to_person_id)):
        return
    revision = source_revision or decision['updated_at']
    identity = [decision['id'], revision, outcome, from_person_id, to_person_id]
    if outcome == 'owner_confirmed':
        # Task context can change without a new decision version. Include the
        # material transition while keeping every genuine version pin intact.
        identity.append(identity_salt)
    key = json.dumps(identity)
    observation_id = hashlib.sha256(key.encode()).hexdigest()
    sequence = db.execute('SELECT COALESCE(MAX(sequence),0)+1 AS n FROM contact_observations WHERE decision_id=?',
                          (decision['id'],)).fetchone()['n']
    snapshot = {key: decision[key] or '' for key in ('repo', 'question', 'category', 'path', 'facts')}
    stamp, observed_at = occurred_at or revision, now_iso()
    source_context = _source_context(db, decision) if source_context is None else source_context
    row = db.execute('''INSERT INTO contact_observations
        (id,decision_id,sequence,from_person_id,to_person_id,outcome,reason,repo,question,category,path,facts,created_at,
         source_revision,observed_at,source_context)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING''',
        (observation_id, decision['id'], sequence, from_person_id, to_person_id, outcome, str(reason or '')[:1000],
         snapshot['repo'], snapshot['question'], snapshot['category'], snapshot['path'], snapshot['facts'], stamp, revision, observed_at, json.dumps(source_context, sort_keys=True)))
    if row.rowcount:
        graph.append_event('contact_observed', {'decision_id': decision['id'], 'task_id': decision['run_id'],
            'observation_id': observation_id, 'sequence': sequence, 'from_person_id': from_person_id,
            'to_person_id': to_person_id, 'outcome': outcome, 'reason': str(reason or '')[:1000],
            'scope': {**snapshot, 'facts': parse_facts(snapshot['facts'])}, 'source_revision': revision,
            'occurred_at': stamp, 'observed_at': observed_at, 'source_context': source_context}, db=db)


def observe_answer(graph, db, decision, basis, actor_id, stamp):
    """Observe the same answering owner the existing learning hook recognizes."""
    owner = db.execute('SELECT person_id FROM owners WHERE id=?', (decision['owner_id'],)).fetchone()
    person_id = owner['person_id'] if owner else ''
    if person_id and basis != 'admin-override' and (not actor_id or actor_id == person_id):
        observe(graph, decision, 'answered', to_person_id=person_id, reason='Recorded human answer',
                db=db, source_revision=stamp)


def observe_owner_confirmation(graph, db, decision_version_id, stamp):
    """Contact-only completion after the ordinary owner sign-off succeeds.

    The caller supplies its already-established owner basis. Freeze the exact
    committed response version, but compare material content and immutable pins
    separately: another signature timestamp is not a new contact response.
    """
    from .context_memory import decision_binding
    version = db.execute('SELECT decision_id,snapshot FROM decision_versions WHERE id=?', (decision_version_id,)).fetchone()
    binding = decision_binding(db, version['decision_id']) if version else None
    if not binding or binding['source_version_id'] != decision_version_id:
        return  # An obsolete callback cannot append a past response again.
    snapshot = json.loads(version['snapshot'])
    decision = snapshot['decision']
    owner = db.execute('SELECT person_id FROM owners WHERE id=?', (decision['owner_id'],)).fetchone()
    if not owner or not owner['person_id'] or db.execute(
            "SELECT 1 FROM events WHERE decision_id=? AND kind='route_learning_optout'", (decision['id'],)).fetchone():
        return
    context = _source_context(db, decision)
    material = {'decision': {key: decision.get(key) for key in (
        'answer', 'rationale', 'answered_by', 'source', 'source_id', 'source_revision', 'kind', 'evidence',
        'repo', 'question', 'context', 'category', 'path', 'scope_paths', 'facts', 'applicability')},
        'sources': sorted([p['record_id'], p['source_version_id'], p['role']] for p in snapshot['sources']),
        'derivations': sorted([p['related_id'], p['kind'], p.get('source_version_id', '')]
                              for p in snapshot['derivations']), 'context': context}
    fingerprint = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
    previous = db.execute("SELECT id,source_context FROM contact_observations WHERE decision_id=? "
        "AND to_person_id=? AND outcome='owner_confirmed' ORDER BY sequence DESC LIMIT 1",
        (decision['id'], owner['person_id'])).fetchone()
    if previous and _context_dict(previous['source_context']).get('confirmation', {}).get('response_fingerprint') == fingerprint:
        return
    previous_id = previous['id'] if previous else ''
    context['confirmation'] = {'decision_version_id': decision_version_id, 'response_fingerprint': fingerprint,
                               'previous_observation_id': previous_id}
    observe(graph, decision, 'owner_confirmed', to_person_id=owner['person_id'],
            reason='Owner confirmed an existing answer', db=db, source_revision=decision_version_id,
            occurred_at=stamp, source_context=context, identity_salt=[fingerprint, previous_id])


def _age(stamp):
    try:
        instant = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        current = datetime.fromisoformat(now_iso().replace('Z', '+00:00'))
        if instant.tzinfo is None or current.tzinfo is None:
            return None
        days = (current.astimezone(timezone.utc) - instant.astimezone(timezone.utc)).total_seconds() / 86400
        return days if 0 <= days <= CONTACT_FRESH_DAYS else None
    except (ValueError, TypeError, AttributeError):
        return None


def _observation_facts(row):
    facts, conflict = _facts(_context_dict(row.get('source_context')).get('facts'))
    own, own_conflict = _facts(row['facts'])
    facts.update(own)
    return facts, conflict or own_conflict


def _same_scope(left, right):
    return (all(left[k] == right[k] for k in ('repo', 'question', 'category', 'path'))
            and _observation_facts(left) == _observation_facts(right)
            and _identities(left.get('source_context'))[0] == _identities(right.get('source_context'))[0])


def _evidence_rows(graph, repo):
    """Current contact evidence, with no reconstruction of historical chains."""
    rows = graph.db.execute('''SELECT o.* FROM contact_observations o JOIN decisions d ON d.id=o.decision_id
        WHERE o.repo=? AND d.needs_review=0 AND NOT EXISTS
        (SELECT 1 FROM events e WHERE e.decision_id=o.decision_id AND e.kind='route_learning_optout')
        ORDER BY o.decision_id,o.sequence''', (repo,)).fetchall()
    chains = {}
    for row in rows:
        chains.setdefault(row['decision_id'], []).append(dict(row))
    for chain in chains.values():
        # An explicit scoped decline is already useful contrary evidence. Its
        # validity does not depend on somebody else eventually answering.
        for edge in chain:
            if edge['outcome'] == 'declined':
                yield {**edge, 'person_id': edge['from_person_id'], 'updated_at': edge['created_at']}
        answers = [i for i, row in enumerate(chain) if row['outcome'] in CONTACT_RESPONSES]
        if not answers:
            continue
        answer_index = answers[-1]
        answer = chain[answer_index]
        if _age(answer['created_at']) is None:
            continue
        yield {**answer, 'person_id': answer['to_person_id'], 'updated_at': answer['created_at']}
        # Work back only through the continuous, same-scope route to this answer.
        person_id, seen, edges = answer['to_person_id'], {answer['to_person_id']}, []
        for edge in reversed(chain[:answer_index]):
            if edge['outcome'] in CONTACT_RESPONSES:
                continue  # A correction does not erase its still-scoped referral path.
            if edge['to_person_id'] != person_id or not _same_scope(edge, answer):
                break
            if edge['from_person_id'] in seen:
                edges = []  # A loop demonstrates no reusable connector path.
                break
            seen.add(edge['from_person_id'])
            edges.append(edge)
            person_id = edge['from_person_id']
        for edge in edges:
            if edge['outcome'] == 'referred':
                yield {**edge, 'person_id': edge['from_person_id'], 'updated_at': edge['created_at'],
                       'outcome': 'connector'}
    # Existing feedback is honest legacy evidence, never a fabricated edge.
    for row in graph.db.execute('''SELECT f.* FROM routing_feedback f JOIN decisions d ON d.id=f.decision_id
        WHERE f.repo=? AND d.needs_review=0 AND NOT EXISTS
        (SELECT 1 FROM events e WHERE e.decision_id=f.decision_id AND e.kind='route_learning_optout')
        AND (NOT EXISTS (SELECT 1 FROM contact_observations o WHERE o.decision_id=f.decision_id)
             OR (f.outcome='answered' AND NOT EXISTS
                 (SELECT 1 FROM contact_observations o WHERE o.decision_id=f.decision_id
                  AND o.outcome IN ('answered','owner_confirmed'))))
        ORDER BY f.updated_at DESC''', (repo,)):
        yield dict(row)


def _matching(graph, repo, question, path, context, category, facts, *, missing_scope=False,
              task_id='', decision_id='', contact_context=None, notes=None):
    from .ladder import _meaningful_terms
    from .scopes import primary_scopes
    terms = set(_meaningful_terms(question))
    if not terms:
        return
    stated = re.search(r'(?im)^facts?:\s*(.+)$', context or '')
    versions, bindings = {}, {}
    current = _pinned_context(graph.db, routing_context(graph, task_id, decision_id, contact_context), versions)
    if current['unavailable_source_pin']:
        if notes is not None:
            notes.append('contact scope: unavailable_source_pin in current context')
        return
    supplied_facts, facts_conflict = _facts(facts if facts is not None else stated[1] if stated else '')
    facts = {**current['inherited_facts'], **supplied_facts}
    if facts_conflict or current['facts_conflict']:
        if notes is not None:
            notes.append('contact scope: material_fact_conflict in current explicit facts')
        return
    topics = set(primary_scopes(question, category))
    for row in _evidence_rows(graph, repo):
        age = _age(row['updated_at'])
        if age is None:
            continue
        prior = set(_meaningful_terms(row['question']))
        overlap = len(terms & prior) / max(1, len(terms | prior))
        if overlap < (.8 if missing_scope else .5):
            continue
        old_facts, old_conflict = _observation_facts(row)
        if old_conflict:
            if notes is not None:
                notes.append(f"contact decision {row['decision_id']}: material_fact_conflict in historical explicit facts")
            continue
        old_context = _pinned_context(graph.db, row.get('source_context'), versions)
        if old_context['unavailable_source_pin']:
            if notes is not None:
                notes.append(f"contact decision {row['decision_id']}: unavailable_source_pin")
            continue
        effective_facts = {**facts, **_facts(current.get('facts'))[0]}
        comparison_facts, declaration_reason = old_facts, ''
        old_item, new_item = old_facts.get('work_item'), effective_facts.get('work_item')
        if old_item and old_item.lower() != (new_item or '').lower():
            old_state, old_identity = _declared_work_item(graph.db, old_context, old_item, bindings)
            new_state, new_identity = _declared_work_item(graph.db, current, new_item, bindings)
            if old_state == new_state == 'linked' and old_identity[:2] == new_identity[:2]:
                # Different explicit work items are relationships, not a
                # material boundary for contact learning. All other facts,
                # namespace, topic and path gates remain mandatory.
                comparison_facts = {k: v for k, v in old_facts.items() if k != 'work_item'}
                declaration_reason = (f'declared_work_item_match: historical {old_item} -> '
                    f'{":".join(old_identity)}; current {new_item} -> {":".join(new_identity)}')
            elif old_state == new_state == 'linked' and notes is not None:
                notes.append(f"contact decision {row['decision_id']}: work_item_namespace_conflict: "
                             f'historical {":".join(old_identity[:2])}; current {":".join(new_identity[:2])}')
            elif notes is not None:
                notes.append(f"contact decision {row['decision_id']}: work_item_declaration: "
                             f'historical={old_state}; current={new_state}')
        typed = _typed_match(old_context, current, comparison_facts, facts)
        if typed['conflict'] or ((typed['missing_source_namespaces'] or typed.get('missing_facts')) and not missing_scope):
            if notes is not None:
                notes.append(f"contact decision {row['decision_id']}: " + '; '.join(typed['reasons']))
            continue
        if row['outcome'] == 'declined' and typed.get('uncertain_negative'):
            if notes is not None:
                notes.append(f"contact decline {row['decision_id']} not transferred: typed_context_unknown")
            continue
        if notes is not None and typed['factor'] < 1:
            notes.append(f"contact decision {row['decision_id']}: typed_context_unknown; reduced confidence")
        conflicting = [k for k, v in comparison_facts.items()
                       if (k in effective_facts or not missing_scope) and effective_facts.get(k, '').lower() != v.lower()]
        if conflicting:
            if notes is not None:
                notes.append(f"contact decision {row['decision_id']}: scope_fact_conflict: " + ', '.join(sorted(conflicting)))
            continue
        if declaration_reason:
            typed['reasons'].append(declaration_reason)
        old_topics = set(primary_scopes(row['question'], row['category']))
        if topics and old_topics and not topics & old_topics:
            continue
        if path not in ('', 'unknown') and row['path'] not in ('', 'unknown') and path != row['path']:
            continue
        person = graph.get_person(row['person_id'])
        if not person or not person['active'] or person['role'] == 'viewer':
            continue
        # A modest linear decay orders equally similar recent contacts without
        # accumulating rewards for retries or repeated answers.
        score = (overlap + typed['bonus']) * (1 - .25 * age / CONTACT_FRESH_DAYS) * typed['factor']
        row['_context_match'] = typed
        yield person, row, score, {k: v for k, v in old_facts.items() if k not in effective_facts}


def candidates(graph, repo, question, path='', context='', category='', facts=None, *,
               task_id='', decision_id='', contact_context=None, details=None, notes=None):
    matches = list(_matching(graph, repo, question, path, context, category, facts,
                            task_id=task_id, decision_id=decision_id, contact_context=contact_context, notes=notes))
    # A newer matching contrary observation wins, even if the old answer had
    # a closer wording match. Source sequence orders a chain's tied times.
    latest = {}
    for person, row, score, _ in sorted(matches, key=lambda r: (
            datetime.fromisoformat(r[1]['updated_at'].replace('Z', '+00:00')),
            r[1]['decision_id'], r[1].get('sequence', 0)), reverse=True):
        if person['id'] not in latest:
            latest[person['id']] = (person, row['outcome'], score, row['decision_id'], row['updated_at'])
            if details is not None:
                details[(person['id'], row['outcome'], row['decision_id'])] = {
                    **row['_context_match'], 'observation_id': row.get('id', '')}
    order = {'answered': 0, 'owner_confirmed': 0, 'connector': 1, 'declined': 2}
    return sorted(latest.values(), key=lambda r: (order.get(r[1], 3), -r[2], r[0]['name'], r[0]['id']))


def contact_evidence(graph, person, outcome, decision_id, detail=None):
    structured = graph.db.execute("SELECT 1 FROM contact_observations WHERE decision_id=? "
                                  "AND outcome='answered' AND to_person_id=? LIMIT 1",
                                  (decision_id, person['id'])).fetchone()
    suffix = ('; contact scope: ' + '; '.join(detail['reasons']) +
              ('; observation ' + detail['observation_id'] if detail.get('observation_id') else '')) if detail else ''
    if outcome == 'connector':
        return (f"learned helpful connector: {person['name']} referred along the completed chain for decision "
                f"{decision_id}; ask for a referral, not presumed expertise or authority" + suffix)
    if outcome == 'owner_confirmed':
        return (f"learned first contact: {person['name']} confirmed an existing answer for decision {decision_id}; "
                'owner confirmation, not answer authorship or permanent authority' + suffix)
    prefix = 'learned first contact' if structured else 'legacy learned first contact'
    return (f"{prefix}: {person['name']} answered decision {decision_id}; "
            'confirm or refer, not permanent authority' + suffix)


def request_key(question, client_ref='', context='', paths=()):
    return client_ref or hashlib.sha256(json.dumps([question.lower(), context, list(paths)],
                                                  sort_keys=True).encode()).hexdigest()[:24]


def clarify(graph, task_id, repo, question, key, path='', category='', facts=None, contact_context=None):
    """Ask before crossing an unstated boundary of learned human routing.

    These historical values are possibilities, never facts inherited by a
    new task. Nothing is sent until the host supplies its actual scope.
    """
    from .store import Invalid
    previous = graph.db.execute("SELECT question FROM scope_clarifications WHERE task_id=? AND request_key=?",
                                (task_id, key)).fetchone()
    if previous and previous['question'] != question:
        raise Invalid('This client_ref already names a different scope clarification')
    facts = parse_facts(facts)
    if any(outcome in CONTACT_RESPONSES for _, outcome, *_ in candidates(
            graph, repo, question, path=path, category=category, facts=facts, task_id=task_id,
            contact_context=contact_context)):
        return None
    suggestions, seen = [], set()
    for person, row, _, missing in _matching(graph, repo, question, path, '', category, facts, missing_scope=True,
                                           task_id=task_id, contact_context=contact_context):
        missing_namespaces = row['_context_match']['missing_source_namespaces']
        if row['outcome'] not in CONTACT_RESPONSES or not (missing or missing_namespaces) or row['decision_id'] in seen:
            continue
        suggestions.append({'decision_id': row['decision_id'], 'prior_contact': person['name'],
                            'source_facts': parse_facts(row['facts']), 'missing_keys': sorted(missing),
                            'missing_source_namespaces': missing_namespaces,
                            'source_context': _context_dict(row.get('source_context')),
                            'match_reasons': row['_context_match']['reasons']})
        seen.add(row['decision_id'])
        if len(suggestions) == 5:
            break
    if not suggestions:
        return None
    result = {'task_id': task_id, 'status': 'needs_scope_clarification', 'authorized': False,
              'blocking': True, 'request_key': key, 'question': question, 'scope_clarifications': suggestions,
              'next': 'Earlier answers and learned contacts depend on unstated scope. Confirm the actual missing '
                      'facts and source namespaces from this task or its requester, then retry bridge_add_node with the same client_ref '
                      'and explicit facts or current task source anchors. Do not copy historical values as current truth. No question was sent.'}
    with graph.transaction():
        graph.db.execute('''INSERT INTO scope_clarifications(task_id,request_key,question,detail,created_at)
            VALUES(?,?,?,?,?) ON CONFLICT(task_id,request_key) DO UPDATE SET detail=excluded.detail''',
            (task_id, key, question, json.dumps(result), now_iso()))
    return result


def pending_scopes(graph, task_id):
    return [json.loads(row['detail']) for row in graph.db.execute(
        'SELECT detail FROM scope_clarifications WHERE task_id=? ORDER BY created_at,request_key', (task_id,))]


def resolve_scope(graph, task_id, key):
    graph.db.execute('DELETE FROM scope_clarifications WHERE task_id=? AND request_key=?', (task_id, key))
