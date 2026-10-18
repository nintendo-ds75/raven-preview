"""Revocation reaches copied answers, transcripts, exports and host contexts."""
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import Mock

from fixtures import OfflineCase
from bridge import canvas, mcp, source_access, visibility
from bridge.store import Store, Invalid


class VisibilityTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'access.db')
        self.addCleanup(self.store.graph.close)
        self.g = self.store.graph
        self.repo = 'fixture/access'
        self.task = self.store.add_run({'title': 'Review the access policy', 'repo': self.repo})['id']
        self.g.db.execute('INSERT INTO source_connections(id,kind,repo,target,options,created_at) VALUES(?,?,?,?,?,?)',
                          ('connection', 'jira', self.repo, 'BILL', '{"host":"fixture.invalid"}', source_access.stamp()))
        self.source = self.store.add_record({'repo': self.repo, 'provider': 'jira', 'namespace': 'fixture.invalid',
            'kind': 'jira', 'external_id': '10001', 'ref': 'BILL-1', 'title': 'Restricted canary',
            'body': 'ALLOWANCE_SECRET_CANARY is 631 seats.',
            'access_scope': '{"jira_project":"BILL","audience":"workspace-shared"}'})['source']
        source_access.observe(self.store, 'connection', self.source['record_id'])
        self.node = self.g.add_decision(self.task, 'What allowance applies?', 'policy', 'pending', repo=self.repo)
        row = dict(self.g.db.execute('SELECT i.*,s.id AS record_id,s.head_id AS source_version_id FROM intents i '
                                    'JOIN source_records s ON s.intent_id=i.id WHERE s.id=?', (self.source['record_id'],)).fetchone())
        self.g.publish_evidence(self.node, [row], status='resolved', source='record',
                                answer='ALLOWANCE_SECRET_CANARY is 631 seats.', kind='evidence', signoff='required')

    def revoke(self):
        source_access.observe(self.store, 'connection', self.source['record_id'], state='revoked')

    def test_trace_state_export_and_decision_hide_copied_content(self):
        self.revoke()
        for value in (canvas.trace(self.store, self.task), self.store.state(), self.store.state(full_history=True),
                      self.store.get_decision(self.node), canvas.get_tree(self.store, self.task)):
            serialized = json.dumps(visibility.public(self.store, value))
            self.assertNotIn('ALLOWANCE_SECRET_CANARY', serialized)
            self.assertIn('withheld', serialized)
        # The private audit history survives; this is not a destructive scrub.
        self.assertIn('ALLOWANCE_SECRET_CANARY', self.store.get_decision(self.node)['answer'])

    def test_contaminated_host_and_mcp_reads_are_refused_but_unrelated_task_works(self):
        self.revoke()
        with self.assertRaisesRegex(Invalid, 'withheld'):
            visibility.require_task(self.store, self.task)
        with self.assertRaisesRegex(Invalid, 'withheld'):
            mcp.call_tool(self.store, 'bridge_get_tree', {'task_id': self.task})
        other = self.store.add_run({'title': 'Unrelated work', 'repo': self.repo})['id']
        visibility.require_task(self.store, other)
        self.assertEqual(mcp.call_tool(self.store, 'bridge_get_tree', {'task_id': other})['task_id'], other)

    def test_cached_answer_read_contaminates_a_new_session_even_without_a_new_decision(self):
        other = self.store.add_run({'title': 'Read prior reasoning', 'repo': self.repo})['id']
        visibility.remember_reads(self.store, other, {'matches': [self.store.get_decision(self.node)]})
        self.revoke()
        with self.assertRaises(Invalid):
            visibility.require_task(self.store, other)

    def test_withheld_historical_version_blocks_copied_answers_even_when_issue_is_readable(self):
        self.g.db.execute('INSERT INTO withheld_source_versions VALUES(?,?,?)',
                          (self.source['source_version_id'], 'Comment restricted', source_access.stamp()))
        from bridge.context_connectors import blocked_decisions, fresh
        self.assertTrue(fresh(self.g.db, self.source['record_id']))
        self.assertIn(self.node, blocked_decisions(self.g.db, self.repo))
        with self.assertRaisesRegex(Invalid, 'withheld'):
            visibility.require_task(self.store, self.task)
        self.assertNotIn('ALLOWANCE_SECRET_CANARY', json.dumps(visibility.public(self.store, self.store.state())))

    def test_revalidated_access_restores_reads_without_rewriting_evidence(self):
        self.revoke()
        source_access.observe(self.store, 'connection', self.source['record_id'])
        visibility.require_task(self.store, self.task)
        self.assertIn('ALLOWANCE_SECRET_CANARY', json.dumps(visibility.public(self.store, self.store.get_decision(self.node))))

    def test_mcp_final_guard_preserves_json_without_decoding_opaque_receipts(self):
        receipt = json.dumps({'record_id': self.source['record_id'], 'body': 'ALLOWANCE_SECRET_CANARY'})
        value = {'source': {'record_id': self.source['record_id'], 'availability': 'inaccessible',
                            'title': 'ALLOWANCE_SECRET_CANARY', 'body': 'ALLOWANCE_SECRET_CANARY',
                            'rationale': 'ALLOWANCE_SECRET_CANARY'}, 'receipt': receipt}
        self.revoke()
        self.assertEqual(visibility.public(self.store, receipt), visibility.NOTICE)
        message = {'jsonrpc': '2.0', 'id': self.source['record_id'], 'result': {
            'content': [{'type': 'text', 'text': json.dumps(value)}], 'isError': False}}
        safe = visibility.mcp_response(self.store, message)
        self.assertEqual(safe['id'], message['id'])
        self.assertFalse(safe['result']['isError'])
        decoded = json.loads(safe['result']['content'][0]['text'])
        self.assertEqual(decoded['receipt'], visibility.NOTICE)
        self.assertTrue(decoded['source']['withheld'])
        self.assertEqual(decoded['source']['availability'], 'inaccessible')
        self.assertNotIn('ALLOWANCE_SECRET_CANARY', json.dumps(safe))
        self.assertEqual(visibility.mcp_response(self.store, safe), safe)

    def test_mcp_final_guard_refuses_malformed_success_payloads(self):
        self.revoke()
        for blocks in (None, [], [{'type': 'text', 'text': 'ALLOWANCE_SECRET_CANARY'}],
                       [{'type': 'text', 'text': {'body': 'ALLOWANCE_SECRET_CANARY'}}],
                       [{'type': 'unknown', 'body': 'ALLOWANCE_SECRET_CANARY'}]):
            with self.subTest(blocks=blocks):
                safe = visibility.mcp_response(self.store, {'jsonrpc': '2.0', 'id': 1,
                    'result': {'content': blocks, 'isError': False}})
                self.assertEqual(safe['jsonrpc'], '2.0')
                self.assertEqual(safe['id'], 1)
                self.assertTrue(safe['result']['isError'])
                self.assertEqual(safe['result']['content'], [{'type': 'text', 'text': visibility.NOTICE}])
                self.assertNotIn('ALLOWANCE_SECRET_CANARY', json.dumps(safe))

    def test_withheld_source_retains_only_bounded_availability_metadata(self):
        self.revoke()
        value = {'record_id': self.source['record_id'], 'availability': 'ALLOWANCE_SECRET_CANARY',
                 'matching_identities': [{'external_id': 'ALLOWANCE_SECRET_CANARY'}]}
        safe = visibility.public(self.store, value)
        self.assertNotIn('availability', safe)
        self.assertNotIn('matching_identities', safe)
        self.assertNotIn('ALLOWANCE_SECRET_CANARY', json.dumps(safe))

    def test_upgrade_binds_existing_native_sources_as_unverified(self):
        self.g.db.execute('DELETE FROM native_source_access')
        source_access.backfill(self.g.db)
        with self.assertRaises(Invalid):
            self.store.get_record(self.source['record_id'], self.repo)
        source_access.backfill(self.g.db)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM native_source_access').fetchone()[0], 1)

    def test_artifact_download_checks_access_before_and_after_provider_read(self):
        from fixtures import ready_server
        read = Mock(return_value=b'ALLOWANCE_SECRET_CANARY')
        service = SimpleNamespace(get=lambda _: {'snapshot': json.dumps({'artifacts': [{'id': 'patch'}]}),
                                                  'session_id': 'provider-session'},
                                  api=SimpleNamespace(artifact_content=read))
        server = ready_server(self.store, port=0, executions=service)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
        url = f'http://127.0.0.1:{server.server_port}/api/executions/{self.task}/artifacts/patch'
        with urlopen(url) as response:
            self.assertEqual(response.read(), b'ALLOWANCE_SECRET_CANARY')
        self.revoke()
        read.reset_mock()
        with self.assertRaises(HTTPError) as failure:
            urlopen(url)
        self.assertNotIn(b'ALLOWANCE_SECRET_CANARY', failure.exception.read())
        read.assert_not_called()
        source_access.observe(self.store, 'connection', self.source['record_id'])
        def revoke_during_read(*_):
            self.revoke()
            return b'ALLOWANCE_SECRET_CANARY'
        read.side_effect = revoke_during_read
        with self.assertRaises(HTTPError) as failure:
            urlopen(url)
        self.assertNotIn(b'ALLOWANCE_SECRET_CANARY', failure.exception.read())
