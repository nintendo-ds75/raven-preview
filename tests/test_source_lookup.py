"""Synthetic fresh-host handover through authenticated HTTP MCP and REST.

No imported record handle is required by the new host. No provider or model
calls, real messages, customer data, external database, or browser are used.
"""
import json
from urllib.parse import urlencode
from unittest.mock import patch

from bridge import canvas, context_memory as cm, github, proof
from bridge.store import Store
from test_auth import BOOTSTRAP, SharedServer

REPO = 'synthetic/handover'


class SourceLookupTests(SharedServer):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.store.graph.close)
        viewer = self.store.add_person({'name': 'Fresh Host Viewer', 'role': 'viewer'})
        self.agent = self.auth.create_token(viewer['id'], label='synthetic-fresh-host')['token']
        self.owner = self.store.add_owner({'name': 'Handover Owner', 'team': 'Synthetic', 'patterns': 'policy/*'})
        self.task = self.store.add_run({'title': 'Synthetic work item', 'repo': REPO})['id']
        for target in ('bridge.github.GitHubAPI.get', 'bridge.mcp.index_repo'):
            blocker = patch(target, side_effect=AssertionError('Lookup must not fetch providers or ingest'))
            blocker.start()
            self.addCleanup(blocker.stop)

    def source(self, **changes):
        return self.store.add_record({'repo': REPO, 'provider': 'jira', 'namespace': 'site-a',
            'kind': 'jira', 'external_id': 'opaque-123', 'ref': 'POL-7', 'title': 'Synthetic policy',
            'body': 'Decided: keep synthetic rows for thirty days.', 'status': 'Done', **changes})['source']

    def node(self, source=None, task=None, role='support', repo=REPO):
        graph = self.store.graph
        node = graph.add_decision(task or self.task, 'How long should synthetic rows remain?', 'policy', 'pending',
                                  repo=repo, owner='Handover Owner', path='policy/retention.py')
        if source:
            row = dict(graph.db.execute('SELECT i.*,s.id AS record_id,s.head_id AS source_version_id '
                                        'FROM intents i JOIN source_records s ON s.intent_id=i.id WHERE s.id=?',
                                        (source['record_id'],)).fetchone())
            graph.publish_evidence(node, [row] if role == 'support' else [], status='resolved', source='record',
                                   answer='Keep thirty days.', kind='evidence', signoff='required')
            if role != 'support':
                with graph.transaction():
                    cm.attach(graph.db, node, [cm.pin(row, role)])
        return node

    def lookup(self, **args):
        args = {'repo': REPO, **args}
        before = self.frozen_state()
        mcp = self.mcp('bridge_lookup_record', args, self.agent)
        self.assertFalse(mcp['isError'], mcp)
        rest = self.get('/api/records/lookup?' + urlencode(args), token=self.agent)
        self.assertEqual(mcp['result'], rest)
        self.assertEqual(before, self.frozen_state())
        return rest

    def frozen_state(self):
        # Transport authentication may update its own last-used timestamp. The
        # knowledge, authority, proofs, read receipts and source graph must not.
        tables = ('runs', 'decisions', 'decision_versions', 'decision_source_edges', 'source_records',
                  'source_versions', 'source_identities', 'task_source_anchors', 'events', 'authority',
                  'decision_links', 'source_review_requests', 'settings')
        return {table: sorted(json.dumps(dict(row), sort_keys=True, default=str)
                              for row in self.store.graph.db.execute('SELECT * FROM ' + table)) for table in tables}

    def test_fresh_host_uses_external_key_to_find_task_and_decisions(self):
        source = self.source(task_id=self.task)
        premise = self.node(source)
        associated = self.node()
        before = self.frozen_state()
        found = self.lookup(provider='jira', namespace='site-a', object_kind='jira', external_id='opaque-123')
        self.assertEqual(found['status'], 'matched')
        self.assertEqual(found['source']['record_id'], source['record_id'])
        self.assertEqual(found['latest_observed']['source_version_id'], source['source_version_id'])
        self.assertEqual(found['task_anchors']['items'][0]['task_id'], self.task)
        self.assertEqual(found['task_anchors']['items'][0]['relation'], 'task_anchor')
        self.assertEqual(found['decision_sources']['items'][0]['decision_id'], premise)
        self.assertEqual(found['decision_sources']['items'][0]['relation'], 'decision_premise')
        self.assertEqual({r['decision_id'] for r in found['task_decisions']['items']}, {premise, associated})
        self.assertTrue(all(r['relation'] == 'task_anchor_association' for r in found['task_decisions']['items']))
        self.assertEqual(before, self.frozen_state())
        self.assertFalse(self.store.get_decision(premise)['authorized'])
        # The new host can now follow supported decision and exact-record APIs.
        decision = self.mcp('bridge_get_decision', {'decision_id': premise}, self.agent)['result']
        self.assertEqual(decision['answer'], 'Keep thirty days.')
        exact = self.mcp('bridge_get_record', {'repo': REPO, 'record_id': found['source']['record_id']}, self.agent)
        self.assertFalse(exact['isError'])
        self.assertEqual(exact['result'], self.get('/api/records?' + urlencode({'repo': REPO, 'record_id': source['record_id']}), token=self.agent))

    def test_duplicate_jira_display_refs_require_installation_and_kind(self):
        a = self.source()
        b = self.source(namespace='site-b', body='Different installation, newer import.')
        ticket = self.source(kind='ticket', body='Different kind, latest import.')
        ambiguous = self.lookup(provider='jira', ref='POL-7')
        self.assertEqual(ambiguous['status'], 'ambiguous')
        self.assertNotIn('source', ambiguous)
        self.assertNotIn('latest_observed', ambiguous)
        self.assertEqual({r['record_id'] for r in ambiguous['candidates']['items']},
                         {a['record_id'], b['record_id'], ticket['record_id']})
        self.assertEqual(self.lookup(provider='jira', namespace='site-a', ref='POL-7')['status'], 'ambiguous')
        exact = self.lookup(provider='jira', namespace='site-a', object_kind='ticket', ref='POL-7')
        self.assertEqual(exact['source']['record_id'], ticket['record_id'])
        # Even an opaque external ID can repeat across installations and kinds.
        self.assertEqual(self.lookup(external_id='opaque-123')['status'], 'ambiguous')
        self.assertEqual(self.lookup(provider='jira', namespace='site-b', object_kind='jira', external_id='opaque-123')['source']['record_id'], b['record_id'])

    def test_cross_provider_duplicates_remain_ambiguous(self):
        self.source()
        other = self.source(provider='generic')
        self.assertEqual(self.lookup(namespace='site-a', object_kind='jira', external_id='opaque-123')['status'], 'ambiguous')
        self.assertEqual(self.lookup(provider='generic', namespace='site-a', object_kind='jira', external_id='opaque-123')['source']['record_id'], other['record_id'])

    def test_native_github_pr_and_issue_are_separate_exact_identities(self):
        issue = self.source(provider='github', namespace='github.com', kind='issue', external_id=REPO + '/issues/42', ref='42')
        # Existing native ingestion, with a synthetic payload, not a provider call.
        github.apply_pull(self.store.graph, REPO, {'repo': REPO, 'number': 42, 'title': 'Synthetic PR',
            'body': 'Synthetic native PR body', 'author': 'synthetic-author', 'merged_by': '', 'merged_at': '',
            'updated_at': '2026-01-01T00:00:00Z', 'merge_sha': '', 'files': [], 'reviews': []})
        ambiguous = self.lookup(provider='github', namespace='github.com', ref='42')
        self.assertEqual(ambiguous['status'], 'ambiguous')
        pr = self.lookup(provider='github', namespace='github.com', object_kind='pr', external_id=REPO + '/pull/42')
        self.assertEqual(pr['status'], 'matched')
        self.assertEqual(pr['latest_observed']['object_kind'], 'pr')
        self.assertNotEqual(pr['source']['record_id'], issue['record_id'])
        self.assertEqual(self.lookup(provider='github', namespace='github.com', object_kind='issue', ref='42')['source']['record_id'], issue['record_id'])
        self.assertEqual(self.lookup(provider='github', external_id='https://github.com/' + REPO + '/pull/42')['status'], 'unknown')

    def test_explicit_native_identity_alias_keeps_one_record(self):
        graph = self.store.graph
        graph.upsert_intent(REPO, 'pr', '42', 'Local synthetic PR', 'Local text', '', '',
                            metadata={'provider': 'git', 'namespace': REPO, 'object_kind': 'pr', 'external_id': '42', 'display_ref': '42'})
        before = self.lookup(provider='git', namespace=REPO, object_kind='pr', external_id='42')['source']['record_id']
        github.apply_pull(graph, REPO, {'repo': REPO, 'number': 42, 'title': 'Synthetic GitHub PR',
            'body': 'Native metadata', 'author': 'synthetic-author', 'merged_by': '', 'merged_at': '',
            'updated_at': '', 'merge_sha': '', 'files': [], 'reviews': []})
        external = self.lookup(provider='github', namespace='github.com', object_kind='pr', external_id=REPO + '/pull/42')
        self.assertEqual(external['source']['record_id'], before)
        self.assertEqual(self.lookup(ref='42')['status'], 'matched')
        self.assertEqual(len(self.lookup(ref='42')['source']['matching_identities']['items']), 2)
        bounded = self.lookup(ref='42', limit=1)
        self.assertEqual(bounded['status'], 'matched')
        self.assertTrue(bounded['source']['matching_identities']['truncated'])
        self.assertEqual(self.lookup(provider='git', namespace=REPO, external_id='42')['source']['record_id'], before)

    def test_slack_workspace_channel_message_identity_is_exact(self):
        a = self.source(provider='slack', namespace='TSYNTH_A', kind='slack', external_id='CSYNTH_A:123.456', ref='thread-7')
        b = self.source(provider='slack', namespace='TSYNTH_B', kind='slack', external_id='CSYNTH_A:123.456', ref='thread-7')
        c = self.source(provider='slack', namespace='TSYNTH_A', kind='slack', external_id='CSYNTH_B:123.456', ref='thread-7')
        self.assertEqual(self.lookup(provider='slack', ref='thread-7')['status'], 'ambiguous')
        self.assertEqual(self.lookup(provider='slack', external_id='CSYNTH_A:123.456')['status'], 'ambiguous')
        exact = self.lookup(provider='slack', namespace='TSYNTH_A', object_kind='slack', external_id='CSYNTH_A:123.456')
        self.assertEqual(exact['source']['record_id'], a['record_id'])
        self.assertNotIn(exact['source']['record_id'], (b['record_id'], c['record_id']))
        self.assertEqual(self.lookup(provider='slack', namespace='TSYNTH_A', external_id='123.456')['status'], 'unknown')

    def test_stable_external_id_survives_display_ref_rename_without_alias_guessing(self):
        source = self.source(task_id=self.task)
        updated = self.source(ref='RENAMED-99', body='Renamed display key, same external identity.')
        self.assertEqual(source['record_id'], updated['record_id'])
        exact = self.lookup(provider='jira', namespace='site-a', object_kind='jira', external_id='opaque-123')
        self.assertEqual(exact['source']['ref'], 'RENAMED-99')
        self.assertEqual({v['ref'] for v in exact['versions']['items']}, {'POL-7', 'RENAMED-99'})
        self.assertEqual(exact['task_anchors']['items'][0]['pinned_source_version_id'], source['source_version_id'])
        self.assertEqual(self.lookup(ref='POL-7')['status'], 'unknown')
        self.assertEqual(self.lookup(ref='RENAMED-99')['source']['record_id'], source['record_id'])

    def test_old_pins_latest_observation_and_lifecycle_are_not_adopted_policy(self):
        source = self.source(task_id=self.task)
        node = self.node(source)
        canvas.sign_off(self.store, node, {'by': 'Handover Owner', 'expected_updated_at': self.store.get_decision(node)['updated_at']})
        updated = self.source(body='The source reopened. Nothing approves new behavior.', status='In Progress', resolved=False,
                              source_sequence='2', updated_at='2026-01-02T00:00:00Z')
        before = self.frozen_state()
        found = self.lookup(provider='jira', namespace='site-a', object_kind='jira', external_id='opaque-123')
        self.assertEqual(found['latest_observed']['status'], 'In Progress')
        self.assertFalse(found['latest_observed']['resolved'])
        self.assertEqual(found['latest_observed']['source_sequence'], 2)
        self.assertEqual(found['latest_observed']['source_updated_at'], '2026-01-02T00:00:00Z')
        self.assertIn('not currently adopted policy', found['notice'])
        live = next(edge for edge in found['decision_sources']['items'] if edge['edge_active'])
        self.assertTrue(live['decision_needs_review'])
        self.assertEqual(live['pinned_source_version_id'], source['source_version_id'])
        self.assertEqual(live['latest_observed_source_version_id'], updated['source_version_id'])
        self.assertFalse(live['pin_matches_latest_observed'])
        self.assertEqual(found['task_anchors']['items'][0]['pinned_source_version_id'], source['source_version_id'])
        self.assertEqual(before, self.frozen_state())
        self.assertFalse(self.store.get_decision(node)['authorized'])

    def test_all_source_roles_remain_explicit_without_approval(self):
        source = self.source()
        expected = {}
        for role in ('support', 'contradiction', 'context', 'work_item'):
            expected[self.node(source, role=role)] = role
        found = self.lookup(provider='jira', namespace='site-a', external_id='opaque-123')
        for edge in found['decision_sources']['items']:
            if not edge['edge_active']:
                continue
            self.assertEqual(edge['role'], expected[edge['decision_id']])
            self.assertEqual(edge['relation'], 'decision_premise' if edge['role'] in ('support', 'contradiction') else 'decision_association')
            self.assertFalse(self.store.get_decision(edge['decision_id'])['authorized'])

    def test_context_associations_are_not_premises_and_retired_edges_are_history(self):
        source = self.source(task_id=self.task, anchor_role='context')
        node = self.node(source, role='context')
        found = self.lookup(external_id='opaque-123')
        self.assertEqual(found['task_anchors']['items'][0]['role'], 'context')
        self.assertEqual(found['decision_sources']['items'][0]['relation'], 'decision_association')
        self.store.answer(node, {'answer': 'Independent task decision.', 'signed_by': 'Handover Owner',
                                'evidence_mode': 'independent'})
        retired = self.lookup(external_id='opaque-123')['decision_sources']['items']
        self.assertTrue(retired)
        self.assertTrue(all(edge['relation'] == 'decision_history' and not edge['edge_active'] for edge in retired))

    def test_abandoned_task_edges_are_history_even_when_storage_edges_remain_active(self):
        source = self.source(task_id=self.task)
        node = self.node(source)
        canvas.abandon_task(self.store, self.task, 'Synthetic abandoned attempt')
        found = self.lookup(external_id='opaque-123')
        edge = next(edge for edge in found['decision_sources']['items'] if edge['decision_id'] == node and edge['edge_active'])
        self.assertEqual(edge['task_status'], 'abandoned')
        self.assertEqual(edge['decision_status'], 'withdrawn')
        self.assertEqual(edge['relation'], 'decision_history')
        self.assertTrue(edge['historical'])
        self.assertEqual(found['task_anchors']['items'][0]['task_status'], 'abandoned')

    def test_superseded_signed_decision_is_history_with_explicit_replacement_metadata(self):
        source = self.source(task_id=self.task)
        old = self.node(source)
        canvas.sign_off(self.store, old, {'by': 'Handover Owner', 'expected_updated_at': self.store.get_decision(old)['updated_at']})
        replacement = self.node()
        self.store.answer(replacement, {'answer': 'Use a different independent policy.', 'signed_by': 'Handover Owner', 'supersedes': old})
        found = self.lookup(external_id='opaque-123')
        edge = next(edge for edge in found['decision_sources']['items'] if edge['decision_id'] == old and edge['edge_active'])
        self.assertEqual(edge['decision_signoff'], 'signed')
        self.assertEqual(edge['decision_superseded_by'], replacement)
        self.assertEqual(edge['relation'], 'decision_history')
        self.assertTrue(edge['historical'])
        association = next(edge for edge in found['task_decisions']['items'] if edge['decision_id'] == old)
        self.assertEqual(association['decision_superseded_by'], replacement)

    def test_completed_proof_and_signature_history_are_unchanged_by_lookup(self):
        source = self.source(task_id=self.task)
        node = self.node(source)
        canvas.sign_off(self.store, node, {'by': 'Handover Owner', 'expected_updated_at': self.store.get_decision(node)['updated_at']})
        self.store.update_run(self.task, {'status': 'completed'})
        bundle = proof.create(self.store, self.task, 'diff --git a/a b/a\n+synthetic\n', checks='synthetic test')
        frozen = json.dumps(bundle, sort_keys=True)
        self.source(body='Later observation differs.')
        before = self.frozen_state()
        found = self.lookup(external_id='opaque-123')
        self.assertTrue(all(edge['relation'] == 'decision_history' for edge in found['decision_sources']['items']))
        self.assertTrue(any(edge['decision_signoff'] == 'signed' and edge['decision_needs_review'] for edge in found['decision_sources']['items']))
        self.assertEqual(before, self.frozen_state())
        self.assertEqual(json.dumps(proof.export(self.store, {'task_id': self.task})['bundle'], sort_keys=True), frozen)

    def test_unknown_unavailable_and_inaccessible_duplicate_refuse_content(self):
        self.assertEqual(self.lookup(external_id='missing')['status'], 'unknown')
        a = self.source(task_id=self.task)
        self.node(a)
        self.source(availability='inaccessible')
        blocked = self.lookup(namespace='site-a', external_id='opaque-123')
        self.assertEqual(blocked['status'], 'unavailable')
        for key in ('versions', 'latest_observed', 'task_anchors', 'decision_sources', 'task_decisions'):
            self.assertNotIn(key, blocked)
        b = self.source(namespace='site-b')
        ambiguous = self.lookup(ref='POL-7')
        self.assertEqual(ambiguous['status'], 'ambiguous')
        self.assertEqual({r['availability'] for r in ambiguous['candidates']['items']}, {'available', 'inaccessible'})
        self.assertEqual(self.lookup(namespace='site-b', external_id='opaque-123')['source']['record_id'], b['record_id'])
        self.assertTrue(self.mcp('bridge_get_record', {'repo': REPO, 'record_id': a['record_id']}, self.agent)['isError'])
        self.assertEqual(self.status_of('GET', '/api/records?' + urlencode({'repo': REPO, 'record_id': a['record_id']}), token=self.agent), 404)
        self.source(availability='deleted')
        self.assertEqual(self.lookup(namespace='site-a', external_id='opaque-123')['status'], 'unavailable')

    def test_repository_isolation_never_resolves_by_basename_or_foreign_relation(self):
        a = self.source(task_id=self.task)
        other_task = self.store.add_run({'title': 'Other repository', 'repo': 'other/handover'})['id']
        other = self.source(repo='other/handover', task_id=other_task)
        self.node(other, task=other_task, repo='other/handover')
        self.assertEqual(self.lookup(ref='POL-7')['source']['record_id'], a['record_id'])
        self.assertEqual(self.lookup(repo='other/handover', ref='POL-7')['source']['record_id'], other['record_id'])
        self.assertEqual(self.lookup(repo='handover', ref='POL-7')['status'], 'unknown')
        self.assertEqual(self.lookup(repo='unknown/handover', ref='POL-7')['status'], 'unknown')
        self.assertNotIn(other_task, json.dumps(self.lookup(ref='POL-7')))
        # Fail closed even for a malformed cross-repo edge already in storage.
        with self.store.graph.transaction():
            cm.add_anchor(self.store.graph.db, self.task, a['record_id'], a['source_version_id'])
            self.store.graph.db.execute('UPDATE runs SET repo=? WHERE id=?', ('foreign/repo', self.task))
        self.assertEqual(self.lookup(ref='POL-7')['task_anchors']['items'], [])
        self.assertEqual(self.lookup(ref='POL-7')['task_decisions']['items'], [])

    def test_lookup_metadata_omits_transcripts_notifications_and_credential_urls(self):
        source = self.source(provider='slack', namespace='TSYNTH', kind='slack',
                             external_id='CSYNTH:123.456', body='SYNTHETIC_TRANSCRIPT_CANARY',
                             url='https://synthetic-user:synthetic-pass@example.invalid/message?token=SYNTHETIC_URL_CANARY',
                             access_scope='SYNTHETIC_AUDIENCE_CANARY', author='SYNTHETIC_AUTHOR_CANARY')
        self.store.graph.set_source(REPO, 'synthetic-connector', 'SYNTHETIC_CONNECTOR_CANARY')
        found = json.dumps(self.lookup(provider='slack', namespace='TSYNTH', external_id='CSYNTH:123.456'))
        for canary in ('SYNTHETIC_TRANSCRIPT_CANARY', 'SYNTHETIC_URL_CANARY', 'synthetic-pass',
                       'SYNTHETIC_AUDIENCE_CANARY', 'SYNTHETIC_AUTHOR_CANARY', 'SYNTHETIC_CONNECTOR_CANARY'):
            self.assertNotIn(canary, found)
        self.assertIn(source['record_id'], found)

    def test_identifier_urls_are_matched_exactly_but_credentials_are_not_echoed(self):
        external = 'https://synthetic-user:synthetic-pass@example.invalid/object?token=EXTERNAL_CANARY'
        ref = 'https://example.invalid/thread?token=REF_CANARY'
        source = self.source(external_id=external, ref=ref)
        found = self.lookup(provider='jira', namespace='site-a', object_kind='jira', external_id=external)
        self.assertEqual(found['status'], 'matched')
        self.assertEqual(found['source']['record_id'], source['record_id'])
        self.assertEqual(found['source']['ref'], '')
        self.assertIn('ref', found['source']['redacted_fields'])
        identity = found['source']['matching_identities']['items'][0]
        self.assertEqual(identity['external_id'], '')
        self.assertIn('external_id', identity['redacted_fields'])
        self.assertEqual(found['latest_observed']['external_id'], '')
        for secret in ('synthetic-pass', 'EXTERNAL_CANARY', 'REF_CANARY'):
            self.assertNotIn(secret, json.dumps(found))
        self.assertEqual(self.lookup(ref=ref)['source']['record_id'], source['record_id'])
        self.assertEqual(self.lookup(external_id='https://example.invalid/object')['status'], 'unknown')

    def test_scheme_relative_identifier_credentials_are_not_echoed(self):
        external = '//synthetic-user:RELATIVE_PASSWORD@example.invalid/object?token=RELATIVE_TOKEN'
        ref = '//example.invalid/thread#RELATIVE_FRAGMENT'
        source = self.source(external_id=external, ref=ref)
        found = self.lookup(external_id=external)
        self.assertEqual(found['source']['record_id'], source['record_id'])
        self.assertIn('external_id', found['latest_observed']['redacted_fields'])
        for secret in ('RELATIVE_PASSWORD', 'RELATIVE_TOKEN', 'RELATIVE_FRAGMENT'):
            self.assertNotIn(secret, json.dumps(found))
        self.source(namespace='site-b', external_id=external, ref=ref)
        ambiguous = self.lookup(external_id=external)
        self.assertEqual(ambiguous['status'], 'ambiguous')
        for candidate in ambiguous['candidates']['items']:
            self.assertIn('ref', candidate['redacted_fields'])
            self.assertIn('external_id', candidate['matching_identities']['items'][0]['redacted_fields'])
        self.assertNotIn('RELATIVE_PASSWORD', json.dumps(ambiguous))

    def test_limits_report_truncation_and_never_collapse_ambiguity(self):
        self.source()
        self.source(namespace='site-b')
        self.source(namespace='site-c')
        ambiguous = self.lookup(ref='POL-7', limit=1)
        self.assertEqual(ambiguous['status'], 'ambiguous')
        self.assertEqual(len(ambiguous['candidates']['items']), 1)
        self.assertTrue(ambiguous['candidates']['truncated'])
        source = self.source(namespace='site-only', external_id='many-versions')
        for number in range(4):
            task = self.store.add_run({'title': f'Synthetic task {number}', 'repo': REPO})['id']
            current = self.source(namespace='site-only', external_id='many-versions', body=f'Synthetic revision {number}', task_id=task)
            self.node(current, task=task)
        found = self.lookup(namespace='site-only', external_id='many-versions', limit=2)
        for key in ('versions', 'task_anchors', 'decision_sources', 'task_decisions'):
            self.assertEqual(len(found[key]['items']), 2, key)
            self.assertTrue(found[key]['truncated'], key)
            self.assertEqual(found[key]['limit'], 2)
        self.assertNotEqual(found['latest_observed']['source_version_id'], source['source_version_id'])

    def test_contract_validation_and_read_only_viewer_access(self):
        listed = self.post('/mcp', {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/list'}, token=self.agent)['result']['tools']
        self.assertIn('bridge_lookup_record', [tool['name'] for tool in listed])
        self.assertEqual(self.status_of('GET', '/api/records/lookup?' + urlencode({'repo': REPO, 'ref': 'POL-7'})), 401)
        for args in ({'repo': REPO}, {'repo': REPO, 'ref': 'POL-7', 'external_id': 'opaque-123'},
                     {'repo': '', 'ref': 'POL-7'}, {'repo': 'https://github.com/' + REPO, 'ref': 'POL-7'},
                     {'repo': REPO, 'ref': 'POL-7', 'limit': 0}, {'repo': REPO, 'ref': 'POL-7', 'limit': 101},
                     {'repo': REPO, 'ref': 'POL-7', 'unknown': 'ignored'}, {'repo': REPO, 'ref': 'POL-7', 'limit': 'many'}):
            self.assertTrue(self.mcp('bridge_lookup_record', args, self.agent)['isError'], args)
            self.assertEqual(self.status_of('GET', '/api/records/lookup?' + urlencode(args), token=self.agent), 400, args)
        self.assertEqual(self.status_of('GET', '/api/records/lookup?repo=' + REPO + '&ref=A&ref=B', token=self.agent), 400)
        self.assertEqual(self.status_of('POST', '/mcp', {'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call',
            'params': {'name': 'bridge_import_record', 'arguments': {'repo': REPO, 'kind': 'jira', 'ref': 'NEW-1', 'title': 'not allowed'}}}, token=self.agent), 403)
        self.assertEqual(self.lookup(ref='NEW-1')['status'], 'unknown')

    def test_restart_retains_exact_external_handover(self):
        source = self.source(task_id=self.task)
        node = self.node(source)
        restarted = Store(self.store.path)
        self.addCleanup(restarted.graph.close)
        with patch.object(self.store, 'lookup_record', side_effect=restarted.lookup_record):
            found = self.lookup(provider='jira', namespace='site-a', object_kind='jira', external_id='opaque-123')
        self.assertEqual(found['source']['record_id'], source['record_id'])
        self.assertEqual(found['decision_sources']['items'][0]['decision_id'], node)
