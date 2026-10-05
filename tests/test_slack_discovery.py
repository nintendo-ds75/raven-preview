"""No manually registered people, authority rows, or recipient accounts."""
import copy
import json
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase, PEOPLE
from test_delivery import FakeSlack
from bridge import canvas
from bridge.authz import Actor, Refused
from bridge.config import Config
from bridge.delivery import SlackTransport
from bridge.mcp import call_tool
from bridge.routing import route_ranked
from bridge.store import Store, Invalid
from bridge.bootstrap import initialize_workspace

CFG = Config(model_api='none')


def member(uid, name, email='', **extra):
    return {'id': uid, 'team_id': 'TTEST', 'real_name': name,
            'profile': {'real_name': name, 'email': email}, **extra}


class DirectorySlack(FakeSlack):
    def __init__(self, users):
        super().__init__()
        self.users = users
        self.error = ''

    def workspace_id(self):
        return 'TTEST'

    def list_users(self):
        if self.error:
            raise RuntimeError(self.error)
        return copy.deepcopy(self.users)

    def user_info(self, uid):
        return next(copy.deepcopy(u) for u in self.users if u['id'] == uid)


class DiscoveryTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.warm_store('qemulike')
        self.g = self.store.graph
        self.slack = DirectorySlack([member('U'+key.upper(), name, email)
                                    for key, (name, email) in PEOPLE.items() if key != 'bot'])
        self.delivery = self.store.connect_delivery(self.slack, fallback_channel='CTRIAGE')
        self.addCleanup(self.g.close)

    def sync(self):
        return self.delivery.sync_directory()

    def node(self, question='Should we change the default interrupt controller?', repo='qemulike', paths='hw/riscv/virt.c'):
        task = call_tool(self.store, 'bridge_start_task', {'title': 'Change default interrupt controller',
                          'goal': 'Change the interrupt controller default without breaking compatibility.',
                          'repo': repo, 'paths': paths, 'client_key': question})['task_id']
        node = call_tool(self.store, 'bridge_add_node', {'task_id': task, 'question': question, 'paths': paths})
        return task, node

    def test_directory_does_not_create_accounts_or_authority(self):
        self.assertEqual(self.g.people(), [])
        self.sync()
        self.assertGreater(len(self.g.people()), 2)
        self.assertEqual(self.g.authority_rows(), [])
        self.assertEqual(self.g.db.execute('SELECT count(*) AS n FROM account_passwords').fetchone()['n'], 0)
        self.assertFalse(any(r['key'].startswith('no_authority') for r in self.store.readiness()))

    def test_repository_to_slack_to_finished_task_without_owner_setup(self):
        self.sync()
        task, node = self.node()
        self.assertEqual(node['owner'], 'Alistair Francis', node)
        self.assertEqual(node['delivery']['notifications'][0]['state'], 'queued')
        with self.assertRaises(Invalid):
            call_tool(self.store, 'bridge_finish_task', {'task_id': task})
        self.delivery.deliver_now()
        message = self.slack.messages[0]
        self.assertEqual(message['channel'], 'DUALISTAIR')
        self.assertIn('inferred first contact', message['text'])
        ack = self.delivery.receive(message['channel'], message['ts'], 'UALISTAIR',
                                    'answer: Preserve the old default because compatibility matters', event_id='answer-1')
        self.assertIn('Recorded', ack)
        self.assertEqual(self.delivery.receive(message['channel'], message['ts'], 'UALISTAIR',
                         'answer: Different because retry', event_id='answer-1'), '')
        result = call_tool(self.store, 'bridge_wait', {'task_id': task, 'timeout': '0'})
        self.assertFalse(result['timed_out'])
        call_tool(self.store, 'bridge_get_tree', {'task_id': task})
        finished = call_tool(self.store, 'bridge_finish_task', {'task_id': task, 'checks': 'fixture check'})
        self.assertFalse(finished.get('verified', False))
        self.assertEqual(self.g.db.execute('SELECT count(*) AS n FROM account_passwords').fetchone()['n'], 0)

    def test_unknown_question_reaches_channel_then_referred_contact(self):
        self.sync()
        task, node = self.node('Should the holiday policy cover contractors?', 'acme/hr', '')
        self.assertFalse(node.get('owner'))
        self.delivery.deliver_now()
        message = self.slack.messages[0]
        self.assertEqual(message['channel'], 'CTRIAGE')
        self.assertIn("I'll take this", message['text'])
        ack = self.delivery.receive('CTRIAGE', message['ts'], 'UALISTAIR', 'answer: Yes because yes')
        self.assertIn('No answer recorded', ack)
        ack = self.delivery.receive('CTRIAGE', message['ts'], 'UALISTAIR', 'ask <@UJASON>')
        self.assertIn('Sent to Jason Wang', ack)
        self.delivery.deliver_now()
        dm = self.slack.messages[-1]
        self.assertEqual(dm['channel'], 'DUJASON')
        self.assertFalse(self.store.get_decision(node['node_id'])['answer'])
        self.assertEqual(self.g.authority_rows(), [])
        self.assertIn('Recorded', self.delivery.receive(dm['channel'], dm['ts'], 'UJASON',
                                                       'answer: Include contractors because the policy covers them'))
        with self.assertRaises(Invalid):
            self.store.claim_slack_question(node['node_id'], self.g.find_person('UALISTAIR')['id'],
                                           Actor.person(self.g.find_person('UALISTAIR')))

    def test_a_new_member_can_receive_a_referral_without_an_account(self):
        self.sync()
        task, node = self.node()
        self.delivery.deliver_now()
        message = self.slack.messages[0]
        self.slack.users.append(member('UNEW', 'New Specialist', 'new@acme.test'))
        reply = self.delivery.receive(message['channel'], message['ts'], 'UALISTAIR', 'not me <@UNEW> just this one')
        self.assertIn('New Specialist', reply)
        self.delivery.deliver_now()
        self.assertEqual(self.slack.messages[-1]['channel'], 'DUNEW')
        self.assertEqual(self.g.authority_rows(), [])

    def test_unrelated_member_cannot_sign_someone_elses_question(self):
        self.sync()
        task, node = self.node()
        self.delivery.deliver_now()
        message = self.slack.messages[0]
        reply = self.delivery.receive(message['channel'], message['ts'], 'UJASON', 'answer: Change it because I say so')
        self.assertIn('Not recorded', reply)
        self.assertFalse(self.store.get_decision(node['node_id'])['answer'])

    def test_email_joins_history_to_a_different_slack_display_name(self):
        self.slack.users[0]['profile']['real_name'] = 'A. Francis'
        self.sync()
        task, node = self.node()
        self.assertEqual(node['owner'], 'A. Francis')
        self.delivery.deliver_now()
        self.assertEqual(self.slack.messages[0]['channel'], 'DUALISTAIR')

    def test_same_name_does_not_merge_with_or_demote_an_admin(self):
        admin = self.g.add_person('Alistair Francis', email='different@acme.test', role='admin', merge=False)
        self.sync()
        imported = self.g.find_person('UALISTAIR')
        self.assertNotEqual(imported['id'], admin)
        self.assertEqual(imported['role'], 'member')
        self.assertEqual(self.g.get_person(admin)['role'], 'admin')
        self.assertEqual(self.g.get_person(admin)['slack_id'], '')

    def test_exact_email_preserves_existing_role_and_stable_id(self):
        admin = self.g.add_person('Workspace admin', email=PEOPLE['alistair'][1], role='admin', merge=False)
        self.sync()
        self.assertEqual(self.g.find_person('UALISTAIR')['id'], admin)
        self.assertEqual(self.g.get_person(admin)['role'], 'admin')

    def test_directory_excludes_bots_deleted_members_guests_and_foreign_members(self):
        self.slack.users += [member('UBOT', 'Bot', is_bot=True), member('UDELETED', 'Deleted', deleted=True),
                             member('UGUEST', 'Guest', is_restricted=True),
                             {**member('UFOREIGN', 'Other org'), 'team_id': 'TOTHER'}]
        self.sync()
        for uid in ['UBOT', 'UDELETED', 'UGUEST', 'UFOREIGN']:
            self.assertIsNone(self.g.find_person(uid))

    def test_failure_preserves_contacts_and_is_visible(self):
        self.sync()
        before = self.g.people()
        self.slack.error = 'Slack users.list failed: missing_scope'
        with self.assertRaises(RuntimeError): self.sync()
        self.assertEqual(before, self.g.people())
        self.assertIn('missing_scope', str(self.store.readiness()))

    def test_duplicate_email_snapshot_is_rejected_atomically(self):
        self.slack.users.append(member('UCOPY', 'Other', PEOPLE['alistair'][1]))
        with self.assertRaises(ValueError): self.sync()
        self.assertEqual(self.g.people(), [])

    def test_deleted_slack_member_cannot_answer_or_keep_receiving(self):
        self.sync()
        task, node = self.node()
        self.delivery.deliver_now()
        message = self.slack.messages[0]
        self.slack.users[0]['deleted'] = True
        self.sync()
        self.assertEqual(self.delivery._destination('Alistair Francis')[0], 'CTRIAGE')
        reply = self.delivery.receive(message['channel'], message['ts'], 'UALISTAIR', 'answer: yes because yes')
        self.assertIn('could not verify', reply)
        self.assertFalse(self.store.get_decision(node['node_id'])['answer'])

    def test_cited_jira_author_and_topic_records_are_routing_signals(self):
        self.sync()
        call_tool(self.store, 'bridge_import_record', {'repo': 'acme/hr', 'kind': 'ticket', 'ref': 'HR-102',
                    'title': 'Contractor holiday allowance', 'body': 'This is an open question for discussion.',
                    'author': 'UJASON', 'status': 'Open', 'url': 'https://jira.example/HR-102'})
        for question in ['Who should decide HR-102?', 'Should the contractor holiday allowance cover weekends?']:
            ranked = route_ranked(self.g, 'acme/hr', question)
            self.assertEqual(ranked[0][0], 'Jason Wang', ranked)
            self.assertIn('inferred first contact', ranked[0][1][0])
        self.assertEqual(self.g.authority_rows(), [])

    def test_stdio_writer_enqueues_for_shared_slack_worker(self):
        self.sync()
        second = Store(self.store.path)
        self.addCleanup(second.graph.close)
        task = canvas.start_task(second, CFG, {'title': 'Change interrupt default', 'repo': 'qemulike',
                                               'paths': 'hw/riscv/virt.c'})['task_id']
        canvas.add_node(second, CFG, {'task_id': task, 'question': 'Should this keep the existing interrupt default?',
                                     'paths': 'hw/riscv/virt.c'})
        self.assertTrue(second.delivery.enabled)
        self.assertEqual(second.delivery.deliver_now(), 0)
        self.assertEqual(self.delivery.deliver_now(), 1)
        self.assertEqual(self.slack.messages[0]['channel'], 'DUALISTAIR')

    def test_headless_workspace_is_idempotent_and_has_no_personal_account(self):
        with patch.dict('os.environ', {'BRIDGE_ADMIN_TOKEN': 'operator-test-token'}):
            initialize_workspace(self.store, 'Test team')
            initialize_workspace(self.store, 'Different team')
        self.assertEqual(self.g.get_setting('workspace_name'), 'Test team')
        self.assertEqual(self.g.db.execute('SELECT count(*) AS n FROM account_passwords').fetchone()['n'], 0)

    def test_first_browser_profile_can_adopt_an_imported_contact(self):
        from bridge.accounts import Accounts
        from bridge.auth import Auth, Identity
        self.sync()
        contact = self.g.find_person('UALISTAIR')
        accounts = Accounts(Auth(self.store, enabled=True))
        bootstrap = Identity('', 'Workspace creator', 'admin', 'bootstrap')
        accounts.create_workspace({'workspace': 'Optional browser'}, bootstrap)
        pid = accounts.setup({'name': 'Alistair', 'email': PEOPLE['alistair'][1],
                              'password': 'a-test-password-123'}, bootstrap)
        self.assertEqual(pid, contact['id'])
        self.assertEqual(self.g.get_person(pid)['slack_id'], 'UALISTAIR')
        self.assertEqual(self.g.get_person(pid)['role'], 'admin')

    def test_claim_cannot_steal_an_already_routed_question(self):
        self.sync()
        task, node = self.node('Which plants for reception?', 'acme/office', '')
        first, second = self.g.find_person('UALISTAIR'), self.g.find_person('UJASON')
        self.store.claim_slack_question(node['node_id'], first['id'], Actor.person(first, kind='slack'))
        with self.assertRaises((Invalid, Refused)):
            self.store.claim_slack_question(node['node_id'], second['id'], Actor.person(second, kind='slack'))
        self.assertEqual(self.store.get_decision(node['node_id'])['owner_name'], first['name'])
        self.assertEqual(self.g.authority_rows(), [])

    def test_slack_directory_paginates(self):
        transport = SlackTransport('fake')
        with patch.object(transport, '_call', side_effect=[{'members': [{'id': 'U1'}], 'response_metadata': {'next_cursor': 'next'}},
                                                        {'members': [{'id': 'U2'}], 'response_metadata': {'next_cursor': ''}}]) as call:
            self.assertEqual([u['id'] for u in transport.list_users()], ['U1', 'U2'])
            self.assertEqual(call.call_args_list[1].args[1]['cursor'], 'next')
