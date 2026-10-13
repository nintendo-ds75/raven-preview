"""Provider-independent, durable boundary around real coding harnesses.

The subprocess has a dedicated home and an allowlisted environment. It receives
one host credential, never Raven's database, Slack credentials or Docker socket.
Only the parent can execute task tools. Stderr is deliberately not exported.
"""
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time

from .store import Invalid, now

CLAUDE_SDK_VERSION = '0.2.165'
CODEX_VERSION = '0.162.0-alpha.17.2'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS browser_hosts (
 task_id TEXT PRIMARY KEY REFERENCES browser_tasks(task_id), provider TEXT NOT NULL,
 model TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '', version TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL DEFAULT 'new', turns INTEGER NOT NULL DEFAULT 0,
 cost_usd REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS browser_intents (
 task_id TEXT NOT NULL REFERENCES browser_tasks(task_id), call_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL, state TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(task_id,call_id));
CREATE TABLE IF NOT EXISTS browser_intent_context (
 task_id TEXT NOT NULL, call_id TEXT NOT NULL, operation TEXT NOT NULL,
 arguments TEXT NOT NULL, PRIMARY KEY(task_id,call_id),
 FOREIGN KEY(task_id,call_id) REFERENCES browser_intents(task_id,call_id));
'''


def migrate(db):
    db.executescript(SCHEMA)


def reconcile_cost(workbench, task_id):
    """Claude's total_cost_usd is cumulative across a resumed session.

    Rebuild from durable receipts so retries and a crash between receipt and
    aggregate update cannot double-charge. A different session adds its own
    total. This also repairs the earlier sum-of-cumulative-totals accounting.
    """
    db = workbench.store.graph.db
    binding = db.execute('SELECT provider,cost_usd FROM browser_hosts WHERE task_id=?', (task_id,)).fetchone()
    if not binding or binding['provider'] != 'claude':
        return
    session, totals = '', {}
    for row in db.execute("SELECT label,detail FROM browser_actions WHERE task_id=? AND kind='host' ORDER BY created_at,id", (task_id,)):
        detail = json.loads(row['detail'])
        if row['label'] == 'Host connected':
            session = detail.get('session_id') or ''
        elif row['label'] == 'Host turn ended' and session:
            value = detail.get('cost_usd')
            if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                totals[session] = max(totals.get(session, 0), value)
    if totals:
        total = sum(totals.values())
        if abs(total - binding['cost_usd']) > 1e-9:
            db.execute('UPDATE browser_hosts SET cost_usd=? WHERE task_id=?', (total, task_id))
            workbench.record(task_id, 'usage', 'Session cost reconciled',
                             {'previous_total_usd': binding['cost_usd'], 'total_usd': total,
                              'session_totals_usd': totals})


def settings(provider):
    if provider not in ('claude', 'codex'):
        raise Invalid('Choose Claude Agent SDK or Codex app-server')
    prefix = 'RAVEN_HOST_' + provider.upper()
    credential = prefix + '_API_KEY'
    # Explicit host variables only: do not borrow Raven inference or CLI login.
    return {'provider': provider, 'key': os.environ.get(credential, '').strip(),
            'credential_env': credential, 'model': os.environ.get(prefix + '_MODEL', '').strip(),
            'workspace': os.environ.get(prefix + '_WORKSPACE_ID', '').strip()}


def capability(provider):
    cfg = settings(provider)
    problems = []
    if not cfg['key']:
        problems.append('Set ' + cfg['credential_env'] + ' on the server')
    if not cfg['model']:
        problems.append('Set RAVEN_HOST_' + provider.upper() + '_MODEL')
    version = ''
    if provider == 'claude':
        try:
            version = importlib.metadata.version('claude-agent-sdk')
        except importlib.metadata.PackageNotFoundError:
            pass
        if version != CLAUDE_SDK_VERSION:
            problems.append('Install requirements-hosts.txt in the server Python environment')
    else:
        binary = os.environ.get('RAVEN_CODEX_BIN') or shutil.which('codex')
        if binary:
            try:
                result = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=5)
                version = result.stdout.strip().removeprefix('codex-cli ')
            except (OSError, subprocess.TimeoutExpired):
                pass
        if version != CODEX_VERSION:
            problems.append('Configure RAVEN_CODEX_BIN with codex-cli ' + CODEX_VERSION)
    return {'id': provider, 'name': 'Claude Agent SDK' if provider == 'claude' else 'Codex app-server',
            'ready': not problems, 'blockers': problems, 'version': version, 'model': cfg['model'],
            'execution': 'Raven task tools; isolated commands',
            'containment': 'tools-only' if provider == 'claude' else 'advisory; read-only host sandbox',
            'session_location': 'Raven server, dedicated task home',
            'inference_destination': 'Anthropic API' if provider == 'claude' else 'OpenAI API',
            'live_verified': False}


READ_TOOLS = {'list_files', 'search_files', 'read_file', 'read_changes', 'bridge_get_tree',
              'bridge_get_decision', 'bridge_search_decisions', 'bridge_search_context',
              'bridge_list_owners', 'bridge_get_record', 'bridge_lookup_record',
              'bridge_connection_status', 'bridge_context_sources'}


def execute_once(workbench, task_id, call):
    """Claim before effect. Unknown writes cannot be retried automatically.

    This is deliberately not an exactly-once claim: an action can succeed before
    its receipt commits. Its intent survives and requires reconciliation.
    """
    db = workbench.store.graph.db
    fingerprint = hashlib.sha256(json.dumps([call['name'], call.get('input', {})], sort_keys=True).encode()).hexdigest()
    with workbench.store.graph.transaction():
        prior = db.execute('SELECT * FROM browser_intents WHERE task_id=? AND call_id=?',
                           (task_id, call['id'])).fetchone()
        if prior:
            if prior['fingerprint'] != fingerprint:
                raise Invalid('The host reused a tool call ID with different arguments')
            if prior['state'] == 'completed':
                return json.loads(prior['result'])
            # Even reads can update read receipts. Retrying here would also race
            # a live claimant; do not infer that the previous worker is dead.
            raise Invalid('This operation has an unresolved intent. Reconcile it before continuing.')
        db.execute('INSERT INTO browser_intents VALUES(?,?,?,?,?,?,?)',
                   (task_id, call['id'], fingerprint, 'pending', '{}', now(), now()))
        db.execute('INSERT INTO browser_intent_context VALUES(?,?,?,?)',
                   (task_id, call['id'], call['name'], json.dumps(call.get('input', {}))))
    workbench.assert_worker()
    try:
        from .visibility import require_task
        require_task(workbench.store, task_id)
        row = workbench.row(task_id)
        if row['status'] in ('paused', 'pausing', 'cancelled') or workbench.stop.is_set():
            result = {'error': 'Task interrupted before this operation; it did not run'}
        else:
            result = workbench.execute(row, call)
    except Invalid as error:
        result = {'error': str(error)}
    except BaseException:
        db.execute("UPDATE browser_intents SET state='unknown',updated_at=? WHERE task_id=? AND call_id=?",
                   (now(), task_id, call['id']))
        raise
    try:
        workbench.assert_worker()
        require_task(workbench.store, task_id)
    except Invalid:
        db.execute("UPDATE browser_intents SET state='unknown',updated_at=? WHERE task_id=? AND call_id=?",
                   (now(), task_id, call['id']))
        raise
    with workbench.store.graph.transaction():
        db.execute("UPDATE browser_intents SET state='completed',result=?,updated_at=? WHERE task_id=? AND call_id=?",
                   (json.dumps(result, default=str), now(), task_id, call['id']))
        db.execute('INSERT INTO browser_actions VALUES(?,?,?,?,?,?)',
                   (hashlib.sha256((task_id + ':' + call['id']).encode()).hexdigest(), task_id, 'tool', call['name'],
                    json.dumps({'call_id': call['id'], 'input': call.get('input'), 'result': result}, default=str), now()))
    return result


def reconcile(workbench, task_id, data, actor):
    """An operator records observed effects; this never executes or authorizes work."""
    from .visibility import require_task
    if actor is None or actor.role != 'admin' or actor.is_agent:
        raise Invalid('An authenticated workspace admin must reconcile this operation')
    note = str(data.get('note') or '').strip()
    outcome = data.get('outcome')
    if outcome not in ('completed', 'not_run') or not note or len(note) > 2000:
        raise Invalid('Choose an observed outcome and explain the evidence in up to 2,000 characters')
    require_task(workbench.store, task_id)
    db = workbench.store.graph.db
    with workbench.store.graph.transaction():
        row = workbench.row(task_id)
        host = db.execute('SELECT state FROM browser_hosts WHERE task_id=?', (task_id,)).fetchone()
        if row['status'] not in ('paused', 'cancelled') or host and host['state'] in ('active', 'starting'):
            raise Invalid('Stop the host and wait for its final receipt before reconciling')
        prior = db.execute('SELECT * FROM browser_intents WHERE task_id=? AND call_id=?',
                           (task_id, data.get('call_id'))).fetchone()
        if not prior or prior['state'] == 'completed':
            raise Invalid('This operation has no unresolved outcome')
        result = {'reconciled': True, 'observed_outcome': outcome, 'operator_id': actor.id,
                  'operator': actor.name, 'evidence': note,
                  'notice': 'Operator attestation, not an execution receipt or approval. Read current task state and files before continuing; do not repeat an operation observed as completed.'}
        db.execute("UPDATE browser_intents SET state='completed',result=?,updated_at=? WHERE task_id=? AND call_id=?",
                   (json.dumps(result), now(), task_id, data['call_id']))
        workbench.record(task_id, 'control', 'Operation reconciled', {'call_id': data['call_id'], **result})
    return {'recorded': True, 'status': row['status'], 'next': 'Review the saved work, then explicitly continue the task.'}


class Harness:
    def run(self, workbench, task_id, prompt):
        from .workbench import SYSTEM, TOOLS
        db = workbench.store.graph.db
        from .visibility import require_task
        require_task(workbench.store, task_id)
        reconcile_cost(workbench, task_id)
        binding = dict(db.execute('SELECT * FROM browser_hosts WHERE task_id=?', (task_id,)).fetchone())
        cfg, cap = settings(binding['provider']), capability(binding['provider'])
        if not cap['ready']:
            raise Invalid('; '.join(cap['blockers']))
        if db.execute("SELECT 1 FROM browser_intents WHERE task_id=? AND state<>'completed'", (task_id,)).fetchone():
            raise Invalid('An earlier tool operation has an unknown outcome. Reconcile it before resuming.')
        budget = float(os.environ.get('RAVEN_HOST_TASK_BUDGET_USD', '5'))
        if budget <= 0 or binding['cost_usd'] >= budget:
            raise Invalid('The host task budget is exhausted. An operator must explicitly increase it to continue.')
        home = workbench.root / '.hosts' / task_id / binding['provider']
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        empty = home / 'empty'
        empty.mkdir(exist_ok=True)
        env = {k: os.environ[k] for k in ('PATH', 'LANG', 'SSL_CERT_FILE', 'SSL_CERT_DIR') if k in os.environ}
        env.update(HOME=str(home), CODEX_HOME=str(home / 'codex'), CLAUDE_CONFIG_DIR=str(home / 'claude'),
                   PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONUNBUFFERED='1')
        if cfg['provider'] == 'claude':
            env['ANTHROPIC_API_KEY'] = cfg['key']
            if cfg['workspace']:
                env['ANTHROPIC_CUSTOM_HEADERS'] = 'anthropic-workspace-id: ' + cfg['workspace']
        else:
            env['OPENAI_API_KEY'] = cfg['key']
            env['RAVEN_CODEX_BIN'] = os.environ.get('RAVEN_CODEX_BIN') or shutil.which('codex') or ''
        process = subprocess.Popen([sys.executable, '-m', 'bridge.harness_worker'], cwd=empty, env=env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        events = queue.Queue()
        def read():
            try:
                for line in process.stdout:
                    events.put(json.loads(line))
            except (ValueError, OSError):
                events.put({'kind': 'error', 'code': 'invalid_host_protocol'})
            finally:
                events.put(None)
        threading.Thread(target=read, daemon=True).start()
        def send(value):
            process.stdin.write(json.dumps(value) + '\n')
            process.stdin.flush()
        db.execute("UPDATE browser_hosts SET state='starting',version=?,updated_at=? WHERE task_id=?",
                   (cap['version'], now(), task_id))
        send({'provider': cfg['provider'], 'model': binding['model'], 'session_id': binding['session_id'],
              'cwd': str(empty), 'prompt': prompt, 'system': SYSTEM, 'tools': TOOLS,
              'max_turns': workbench.max_steps, 'budget_usd': budget - binding['cost_usd']})
        deadline = time.monotonic() + float(os.environ.get('RAVEN_HOST_TURN_TIMEOUT', '900'))
        interrupt_at = None
        finished = False
        calls = 0
        access_check_at = 0
        try:
            while True:
                row = workbench.row(task_id)
                workbench.assert_worker()
                if time.monotonic() >= access_check_at:
                    require_task(workbench.store, task_id)
                    access_check_at = time.monotonic() + 1
                if (workbench.stop.is_set() or row['status'] in ('pausing', 'paused', 'cancelled', 'waiting', 'completed')
                        or time.monotonic() > deadline) and interrupt_at is None:
                    send({'kind': 'interrupt'})
                    interrupt_at = time.monotonic()
                if interrupt_at and time.monotonic() - interrupt_at > 10:
                    raise Invalid('The host did not acknowledge interruption. Its session is saved; inspect before resuming.')
                try:
                    event = events.get(timeout=.1)
                except queue.Empty:
                    continue
                if event is None:
                    break
                kind = event.get('kind')
                if kind == 'tool':
                    calls += 1
                    if calls > workbench.max_steps:
                        raise Invalid('The host reached its tool-call budget. Review progress before continuing.')
                    result = execute_once(workbench, task_id, event['call'])
                    send({'kind': 'tool_result', 'id': event['call']['id'], 'result': result})
                elif kind == 'session':
                    db.execute("UPDATE browser_hosts SET session_id=?,state='active',updated_at=? WHERE task_id=?",
                               (event['id'], now(), task_id))
                    workbench.record(task_id, 'host', 'Host connected', {'provider': cfg['provider'], 'session_id': event['id']})
                elif kind == 'text':
                    require_task(workbench.store, task_id)
                    messages = json.loads(workbench.row(task_id)['messages'])
                    messages.append({'role': 'assistant', 'content': [{'type': 'text', 'text': event['text']}]})
                    workbench.update(task_id, messages=json.dumps(messages))
                elif kind == 'result':
                    finished = True
                    workbench.record(task_id, 'host', 'Host turn ended', event)
                    reconcile_cost(workbench, task_id)
                    db.execute('UPDATE browser_hosts SET state=?,turns=turns+1,updated_at=? WHERE task_id=?',
                               ('interrupted' if interrupt_at else event.get('status', 'stopped'), now(), task_id))
                    if event.get('is_error') and interrupt_at is None:
                        raise Invalid('The coding host ended with ' + str(event.get('status')) + '. Your session and work are saved.')
                elif kind == 'usage':
                    workbench.record(task_id, 'host', 'Host usage', event)
                elif kind == 'error':
                    raise Invalid('The selected coding host failed (' + str(event.get('code', 'unknown')) + '). No alternate provider was used.')
            if not finished:
                raise Invalid('The host disconnected without a final receipt. Inspect the saved session before continuing.')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdin.close()
            process.stdout.close()
            if not finished:
                db.execute("UPDATE browser_hosts SET state='stopped',updated_at=? WHERE task_id=?", (now(), task_id))
