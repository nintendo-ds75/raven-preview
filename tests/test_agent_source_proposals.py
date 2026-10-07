"""Unsigned agent proposals bind exact sources without acquiring human authority.

All records, model outcomes and signed Slack HTTP callbacks are synthetic and
local. No provider APIs, real messages or real credentials are used.
"""
import hashlib
import hmac
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from fixtures import OfflineCase, ready_server
import test_delivery as slack
from bridge import canvas, context_memory as cm, mcp
from bridge.config import Config
from bridge.store import Invalid, Store

REPO = 'synthetic/agent-sources'
ANSWER = 'Keep the scoped archive for thirty days.'


class AgentSourceProposalTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'agent-pins.db')
        self.g = self.store.graph
        self.addCleanup(self.g.close)
        self.store.add_owner({'name': 'Policy Owner', 'team': 'Policy', 'patterns': 'policy/*'})
        self.task = self.store.add_run({'title': 'Scoped retention', 'repo': REPO})['id']
        self.did = self.node()
        for method in ('complete', 'complete_json'):
            guard = patch('bridge.llm.Client.' + method, side_effect=AssertionError('Provider calls forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def node(self, **fields):
        return self.g.add_decision(self.task, 'How long should this archive remain?', 'policy', 'pending',
            repo=REPO, owner='Policy Owner', path='policy/retention.py', **fields)

    def source(self, ref='POL-1', **fields):
        return self.store.add_record({'repo': REPO, 'kind': 'jira', 'ref': ref,
            'title': 'Retention policy', 'body': 'Retain this archive for thirty days.',
            'author': 'Source Writer', 'status': 'Done', 'url': 'https://example.invalid/' + ref,
            'paths': ['policy/retention.py'], **fields})['source']

    def pin(self, source, role='support'):
        return {key: source[key] for key in ('record_id', 'source_version_id')} | {'role': role}

    def settle(self, **fields):
        return canvas.settle_node(self.store, {'task_id': self.task, 'node_id': self.did,
            'answer': ANSWER, 'rationale': 'The exact current records support this scoped proposal.', **fields})

    def decision(self):
        return self.store.get_decision(self.did)

    def active(self):
        return [cm.pin(e, e['role']) for e in cm.edges(self.g.db, self.did)]

    def state(self):
        return {table: [dict(r) for r in self.g.db.execute('SELECT * FROM ' + table)]
                for table in ('decisions', 'decision_source_edges', 'decision_versions', 'decision_links', 'events', 'notifications')}

    def assert_unsigned(self, row):
        self.assertFalse(row['authorized'])
        self.assertTrue(row['blocking'])
        self.assertEqual(row['kind'], 'agent')
        self.assertEqual(row['signoff'], 'required')
        self.assertEqual(self.decision()['source'], 'agent')
        self.assertFalse(self.decision()['signed_by'])
        self.assertFalse(self.decision()['answered_by'])
        self.assertFalse(self.decision()['actor_id'])
        self.assertFalse(self.decision()['actor_name'])
        self.assertFalse(self.decision()['actor_basis'])
        self.assertEqual(json.loads(self.decision()['signatures']), [])
        self.assertFalse(self.decision()['reusable'])
        self.assertEqual(self.decision()['independent_source_replacement'], 0)

    def test_exact_multiple_pins_are_visible_but_human_approval_is_still_required(self):
        one, two = self.source(), self.source('POL-2')
        pins = [self.pin(one), self.pin(two, 'contradiction')]
        result = self.settle(source_evidence=pins)
        self.assert_unsigned(result)
        self.assertCountEqual(self.active(), pins)
        self.assertIn('do not ship', result['next'])
        review = self.decision()['source_revalidation']
        self.assertTrue(review['available'])
        self.assertCountEqual(review['pins'], pins)
        self.assertTrue(all(s['snapshot']['body'] for s in review['sources']))
        with self.assertRaises(Invalid):
            canvas.finish_task(self.store, {'task_id': self.task})
        signed = canvas.sign_off(self.store, self.did, {'by': 'Policy Owner',
            'expected_updated_at': self.decision()['updated_at'], 'source_evidence': review['pins'],
            'source_decision_pins': review['decision_pins']})
        self.assertTrue(signed['authorized'])
        self.assertCountEqual(self.active(), pins)

    def test_omitted_or_empty_pins_do_not_infer_provenance_from_prose(self):
        source = self.source()
        for fields in ({}, {'source_evidence': []}):
            with self.subTest(fields=fields):
                self.assert_unsigned(self.settle(rationale='As approved in POL-1 ' + source['source_version_id'], **fields))
                self.assertEqual(self.active(), [])
                self.assertFalse(self.decision()['source_revalidation']['available'])

    def test_bad_pin_shapes_roles_and_sizes_fail_without_writing(self):
        pin = self.pin(self.source())
        malformed = [None, 'POL-1', [None], ['POL-1'], [{}], [{**pin, 'role': 'approval'}],
            [{**pin, 'record_id': ''}], [{**pin, 'source_version_id': 'x' * 101}],
            [{**pin, 'approved': True}], [pin] * 65]
        before = self.state()
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(Invalid):
                self.settle(source_evidence=value)
            self.assertEqual(self.state(), before)

    def test_wrong_record_version_repository_and_missing_pins_fail_atomically(self):
        one, two = self.source(), self.source('POL-2')
        foreign = self.source('POL-3', repo='synthetic/elsewhere')
        invalid = [self.pin(one) | {'source_version_id': two['source_version_id']},
            self.pin(one) | {'record_id': 'missing'}, self.pin(one) | {'source_version_id': 'missing'}, self.pin(foreign)]
        before = self.state()
        for pin in invalid:
            with self.subTest(pin=pin), self.assertRaisesRegex(Invalid, 'bridge_get_record'):
                self.settle(source_evidence=[pin])
            self.assertEqual(self.state(), before)

    def test_selected_source_namespace_is_enforced_for_all_roles(self):
        selected = self.source(provider='jira', namespace='selected.example', task_id=self.task)
        other = self.source(provider='jira', namespace='other.example')
        self.settle(source_evidence=[self.pin(selected)])
        before = self.state()
        for role in cm.ROLES:
            with self.subTest(role=role), self.assertRaisesRegex(Invalid, 'selected task namespace'):
                self.settle(source_evidence=[self.pin(other, role)])
            self.assertEqual(self.state(), before)

    def test_namespace_selection_cannot_hide_an_existing_outside_dependency(self):
        outside = self.source(provider='jira', namespace='other.example')
        self.settle(source_evidence=[self.pin(outside)])
        selected = self.source(provider='jira', namespace='selected.example', task_id=self.task)
        before = self.state()
        with self.assertRaisesRegex(Invalid, 'selected task namespace'):
            self.settle(source_evidence=[self.pin(selected)])
        self.assertEqual(self.state(), before)
        self.assertEqual(self.active(), [self.pin(outside)])

    def test_stale_second_pin_rolls_back_the_entire_proposal(self):
        one, two = self.source(), self.source('POL-2')
        self.source('POL-2', body='Retain for seven days.', source_version='source-v2')
        before = self.state()
        with self.assertRaisesRegex(Invalid, 'current source versions'):
            self.settle(source_evidence=[self.pin(one), self.pin(two)])
        self.assertEqual(self.state(), before)

    def test_unavailable_source_and_expired_or_pending_lease_refuse_all_roles(self):
        for availability in ('deleted', 'inaccessible'):
            source = self.source(availability=availability)
            before = self.state()
            with self.subTest(availability=availability), self.assertRaisesRegex(Invalid, 'unavailable'):
                self.settle(source_evidence=[self.pin(source)])
            self.assertEqual(self.state(), before)
        source = self.source(availability='available')
        self.g.db.execute("INSERT INTO context_connections(repo,collection,audience) VALUES(?,?,?)", (REPO, 'local', '[]'))
        self.g.db.execute("INSERT INTO context_entities(record_id,repo,collection,entity_id,source_name,chunk_index,generation,verified_until,checked_at,state,query) VALUES(?,?,?,?,?,0,1,?,?,?,?)",
            (source['record_id'], REPO, 'local', 'synthetic-entity', 'fixture', '2099-01-01T00:00:00+00:00',
             '2026-01-01T00:00:00+00:00', 'pending', 'retention'))
        for state, until in (('pending', '2099-01-01T00:00:00+00:00'), ('fresh', '2000-01-01T00:00:00+00:00')):
            self.g.db.execute('UPDATE context_entities SET state=?,verified_until=?', (state, until))
            before = self.state()
            for role in cm.ROLES:
                with self.subTest(state=state, role=role), self.assertRaisesRegex(Invalid, 'source access must be fresh'):
                    self.settle(source_evidence=[self.pin(source, role)])
                self.assertEqual(self.state(), before)

    def test_existing_pins_are_additive_and_cannot_be_dropped_or_downgraded(self):
        one, two = self.source(), self.source('POL-2')
        self.settle(source_evidence=[self.pin(one)])
        self.settle(source_evidence=[self.pin(two), self.pin(one, 'context')])
        retained = [self.pin(one), self.pin(one, 'context'), self.pin(two)]
        self.assertCountEqual(self.active(), retained)
        self.settle(source_evidence=[])
        self.assertCountEqual(self.active(), retained)
        self.settle()
        self.assertCountEqual(self.active(), retained)

    def test_stale_existing_pins_cannot_be_refreshed_by_an_agent(self):
        one = self.source()
        self.settle(source_evidence=[self.pin(one)])
        newer = self.source(body='Retain for seven days.', source_version='v2')
        before = self.state()
        with self.assertRaisesRegex(Invalid, 'current-source review from a person'):
            self.settle(source_evidence=[self.pin(newer)])
        self.assertEqual(self.state(), before)
        review_reason = self.decision()['review_reason']
        self.settle()
        self.assertTrue(self.decision()['needs_review'])
        self.assertEqual(self.decision()['review_reason'], review_reason)
        self.assertEqual(self.active(), [self.pin(one)])
        self.assertEqual(self.g.db.execute('SELECT stale FROM decision_source_edges WHERE decision_id=? AND active=1', (self.did,)).fetchone()[0], 1)
        self.assertFalse(self.decision()['authorized'])

    def test_existing_decision_dependencies_and_unknown_provenance_survive(self):
        parent = self.node()
        self.store.answer(parent, {'answer': 'Use thirty days.'})
        parent_row = self.store.get_decision(parent)
        with self.g.transaction():
            version = cm.snapshot_decision(self.g.db, parent)
            self.g.update_decision(self.did, source='memory', source_id=parent, source_revision=parent_row['updated_at'])
            self.g.add_link(self.did, parent, 'derived', 'Exact prior decision')
            self.g.db.execute("UPDATE decision_links SET source_version_id=? WHERE decision_id=?", (version, self.did))
        self.settle(source_evidence=[self.pin(self.source())])
        child = self.decision()
        self.assertEqual(child['source_id'], parent)
        self.assertEqual(child['source_revision'], parent_row['updated_at'])
        link = self.g.db.execute('SELECT related_id,source_version_id FROM decision_links WHERE decision_id=?', (self.did,)).fetchone()
        self.assertEqual(tuple(link), (parent, version))
        self.assertEqual(child['source_revalidation']['decision_pins'][0]['decision_id'], parent)
        self.store.answer(parent, {'answer': 'Use seven days.', 'expected_updated_at': parent_row['updated_at']})
        self.assertTrue(self.decision()['needs_review'])
        before = self.state()
        with self.assertRaisesRegex(Invalid, 'current-source review from a person'):
            self.settle(source_evidence=[])
        self.assertEqual(self.state(), before)
        self.settle()
        self.assertTrue(self.decision()['needs_review'])
        self.assertEqual(self.decision()['source_id'], parent)

        # A manual proposal cannot retrospectively recover unknown provenance.
        self.did = self.node()
        self.g.update_decision(self.did, status='resolved', source='record', kind='evidence', answer='Legacy claim')
        self.settle(source_evidence=[self.pin(self.source())])
        self.assertEqual(self.decision()['source_reuse_state'], 'unknown')
        self.assertFalse(self.decision()['source_revalidation']['available'])

    def test_same_text_proposal_clears_partial_signatures_and_cannot_make_a_grant(self):
        self.settle()
        self.g.db.execute("UPDATE decisions SET signatures=?,signed_by=?,signed_hash='old',signed_revision='old',reusable=1,answered_by='Previous author',actor_id='previous-actor',actor_name='Previous author',actor_basis='old answer' WHERE id=?",
            (json.dumps([{'by': 'Policy Owner', 'answer': ANSWER}]), 'Policy Owner', self.did))
        row = self.settle(source_evidence=[self.pin(self.source())])
        self.assert_unsigned(row)
        self.assertEqual(self.decision()['signed_hash'], '')
        self.assertEqual(self.decision()['signed_revision'], '')
        old = [json.loads(v['snapshot'])['decision'] for v in cm.history(self.g.db, self.did)]
        self.assertTrue(any(d['signed_by'] == 'Policy Owner' and d['reusable'] for d in old))
        self.assertTrue(self.decision()['rule_ended_at'])

    def test_mcp_advertises_and_accepts_typed_pins_with_optional_manual_mode(self):
        listed = mcp.dispatch(self.store, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['result']['tools']
        spec = next(t for t in listed if t['name'] == 'bridge_settle_node')
        schema = spec['inputSchema']
        self.assertNotIn('source_evidence', schema['required'])
        self.assertEqual(schema['properties']['source_evidence']['maxItems'], cm.AGENT_SOURCE_LIMIT)
        self.assertEqual(schema['properties']['source_evidence']['items']['required'], ['record_id', 'source_version_id', 'role'])
        self.assertFalse(schema['properties']['source_evidence']['items']['additionalProperties'])
        self.assertIn('bridge_wait', spec['description'])
        pins = [self.pin(self.source())]
        args = {'task_id': self.task, 'node_id': self.did, 'answer': ANSWER, 'source_evidence': pins}
        result = mcp.dispatch(self.store, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'bridge_settle_node', 'arguments': args}})['result']
        self.assertFalse(result['isError'], result)
        self.assert_unsigned(json.loads(result['content'][0]['text']))
        self.assertEqual(self.active(), pins)
        for value in ([pins[0]] * 65, ['a'], [{**pins[0], 'role': 'approval'}]):
            args['source_evidence'] = value
            before = self.state()
            result = mcp.dispatch(self.store, {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'bridge_settle_node', 'arguments': args}})['result']
            self.assertTrue(result['isError'])
            self.assertEqual(self.state(), before)

    def test_rest_settle_uses_the_same_atomic_pin_contract(self):
        server = ready_server(self.store, port=0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        source = self.source()
        with urlopen(f'http://127.0.0.1:{server.server_port}/api/state', timeout=5) as response:
            csrf = json.loads(response.read())['csrf_token']
        def post(pins):
            request = Request(f'http://127.0.0.1:{server.server_port}/api/tasks/{self.task}/settle',
                data=json.dumps({'node_id': self.did, 'answer': ANSWER, 'source_evidence': pins}).encode(),
                method='POST', headers={'Content-Type': 'application/json', 'X-Bridge-CSRF': csrf})
            with urlopen(request, timeout=5) as response:
                return json.loads(response.read())
        result = post([self.pin(source)])
        self.assert_unsigned(result)
        self.assertEqual(self.active(), [self.pin(source)])
        before = self.state()
        with self.assertRaises(HTTPError) as error:
            post([self.pin(source), {**self.pin(source), 'source_version_id': 'missing'}])
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(self.state(), before)

    def test_concurrent_source_change_serializes_after_atomic_proposal(self):
        source = self.source()
        entered, release, attempted, updated = (threading.Event() for _ in range(4))
        original = cm.validate
        failures = []
        def paused(db, did, pins, **kwargs):
            result = original(db, did, pins, **kwargs)
            if did == self.did and pins and not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Publication release timed out')
            return result
        def propose():
            try:
                self.settle(source_evidence=[self.pin(source)])
            except BaseException as error:
                failures.append(error)
            finally:
                self.g.close_thread()
        def change():
            try:
                attempted.set()
                self.source(body='The source changed concurrently.', source_version='v2')
                updated.set()
            except BaseException as error:
                failures.append(error)
            finally:
                self.g.close_thread()
        with patch.object(cm, 'validate', side_effect=paused):
            proposal = threading.Thread(target=propose)
            updater = threading.Thread(target=change)
            proposal.start()
            self.assertTrue(entered.wait(5))
            updater.start()
            self.assertTrue(attempted.wait(5))
            self.assertFalse(updated.wait(.05), 'A source writer crossed the proposal publication transaction')
            release.set()
            proposal.join(5)
            updater.join(5)
        self.assertFalse(proposal.is_alive() or updater.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(updated.is_set())
        self.assertEqual(self.active(), [self.pin(source)])
        self.assertTrue(self.decision()['needs_review'])
        self.assertFalse(self.decision()['authorized'])

    def test_explicit_proposal_supersedes_running_or_not_yet_started_model_result(self):
        source = self.source()
        self.g.update_decision(self.did, model_pending=1)
        scheduled = self.decision()['updated_at']
        entered, release = threading.Event(), threading.Event()
        failures = []
        def ask(*args, **kwargs):
            scratch = self.g.add_decision(self.task, 'Scratch result', 'policy', 'pending', repo=REPO,
                owner='Policy Owner', path='policy/retention.py', draft=1)
            self.g.update_decision(scratch, status='resolved', answer='Older background outcome', signoff='required')
            entered.set()
            if not release.wait(5):
                raise AssertionError('Model release timed out')
            return {'id': scratch, 'drafts': [scratch]}
        def background():
            try:
                canvas._model_node_pass(self.store, Config(model_api='none'), self.did, '', ['policy/retention.py'], {}, '', None, '', scheduled)
            except BaseException as error:
                failures.append(error)
            finally:
                self.g.close_thread()
        with patch('bridge.ladder.ask', side_effect=ask):
            worker = threading.Thread(target=background)
            worker.start()
            self.assertTrue(entered.wait(5))
            # Even timestamp collision does not let the old result win.
            with patch('bridge.graph.now_iso', return_value=scheduled):
                result = self.settle(source_evidence=[self.pin(source)])
            self.assertFalse(result['model_pending'])
            self.assertIn('do not ship', result['next'])
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.decision()['answer'], ANSWER)
        self.assertEqual(self.active(), [self.pin(source)])
        self.assert_unsigned(canvas.node_view(self.store, self.did))
        with patch('bridge.ladder.ask', side_effect=AssertionError('Superseded model must not start')) as infer:
            background()
        infer.assert_not_called()
        self.assertEqual(failures, [])


class AgentSourceSlackReadbackTests(slack.DeliveryCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.delivery.close)
        self.task_id = self.task()
        self.node_id = self.node(self.task_id, facts='customer=scope-customer,release=r17')['node_id']
        self.graph.db.execute('UPDATE decisions SET scope_paths=? WHERE id=?',
            (json.dumps(['billing/usage.py', 'billing/rollout.py']), self.node_id))
        self.sources = [self.store.add_record({'repo': 'acme/platform', 'kind': 'jira', 'ref': f'BILL-{i}',
            'title': f'Source {i}', 'body': f'COMPLETE_SOURCE_{i}: exclude internal load tests.',
            'author': 'Source Writer', 'status': 'Done'})['source'] for i in (1, 2)]
        self.pins = [cm.pin(s) for s in self.sources]
        self.answer = 'SCOPED_AGENT_ANSWER: Exclude internal load tests for scope-customer release r17.'
        canvas.settle_node(self.store, {'task_id': self.task_id, 'node_id': self.node_id,
            'answer': self.answer, 'source_evidence': self.pins})
        self.delivery.deliver_now()
        notifications = [dict(r) for r in self.graph.db.execute("SELECT * FROM notifications WHERE decision_id=? AND kind='signoff' AND state='sent'", (self.node_id,))]
        self.assertTrue(notifications)
        self.channel, self.thread = notifications[-1]['external_ref'].split(':', 1)
        self.server = ready_server(self.store, port=0, slack_signing_secret='local-synthetic-signing-secret')
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.counter = 0
        for method in ('complete', 'complete_json'):
            guard = patch('bridge.llm.Client.' + method, side_effect=AssertionError('Provider calls forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def send(self, text):
        self.counter += 1
        event_id = f'agent-source-http-{self.counter}'
        body = json.dumps({'type': 'event_callback', 'team_id': 'TTEST', 'event_id': event_id,
            'event': {'type': 'message', 'user': 'UWES', 'text': text, 'channel': self.channel,
                'thread_ts': self.thread, 'ts': f'1700000001.{self.counter:06d}'}}).encode()
        stamp = str(int(time.time()))
        signature = 'v0=' + hmac.new(b'local-synthetic-signing-secret',
            b'v0:' + stamp.encode() + b':' + body, hashlib.sha256).hexdigest()
        request = Request(f'http://127.0.0.1:{self.server.server_port}/webhooks/slack', data=body, method='POST',
            headers={'Content-Type': 'application/json', 'X-Slack-Request-Timestamp': stamp, 'X-Slack-Signature': signature})
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            receipt = self.graph.db.execute('SELECT state FROM slack_ingress WHERE id=?', (event_id,)).fetchone()
            reply = self.graph.db.execute('SELECT state FROM slack_replies WHERE id=?', (event_id,)).fetchone()
            if receipt is not None and receipt['state'] == 'done' and reply is not None and reply['state'] == 'sent':
                return
            time.sleep(.01)
        self.fail('Local signed Slack callback did not complete')

    def held(self):
        return self.delivery._reading(self.channel, self.thread, self.wes)

    def test_signed_local_slack_reads_every_pin_and_full_scope_before_approval(self):
        notification = next(m for m in self.slack.messages if m['ts'] == self.thread)
        from bridge.approval_scope import transport_text
        for value in (self.answer, 'scope-customer', 'billing/rollout.py'):
            self.assertIn(transport_text(value), notification['text'])
        self.assertFalse(self.store.get_decision(self.node_id)['authorized'])
        self.send('sign off')
        held = self.held()
        self.assertIsNotNone(held)
        self.assertTrue(held['delivered_ref'])
        payload = json.loads(held['source_review'])
        self.assertCountEqual(payload['source_evidence'], self.pins)
        self.assertEqual(payload['decision']['answer'], self.answer)
        for value in (self.answer, 'scope-customer', 'billing/rollout.py', 'COMPLETE_SOURCE_1', 'COMPLETE_SOURCE_2',
                      *(s['record_id'] for s in self.sources), *(s['source_version_id'] for s in self.sources)):
            self.assertIn(value, held['prompt'])
        self.assertEqual(self.slack.messages[-1]['text'], held['prompt'])
        self.assertFalse(self.store.get_decision(self.node_id)['authorized'])
        self.send('yes')
        self.assertFalse(self.store.get_decision(self.node_id)['authorized'])
        self.send('confirm ' + held['proposal_id'])
        row = self.store.get_decision(self.node_id)
        self.assertTrue(row['authorized'])
        self.assertEqual(row['source'], 'agent')
        self.assertCountEqual([cm.pin(e, e['role']) for e in row['sources']], self.pins)
        self.assertFalse(row['reusable'])

    def test_adding_evidence_to_same_answer_notifies_a_fresh_complete_review(self):
        self.send('sign off')
        prior = self.held()
        count = self.graph.db.execute("SELECT count(*) FROM notifications WHERE decision_id=? AND kind='signoff'", (self.node_id,)).fetchone()[0]
        source = self.store.add_record({'repo': 'acme/platform', 'kind': 'jira', 'ref': 'BILL-3',
            'title': 'Third source', 'body': 'COMPLETE_THIRD_SOURCE', 'status': 'Done'})['source']
        canvas.settle_node(self.store, {'task_id': self.task_id, 'node_id': self.node_id,
            'answer': self.answer, 'source_evidence': [cm.pin(source)]})
        self.assertEqual(self.graph.db.execute("SELECT count(*) FROM notifications WHERE decision_id=? AND kind='signoff'", (self.node_id,)).fetchone()[0], count + 1)
        self.delivery.deliver_now()
        self.send('confirm ' + prior['proposal_id'])
        self.assertFalse(self.store.get_decision(self.node_id)['authorized'])
        self.send('sign off')
        fresh = self.held()
        self.assertNotEqual(fresh['proposal_id'], prior['proposal_id'])
        self.assertCountEqual(json.loads(fresh['source_review'])['source_evidence'], self.pins + [cm.pin(source)])
        self.assertIn('COMPLETE_THIRD_SOURCE', fresh['prompt'])

    def test_signed_local_slack_stale_second_source_cannot_approve_held_proposal(self):
        self.send('sign off')
        held = self.held()
        self.store.add_record({'repo': 'acme/platform', 'kind': 'jira', 'ref': 'BILL-2',
            'title': 'Source 2', 'body': 'A changed second premise.', 'status': 'Done', 'source_version': 'newer'})
        self.send('confirm ' + held['proposal_id'])
        row = self.store.get_decision(self.node_id)
        self.assertFalse(row['authorized'])
        self.assertTrue(row['needs_review'])
        self.assertCountEqual([cm.pin(e, e['role']) for e in row['sources']], self.pins)
        self.assertEqual(self.graph.db.execute("SELECT count(*) FROM events WHERE kind='readback_confirmed'").fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
