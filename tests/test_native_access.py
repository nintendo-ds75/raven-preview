"""Revocation is about retained content, not only skipping the next import."""
import json
from unittest.mock import patch

from bridge import sources, source_access
from bridge.store import Invalid
import test_sources_jira as jira
import test_sources_docs as docs


class JiraAccessTests(jira.JiraSourceTests):
    setUp = jira.JiraSourceTests.setUp
    add = jira.JiraSourceTests.add

    def ingest(self):
        self.jira.issues = [jira.issue(1, '2026-10-01T10:00:00.000+0000')]
        connection = self.add()
        sources.sync(self.store, connection['id'])
        record = self.store.graph.db.execute('SELECT id FROM source_records').fetchone()['id']
        return connection, record

    def test_now_restricted_issue_cannot_be_read_from_old_cache(self):
        connection, record = self.ingest()
        self.jira.issues[0]['fields']['security'] = {'name': 'Private'}
        sources.sync(self.store, connection['id'])
        with self.assertRaisesRegex(Invalid, 'unavailable'):
            self.store.get_record(record, 'example/service')
        self.assertEqual(self.store.graph.db.execute('SELECT state FROM native_source_access').fetchone()['state'], 'revoked')
        self.assertEqual(self.store.graph.db.execute('SELECT availability FROM source_records').fetchone()['availability'], 'available')

    def test_failure_blocks_reuse_without_claiming_deletion_and_success_restores(self):
        connection, record = self.ingest()
        self.jira.fail = (503, {'errorMessages': ['temporary']})
        with self.assertRaises(sources.SourceError):
            sources.sync(self.store, connection['id'])
        with self.assertRaises(Invalid):
            self.store.get_record(record, 'example/service')
        self.assertEqual(self.store.graph.db.execute('SELECT state FROM native_source_access').fetchone()['state'], 'unverified')
        self.jira.fail = None
        sources.sync(self.store, connection['id'])
        self.assertTrue(self.store.get_record(record, 'example/service')['source']['current'])

    def test_disable_and_expired_access_are_not_permission_to_use_retained_content(self):
        connection, record = self.ingest()
        self.store.graph.db.execute("UPDATE native_source_access SET verified_until='2000-01-01'")
        with self.assertRaises(Invalid):
            self.store.get_record(record, 'example/service')

    def test_older_issue_outside_incremental_results_gets_an_exact_refresh(self):
        connection, record = self.ingest()
        self.store.graph.db.execute("UPDATE native_source_access SET verified_until='2000-01-01'")
        with patch('bridge.source_jira.collect', return_value=sources.Batch()):
            sources.sync(self.store, connection['id'])
        self.assertFalse(source_access.blocked_records(self.store.graph.db, 'example/service'))
        self.jira.issues[0]['fields']['security'] = {'name': 'Now private'}
        self.store.graph.db.execute("UPDATE native_source_access SET verified_until='2000-01-01'")
        with patch('bridge.source_jira.collect', return_value=sources.Batch()):
            sources.sync(self.store, connection['id'])
        with self.assertRaises(Invalid):
            self.store.get_record(record, 'example/service')

    def test_disable_keeps_history_but_removes_current_access(self):
        connection, record = self.ingest()
        sources.disable(self.store, connection['id'])
        with self.assertRaises(Invalid):
            self.store.get_record(record, 'example/service')

    def test_newly_restricted_comment_is_withheld_from_retained_versions(self):
        public = {**jira.comment('COMMENT_ACCESS_CANARY'), 'id': 'comment-one'}
        self.jira.issues = [jira.issue(1, '2026-10-01T10:00:00.000+0000', comments=[public])]
        connection = self.add()
        sources.sync(self.store, connection['id'])
        record = self.store.graph.db.execute('SELECT id FROM source_records').fetchone()['id']
        self.assertIn('COMMENT_ACCESS_CANARY', json.dumps(self.store.get_record(record, 'example/service')))
        public['visibility'] = {'type': 'role', 'value': 'Administrators'}
        self.jira.issues[0]['fields']['updated'] = '2026-10-02T10:00:00.000+0000'
        sources.sync(self.store, connection['id'])
        result = self.store.get_record(record, 'example/service')
        self.assertNotIn('COMMENT_ACCESS_CANARY', json.dumps(result))
        self.assertTrue(result['versions'][0]['withheld'])
        self.assertIn('COMMENT_ACCESS_CANARY', self.store.graph.db.execute('SELECT snapshot FROM source_versions ORDER BY sequence').fetchone()[0])


class DocsAccessTests(docs.DocsSourceTests):
    setUp = docs.DocsSourceTests.setUp
    add = docs.DocsSourceTests.add
    records = docs.DocsSourceTests.records

    def test_robots_revocation_is_not_false_deletion_and_never_reuses_the_body(self):
        connection = self.add()
        sources.sync(self.store, connection['id'])
        self.site.page('/robots.txt', 'User-agent: *\nDisallow: /billing\n', content_type='text/plain')
        sources.sync(self.store, connection['id'])
        for row in self.store.graph.db.execute('SELECT id,availability FROM source_records'):
            self.assertEqual(row['availability'], 'available')
            with self.assertRaises(Invalid):
                self.store.get_record(row['id'], 'example/service')

    def test_conditional_304_read_renews_lease_without_new_source_revision(self):
        connection = self.add()
        sources.sync(self.store, connection['id'])
        count = self.store.graph.db.execute('SELECT count(*) FROM source_versions').fetchone()[0]
        self.store.graph.db.execute("UPDATE native_source_access SET verified_until='2000-01-01'")
        sources.sync(self.store, connection['id'])
        self.assertFalse(source_access.blocked_records(self.store.graph.db, 'example/service'))
        self.assertEqual(self.store.graph.db.execute('SELECT count(*) FROM source_versions').fetchone()[0], count)
