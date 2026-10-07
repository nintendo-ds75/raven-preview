"""Offline existing-record association contracts; no provider or model calls."""
import json
import threading
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
from test_auth import SharedServer
from bridge import canvas, context_memory as cm, github, host_client, mcp, proof
from bridge.config import Config
from bridge.store import Invalid, Store

REPO = 'synthetic/work-items'


class WorkItemLinksTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'links.db')
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.store.add_owner({'name': 'Synthetic Policy Owner', 'team': 'Synthetic', 'patterns': 'policy/*'})
        self.task = canvas.start_task(self.store, Config(model_api='none'), {
            'repo': REPO, 'title': 'Update archive policy', 'facts': 'work_item=CASE-1'})['task_id']
        self.did = canvas.add_node(self.store, Config(model_api='none'), {'task_id': self.task,
            'question': 'How long should archived rows remain?', 'category': 'policy',
            'paths': 'policy/archive.py', 'facts': 'work_item=CASE-1'})['node_id']
        for target in ('bridge.llm.Client.complete', 'bridge.llm.Client.complete_json',
                       'bridge.github.GitHubAPI.get', 'bridge.mcp.index_repo'):
            guard = patch(target, side_effect=AssertionError('External calls forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def source(self, **fields):
        return self.store.add_record({'repo': REPO, 'provider': 'jira', 'namespace': 'site-a',
            'kind': 'jira', 'ref': 'CASE-1', 'external_id': 'object-1', 'title': 'Synthetic archive change',
            'body': 'Consider archive retention.', 'status': 'Open', **fields})['source']

    def args(self, source, **changes):
        return {'task_id': self.task, 'repo': REPO, 'record_id': source['record_id'],
            'source_version_id': source['source_version_id'], 'provider': source['provider'],
            'namespace': source['namespace'], 'object_kind': source['kind'],
            'external_id': source['external_id'], **changes}

    def frozen(self, tables=None):
        tables = tables or ('runs', 'decisions', 'decision_versions', 'decision_source_edges',
            'decision_links', 'source_records', 'source_versions', 'source_identities',
            'task_source_anchors', 'authority', 'events', 'notifications')
        return {table: sorted(json.dumps(dict(r), sort_keys=True, default=str) for r in self.graph.db.execute('SELECT * FROM ' + table))
                for table in tables}

    def test_supported_lookup_read_link_flow_has_no_import_or_authority(self):
        source = self.source()
        before = self.frozen(('decisions', 'decision_versions', 'decision_source_edges', 'decision_links',
                              'source_records', 'source_versions', 'source_identities', 'authority', 'notifications'))
        found = mcp.call_tool(self.store, 'bridge_lookup_record', {'repo': REPO, 'ref': 'CASE-1'})
        read = mcp.call_tool(self.store, 'bridge_get_record', {'repo': REPO, 'record_id': found['source']['record_id']})
        linked = mcp.call_tool(self.store, 'bridge_link_work_item', self.args(read['source']))
        self.assertTrue(linked['changed'])
        self.assertEqual(linked['relation'], 'task_anchor')
        tree = mcp.call_tool(self.store, 'bridge_get_tree', {'task_id': self.task})
        self.assertEqual(tree['work_item_association']['status'], 'linked')
        self.assertEqual(tree['nodes'][0]['work_item_association']['status'], 'linked')
        self.assertEqual(tree['source_anchors'][0]['source_version_id'], source['source_version_id'])
        self.assertEqual(tree['nodes'][0]['sources'], [])
        self.assertFalse(tree['nodes'][0]['authorized'])
        lookup = self.store.lookup_record(repo=REPO, ref='CASE-1')
        self.assertEqual(lookup['task_anchors']['items'][0]['task_id'], self.task)
        self.assertEqual(lookup['task_decisions']['items'][0]['relation'], 'task_anchor_association')
        self.assertEqual(lookup['decision_sources']['items'], [])
        decision = self.store.get_decision(self.did)
        self.assertEqual(decision['work_item_association']['status'], 'linked')
        self.assertEqual(len(decision['work_item_history']), 1)
        self.assertEqual(decision['work_item_history'][0]['kind'], 'work_item_linked')
        trace = canvas.trace(self.store, self.task)
        self.assertEqual(trace['work_item_association']['status'], 'linked')
        self.assertEqual(len([e for e in trace['events'] if e['kind'] == 'work_item_linked']), 1)
        self.assertEqual(before, self.frozen(before.keys()))

    def test_structured_declaration_is_unlinked_prose_and_client_ref_are_not_parsed(self):
        source = self.source()
        tree = canvas.get_tree(self.store, self.task)
        self.assertEqual(tree['work_item_association']['status'], 'unlinked')
        self.assertIn('bridge_link_work_item', tree['next'])
        node = canvas.node_view(self.store, self.did)
        self.assertEqual(node['work_item_association']['status'], 'unlinked')
        with self.graph.transaction() as db:
            db.execute("UPDATE decisions SET facts='{}',client_ref='CASE-1',context='Issue CASE-1' WHERE id=?", (self.did,))
        self.assertEqual(canvas.node_view(self.store, self.did)['work_item_association']['status'], 'undeclared')
        self.assertEqual(cm.anchors(self.graph.db, self.task), [])
        self.assertEqual(self.store.lookup_record(repo=REPO, ref=source['ref'])['task_anchors']['items'], [])

    def test_current_retry_is_strict_noop_including_history(self):
        args = self.args(self.source())
        self.store.link_work_item(args)
        before = self.frozen()
        retry = self.store.link_work_item(args)
        self.assertFalse(retry['changed'])
        self.assertEqual(before, self.frozen())

    def test_context_is_not_a_work_item_or_premise(self):
        self.store.link_work_item(self.args(self.source(), role='context'))
        tree = canvas.get_tree(self.store, self.task)
        self.assertEqual(tree['work_item_association']['status'], 'unlinked')
        self.assertEqual(tree['source_anchors'][0]['role'], 'context')
        self.assertEqual(tree['nodes'][0]['sources'], [])
        self.assertFalse(tree['nodes'][0]['authorized'])

    def test_existing_settle_work_item_is_decision_association_not_task_anchor(self):
        source = self.source()
        mcp.call_tool(self.store, 'bridge_settle_node', {'task_id': self.task, 'node_id': self.did,
            'answer': 'Proposed archive setting, with this issue as context.',
            'source_evidence': [{'record_id': source['record_id'],
                'source_version_id': source['source_version_id'], 'role': 'work_item'}]})
        tree = canvas.get_tree(self.store, self.task)
        self.assertEqual(tree['source_anchors'], [])
        self.assertEqual(tree['work_item_association']['status'], 'unlinked')
        self.assertEqual(tree['nodes'][0]['work_item_association']['status'], 'linked')
        self.assertEqual(tree['nodes'][0]['sources'][0]['role'], 'work_item')
        self.assertEqual(tree['nodes'][0]['work_item_association']['links'][0]['origin'], 'decision')
        self.assertEqual(tree['nodes'][0]['work_item_association']['links'][0]['record_id'], source['record_id'])
        self.assertEqual(tree['work_item_association']['links'], [])
        self.assertFalse(tree['nodes'][0]['authorized'])
        self.assertEqual(tree['nodes'][0]['signoff'], 'required')
        lookup = self.store.lookup_record(repo=REPO, ref=source['ref'])
        self.assertEqual(lookup['decision_sources']['items'][0]['relation'], 'decision_association')
        self.assertEqual(lookup['task_anchors']['items'], [])
        self.assertEqual(lookup['task_decisions']['items'], [])
        before = self.frozen(('decisions', 'decision_versions', 'decision_source_edges', 'decision_links'))
        self.store.link_work_item(self.args(source))
        self.assertEqual(before, self.frozen(before.keys()))
        lookup = self.store.lookup_record(repo=REPO, ref=source['ref'])
        self.assertEqual(lookup['decision_sources']['items'][0]['relation'], 'decision_association')
        self.assertEqual(lookup['task_decisions']['items'][0]['relation'], 'task_anchor_association')
        self.assertEqual(canvas.get_tree(self.store, self.task)['work_item_association']['status'], 'linked')
        links = canvas.node_view(self.store, self.did)['work_item_association']['links']
        self.assertEqual({s['origin'] for s in links}, {'task', 'decision'})

    def test_ref_ambiguity_requires_explicit_stable_identity(self):
        source = self.source()
        other = self.source(external_id='object-2')
        args = self.args(source)
        del args['external_id']
        args['ref'] = 'CASE-1'
        before = self.frozen()
        with self.assertRaisesRegex(Invalid, 'ambiguous'):
            self.store.link_work_item(args)
        self.assertEqual(before, self.frozen())
        self.store.link_work_item(self.args(source))
        self.assertEqual(canvas.get_tree(self.store, self.task)['work_item_association']['status'], 'linked')
        self.store.link_work_item(self.args(other))
        tree = canvas.get_tree(self.store, self.task)
        self.assertEqual(tree['work_item_association']['status'], 'ambiguous')
        self.assertIn('Inspect work_item_association.links', tree['next'])
        self.assertNotIn('use bridge_link_work_item', tree['next'])
        association = tree['nodes'][0]['work_item_association']
        self.assertEqual(association['status'], 'ambiguous')
        self.assertEqual({s['record_id'] for s in association['links']}, {source['record_id'], other['record_id']})
        self.assertEqual({s['origin'] for s in association['links']}, {'task'})

    def test_wrong_repository_identity_namespace_kind_and_pin_are_atomic(self):
        source = self.source()
        another = self.source(external_id='object-2', ref='CASE-2')
        foreign = self.source(repo='synthetic/other')
        variants = [self.args(source, repo='synthetic/other'), self.args(source, task_id='absent'),
                    self.args(source, namespace='site-b'), self.args(source, object_kind='ticket'),
                    self.args(source, provider='generic'), self.args(source, external_id='object-2'),
                    self.args(source, source_version_id=another['source_version_id']),
                    self.args(source, source_version_id='absent'), self.args(foreign),
                    self.args(source, role='support'), self.args(source, role='contradiction')]
        before = self.frozen()
        for args in variants:
            with self.subTest(args=args), self.assertRaises(Invalid):
                self.store.link_work_item(args)
            self.assertEqual(before, self.frozen())

    def test_existing_task_namespace_is_not_silently_widened(self):
        source = self.source(task_id=self.task)
        other = self.source(namespace='site-b')
        before = self.frozen()
        with self.assertRaisesRegex(Invalid, 'selected task namespace'):
            self.store.link_work_item(self.args(other))
        self.assertEqual(before, self.frozen())
        self.assertFalse(self.store.link_work_item(self.args(source))['changed'])

    def test_native_git_to_github_alias_uses_anchored_canonical_namespace(self):
        self.graph.upsert_intent(REPO, 'pr', '42', 'Local synthetic PR', 'Local text', '', '',
            metadata={'provider': 'git', 'namespace': REPO, 'object_kind': 'pr', 'external_id': '42', 'display_ref': '42'})
        github.apply_pull(self.graph, REPO, {'repo': REPO, 'number': 42, 'title': 'Synthetic GitHub PR',
            'body': 'Native metadata', 'author': 'synthetic-author', 'merged_by': '', 'merged_at': '',
            'updated_at': '', 'merge_sha': '', 'files': [], 'reviews': []})
        found = self.store.lookup_record(repo=REPO, provider='github', namespace='github.com',
            object_kind='pr', external_id=REPO + '/pull/42')
        source = self.store.get_record(found['source']['record_id'], REPO)['source']
        legacy_row = self.graph.db.execute('SELECT provider,namespace FROM source_records WHERE id=?',
                                          (source['record_id'],)).fetchone()
        self.assertEqual((legacy_row['provider'], legacy_row['namespace']), ('git', REPO))
        self.assertEqual((source['provider'], source['namespace']), ('github', 'github.com'))
        self.store.link_work_item(self.args(source))
        other = self.source(provider='github', namespace='second-installation', kind='issue', external_id='issue-7')
        before = self.frozen()
        with self.assertRaisesRegex(Invalid, 'selected task namespace'):
            self.store.link_work_item(self.args(other))
        self.assertEqual(before, self.frozen())
        same = self.source(provider='github', namespace='github.com', kind='issue', external_id='issue-8')
        self.assertTrue(self.store.link_work_item(self.args(same))['changed'])

    def test_stale_unavailable_and_unfresh_reads_do_not_create_links(self):
        old = self.source()
        current = self.source(body='A newer observation.')
        with self.assertRaisesRegex(Invalid, 'changed'):
            self.store.link_work_item(self.args(old))
        for availability in ('deleted', 'inaccessible'):
            self.source(availability=availability)
            with self.assertRaisesRegex(Invalid, 'unavailable'):
                self.store.link_work_item(self.args(current))
        current = self.source(availability='available')
        self.graph.db.execute('INSERT INTO context_entities(record_id,repo,collection,entity_id,source_name,chunk_index,generation,verified_until,checked_at,state,query) '
            "VALUES(?,?, 'test','test','test',0,1,'2000-01-01','','stale','')", (current['record_id'], REPO))
        with self.assertRaisesRegex(Invalid, 'unavailable'):
            self.store.link_work_item(self.args(current))
        self.assertEqual(cm.anchors(self.graph.db, self.task), [])

    def test_new_versions_and_changed_declarations_retain_prior_dependencies(self):
        source = self.source()
        self.store.link_work_item(self.args(source))
        premise = self.source(
            provider='legacy', namespace='legacy', kind='doc', ref='ADR-1', external_id='adr')
        canvas.settle_node(self.store, {'task_id': self.task, 'node_id': self.did, 'answer': 'Keep thirty days.',
            'source_evidence': [{'record_id': premise['record_id'], 'source_version_id': premise['source_version_id'], 'role': 'support'}]})
        before = self.frozen(('decisions', 'decision_versions', 'decision_source_edges', 'decision_links'))
        current = self.source(body='Updated work-item context only.')
        self.store.link_work_item(self.args(current))
        self.assertEqual(before, self.frozen(before.keys()))
        anchors = cm.anchors(self.graph.db, self.task)
        self.assertEqual({s['source_version_id'] for s in anchors}, {source['source_version_id'], current['source_version_id']})
        canvas.start_task(self.store, Config(model_api='none'), {'task_id': self.task, 'facts': 'work_item=CASE-NEW'})
        tree = canvas.get_tree(self.store, self.task)
        self.assertEqual(tree['work_item_association']['status'], 'unlinked')
        self.assertEqual(len(tree['source_anchors']), 2)
        self.assertEqual(before, self.frozen(before.keys()))

    def test_link_does_not_change_existing_signature_or_decision_snapshot(self):
        source = self.source()
        canvas.settle_node(self.store, {'task_id': self.task, 'node_id': self.did, 'answer': 'Keep thirty days.'})
        canvas.sign_off(self.store, self.did, {'by': 'Synthetic Policy Owner',
            'expected_updated_at': self.store.get_decision(self.did)['updated_at']})
        before = self.frozen(('decisions', 'decision_versions', 'decision_source_edges', 'decision_links', 'authority'))
        self.store.link_work_item(self.args(source))
        self.assertEqual(before, self.frozen(before.keys()))
        self.assertTrue(self.store.get_decision(self.did)['authorized'])
        self.source(body='New background only.')
        self.assertTrue(self.store.get_decision(self.did)['authorized'])

    def test_completed_legacy_history_and_saved_proof_are_not_reconstructed(self):
        source = self.source()
        canvas.settle_node(self.store, {'task_id': self.task, 'node_id': self.did, 'answer': 'Keep thirty days.'})
        canvas.sign_off(self.store, self.did, {'by': 'Synthetic Policy Owner',
            'expected_updated_at': self.store.get_decision(self.did)['updated_at']})
        self.store.update_run(self.task, {'status': 'completed'})
        bundle = proof.create(self.store, self.task, 'diff --git a/a b/a\n+synthetic\n')
        before = self.frozen()
        with self.assertRaisesRegex(Invalid, 'Closed task'):
            self.store.link_work_item(self.args(source))
        self.assertEqual(before, self.frozen())
        self.assertEqual(canvas.get_tree(self.store, self.task)['work_item_association']['status'], 'unlinked')
        tree = canvas.get_tree(self.store, self.task)
        self.assertTrue(tree['work_item_association']['historical'])
        self.assertIn('start a new task', tree['next'])
        self.assertNotIn('bridge_link_work_item', tree['next'])
        for projected in (tree['work_item_association'], tree['nodes'][0]['work_item_association'],
                          canvas.node_view(self.store, self.did)['work_item_association'],
                          self.store.get_decision(self.did)['work_item_association'],
                          canvas.trace(self.store, self.task)['work_item_association']):
            self.assertTrue(projected['historical'])
            self.assertIn('start a new task', projected['notice'])
            self.assertNotIn('bridge_link_work_item', projected['notice'])
        self.assertEqual(cm.anchors(self.graph.db, self.task), [])
        exported = proof.export(self.store, {'task_id': self.task})
        self.assertEqual(bundle, exported['bundle'])
        self.assertFalse(exported['stale'])
        self.assertTrue(proof.verify(bundle)['valid'])

    def test_abandoned_task_refuses_new_link(self):
        source = self.source()
        canvas.abandon_task(self.store, self.task, 'Synthetic cancelled task')
        before = self.frozen()
        with self.assertRaisesRegex(Invalid, 'Closed task'):
            self.store.link_work_item(self.args(source))
        self.assertEqual(before, self.frozen())

    def test_completed_import_compatibility_does_not_rewrite_saved_proof(self):
        source = self.source()
        canvas.settle_node(self.store, {'task_id': self.task, 'node_id': self.did, 'answer': 'Keep thirty days.'})
        canvas.sign_off(self.store, self.did, {'by': 'Synthetic Policy Owner',
            'expected_updated_at': self.store.get_decision(self.did)['updated_at']})
        self.store.update_run(self.task, {'status': 'completed'})
        bundle = proof.create(self.store, self.task, 'diff --git a/a b/a\n+synthetic\n')
        before = self.frozen(('decisions', 'decision_versions', 'decision_source_edges', 'decision_links'))
        self.source(task_id=self.task)
        self.assertEqual(before, self.frozen(before.keys()))
        self.assertEqual(cm.anchors(self.graph.db, self.task)[0]['source_version_id'], source['source_version_id'])
        exported = proof.export(self.store, {'task_id': self.task})
        self.assertEqual(bundle, exported['bundle'])
        self.assertFalse(exported['stale'])

    def test_duplicate_node_does_not_borrow_canonical_work_item_association(self):
        source = self.source()
        other = self.store.add_run({'repo': REPO, 'title': 'Separate task'})['id']
        original = self.graph.add_decision(other, 'Original question', 'policy', 'pending', repo=REPO)
        with self.graph.transaction():
            cm.attach(self.graph.db, original, [{'record_id': source['record_id'],
                'source_version_id': source['source_version_id'], 'role': 'work_item'}])
            self.graph.update_decision(self.did, status='duplicate', superseded_by=original)
        self.assertEqual(canvas.node_view(self.store, self.did)['work_item_association']['status'], 'unlinked')
        self.assertEqual(canvas.get_tree(self.store, self.task)['nodes'][0]['work_item_association']['status'], 'unlinked')
        self.assertEqual(self.store.get_decision(self.did)['work_item_association']['status'], 'unlinked')

    def test_completion_committed_while_link_waits_rechecks_task_lifecycle(self):
        source = self.source()
        task = self.store.add_run({'repo': REPO, 'title': 'Empty task completing'})['id']
        args = self.args(source, task_id=task)
        started, errors = threading.Event(), []
        other = Store(self.store.path)
        self.addCleanup(other.graph.close)
        def attempt():
            started.set()
            try:
                other.link_work_item(args)
            except BaseException as error:
                errors.append(error)
            finally:
                other.graph.close()
        with self.graph.transaction() as db:
            db.execute("UPDATE runs SET status='completed' WHERE id=?", (task,))
            thread = threading.Thread(target=attempt)
            thread.start()
            self.assertTrue(started.wait(5))
        thread.join(20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], Invalid)
        self.assertIn('Closed task', str(errors[0]))
        self.assertEqual(cm.anchors(self.graph.db, task), [])

    def test_concurrent_retries_create_one_anchor_and_one_history_event(self):
        args = self.args(self.source())
        barrier = threading.Barrier(4)
        results, errors = [], []
        def attempt():
            store = Store(self.store.path)
            try:
                barrier.wait(10)
                results.append(store.link_work_item(args))
            except BaseException as error:
                errors.append(error)
            finally:
                store.graph.close()
        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sum(r['changed'] for r in results), 1)
        self.assertEqual(len(cm.anchors(self.graph.db, self.task)), 1)
        self.assertEqual(self.graph.count_events('work_item_linked', task_id=self.task), 1)

    def test_source_update_committed_while_link_waits_rechecks_head(self):
        old = self.source()
        started, errors = threading.Event(), []
        other = Store(self.store.path)
        self.addCleanup(other.graph.close)
        def attempt():
            started.set()
            try:
                other.link_work_item(self.args(old))
            except BaseException as error:
                errors.append(error)
            finally:
                other.graph.close()
        with self.graph.transaction():
            self.source(body='Committed before the blocked link.')
            thread = threading.Thread(target=attempt)
            thread.start()
            self.assertTrue(started.wait(5))
        thread.join(20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], Invalid)
        self.assertIn('changed', str(errors[0]))
        self.assertEqual(cm.anchors(self.graph.db, self.task), [])

    def test_existing_import_path_remains_supported_without_new_global_guards(self):
        source = self.source(task_id=self.task)
        self.assertEqual(cm.anchors(self.graph.db, self.task)[0]['record_id'], source['record_id'])
        other = self.source(namespace='site-b', task_id=self.task)
        self.assertEqual(len(cm.anchors(self.graph.db, self.task)), 2)
        # Existing import historically accepts multiple explicitly selected
        # installations; the new API does not claim to change that contract.
        self.assertFalse(self.store.link_work_item(self.args(other))['changed'])

    def test_schema_and_hook_describe_existing_record_flow(self):
        schema = next(t for t in mcp.TOOLS if t['name'] == 'bridge_link_work_item')
        self.assertNotIn(schema['name'], mcp.READ_ONLY_TOOLS)
        self.assertIn('bridge_link_work_item', mcp.INSTRUCTIONS)
        tools = ['bridge_link_work_item', 'bridge_get_tree']
        with patch.object(host_client, 'verify_project'), patch.object(host_client, 'capabilities', return_value=tools), \
             patch.object(host_client, 'call', return_value={'task_id': self.task}), patch.object(host_client, 'binding', return_value={}):
            out = host_client.hook({'project': self.temp.name}, 'claude', {'hook_event_name': 'SessionStart', 'session_id': 'synthetic'})
        text = out['hookSpecificOutput']['additionalContext']
        for token in ('bridge_lookup_record', 'bridge_get_record', 'bridge_link_work_item', 'facts.work_item alone remains unlinked'):
            self.assertIn(token, text)


class WorkItemTransportTests(SharedServer):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.store.graph.close)
        member = self.store.add_person({'name': 'Synthetic Host', 'role': 'member'})
        self.agent = self.auth.create_token(member['id'], label='synthetic')['token']
        self.task = self.mcp('bridge_start_task', {'repo': REPO, 'title': 'A synthetic work item',
                                                  'facts': 'work_item=CASE-1'}, self.agent)['result']['task_id']
        self.source = self.store.add_record({'repo': REPO, 'provider': 'jira', 'namespace': 'site-a',
            'kind': 'jira', 'ref': 'CASE-1', 'external_id': 'object-1', 'title': 'Synthetic issue'})['source']
        self.args = {'task_id': self.task, 'repo': REPO, 'provider': 'jira', 'namespace': 'site-a',
            'object_kind': 'jira', 'external_id': 'object-1',
            'record_id': self.source['record_id'], 'source_version_id': self.source['source_version_id']}
        self.route = '/api/tasks/' + self.task + '/work-items'

    def test_mcp_rest_same_checks_and_idempotency(self):
        first = self.mcp('bridge_link_work_item', self.args, self.agent)
        self.assertFalse(first['isError'], first)
        retry = self.post(self.route, self.args, token=self.agent)
        self.assertEqual({**first['result'], 'changed': False}, retry)
        for bad in ({'unexpected': True}, {'role': 'support'}, {'source_version_id': 'wrong'},
                    {'provider': ''}, {'ref': 'CASE-1'}, {'namespace': 'site-b'},
                    {'repo': 'https://github.com/' + REPO}, {'record_id': []}, {'role': []}):
            with self.subTest(bad=bad):
                self.assertTrue(self.mcp('bridge_link_work_item', {**self.args, **bad}, self.agent)['isError'])
                self.assertEqual(self.status_of('POST', self.route, {**self.args, **bad}, token=self.agent), 400)
        self.assertEqual(self.status_of('POST', self.route, {**self.args, 'task_id': 'different'}, token=self.agent), 400)
        self.assertEqual(self.store.graph.count_events('work_item_linked', task_id=self.task), 1)

    def test_viewer_is_read_only_and_anonymous_cannot_link(self):
        viewer = self.store.add_person({'name': 'Synthetic Viewer', 'role': 'viewer'})
        token = self.auth.create_token(viewer['id'], label='synthetic-viewer')['token']
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': 'bridge_link_work_item', 'arguments': self.args}}
        self.assertEqual(self.status_of('POST', '/mcp', request, token=token), 403)
        self.assertEqual(self.status_of('POST', self.route, self.args, token=token), 403)
        self.assertEqual(self.status_of('POST', self.route, self.args, token='invalid'), 401)
        self.assertEqual(cm.anchors(self.store.graph.db, self.task), [])
