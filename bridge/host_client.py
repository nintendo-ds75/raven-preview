"""Standalone, standard-library host adapter, copied privately by setup.

Run hooks, inspect capabilities, capture a complete diff or explicitly supervise
an idle session. No host permissions are bypassed and no remote command is run.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.request import Request, HTTPRedirectHandler, build_opener
from urllib.parse import urlsplit
import uuid

HOOK_BUDGET_SECONDS = 45
HOOK_TIMEOUT_MESSAGE = ('Raven task registration did not finish before its deadline. '
    'Retry the same prompt after checking Raven. '
    'An interrupted registration will recover the same task.')


def remaining(deadline, ceiling=55):
    if deadline is None:
        return ceiling
    left = deadline - time.monotonic()
    if left <= 0:
        raise ValueError(HOOK_TIMEOUT_MESSAGE)
    return min(ceiling, left)


def bounded_hook(operation, *, budget=None):
    """Return a blocking failure before the host's 60-second hook timeout.

    Socket timeouts alone do not bound DNS or a slow response body. The
    command's main thread enforces the total deadline; its worker is a daemon
    so a stalled operation cannot keep the hook process alive after exit 2.
    A server may still finish an interrupted request. Its stable session/event
    keys must therefore be reused on retry, never replaced here.
    """
    seconds = HOOK_BUDGET_SECONDS if budget is None else budget
    if not isinstance(seconds, (int, float)) or not 0 < seconds <= HOOK_BUDGET_SECONDS:
        raise ValueError('The hook budget must be positive and below the host timeout')
    deadline = time.monotonic() + seconds
    results, failures = [], []

    def run():
        try:
            results.append(operation(deadline))
        except Exception as error:
            failures.append(error)

    worker = threading.Thread(target=run, name='raven-hook-request', daemon=True)
    worker.start()
    worker.join(max(0, deadline - time.monotonic()))
    if worker.is_alive() or time.monotonic() >= deadline:
        raise ValueError(HOOK_TIMEOUT_MESSAGE)
    if failures:
        error = failures[0]
        if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
            raise ValueError(HOOK_TIMEOUT_MESSAGE) from None
        raise error
    return results[0]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def rpc(config, method, params=None, *, deadline=None):
    payload = {'jsonrpc': '2.0', 'id': uuid.uuid4().hex, 'method': method, 'params': params or {}}
    parts = urlsplit(config['url'])
    if (parts.scheme not in ('http', 'https') or parts.username or parts.password or parts.fragment or parts.query
            or (parts.scheme == 'http' and parts.hostname not in ('localhost', '127.0.0.1'))):
        raise ValueError('Raven requires HTTPS or a local HTTP endpoint')
    request = Request(config['url'], data=json.dumps(payload).encode(), headers={
        'Content-Type': 'application/json', 'Accept': 'application/json',
        'MCP-Protocol-Version': '2025-03-26', 'Authorization': 'Bearer ' + config['token']})
    with build_opener(NoRedirect).open(request, timeout=remaining(deadline)) as response:
        raw = response.read(4_000_001)
    remaining(deadline)
    if len(raw) > 4_000_000:
        raise ValueError('Raven response exceeds the local adapter limit')
    value = json.loads(raw)
    if value.get('error'):
        raise ValueError('Raven rejected the protocol request')
    return value['result']


def call(config, name, args, *, deadline=None):
    value = rpc(config, 'tools/call', {'name': name, 'arguments': args}, deadline=deadline)
    texts = [c['text'] for c in value.get('content', []) if c.get('type') == 'text']
    if value.get('isError'):
        raise ValueError('\n'.join(texts))
    if not texts:
        raise ValueError('Raven returned no tool result')
    return json.loads(texts[0])


def binding(config, host, session, event, **extra):
    if not isinstance(session, str) or not session or session.startswith('-') or len(session) > 200:
        raise ValueError('A valid exact host session ID is required')
    return {'event': event, 'host': host, 'session_id': session, 'project': config['project'],
            'repo': config['repo'], **extra}


def verify_project(config, cwd, *, deadline=None):
    root = Path(config['project']).resolve()
    work = Path(cwd).resolve()
    actual = subprocess.check_output(['git', '-C', str(work), 'rev-parse', '--show-toplevel'],
        stderr=subprocess.PIPE, timeout=None if deadline is None else remaining(deadline)).decode().strip()
    remaining(deadline)
    if Path(actual).resolve() != root:
        raise ValueError('This hook belongs to another checkout; reconnect Raven in this project')


def capabilities(config, *, deadline=None):
    rpc(config, 'initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
        'clientInfo': {'name': 'raven-host-adapter', 'version': '1'}}, deadline=deadline)
    found = rpc(config, 'tools/list', deadline=deadline)['tools']
    required = {'bridge_host_event', 'bridge_start_task', 'bridge_get_tree', 'bridge_finish_task'}
    if not required <= {t['name'] for t in found}:
        raise ValueError('This Raven server does not support the installed host adapter')
    return [t['name'] for t in found]


def hook(config, host, payload, *, deadline=None):
    verify_project(config, payload.get('cwd', config['project']), deadline=deadline)
    event_name = payload.get('hook_event_name')
    session = payload.get('session_id')
    if event_name not in ('SessionStart', 'UserPromptSubmit'):
        raise ValueError('Unsupported hook event')
    tools = capabilities(config, deadline=deadline)
    reported = list(tools)
    for filename in ('.mcp.json', '.cursor/mcp.json'):
        path = Path(config['project']) / filename
        if path.exists():
            configured = json.loads(path.read_text()).get('mcpServers', {})
            reported.extend('server:' + name for name in configured if isinstance(name, str) and len(name) < 150)
    action, prompt = 'connect', ''
    if event_name == 'UserPromptSubmit':
        action, prompt = 'prompt', payload.get('prompt', '')
        for prefix in ('Raven new task: ', '/raven-new '):
            if prompt.startswith(prefix):
                action, prompt = 'new_task', prompt[len(prefix):]
                break
    result = call(config, 'bridge_host_event', binding(config, host, session, action,
        prompt=prompt, tools=reported[:200],
        event_key=str(payload.get('turn_id') or hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest())),
        deadline=deadline)
    # Source and human text is intentionally not elevated into hook/developer
    # instructions. The host reads that untrusted content through normal tools.
    text = ('Raven connection verified. Available tools: ' + ', '.join(tools) + '. ')
    if result['task_id']:
        text += ('Task already registered as ' + result['task_id'] + '. Do not create another task for this prompt. '
                 'Read bridge_get_tree for current decisions and evidence. Discover the relevant files yourself. '
                 'Use this task_id when adding decisions and finishing. ')
    text += 'Start a separate task in this session with the plain-text prefix Raven new task: followed by the request.'
    return {'hookSpecificOutput': {'hookEventName': event_name, 'additionalContext': text}}


def finish(config, task_id, base, checks):
    if not base:
        raise ValueError('Supply --base with the commit before this task began; HEAD can omit work the agent already committed')
    verify_project(config, os.getcwd())
    root = config['project']
    sha = subprocess.check_output(['git', '-C', root, 'rev-parse', '--verify', base + '^{commit}'], stderr=subprocess.PIPE).decode().strip()
    untracked = subprocess.check_output(['git', '-C', root, 'ls-files', '--others', '--exclude-standard'], stderr=subprocess.PIPE)
    if untracked:
        raise ValueError('Stage intended new files before finishing; untracked files would be absent from the diff')
    diff = subprocess.check_output(['git', '-C', root, 'diff', '--binary', '--no-ext-diff', '--no-textconv', sha, '--'], stderr=subprocess.PIPE)
    # UTF-8 is the existing MCP contract. Refuse rather than rewrite unknown bytes.
    text = diff.decode('utf-8')
    return call(config, 'bridge_finish_task', {'task_id': task_id, 'diff': text,
        'diff_sha256': hashlib.sha256(diff).hexdigest(), 'checks': checks})


def resume_command(host, session, prompt):
    if not session or session.startswith('-'):
        raise ValueError('An exact session ID is required')
    if host == 'claude':
        return ['claude', '--print', '--resume', session, '--permission-mode', 'default', prompt]
    if host == 'codex':
        return ['codex', 'exec', 'resume', session, prompt]
    raise ValueError('Unsupported coding host')


def watch_once(config, host, session, worker, run=subprocess.run):
    verify_project(config, config['project'])
    args = binding(config, host, session, 'poll', worker=worker)
    result = call(config, 'bridge_host_event', args)
    message = result.get('message')
    if not message:
        return result
    try:
        completed = run(resume_command(host, session, message['prompt']), cwd=config['project'], timeout=1200, check=False)
        success = completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        success = False
    call(config, 'bridge_host_event', {**args, 'event': 'ack' if success else 'release', 'message_id': message['id']})
    return {'resumed': success, 'message_id': message['id']}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['hook', 'status', 'finish', 'watch'])
    p.add_argument('--config', default=str(Path(__file__).with_name('host-config.json')))
    p.add_argument('--host', choices=['claude', 'codex'], default='claude')
    p.add_argument('--session')
    p.add_argument('--task')
    p.add_argument('--base', help='Required for finish: exact commit before the task began')
    p.add_argument('--checks', default='')
    p.add_argument('--allow-background', action='store_true')
    args = p.parse_args()
    try:
        if args.action == 'hook':
            raw = sys.stdin.read(200_001)
            if len(raw) > 200_000: raise ValueError('Hook input is too large')
            def read_and_hook(deadline):
                config = json.loads(Path(args.config).read_text())
                return hook(config, args.host, json.loads(raw), deadline=deadline)
            result = bounded_hook(read_and_hook)
        else:
            config = json.loads(Path(args.config).read_text())
        if args.action == 'status':
            result = {'tools': capabilities(config), 'connection': call(config, 'bridge_connection_status', {})}
        elif args.action == 'finish':
            if not args.task: raise ValueError('--task is required')
            result = finish(config, args.task, args.base, args.checks)
        elif args.action == 'watch':
            if not args.allow_background or not args.session:
                raise ValueError('Background resume requires --allow-background and an exact --session; stop the interactive host first')
            worker = uuid.uuid4().hex
            print('Watching this idle session with its normal permissions. Ctrl-C stops background resume.', flush=True)
            while True:
                result = watch_once(config, args.host, args.session, worker)
                print(json.dumps(result), flush=True)
                time.sleep(15)
        print(json.dumps(result))
    except KeyboardInterrupt:
        return
    except Exception as error:
        # Avoid URLs, HTTP bodies, environment values or tracebacks with secrets.
        message = str(error) if isinstance(error, ValueError) else 'Raven connection or host operation failed; check the local connection with status'
        print(message, file=sys.stderr)
        if args.action == 'hook':
            print(json.dumps({'decision': 'block', 'reason': message}))
        raise SystemExit(2)


if __name__ == '__main__':
    main()
