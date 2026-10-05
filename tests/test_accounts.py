import json
import re
import time
from urllib.parse import urlencode
from unittest.mock import patch

from test_auth import SharedServer, BOOTSTRAP
from bridge.accounts import Accounts
from bridge.store import Invalid


class AccountFlowTests(SharedServer):
    workspace_ready = False
    def setUp(self):
        super().setUp()
        self.accounts = Accounts(self.auth)
        self.owner_data = dict(workspace='Test workspace', name='Owner', email='owner@example.test',
                               password='long-private-password', setup_token=BOOTSTRAP)

    def form(self, path, data, cookie=''):
        _, _, body = self.raw('GET', path if path in ('/auth/setup', '/auth/profile') else '/auth/join')
        csrf = re.search(rb'name="csrf" value="([^"]+)"', body).group(1).decode()
        return self.raw('POST', path, {'Content-Type': 'application/x-www-form-urlencoded', 'Cookie': cookie},
                        urlencode(dict(csrf=csrf, **data)))

    def setup_owner(self):
        status, headers, _ = self.form('/auth/setup', self.owner_data)
        self.assertEqual(status, 302)
        self.assertEqual(headers['Location'], '/auth/profile')
        cookie = headers['Set-Cookie'].split(';')[0]
        status, headers, _ = self.form('/auth/profile', self.owner_data, cookie=cookie)
        self.assertEqual(status, 302)
        self.assertEqual(headers['Location'], '/#connect')
        return headers['Set-Cookie'].split(';')[0]

    def test_full_owner_invite_returning_login_flow(self):
        cookie = self.setup_owner()
        state = self.get('/api/state', cookie=cookie)
        self.assertEqual(state['workspace']['name'], 'Test workspace')
        self.assertEqual(state['me']['role'], 'admin')
        # The decision card and the inbox read these; they must follow the saved settings.
        self.assertEqual(state['settings'], {'auto_rules': False, 'overdue_hours': 72})
        self.post('/api/settings', {'auto_rules': True, 'overdue_hours': 24}, cookie=cookie, csrf=state['csrf_token'])
        self.assertEqual(self.get('/api/state', cookie=cookie)['settings'], {'auto_rules': True, 'overdue_hours': 24})
        invitation = self.post('/api/invitations', {'email': 'member@example.test', 'role': 'member'},
                               cookie=cookie, csrf=state['csrf_token'])
        token = invitation['url'].split('#invite=')[1]
        status, headers, _ = self.form('/auth/join', dict(invite=token, name='Teammate', password='another-long-password'))
        self.assertEqual(status, 302)
        member_cookie = headers['Set-Cookie'].split(';')[0]
        member = self.get('/api/state', cookie=member_cookie)
        self.assertEqual(member['me']['role'], 'member')
        self.assertEqual(self.status_of('POST', '/api/invitations', {'email': 'x@example.test'},
                        cookie=member_cookie, csrf=state['csrf_token']), 403)
        status, _, _ = self.form('/auth/join', dict(invite=token, name='Replay', password='another-long-password'))
        self.assertEqual(status, 400)
        status, headers, _ = self.form('/auth/password', dict(email='member@example.test', password='another-long-password'))
        self.assertEqual(status, 302)
        self.assertEqual(headers['Location'], '/#inbox')
        self.assertEqual(self.get('/api/state', cookie=headers['Set-Cookie'].split(';')[0])['me']['id'], member['me']['id'])
        saved = self.store.graph.db.execute('SELECT password_hash FROM account_passwords').fetchall()
        self.assertTrue(all('password' not in row['password_hash'] for row in saved))
        invite = self.store.graph.db.execute('SELECT token_hash FROM account_invites').fetchone()
        self.assertNotEqual(invite['token_hash'], token)

    def test_agent_activity_is_polled_private_and_excludes_revoked_credentials(self):
        cookie = self.setup_owner()
        owner = self.get('/api/state', cookie=cookie)['me']['id']
        agent = self.auth.create_token(owner, label='Codex')
        other = self.store.graph.add_person('Other', role='member')
        self.auth.create_token(other, label='Private other agent')
        state = self.get('/api/state', cookie=cookie)
        self.assertEqual([t['label'] for t in state['agent_connections']], ['Codex'])
        self.assertFalse(state['agent_connections'][0]['last_used_at'])
        self.assertEqual(self.status_of('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}}, token=agent['token']), 200)
        state = self.get('/api/state', cookie=cookie)
        self.assertTrue(state['agent_connections'][0]['last_used_at'])
        self.assertNotIn(agent['token'], json.dumps(state))
        self.auth.revoke_token(agent['id'])
        self.assertEqual(self.get('/api/state', cookie=cookie)['agent_connections'], [])

    def test_claim_requires_admin_and_can_only_happen_once(self):
        status, _, _ = self.form('/auth/setup', dict(self.owner_data, setup_token='invalid'))
        self.assertEqual(status, 400)
        self.assertFalse(self.accounts.workspace())
        self.setup_owner()
        with self.assertRaises(Invalid):
            self.accounts.setup(self.owner_data, self.auth.identify({'Authorization': 'Bearer ' + BOOTSTRAP}))

    def test_workspace_and_profile_are_separate_and_gate_all_access(self):
        status, _, body = self.raw('GET', '/auth/setup')
        self.assertEqual(status, 200)
        self.assertNotIn(b'name="email"', body)
        self.assertNotIn(b'name="password"', body)
        self.assertNotIn(b'name="name"', body)
        for route in ('/api/state', '/api/people', '/mcp'):
            self.assertEqual(self.status_of('GET', route, token=BOOTSTRAP), 409)
        self.assertEqual(self.status_of('POST', '/api/people', {'name': 'Bypass'}, token=BOOTSTRAP), 409)
        self.assertEqual(self.status_of('POST', '/webhooks/github', {'anything': True}), 409)
        status, headers, _ = self.form('/auth/setup', dict(workspace='Workspace only', setup_token=BOOTSTRAP))
        self.assertEqual(status, 302)
        self.assertFalse(self.accounts.ready())
        self.assertEqual(self.store.graph.people(), [])
        self.assertEqual(self.status_of('GET', '/api/state', token=BOOTSTRAP), 409)
        status, profile_headers, body = self.raw('GET', '/auth/profile', {'Cookie': headers['Set-Cookie'].split(';')[0]})
        self.assertIn(b'Create your profile', body)
        self.assertNotIn(b'name="workspace"', body)
        status, _, _ = self.form('/auth/profile', dict(name='Attacker', email='a@example.test', password='long-bad-password'))
        self.assertEqual(status, 400)
        status, _, _ = self.form('/auth/profile', self.owner_data, cookie=headers['Set-Cookie'].split(';')[0])
        self.assertEqual(status, 302)
        self.assertTrue(self.accounts.ready())

    def test_pending_setup_survives_restart_and_rejects_another_admin(self):
        from bridge.auth import Identity
        first = self.store.graph.add_person('First', role='admin')
        second = self.store.graph.add_person('Second', role='admin')
        secret = self.accounts.create_workspace({'workspace': 'Pending'}, Identity(first, 'First', 'admin', 'session'))
        restarted = Accounts(self.auth)
        self.assertEqual(restarted.profile_identity(secret).id, first)
        self.assertIsNone(restarted.profile_identity('bad'))
        with self.assertRaises(Invalid):
            restarted.setup(self.owner_data, Identity(second, 'Second', 'admin', 'session'))
        with patch('bridge.accounts.time.time', return_value=time.time() + 3601):
            self.assertIsNone(restarted.profile_identity(secret))
        restarted.setup(self.owner_data, Identity(first, 'First', 'admin', 'session'))
        self.assertIsNone(restarted.profile_identity(secret))

    def test_invite_expiry_replacement_and_role_restriction(self):
        self.setup_owner()
        actor = self.auth.identify({'Authorization': 'Bearer ' + BOOTSTRAP})
        with self.assertRaises(Invalid):
            self.accounts.invite('x@example.test', 'admin', actor)
        first = self.accounts.invite('x@example.test', 'viewer', actor)
        second = self.accounts.invite('x@example.test', 'viewer', actor)
        data = dict(name='Viewer', password='long-viewer-password')
        with self.assertRaises(Invalid):
            self.accounts.accept(dict(data, invite=first['token']))
        with patch('bridge.accounts.time.time', return_value=time.time() + 172801):
            with self.assertRaises(Invalid):
                self.accounts.accept(dict(data, invite=second['token']))
        pid = self.accounts.accept(dict(data, invite=second['token'], role='admin'))
        self.assertEqual(self.store.graph.get_person(pid)['role'], 'viewer')

    def test_invite_gives_a_mapped_person_their_login(self):
        # Readiness asks the admin to add the deciders as people first. The
        # invite then has to sign in that same person, authority and all.
        self.setup_owner()
        actor = self.auth.identify({'Authorization': 'Bearer ' + BOOTSTRAP})
        graph = self.store.graph
        atij = graph.add_person('Atij Mahesh', email='atij@example.test', role='viewer')
        graph.add_authority('path', 'bridge/*', 'decides', person_id=atij, repo='acme/bridge')
        invite = self.accounts.invite('Atij@Example.test', 'member', actor)
        pid = self.accounts.accept(dict(name='Someone Else', password='atij-long-password', invite=invite['token']))
        self.assertEqual(pid, atij)
        self.assertEqual(graph.get_person(pid)['name'], 'Atij Mahesh')
        self.assertEqual(graph.get_person(pid)['role'], 'member')
        self.assertEqual([r['person_id'] for r in graph.deciders('acme/bridge')], [atij])
        self.assertEqual(self.accounts.login('atij@example.test', 'atij-long-password'), atij)
        self.assertEqual(len([p for p in graph.people() if 'Atij' in p['name']]), 1)
        # Once they have a login, nobody can be invited into it again.
        with self.assertRaisesRegex(Invalid, 'already has a login'):
            self.accounts.invite('atij@example.test', 'member', actor)
        with self.assertRaisesRegex(Invalid, 'already has a login'):
            self.accounts.invite('owner@example.test', 'member', actor)

    def test_login_throttling_inactive_and_csrf(self):
        self.setup_owner()
        for _ in range(10):
            with self.assertRaises(Invalid):
                self.accounts.login('owner@example.test', 'wrong-password')
        with self.assertRaisesRegex(Invalid, 'Too many'):
            self.accounts.login('owner@example.test', self.owner_data['password'])
        with patch('bridge.accounts.time.time', return_value=time.time() + 901):
            pid = self.accounts.login('owner@example.test', self.owner_data['password'])
        self.store.graph.db.execute('UPDATE people SET active=0 WHERE id=?', (pid,))
        with self.assertRaises(Invalid):
            self.accounts.login('owner@example.test', self.owner_data['password'])
        status, _, _ = self.raw('POST', '/auth/password', {'Content-Type': 'application/json'}, json.dumps(self.owner_data))
        self.assertEqual(status, 403)

    def test_existing_dev_account_is_preserved(self):
        pid = self.store.graph.add_person('Local developer', email='developer@bridge.local', role='admin')
        human = self.auth.create_token(pid, label='Docker development login', kind='human')['token']
        status, _, _ = self.form('/auth/setup', dict(self.owner_data, setup_token=human))
        self.assertEqual(status, 302)
        status, _, _ = self.form('/auth/profile', dict(self.owner_data, setup_token=human))
        self.assertEqual(status, 302)
        self.assertEqual(self.accounts.login('owner@example.test', self.owner_data['password']), pid)
        self.assertIsNone(self.auth.identify({'Authorization': 'Bearer ' + human}))

    def test_agent_cannot_claim_or_invite_and_cross_origin_is_rejected(self):
        pid = self.store.graph.add_person('Admin', email='admin@example.test', role='admin')
        agent = self.auth.create_token(pid)['token']
        identity = self.auth.identify({'Authorization': 'Bearer ' + agent})
        with self.assertRaises(Invalid):
            self.accounts.setup(self.owner_data, identity)
        with self.assertRaises(Invalid):
            self.accounts.invite('other@example.test', 'member', identity)
        status, _, _ = self.raw('POST', '/auth/setup', {'Origin': 'https://untrusted.example',
            'Content-Type': 'application/json'}, json.dumps(self.owner_data))
        self.assertEqual(status, 403)

    def test_password_and_email_validation(self):
        actor = self.auth.identify({'Authorization': 'Bearer ' + BOOTSTRAP})
        for data in (dict(self.owner_data, password='short'), dict(self.owner_data, email='invalid'),
                     dict(self.owner_data, name='')):
            with self.assertRaises(Invalid):
                self.accounts.setup(data, actor)
        self.assertFalse(self.accounts.workspace())
