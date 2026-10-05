"""Opt-in real-inference Slack loop on an existing OSS checkout.

Run with --repo /path/to/urllib3 --output /tmp/raven-live-new-directory.
Uses your configured real inference backend, real ingestion and signed HTTP callbacks.
Slack's directory and messages are simulated; no real people are contacted.
"""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bridge.ingest import index_repo
from bridge.mcp import call_tool
from bridge.server import make_server
from bridge.store import Store, Invalid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    authors = subprocess.check_output(['git', '-C', args.repo, 'log', '-500', '--format=%aN%x09%aE'], text=True).splitlines()
    people = {}
    for line in authors:
        name, email = line.split('\t', 1)
        if '[bot]' not in name:
            people.setdefault(email, name)
    members = [{'id': f'U{i:05}', 'team_id': 'TLIVE', 'real_name': name,
                'profile': {'real_name': name, 'email': email}} for i, (email, name) in enumerate(people.items())]
    messages, trace, checks = [], [], []

    class Slack:
        name = 'slack'
        def workspace_id(self): return 'TLIVE'
        def list_users(self): return members
        def user_info(self, uid): return next(x for x in members if x['id'] == uid)
        def open_dm(self, uid): return 'D' + uid
        def post_message(self, channel, text, blocks=None, thread_ts=''):
            ts = f'{int(time.time())}.{len(messages):06}'
            messages.append({'channel': channel, 'text': text, 'ts': ts, 'thread_ts': thread_ts})
            return ts

    os.environ['BRIDGE_MODEL_API'] = 'anthropic' if os.environ.get('ANTHROPIC_API_KEY') else 'claude-cli'
    os.environ['BRIDGE_SEMANTIC'] = '0'
    os.environ['BRIDGE_DEEP'] = '0'
    store = Store(out / 'workspace.db')
    store.graph.set_setting('workspace_name', 'Isolated natural conversation evaluation')
    print('Ingesting public repository history', flush=True)
    index_repo(store.graph, args.repo, max_commits=500, repo_name='urllib3/urllib3')
    slack = Slack()
    secret = secrets.token_hex(24)
    server = None

    def start():
        nonlocal server
        store.connect_delivery(slack, fallback_channel='CTRIAGE')
        store.delivery.sync_directory()
        server = make_server(store, port=0, slack_signing_secret=secret)
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def check(name, passed, detail=None):
        checks.append({'check': name, 'passed': bool(passed), 'detail': detail})
        print(('PASS ' if passed else 'FAIL ') + name, flush=True)
        save()
        if not passed:
            raise AssertionError(name)

    def save():
        (out / 'transcript.json').write_text(json.dumps({'checks': checks, 'calls': trace, 'messages': messages}, indent=2))

    def call(name, **kwargs):
        # Keep ladder decisions deterministic to isolate live conversation behavior.
        os.environ['BRIDGE_SEMANTIC'] = '0'
        try:
            result = call_tool(store, name, kwargs)
            trace.append({'tool': name, 'args': kwargs, 'result': result})
            return result
        finally:
            os.environ['BRIDGE_SEMANTIC'] = '1'

    def say(msg, uid, text, repeat=False):
        eid = secrets.token_hex(12)
        payload = {'type': 'event_callback', 'event_id': eid, 'event': {'type': 'message',
            'channel': msg['channel'], 'thread_ts': msg.get('thread_ts') or msg['ts'], 'user': uid, 'text': text}}
        body = json.dumps(payload).encode()
        timestamp = str(int(time.time()))
        signature = 'v0=' + hmac.new(secret.encode(), b'v0:' + timestamp.encode() + b':' + body, hashlib.sha256).hexdigest()
        headers = {'Content-Type': 'application/json', 'X-Slack-Request-Timestamp': timestamp, 'X-Slack-Signature': signature}
        tick = time.monotonic()
        for _ in range(2 if repeat else 1):
            with urlopen(Request(f'http://127.0.0.1:{server.server_port}/webhooks/slack', data=body, headers=headers)) as response:
                assert response.status == 200
        elapsed = time.monotonic() - tick
        check('Webhook acknowledges before inference: ' + text[:35], elapsed < 3, round(elapsed, 3))
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            row = store.graph.db.execute('SELECT state FROM slack_ingress WHERE id=?', (eid,)).fetchone()
            if row and row['state'] == 'done':
                ack = store.graph.db.execute('SELECT text FROM slack_replies WHERE id=?', (eid,)).fetchone()
                result = ack['text'] if ack else ''
                trace.append({'human': text, 'raven': result})
                print('Raven:', result, flush=True)
                save()
                return result
            time.sleep(.25)
        raise AssertionError('Slack event did not finish; see saved database')

    try:
        start()
        task = call('bridge_start_task', title='Cap retry delays without breaking compatibility',
                    goal='Add an optional retry delay cap. Preserve existing defaults and keep server Retry-After semantics explicit.',
                    repo='urllib3/urllib3', client_key='natural-one', paths='src/urllib3/util/retry.py')['task_id']
        question = 'Should the retry delay cap apply to Retry-After as well as exponential backoff?'
        node = call('bridge_add_node', task_id=task, question=question, paths='src/urllib3/util/retry.py', client_ref='cap',
                    context='This is an opt-in cap; existing applications keep their current behavior. A server may request 120 seconds with Retry-After.')
        store.delivery.deliver_now()
        msg = next(m for m in messages if question in m['text'])
        uid = msg['channel'][1:]
        check('Initial owner inferred from OSS without a manual map', bool(node['owner']) and not store.graph.authority_rows(), node['owner'])
        ack = say(msg, uid, 'Why do we need a decision here? What behavior do we need to preserve?')
        check('Context question leaves decision unsigned', not store.get_decision(node['node_id'])['authorized'] and 'No decision' in ack)
        next_member = next(x for x in members if x['id'] != uid)
        ack = say(msg, uid, f"I worked on the implementation, but <@{next_member['id']}> makes this compatibility call. Could you ask them?")
        check('Natural referral is read back first', 'Pass this question' in ack and store.get_decision(node['node_id'])['owner_name'] == node['owner'])
        say(msg, uid, 'yes')
        store.delivery.deliver_now()
        msg = next(m for m in reversed(messages) if question in m['text'] and m['channel'] == 'D' + next_member['id'])
        uid = next_member['id']
        check('Referred person receives a DM', msg['channel'] == 'D' + uid)
        ack = say(msg, uid, "I'm away today, so I can't make this call yet.")
        check('Non-answer leaves finish blocked', not store.get_decision(node['node_id'])['authorized'])
        ack = say(msg, uid, 'Keep Retry-After outside the cap. The server explicitly requested that delay; cap only exponential backoff.')
        check('Natural answer is read back without authorization', 'Retry-After' in ack and 'yes' in ack and not store.get_decision(node['node_id'])['authorized'])
        ack = say(msg, uid, 'Actually, one qualification: zero must disable the backoff cap, not disable retries. Keep the Retry-After part as stated.')
        check('Correction retains both requirements', 'zero' in ack.lower() and 'Retry-After' in ack and 'Nothing applied' in ack)
        ack = say(msg, uid, 'ok')
        check('Casual acknowledgement is not a signature', not store.get_decision(node['node_id'])['authorized'])
        server.shutdown(); server.server_close(); store.graph.close()
        store = Store(out / 'workspace.db')
        start()
        ack = say(msg, uid, 'yes', repeat=True)
        d = store.get_decision(node['node_id'])
        check('Restart and duplicate delivery preserve one confirmed answer', d['authorized'] and 'zero' in d['answer'].lower(), d['answer'])
        call('bridge_wait', task_id=task, timeout='0')
        call('bridge_get_tree', task_id=task)
        done = call('bridge_finish_task', task_id=task, checks='Conversation and routing evaluation; no implementation patch.')
        check('Host receives the decision and can finish honestly', done.get('verified') is False)
        sibling = call('bridge_start_task', title='Reuse retry cap policy', repo='urllib3/urllib3', client_key='natural-two', paths='src/urllib3/util/retry.py')['task_id']
        reuse = call('bridge_add_node', task_id=sibling, question=question, paths='src/urllib3/util/retry.py', client_ref='same-policy')
        check('A new task reaches the person who ultimately answered', reuse['owner'] == next_member['real_name'], reuse['owner'])
        reused = store.get_decision(reuse['node_id'])
        check('Answer is reused but needs a fresh signature', bool(reused['answer']) and not reused['authorized'], reused['answer'])
        check('No personal Raven accounts were created', store.graph.db.execute('SELECT count(*) FROM account_passwords').fetchone()[0] == 0)
        print('Evidence:', out / 'transcript.json', flush=True)
    finally:
        save()
        if server:
            server.shutdown(); server.server_close()
        store.graph.close()


if __name__ == '__main__':
    main()
