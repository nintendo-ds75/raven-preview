"""Scripted end-to-end flows against a real Raven, over HTTP MCP and the
HTTP API, one per invariant of the decision protocol (quality rubric,
dimensions 3 and 8): nothing finishes while a decision is unsigned, on
the pending, agent-settled and standing-rule paths; an agent credential
never answers, signs, refers, invites or administers; a corrected answer
puts its dependents up for review and the agent hears it; a required
follow-up added after completion can be taken up and answered; a retry
with the same key never duplicates a task, a node or a notification; a
restart loses no binding, hold, outbox row or source state; the host's
resume report survives a Slack outage and is posted once; an unreachable
model degrades every path to its deterministic behaviour without
hanging; an unreachable source records the error and loses nothing."""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import canvas, host_sessions as hs, source_docs, sources
from bridge.auth import Auth, Identity
from bridge.canvas import wait_for_background
from bridge.github import GitHubAPI, GitHubError, sync_repo, sync_state
from bridge.llm import Client, LLMError
from bridge.server import make_server
from bridge.store import Store
from test_auth import BOOTSTRAP, SharedServer
from test_delivery import FakeSlack
from test_github import REPO as GITHUB_REPO, WES, GitHubCase
from test_sources_docs import PAGE, Site
from test_sources_jira import JiraDouble, issue

REPO = 'example/service'
PROMPT = 'Add usage-based pricing to billing.'
QUESTION = 'Should we bill the usage spike?'
DIFF = ('diff --git a/billing/usage.py b/billing/usage.py\n--- a/billing/usage.py\n+++ b/billing/usage.py\n'
        '@@ -1 +1,2 @@\n LIMIT = 1\n+CAP = 2\n')


class FlowCase(SharedServer):
    """A shared Raven with auth on: Ada's agent token speaks MCP, Wes
    decides billing/* and answers over the HTTP API as himself."""

    def setUp(self):
        super().setUp()
        self.ada = self.post('/api/people', {'name': 'Ada Example', 'role': 'member'}, token=BOOTSTRAP)
        self.agent = self.post('/api/tokens', {'person_id': self.ada['id']}, token=BOOTSTRAP)['token']
        wes = self.post('/api/people', {'name': 'Wes Chen', 'email': 'wes@example.invalid', 'slack_id': 'UWES'},
                        token=BOOTSTRAP)
        self.wes_id = wes['id']
        self.post('/api/authority', {'person': wes['id'], 'scope_kind': 'path', 'scope': 'billing/*',
                                     'role': 'decides'}, token=BOOTSTRAP)
        self.wes = self.post('/api/tokens', {'person_id': wes['id'], 'kind': 'human'}, token=BOOTSTRAP)['token']

    # ---- the agent, over HTTP MCP ----

    def ok(self, name, args):
        result = self.mcp(name, args, self.agent)
        self.assertFalse(result['isError'], result['result'])
        return result['result']

    def refused(self, name, args):
        result = self.mcp(name, args, self.agent)
        self.assertTrue(result['isError'], result['result'])
        return result['result']

    def start(self, key='k1', title=PROMPT, **extra):
        return self.ok('bridge_start_task', {'title': title, 'goal': title, 'repo': REPO, 'client_key': key,
                                             'paths': 'billing/usage.py', **extra})

    def node(self, task, question=QUESTION, **extra):
        return self.ok('bridge_add_node', {'task_id': task, 'question': question, 'paths': 'billing/usage.py',
                                           **extra})

    def settle(self, task, node_id, answer='Bill it, capped at 2x the plan limit'):
        return self.ok('bridge_settle_node', {'task_id': task, 'node_id': node_id, 'answer': answer,
                                              'rationale': 'the plan terms'})

    def tree(self, task):
        return self.ok('bridge_get_tree', {'task_id': task})

    def host(self, event, session='session-flow', **extra):
        return self.ok('bridge_host_event', {'event': event, 'host': 'claude', 'session_id': session,
                                             'project': '/checkout/service', 'repo': REPO, **extra})

    # ---- Wes, over the HTTP API ----

    def decision(self, node_id):
        return self.get(f'/api/decisions/{node_id}', token=self.wes)

    def answer(self, node_id, text='Bill it, capped at 2x the plan limit.'):
        current = self.decision(node_id)
        return self.post(f'/api/decisions/{node_id}/answer', {'answer': text, 'rationale': 'plan terms',
                                                              'expected_updated_at': current['updated_at']},
                         token=self.wes)

    def sign(self, node_id, **extra):
        current = self.decision(node_id)
        return self.post(f'/api/decisions/{node_id}/signoff', {'expected_updated_at': current['updated_at'], **extra},
                         token=self.wes)

    def rule(self, node_id, **extra):
        current = self.decision(node_id)
        return self.post(f'/api/decisions/{node_id}/rule', {'expected_updated_at': current['updated_at'], **extra},
                         token=self.wes)

    def events(self, task):
        return [r['kind'] for r in self.store.graph.db.execute(
            'SELECT kind FROM events WHERE run_id=? ORDER BY id', (task,))]

    def count(self, sql, *args):
        return self.store.graph.db.execute(sql, args).fetchone()[0]


class FinishGateTests(FlowCase):
    def test_nothing_finishes_while_a_decision_is_unsigned_on_any_path(self):
        self.store.update_settings({'auto_rules': True})
        task = self.start()['task_id']
        n = self.node(task, client_ref='n1')
        self.assertEqual((n['status'], n['owner'], n['blocking']), ('pending', 'Wes Chen', True))
        # An open question waits on its owner.
        self.assertIn('waiting on Wes Chen', self.refused('bridge_finish_task', {'task_id': task}))
        # The agent's own proposal is evidence, not sign-off.
        settled = self.settle(task, n['node_id'])
        self.assertEqual((settled['status'], settled['signoff'], settled['authorized']), ('resolved', 'required', False))
        self.assertIn('sign-off wanted from Wes Chen', self.refused('bridge_finish_task', {'task_id': task}))
        # Wes answers; the agent reads it; the task finishes.
        self.assertEqual(self.answer(n['node_id'])['status'], 'approved')
        self.tree(task)
        self.assertEqual(self.ok('bridge_finish_task', {'task_id': task})['status'], 'completed')
        # Wes makes his answer a standing rule: a later twin of the question
        # comes back authorized by the rule, with no signature on its task.
        self.assertTrue(self.rule(n['node_id'])['reusable'])
        second = self.start(key='k2', title='Bill the usage spike for the new plan in billing')['task_id']
        covered = self.node(second, client_ref='c1')
        self.assertEqual((covered['signoff'], covered['authorized'], covered['blocking']), ('rule', True, False))
        # The rule ends: what it authorized on the open task wants a person
        # again, and the finish says so.
        self.assertFalse(self.rule(n['node_id'], end=True)['reusable'])
        refusal = self.refused('bridge_finish_task', {'task_id': second})
        self.assertIn(covered['node_id'], refusal)
        self.assertIn('needs review', refusal)
        doubted = self.decision(covered['node_id'])
        self.assertFalse(doubted['authorized'])
        # A node a rule covered was routed to nobody; with the rule gone a
        # person recovers it only through the complete current-source
        # review, pinning the version of the answer it leaned on.
        self.assertEqual(self.status_of('POST', f"/api/decisions/{covered['node_id']}/answer",
                                        {'answer': 'Bill it', 'expected_updated_at': doubted['updated_at']},
                                        token=self.wes), 400)
        pins = doubted['source_revalidation']['decision_pins']
        self.assertEqual([p['decision_id'] for p in pins], [n['node_id']])
        signed = self.sign(covered['node_id'], answer='Bill it, capped at 2x, for the new plan too.',
                           source_decision_pins=pins)
        self.assertTrue(signed['authorized'])
        self.tree(second)
        self.assertEqual(self.ok('bridge_finish_task', {'task_id': second})['status'], 'completed')
        self.assertEqual(self.count("SELECT count(*) FROM runs WHERE status='completed'"), 2)


class AgentCredentialTests(FlowCase):
    def test_an_agent_credential_cannot_answer_sign_refer_invite_or_administer(self):
        task = self.start()['task_id']
        did = self.node(task)['node_id']
        before = self.store.get_decision(did)
        revision = before['updated_at']
        people = self.count('SELECT count(*) FROM people')
        probes = [
            (f'/api/decisions/{did}/answer', {'answer': 'Bill it', 'expected_updated_at': revision}),
            (f'/api/decisions/{did}/signoff', {'expected_updated_at': revision}),
            (f'/api/decisions/{did}/signoff', {'answer': 'Bill it', 'expected_updated_at': revision}),
            (f'/api/decisions/{did}/refer', {'to': 'Ada Example', 'reason': 'mine'}),
            (f'/api/decisions/{did}/assign', {'owner_id': 'anyone'}),
            (f'/api/decisions/{did}/followups', {'questions': ['And this?'], 'required': True}),
            (f'/api/decisions/{did}/rule', {'expected_updated_at': revision}),
            (f'/api/decisions/{did}/reframe', {'question': 'A different question?'}),
            ('/api/invitations', {'email': 'mallory@example.invalid', 'role': 'admin'}),
            ('/api/settings', {'auto_rules': True}),
            ('/api/people', {'name': 'Mallory', 'role': 'admin'}),
            ('/api/authority', {'person': self.ada['id'], 'scope_kind': 'repo', 'scope': REPO, 'role': 'decides'}),
            ('/api/tokens', {'person_id': self.wes_id, 'kind': 'human'}),
            ('/api/slack/sync', {}),
            (f'/api/tasks/{task}/notes', {'text': 'Approved, go ahead.'}),
        ]
        for path, body in probes:
            with self.subTest(path=path):
                self.assertEqual(self.status_of('POST', path, body, token=self.agent), 403)
        # The agent token is not a task link, and no MCP tool answers or signs.
        status, _, _ = self.raw('POST', '/api/brief/answer', {'Content-Type': 'application/json',
                                                              'X-Raven-Link': self.agent},
                                json.dumps({'decision_id': did, 'answer': 'Bill it',
                                            'expected_updated_at': revision}).encode())
        self.assertIn(status, (401, 403))
        tools = self.post('/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, token=self.agent)
        names = {t['name'] for t in tools['result']['tools']}
        self.assertFalse({n for n in names if any(w in n for w in ('answer', 'sign', 'refer', 'invite', 'setting'))})
        for name in ('bridge_answer', 'bridge_sign_off', 'bridge_refer'):
            self.assertTrue(self.mcp(name, {'task_id': task, 'node_id': did, 'answer': 'Bill it'}, self.agent)['isError'])
        self.assertEqual(self.store.get_decision(did), before)
        self.assertFalse({'owner_approved', 'signoff', 'signature', 'answer_corrected', 'referred', 'followup_added',
                          'rule_made', 'task_note'} & set(self.events(task)))
        self.assertEqual(self.count('SELECT count(*) FROM people'), people)
        self.assertNotEqual(self.store.graph.get_setting('auto_rules'), '1')


class CorrectionTests(FlowCase):
    def test_a_corrected_answer_puts_dependents_up_for_review_and_the_agent_hears_it(self):
        task = self.host('prompt', prompt=PROMPT)['task_id']
        a = self.node(task, client_ref='a')
        self.answer(a['node_id'])
        b = self.node(task, question='Which multiplier caps the spike?', parent_id=a['node_id'], client_ref='b')
        self.settle(task, b['node_id'], '2x, as the parent answer says')
        self.assertTrue(self.sign(b['node_id'])['authorized'])
        tree = self.tree(task)
        self.assertEqual(tree['counts']['blocking'], 0)
        observed = tree['observed_at']
        # The agent read both answers on the tree: nothing to wake it for.
        self.assertIsNone(self.host('poll', worker='w')['message'])
        corrected = self.sign(a['node_id'], answer='Do not bill the spike; absorb it this cycle.')
        self.assertEqual((corrected['status'], corrected['authorized']), ('answered', True))
        child = self.decision(b['node_id'])
        self.assertTrue(child['needs_review'])
        self.assertIn(a['node_id'], child['review_reason'])
        tree = self.tree(task)
        self.assertTrue(tree['needs_review'])
        shown = {n['node_id']: n for n in canvas._flatten(tree['nodes'])}
        self.assertEqual((shown[b['node_id']]['needs_review'], shown[b['node_id']]['blocking'],
                          shown[b['node_id']]['authorized']), (True, True, False))
        self.assertIn('need', tree['next'])
        refusal = self.refused('bridge_finish_task', {'task_id': task})
        self.assertIn(b['node_id'], refusal)
        self.assertIn('needs review', refusal)
        # The agent is told on every channel it reads: the wait since its
        # last look, and the host mailbox that resumes a stopped session.
        waited = self.ok('bridge_wait', {'task_id': task, 'since': observed, 'timeout': '1'})
        changed = {c['node_id']: c for c in waited['changed']}
        self.assertIn(a['node_id'], changed)
        self.assertTrue(changed[b['node_id']]['needs_review'])
        self.assertFalse(changed[b['node_id']]['authorized'])
        self.assertIsNotNone(self.host('poll', worker='w')['message'])
        kinds = self.events(task)
        self.assertIn('answer_corrected', kinds)
        self.assertIn('dependent_flagged', kinds)


class LateFollowUpTests(FlowCase):
    def test_a_required_follow_up_added_after_completion_can_be_adopted_and_answered(self):
        task = self.host('prompt', prompt=PROMPT)['task_id']
        a = self.node(task, client_ref='a')
        self.answer(a['node_id'])
        self.tree(task)
        self.assertEqual(self.ok('bridge_finish_task', {'task_id': task})['status'], 'completed')
        self.assertFalse(self.host('stop')['hold'])
        # Wes adds a required follow-up after the task finished.
        added = self.post(f'/api/decisions/{a["node_id"]}/followups',
                          {'questions': ['Which release note must mention the cap?'], 'required': True}, token=self.wes)
        follow = added['nodes'][0]
        self.assertEqual((follow['status'], follow['followup_required']), ('suggested', True))
        tree = self.tree(task)
        self.assertEqual((tree['status'], tree['counts']['blocking']), ('completed', 1))
        self.assertIn('adopt', tree['next'])
        self.assertIsNotNone(self.host('poll', worker='w')['message'])
        self.assertIn('required follow-up', self.refused('bridge_finish_task', {'task_id': task}))
        # A new decision still has no place on a finished task; the
        # follow-up a person added does, and taking it up reopens the task.
        self.assertIn('finished', self.refused('bridge_add_node', {'task_id': task, 'question': 'Something new?',
                                                                   'paths': 'billing/usage.py'}))
        adoption = {'task_id': task, 'adopt': follow['node_id'], 'question': follow['question'],
                    'paths': 'billing/usage.py', 'client_ref': 'f1'}
        adopted = self.ok('bridge_add_node', adoption)
        self.assertEqual((adopted['status'], adopted['owner'], adopted['parent_id'], adopted['origin']),
                         ('pending', 'Wes Chen', a['node_id'], 'human'))
        self.assertEqual(self.ok('bridge_add_node', adoption)['node_id'], adopted['node_id'])
        tree = self.tree(task)
        # Open again, with the adopted question waiting on Wes.
        self.assertEqual((tree['status'], tree['followups'], tree['counts']['blocking']), ('needs_judgment', [], 1))
        self.assertIn('task_reopened', self.events(task))
        # The same session, binding and Stop hold carry on: the agent is
        # held while Wes answers, and the finish gate applies afresh.
        self.assertTrue(self.host('stop')['hold'])
        self.assertIn('waiting on Wes Chen', self.refused('bridge_finish_task', {'task_id': task}))
        self.answer(adopted['node_id'], 'The 1.26 release note.')
        self.tree(task)
        self.assertEqual(self.ok('bridge_finish_task', {'task_id': task})['status'], 'completed')
        self.assertEqual(self.tree(task)['counts']['blocking'], 0)
        self.assertFalse(self.host('stop')['hold'])
        self.assertEqual(self.count('SELECT count(*) FROM runs'), 1)
        self.assertEqual(self.count("SELECT count(*) FROM decisions WHERE run_id=? AND status='suggested'", task), 0)


class RetryTests(FlowCase):
    def test_retries_with_the_same_keys_never_duplicate_tasks_nodes_or_notifications(self):
        self.store.connect_delivery(FakeSlack(), base_url='https://raven.example')
        first = self.start(key='same')
        self.assertEqual({self.start(key='same')['task_id'] for _ in range(3)}, {first['task_id']})
        task = first['task_id']
        with ThreadPoolExecutor(max_workers=4) as pool:
            nodes = list(pool.map(lambda _: self.node(task, client_ref='n1'), range(4)))
        self.assertEqual(len({n['node_id'] for n in nodes}), 1)
        did = nodes[0]['node_id']
        self.assertEqual(self.count('SELECT count(*) FROM decisions WHERE run_id=?', task), 1)
        self.assertEqual(self.count("SELECT count(*) FROM notifications WHERE decision_id=? AND kind='ask'", did), 1)
        for _ in range(2):
            self.settle(task, did)
        self.assertEqual(self.count("SELECT count(*) FROM notifications WHERE decision_id=? AND kind='signoff' "
                                    "AND state IN ('queued','sent')", did), 1)
        bound = [self.host('new_task', prompt='A separate task for the gateway', event_key='hook-1')['task_id']
                 for _ in range(3)]
        self.assertEqual(len(set(bound)), 1)
        self.assertEqual(self.count('SELECT count(*) FROM runs'), 2)
        self.assertEqual(self.count('SELECT count(*) FROM host_sessions'), 1)


class RestartTests(FlowCase):
    def restart(self):
        """The Raven process goes away and comes back on the same database."""
        self.server.shutdown()
        wait_for_background(10)
        self.store.graph.close()
        self.store = Store(Path(self.temp.name) / 'shared.db')
        self.auth = Auth(self.store, enabled=True)
        self.server = make_server(self.store, port=0, auth=self.auth)
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_port
        self.base = f'http://127.0.0.1:{self.port}'

    def test_a_restart_mid_task_loses_no_binding_hold_outbox_or_source_state(self):
        slack = FakeSlack()
        slack.fail = True
        self.store.connect_delivery(slack, base_url='https://raven.example')
        site = Site()
        self.addCleanup(site.close)
        site.page('/llms.txt', '- [Guide](/guide)\n', content_type='text/plain')
        site.page('/guide', PAGE, etag='"v1"')
        connection = sources.register(self.store, 'docs', REPO, site.origin + '/', shared=True,
                                      client=source_docs.HTTPClient())
        self.assertGreater(sources.sync(self.store, connection['id'], client=source_docs.HTTPClient())['records'], 0)
        source_before = sources._row(self.store, connection['id'])
        items_before = sources._items(self.store, connection['id'])
        task = self.host('prompt', prompt=PROMPT)['task_id']
        n = self.node(task, client_ref='n1')
        self.assertEqual(n['status'], 'pending')
        self.assertEqual(self.store.delivery.deliver_now(), 0)  # Slack is down: the ask stays queued
        self.assertEqual(self.count("SELECT count(*) FROM notifications WHERE state='queued'"), 1)
        self.assertTrue(self.host('stop')['hold'])
        held_since = self.store.graph.db.execute('SELECT held_since FROM host_activity').fetchone()[0]
        self.assertTrue(held_since)

        self.restart()

        # The same tokens, binding, task and hold clock.
        self.assertEqual(self.host('connect')['task_id'], task)
        self.assertTrue(self.host('stop')['hold'])
        self.assertEqual(self.store.graph.db.execute('SELECT held_since FROM host_activity').fetchone()[0], held_since)
        # The queued ask is still in the outbox and goes out once Slack answers.
        recovered = FakeSlack()
        self.store.connect_delivery(recovered, base_url='https://raven.example')
        with patch('bridge.delivery.time.time', return_value=time.time() + 3600):
            self.assertEqual(self.store.delivery.deliver_now(), 1)
        self.assertEqual(len(recovered.messages), 1)
        self.assertEqual(self.count("SELECT count(*) FROM notifications WHERE state='queued'"), 0)
        # The source connection, its cursor and its item state survived.
        source_after = sources._row(self.store, connection['id'])
        self.assertEqual((source_after['cursor'], source_after['last_success_at'], source_after['enabled']),
                         (source_before['cursor'], source_before['last_success_at'], True))
        self.assertEqual(sources._items(self.store, connection['id']), items_before)
        again = sources.sync(self.store, connection['id'], client=source_docs.HTTPClient())
        self.assertEqual(again['changed'], 0)
        # The task goes on where it was: Wes answers, the host wakes, the agent finishes.
        self.answer(n['node_id'])
        self.assertFalse(self.host('stop')['hold'])
        self.assertIsNotNone(self.host('poll', worker='w')['message'])
        self.tree(task)
        self.assertEqual(self.ok('bridge_finish_task', {'task_id': task})['status'], 'completed')


class HostReportTests(OfflineCase):
    """The supervisor's resume report goes through the outbox."""

    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'report.db')
        self.addCleanup(self.store.graph.close)
        self.slack = FakeSlack()
        self.delivery = self.store.connect_delivery(self.slack, base_url='https://raven.example')
        self.actor = Identity('person-a', 'Ada Example', 'member', 'agent')
        self.args = {'host': 'claude', 'session_id': 'session-a', 'project': '/example/checkout', 'repo': REPO,
                     'prompt': PROMPT}
        self.task = self.event('prompt')['task_id']
        with self.store.graph.transaction():
            self.did = self.store.graph.add_decision(self.task, QUESTION, '', 'pending', repo=REPO,
                                                     owner='Wes Chen', path='billing/usage.py')
            self.store.graph.db.execute(
                "INSERT INTO notifications(id, decision_id, run_id, kind, channel, dedupe_key, external_ref, state, "
                "person_name, payload, created_at) VALUES('n1', ?, ?, 'ask', 'slack', 'k1', 'D123:1700.5', 'sent', "
                "'Wes Chen', '{}', '')", (self.did, self.task))
            self.store.graph.append_event('signature', {'task_id': self.task, 'decision_id': self.did, 'by': 'Wes Chen'})
        self.message = self.event('poll', worker='w')['message']
        self.assertIsNotNone(self.message)

    def event(self, event, **extra):
        return hs.event(self.store, {**self.args, 'event': event, **extra}, principal=self.actor)

    def report(self, **extra):
        return self.event('report', message_id=self.message['id'], exit_code=0, summary='Applied the cap.',
                          changed=['usage.py (+2 -1)'], **extra)

    def rows(self):
        return [dict(r) for r in self.store.graph.db.execute("SELECT * FROM notifications WHERE kind='report'")]

    def test_a_resume_report_survives_a_slack_outage_and_is_posted_once(self):
        self.slack.fail = True
        first = self.report()
        self.assertEqual((first['reported'], first['state'], first['posted'], first['queued']), (True, 'done', 0, 1))
        (row,) = self.rows()
        self.assertEqual((row['state'], row['attempts'], row['destination'], row['run_id']),
                         ('queued', 1, 'D123', self.task))
        self.assertIn('ratelimited', row['last_error'])
        self.assertEqual(self.slack.messages, [])
        # Replayed while Slack is still down: recorded once, still waiting.
        replay = self.report()
        self.assertEqual((replay['duplicate'], replay['posted'], replay['queued']), (True, 0, 1))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.store.graph.db.execute(
            "SELECT count(*) FROM events WHERE kind='host_resume_report'").fetchone()[0], 1)
        # Slack is back: the replay posts the line, once, in the thread.
        self.slack.fail = False
        recovered = self.report()
        self.assertEqual((recovered['duplicate'], recovered['posted'], recovered['queued']), (True, 1, 0))
        self.assertEqual(len(self.slack.messages), 1)
        message = self.slack.messages[0]
        self.assertEqual((message['channel'], message['thread_ts']), ('D123', '1700.5'))
        self.assertTrue(message['text'].startswith('✅ *Agent continued*'), message['text'])
        self.assertIn('Applied the cap.', message['text'])
        self.assertEqual(self.delivery.deliver_now(), 0)
        self.assertEqual(self.report()['posted'], 1)
        self.assertEqual(len(self.slack.messages), 1)
        self.assertEqual(self.store.graph.db.execute('SELECT posted FROM host_reports').fetchone()[0], 1)
        # A reply in that thread still finds the question, not the report.
        self.assertEqual(self.delivery.notification_for_thread('D123', '1700.5')['kind'], 'ask')

    def test_the_delivery_worker_posts_a_queued_report_after_its_backoff(self):
        self.slack.fail = True
        self.assertEqual(self.report()['queued'], 1)
        self.slack.fail = False
        self.assertEqual(self.delivery.deliver_now(), 0)  # still inside the backoff
        with patch('bridge.delivery.time.time', return_value=time.time() + 3600):
            self.assertEqual(self.delivery.deliver_now(), 1)
        self.assertEqual(len(self.slack.messages), 1)
        self.assertEqual(self.rows()[0]['state'], 'sent')
        self.assertEqual(self.report()['posted'], 1)
        self.assertEqual(len(self.slack.messages), 1)

    def test_a_report_with_no_transport_waits_in_the_outbox_for_the_server(self):
        # A stdio Raven knows Slack is connected but holds no token: the
        # report is queued for the server's worker rather than dropped.
        self.store._delivery = None
        self.store.graph.set_setting('slack_connected', '1')
        self.assertEqual(self.report()['queued'], 1)
        self.assertEqual(self.rows()[0]['state'], 'queued')
        self.assertEqual(self.store.connect_delivery(FakeSlack(), base_url='https://raven.example').deliver_now(), 1)


class ModelOutageTests(FlowCase):
    """A model is configured and every call to it fails at once."""

    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key-not-real', 'BRIDGE_SEMANTIC': '1',
                                      'BRIDGE_MODEL_API': 'anthropic', 'BRIDGE_DEEP': '0'})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []

        def unreachable(client, url, headers, body, timeout=120):
            self.calls.append(url)
            raise LLMError(f'could not reach {url}: ConnectionRefusedError. Tried 4 times; the model API is '
                           'refusing requests right now.')
        reach = patch.object(Client, '_post_json', unreachable)
        reach.start()
        self.addCleanup(reach.stop)

    def test_an_unreachable_model_degrades_every_path_without_hanging(self):
        started = time.monotonic()
        task = self.start()['task_id']
        self.assertTrue(wait_for_background(20))
        discovery = json.loads(self.store.graph.get_task(task)['discovery'])
        self.assertNotIn('model_pending', discovery)
        self.assertNotEqual(discovery.get('verdict_source'), 'model')
        self.assertTrue(self.calls)  # the model was asked and did not answer; the rules verdict stands
        n = self.node(task, client_ref='n1')
        self.assertEqual((n['status'], n['owner']), ('pending', 'Wes Chen'))
        self.assertTrue(wait_for_background(20))
        row = self.store.get_decision(n['node_id'])
        self.assertEqual((row['status'], row['owner_name'], bool(row['model_pending'])), ('pending', 'Wes Chen', False))
        # The ladder absorbed the failed model call and published its
        # deterministic reading; nothing is left pending on the node.
        self.assertTrue({'model_read', 'model_read_failed'} & set(self.events(task)))
        waited = self.ok('bridge_wait', {'task_id': task, 'timeout': '1'})
        self.assertTrue(waited['timed_out'])
        self.assertLess(waited['waited_seconds'], 5)
        self.answer(n['node_id'])
        self.tree(task)
        # The discovery review and the finish gate behind it.
        with self.store.graph.transaction():
            discovery['investigation_required'] = True
            self.store.graph.db.execute("UPDATE runs SET verdict='engage', discovery=? WHERE id=?",
                                        (json.dumps(discovery), task))
        review = self.ok('bridge_review_discovery', {'task_id': task, 'phase': 'finish', 'diff': DIFF,
                                                     'paths': ['billing/usage.py'],
                                                     'observations': 'billing/usage.py:1 sets LIMIT; the cap is new.'})
        self.assertTrue(wait_for_background(20))
        reviews = self.tree(task)['discovery_reviews']
        self.assertEqual((reviews[0]['review_id'], reviews[0]['status'], reviews[0]['error'], reviews[0]['unaddressed']),
                         (review['review_id'], 'unavailable', 'LLMError', []))
        done = self.ok('bridge_finish_task', {'task_id': task, 'diff': DIFF, 'checks': 'unit tests pass'})
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['review']['status'], 'failed')
        self.assertIn('did not complete', done['caveat'])
        self.assertEqual([a['node_id'] for a in done['authorized']], [n['node_id']])
        self.assertTrue(self.calls)
        self.assertLess(time.monotonic() - started, 60)


class SourceOutageTests(OfflineCase):
    def test_an_unreachable_jira_records_the_error_and_marks_nothing_deleted(self):
        store = Store(Path(self.temp.name) / 'jira.db')
        self.addCleanup(store.graph.close)
        jira = JiraDouble()
        self.addCleanup(jira.close)
        env = patch.dict(os.environ, {'JIRA_BASE_URL': jira.base, 'JIRA_EMAIL': 'bot@example.invalid',
                                      'JIRA_API_TOKEN': 'secret-token'})
        env.start()
        self.addCleanup(env.stop)
        jira.issues = [issue(1, '2026-10-01T10:00:00.000+0000'), issue(2, '2026-10-01T11:00:00.000+0000')]
        connection = sources.register(store, 'jira', REPO, 'bill', shared=True)
        self.assertEqual(sources.sync(store, connection['id'])['records'], 2)
        healthy = sources._row(store, connection['id'])

        def records():
            return sorted((r['external_id'], r['availability']) for r in store.graph.db.execute(
                "SELECT external_id, availability FROM source_records WHERE provider='jira'"))
        before = records()
        self.assertEqual(before, [('10001', 'available'), ('10002', 'available')])
        jira.fail = (503, {'errorMessages': ['maintenance']})
        with self.assertRaises(sources.SourceError):
            sources.sync(store, connection['id'])
        outage = sources._row(store, connection['id'])
        self.assertIn('HTTP 503', outage['last_error'])
        self.assertEqual((outage['cursor'], outage['last_success_at'], outage['enabled']),
                         (healthy['cursor'], healthy['last_success_at'], True))
        self.assertEqual(records(), before)
        jira.fail = None
        self.assertEqual(sources._row(store, connection['id'])['last_error'], outage['last_error'])
        sources.sync(store, connection['id'])
        self.assertEqual(sources._row(store, connection['id'])['last_error'], '')
        jira.close()
        with self.assertRaisesRegex(sources.SourceError, 'unreachable'):
            sources.sync(store, connection['id'])
        self.assertIn('unreachable', sources._row(store, connection['id'])['last_error'])
        self.assertEqual(records(), before)
        states = {r[0] for r in store.graph.db.execute('SELECT DISTINCT state FROM native_source_access')}
        self.assertNotIn('revoked', states)
        self.assertEqual(store.graph.db.execute(
            "SELECT count(*) FROM source_records WHERE provider='jira' AND availability<>'available'").fetchone()[0], 0)


class GitHubOutageTests(GitHubCase):
    def test_an_unreachable_github_records_the_error_and_keeps_what_it_synced(self):
        self.assertEqual(sync_repo(self.graph, self.api, GITHUB_REPO)['pulls'], 1)
        healthy = sync_state(self.graph, GITHUB_REPO)
        records = self.graph.db.execute('SELECT count(*) FROM source_records').fetchone()[0]
        self.assertGreater(records, 0)

        def refused(request, timeout=None):
            raise OSError(61, 'Connection refused')
        unreachable = GitHubAPI('token', base='https://api.github.invalid', opener=refused)
        with self.assertRaisesRegex(GitHubError, 'unreachable'):
            sync_repo(self.graph, unreachable, GITHUB_REPO)
        state = sync_state(self.graph, GITHUB_REPO)
        self.assertIn('unreachable', state['last_error'])
        self.assertEqual((state['cursor'], state['last_success_at']), (healthy['cursor'], healthy['last_success_at']))
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM source_records').fetchone()[0], records)
        self.assertEqual(self.graph.db.execute(
            "SELECT count(*) FROM source_records WHERE availability<>'available'").fetchone()[0], 0)
        self.assertEqual(self.approvals(), [(WES[0], 'approved-by')])
        sync_repo(self.graph, self.api, GITHUB_REPO)
        self.assertEqual(sync_state(self.graph, GITHUB_REPO)['last_error'], '')
