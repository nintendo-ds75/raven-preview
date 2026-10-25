"""Durable outcome ownership around (never instead of) Raven's judgment core.

A completed coding run is progress, not an achieved business outcome. Proposals
are untrusted evidence until an ordinary, current core decision is signed.
"""
import hashlib
import json
import uuid
from datetime import datetime, timezone

from . import context_memory as cm
from .store import Invalid, field, now, repo_key

SCHEMA = '''
CREATE TABLE IF NOT EXISTS objectives (
 id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL,
 title TEXT NOT NULL, outcome TEXT NOT NULL, success_criteria TEXT NOT NULL,
 stop_criteria TEXT NOT NULL, guardrails TEXT NOT NULL, repository TEXT NOT NULL,
 provider TEXT NOT NULL, requester_id TEXT NOT NULL, requester TEXT NOT NULL, requester_kind TEXT NOT NULL DEFAULT 'unknown',
 lifecycle TEXT NOT NULL DEFAULT 'active', revision INTEGER NOT NULL DEFAULT 1,
 next_check_at TEXT NOT NULL DEFAULT '', waiting_on TEXT NOT NULL DEFAULT '',
 blocked_reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS objective_sources (
 objective_id TEXT NOT NULL REFERENCES objectives(id), record_id TEXT NOT NULL,
 source_version_id TEXT NOT NULL, PRIMARY KEY(objective_id,record_id,source_version_id));
CREATE TABLE IF NOT EXISTS objective_metadata_dependencies (
 objective_id TEXT NOT NULL REFERENCES objectives(id), record_id TEXT NOT NULL,
 source_version_id TEXT NOT NULL, metadata_sha256 TEXT NOT NULL,
 PRIMARY KEY(objective_id,record_id,source_version_id,metadata_sha256));
CREATE TABLE IF NOT EXISTS objective_runs (
 objective_id TEXT NOT NULL REFERENCES objectives(id), task_id TEXT NOT NULL,
 purpose TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(objective_id,task_id));
CREATE TABLE IF NOT EXISTS objective_run_authority (
 task_id TEXT PRIMARY KEY, objective_id TEXT NOT NULL, proposal_id TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS objective_events (
 id TEXT PRIMARY KEY, objective_id TEXT NOT NULL REFERENCES objectives(id),
 kind TEXT NOT NULL, summary TEXT NOT NULL, actor_id TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS objective_requests (
 request_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS objective_proposals (
 id TEXT PRIMARY KEY, objective_id TEXT NOT NULL DEFAULT '', repository TEXT NOT NULL,
 fingerprint TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, sources TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'proposed', revision INTEGER NOT NULL DEFAULT 1,
 governance_run_id TEXT NOT NULL DEFAULT '', decision_id TEXT NOT NULL DEFAULT '',
 acceptance_hash TEXT NOT NULL DEFAULT '', scope_hash TEXT NOT NULL DEFAULT '',
 rationale TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
'''


def migrate(db):
    db.executescript(SCHEMA)
    columns = {r['name'] for r in db.execute('PRAGMA table_info(objectives)')}
    if 'requester_kind' not in columns:
        db.execute("ALTER TABLE objectives ADD COLUMN requester_kind TEXT NOT NULL DEFAULT 'unknown'")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def human(actor):
    if actor is None or actor.kind == 'agent' or not actor.allows('member'):
        raise Invalid('A signed-in workspace member must request objective work')


def current_actor(store, actor, role='member', *, allow_operator=True):
    """Resolve current membership, never recreate privilege from saved names."""
    from .auth import Identity
    if actor.id:
        person = store.graph.db.execute('SELECT id,name,role,active FROM people WHERE id=?', (actor.id,)).fetchone()
        if not person or not person['active']:
            raise Invalid('The requester is no longer an active workspace member')
        current = Identity(person['id'], person['name'], person['role'], 'session')
    elif allow_operator and actor.kind == 'operator':
        current = Identity('', 'Local operator', 'admin', 'operator')
    else:
        raise Invalid('A current named workspace identity is required for background work')
    if not current.allows(role):
        raise Invalid('The requester no longer has the required workspace role')
    return current


def row(store, ident):
    result = store.graph.db.execute('SELECT * FROM objectives WHERE id=?', (ident,)).fetchone()
    if result is None:
        raise Invalid('Objective not found')
    return dict(result)


def _metadata_changed(source, current, historical=None):
    from .objective_research import metadata_fingerprint
    expected = source.get('metadata_sha256')
    if not expected and historical:
        expected = metadata_fingerprint(historical)
    return not expected or metadata_fingerprint(current) != expected


def sources_current_db(db, sources, repo, *, exact=False):
    """Read-only checks safe inside the core's selected writer transaction."""
    from .context_connectors import fresh
    out = []
    for source in sources:
        record = db.execute('SELECT repo,availability,head_id FROM source_records WHERE id=?', (source['record_id'],)).fetchone()
        if not record or record['repo'] != repo_key(repo) or record['availability'] != 'available' or not fresh(db, source['record_id']):
            raise Invalid('Objective source access is not current; content is withheld')
        version = source.get('source_version_id') or record['head_id']
        historical = cm.citation(db, source['record_id'], version)
        withheld = db.execute('SELECT 1 FROM withheld_source_versions WHERE source_version_id=?', (version,)).fetchone()
        if not historical or withheld:
            raise Invalid('Objective source access is not current; content is withheld')
        current = cm.citation(db, source['record_id'], record['head_id'])
        changed = (_metadata_changed(source, current, historical) if source.get('metadata_only')
                   else record['head_id'] != version)
        if exact and changed:
            raise Invalid('Objective evidence changed; review the current source before acting')
        out.append(current)
    return out


def sources_current(store, sources, repo, *, exact=False):
    return sources_current_db(store.graph.db, sources, repo, exact=exact)


def remember_sources(store, ident, sources):
    """Persist body reads and metadata-only disclosure without conflating them."""
    from .objective_research import metadata_fingerprint
    db = store.graph.db
    for source in sources:
        if source.get('metadata_only'):
            observed = cm.citation(db, source['record_id'], source['source_version_id'])
            if not observed:
                raise Invalid('The observed metadata source version is missing')
            expected = source.get('metadata_sha256') or metadata_fingerprint(observed)
            db.execute('INSERT INTO objective_metadata_dependencies VALUES(?,?,?,?) ON CONFLICT DO NOTHING',
                       (ident, source['record_id'], source['source_version_id'], expected))
        else:
            db.execute('INSERT OR IGNORE INTO objective_sources VALUES(?,?,?)',
                       (ident, source['record_id'], source['source_version_id']))


def _sources(store, ident, db=None):
    db = db or store.graph.db
    full = [dict(r) for r in db.execute('SELECT record_id,source_version_id FROM objective_sources WHERE objective_id=?', (ident,))]
    metadata = [{**dict(r), 'metadata_only': True} for r in db.execute(
        'SELECT record_id,source_version_id,metadata_sha256 FROM objective_metadata_dependencies WHERE objective_id=?', (ident,))]
    return full + metadata


def _latest_sources(store, ident):
    selected = {}
    sources = _sources(store, ident)
    # Scope relies on body reads when present; disclosure retains every historical pin.
    read_ids = {source['record_id'] for source in sources if not source.get('metadata_only')}
    for source in sources:
        if source.get('metadata_only') and source['record_id'] in read_ids:
            continue
        version = store.graph.db.execute('SELECT sequence FROM source_versions WHERE id=?', (source['source_version_id'],)).fetchone()
        if version and (source['record_id'] not in selected or version['sequence'] > selected[source['record_id']][0]):
            selected[source['record_id']] = (version['sequence'], source)
    return [source for _, source in selected.values()]


def require_visible(store, value):
    sources_current(store, _sources(store, value['id']), value['repository'])
    from .visibility import require_task
    for run in store.graph.db.execute('SELECT task_id FROM objective_runs WHERE objective_id=?', (value['id'],)):
        require_task(store, run['task_id'])


def event(store, ident, kind, summary, actor='', detail=None):
    store.graph.db.execute('INSERT INTO objective_events VALUES(?,?,?,?,?,?,?)',
        (uuid.uuid4().hex, ident, kind, summary, actor, json.dumps(detail or {}), now()))


def _timestamp(value):
    if not value:
        return ''
    if not isinstance(value, str):
        raise Invalid('next_check_at must be an ISO timestamp with timezone')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError as error:
        raise Invalid('next_check_at must be an ISO timestamp with timezone') from error


def _payload(data):
    result = {k: field(data, k, limit=limit) for k, limit in (
        ('title', 300), ('outcome', 2000), ('success_criteria', 2000),
        ('stop_criteria', 2000), ('guardrails', 2000), ('repository', 100))}
    result['repository'] = repo_key(result['repository'])
    provider = data.get('provider', 'claude')
    from .workbench_hosts import settings
    settings(provider)  # Validate identifier, without requiring credentials to save an objective.
    result['provider'] = provider
    return result


def create(store, data, actor):
    human(actor)
    payload = _payload(data)
    key = field(data, 'request_key', limit=100)
    ids = data.get('source_ids', [])
    if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(v, str) for v in ids):
        raise Invalid('source_ids must contain at most 100 record IDs')
    selected = sources_current(store, [{'record_id': ident} for ident in sorted(set(ids))], payload['repository'])
    fingerprint = digest([payload, sorted(set(ids)), actor.id, data.get('start_preparation', False)])
    with store.graph.transaction() as db:
        prior = db.execute('SELECT id,request_hash FROM objectives WHERE request_key=?', (key,)).fetchone()
        if prior:
            if prior['request_hash'] != fingerprint:
                raise Invalid('This request key belongs to a different objective')
            return get(store, prior['id'])
        ident, stamp = uuid.uuid4().hex[:16], now()
        db.execute('INSERT INTO objectives(id,request_key,request_hash,title,outcome,success_criteria,stop_criteria,guardrails,repository,provider,requester_id,requester,requester_kind,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (ident, key, fingerprint, *[payload[k] for k in ('title','outcome','success_criteria','stop_criteria','guardrails','repository','provider')], actor.id, actor.name, actor.kind, stamp, stamp))
        for source in selected:
            db.execute('INSERT INTO objective_sources VALUES(?,?,?)', (ident, source['record_id'], source['source_version_id']))
        event(store, ident, 'accepted', 'A person asked Raven to take point within the recorded scope.', actor.id,
              {'authority': 'explicit-human-request', 'external_actions_authorized': False})
    return get(store, ident)


def _proposal_view(store, raw):
    value = dict(raw)
    sources = json.loads(value.pop('sources'))
    payload = json.loads(value.pop('payload'))
    try:
        current = sources_current(store, sources, value['repository'])
        stale = any(_metadata_changed(s, c, cm.citation(store.graph.db, s['record_id'], s['source_version_id'])) if s.get('metadata_only') else s['source_version_id'] != c['source_version_id'] for s, c in zip(sources, current))
    except Invalid:
        return {'id': value['id'], 'state': value['state'], 'revision': value['revision'],
                'withheld': True, 'title': 'Proposal withheld: source access changed'}
    return {**value, **payload, 'sources': sources, 'evidence_stale': stale,
            'notice': 'Evidence changed; dismiss this proposal or re-investigate current facts' if stale else '',
            'task_id': value['governance_run_id'], 'status': value['state']}


def get(store, ident):
    value = row(store, ident)
    require_visible(store, value)
    runs = [dict(r) for r in store.graph.db.execute('SELECT o.task_id,o.purpose,r.status,r.title,r.updated_at FROM objective_runs o JOIN runs r ON r.id=o.task_id WHERE o.objective_id=? ORDER BY o.created_at', (ident,))]
    for run in runs:
        run['browser_task'] = bool(store.graph.db.execute('SELECT 1 FROM browser_tasks WHERE task_id=?', (run['task_id'],)).fetchone())
    proposals = [_proposal_view(store, r) for r in store.graph.db.execute('SELECT * FROM objective_proposals WHERE objective_id=? ORDER BY created_at DESC', (ident,))]
    status = value['lifecycle']
    if status == 'active':
        status = 'needs_you' if value['blocked_reason'] or any(p['state'] in ('proposed','review','preparing','review_error','reconcile') for p in proposals) else (
            'moving' if any(r['status'] in ('queued','working') for r in runs) else 'waiting')
    return {**value, 'status': status, 'sources': [{**cm.citation(store.graph.db, s['record_id'], s['source_version_id']), **{k: s[k] for k in ('metadata_only','metadata_sha256') if k in s}} for s in _sources(store, ident)], 'runs': runs, 'proposals': proposals,
            'events': [{**dict(r), 'detail': json.loads(r['detail'])} for r in store.graph.db.execute('SELECT * FROM objective_events WHERE objective_id=? ORDER BY created_at,id', (ident,))],
            'notice': 'An objective survives completed coding runs. External actions require separate authorization.'}


def overview(store, workbench=None):
    items = []
    for r in store.graph.db.execute('SELECT id,lifecycle,revision FROM objectives ORDER BY updated_at DESC LIMIT 100'):
        try:
            items.append(get(store, r['id']))
        except Invalid:
            items.append({'id': r['id'], 'status': 'blocked', 'revision': r['revision'], 'withheld': True,
                          'title': 'Objective withheld: source access changed'})
    if workbench:
        config = workbench.config()
    else:
        from .workbench_hosts import capability, PROVIDERS
        config = {'hosts': [capability(p) for p in PROVIDERS]}
    from .inquiry import repositories
    from .objective_research import corpus_revision
    from .objective_head import background_budget
    research_limits = [background_budget(store, r['repository']) for r in store.graph.db.execute('SELECT DISTINCT repository FROM objective_background_attempts ORDER BY repository')]
    from .config import load
    background_cfg = load()
    background_inference = {'available': background_cfg.model_api != 'claude-cli',
        'notice': 'Automatic investigation requires a token-bounded API backend. The internal claude-cli backend supports explicit manual investigation only.' if background_cfg.model_api == 'claude-cli' else ''}
    research_status, status_repos = [], set()
    for evaluation in store.graph.db.execute('SELECT repository,state,attempts,updated_at FROM objective_evaluations ORDER BY updated_at DESC LIMIT 100'):
        if evaluation['repository'] in status_repos:
            continue
        status_repos.add(evaluation['repository'])
        if evaluation['state'] in ('failed', 'blocked', 'running'):
            research_status.append(dict(evaluation))
    research = []
    seen_repos = set()
    for evaluation in store.graph.db.execute("SELECT repository,result FROM objective_evaluations WHERE state='completed' ORDER BY updated_at DESC LIMIT 20"):
        if evaluation['repository'] in seen_repos:
            continue
        result = json.loads(evaluation['result'])
        packet = result.get('research') or {}
        if (packet.get('coverage') or {}).get('corpus_snapshot') != corpus_revision(store, evaluation['repository']):
            continue
        seen_repos.add(evaluation['repository'])
        research.append({'repository': evaluation['repository'], 'reason': result.get('reason', ''), **packet})
    return {'objectives': items, 'research': research, 'research_limits': research_limits, 'research_status': research_status, 'background_inference': background_inference, 'watches': [dict(w) for w in store.graph.db.execute('SELECT repository,enabled FROM objective_watches ORDER BY repository')], 'proposals': [_proposal_view(store, r) for r in store.graph.db.execute(
        "SELECT * FROM objective_proposals WHERE objective_id='' AND state IN ('proposed','review','preparing','review_error','reconcile') ORDER BY created_at DESC LIMIT 50")],
        'capabilities': config.get('hosts', []), 'repositories': config.get('repositories') or repositories(store),
        'execution_ready': bool(config.get('ready'))}


def _revision(data, value):
    if type(data.get('expected_revision')) is not int or data['expected_revision'] != value['revision']:
        raise Invalid('This item changed; reopen it before acting')


def _prior(db, data, ident, action, actor):
    key = field(data, 'request_key', limit=100)
    fingerprint = digest([ident, action, data, actor.id])
    prior = db.execute('SELECT * FROM objective_requests WHERE request_key=?', (key,)).fetchone()
    if prior and prior['fingerprint'] != fingerprint:
        raise Invalid('This request key belongs to a different action')
    return key, fingerprint, json.loads(prior['result']) if prior else None


def _save_request(db, key, fingerprint, result):
    db.execute('INSERT INTO objective_requests VALUES(?,?,?)', (key, fingerprint, json.dumps(result)))


def act(store, ident, action, data, actor, workbench=None):
    human(actor)
    with store.graph.transaction() as db:
        value = row(store, ident)
        require_visible(store, value)
        key, fingerprint, prior = _prior(db, data, ident, action, actor)
        if prior is not None:
            return get(store, ident)
        _revision(data, value)
        if value['lifecycle'] in ('achieved', 'stopped'):
            raise Invalid('This objective is closed; create a new objective for new work')
        changes = {}
        summary = str(data.get('summary') or '')
        if action == 'pause':
            changes['lifecycle'] = 'paused'
            _control_workers(store, ident, 'pause', actor, workbench)
        elif action == 'resume':
            assert_objective_authority(store, ident)
            changes.update(lifecycle='active', blocked_reason='')
            _control_workers(store, ident, 'resume', actor, workbench)
        elif action == 'cancel':
            changes.update(lifecycle='stopped', next_check_at='')
            _control_workers(store, ident, 'cancel', actor, workbench)
        elif action == 'checkpoint':
            summary = field(data, 'summary', limit=4000)
            changes.update(next_check_at=_timestamp(data.get('next_check_at', '')), waiting_on=str(data.get('waiting_on') or '')[:1000])
        elif action == 'achieve':
            summary = field(data, 'summary', limit=4000)
            _control_workers(store, ident, 'pause', actor, workbench)
            changes.update(lifecycle='achieved', next_check_at='')
        elif action == 'dispatch':
            if value['lifecycle'] != 'active':
                raise Invalid('Resume this objective before starting work')
            if workbench is None:
                raise Invalid('Browser execution is not configured; objective is saved')
            active = db.execute("SELECT 1 FROM objective_runs o JOIN browser_tasks b ON b.task_id=o.task_id WHERE o.objective_id=? AND b.status NOT IN ('completed','cancelled','failed')", (ident,)).fetchone()
            if active:
                raise Invalid('This objective already has an unfinished worker; open or resume that task')
            assert_objective_authority(store, ident)
            sources_current(store, _latest_sources(store, ident), value['repository'], exact=True)
            accepted = db.execute("SELECT id,payload FROM objective_proposals WHERE objective_id=? AND state='accepted' ORDER BY updated_at DESC LIMIT 1", (ident,)).fetchone()
            move = json.loads(accepted['payload']) if accepted else value
            prompt = '\n'.join([
                'Accepted objective outcome: ' + value['outcome'],
                'Outer objective guardrails: ' + value['guardrails'],
                'Outer objective stop criteria: ' + value['stop_criteria'],
                'Exact current move (all cases and limits are part of its signed scope):',
                json.dumps({k: move[k] for k in ('outcome', 'success_criteria', 'stop_criteria', 'guardrails', 'cases', 'judgment_kind') if k in move}, ensure_ascii=False),
                'Prepare and verify work in the private repository only. No external publication, communication, deployment or spending is authorized. Route consequential choices through Raven. Coding completion is not objective achievement.'])
            if len(prompt) > 12000:
                raise Invalid('The complete signed move exceeds the host prompt bound; revise its scope explicitly rather than truncating its limits')
            result = workbench.submit({'prompt': prompt, 'repository': value['repository'], 'host': value['provider'],
                                       'request_key': 'objective:' + digest([ident, key])[:60]}, actor)
            db.execute('INSERT INTO objective_runs VALUES(?,?,?,?)', (ident, result['task_id'], 'Bounded internal preparation', now()))
            db.execute('INSERT INTO objective_run_authority VALUES(?,?,?)', (result['task_id'], ident, accepted['id'] if accepted else ''))
            for source in _latest_sources(store, ident):
                cm.add_anchor(db, result['task_id'], source['record_id'], source['source_version_id'], 'context')
            changes.update(blocked_reason='', waiting_on='Coding worker; outcome still requires evidence')
            summary = 'Started a private-copy coding worker; no external actions authorized.'
        else:
            raise Invalid('Unknown objective action')
        changes.update(revision=value['revision'] + 1, updated_at=now())
        db.execute('UPDATE objectives SET ' + ','.join(k+'=?' for k in changes) + ' WHERE id=?', (*changes.values(), ident))
        event(store, ident, action, summary or action, actor.id,
              {'provenance': 'human-attested' if action == 'achieve' else 'human-request', 'independently_verified': False})
        result = get(store, ident)
        _save_request(db, key, fingerprint, result)
        return result


def governance_context(payload, sources):
    """Keep every decision limit readable; bind bulky research without truncation."""
    context = {key: value for key, value in payload.items() if key != 'research'}
    context['research_digest'] = digest(payload.get('research', {}))
    context['source_dependencies_digest'] = digest(sources)
    text = json.dumps(context, ensure_ascii=False)
    if len(text) > 12000:
        raise Invalid('The complete proposed scope exceeds the judgment context limit; propose a smaller bounded move')
    return text


def propose(store, payload, sources, *, objective_id=''):
    """Only bounded validated head output calls this; it grants no authority."""
    payload = _payload(payload) | {'why_now': field(payload, 'why_now', limit=2000),
        'cases': payload.get('cases', {}), 'judgment_kind': payload.get('judgment_kind', 'interpretation_tradeoff'),
        'citations': payload.get('citations', []), 'research': payload.get('research', {})}
    governance_context(payload, sources)
    sources_current(store, sources, payload['repository'], exact=True)
    if not sources:
        raise Invalid('An originated proposal requires current versioned evidence')
    fingerprint = digest([objective_id, payload, sources])
    with store.graph.transaction() as db:
        prior = db.execute('SELECT * FROM objective_proposals WHERE fingerprint=?', (fingerprint,)).fetchone()
        if prior:
            return _proposal_view(store, prior)
        if not objective_id:
            existing = db.execute("SELECT * FROM objective_proposals WHERE objective_id='' AND repository=? AND state IN ('proposed','review','preparing','review_error','reconcile')", (payload['repository'],)).fetchone()
            if existing:
                return _proposal_view(store, existing)
        if objective_id:
            existing = db.execute("SELECT * FROM objective_proposals WHERE objective_id=? AND state IN ('proposed','review','preparing','review_error','reconcile')", (objective_id,)).fetchone()
            if existing:
                return _proposal_view(store, existing)
        ident, stamp = uuid.uuid4().hex[:16], now()
        db.execute('INSERT INTO objective_proposals(id,objective_id,repository,fingerprint,payload,sources,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                   (ident, objective_id, payload['repository'], fingerprint, json.dumps(payload), json.dumps(sources), stamp, stamp))
        if objective_id:
            event(store, objective_id, 'proposal', payload['why_now'], detail={'proposal_id': ident})
        return _proposal_view(store, db.execute('SELECT * FROM objective_proposals WHERE id=?', (ident,)).fetchone())


def proposal_action(store, ident, action, data, actor, cfg=None):
    human(actor)
    from . import canvas
    from .config import Config
    from .approval_scope import scope_hash
    from .store import answer_hash
    with store.graph.transaction() as db:
        raw = db.execute('SELECT * FROM objective_proposals WHERE id=?', (ident,)).fetchone()
        if raw is None:
            raise Invalid('Proposal not found')
        value = dict(raw)
        sources = json.loads(value['sources'])
        if action != 'dismiss':
            sources_current(store, sources, value['repository'], exact=True)
        key, fingerprint, prior = _prior(db, data, ident, action, actor)
        if prior is not None:
            return _proposal_view(store, raw)
        _revision(data, value)
        if value['state'] not in ('proposed', 'review', 'review_error', 'reconcile'):
            raise Invalid('This proposal is no longer open or its review is being prepared')
        if action == 'dismiss':
            db.execute("UPDATE objective_proposals SET state='dismissed',rationale=?,revision=revision+1,updated_at=? WHERE id=?",
                       (field(data, 'rationale', 'Not pursuing this proposal', 2000), now(), ident))
            result = _proposal_view(store, db.execute('SELECT * FROM objective_proposals WHERE id=?', (ident,)).fetchone())
            _save_request(db, key, fingerprint, result)
            return result
        if value['state'] == 'reconcile':
            raise Invalid('The signed response changed the proposal; dismiss it or review the changed judgment in the existing inbox')
        if action != 'accept':
            raise Invalid('Unknown proposal action')
        if value['decision_id']:
            result = _proposal_view(store, raw)
            _save_request(db, key, fingerprint, result)
            return result
        db.execute("UPDATE objective_proposals SET state='preparing',updated_at=? WHERE id=?", (now(), ident))
    # Existing core APIs may open their own transactions and read connectors.
    # Never hold our writer lock across them. Stable client identities recover
    # a completed kickoff/node if a later step fails.
    try:
        payload = json.loads(value['payload'])
        run = canvas.start_task(store, Config(model_api='none'), {'title': payload['title'], 'goal': payload['outcome'],
            'repo': value['repository'], 'agent': 'Raven objective head', 'requester': actor.name, 'client_key': 'objective-review:' + ident})
        task_id = run['task_id']
        with store.graph.transaction() as db:
            for source in sources:
                cm.add_anchor(db, task_id, source['record_id'], source['source_version_id'], 'context')
        node = canvas.add_node(store, Config(model_api='none'), {'task_id': task_id,
            'question': 'Should Raven take point on: ' + payload['title'] + '?',
            'context': governance_context(payload, sources), 'client_ref': 'objective-acceptance:' + ident,
            'category': 'objective_scope', 'requester': actor.name})
        answer = 'Take point on ' + payload['outcome'] + '\nWithin: ' + payload['guardrails'] + '\nOnly private preparation; no external actions authorized.'
        if node.get('authorized'):
            raise Invalid('A prior rule resolved this governance request; create a fresh scoped proposal for explicit review')
        canvas.settle_node(store, {'task_id': task_id, 'node_id': node['node_id'], 'answer': answer,
            'rationale': payload['why_now'], 'source_evidence': [{'record_id': s['record_id'], 'source_version_id': s['source_version_id'], 'role': s.get('role', 'support')} for s in sources if not s.get('metadata_only')]})
        decision = store.get_decision(node['node_id'])
        with store.graph.transaction() as db:
            sources_current(store, sources, value['repository'], exact=True)
            db.execute("UPDATE objective_proposals SET state='review',governance_run_id=?,decision_id=?,acceptance_hash=?,scope_hash=?,revision=revision+1,updated_at=? WHERE id=?",
                       (task_id, node['node_id'], answer_hash(answer), scope_hash(decision), now(), ident))
            result = _proposal_view(store, db.execute('SELECT * FROM objective_proposals WHERE id=?', (ident,)).fetchone())
            _save_request(db, key, fingerprint, result)
            return result
    except Exception:
        with store.graph.transaction() as db:
            db.execute("UPDATE objective_proposals SET state='review_error',updated_at=? WHERE id=?", (now(), ident))
        raise


def assert_objective_authority(store, ident, proposal_id=None, db=None):
    """Consume current core judgment; never create or infer approval here."""
    from .approval_scope import scope_hash
    from .store import answer_hash
    db = db or store.graph.db
    query = "SELECT * FROM objective_proposals WHERE objective_id=? AND state='accepted'"
    proposals = list(db.execute(query, (ident,)))
    if proposal_id is not None:
        if not proposal_id:
            return
        proposals = [p for p in proposals if p['id'] == proposal_id]
        if not proposals:
            raise Invalid('This worker scope was superseded; start a fresh worker for the new signed move')
    for proposal in proposals:
        decision = store.get_decision(proposal['decision_id'], _db=db)
        if (not decision['authorized'] or answer_hash(decision['answer']) != proposal['acceptance_hash']
                or scope_hash(decision) != proposal['scope_hash']):
            raise Invalid('The objective judgment changed or requires review; resolve it in the existing inbox before continuing')
        sources_current_db(db, json.loads(proposal['sources']), proposal['repository'], exact=True)


def assert_task_authority(store, task_id, db=None):
    db = db or store.graph.db
    for linked in db.execute('SELECT objective_id,proposal_id FROM objective_run_authority WHERE task_id=?', (task_id,)):
        value = dict(db.execute('SELECT * FROM objectives WHERE id=?', (linked['objective_id'],)).fetchone())
        from .visibility import affected
        denied, tasks = affected(db)
        sources = _sources(store, value['id'], db=db)
        if task_id in tasks or any(s['record_id'] in denied or s['source_version_id'] in denied for s in sources):
            raise Invalid('Objective source access is not current; this worker cannot continue')
        if value['lifecycle'] != 'active':
            raise Invalid('The owning objective is paused or closed; this worker cannot continue')
        assert_objective_authority(store, value['id'], linked['proposal_id'], db=db)


def _control_workers(store, ident, action, actor, workbench):
    active = [dict(r) for r in store.graph.db.execute('SELECT b.task_id,b.status FROM objective_runs o JOIN browser_tasks b ON b.task_id=o.task_id WHERE o.objective_id=?', (ident,))]
    for run in active:
        if run['status'] in ('completed', 'cancelled', 'failed'):
            continue
        if action == 'resume' and run['status'] != 'paused':
            continue
        if not workbench:
            raise Invalid('The linked worker service is unavailable; no objective control was changed')
        workbench.control(run['task_id'], action, actor)
