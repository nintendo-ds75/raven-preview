"""Persisted request bounds; all inference is a counted deterministic double."""
import json
import threading
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import objective_head as head, objectives as obj
from bridge.auth import Identity
from bridge.store import Store, Invalid


class Reader:
    def __init__(self):
        self.calls = 0

    def complete_json(self, *args, **kwargs):
        self.calls += 1
        return {'action': 'finish', 'disposition': 'no_material_move', 'observations': [], 'reason': 'No further move is needed.'}


class ObjectiveBudgetTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store('objective-budget')
        self.repo = 'sample/project'
        self.actor = Identity('', 'Local operator', 'admin', 'operator')
        self.client = Reader()
        self.record(0)

    def test_unbounded_cli_backend_never_runs_automatically(self):
        from bridge.config import Config
        result = head.scan(self.store, {'repository': self.repo, 'request_key': 'automatic-cli'},
                           self.actor, cfg=Config(model_api='claude-cli'), client=self.client, automatic=True)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.client.calls, 0)
        self.assertEqual(head.background_budget(self.store, self.repo)['used'], 0)
        manual = head.scan(self.store, {'repository': self.repo, 'request_key': 'manual-cli'},
                           self.actor, cfg=Config(model_api='claude-cli'), client=self.client)
        self.assertEqual(manual['status'], 'no_change')
        self.assertGreater(self.client.calls, 0)

    def record(self, revision):
        source = self.store.add_record({'repo': self.repo, 'ref': 'meeting', 'kind': 'note',
            'title': 'Meeting evidence', 'body': 'Distinct transcript update ' + str(revision)})['source']
        return {k: source[k] for k in ('record_id', 'source_version_id')}

    def decline(self, index, pin):
        """A proposal on the current evidence, dismissed with a reason."""
        payload = {'title': 'Move ' + str(index), 'outcome': 'Prepare it', 'success_criteria': 'Done', 'stop_criteria': 'Blocked',
                   'guardrails': 'Private only', 'repository': self.repo, 'provider': 'claude', 'why_now': 'Evidence says so',
                   'citations': [{**pin, 'quote': 'Distinct transcript update'}]}
        proposal = obj.propose(self.store, payload, [pin])
        return obj.proposal_action(self.store, proposal['id'], 'dismiss', {'request_key': 'dismiss-' + str(index),
            'expected_revision': proposal['revision'], 'rationale': 'Reason ' + str(index)}, self.actor)

    def test_declined_rationales_reach_the_next_investigation(self):
        class CapturingHead:
            def __init__(self):
                self.inputs = []

            def complete_json(self, purpose, system, prompt, **kwargs):
                if purpose == 'objective factual research':
                    return {'action': 'finish', 'disposition': 'needs_judgment', 'observations': [], 'reason': 'A judgment remains'}
                self.inputs.append((system, json.loads(prompt)))
                return {'proposal': None, 'reason': 'Nothing new to propose'}
        self.client = CapturingHead()
        first = self.decline(1, self.record(1))
        result = head.scan(self.store, {'repository': self.repo, 'request_key': 'after-one'}, self.actor, client=self.client)
        self.assertEqual(result['status'], 'no_change')
        system, prompt = self.client.inputs[-1]
        self.assertIn('Do not re-propose a declined move unless the cited evidence is newer than the declination.', system)
        self.assertEqual(prompt['declined_before'], [{'title': 'Move 1', 'rationale': 'Reason 1', 'updated_at': first['updated_at']}])
        for index in range(2, 7):
            self.decline(index, self.record(index))
        self.record(7)
        self.assertEqual(head.scan(self.store, {'repository': self.repo, 'request_key': 'after-six'}, self.actor, client=self.client)['status'], 'no_change')
        declined = self.client.inputs[-1][1]['declined_before']
        self.assertEqual([d['title'] for d in declined], ['Move 6', 'Move 5', 'Move 4', 'Move 3', 'Move 2'])
        self.assertEqual([d['rationale'] for d in declined], ['Reason 6', 'Reason 5', 'Reason 4', 'Reason 3', 'Reason 2'])
        self.assertNotIn('Move 1', json.dumps(self.client.inputs[-1][1]))
        self.assertEqual(len(self.client.inputs), 2)

    def scan(self, **kwargs):
        return head.scan(self.store, {'repository': self.repo, 'request_key': 'scan'}, self.actor,
                         client=self.client, automatic=True, **kwargs)

    def test_new_evidence_is_debounced_then_rolling_budget_survives_restart(self):
        with patch('bridge.objective_head.time.time', return_value=10000):
            self.assertEqual(self.scan()['status'], 'no_change')
            self.record(1)
            self.assertEqual(self.scan()['status'], 'throttled')
            self.assertEqual(self.client.calls, 1)
        for index in range(1, 6):
            self.record(index + 1)
            with patch('bridge.objective_head.time.time', return_value=10000 + index * 60):
                self.assertEqual(self.scan()['status'], 'no_change')
        self.record(7)
        reopened = Store(self.store.path)
        self.addCleanup(reopened.graph.close)
        with patch('bridge.objective_head.time.time', return_value=10400):
            result = head.scan(reopened, {'repository': self.repo, 'request_key': 'restart'}, self.actor,
                               client=self.client, automatic=True)
            self.assertEqual(result['status'], 'throttled')
            self.assertEqual(result['background_budget']['remaining'], 0)
            self.assertTrue(result['next_retry_at'])
            self.assertEqual(self.client.calls, 6)
        with patch('bridge.objective_head.time.time', return_value=13600):
            self.assertEqual(self.scan()['status'], 'no_change')
            self.assertEqual(self.client.calls, 7)

    def test_cached_and_pending_calls_do_not_spend_budget(self):
        with patch('bridge.objective_head.time.time', return_value=10000):
            self.scan()
            self.scan()
            self.assertEqual(head.background_budget(self.store, self.repo)['used'], 1)
            self.store.graph.db.execute("UPDATE objective_evaluations SET state='running',lease_until=10600")
            self.assertEqual(self.scan()['status'], 'pending')
            self.assertEqual(head.background_budget(self.store, self.repo)['used'], 1)
            self.assertEqual(self.client.calls, 1)

    def test_explicit_manual_scan_is_separate_not_an_automatic_budget_reset(self):
        with patch('bridge.objective_head.time.time', return_value=10000):
            self.scan()
            self.record(2)
            self.assertEqual(self.scan()['status'], 'throttled')
            result = head.scan(self.store, {'repository': self.repo, 'request_key': 'manual'}, self.actor, client=self.client)
            self.assertEqual(result['status'], 'no_change')
            self.assertEqual(head.background_budget(self.store, self.repo)['used'], 1)
            self.assertEqual(self.client.calls, 2)

    def test_manual_scans_spend_a_bounded_budget_unless_an_admin_overrides(self):
        def manual(key, actor=None, **data):
            return head.scan(self.store, {'repository': self.repo, 'request_key': key, **data}, actor or self.actor, client=self.client)
        with patch('bridge.objective_head.time.time', return_value=10000):
            for index in range(1, 7):
                self.record(index)
                self.assertEqual(manual('manual-' + str(index))['status'], 'no_change')
            self.assertEqual(self.client.calls, 6)
            self.record(7)
            throttled = manual('manual-7')
            self.assertEqual(throttled['status'], 'throttled')
            self.assertTrue(throttled['next_retry_at'])
            self.assertEqual(throttled['background_budget']['kind'], 'manual')
            self.assertEqual(self.client.calls, 6)
            self.assertEqual(head.background_budget(self.store, self.repo)['used'], 0)
            member = Identity(self.store.graph.add_person('Member', role='member'), 'Member', 'member', 'session')
            with self.assertRaisesRegex(Invalid, 'administrator'):
                manual('member-override', member, override=True)
            with self.assertRaises(Invalid):
                manual('bad-override', override='yes')
            self.assertEqual(self.client.calls, 6)
            self.assertEqual(manual('admin-override', override=True)['status'], 'no_change')
            self.assertEqual(self.client.calls, 7)
        with patch('bridge.objective_head.time.time', return_value=13601):
            self.record(8)
            self.assertEqual(manual('manual-8')['status'], 'no_change')

    def test_simultaneous_writer_claims_only_one_debounced_attempt(self):
        barrier = threading.Barrier(2)
        outcomes, errors = [], []
        def claim():
            try:
                barrier.wait(timeout=5)
                with self.store.graph.transaction():
                    outcomes.append(head._claim_background(self.store, self.repo))
            except Exception as error:
                errors.append(error)
            finally:
                self.store.graph.close_thread()
        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sum(outcome is None for outcome in outcomes), 1)
        self.assertEqual(head.background_budget(self.store, self.repo)['used'], 1)

    def test_watch_toggle_reopens_evidence_but_never_resets_budget(self):
        head.configure_watch(self.store, {'repository': self.repo, 'enabled': True}, self.actor)
        self.store.graph.db.execute("UPDATE objective_watches SET last_corpus='old-result'")
        with self.store.graph.transaction():
            head._claim_background(self.store, self.repo)
        head.configure_watch(self.store, {'repository': self.repo, 'enabled': False}, self.actor)
        head.configure_watch(self.store, {'repository': self.repo, 'enabled': True}, self.actor)
        row = self.store.graph.db.execute('SELECT last_corpus FROM objective_watches').fetchone()
        self.assertEqual(row['last_corpus'], '')
        self.assertEqual(head.background_budget(self.store, self.repo)['used'], 1)

    def test_failed_retry_consumes_same_repository_budget(self):
        class InvalidReader:
            def complete_json(self, *args, **kwargs):
                return {'action': 'not-a-tool'}
        self.client = InvalidReader()
        for stamp in (10000, 10100):
            with patch('bridge.objective_head.time.time', return_value=stamp):
                with self.assertRaises(Invalid):
                    self.scan()
        self.assertEqual(head.background_budget(self.store, self.repo, at=10100)['used'], 2)

    def test_throttled_watch_keeps_dirty_corpus_for_later_observation(self):
        from types import SimpleNamespace
        head.configure_watch(self.store, {'repository': self.repo, 'enabled': True}, self.actor)
        self.store.graph.db.execute("UPDATE objective_watches SET last_corpus='older'")
        worker = head.Worker(self.store, cfg=SimpleNamespace(semantic_retrieval=True), allow_operator=True)
        with patch.object(head, 'scan', return_value={'status': 'throttled'}) as scan:
            worker.investigate_watches()
        self.assertTrue(scan.call_args.kwargs['automatic'])
        self.assertEqual(self.store.graph.db.execute('SELECT last_corpus FROM objective_watches').fetchone()['last_corpus'], 'older')
