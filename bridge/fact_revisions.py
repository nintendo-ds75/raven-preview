"""Explicit, optimistic replacement of an unsigned node's current facts.

No historical fact, signature or grant is copied into the replacement. All
retrieval, publication, audit and dependency invalidation share one writer.
"""
import json
import re
from datetime import datetime, timedelta

from .config import Config
from .graph import parse_facts
from .store import Invalid, field, now, repo_key


class _LocalRecheck(Config):
    """Corrections use stored records only, regardless of deployment flags."""
    @property
    def live_retrieval(self):
        return False


def revision_tokens(db, rows):
    """Bind material and existing version/event generations in bounded reads.

    Tree callers pass the rows already shown, preserving their single read of
    decisions. A concurrent publication can only make their old token stale.
    """
    from collections import defaultdict
    from . import context_memory as cm
    from .approval_scope import revision
    rows = list(rows)
    events, versions, pins, links = (defaultdict(list) for _ in range(4))
    ids = [r['id'] for r in rows]
    for offset in range(0, len(ids), 400):
        chunk = ids[offset:offset + 400]
        marks = ','.join('?' for _ in chunk)
        for item in db.execute(f'SELECT decision_id,id,kind FROM events WHERE decision_id IN ({marks}) ORDER BY id', chunk):
            if not item['kind'].startswith('notification_'):
                events[item['decision_id']].append({'id': item['id'], 'kind': item['kind']})
        for item in db.execute(f'SELECT decision_id,id,fingerprint FROM decision_versions WHERE decision_id IN ({marks}) ORDER BY sequence', chunk):
            versions[item['decision_id']].append({'id': item['id'], 'fingerprint': item['fingerprint']})
        for item in db.execute('SELECT e.decision_id,e.record_id,e.source_version_id,e.role,e.stale,s.head_id,s.availability '
                'FROM decision_source_edges e JOIN source_records s ON s.id=e.record_id '
                f'WHERE e.decision_id IN ({marks}) AND e.active=1 ORDER BY e.record_id,e.role,e.source_version_id', chunk):
            pins[item['decision_id']].append({k: item[k] for k in
                ('record_id', 'source_version_id', 'role', 'stale', 'head_id', 'availability')})
        for item in db.execute('SELECT decision_id,related_id,kind,source_version_id FROM decision_links '
                f'WHERE decision_id IN ({marks}) ORDER BY related_id,kind', chunk):
            links[item['decision_id']].append({k: item[k] for k in ('related_id', 'kind', 'source_version_id')})
    result = {}
    for row in rows:
        node_id = row['id']
        content = {k: v for k, v in dict(row).items()
                   if k not in ('embedding', 'search_vector', 'source_reuse_uncertain', 'owner_name',
                                'owner_team', 'run_title', 'agent', 'task_status')}
        content['events'] = events[node_id]
        result[node_id] = 'facts:' + cm.digest({'content': content, 'review': revision(content),
            'versions': versions[node_id], 'sources': pins[node_id], 'dependencies': links[node_id]})
    return result


def revision_token(db, node_id, row=None):
    row = row if row is not None else db.execute('SELECT * FROM decisions WHERE id=?', (node_id,)).fetchone()
    if row is None:
        raise Invalid('Decision not found')
    return revision_tokens(db, [row])[node_id]


def repeated_node(store, node_id, supplied, effective_facts):
    """An add retry remains a read; changed explicit facts are never applied."""
    from .canvas import node_view
    view = node_view(store, node_id, repeated=True)
    if 'facts' in supplied and parse_facts(view['facts']) != effective_facts:
        view['facts_applied'] = False
        view['facts_correction'] = {
            'required': True, 'tool': 'bridge_correct_node_facts',
            'expected_revision': view['fact_revision'], 'requested_facts': effective_facts,
            'notice': 'These changed facts were not applied. Read the current node, then call '
                      'bridge_correct_node_facts with a complete facts snapshot, this '
                      'expected_revision, a new correction_ref, and a reason. Signed or '
                      'rule-authorized nodes require a person to correct them in the inbox; '
                      'completed tasks require a new task.'}
        view['next'] = view['facts_correction']['notice'] + ' ' + view['next']
    return view


def _has_recorded_interaction(db, row):
    """Use durable accepted associations only; never expose private content."""
    node_id = row['id']
    if db.execute("SELECT 1 FROM slack_conversation WHERE decision_id=? AND role='user' LIMIT 1", (node_id,)).fetchone():
        return True
    if db.execute('SELECT 1 FROM reply_readings WHERE decision_id=? LIMIT 1', (node_id,)).fetchone():
        return True
    if db.execute("SELECT 1 FROM reply_turns t JOIN notifications n "
                  "ON n.channel=t.platform AND n.external_ref=t.channel || ':' || t.thread_ts "
                  'WHERE n.decision_id=? LIMIT 1', (node_id,)).fetchone():
        return True
    # Explicit forwarded context is attached to the task, with its original
    # decision association carried by internal, verified reply provenance.
    for event in db.execute("SELECT detail FROM events WHERE run_id=? AND kind='task_note'", (row['run_id'],)):
        detail = json.loads(event['detail'])
        if detail.get('actor_id') and (detail.get('reply_source') or {}).get('decision_id') == node_id:
            return True
    return False


def _retained_scope_problem(graph, row, facts, repo):
    """Fresh source versions must also apply to the replacement facts.

    Traverse actual typed premises, including nested derivations. Source text
    and historical facts never fill missing facts on the corrected request.
    """
    from . import context_memory as cm
    from .graph import applicability_status, rule_status
    from .ladder import _scope_difference
    db = graph.db
    seen, queue = {row['id']}, [row]
    while queue:
        current = queue.pop()
        ids = [r['related_id'] for r in db.execute("SELECT related_id FROM decision_links "
               "WHERE decision_id=? AND kind IN ('derived','depends')", (current['id'],))]
        relied = cm._relied_on_source(db, current)
        if relied:
            ids.append(relied)
        if current['parent_id'] and not current['independent_source_replacement'] and not current['historical_parent_revalidated']:
            ids.append(current['parent_id'])
        for source_id in ids:
            if source_id in seen:
                continue
            seen.add(source_id)
            source = graph.get_decision(source_id, exact=True)
            source_row = db.execute('SELECT * FROM decisions WHERE id=?', (source_id,)).fetchone()
            if source is None or source_row is None:
                return 'A retained source decision is unavailable; a person must review the current premises'
            applies, why = applicability_status(source, row['path'], facts, repo=repo)
            if not applies:
                return f'Retained premise {source_id} does not apply to the corrected facts: {why}'
            rule_applies = False
            if source.reusable or source.rule_conditions:
                rule_applies, why = rule_status(source, row['question'], row['context'], facts, repo=repo)
                if not rule_applies:
                    return (f'Retained premise {source_id} does not cover the corrected facts: '
                            + (why or 'its standing grant is no longer applicable'))
            if not (rule_applies and source.rule_scope == 'any'):
                difference, _ = _scope_difference(row['question'], row['context'], row['path'], source,
                                                  facts, parse_facts(source_row['facts']), repo)
                if difference:
                    return f'Retained premise {source_id} has unresolved scope after the correction: {difference}'
            queue.append(source_row)
    return ''


def _retained_external_problem(graph, retained, rule_id):
    """Require exact approved-chain coverage, never applicability from prose."""
    from . import context_memory as cm
    from .graph import authorized
    required = {(p['record_id'], p['source_version_id'], p['role'])
                for p in retained if p['role'] in cm.RELIANCE}
    if not required:
        return ''
    db, covered, seen, queue = graph.db, set(), set(), [rule_id]
    while queue:
        source_id = queue.pop()
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        source = db.execute('SELECT * FROM decisions WHERE id=?', (source_id,)).fetchone()
        if source is None or not authorized(source):
            continue
        try:
            cm.check_current(db, source_id)
            cm.validate_derivations(db, source_id)
        except Invalid:
            continue
        covered.update((p['record_id'], p['source_version_id'], p['role'])
                       for p in cm.edges(db, source_id)
                       if p['role'] in cm.RELIANCE and not p['stale'])
        # Only immutable version-pinned decision premises prove a chain.
        # A legacy source pointer or prose citation never supplies a pin.
        queue.extend(link['related_id'] for link in db.execute(
            "SELECT related_id FROM decision_links WHERE decision_id=? "
            "AND kind IN ('derived','depends') AND source_version_id<>''", (source_id,)))
    if required - covered:
        return ('Retained supporting or contradicting records are not completely covered by the selected '
                "rule's exact approved source chain; a person must review the complete current sources "
                'before this fact correction can authorize work')
    return ''


def _eligible(db, row, run):
    if run['status'] in ('completed', 'abandoned'):
        raise Invalid('Fact correction requires an active task; start a new task with bridge_start_task '
                      'and preserve the completed decision history')
    if (row['draft'] or row['origin'] != 'agent'
            or row['status'] not in ('pending', 'resolved', 'partial', 'assumed', 'proposed')
            or row['signoff'] in ('signed', 'rule') or row['signed_by'] or row['signed_hash']
            or row['signed_revision'] or json.loads(row['signatures'] or '[]') or row['reusable']):
        raise Invalid('Only unsigned agent-origin nodes can have facts corrected; ask a person to '
                      'correct this decision in the inbox. Existing signatures and standing authority are preserved')
    # A revoked approval remains historical authority, not an unsigned
    # proposal the agent may rewrite. Keep those corrections human-owned.
    for version in db.execute('SELECT snapshot FROM decision_versions WHERE decision_id=?', (row['id'],)):
        previous = json.loads(version['snapshot']).get('decision', {})
        if (previous.get('status') == 'approved' or previous.get('signoff') in ('signed', 'rule')
                or previous.get('signed_by') or previous.get('signed_hash')
                or previous.get('signed_revision') or json.loads(previous.get('signatures') or '[]')):
            raise Invalid('This node has historical signatures or standing authority; request a correction in the inbox')
    for event in db.execute("SELECT detail FROM events WHERE decision_id=? AND kind='dependent_flagged'", (row['id'],)):
        previous = json.loads(event['detail']).get('previous_authorization', {})
        if previous.get('status') == 'approved' or previous.get('signoff') in ('signed', 'rule'):
            raise Invalid('This node previously had standing authority; request a correction in the inbox')
    if _has_recorded_interaction(db, row):
        raise Invalid('This node is not eligible for agent fact correction; use the inbox correction workflow')
    actions = ('owner_approved', 'answer_corrected', 'signoff', 'signature', 'owner_changed',
               'followup_added', 'reply_received', 'rule_made', 'rule_ended', 'question_reframed')
    if db.execute('SELECT 1 FROM events WHERE decision_id=? AND kind IN ('
                  + ','.join('?' for _ in actions) + ') LIMIT 1', (row['id'], *actions)).fetchone():
        raise Invalid('A person has already acted on this node; request a correction in the inbox '
                      'so their decision history and authority remain intact')


def _receipt(db, task_id, correction_ref):
    for row in db.execute("SELECT detail FROM events WHERE run_id=? AND kind='node_facts_corrected' ORDER BY id",
                          (task_id,)):
        detail = json.loads(row['detail'])
        if detail.get('correction_ref') == correction_ref:
            return detail
    return None


_REFRESH_NOTICE = 'External evidence needs a successful refresh before this answer can authorize work.'


def _refresh_required(db, node_id):
    """Use the existing live source guard without interpreting its internals."""
    from .context_connectors import blocked_decisions
    row = db.execute('SELECT repo FROM decisions WHERE id=?', (node_id,)).fetchone()
    if row is None:
        raise Invalid('Decision not found')
    return node_id in blocked_decisions(db, row['repo'])


def _response(store, node_id, receipt, repeated=False):
    from .canvas import node_view
    # Serialize the current row view and its source guard on one selected
    # writer. Replay never returns the original node as live authorization.
    with store.graph.transaction() as db:
        view = node_view(store, node_id)
        refresh = _refresh_required(db, view.get('duplicate_of') or node_id)
        view['source_refresh_required'] = refresh
        if refresh:
            view['authorized'] = False
            view['blocking'] = True
            view['approval_pending'] = True
            view['source_notice'] = _REFRESH_NOTICE
            view['next'] = _REFRESH_NOTICE + ' Check bridge_connection_status.'
        applied_refresh = receipt.get('published_source_refresh_required')
        view['facts_correction'] = {
            'applied': True, 'repeated': repeated, 'correction_ref': receipt['correction_ref'],
            'before_version_id': receipt['before_version_id'], 'after_version_id': receipt['after_version_id'],
            'applied_updated_at': receipt['after_updated_at'],
            'applied_signoff': receipt['published_signoff'],
            'applied_authorized': receipt['published_authorized'],
            'applied_needs_review': receipt['published_needs_review'],
            'applied_source_refresh_required': applied_refresh,
            'is_current': view['fact_revision'] == receipt.get('after_revision') and refresh == applied_refresh,
            'notice': 'Applied fields and version IDs describe the committed correction. The node fields show current facts and guarded authority; is_current also accounts for the current source-refresh requirement.'}
        if repeated:
            view['repeated'] = True
        return view


def _retain_pins(old, new):
    pins = {}
    for edge in old + new:
        key = (edge['record_id'], edge['source_version_id'], edge['role'])
        stale = bool(edge.get('stale') or pins.get(key, {}).get('stale'))
        pins[key] = {k: edge[k] for k in ('record_id', 'source_version_id', 'role')}
        # A new retrieval cannot clear a retained stale premise.
        pins[key]['stale'] = stale
    return list(pins.values())


def _replacement_facts(raw):
    """Reject malformed/truncated replacements instead of silently clearing."""
    message = 'facts must be complete key=value pairs or a JSON object; use empty text to clear all business facts'
    if isinstance(raw, str):
        if len(raw) > 12000:
            raise Invalid('facts must be at most 12000 characters')
        text = raw.strip()
        if text.startswith('{'):
            try:
                raw = json.loads(text)
            except ValueError as error:
                raise Invalid(message) from error
        else:
            parts = [part.strip() for part in re.split(r'[,;\n]+', text) if part.strip()]
            if any('=' not in part for part in parts):
                raise Invalid(message)
            raw = [part.split('=', 1) for part in parts]
    elif not isinstance(raw, dict):
        raise Invalid(message)
    items = list(raw.items()) if isinstance(raw, dict) else raw
    if not isinstance(items, list) or len(items) > 50:
        raise Invalid('facts accepts at most 50 complete facts')
    result = {}
    for key, value in items:
        if not isinstance(key, str) or not isinstance(value, str):
            raise Invalid(message)
        key, value = key.strip().lower(), ' '.join(value.split())
        if not key or not value or len(key) > 60 or len(value) > 200 or key in result:
            raise Invalid('facts keys must be unique, nonempty, at most 60 characters; values must be nonempty text of at most 200 characters')
        result[key] = value
    return result


def correct_node_facts(store, data):
    from . import canvas, context_memory as cm
    from .graph import condition_facts
    from .ladder import run_task
    task_id = field(data, 'task_id', limit=100)
    node_id = field(data, 'node_id', limit=100)
    expected = field(data, 'expected_revision', limit=100)
    correction_ref = field(data, 'correction_ref', limit=200)
    reason = field(data, 'reason', limit=2000)
    if 'facts' not in data:
        raise Invalid('facts is required: supply the complete replacement snapshot; empty text clears all business facts')
    facts = _replacement_facts(data['facts'])
    request = {'node_id': node_id, 'expected_revision': expected, 'facts': facts, 'reason': reason}
    graph = store.graph
    with graph.transaction() as db:
        run = canvas._task(store, task_id)
        row = db.execute('SELECT * FROM decisions WHERE id=? AND run_id=?', (node_id, task_id)).fetchone()
        if row is None:
            raise Invalid('node_id must name a node of this task')
        previous = _receipt(db, task_id, correction_ref)
        if previous:
            if previous['request'] != request:
                raise Invalid('correction_ref already names a different correction; read the current node '
                              'and use a new correction_ref for a new change')
            return _response(store, node_id, previous, repeated=True)
        _eligible(db, row, run)
        if revision_token(db, node_id) != expected:
            raise Invalid('Fact correction was not applied: expected_revision is stale; read '
                          'bridge_get_decision and review the current facts before retrying')
        repo = graph.resolve_repo(repo_key(run['repo']))
        _, conflict = condition_facts(facts, repo)
        if conflict:
            raise Invalid('Fact correction was not applied: ' + conflict)
        before_version = cm.snapshot_decision(db, node_id, reason='before-fact-correction')
        old_pins = cm.edges(db, node_id)
        dependency_problem = ''
        try:
            cm.check_current(db, node_id)
            cm.validate_derivations(db, node_id)
        except Invalid as error:
            dependency_problem = str(error)
        dependency_problem = dependency_problem or _retained_scope_problem(graph, row, facts, repo)
        old_source = cm._relied_on_source(db, row)
        # Retain every actual premise, including legacy unknown provenance.
        # Do not turn an unused routing hint into a supporting source.
        if old_source:
            pinned = db.execute("SELECT source_version_id FROM decision_links WHERE decision_id=? AND related_id=? AND kind='derived'", (node_id, old_source)).fetchone()
            if pinned is None or not pinned['source_version_id']:
                dependency_problem = dependency_problem or ('An existing source decision has no immutable version pin; '
                    'a person must review its current source before the correction can authorize work')
            graph.add_link(node_id, old_source, 'derived', 'Retained dependency from before the fact correction')
        scope_paths = canvas._json_list(row['scope_paths'])
        paths = [row['path']] if row['path'] and row['path'] != 'unknown' else []
        context = row['context']
        # Scope prose remains historical context. Only the new structured facts
        # are passed to condition checks; task facts are deliberately not merged.
        excluded = getattr(graph._local, 'model_exclude', '')
        graph._local.model_exclude = node_id
        try:
            with graph.source_scope(task_id):
                result = run_task(graph, _LocalRecheck(model_api='none', deterministic=True),
                    run['title'], repo=repo, run_id=task_id, keep_draft=True, scratch=True,
                    decisions=[{'question': row['question'], 'category': row['category'],
                        'context': context, 'path': paths[0] if paths else 'unknown',
                        'requester': run['requester'], 'facts': facts,
                        'hints': scope_paths, 'also_paths': scope_paths[1:]}])
        finally:
            graph._local.model_exclude = excluded
        if not result.drafts:
            raise Invalid('The fact correction produced no decision; no changes were applied')
        scratch = result.drafts[0]
        outcome = dict(db.execute('SELECT * FROM decisions WHERE id=?', (scratch,)).fetchone())
        new_pins = cm.edges(db, scratch)
        cm.validate(db, node_id, [cm.pin(p, p['role']) for p in new_pins])
        cm.validate_derivations(db, scratch)
        db.execute('INSERT OR IGNORE INTO decision_links(decision_id,related_id,kind,note,created_at,source_version_id) '
                   'SELECT ?,related_id,kind,note,created_at,source_version_id FROM decision_links WHERE decision_id=?',
                   (node_id, scratch))
        if outcome['status'] == 'duplicate':
            # Keep this as its own unsigned node. A duplicate's live view must
            # never transfer another node's authority across a fact correction.
            if outcome['superseded_by']:
                graph.add_link(node_id, outcome['superseded_by'], 'related', 'Similar open decision after fact correction')
            outcome.update(status='pending', superseded_by='', answer=None, prediction=None,
                           signoff='', signed_by='', source_id=None, source_revision='')
        required = canvas._json_list(row['required_signers'])
        try:
            current_required = canvas._approvers(graph, repo, row['question'], context,
                                                 row['category'], scope_paths or paths)
            required = list(dict.fromkeys(required + current_required))
        except Exception as error:
            dependency_problem = dependency_problem or (
                f'Raven could not refresh the current required approvers ({type(error).__name__}); '
                'a person must review the authority map before relying on this correction')
        # Refresh through the ordinary publication helper without removing any
        # previously required approver on this unchanged question and scope.
        db.execute('UPDATE decisions SET required_signers=? WHERE id=?', (json.dumps(required), node_id))
        if outcome['status'] in ('resolved', 'partial', 'assumed', 'proposed') and outcome['signoff'] != 'rule':
            outcome['signoff'] = 'required'
        if outcome['signoff'] == 'rule' and canvas._not_behind_rule(
                graph, outcome['source_id'] or '', required):
            outcome['signoff'] = 'required'
            outcome['evidence'] += '; the standing grant does not cover every required approver; fresh sign-off required'
        if outcome['signoff'] == 'rule':
            dependency_problem = dependency_problem or _retained_external_problem(graph, old_pins, outcome['source_id'])
        if outcome['signoff'] != 'rule':
            outcome['signed_by'] = ''
        # Human identity, required approvers, signatures and scope stay intact.
        fields = ['status', 'answer', 'rationale', 'source', 'source_id', 'source_revision',
                  'evidence', 'kind', 'prediction', 'answered_by', 'signoff', 'signed_by', 'source_reuse_state']
        timestamp = now()
        if timestamp <= row['updated_at']:
            timestamp = (datetime.fromisoformat(row['updated_at']) + timedelta(microseconds=1)).isoformat()
        db.execute('UPDATE decisions SET ' + ','.join(f + '=?' for f in fields)
                   + ",facts=?,model_pending=0,brief='',updated_at=? WHERE id=? AND updated_at=?",
                   (*[outcome[f] for f in fields], json.dumps(facts, sort_keys=True), timestamp, node_id, row['updated_at']))
        retained = _retain_pins(old_pins, new_pins)
        cm.snapshot_decision(db, node_id, retained, reason='fact-correction')
        if dependency_problem:
            db.execute('UPDATE decisions SET needs_review=1,review_reason=? WHERE id=?',
                       (row['review_reason'] or dependency_problem, node_id))
        # Old pins, review flags and decision dependencies remain binding even
        # when a newly found standing grant otherwise matches the new facts.
        if outcome['signoff'] == 'rule':
            try:
                if row['needs_review'] or dependency_problem or any(p.get('stale') and p['role'] in cm.RELIANCE for p in retained):
                    raise Invalid(dependency_problem or row['review_reason'] or 'Retained source evidence requires a person to review it')
                cm.check_current(db, node_id)
                cm.validate_derivations(db, node_id)
            except Invalid as error:
                db.execute("UPDATE decisions SET signoff='required',signed_by='',needs_review=1,review_reason=? WHERE id=?",
                           (str(error), node_id))
        graph.flag_dependents(node_id, f'Facts were explicitly corrected on decision {node_id}')
        for sid in result.drafts:
            db.execute('DELETE FROM decision_links WHERE decision_id=? OR related_id=?', (sid, sid))
            db.execute('DELETE FROM events WHERE decision_id=?', (sid,))
            db.execute('DELETE FROM decisions WHERE id=?', (sid,))
        if db.execute('SELECT 1 FROM decisions WHERE draft=1 LIMIT 1').fetchone() is None:
            graph.set_setting('drafts_pending', '')
        after_version = cm.snapshot_decision(db, node_id, reason='fact-correction-published')
        published = db.execute('SELECT * FROM decisions WHERE id=?', (node_id,)).fetchone()
        from .graph import authorized
        publication_refresh = _refresh_required(db, node_id)
        receipt = {'task_id': task_id, 'decision_id': node_id, 'correction_ref': correction_ref,
                   'request': request, 'before_facts': parse_facts(row['facts']), 'after_facts': facts,
                   'before_version_id': before_version, 'after_version_id': after_version,
                   'before_updated_at': row['updated_at'], 'after_updated_at': timestamp,
                   'reason': reason, 'reevaluated_signoff': outcome['signoff'],
                   'published_signoff': published['signoff'],
                   'published_authorized': authorized(published) and not publication_refresh,
                   'published_source_refresh_required': publication_refresh,
                   'published_needs_review': bool(published['needs_review'])}
        graph.append_event('node_facts_corrected', receipt)
        db.execute('UPDATE runs SET updated_at=? WHERE id=?', (timestamp, task_id))
        graph._bump(repo)
        # Old queued prompts describe obsolete scope. Queue only the published
        # revision through the ordinary delivery path; no provider is invoked.
        db.execute("UPDATE notifications SET state='superseded' WHERE decision_id=? AND state IN ('queued','failed')", (node_id,))
        current = db.execute('SELECT status,signoff FROM decisions WHERE id=?', (node_id,)).fetchone()
        if current['status'] == 'pending':
            store.notify(node_id, 'ask')
        elif current['signoff'] == 'required':
            store.notify(node_id, 'signoff')
            for name in required:
                store.notify(node_id, 'signoff', to=name)
        receipt['after_revision'] = revision_token(db, node_id)
        # Store the resulting token alongside the request; the token hashes event
        # identity and kind, never this payload, so it remains stable.
        event = db.execute("SELECT id FROM events WHERE run_id=? AND decision_id=? AND kind='node_facts_corrected' ORDER BY id DESC LIMIT 1", (task_id, node_id)).fetchone()
        db.execute('UPDATE events SET detail=? WHERE id=?', (json.dumps(receipt, sort_keys=True), event['id']))
    return _response(store, node_id, receipt)
