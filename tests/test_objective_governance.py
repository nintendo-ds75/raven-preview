"""Objective head safety: unlocked workbench calls, accepter routing, rule
reconciliation, dispatch backoff and the cheap completion hook. Inference and
the coding workbench are explicit doubles; the store and decision core are real."""
import builtins
import uuid
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import objectives as obj, objective_head as head, canvas
from bridge.auth import Identity
from bridge.config import Config
from bridge.store import Invalid, now


class FakeWorkbench:
    """Records every call and refuses to run under the objectives writer lock."""

    def __init__(self, store, *, fail=False):
        self.store, self.fail, self.submits, self.controls = store, fail, [], []

    def _unlocked(self):
        if self.store.graph.db.in_transaction:
            raise AssertionError('The workbench must not be called inside the objectives writer transaction')

    def submit(self, data, actor):
        self._unlocked()
        self.submits.append(data)
        if self.fail:
            raise Invalid('Host is not configured')
        db = self.store.graph.db
        prior = db.execute('SELECT task_id FROM browser_tasks WHERE request_key=?', (data['request_key'],)).fetchone()
        if prior:
            return {'task_id': prior['task_id'], 'status': 'queued'}
        task_id, stamp = uuid.uuid4().hex[:12], now()
        with self.store.graph.transaction():
            db.execute("INSERT INTO runs(id,title,agent,repo,status,updated_at) VALUES(?,?,?,?,?,?)",
                       (task_id, 'Worker', 'test', data['repository'], 'queued', stamp))
            db.execute('INSERT INTO browser_tasks(task_id,request_key,requester_id,requester,repository,prompt,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                       (task_id, data['request_key'], actor.id, actor.name, data['repository'], data['prompt'], 'queued', stamp, stamp))
        return {'task_id': task_id, 'status': 'queued'}

    def control(self, task_id, action, actor=None):
        self._unlocked()
        self.controls.append((task_id, action))
        status = {'pause': 'paused', 'resume': 'queued', 'cancel': 'cancelled'}[action]
        with self.store.graph.transaction() as db:
            db.execute('UPDATE browser_tasks SET status=? WHERE task_id=?', (status, task_id))
        return {'status': status}

    def finish(self, task_id):
        with self.store.graph.transaction() as db:
            db.execute("UPDATE browser_tasks SET status='completed' WHERE task_id=?", (task_id,))


class GovernanceCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store('objective-governance')
        self.operator = Identity('', 'Local operator', 'admin', 'operator')
        self.payload = {'request_key': 'create-one', 'title': 'Pilot to value', 'outcome': 'Prepare the CSV path',
            'success_criteria': 'Customer independently completes first report', 'stop_criteria': 'Customer declines pilot',
            'guardrails': 'No custom integration or external sending', 'repository': 'sample/project', 'provider': 'claude'}
        self.record_data = {'repo': 'sample/project', 'kind': 'note', 'ref': 'pilot-call', 'body': 'The CSV example does not work.', 'title': 'Pilot call'}
        source = self.store.add_record(self.record_data)['source']
        self.pin = {k: source[k] for k in ('record_id', 'source_version_id')}
        self.payload['source_ids'] = [source['record_id']]

    def member(self, name='Member', role='member'):
        with self.store.graph.transaction():
            person = self.store.graph.add_person(name, role=role)
        return Identity(person, name, role, 'session')

    def proposal(self):
        return {**self.payload, 'why_now': 'Pilot is blocked', 'judgment_kind': 'implementation_uncertainty',
            'cases': {'positive': 'Fix example CSV', 'negative': 'Do not create a custom integration',
                      'boundary': 'Ask if CSV changes billable meaning', 'non_implications': 'No external sending'},
            'citations': [{**self.pin, 'quote': 'The CSV example does not work.'}]}

    def dispatch(self, objective, key, workbench, actor=None):
        current = obj.get(self.store, objective['id'])
        return obj.act(self.store, objective['id'], 'dispatch', {'request_key': key, 'expected_revision': current['revision']},
                       actor or self.operator, workbench)


class UnlockedWorkbenchTests(GovernanceCase):
    def test_dispatch_submits_outside_the_writer_lock_once_per_request_key(self):
        objective = obj.create(self.store, self.payload, self.operator)
        workbench = FakeWorkbench(self.store)
        started = self.dispatch(objective, 'start', workbench)
        self.assertEqual(len(workbench.submits), 1)
        self.assertEqual(len(started['runs']), 1)
        self.assertTrue(started['runs'][0]['browser_task'])
        self.assertEqual(obj.row(self.store, objective['id'])['pending_request'], '')
        # A retry with the same key is answered from the receipt, not resubmitted.
        replay = obj.act(self.store, objective['id'], 'dispatch', {'request_key': 'start', 'expected_revision': 1}, self.operator, workbench)
        self.assertEqual(replay['revision'], started['revision'])
        self.assertEqual(len(workbench.submits), 1)
        with self.assertRaisesRegex(Invalid, 'unfinished worker'):
            self.dispatch(objective, 'again', workbench)
        self.assertEqual(len(workbench.submits), 1)
        workbench.finish(started['runs'][0]['task_id'])
        self.dispatch(objective, 'second-move', workbench)
        self.assertEqual(len(workbench.submits), 2)
        self.assertEqual(len(obj.get(self.store, objective['id'])['runs']), 2)

    def test_failed_submission_releases_the_claim_and_records_nothing(self):
        objective = obj.create(self.store, self.payload, self.operator)
        with self.assertRaisesRegex(Invalid, 'not configured'):
            self.dispatch(objective, 'start', FakeWorkbench(self.store, fail=True))
        saved = obj.row(self.store, objective['id'])
        self.assertEqual((saved['pending_request'], saved['revision']), ('', 1))
        self.assertEqual(obj.get(self.store, objective['id'])['runs'], [])
        self.dispatch(objective, 'start', FakeWorkbench(self.store))
        self.assertEqual(len(obj.get(self.store, objective['id'])['runs']), 1)

    def test_a_live_claim_refuses_a_second_start_until_it_expires(self):
        objective = obj.create(self.store, self.payload, self.operator)
        workbench = FakeWorkbench(self.store)
        with self.store.graph.transaction() as db:
            db.execute("UPDATE objectives SET pending_request='in-flight',pending_since=? WHERE id=?", (now(), objective['id']))
        with self.assertRaisesRegex(Invalid, 'still completing'):
            self.dispatch(objective, 'start', workbench)
        self.assertEqual(workbench.submits, [])
        with self.store.graph.transaction() as db:
            db.execute("UPDATE objectives SET pending_since='2000-01-01T00:00:00+00:00' WHERE id=?", (objective['id'],))
        self.dispatch(objective, 'start', workbench)
        self.assertEqual(len(workbench.submits), 1)

    def test_pause_resume_and_cancel_control_workers_outside_the_lock(self):
        objective = obj.create(self.store, self.payload, self.operator)
        workbench = FakeWorkbench(self.store)
        started = self.dispatch(objective, 'start', workbench)
        task = started['runs'][0]['task_id']
        paused = obj.act(self.store, objective['id'], 'pause', {'request_key': 'pause', 'expected_revision': started['revision']}, self.operator, workbench)
        self.assertEqual(paused['lifecycle'], 'paused')
        self.assertEqual(workbench.controls, [(task, 'pause')])
        resumed = obj.act(self.store, objective['id'], 'resume', {'request_key': 'resume', 'expected_revision': paused['revision']}, self.operator, workbench)
        self.assertEqual(resumed['lifecycle'], 'active')
        self.assertEqual(workbench.controls[-1], (task, 'resume'))
        with self.assertRaisesRegex(Invalid, 'unavailable'):
            obj.act(self.store, objective['id'], 'cancel', {'request_key': 'cancel', 'expected_revision': resumed['revision']}, self.operator, None)
        self.assertEqual(obj.get(self.store, objective['id'])['lifecycle'], 'active')
        cancelled = obj.act(self.store, objective['id'], 'cancel', {'request_key': 'cancel', 'expected_revision': resumed['revision']}, self.operator, workbench)
        self.assertEqual(cancelled['lifecycle'], 'stopped')
        self.assertEqual(workbench.controls[-1], (task, 'cancel'))


class AccepterRoutingTests(GovernanceCase):
    def test_unrouted_governance_node_goes_to_the_accepting_member_who_can_sign_it(self):
        member = self.member()
        proposal = obj.propose(self.store, self.proposal(), [self.pin])
        review = obj.proposal_action(self.store, proposal['id'], 'accept', {'request_key': 'review', 'expected_revision': 1}, member)
        self.assertEqual(review['state'], 'review')
        decision = self.store.get_decision(review['decision_id'])
        owner_id = self.store.graph.owner_id_for_person(member.id)
        self.assertEqual(decision['owner_id'], owner_id)
        self.assertEqual(decision['actor_basis'], 'objective-accepter')
        self.assertIn('objective accepter', decision['routing_reason'])
        self.assertIn(review['decision_id'], [item['id'] for item in self.store.inbox(owner_id=owner_id)['items']])
        head.activate_reviews(self.store)
        self.assertEqual(obj.overview(self.store)['objectives'], [])
        canvas.sign_off(self.store, review['decision_id'], {'expected_updated_at': decision['updated_at']}, actor=member)
        head.activate_reviews(self.store)
        objectives = obj.overview(self.store)['objectives']
        self.assertEqual(len(objectives), 1)
        self.assertEqual(objectives[0]['requester_id'], member.id)
        self.assertEqual(objectives[0]['lifecycle'], 'active')

    def test_authority_map_owner_keeps_the_governance_node(self):
        member = self.member()
        self.store.add_owner({'name': 'Policy Owner', 'team': 'Policy', 'patterns': '*'})
        proposal = obj.propose(self.store, self.proposal(), [self.pin])
        review = obj.proposal_action(self.store, proposal['id'], 'accept', {'request_key': 'review', 'expected_revision': 1}, member)
        decision = self.store.get_decision(review['decision_id'])
        self.assertEqual(decision['owner_name'], 'Policy Owner')
        self.assertNotEqual(decision['owner_id'], self.store.graph.owner_id_for_person(member.id))
        self.assertNotEqual(decision['actor_basis'], 'objective-accepter')

    def test_operator_without_a_person_row_leaves_the_node_visibly_unassigned(self):
        proposal = obj.propose(self.store, self.proposal(), [self.pin])
        review = obj.proposal_action(self.store, proposal['id'], 'accept', {'request_key': 'review', 'expected_revision': 1}, self.operator)
        self.assertFalse(self.store.get_decision(review['decision_id'])['owner_id'])


class StandingRuleTests(GovernanceCase):
    def test_rule_resolved_governance_request_is_reconciled_with_the_rule_named(self):
        self.store.add_owner({'name': 'Policy Owner', 'team': 'Policy', 'patterns': '*'})
        self.store.update_settings({'auto_rules': True})
        question = 'Should Raven take point on: ' + self.payload['title'] + '?'
        task = canvas.start_task(self.store, Config(model_api='none'), {'title': 'Earlier governance', 'repo': 'sample/project', 'client_key': 'earlier'})['task_id']
        node = canvas.add_node(self.store, Config(model_api='none'), {'task_id': task, 'question': question, 'context': 'Earlier scope', 'client_ref': 'earlier-1'})
        row = self.store.get_decision(node['node_id'])
        self.store.answer(node['node_id'], {'answer': 'Yes, within private preparation', 'rationale': 'standing policy',
                                            'signed_by': 'Policy Owner', 'expected_updated_at': row['updated_at']})
        canvas.finish_task(self.store, {'task_id': task})
        row = self.store.get_decision(node['node_id'])
        made = self.store.make_rule(node['node_id'], {'by': 'Policy Owner', 'expected_updated_at': row['updated_at']})
        self.assertTrue(made['reusable'])
        objective = obj.create(self.store, self.payload, self.operator)
        proposal = obj.propose(self.store, self.proposal(), [self.pin], objective_id=objective['id'])
        result = obj.proposal_action(self.store, proposal['id'], 'accept', {'request_key': 'review', 'expected_revision': 1}, self.operator)
        self.assertEqual(result['state'], 'reconcile')
        self.assertIn(node['node_id'], result['rationale'])
        self.assertIn('Policy Owner', result['rationale'])
        self.assertTrue(canvas.node_view(self.store, result['decision_id'])['authorized'])
        current = obj.get(self.store, objective['id'])
        self.assertEqual(current['blocked_reason'], result['rationale'])
        self.assertEqual(current['status'], 'needs_you')
        self.assertEqual([e['kind'] for e in current['events'] if e['kind'] == 'blocked'], ['blocked'])
        # The rule never activates the move; the person has a named way out.
        head.activate_reviews(self.store)
        self.assertEqual(self.store.graph.db.execute('SELECT state FROM objective_proposals WHERE id=?', (proposal['id'],)).fetchone()['state'], 'reconcile')
        with self.assertRaisesRegex(Invalid, 'Standing rule'):
            obj.proposal_action(self.store, proposal['id'], 'accept', {'request_key': 'retry', 'expected_revision': result['revision']}, self.operator)
        dismissed = obj.proposal_action(self.store, proposal['id'], 'dismiss', {'request_key': 'dismiss', 'expected_revision': result['revision']}, self.operator)
        self.assertEqual(dismissed['state'], 'dismissed')


class DispatchBackoffTests(GovernanceCase):
    def accepted(self):
        self.store.add_owner({'name': 'Policy Owner', 'team': 'Policy', 'patterns': '*'})
        proposal = obj.propose(self.store, self.proposal(), [self.pin])
        review = obj.proposal_action(self.store, proposal['id'], 'accept', {'request_key': 'review', 'expected_revision': 1}, self.operator)
        decision = self.store.get_decision(review['decision_id'])
        canvas.sign_off(self.store, review['decision_id'], {'by': 'Policy Owner', 'expected_updated_at': decision['updated_at']})
        head.activate_reviews(self.store)
        return obj.overview(self.store)['objectives'][0]

    def test_blocked_move_is_retried_every_minute_and_recorded_once(self):
        objective = self.accepted()
        worker = head.Worker(self.store, None, Config(model_api='none'), allow_operator=True)
        with patch.object(head.obj, 'act', wraps=obj.act) as act:
            with patch('bridge.objective_head.time.time', return_value=10000):
                worker.dispatch_signed_moves()
                worker.dispatch_signed_moves()
            self.assertEqual(act.call_count, 1)
            with patch('bridge.objective_head.time.time', return_value=10061):
                worker.dispatch_signed_moves()
            self.assertEqual(act.call_count, 2)
        current = obj.get(self.store, objective['id'])
        self.assertIn('not configured', current['blocked_reason'])
        self.assertEqual(current['revision'], objective['revision'] + 1)
        self.assertEqual(len([e for e in current['events'] if e['kind'] == 'dispatch_blocked']), 1)

    def test_block_clears_and_bumps_revision_once_per_changed_reason(self):
        objective = obj.create(self.store, self.payload, self.operator)
        self.assertTrue(obj.block(self.store, objective['id'], 'Host missing'))
        self.assertFalse(obj.block(self.store, objective['id'], 'Host missing'))
        self.assertTrue(obj.block(self.store, objective['id'], ''))
        current = obj.get(self.store, objective['id'])
        self.assertEqual((current['blocked_reason'], current['revision']), ('', 3))
        self.assertEqual(len([e for e in current['events'] if e['kind'] == 'blocked']), 2)


class CompletionHookTests(GovernanceCase):
    def imports(self):
        seen, real = [], builtins.__import__

        def importer(name, globals=None, locals=None, fromlist=(), level=0):
            if (globals or {}).get('__name__') == 'bridge.store' and name in ('objectives', 'bridge.objectives'):
                seen.append(name)
            return real(name, globals, locals, fromlist, level)
        return seen, patch('builtins.__import__', importer)

    def test_ordinary_completion_never_loads_the_objectives_head(self):
        task = self.store.add_run({'title': 'Plain work', 'repo': 'sample/project'})['id']
        seen, importer = self.imports()
        with importer:
            self.assertEqual(self.store.update_run(task, {'status': 'completed'})['status'], 'completed')
        self.assertEqual(seen, [])

    def test_bound_completion_still_consumes_objective_authority(self):
        objective = obj.create(self.store, self.payload, self.operator)
        task = self.store.add_run({'title': 'Owned work', 'repo': 'sample/project'})['id']
        with self.store.graph.transaction() as db:
            db.execute('INSERT INTO objective_run_authority VALUES(?,?,?)', (task, objective['id'], ''))
        obj.act(self.store, objective['id'], 'pause', {'request_key': 'pause', 'expected_revision': 1}, self.operator)
        seen, importer = self.imports()
        with importer:
            with self.assertRaisesRegex(Invalid, 'paused or closed'):
                self.store.update_run(task, {'status': 'completed'})
        self.assertEqual(seen, ['objectives'])


class SchemaTests(GovernanceCase):
    def test_hot_lookups_are_indexed(self):
        if getattr(self.store.graph.db, 'dialect', 'sqlite') != 'sqlite':
            self.skipTest('index catalog query is SQLite-specific')
        names = {r['name'] for r in self.store.graph.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue({'objective_proposals_objective_state', 'objective_events_objective', 'objective_runs_task'} <= names)


class MigrationTests(GovernanceCase):
    def test_each_claim_column_is_added_on_its_own(self):
        import sqlite3
        if sqlite3.sqlite_version_info < (3, 35):
            self.skipTest('needs ALTER TABLE DROP COLUMN')
        db = self.store.graph.db
        # A start that died between the two ALTERs left only pending_request.
        db.execute('ALTER TABLE objectives DROP COLUMN pending_since')
        self.assertNotIn('pending_since', {r['name'] for r in db.execute('PRAGMA table_info(objectives)')})
        obj.migrate(db)
        columns = {r['name'] for r in db.execute('PRAGMA table_info(objectives)')}
        self.assertLessEqual({'pending_request', 'pending_since'}, columns)
        objective = obj.create(self.store, self.payload, self.operator)
        obj.act(self.store, objective['id'], 'checkpoint', {'request_key': 'cp', 'expected_revision': 1, 'summary': 'Await', 'waiting_on': 'Customer'}, self.operator)
