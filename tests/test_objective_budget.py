"""Persisted request bounds; all inference is a counted deterministic double."""
import threading
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import objective_head as head
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
        self.store.add_record({'repo': self.repo, 'ref': 'meeting', 'kind': 'note',
            'title': 'Meeting evidence', 'body': 'Distinct transcript update ' + str(revision)})

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
