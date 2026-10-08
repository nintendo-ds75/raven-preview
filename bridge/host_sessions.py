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
CREATE TABLE IF NOT EXISTS host_activity (
 binding_id TEXT PRIMARY KEY REFERENCES host_sessions(id), state TEXT NOT NULL,
 held_since TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS host_reports (
 message_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, state TEXT NOT NULL, posted INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL);
'''
# Answers the agent may already have read on the tree (bridge_wait while its
# Stop hook held it). Those are not a reason to resume it.
ANSWER_EVENTS = ('owner_approved', 'answer_corrected', 'signature', 'signoff', 'owner_changed')
WAKE_EVENTS = ('owner_approved', 'answer_corrected', 'signature', 'signoff', 'owner_changed',
               'source_review_required', 'followup_added', 'rule_ended', 'rule_made',
               'dependent_flagged', 'prediction_withdrawn', 'context_evidence_restored', 'conformance_read')


def migrate(db):
    db.executescript(SCHEMA)


def _stop_hold():
    """How long, in total, a Stop hook keeps an agent waiting on people
    before it may stop; a person's answer after that resumes it instead."""
    import os
    try:
        return timedelta(minutes=max(0.0, float(os.environ.get('BRIDGE_STOP_HOLD_MINUTES', '20'))))
    except ValueError:
        return timedelta(minutes=20)


def _activity(g, bid, state, at, held_since=None):
    row = g.db.execute('SELECT held_since FROM host_activity WHERE binding_id=?', (bid,)).fetchone()
    held = (row['held_since'] if row else '') if held_since is None else held_since
    if row is None:
        g.db.execute('INSERT INTO host_activity(binding_id,state,held_since,updated_at) VALUES(?,?,?,?)',
                     (bid, state, held, at))
    else:
        g.db.execute('UPDATE host_activity SET state=?,held_since=?,updated_at=? WHERE binding_id=?',
                     (state, held, at, bid))


def sessions(store, args, principal=None):
    """The stopped sessions of this credential in this checkout, for a
    supervisor that watches all of them rather than one exact session.
    A session that is working, or held waiting on a person, is not listed:
    it reads its answers itself."""
    host = args.get('host', '')
    if host not in ('claude', 'codex'):
        raise Invalid('Supported hosts are claude and codex')
    if not isinstance(args.get('project'), str) or not args['project']:
        raise Invalid('A bounded project is required')
    who = getattr(principal, 'id', '') or 'local-operator'
    rows = store.graph.db.execute(
        "SELECT s.session_id FROM host_sessions s JOIN host_activity a ON a.binding_id=s.id "
        "WHERE s.principal=? AND s.host=? AND s.project=? AND s.task_id<>'' AND a.state IN ('stopped','ended') "
        "ORDER BY a.updated_at", (who, host, args['project'])).fetchall()
    return {'sessions': [r['session_id'] for r in rows]}


def event(store, args, principal=None):
    from . import canvas
    from .config import load
    from .store import repo_key
    action = args.get('event')
    if action == 'sessions':
        return sessions(store, args, principal)
    if action not in ('connect', 'prompt', 'new_task', 'poll', 'ack', 'release', 'stop', 'end', 'report'):
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
        if action in ('connect', 'prompt', 'new_task'):
            # A person typing starts any Stop hold afresh.
            _activity(g, bid, 'working', at, '' if action != 'connect' else None)
        elif action == 'end':
            _activity(g, bid, 'ended', at, '')
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
    if action == 'end':
        return {'ended': True}
    if action == 'stop':
        return stop(store, row)
    if action == 'report':
        return report(store, row, args)
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
    from . import canvas
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
            unread = {n['node_id'] for n in canvas.unread_by_agent(store, binding['task_id'])}
            if all(e['kind'] in ANSWER_EVENTS and e['decision_id'] and e['decision_id'] not in unread for e in events):
                # The agent read these answers on the tree while it was held
                # waiting; resuming it for them again would only repeat work.
                g.db.execute('UPDATE host_sessions SET cursor=? WHERE id=?', (through, binding['id']))
                return {'message': None, 'reason': 'The agent already read these answers'}
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


def stop(store, binding):
    """Whether the host may stop now. While a decision on the session's open
    task waits on a person, the agent is held and told to wait for it, so an
    answer given in Slack reaches an agent that is still there. The hold
    has a budget; past it the agent stops and a later answer resumes it.
    Only counts and ids are returned: human and source text stays on the
    tree, read through the normal tools."""
    from . import canvas
    g, at = store.graph, cm.stamp()
    waiting, observed = [], ''
    if binding['task_id']:
        task = g.db.execute('SELECT status FROM runs WHERE id=?', (binding['task_id'],)).fetchone()
        if task and task['status'] not in ('completed', 'abandoned'):
            tree = canvas.get_tree(store, binding['task_id'])
            observed = tree['observed_at']
            waiting = [n for n in canvas._flatten(tree['nodes']) if n.get('blocking')]
    with g.transaction():
        row = g.db.execute('SELECT held_since FROM host_activity WHERE binding_id=?', (binding['id'],)).fetchone()
        held = row['held_since'] if row else ''
        if waiting:
            started = held or at
            if datetime.fromisoformat(started) + _stop_hold() > datetime.now(timezone.utc):
                _activity(g, binding['id'], 'holding', at, started)
                return {'hold': True, 'task_id': binding['task_id'], 'waiting': len(waiting), 'observed_at': observed,
                        'reason': (f"Raven: {len(waiting)} decision(s) on task {binding['task_id']} still wait on a "
                                   f"person. Call bridge_wait with task_id={binding['task_id']} and since={observed}, "
                                   "act on what changed, and continue. Do not stop while a decision you need is "
                                   "unanswered.")}
        _activity(g, binding['id'], 'stopped', at, held if waiting else '')
    return {'hold': False, 'task_id': binding['task_id'], 'waiting': len(waiting)}


def _clean(text, limit):
    """Agent or person text quoted into Slack: no mentions, no markup that
    reads as Raven's own, cut to a bound and said so."""
    import re
    text = re.sub(r'<[!@#][^>]*>', '', str(text or '')).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + ' …'


def _report_text(store, binding, message, state, summary, changed, denied, base_url):
    g = store.graph
    tree_task = g.db.execute('SELECT title, repo FROM runs WHERE id=?', (message['task_id'],)).fetchone()
    previous = g.db.execute("SELECT max(through_event) AS id FROM host_resumes WHERE binding_id=? AND task_id=? "
                            "AND state='done' AND through_event<?", (binding['id'], message['task_id'],
                                                                     message['through_event'])).fetchone()
    marks = ','.join('?' for _ in ANSWER_EVENTS)
    decided = [r['decision_id'] for r in g.db.execute(
        f'SELECT DISTINCT decision_id FROM events WHERE run_id=? AND id>? AND id<=? AND kind IN ({marks}) '
        'AND decision_id IS NOT NULL', [message['task_id'], previous['id'] or 0, message['through_event'],
                                        *ANSWER_EVENTS]).fetchall()]
    woken = []
    for did in decided[:3]:
        d = g.db.execute('SELECT answer, answered_by, signed_by FROM decisions WHERE id=?', (did,)).fetchone()
        if d and (d['answer'] or '').strip():
            woken.append(f"{_clean(d['signed_by'] or d['answered_by'] or 'A person', 80)} — "
                         f"“{_clean(d['answer'], 200)}”")
    from . import canvas
    still = [n for n in canvas._flatten(canvas.get_tree(store, message['task_id'])['nodes']) if n.get('blocking')]
    head = {'done': '🔁 The coding agent picked up the new answers and continued.',
            'blocked': '⚠️ The coding agent continued, but some actions were blocked and need a person.',
            'failed': '❌ The coding agent could not be resumed. The answers are saved on the task; it sees them '
                      'the next time it runs.'}[state]
    lines = [head, '', f"*Task:* {_clean(tree_task['title'], 120)} · {_clean(tree_task['repo'], 80)}"
             if tree_task else f"*Task:* {message['task_id']}"]
    if woken:
        lines.append('*Woken by:* ' + '; '.join(woken))
    lines.append(f"*Agent:* {binding['host']} session {binding['session_id'][:8]}…")
    if summary:
        lines += ['', '*What it reported*', '> ' + _clean(summary, 1200).replace('\n', '\n> ')]
    if changed:
        lines.append('*Changed:* ' + ', '.join(_clean(c, 120) for c in changed[:8])
                     + (f' and {len(changed) - 8} more' if len(changed) > 8 else ''))
    if denied:
        lines += ['*Blocked:*'] + ['• ' + _clean(d, 160) for d in denied[:5]]
        lines.append(f"Resume it yourself to approve them: `{binding['host']} --resume {binding['session_id']}`"
                     if binding['host'] == 'claude' else f"Resume it yourself: `codex resume {binding['session_id']}`")
    if still:
        lines.append(f"*Still waiting on people:* {len(still)} decision(s)")
    if base_url:
        lines.append(f"<{base_url}/#runs/{message['task_id']}|Open task in Raven>")
    return '\n'.join(lines)


def report(store, binding, args):
    """The supervisor's account of one resume, recorded on the task and
    posted once to each Slack thread Raven started for that task. The
    agent's words are its claim, quoted and bounded, never Raven's own."""
    g, at = store.graph, cm.stamp()
    mid = args.get('message_id', '')
    message = g.db.execute('SELECT * FROM host_resumes WHERE id=? AND binding_id=?', (mid, binding['id'])).fetchone()
    if message is None:
        raise Invalid('No resume message for this session')
    exit_code = args.get('exit_code')
    if not isinstance(exit_code, int):
        raise Invalid('exit_code must be the resumed process exit status')
    summary = args.get('summary', '')
    changed, denied = args.get('changed', []), args.get('denied', [])
    if not isinstance(summary, str) or len(summary) > 20000:
        raise Invalid('summary must be text up to 20000 characters')
    for name, value in (('changed', changed), ('denied', denied)):
        if not isinstance(value, list) or len(value) > 200 or any(not isinstance(v, str) or len(v) > 500 for v in value):
            raise Invalid(f'{name} must be at most 200 short strings')
    state = 'failed' if exit_code != 0 else 'blocked' if denied else 'done'
    with g.transaction():
        if g.db.execute('SELECT 1 FROM host_reports WHERE message_id=?', (mid,)).fetchone():
            return {'reported': True, 'duplicate': True}
        g.db.execute('INSERT INTO host_reports(message_id,task_id,state,created_at) VALUES(?,?,?,?)',
                     (mid, message['task_id'], state, at))
        g.append_event('host_resume_report', {'task_id': message['task_id'], 'binding_id': binding['id'],
                                              'message_id': mid, 'state': state, 'exit_code': exit_code,
                                              'summary': summary[-3000:], 'changed': changed[:50],
                                              'denied': denied[:20]})
    posted = 0
    delivery = store.delivery
    if delivery.transport is not None and hasattr(delivery.transport, 'post_message'):
        text = _report_text(store, binding, message, state, summary, changed, denied, delivery.base_url)
        refs = sorted({r['external_ref'] for r in g.db.execute(
            "SELECT external_ref FROM notifications WHERE run_id=? AND external_ref<>'' AND state='sent'",
            (message['task_id'],)).fetchall()})
        for ref in refs:
            channel, _, thread = ref.partition(':')
            try:
                delivery.transport.post_message(channel, text, None, thread_ts=thread)
                posted += 1
            except Exception as error:
                print(f'Raven: could not post the resume report to Slack: {type(error).__name__}')
        with g.transaction():
            g.db.execute('UPDATE host_reports SET posted=? WHERE message_id=?', (posted, mid))
    return {'reported': True, 'state': state, 'posted': posted}
