"""Notification-only handoff to an employee's existing authorized host tool.

Raven does not acquire the connector's credentials or execute its send tool.
The host enforces its own permissions and verifies its employee and recipient
identities. A claimed send is never automatically reissued, even after a crash:
an absent receipt means unknown delivery, not a failed send safe to retry.
"""
import json
import re
from urllib.parse import urlsplit

from . import context_memory as cm, visibility
from .store import Invalid

SCHEMA = '''
CREATE TABLE IF NOT EXISTS host_notifications (
 id TEXT PRIMARY KEY, binding_id TEXT NOT NULL REFERENCES host_sessions(id),
 task_id TEXT NOT NULL, decision_id TEXT NOT NULL, revision TEXT NOT NULL,
 recipient_id TEXT NOT NULL, workspace_id TEXT NOT NULL, sender_id TEXT NOT NULL,
 tool TEXT NOT NULL, text TEXT NOT NULL, content_hash TEXT NOT NULL,
 state TEXT NOT NULL, external_ref TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(task_id,decision_id,revision,recipient_id));
'''


def migrate(db):
    db.executescript(SCHEMA)


def _text(args, key, limit=200):
    value = args.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Invalid(f'{key} must be nonempty text up to {limit} characters')
    return value.strip()


def _binding(store, args, principal):
    who = getattr(principal, 'id', '') or 'local-operator'
    row = store.graph.db.execute('SELECT * FROM host_sessions WHERE id=? AND principal=?',
                                (_text(args, 'binding_id'), who)).fetchone()
    if not row or not row['task_id'] or row['task_id'] != args.get('task_id'):
        raise Invalid('This authenticated host binding does not own this task')
    visibility.require_task(store, row['task_id'])
    return row


def handle(store, args, principal=None):
    binding = _binding(store, args, principal)
    action = args.get('action')
    if action == 'capabilities':
        return {'mode': 'host_notification', 'reported_tools': json.loads(binding['inventory']),
                'inventory_verified': False, 'backend_can_invoke_host_tools': False,
                'requires': ['existing authorized Slack send tool', 'authenticated employee identity',
                             'verified recipient identity', 'permission to send this notification'],
                'fallback': 'The expert request is in Raven. Share its authenticated task link manually.',
                'next': 'After verifying tool permissions and identities, claim one notification. Send exactly '
                        'the returned text once, then record its receipt. Never collect answers or signoff in Slack.'}
    if action not in ('claim', 'receipt', 'status'):
        raise Invalid('action must be capabilities, claim, receipt or status')
    g, at = store.graph, cm.stamp()
    with g.transaction():
        if action in ('receipt', 'status'):
            row = g.db.execute('SELECT * FROM host_notifications WHERE id=? AND binding_id=? AND task_id=?',
                               (_text(args, 'notification_id'), binding['id'], binding['task_id'])).fetchone()
            if not row:
                raise Invalid('Notification does not belong to this host binding and task')
            if action == 'status':
                return _view(row)
            if _text(args, 'content_hash') != row['content_hash']:
                raise Invalid('Receipt content does not match the claimed notification')
            state = args.get('state')
            if state not in ('sent', 'not_sent', 'unknown'):
                raise Invalid('Receipt state must be sent, not_sent or unknown')
            ref = _text(args, 'external_ref', 500) if state == 'sent' else ''
            if row['state'] in ('sent', 'not_sent'):
                if row['state'] != state or row['external_ref'] != ref:
                    raise Invalid('A terminal receipt cannot be replaced by a conflicting receipt')
                return _view(row)
            g.db.execute('UPDATE host_notifications SET state=?,external_ref=?,updated_at=? WHERE id=?',
                         (state, ref, at, row['id']))
            g.append_event('host_notification_receipt', {'task_id': row['task_id'],
                           'decision_id': row['decision_id'], 'notification_id': row['id'],
                           'state': state, 'host_reported': True})
            return _view(g.db.execute('SELECT * FROM host_notifications WHERE id=?', (row['id'],)).fetchone())

        if store.delivery.enabled:
            raise Invalid('Server notification delivery is enabled. Use that channel rather than sending a duplicate host notification')
        if args.get('permission_confirmed') is not True:
            raise Invalid('The host must confirm permission to send this notification through its existing connector')
        tool = _text(args, 'tool')
        if tool not in json.loads(binding['inventory']):
            raise Invalid('The selected tool is not in this host binding\'s reported tool inventory')
        workspace, sender, recipient = (_text(args, k) for k in ('workspace_id', 'sender_id', 'recipient_id'))
        if not re.fullmatch(r'T[A-Z0-9]+', workspace) or any(not re.fullmatch(r'[UW][A-Z0-9]+', v) for v in (sender, recipient)):
            raise Invalid('Verified Slack workspace and user IDs are required; display names are not identities')
        # A connector lookup is necessary even with cached IDs. The host must
        # supply its observed recipient email, matched to the exact linked owner.
        email = _text(args, 'recipient_email', 320).lower()
        row = g.db.execute('SELECT d.*,o.person_id,p.email,p.slack_id,p.active FROM decisions d '
                           'JOIN owners o ON o.id=d.owner_id JOIN people p ON p.id=o.person_id '
                           'WHERE d.id=? AND d.run_id=?',
                           (_text(args, 'node_id'), binding['task_id'])).fetchone()
        if not row or not row['active'] or row['draft'] or row['status'] in ('withdrawn', 'superseded'):
            raise Invalid('A persisted request assigned to an active Raven expert is required')
        from .graph import blocks_finish
        task = g.get_task(binding['task_id'])
        if not task or task['status'] in ('completed', 'abandoned') or not blocks_finish(row):
            raise Invalid('This task or request no longer needs human judgment')
        if not row['email'] or row['email'].lower() != email or (row['slack_id'] and row['slack_id'] != recipient):
            raise Invalid('The connector recipient does not match the request\'s exact Raven owner')
        if _text(args, 'revision') != row['updated_at']:
            raise Invalid('The request changed; read its current revision before notifying')
        old = g.db.execute('SELECT * FROM host_notifications WHERE task_id=? AND decision_id=? AND revision=? AND recipient_id=?',
                           (binding['task_id'], row['id'], row['updated_at'], recipient)).fetchone()
        if old:
            return _view(old)
        base = store.delivery.base_url.rstrip('/')
        parsed = urlsplit(base)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise Invalid('Configure Raven\'s public HTTPS URL before sending an external notification')
        # No source content, bearer token, answer or implied authority leaves Raven.
        text = 'Your judgment is requested in Raven. Review the task and answer there: ' + base + '/#runs/' + row['run_id'] + '/' + row['id']
        ident = cm.digest([binding['task_id'], row['id'], row['updated_at'], recipient])
        content_hash = cm.digest([workspace, sender, recipient, text])
        g.db.execute('INSERT INTO host_notifications(id,binding_id,task_id,decision_id,revision,recipient_id,'
                     'workspace_id,sender_id,tool,text,content_hash,state,created_at,updated_at) '
                     'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (ident, binding['id'], row['run_id'], row['id'], row['updated_at'], recipient,
                      workspace, sender, tool, text, content_hash, 'unknown', at, at))
        g.append_event('host_notification_claimed', {'task_id': row['run_id'], 'decision_id': row['id'],
                       'notification_id': ident, 'principal': binding['principal'], 'tool': tool,
                       'identity_verified_by': 'host_connector', 'host_reported': True})
        result = _view(g.db.execute('SELECT * FROM host_notifications WHERE id=?', (ident,)).fetchone())
        result.update(send_once=True, text=text, tool=tool, workspace_id=workspace,
                      sender_id=sender, recipient_id=recipient,
                      instruction='Send only this notification using the employee\'s existing authorized connector. '
                                  'If the send times out, record unknown; do not retry. Answers and signoff stay in Raven.')
        return result


def _view(row):
    return {'notification_id': row['id'], 'task_id': row['task_id'], 'node_id': row['decision_id'],
            'state': row['state'], 'external_ref': row['external_ref'], 'content_hash': row['content_hash'],
            'send_once': False, 'receipt_verified': False,
            'notice': 'Delivery receipts are host reports, not independent Slack attestation. '
                      'Unknown delivery must be reconciled in the connector; never automatically resend.'}
