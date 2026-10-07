"""Contract fixtures follow Airweave's documented SearchV2Response schema."""
import copy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import canvas, context_connectors as cc, context_memory as cm
from bridge.config import Config
from bridge.store import Store, Invalid

REPO = 'example/service'


def result(body='Exclude synthetic traffic before aggregation.'):
    return {'entity_id': 'chunk-policy-0', 'name': 'Metering policy', 'relevance_score': 0.91,
        'breadcrumbs': [], 'created_at': '2025-01-01T00:00:00Z', 'updated_at': '2025-02-01T00:00:00Z',
        'textual_representation': body, 'airweave_system_metadata': {'source_name': 'notion',
            'entity_type': 'NotionPageEntity', 'sync_id': 'sync-example', 'sync_job_id': 'job-example',
            'chunk_index': 0, 'original_entity_id': 'policy-123'},
        'access': {'is_public': False, 'viewers': ['member-a', 'member-b']},
        'web_url': 'https://example.invalid/policy', 'raw_source_fields': {'status': 'Approved'}}


class Source:
    def __init__(self):
        self.results = [result()]
        self.calls = []

    def search(self, collection, query, **kwargs):
        self.calls.append((collection, query, kwargs))
        return copy.deepcopy(self.results)


class ConnectorCases:
    def connect(self):
        return cc.configure(self.store, REPO, 'workspace-docs', ['member-a', 'member-b'], shared=True)

    def search(self, task=None, query='Metering policy'):
        return cc.search(self.store, {'repo': REPO, 'query': query, 'task_id': task or self.task}, client=self.client)

    def signed(self):
        source = self.search()['sources'][0]
        did = self.g.add_decision(self.task, 'Which usage is billable?', 'policy', 'pending',
                                 repo=REPO, owner='Ada Example', path='billing/main.py')
        self.g.publish_evidence(did, [source], status='resolved', source='record',
                                answer='Exclude synthetic traffic.', kind='evidence', signoff='required')
        canvas.sign_off(self.store, did, {'by': 'Ada Example', 'expected_updated_at': self.store.get_decision(did)['updated_at']})
        return did, source

    def test_sources_and_people_answers_join_even_without_lexical_overlap(self):
        did, source = self.signed()
        sibling = self.store.add_run({'title': 'Invoice generation', 'repo': REPO})['id']
        found = self.search(sibling, 'Can customer charges include a staging canary?')
        self.assertEqual(found['related_decisions'][0]['decision_id'], did)
        self.assertEqual(found['sources'][0]['record_id'], source['record_id'])
        with self.g.source_scope(sibling):
            self.assertEqual(self.g.anchored_answers(REPO)[0].id, did)
        self.assertTrue(cm.anchors(self.g.db, sibling))
        self.assertEqual(self.g.count_events('context_searched', sibling), 1)
        # Same observed source does not accumulate duplicate revisions.
        self.assertEqual(len(self.store.get_record(source['record_id'], REPO)['versions']), 1)

    def test_changed_external_source_invalidates_signed_memory_transitively(self):
        did, source = self.signed()
        old_history = self.store.get_decision(did)['context_history']
        self.client.results[0]['textual_representation'] = 'Include only paid production traffic.'
        self.client.results[0]['updated_at'] = '2025-03-01T00:00:00Z'
        cc.refresh(self.store, REPO, client=self.client, force=True)
        current = self.store.get_decision(did)
        self.assertTrue(current['needs_review'])
        self.assertFalse(current['authorized'])
        self.assertTrue(current['sources'][0]['stale'])
        self.assertEqual(self.search()['related_decisions'], [])
        self.assertTrue(all(v in current['context_history'] for v in old_history))
        self.assertEqual(len(self.store.get_record(source['record_id'], REPO)['versions']), 2)

    def test_permission_revocation_blocks_reuse_without_changing_source_text(self):
        did, _ = self.signed()
        self.client.results[0]['access']['viewers'] = ['member-a']
        outcome = cc.refresh(self.store, REPO, client=self.client, force=True)
        self.assertEqual(outcome['unavailable'], 1)
        self.assertFalse(self.store.get_decision(did)['authorized'])
        self.assertEqual(self.search()['related_decisions'], [])
        self.assertEqual(self.search()['sources'], [])
        with self.assertRaises(Invalid): self.store.get_record(self.g.db.execute('SELECT record_id FROM context_entities').fetchone()['record_id'], REPO)
        with self.assertRaises(Invalid):
            canvas.sign_off(self.store, did, {'by': 'Ada Example'})

    def test_expiry_fails_closed_even_when_worker_is_not_running(self):
        did, source = self.signed()
        with self.g.transaction():
            self.g.db.execute("UPDATE context_entities SET verified_until='2000-01-01T00:00:00+00:00'")
        self.assertFalse(cm.citation(self.g.db, source['record_id'], source['source_version_id'])['current'])
        with self.assertRaises(Invalid):
            cm.check_current(self.g.db, did)
        with self.g.source_scope(self.task):
            self.assertEqual(self.g.linked_answers([source['record_id']], REPO), [])
        self.assertTrue(self.g.blocking_nodes(self.task))

    def test_missing_filtered_result_is_unavailable_not_proven_deleted(self):
        did, source = self.signed()
        self.client.results = []
        cc.refresh(self.store, REPO, client=self.client, force=True)
        self.assertFalse(self.store.get_decision(did)['authorized'])
        self.assertEqual(self.g.db.execute('SELECT count(*) n FROM source_versions WHERE record_id=?', (source['record_id'],)).fetchone()['n'], 1)
        self.assertEqual(self.g.db.execute('SELECT state FROM context_entities').fetchone()['state'], 'refresh_failed')

    def test_temporary_outage_restores_identical_evidence_without_demanding_new_signature(self):
        did, _ = self.signed()
        self.client.results = []
        cc.refresh(self.store, REPO, client=self.client, force=True)
        tree = canvas.get_tree(self.store, self.task)
        self.assertTrue(tree['nodes'][0]['source_refresh_required'])
        self.assertFalse(tree['nodes'][0]['authorized'])
        self.client.results = [result()]
        cc.refresh(self.store, REPO, client=self.client, force=True)
        self.assertTrue(self.store.get_decision(did)['authorized'])
        self.assertEqual(self.g.count_events('context_evidence_restored', self.task), 1)

    def test_revocation_after_outage_still_requires_reapproval_when_access_returns(self):
        did, _ = self.signed()
        self.client.results = []
        cc.refresh(self.store, REPO, client=self.client, force=True)
        self.client.results = [result()]
        self.client.results[0]['access']['viewers'] = ['member-a']
        self.search()
        self.client.results = [result()]
        cc.refresh(self.store, REPO, client=self.client, force=True)
        self.assertFalse(self.store.get_decision(did)['authorized'])
        self.assertTrue(self.store.get_decision(did)['needs_review'])

    def test_unknown_access_and_transient_search_are_not_saved(self):
        for change in ('unknown', 'slack', 'federated'):
            item = result()
            if change == 'unknown': item['access'] = {'is_public': None, 'viewers': None}
            elif change == 'slack': item['airweave_system_metadata']['source_name'] = 'slack'
            else: item['airweave_system_metadata']['sync_id'] = None
            self.client.results = [item]
            observed = self.search()
            self.assertEqual(observed['external']['rejected'], 1)
            self.assertEqual(observed['sources'], [])
        self.assertEqual(self.g.db.execute('SELECT count(*) n FROM source_records').fetchone()['n'], 0)

    def test_connection_grant_change_retires_old_cached_answers(self):
        did, _ = self.signed()
        cc.configure(self.store, REPO, 'workspace-docs', ['member-a', 'member-b', 'member-c'], shared=True)
        self.assertFalse(self.store.get_decision(did)['authorized'])
        self.assertEqual(self.search()['sources'], [])

    def test_inflight_response_cannot_cross_connection_generation(self):
        connection = cc._connection(self.store, REPO)
        cc.configure(self.store, REPO, 'other-docs', ['member-a'], shared=True)
        with self.assertRaises(Invalid): cc._accept(self.store, connection, result(), 'old query')
        self.assertEqual(self.g.db.execute('SELECT count(*) n FROM intents').fetchone()['n'], 0)

    def test_evidence_is_excerpt_and_does_not_infer_authority_or_adoption(self):
        row = self.search()['sources'][0]
        self.assertFalse(row['resolved'])
        self.assertEqual(row['author'], '')
        self.assertTrue(row['body'].startswith('Retrieved excerpt (not the complete source).'))
        self.assertEqual(self.g.db.execute('SELECT count(*) n FROM authority').fetchone()['n'], 1)

    def test_explicit_collection_access_denial_withdraws_cached_authorization_immediately(self):
        did, _ = self.signed()
        with patch.object(self.client, 'search', side_effect=cc.AccessRejected('HTTP 403')):
            self.assertTrue(self.search()['external']['error'])
        self.assertFalse(self.store.get_decision(did)['authorized'])
        cc.refresh(self.store, REPO, client=self.client, force=True)
        self.assertTrue(self.store.get_decision(did)['needs_review'])

    def test_cancelled_jira_resolution_survives_generic_excerpt_import(self):
        from bridge.ladder import _void
        self.client.results[0]['raw_source_fields'] = {'fields': {'status': {'name': 'Closed'}, 'resolution': {'name': "Won't Do"}}}
        self.search()
        row = dict(self.g.db.execute('SELECT * FROM intents').fetchone())
        self.assertIn("Resolution: Won't Do", row['body'])
        self.assertTrue(_void(row))

    def test_automatic_kickoff_and_decision_retrieval_use_same_graph(self):
        with patch.object(cc, 'Airweave', return_value=self.client):
            task = canvas.start_task(self.store, Config().without_models(), {'title': 'Change invoices',
                'goal': 'Can we charge for staging canaries?', 'repo': REPO, 'client_key': 'one'})
            self.assertEqual(task['discovery']['context_retrieval']['imported'], 1)
            node = canvas.add_node(self.store, Config().without_models(), {'task_id': task['task_id'],
                'question': 'Should synthetic traffic count?', 'paths': 'billing/main.py'})
            self.assertTrue(node['node_id'])
        self.assertEqual(len(self.client.calls), 2)


class ConnectorTests(ConnectorCases, OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'connectors.db')
        self.g = self.store.graph
        self.addCleanup(self.g.close)
        self.store.add_owner({'name': 'Ada Example', 'team': 'Billing', 'patterns': 'billing/*'})
        self.task = self.store.add_run({'title': 'Billing', 'repo': REPO})['id']
        self.client = Source()
        self.connect()


class AirweaveHTTPTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.requests, self.response = [], {'results': [result()]}
        self.code, self.redirected = 200, False
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.requests.append((self.path, self.headers.get('x-api-key'),
                    json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(owner.code)
                if owner.code == 302:
                    self.send_header('Location', '/redirect-target')
                self.end_headers()
                self.wfile.write(json.dumps(owner.response).encode())
            def do_GET(self):
                owner.redirected = True
                self.send_response(200); self.end_headers()
            def log_message(self, *args): pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = cc.Airweave('http://127.0.0.1:' + str(self.server.server_port), 'synthetic-contract-key')

    def test_documented_wire_contract_and_exact_refresh_filter(self):
        filters = [{'field': 'airweave_system_metadata.chunk_index', 'operator': 'equals', 'value': 0}]
        self.assertEqual(self.client.search('workspace-docs', 'billing policy', filters=filters, limit=1), [result()])
        path, key, body = self.requests[0]
        self.assertEqual(path, '/collections/workspace-docs/search/instant')
        self.assertEqual(key, 'synthetic-contract-key')
        self.assertEqual(body, {'query': 'billing policy', 'retrieval_strategy': 'hybrid',
            'filter': [{'conditions': filters}], 'limit': 1, 'offset': 0})

    def test_redirect_does_not_forward_credentials(self):
        self.code = 302
        with self.assertRaises(Invalid): self.client.search('workspace-docs', 'billing')
        self.assertFalse(self.redirected)
        self.assertEqual(len(self.requests), 1)

    def test_failed_contract_and_remote_error_are_not_returned_as_sources(self):
        self.response = {'error': 'synthetic private response'}
        with self.assertRaises(Invalid) as failed: self.client.search('workspace-docs', 'billing')
        self.assertNotIn('synthetic private', str(failed.exception))
        self.code = 403
        with self.assertRaises(Invalid) as failed: self.client.search('workspace-docs', 'billing')
        self.assertNotIn('synthetic private', str(failed.exception))
