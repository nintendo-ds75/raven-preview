"""Accepted sync receipts are separate from immutable supporting evidence."""
import json
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import canvas, context_memory as cm, mcp, proof
from bridge.store import Invalid, Store

REPO = 'synthetic/observation'


class SourceObservationTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'observations.db')
        self.g = self.store.graph
        self.addCleanup(self.g.close)
        self.store.add_owner({'name': 'Ada Example', 'team': 'Policy', 'patterns': 'policy/*'})
        self.task = self.store.add_run({'title': 'Review retention', 'repo': REPO})['id']

    def source(self, **changes):
        return self.store.add_record({'repo': REPO, 'kind': 'jira', 'ref': 'POL-7',
            'title': 'Retention', 'body': 'Keep records for thirty days.', 'status': 'Done',
            'provider': 'jira', 'namespace': 'test-site', 'external_id': 'item-7',
            'paths': ['policy/retention.py'], **changes})

    def signed(self, source):
        row = dict(self.g.db.execute('SELECT i.*,s.id AS record_id,s.head_id AS source_version_id '
            'FROM intents i JOIN source_records s ON s.intent_id=i.id WHERE s.id=?',
            (source['source']['record_id'],)).fetchone())
        did = self.g.add_decision(self.task, 'How long should records remain?', 'policy', 'pending',
            repo=REPO, owner='Ada Example', path='policy/retention.py')
        self.g.publish_evidence(did, [row], source='record', status='resolved', kind='evidence',
            answer='Keep records for thirty days.', signoff='required')
        canvas.sign_off(self.store, did, {'by': 'Ada Example', 'expected_updated_at': self.store.get_decision(did)['updated_at']})
        return did

    def read(self, source):
        return self.store.get_record(source['source']['record_id'], REPO)

    def test_identical_and_metadata_only_syncs_preserve_approval_history_and_proof(self):
        with patch.object(cm, 'stamp', return_value='2026-01-01T00:00:00+00:00'):
            first = self.source(updated_at='2026-01-01T00:00:00Z', source_sequence='1', source_version='v1')
        did = self.signed(first)
        before = self.store.get_decision(did)
        versions = self.read(first)['versions']
        history = cm.history(self.g.db, did)
        self.store.update_run(self.task, {'status': 'completed'})
        historical_task = self.task
        saved = proof.create(self.store, historical_task, 'diff --git a/policy/a b/policy/a\n+unchanged\n')
        self.task = self.store.add_run({'title': 'Active observation control', 'repo': REPO})['id']
        active = self.signed(first)
        active_before = self.store.get_decision(active)
        for ordinal, changes in enumerate(({}, {'updated_at': '2026-01-02T00:00:00Z',
                                                'source_sequence': '2', 'source_version': 'v2'}), 3):
            when = f'2026-01-0{ordinal}T00:00:00+00:00'
            with patch.object(cm, 'stamp', return_value=when):
                accepted = self.source(**changes)
            self.assertFalse(accepted['changed'])
            self.assertEqual(accepted['affected_decisions'], [])
            self.assertEqual(accepted['source']['source_version_id'], first['source']['source_version_id'])
            self.assertEqual(accepted['latest_observation']['observed_at'], when)
            current = self.store.get_decision(did)
            self.assertTrue(current['authorized'])
            self.assertEqual(current['signatures'], before['signatures'])
            self.assertFalse(current['needs_review'])
            self.assertEqual(cm.history(self.g.db, did), history)
            self.assertEqual(self.read(first)['versions'], versions)
            self.assertEqual(proof.export(self.store, {'task_id': historical_task})['bundle'], saved)
            self.assertFalse(proof.export(self.store, {'task_id': historical_task})['stale'])
            active_now = self.store.get_decision(active)
            self.assertTrue(active_now['authorized'])
            self.assertFalse(active_now['needs_review'])
            self.assertEqual(active_now['signatures'], active_before['signatures'])
            self.assertEqual(self.g.db.execute('SELECT count(*) FROM source_review_requests').fetchone()[0], 0)
        receipt = self.read(first)['latest_observation']
        self.assertEqual(receipt['source_updated_at'], '2026-01-02T00:00:00Z')
        self.assertEqual(receipt['source_sequence'], 2)
        self.assertEqual(receipt['source_version'], 'v2')
        self.assertEqual(self.read(first)['source']['source_version'], 'v1')

    def test_current_receipt_is_exposed_separately_by_both_read_tools(self):
        source = self.source(source_version='v1')
        changed = self.source(source_version='v2', updated_at='2026-02-01T00:00:00Z')
        exact = mcp.call_tool(self.store, 'bridge_get_record', {'record_id': source['source']['record_id'], 'repo': REPO})
        lookup = mcp.call_tool(self.store, 'bridge_lookup_record', {'repo': REPO, 'provider': 'jira',
            'namespace': 'test-site', 'external_id': 'item-7'})
        self.assertEqual(exact['latest_observation'], changed['latest_observation'])
        self.assertEqual(exact['source_ordering'], 'provided')
        self.assertEqual(lookup['latest_observation'], exact['latest_observation'])
        self.assertEqual(lookup['latest_observed']['source_version'], 'v1')
        self.assertNotIn('latest_observation', exact['source'])
        self.assertNotIn('last_observed_at', exact['versions'][0]['snapshot'])

    def test_rejected_and_rolled_back_observations_do_not_advance_receipt(self):
        source = self.source(source_sequence='5', updated_at='2026-02-05T00:00:00Z', source_version='v5')
        before = self.read(source)
        with self.assertRaises(Invalid):
            self.source(source_sequence='4', updated_at='2026-02-04T00:00:00Z', source_version='v4')
        self.assertEqual(self.read(source), before)
        with patch.object(self.g, 'append_event', side_effect=RuntimeError('rollback after accepted observation')):
            with self.assertRaises(RuntimeError):
                self.source(source_sequence='6', updated_at='2026-02-06T00:00:00Z', source_version='v6')
        self.assertEqual(self.read(source), before)

    def test_material_body_state_and_path_changes_still_revoke_approval(self):
        for changes in ({'body': 'Keep for seven days.'}, {'status': 'Superseded'}, {'paths': ['new/policy.py']}):
            with self.subTest(changes=changes):
                source = self.source()
                did = self.signed(source)
                with patch.object(cm, 'stamp', return_value='2026-03-01T00:00:00+00:00'):
                    changed = self.source(**changes)
                self.assertTrue(changed['changed'])
                self.assertNotEqual(changed['source']['source_version_id'], source['source']['source_version_id'])
                self.assertIn(did, changed['affected_decisions'])
                self.assertFalse(self.store.get_decision(did)['authorized'])
                self.assertTrue(self.store.get_decision(did)['needs_review'])
                self.assertEqual(changed['latest_observation']['observed_at'], '2026-03-01T00:00:00+00:00')

    def test_additive_migration_does_not_invent_recent_observation(self):
        source = self.source()
        versions = self.read(source)['versions']
        self.g.db.execute('ALTER TABLE source_records DROP COLUMN last_observed_at')
        self.g.db.execute('ALTER TABLE source_records DROP COLUMN newest_source_version')
        cm.migrate(self.g.db)
        cm.migrate(self.g.db)
        upgraded = self.read(source)
        self.assertEqual(upgraded['latest_observation']['observed_at'], '')
        self.assertEqual(upgraded['latest_observation']['source_version'], '')
        self.assertEqual(upgraded['versions'], versions)
        refreshed = self.source()
        self.assertTrue(refreshed['latest_observation']['observed_at'])
        self.assertFalse(refreshed['changed'])

    def test_lookup_does_not_echo_private_url_in_provider_version_metadata(self):
        source = self.source()
        self.source(source_version='//example.invalid/version?token=synthetic-test-value')
        result = self.store.lookup_record(repo=REPO, external_id='item-7', namespace='test-site')
        self.assertEqual(result['latest_observation']['source_version'], '')
        self.assertIn('source_version', result['latest_observation']['redacted_fields'])
        self.assertNotIn('synthetic-test-value', json.dumps(result))
        self.assertEqual(self.read(source)['versions'][0]['source_version'], '')
