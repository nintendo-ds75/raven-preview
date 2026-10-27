"""A default install pays nothing for objectives, meetings and harness probes.

Page polls never spawn a host binary; workers idle slowly until there is work;
an active objective is re-read only when its repository evidence changes.
"""
import json
import os
import threading
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import urlopen

from fixtures import OfflineCase, ready_server
from bridge import meeting_worker as mw, objectives as obj, objective_head as head, workbench_hosts as hosts
from bridge.auth import Identity
from bridge.config import Config
from bridge.store import Invalid

CODEX_ENV = {'RAVEN_HOST_CODEX_API_KEY': 'fixture', 'RAVEN_HOST_CODEX_MODEL': hosts.CODEX_MODEL,
             'RAVEN_CODEX_BIN': 'fixture-codex'}
UNCONFIGURED = {'RAVEN_HOST_' + p.upper() + suffix: '' for p in hosts.PROVIDERS
                for suffix in ('_API_KEY', '_MODEL', '_PROVIDER', '_BASE_URL')}


def codex_probe(*args, **kwargs):
    return SimpleNamespace(stdout='codex-cli ' + hosts.CODEX_VERSION, returncode=0)


class CapabilityCacheTests(OfflineCase):
    def setUp(self):
        super().setUp()
        hosts.reset_probes()
        self.addCleanup(hosts.reset_probes)

    def test_probe_runs_once_per_ttl_and_refresh_or_expiry_probes_again(self):
        with patch.dict(os.environ, {**CODEX_ENV, 'BRIDGE_HOST_PROBE_SECONDS': '60'}), \
                patch('bridge.workbench_hosts.subprocess.run', side_effect=codex_probe) as run, \
                patch('bridge.workbench_hosts.time.monotonic', return_value=1000.0) as clock:
            first = hosts.capability('codex')
            self.assertTrue(first['ready'])
            self.assertEqual(run.call_count, 1)
            first['blockers'].append('mutated by a caller')
            self.assertEqual(hosts.capability('codex')['blockers'], [])
            self.assertEqual(run.call_count, 1, 'a second poll inside the TTL reads the cached probe')
            self.assertTrue(hosts.capability('codex', refresh=True)['ready'])
            self.assertEqual(run.call_count, 2, 'dispatch asks for a fresh answer')
            clock.return_value = 1000.0 + 59
            hosts.capability('codex')
            self.assertEqual(run.call_count, 2)
            clock.return_value = 1000.0 + 61
            hosts.capability('codex')
            self.assertEqual(run.call_count, 3, 'the TTL expired')

    def test_zero_ttl_disables_the_cache_and_a_changed_configuration_is_a_miss(self):
        with patch.dict(os.environ, {**CODEX_ENV, 'BRIDGE_HOST_PROBE_SECONDS': '0'}), \
                patch('bridge.workbench_hosts.subprocess.run', side_effect=codex_probe) as run:
            hosts.capability('codex')
            hosts.capability('codex')
            self.assertEqual(run.call_count, 2)
        with patch.dict(os.environ, {**CODEX_ENV, 'BRIDGE_HOST_PROBE_SECONDS': '60'}), \
                patch('bridge.workbench_hosts.subprocess.run', side_effect=codex_probe) as run:
            hosts.capability('codex')
            with patch.dict(os.environ, {'RAVEN_HOST_CODEX_MODEL': 'other'}):
                self.assertFalse(hosts.capability('codex')['ready'])
            self.assertEqual(run.call_count, 2, 'a different configuration is probed on its own')

    def test_unconfigured_hosts_are_never_probed(self):
        with patch.dict(os.environ, {**UNCONFIGURED, 'BRIDGE_HOST_PROBE_SECONDS': '0'}), \
                patch('bridge.workbench_hosts.subprocess.run', side_effect=AssertionError('no binary probe without a credential')):
            for provider in hosts.PROVIDERS:
                result = hosts.capability(provider)
                self.assertFalse(result['ready'])
                self.assertIn('RAVEN_HOST_' + provider.upper() + '_API_KEY', ' '.join(result['blockers']))
                self.assertIn('probed once the credential and model are set', ' '.join(result['blockers']))
        with self.assertRaises(Invalid):
            hosts.capability('unknown')


class StatePollTests(OfflineCase):
    def setUp(self):
        super().setUp()
        hosts.reset_probes()
        self.addCleanup(hosts.reset_probes)
        self.store = self.fresh_store('poll.db')
        self.addCleanup(self.store.graph.close)

    def serve(self, **kwargs):
        server = ready_server(self.store, 0, **kwargs)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(close)
        return f'http://127.0.0.1:{server.server_port}'

    def state(self, url):
        with urlopen(url + '/api/state') as response:
            return json.load(response)

    def test_state_without_a_workbench_reports_hosts_unavailable_without_probing(self):
        url = self.serve()
        with patch.dict(os.environ, CODEX_ENV), \
                patch('bridge.workbench_hosts.subprocess.run', side_effect=AssertionError('a poll must not probe a host')):
            for _ in range(2):
                workbench = self.state(url)['workbench']
        self.assertFalse(workbench['enabled'])
        self.assertFalse(workbench['ready'])
        self.assertEqual(workbench['hosts'], [])
        self.assertIn('--workbench', workbench['reason'])

    def test_state_with_a_workbench_probes_once_per_ttl(self):
        from pathlib import Path
        from bridge.workbench import Workbench
        root = Path(self.temp.name)
        workbench = Workbench(self.store, [{'id': 'sample/project', 'path': str(root / 'missing')}], root / 'copies',
                              cfg=Config(model_api='none'))
        url = self.serve(workbench=workbench)
        with patch.dict(os.environ, {**CODEX_ENV, 'BRIDGE_HOST_PROBE_SECONDS': '60'}), \
                patch('bridge.workbench_hosts.subprocess.run', side_effect=codex_probe) as run:
            first = self.state(url)['workbench']
            self.assertTrue(first['enabled'])
            self.assertTrue(next(h for h in first['hosts'] if h['id'] == 'codex')['ready'])
            probes = run.call_count
            self.assertGreaterEqual(probes, 1)
            second = self.state(url)['workbench']
            self.assertEqual(run.call_count, probes, 'the second poll inside the TTL spawned nothing')
            self.assertEqual(second['hosts'], first['hosts'])
            self.assertEqual(workbench.config()['hosts'], first['hosts'])
            self.assertEqual(run.call_count, probes)


class ObjectiveCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store('workers.db')
        self.addCleanup(self.store.graph.close)
        self.actor = Identity('', 'Local operator', 'admin', 'operator')

    def objective(self):
        source = self.store.add_record({'repo': 'sample/project', 'kind': 'note', 'ref': 'pilot-call',
                                        'body': 'The CSV example does not work.', 'title': 'Pilot call'})['source']
        return obj.create(self.store, {'request_key': 'create-one', 'title': 'Pilot to value', 'outcome': 'Prepare the CSV path',
            'success_criteria': 'Customer completes a report', 'stop_criteria': 'Customer declines',
            'guardrails': 'No external sending', 'repository': 'sample/project', 'provider': 'claude',
            'source_ids': [source['record_id']]}, self.actor)


class WorkerIntervalTests(ObjectiveCase):
    def test_objective_worker_idles_until_there_is_work(self):
        with patch.dict(os.environ, {'BRIDGE_OBJECTIVE_TICK_SECONDS': ''}):
            worker = head.Worker(self.store, cfg=Config(model_api='none'))
            self.assertFalse(worker.needs_work())
            self.assertEqual(worker.interval(), head.IDLE_SECONDS)
            self.assertTrue(head.Worker(self.store, object(), cfg=Config(model_api='none')).needs_work())
            self.assertTrue(head.Worker(self.store, cfg=SimpleNamespace(semantic_retrieval=True)).needs_work())
            self.objective()
            self.assertTrue(worker.needs_work(), 'saved objectives are work even without a model or workbench')
            self.assertEqual(worker.interval(), 15.0)
        with patch.dict(os.environ, {'BRIDGE_OBJECTIVE_TICK_SECONDS': '7'}):
            self.assertEqual(worker.interval(), 7.0)
        with patch.dict(os.environ, {'BRIDGE_OBJECTIVE_TICK_SECONDS': 'soon'}):
            self.assertEqual(worker.interval(), 15.0)

    def test_objective_worker_thread_does_not_tick_while_idle(self):
        worker = head.Worker(self.store, cfg=Config(model_api='none'))
        ticks = []
        with patch.object(worker, 'tick', side_effect=lambda: ticks.append(1)), \
                patch.object(worker.stop, 'wait', side_effect=lambda seconds: ticks.append(seconds) or worker.stop.set()):
            worker.start()
            worker.thread.join(timeout=5)
        self.assertEqual(ticks, [head.IDLE_SECONDS])

    def test_meeting_worker_idles_without_observers_or_vexa(self):
        from bridge import meeting_records as mr
        mr.migrate(self.store.graph.db)
        mw.migrate(self.store.graph.db)
        worker = mw.Worker(self.store, client_factory=lambda: None)
        meeting = mr.create(self.store, {'repo': 'acme/app', 'provider': 'vexa', 'namespace': 'selfhost-one',
            'platform': 'google_meet', 'native_meeting_id': 'abc-defg-hij', 'title': 'Planning'})['id']
        with patch.dict(os.environ, {'BRIDGE_VEXA_URL': '', 'VEXA_API_KEY': '', 'BRIDGE_MEETING_TICK_SECONDS': ''}):
            self.assertFalse(worker.needs_work())
            self.assertEqual(worker.interval(), mw.IDLE_SECONDS)
            self.store.graph.db.execute("INSERT INTO meeting_observers(meeting_id,enabled,workspace_shared,actor_id,actor_name,actor_kind,updated_at) VALUES(?,1,1,'','Operator','operator','now')", (meeting,))
            self.assertTrue(worker.needs_work())
            self.assertEqual(worker.interval(), 15.0)
            self.store.graph.db.execute("UPDATE meeting_observers SET enabled=0")
            self.assertFalse(worker.needs_work())
        with patch.dict(os.environ, {'BRIDGE_VEXA_URL': 'https://vexa.example', 'VEXA_API_KEY': 'k', 'BRIDGE_MEETING_TICK_SECONDS': '5'}):
            self.assertTrue(worker.needs_work())
            self.assertEqual(worker.interval(), 5.0)


class EventDrivenScanTests(ObjectiveCase):
    def setUp(self):
        super().setUp()
        self.created = self.objective()
        self.worker = head.Worker(self.store, cfg=SimpleNamespace(semantic_retrieval=True), allow_operator=True)

    def scans(self, status='no_evidence', ticks=1):
        with patch.object(head, 'scan', return_value={'status': status, 'proposal': None, 'reason': 'fixture'}) as scan:
            for _ in range(ticks):
                self.worker.tick()
        return scan

    def test_unchanged_corpus_is_not_scanned_again(self):
        self.assertEqual(self.scans().call_count, 1, 'a never-evaluated objective is read once')
        self.assertEqual(self.scans(ticks=3).call_count, 0, 'nothing changed, so nothing is re-read')
        self.store.add_record({'repo': 'sample/project', 'kind': 'note', 'ref': 'follow-up',
                               'body': 'The customer sent the broken file.', 'title': 'Follow-up'})
        scan = self.scans(ticks=2)
        self.assertEqual(scan.call_count, 1, 'new evidence is read exactly once')
        self.assertEqual(scan.call_args.kwargs['objective_id'], self.created['id'])
        self.assertTrue(scan.call_args.kwargs['automatic'])

    def test_deferred_reading_keeps_the_objective_due(self):
        self.assertEqual(self.scans('throttled', ticks=2).call_count, 2)
        self.assertEqual(self.scans('pending').call_count, 1)
        self.assertEqual(self.scans('blocked').call_count, 1)
        self.assertEqual(self.scans().call_count, 0)

    def test_due_checkpoint_scans_without_new_evidence(self):
        self.scans()
        obj.act(self.store, self.created['id'], 'checkpoint', {'request_key': 'cp', 'expected_revision': self.created['revision'],
            'summary': 'Await report', 'next_check_at': '2020-01-01T00:00:00Z', 'waiting_on': 'Customer'}, self.actor)
        self.assertEqual(self.scans().call_count, 1)
        self.assertEqual(obj.get(self.store, self.created['id'])['next_check_at'], '')
        self.assertEqual(self.scans().call_count, 0)

    def test_corpus_revision_is_computed_once_per_repository_per_tick(self):
        head.configure_watch(self.store, {'repository': 'sample/project', 'enabled': True}, self.actor)
        from bridge import objective_research
        with patch('bridge.objective_research.corpus_revision', wraps=objective_research.corpus_revision) as corpus:
            self.scans()
        self.assertEqual(corpus.call_count, 1)
