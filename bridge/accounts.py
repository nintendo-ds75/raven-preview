"""Invite-only workspace onboarding; shares Bridge identities and transactions."""
import hashlib
import hmac
import secrets
import time
from .store import Invalid


def email_address(value):
    value = str(value or '').strip().lower()
    if len(value) > 254 or '@' not in value or any(c.isspace() for c in value):
        raise Invalid('Enter a valid email address')
    return value


def password_hash(password, salt=None):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise Invalid('Use a password of 12–256 characters')
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ':' + digest


class Accounts:
    def __init__(self, auth):
        self.auth, self.graph = auth, auth.store.graph

    def workspace(self):
        return self.graph.get_setting('workspace_name', '')

    def ready(self):
        return bool(self.workspace()) and self.graph.get_setting('workspace_profile_pending', '') != '1'

    def create_workspace(self, data, identity):
        if not self.auth.enabled or not identity or not identity.allows('admin'):
            raise Invalid('Sign in as an administrator or provide the setup credential')
        workspace = str(data.get('workspace', '')).strip()[:100]
        if not workspace:
            raise Invalid('Workspace name is required')
        continuation = secrets.token_urlsafe(32)
        with self.graph.transaction():
            if self.workspace():
                raise Invalid('A workspace already exists')
            self.graph.set_setting('workspace_name', workspace)
            self.graph.set_setting('workspace_profile_pending', '1')
            self.graph.set_setting('workspace_creator', identity.id or 'bootstrap')
            self.graph.set_setting('workspace_setup_token', self.auth._hash(continuation))
            self.graph.set_setting('workspace_setup_expires', str(int(time.time()) + 3600))
            self.graph.append_event('workspace_created', {'name': workspace})
        return continuation

    def profile_identity(self, token):
        from .auth import Identity
        if not token or self.ready() or int(self.graph.get_setting('workspace_setup_expires', '0')) <= time.time():
            return None
        if not hmac.compare_digest(self.auth._hash(token), self.graph.get_setting('workspace_setup_token', '')):
            return None
        creator = self.graph.get_setting('workspace_creator', '')
        if creator == 'bootstrap':
            return Identity('', 'Workspace creator', 'admin', 'bootstrap')
        person = self.graph.get_person(creator)
        if person and person['active'] and person['role'] == 'admin':
            return Identity(person['id'], person['name'], 'admin', 'session')
        return None

    def setup(self, data, identity):
        """Complete the first profile, separately from workspace creation."""
        if not self.auth.enabled or not identity or not identity.allows('admin'):
            raise Invalid('Sign in as an administrator or provide the setup credential')
        name = str(data.get('name', '')).strip()[:100]
        email = email_address(data.get('email'))
        hashed = password_hash(data.get('password'))
        if not name:
            raise Invalid('Your name is required')
        with self.graph.transaction():
            if not self.workspace() or self.ready():
                raise Invalid('Create a workspace first, or sign in to the existing workspace')
            if self.graph.get_setting('workspace_creator', '') != (identity.id or 'bootstrap'):
                raise Invalid('Only the workspace creator can finish the first profile')
            existing = self.graph.person_by_email(email)
            imported_contact = (existing and not identity.id and existing['source'] == 'slack-directory'
                                and existing['active'] and not self.has_password(existing['id']))
            if existing and existing['id'] != identity.id and not imported_contact:
                raise Invalid('That email is already assigned to another person')
            if identity.id or imported_contact:
                pid = identity.id or existing['id']
                self.graph.db.execute('UPDATE people SET name=?, email=? WHERE id=?', (name, email, pid))
                self.graph.db.execute('UPDATE owners SET name=? WHERE person_id=?', (name, pid))
                if imported_contact:
                    self.graph.db.execute("UPDATE people SET role='admin' WHERE id=?", (pid,))
            else:
                pid = self.graph.add_person(name, email=email, role='admin', merge=False)
            self.graph.db.execute('INSERT INTO account_passwords(person_id,password_hash) VALUES(?,?)', (pid, hashed))
            self.graph.set_setting('workspace_profile_pending', '')
            self.graph.set_setting('workspace_setup_token', '')
            # The development bootstrap login is no longer an alternate owner login.
            self.graph.db.execute("UPDATE api_tokens SET revoked_at=? WHERE person_id=? AND label='Docker development login' AND revoked_at=''",
                                  (str(int(time.time())), pid))
            self.graph.append_event('workspace_admin_created', {'person_id': pid})
        return pid

    def has_password(self, person_id):
        return self.graph.db.execute('SELECT 1 FROM account_passwords WHERE person_id=?',
                                     (person_id,)).fetchone() is not None

    def invite(self, email, role, actor):
        if not self.ready():
            raise Invalid('Complete workspace setup first')
        if not self.auth.enabled or not actor or not actor.allows('admin'):
            raise Invalid('Only an administrator can invite teammates')
        if role not in ('viewer', 'member'):
            raise Invalid('Invitations may grant viewer or member access')
        email = email_address(email)
        token, identifier = secrets.token_urlsafe(32), secrets.token_hex(12)
        with self.graph.transaction():
            # A person the admin put on the map has authority but no login
            # yet; the invite is how they get one, as that same person.
            person = self.graph.person_by_email(email)
            if person and self.has_password(person['id']):
                raise Invalid('This person already has a login; they sign in with it')
            self.graph.db.execute('UPDATE account_invites SET consumed=1 WHERE email=?', (email,))
            self.graph.db.execute('INSERT INTO account_invites(id,email,role,token_hash,expires_at,created_by) VALUES(?,?,?,?,?,?)',
                (identifier, email, role, self.auth._hash(token), int(time.time()) + 172800, actor.id))
            self.graph.append_event('teammate_invited', {'email': email, 'role': role, 'by': actor.id})
        return {'id': identifier, 'token': token, 'expires_in': 172800}

    def accept(self, data):
        if not self.ready():
            raise Invalid('Complete workspace setup first')
        name = str(data.get('name', '')).strip()[:100]
        if not name:
            raise Invalid('Your name is required')
        hashed = password_hash(data.get('password'))
        with self.graph.transaction():
            invite = self.graph.db.execute('SELECT * FROM account_invites WHERE token_hash=?',
                (self.auth._hash(str(data.get('invite', ''))),)).fetchone()
            if not invite or invite['consumed'] or invite['expires_at'] <= time.time():
                raise Invalid('This invitation is invalid or expired; ask for a new one')
            person = self.graph.person_by_email(invite['email'])
            if person and self.has_password(person['id']):
                raise Invalid('This account already exists; sign in instead')
            if person:
                # Keep the person the map names, so the authority the admin
                # recorded is theirs from the first sign-in.
                pid = person['id']
                if person.get('role') != 'admin':
                    self.graph.db.execute('UPDATE people SET role=? WHERE id=?', (invite['role'], pid))
            else:
                pid = self.graph.add_person(name, email=invite['email'], role=invite['role'], merge=False)
            self.graph.db.execute('INSERT INTO account_passwords(person_id,password_hash) VALUES(?,?)', (pid, hashed))
            self.graph.db.execute('UPDATE account_invites SET consumed=1 WHERE id=?', (invite['id'],))
            self.graph.append_event('invitation_accepted', {'person_id': pid, 'invite_id': invite['id']})
        return pid

    def claim(self, person, data):
        """A person Raven messaged makes their own login from their task
        link. The link reached them in their own Slack DM, the same proof
        an emailed invitation is, and the account is the person the map
        already names, with the role it already gives them. An admin
        turns this off with the brief_signup setting."""
        if not self.ready():
            raise Invalid('Complete workspace setup first')
        if self.graph.get_setting('brief_signup', '1') == '0':
            raise Invalid('Ask an administrator for an invitation to create your account')
        if person.get('role') == 'admin':
            # The link acts as a member and never carries an override; a
            # login made from it would carry the admin role to whoever
            # holds the link. An administrator's login comes from GitHub
            # sign-in or an invitation, as it always has.
            raise Invalid('An administrator signs in with GitHub or an invitation, not from a task link')
        hashed = password_hash(data.get('password'))
        with self.graph.transaction():
            if self.has_password(person['id']) or person.get('github_id'):
                raise Invalid('You already have an account; sign in instead')
            email = person.get('email') or email_address(data.get('email'))
            existing = self.graph.person_by_email(email)
            if existing and existing['id'] != person['id']:
                raise Invalid('That email belongs to another person in this workspace')
            if not person.get('email'):
                self.graph.db.execute('UPDATE people SET email=? WHERE id=?', (email, person['id']))
            self.graph.db.execute('INSERT INTO account_passwords(person_id,password_hash) VALUES(?,?)',
                                  (person['id'], hashed))
            self.graph.append_event('account_claimed', {'person_id': person['id'], 'via': 'task link'})
        return person['id']

    def login(self, email, password):
        email = str(email or '').strip().lower()
        # Database-backed attempt window works across restarts and worker threads.
        key = 'login_attempts:' + self.auth._hash(email)
        with self.graph.transaction():
            saved = self.graph.get_setting(key, '0:0').split(':')
            start, count = int(saved[0]), int(saved[1])
            if time.time() - start > 900:
                start, count = int(time.time()), 0
            if count >= 10:
                raise Invalid('Too many attempts. Try again in 15 minutes')
            self.graph.set_setting(key, f'{start}:{count + 1}')
        person = self.graph.person_by_email(email)
        row = self.graph.db.execute('SELECT password_hash FROM account_passwords WHERE person_id=?',
                                   ((person or {}).get('id', ''),)).fetchone()
        stored = row['password_hash'] if row else '00' * 16 + ':' + '00' * 64
        try:
            candidate = password_hash(password, stored.split(':')[0])
        except Invalid:
            candidate = ''
        if not hmac.compare_digest(candidate, stored) or not person or not person['active']:
            raise Invalid('Email or password is incorrect')
        with self.graph.transaction():
            self.graph.set_setting(key, '0:0')
            self.graph.append_event('signed_in', {'person_id': person['id'], 'via': 'password'})
        return person['id']
