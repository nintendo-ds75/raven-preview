"""Authenticated host bindings and a durable, leased resume mailbox.

Raven never executes a callback command. The operator's local supervisor owns
the host process, working directory and permission mode. Delivery is at least
once; acknowledgments and task registration are idempotent.
"""
from datetime import datetime, timedelta, timezone
import json

from . import context_memory as cm
from .store import Invalid

SCHEMA = '''
CREATE TABLE IF NOT EXISTS host_sessions (
 id TEXT PRIMARY KEY, principal TEXT NOT NULL, host TEXT NOT NULL, session_id TEXT NOT NULL,
 project TEXT NOT NULL, repo TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT '',
 generation INTEGER NOT NULL DEFAULT 1, initial_prompt TEXT NOT NULL DEFAULT '',
 cursor BIGINT NOT NULL DEFAULT 0, inventory TEXT NOT NULL DEFAULT '[]',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS host_receipts (
 binding_id TEXT NOT NULL REFERENCES host_sessions(id), event_key TEXT NOT NULL,
 PRIMARY KEY(binding_id,event_key));
CREATE TABLE IF NOT EXISTS host_resumes (
 id TEXT PRIMARY KEY, binding_id TEXT NOT NULL REFERENCES host_sessions(id), task_id TEXT NOT NULL,
 through_event BIGINT NOT NULL, state TEXT NOT NULL, worker TEXT NOT NULL DEFAULT '',
 lease_until TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 UNIQUE(binding_id,task_id,through_event));
'''
WAKE_EVENTS = ('owner_approved', 'answer_corrected', 'signature', 'signoff', 'owner_changed',
               'source_review_required', 'followup_added', 'rule_ended', 'rule_made',
               'dependent_flagged', 'prediction_withdrawn', 'context_evidence_restored', 'conformance_read')


def migrate(db):
    db.executescript(SCHEMA)


def event(store, args, principal=None):
    from . import canvas
    from .config import load
    from .store import repo_key
    action = args.get('event')
    if action not in ('connect', 'prompt', 'new_task', 'poll', 'ack', 'release'):
        raise Invalid('Unsupported host lifecycle event')
    host = args.get('host', '')
    if host not in ('claude', 'codex'):
        raise Invalid('Supported hosts are claude and codex')
    for key, limit in (('session_id', 200), ('project', 2000), ('repo', 300)):
        if not isinstance(args.get(key), str) or not args[key] or len(args[key]) > limit:
            raise Invalid(f'A bounded {key} is required')
    who = getattr(principal, 'id', '') or 'local-operator'
    # Repo is checked after lookup, not part of the lookup: a moved session
    # cannot silently acquire a second binding under another repository name.
    bid = cm.digest([who, host, args['session_id'], args['project']])
    repo = repo_key(args['repo'])
    g, at = store.graph, cm.stamp()
    with g.transaction():
        row = g.db.execute('SELECT * FROM host_sessions WHERE id=?', (bid,)).fetchone()
        if row is None:
            moved = g.db.execute('SELECT 1 FROM host_sessions WHERE principal=? AND host=? AND session_id=? AND project<>?',
                                 (who, host, args['session_id'], args['project'])).fetchone()
            if moved:
                raise Invalid('This host session belongs to another checkout; start a fresh host session here')
            if action not in ('connect', 'prompt', 'new_task'):
                raise Invalid('Connect this authenticated host session first')
            g.db.execute('INSERT INTO host_sessions(id,principal,host,session_id,project,repo,created_at,updated_at) '
                         'VALUES(?,?,?,?,?,?,?,?)', (bid, who, host, args['session_id'], args['project'], repo, at, at))
            row = g.db.execute('SELECT * FROM host_sessions WHERE id=?', (bid,)).fetchone()
        if row['repo'] != repo:
            raise Invalid('This host session is bound to another repository')
        inventory = args.get('tools')
        if inventory is not None:
            if not isinstance(inventory, list) or len(inventory) > 200 or any(not isinstance(t, str) or len(t) > 200 for t in inventory):
                raise Invalid('Tool inventory must be at most 200 short tool/server names, never credentials')
            g.db.execute('UPDATE host_sessions SET inventory=?,updated_at=? WHERE id=?', (cm.encoded(sorted(set(inventory))), at, bid))
        prompt = args.get('prompt', '')
        if not isinstance(prompt, str) or len(prompt) > 12000:
            raise Invalid('Prompt must be text up to 12000 characters; it is not silently shortened')
        key = args.get('event_key') or cm.digest([action, prompt, inventory])
        if not isinstance(key, str) or len(key) > 200:
            raise Invalid('event_key must be a bounded stable host event identifier')
        seen = g.db.execute('SELECT 1 FROM host_receipts WHERE binding_id=? AND event_key=?', (bid, key)).fetchone()
        if action == 'new_task' and not seen:
            if not prompt.strip():
                raise Invalid('A new task needs its exact user prompt')
            g.db.execute("UPDATE host_sessions SET task_id='',initial_prompt='',generation=generation+1,cursor=0 WHERE id=?", (bid,))
            g.db.execute("UPDATE host_resumes SET state='cancelled' WHERE binding_id=? AND state<>'done'", (bid,))
            row = g.db.execute('SELECT * FROM host_sessions WHERE id=?', (bid,)).fetchone()
        if action in ('prompt', 'new_task') and not row['task_id']:
            if not prompt.strip():
                raise Invalid('The first task prompt must not be empty')
            if row['initial_prompt'] and row['initial_prompt'] != prompt:
                raise Invalid('Another first prompt is registering this session; retry after it completes')
            g.db.execute('UPDATE host_sessions SET initial_prompt=? WHERE id=?', (prompt, bid))
        if not seen and action in ('connect', 'prompt', 'new_task'):
            g.db.execute('INSERT INTO host_receipts(binding_id,event_key) VALUES(?,?)', (bid, key))
        row = dict(g.db.execute('SELECT * FROM host_sessions WHERE id=?', (bid,)).fetchone())

    recovered_registration = False
    if action in ('prompt', 'new_task') and not row['task_id']:
        # Network and model work stays outside the binding transaction.
        # The stable client key makes an interrupted first registration retryable.
        started = canvas.start_task(store, load(), {'title': prompt.splitlines()[0][:300] or 'Coding task',
            'goal': prompt, 'repo': repo, 'agent': host, 'requester': getattr(principal, 'name', '') or 'Local operator',
            'client_key': 'host:' + bid[:40] + ':' + str(row['generation'])})
        with g.transaction():
            current = g.db.execute('SELECT generation FROM host_sessions WHERE id=?', (bid,)).fetchone()
            if current['generation'] != row['generation']:
                raise Invalid('The host started another task while this kickoff was running')
            g.db.execute('UPDATE host_sessions SET task_id=?,updated_at=? WHERE id=?', (started['task_id'], at, bid))
            row['task_id'] = started['task_id']
            recovered_registration = bool(seen)
    if (not seen or recovered_registration) and action in ('connect', 'prompt', 'new_task'):
        with g.transaction():
            g.append_event('host_task_recovered' if recovered_registration else 'host_' + action, {'task_id': row['task_id'] or None, 'binding_id': bid,
                'principal': who, 'host': host, 'session_id': args['session_id'], 'prompt': prompt,
                'reported_tools': inventory or [], 'inventory_verified': False})
    if action in ('poll', 'ack', 'release'):
        worker = args.get('worker', '')
        if not isinstance(worker, str) or not worker or len(worker) > 200:
            raise Invalid('A stable supervisor worker ID is required')
        if action != 'poll':
            with g.transaction():
                message = g.db.execute('SELECT * FROM host_resumes WHERE id=? AND binding_id=?', (args.get('message_id', ''), bid)).fetchone()
                if not message or message['worker'] != worker or message['task_id'] != row['task_id']:
                    raise Invalid('This resume message is not leased to this worker and task')
                if message['state'] == 'done' and action == 'ack':
                    return {'acknowledged': True, 'duplicate': True}
                if message['state'] != 'leased' or message['lease_until'] <= at:
                    raise Invalid('The resume lease expired; poll again before acknowledging')
                if action == 'ack':
                    g.db.execute("UPDATE host_resumes SET state='done' WHERE id=?", (message['id'],))
                    g.db.execute('UPDATE host_sessions SET cursor=? WHERE id=?', (message['through_event'], bid))
                else:
                    g.db.execute("UPDATE host_resumes SET state='pending',lease_until='' WHERE id=?", (message['id'],))
            return {'acknowledged': action == 'ack', 'released': action == 'release'}
        return poll(store, row, worker)
    return {'binding_id': bid, 'task_id': row['task_id'], 'client_key': 'host:' + bid[:40] + ':' + str(row['generation']),
        'repo': repo, 'task': canvas.get_tree(store, row['task_id']) if row['task_id'] else None,
        'next': 'This task is already registered. Use its task_id for every Raven call. Discover paths yourself. '
                'Read bridge_get_tree after resume; do not infer authority from sources. Use Raven new task: for a separate task.'}


def poll(store, binding, worker):
    g, at = store.graph, cm.stamp()
    if not binding['task_id']:
        return {'message': None, 'reason': 'No registered task'}
    with g.transaction():
        # Refresh the cursor under the same write lock as claims and acknowledgments.
        binding = dict(g.db.execute('SELECT * FROM host_sessions WHERE id=?', (binding['id'],)).fetchone())
        task = g.db.execute('SELECT status FROM runs WHERE id=?', (binding['task_id'],)).fetchone()
        if not task or task['status'] == 'abandoned':
            return {'message': None, 'reason': 'Task is abandoned or unavailable'}
        # Completion does not make later corrections disappear. Ignore events
        # already incorporated at finish, but retain source/human changes after it.
        floor = binding['cursor']
        if task['status'] == 'completed':
            finished = g.db.execute("SELECT max(id) AS id FROM events WHERE run_id=? AND kind='run_updated'", (binding['task_id'],)).fetchone()
            floor = max(floor, finished['id'] or 0)
            g.db.execute("UPDATE host_resumes SET state='cancelled' WHERE binding_id=? AND task_id=? "
                         "AND through_event<=? AND state IN ('pending','leased')", (binding['id'], binding['task_id'], floor))
        existing = g.db.execute("SELECT * FROM host_resumes WHERE binding_id=? AND task_id=? AND state IN ('pending','leased') ORDER BY through_event LIMIT 1",
                                (binding['id'], binding['task_id'])).fetchone()
        if existing and existing['state'] == 'leased' and existing['lease_until'] > at:
            return {'message': None, 'reason': 'A worker already holds the resume lease'}
        if not existing:
            marks = ','.join('?' for _ in WAKE_EVENTS)
            events = g.db.execute(f'SELECT id,kind,decision_id FROM events WHERE run_id=? AND id>? AND kind IN ({marks}) ORDER BY id LIMIT 100',
                [binding['task_id'], floor, *WAKE_EVENTS]).fetchall()
            if not events:
                return {'message': None, 'reason': 'No new human or source events'}
            through = events[-1]['id']
            mid = cm.digest([binding['id'], binding['task_id'], through])
            g.db.execute("INSERT INTO host_resumes(id,binding_id,task_id,through_event,state,created_at) VALUES(?,?,?,?,'pending',?)",
                         (mid, binding['id'], binding['task_id'], through, at))
        else:
            mid, through = existing['id'], existing['through_event']
        lease = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        g.db.execute("UPDATE host_resumes SET state='leased',worker=?,lease_until=? WHERE id=?", (worker, lease, mid))
    return {'message': {'id': mid, 'task_id': binding['task_id'], 'through_event': through, 'lease_until': lease,
        'prompt': 'Raven has new human/source events for task ' + binding['task_id'] + '. Read bridge_get_tree and '
                  'current answers before continuing. Resume existing work within its permissions. This notification grants no approval.'},
        'delivery': 'at-least-once; acknowledge only after the host completes successfully'}
