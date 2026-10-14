"""Isolated synthetic authenticated-inbox QA server. No external providers."""
import json
import os
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from unittest.mock import patch
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import fixtures  # noqa: F401,E402
from bridge import briefing, canvas, source_access  # noqa: E402
from bridge.accounts import Accounts  # noqa: E402
from bridge.auth import Auth, Identity  # noqa: E402
from bridge.config import Config  # noqa: E402
from bridge.server import make_server  # noqa: E402
from bridge.store import Store  # noqa: E402

os.environ.update(BRIDGE_MODEL_API='none', BRIDGE_SEMANTIC='0', BRIDGE_LIVE='0')
for key in ('ANTHROPIC_API_KEY', 'RAVEN_HOST_CODEX_API_KEY', 'RAVEN_HOST_CLAUDE_API_KEY'):
    os.environ.pop(key, None)
for target in ('bridge.llm.Client.complete', 'bridge.llm.Client.complete_json', 'bridge.github.GitHubAPI.get', 'urllib.request.urlopen'):
    patch(target, side_effect=AssertionError('External calls forbidden in synthetic expert QA')).start()

class World:
    def __init__(self, root, label):
        self.store = Store(root / (label + '.db'))
        self.g = self.store.graph
        self.g.set_setting('workspace_name', 'Synthetic tenant ' + label)
        self.auth = Auth(self.store, enabled=True)
        self.accounts = Accounts(self.auth)
        self.admin = self.store.add_person({'name': 'Synthetic Admin', 'email': 'admin@example.test', 'role': 'admin'})
        self.expert = self.store.add_person({'name': 'Synthetic Expert', 'email': 'expert@example.test', 'role': 'member'})
        self.peer = self.store.add_person({'name': 'Synthetic Peer', 'email': 'peer@example.test', 'role': 'member'})
        self.viewer = self.store.add_person({'name': 'Synthetic Viewer', 'email': 'viewer@example.test', 'role': 'viewer'})
        self.actor = Identity(self.admin['id'], self.admin['name'], 'admin', 'session')
        self.invite = self.accounts.invite(self.expert['email'], 'member', self.actor)['token']
        self.expired = self.accounts.invite('expired@example.test', 'member', self.actor)['token']
        self.g.db.execute('UPDATE account_invites SET expires_at=? WHERE token_hash=?', (int(time.time())-60, self.auth._hash(self.expired)))
        self.password = 'synthetic-expert-password-2026'
        for person in (self.peer, self.viewer):
            token = self.accounts.invite(person['email'], person['role'], self.actor)['token']
            self.accounts.accept({'invite': token, 'name': person['name'], 'password': self.password})
        self.repo = 'synthetic/' + label.lower()
        self.task = canvas.start_task(self.store, Config(model_api='none'), {'title': 'Review synthetic export retention', 'goal': 'TENANT_' + label + '_PRIVATE_GOAL', 'repo': self.repo, 'requester': 'Synthetic Requester'})['task_id']
        self.node = self.g.add_decision(self.task, 'Keep synthetic export copies for seven days?', 'policy', 'pending', owner=self.expert['name'], repo=self.repo, context='SYNTHETIC_TASK_CONTEXT: Only test exports. Production records remain unchanged.')
        canvas.settle_node(self.store, {'task_id': self.task, 'node_id': self.node, 'answer': 'Keep test export copies for seven days.', 'rationale': 'Synthetic retention policy; production remains unchanged.'})
        other = canvas.start_task(self.store, Config(model_api='none'), {'title': 'UNRELATED_TASK_CANARY', 'repo': self.repo})['task_id']
        self.other_node = self.g.add_decision(other, 'UNRELATED_DECISION_CANARY', '', 'pending', owner=self.peer['name'])
        self.g.db.execute('INSERT INTO source_connections(id,kind,repo,target,options,created_at) VALUES(?,?,?,?,?,?)', ('synthetic-jira','jira',self.repo,'QA','{"host":"fixture.invalid"}', source_access.stamp()))
        self.source = self.store.add_record({'repo': self.repo, 'provider': 'jira', 'namespace': 'fixture.invalid', 'kind': 'jira', 'external_id': '1001', 'ref': 'QA-1', 'title': 'Revocable fixture source', 'body': 'REVOKED_SOURCE_CANARY: internal staging allowance 631.', 'access_scope':'{"jira_project":"QA","audience":"workspace-shared"}'})['source']
        source_access.observe(self.store, 'synthetic-jira', self.source['record_id'])
        self.restricted_task = canvas.start_task(self.store, Config(model_api='none'), {'title':'Connected source review', 'repo': self.repo})['task_id']
        self.restricted_node = self.g.add_decision(self.restricted_task, 'What staging allowance applies?', 'policy', 'pending', repo=self.repo, owner=self.expert['name'])
        row = dict(self.g.db.execute('SELECT i.*,s.id AS record_id,s.head_id AS source_version_id FROM intents i JOIN source_records s ON s.intent_id=i.id WHERE s.id=?',(self.source['record_id'],)).fetchone())
        self.g.publish_evidence(self.restricted_node,[row],status='resolved',source='record',answer='REVOKED_SOURCE_CANARY: internal staging allowance 631.',kind='evidence',signoff='required')
        with self.g.transaction():
            self.legacy = briefing.mint(self.g,self.expert['id'],self.task,self.node)
            self.expired_legacy = briefing.mint(self.g,self.expert['id'],self.task,self.node)
            self.g.db.execute('UPDATE brief_links SET expires_at=? WHERE token_hash=?',(int(time.time())-60,briefing._hash(self.expired_legacy)))
        self.server = make_server(self.store,port=0,auth=self.auth)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        assert not self.store.delivery.enabled
    def state(self):
        return {k:getattr(self,k) for k in ('task','node','other_node','restricted_task','restricted_node','invite','expired','legacy','expired_legacy','password')} | {'base':f'http://127.0.0.1:{self.server.server_port}', 'expert':self.expert, 'peer':self.peer, 'viewer':self.viewer}

root = Path(sys.argv[1]); root.mkdir(parents=True,exist_ok=True)
worlds = {key:World(root,key) for key in ('A','B')}
class Control(BaseHTTPRequestHandler):
    def do_GET(self):
        q=parse_qs(urlsplit(self.path).query); w=worlds[q.get('tenant',['A'])[0]]; action=q.get('action',['state'])[0]
        if action=='revoke_expert': w.g.db.execute('UPDATE people SET active=0 WHERE id=?',(w.expert['id'],))
        if action=='revoke_source': source_access.observe(w.store,'synthetic-jira',w.source['record_id'],state='revoked')
        if action=='decision': result=w.store.get_decision(q.get('id',[w.node])[0])
        elif action=='counts': result={t:w.g.db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('runs','decisions','host_sessions','notifications')}
        else: result={'ok':True}
        data=json.dumps(result).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def log_message(self,*args): pass
control=ThreadingHTTPServer(('127.0.0.1',0),Control)
print(json.dumps({key:w.state() for key,w in worlds.items()} | {'control':f'http://127.0.0.1:{control.server_port}'}),flush=True)
control.serve_forever()
