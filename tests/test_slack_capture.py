"""Synthetic captured-message mutations; no Slack history or provider calls."""
import hashlib
import hmac
import json
import threading
import time
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fixtures import OfflineCase, ready_server
from bridge import canvas, context_memory as cm, proof, slack_capture as capture
from bridge.delivery import handle_slack_event
from bridge.store import Invalid, Store

REPO = 'synthetic/captured'
TS = '1700000000.000001'


class SyntheticSlack:
    name = 'slack'

    def __init__(self):
        self.messages = []

    def post_message(self, channel, text, blocks=None, thread_ts=''):
        self.messages.append({'channel': channel, 'text': text, 'thread_ts': thread_ts})
        return f'1800000000.{len(self.messages):06d}'


class CaptureCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'captures.db')
        self.g = self.store.graph
        self.addCleanup(self.g.close)
        self.slack = SyntheticSlack()
        self.delivery = self.store.connect_delivery(self.slack)
        self.g.set_setting('slack_team_id', 'TTEST')
        self.g.set_setting('slack_capture_repo', REPO)
        start_patch = patch.object(self.delivery.inbox, 'start')
        self.start = start_patch.start()
        self.addCleanup(start_patch.stop)

    def original(self, eid='original', ts=TS, **inner):
        return {'type': 'event_callback', 'team_id': 'TTEST', 'event_id': eid,
                'event': {'type': 'message', 'channel': 'CTEST', 'ts': ts,
                          'user': 'UWRITER', 'text': 'record: Keep for thirty days.', **inner}}

    def edit(self, eid='edit', ts=TS, at='1700000010.000001', text='record: Keep for seven days.', **inner):
        return {'type': 'event_callback', 'team_id': 'TTEST', 'event_id': eid,
                'event': {'type': 'message', 'subtype': 'message_changed', 'hidden': True,
                          'channel': 'CTEST', 'ts': at, 'event_ts': at,
                          'message': {'ts': ts, 'user': 'UWRITER', 'text': text,
                                      'edited': {'ts': at}}, **inner}}

    def delete(self, eid='delete', ts=TS, at='1700000020.000001', **inner):
        return {'type': 'event_callback', 'team_id': 'TTEST', 'event_id': eid,
                'event': {'type': 'message', 'subtype': 'message_deleted', 'hidden': True,
                          'channel': 'CTEST', 'ts': at, 'deleted_ts': ts, **inner}}

    def enqueue(self, event):
        return self.delivery.inbox.enqueue(event)

    def accept(self, event=None):
        event = event or self.original()
        self.enqueue(event)
        self.delivery.inbox.process()
        return self.record(ts=event['event']['ts'])

    def record(self, ts=TS, workspace='TTEST', channel='CTEST'):
        return dict(self.g.db.execute('SELECT s.*,i.body,i.title,i.status FROM source_records s '
            'JOIN intents i ON i.id=s.intent_id WHERE s.provider=? AND s.namespace=? AND s.external_id=?',
            ('slack', workspace, channel + ':' + ts)).fetchone())

    def pending(self):
        return [dict(r) for r in self.g.db.execute('SELECT * FROM slack_capture_mutations')]



class CapturedSlackTests(CaptureCase):
    def test_explicit_capture_is_canonical_and_receipt_is_atomic_with_import(self):
        self.enqueue(self.original())
        with patch.object(self.store, 'add_record', side_effect=RuntimeError('synthetic importer failure')):
            self.assertEqual(self.delivery.inbox.process(), 0)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM source_records').fetchone()[0], 0)
        self.assertIsNone(self.g.db.execute("SELECT * FROM webhook_receipts WHERE id='original'").fetchone())
        self.g.db.execute('UPDATE slack_ingress SET next_attempt=0')
        self.delivery.inbox.process()
        row = self.record()
        self.assertEqual((row['namespace'], row['external_id'], row['repo']), ('TTEST', 'CTEST:' + TS, REPO))
        self.assertEqual(row['newest_sequence'], 1700000000000001)
        self.assertEqual(self.g.db.execute("SELECT state FROM webhook_receipts WHERE id='original'").fetchone()[0], 'applied')
        self.assertEqual(len(self.slack.messages), 1)

    def test_import_that_raises_after_writing_rolls_back_source_enrollment_and_receipt(self):
        self.enqueue(self.original())
        actual = self.store.add_record
        def fail_after_write(data):
            actual(data)
            raise RuntimeError('after source write')
        with patch.object(self.store, 'add_record', side_effect=fail_after_write):
            self.delivery.inbox.process()
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM source_records').fetchone()[0], 0)
        self.assertEqual(self.g.db.execute('SELECT record_id FROM slack_captures').fetchone()[0], '')
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM webhook_receipts').fetchone()[0], 0)

    def test_nested_edit_identity_and_deleted_ts_are_distinct_from_event_clock(self):
        first = self.accept()
        self.enqueue(self.edit())
        self.delivery.inbox.process()
        edited = self.record()
        self.assertEqual(edited['id'], first['id'])
        self.assertEqual(edited['external_id'], 'CTEST:' + TS)
        self.assertEqual(edited['body'], 'Keep for seven days.')
        self.assertEqual(edited['newest_sequence'], 1700000010000001)
        self.enqueue(self.delete())
        self.delivery.inbox.process()
        removed = self.record()
        self.assertEqual(removed['id'], first['id'])
        self.assertEqual(removed['availability'], 'deleted')
        self.assertEqual(removed['status'], 'deleted')
        self.assertEqual(removed['head_sequence'], 3)
        with self.assertRaises(Invalid):
            self.store.get_record(first['id'], REPO)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM source_records').fetchone()[0], 1)

    def test_duplicate_original_message_and_mention_do_not_reimport_or_restore(self):
        first = self.accept()
        self.enqueue(self.delete())
        self.delivery.inbox.process()
        retired = self.record()
        self.enqueue(self.original('mention', type='app_mention'))
        self.enqueue(self.original())
        self.delivery.inbox.process()
        self.assertEqual(self.record(), retired)
        self.assertEqual(first['id'], retired['id'])
        self.assertEqual(len(self.slack.messages), 1)

    def test_unknown_edits_do_not_capture_even_with_record_marker_or_previous_message(self):
        event = self.edit(previous_message={'text': 'record: previously visible', 'ts': TS})
        self.assertEqual(self.enqueue(event)['ignored'], 'uncaptured_message')
        self.assertEqual(self.enqueue(self.delete())['ignored'], 'uncaptured_message')
        for table in ('slack_captures', 'slack_capture_mutations', 'slack_ingress', 'source_records', 'webhook_receipts'):
            self.assertEqual(self.g.db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
        # An ordinary accepted message is not an explicit capture enrollment.
        self.enqueue(self.original(text='ordinary conversation'))
        self.assertEqual(self.enqueue(event)['ignored'], 'uncaptured_message')

    def test_legacy_and_caller_imports_do_not_autoenroll(self):
        for meta in ({}, {'provider': 'slack', 'namespace': 'TTEST', 'external_id': 'CTEST:' + TS}):
            self.store.add_record({'repo': REPO, 'kind': 'slack', 'ref': 'CTEST:' + TS,
                                  'body': 'Caller supplied snapshot.', **meta})
        self.assertEqual(self.enqueue(self.edit())['ignored'], 'uncaptured_message')
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM slack_captures').fetchone()[0], 0)
        self.accept()  # A new explicit capture can enroll the canonical identity.
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM source_records').fetchone()[0], 2)
        self.assertEqual(self.record()['body'], 'Keep for thirty days.')
        self.assertEqual(self.g.db.execute("SELECT i.body FROM source_records s JOIN intents i ON i.id=s.intent_id WHERE s.provider='legacy'").fetchone()[0], 'Caller supplied snapshot.')

    def test_old_applied_capture_receipt_does_not_enroll_during_redelivery(self):
        self.g.db.execute("INSERT INTO webhook_receipts(id,channel,state) VALUES('original','slack','applied')")
        self.enqueue(self.original())
        self.delivery.inbox.process()
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM slack_captures').fetchone()[0], 0)
        self.assertEqual(self.enqueue(self.edit())['ignored'], 'uncaptured_message')

    def test_removing_record_marker_retires_source_and_later_marker_restores_same_identity(self):
        first = self.accept()
        self.enqueue(self.edit(text='Unrelated uncaptured private text', previous_message={'text': 'do not keep me'}))
        self.assertNotIn('Unrelated', json.dumps(self.pending()))
        self.assertNotIn('do not keep me', json.dumps(self.pending()))
        self.delivery.inbox.process()
        self.assertEqual(self.record()['availability'], 'deleted')
        self.assertEqual(self.record()['status'], 'capture_removed')
        self.enqueue(self.edit('restore', at='1700000030.000001'))
        self.delivery.inbox.process()
        self.assertEqual(self.record()['availability'], 'available')
        self.assertEqual(self.record()['id'], first['id'])

    def test_deleted_message_does_not_resurrect_from_later_edit(self):
        self.accept()
        self.enqueue(self.delete())
        self.enqueue(self.edit('impossible-later-edit', at='1700000030.000001'))
        self.delivery.inbox.process()
        self.assertEqual(self.record()['status'], 'deleted')
        before = self.record()
        self.enqueue(self.edit('another-impossible-edit', at='1700000040.000001'))
        self.delivery.inbox.process()
        self.assertEqual(self.record(), before)

    def test_order_is_lossless_above_float_precision_and_rejects_stale_or_tied_content(self):
        self.accept(self.original(ts='9000000000.000001'))
        self.enqueue(self.edit(ts='9000000000.000001', at='9000000000.000002'))
        self.delivery.inbox.process()
        first = self.record(ts='9000000000.000001')
        self.enqueue(self.edit('second', ts='9000000000.000001', at='9000000000.000003', text='record: Third microsecond.'))
        self.delivery.inbox.process()
        latest = self.record(ts='9000000000.000001')
        self.assertEqual(latest['newest_sequence'], first['newest_sequence'] + 1)
        for at in ('9000000000.000002', '9000000000.000003'):
            self.enqueue(self.edit('stale-' + at, ts='9000000000.000001', at=at, text='record: conflicting text'))
        self.delivery.inbox.process()
        self.assertEqual(self.record(ts='9000000000.000001'), latest)

    def test_malformed_clocks_do_not_borrow_original_edited_or_receipt_time(self):
        first = self.accept()
        invalid = (None, '', True, 1700000010.000001, 'NaN', 'Infinity', '1e12', '-1.000001',
                   '1700000010.1', '1700000010.0000001', '9223372036854.775808', TS)
        for value in invalid:
            with self.subTest(value=value):
                event = self.edit(at=value)
                event['event']['message']['edited']['ts'] = '1700000010.000001'
                self.assertIn('ignored', self.enqueue(event))
        event = self.edit()
        del event['event']['ts']; del event['event']['event_ts']
        self.assertIn('ignored', self.enqueue(event))
        self.delivery.inbox.process()
        self.assertEqual(self.record(), first)
        self.assertEqual(self.pending(), [])

    def test_oversized_mutation_is_rejected_whole_without_truncation_or_storage(self):
        first = self.accept()
        self.assertEqual(self.enqueue(self.edit(text='record: ' + 'x' * capture.MAX_TEXT))['ignored'], 'invalid_capture_mutation')
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.record(), first)

    def test_pending_before_capture_survives_restart_and_original_job_exhaustion(self):
        for mutation in (self.edit(), self.delete()):
            with self.subTest(subtype=mutation['event']['subtype']):
                ts = '1700000000.' + ('000011' if mutation['event']['subtype'] == 'message_changed' else '000012')
                first_id, second_id = 'first-' + ts, 'second-' + ts
                self.enqueue(self.original(first_id, ts=ts))
                self.enqueue(self.original(second_id, ts=ts, type='app_mention'))
                if 'message' in mutation['event']:
                    mutation['event']['message']['ts'] = ts
                else:
                    mutation['event']['deleted_ts'] = ts
                self.enqueue(mutation)
                with patch.object(self.store, 'add_record', side_effect=RuntimeError('retry exhaustion')):
                    for _ in range(5):
                        self.g.db.execute('UPDATE slack_ingress SET next_attempt=0')
                        self.delivery.inbox.process(limit=1)
                self.assertEqual(self.g.db.execute('SELECT state FROM slack_ingress WHERE id=?', (first_id,)).fetchone()[0], 'failed')
                self.assertTrue(self.pending())
                other = Store(self.store.path)
                self.addCleanup(other.graph.close)
                worker = other.connect_delivery(self.slack)
                worker.inbox.process()
                current = self.record(ts=ts)
                self.assertEqual(current['availability'], 'deleted' if mutation['event']['subtype'] == 'message_deleted' else 'available')
                if current['availability'] == 'available':
                    self.assertEqual(current['body'], 'Keep for seven days.')
                self.assertEqual(self.pending(), [])

    def test_pending_mutation_survives_failed_original_without_another_job_until_operator_retry(self):
        self.enqueue(self.original())
        self.enqueue(self.delete())
        with patch.object(self.store, 'add_record', side_effect=RuntimeError('import failed')):
            for _ in range(5):
                self.g.db.execute('UPDATE slack_ingress SET next_attempt=0')
                self.delivery.inbox.process()
        self.assertEqual(len(self.pending()), 1)
        self.delivery.retry_inbound('original')
        self.delivery.inbox.process()
        self.assertEqual(self.record()['availability'], 'deleted')
        self.assertEqual(self.pending(), [])

    def test_mutation_failure_is_durable_atomic_and_retried_after_restart(self):
        first = self.accept()
        self.enqueue(self.edit())
        actual = self.store.add_record
        def fail(data):
            actual(data)
            raise RuntimeError('after mutation source write')
        with patch.object(self.store, 'add_record', side_effect=fail):
            self.delivery.inbox.process()
        self.assertEqual(self.record(), first)
        self.assertIsNone(self.g.db.execute("SELECT * FROM webhook_receipts WHERE id='edit'").fetchone())
        self.assertEqual(self.pending()[0]['attempts'], 1)
        self.assertTrue(any(r['id'] == 'edit' for r in self.delivery.inbound_failed()))
        other = Store(self.store.path)
        self.addCleanup(other.graph.close)
        other.graph.db.execute('UPDATE slack_capture_mutations SET next_attempt=0')
        other.connect_delivery(self.slack).inbox.process()
        self.assertEqual(self.record()['body'], 'Keep for seven days.')
        self.assertEqual(self.pending(), [])

    def test_queue_coalesces_newest_in_any_order_and_applies_a_bounded_batch(self):
        self.accept()
        for i in (2, 5, 1, 4, 3):
            self.enqueue(self.edit(str(i), at=f'1700000010.{i:06d}', text=f'record: update {i}'))
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.pending()[0]['clock'], 1700000010000005)
        self.delivery.inbox.process()
        self.assertEqual(self.record()['body'], 'update 5')
        for i in range(3):
            ts = f'1700000000.{i + 10:06d}'
            self.accept(self.original('original-' + ts, ts=ts))
            self.enqueue(self.edit('edit-' + ts, ts=ts))
        # accept() processes older pending entries, so enqueue all again.
        for i in range(3):
            self.enqueue(self.edit('next-' + str(i), ts=f'1700000000.{i + 10:06d}', at='1700000020.000001'))
        self.assertEqual(capture.process(self.delivery, limit=1), 1)
        self.assertEqual(len(self.pending()), 2)

    def test_queue_capacity_never_evicts_an_accepted_update_and_allows_same_identity_replacement(self):
        self.enqueue(self.original())
        self.enqueue(self.original('second', ts='1700000000.000002'))
        with patch.object(capture, 'MAX_PENDING', 1):
            self.enqueue(self.edit())
            self.enqueue(self.edit('newer', at='1700000011.000001'))
            with self.assertRaises(capture.CaptureQueueFull):
                self.enqueue(self.delete(ts='1700000000.000002'))
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.pending()[0]['event_id'], 'newer')
        self.delivery.inbox.process()
        self.enqueue(self.delete(ts='1700000000.000002'))
        self.delivery.inbox.process()
        self.assertEqual(self.record(ts='1700000000.000002')['availability'], 'deleted')

    def test_workspace_channel_and_accepted_repo_are_bound_to_capture_identity(self):
        first = self.accept()
        foreign = self.edit(); foreign['team_id'] = 'TOTHER'
        self.assertEqual(self.enqueue(foreign)['ignored'], 'workspace_mismatch')
        self.assertEqual(self.enqueue(self.edit(channel='COTHER'))['ignored'], 'uncaptured_message')
        self.g.set_setting('slack_capture_repo', 'other/repo')
        self.enqueue(self.edit())
        self.delivery.inbox.process()
        self.assertEqual(self.record()['repo'], REPO)
        self.assertEqual(self.record()['id'], first['id'])

    def test_authenticated_event_workspace_is_carried_past_mutable_setting_change(self):
        def authorize_then_change(delivery, event):
            self.assertEqual(event['team_id'], 'TTEST')
            self.g.set_setting('slack_team_id', 'TOTHER')
            return True
        with patch('bridge.slack_events.accepts_workspace', side_effect=authorize_then_change):
            handle_slack_event(self.delivery, self.original())
        self.assertEqual(self.record()['namespace'], 'TTEST')
        self.assertEqual(self.g.db.execute("SELECT count(*) FROM source_records WHERE namespace='TOTHER'").fetchone()[0], 0)

    def test_pending_workspace_mismatch_is_retained_and_applied_only_after_reconnect(self):
        self.accept()
        self.enqueue(self.edit())
        self.g.set_setting('slack_team_id', 'TOTHER')
        self.delivery.inbox.process()
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.record()['body'], 'Keep for thirty days.')
        self.g.set_setting('slack_team_id', 'TTEST')
        self.delivery.inbox.process()
        self.assertEqual(self.record()['body'], 'Keep for seven days.')

    def test_duplicate_event_id_cannot_enroll_a_different_message(self):
        self.enqueue(self.original())
        self.enqueue(self.original(ts='1700000000.000099'))
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM slack_captures').fetchone()[0], 1)
        self.assertEqual(self.enqueue(self.edit(ts='1700000000.000099'))['ignored'], 'uncaptured_message')

    def test_reply_failure_after_import_recovers_receipt_without_second_version(self):
        self.enqueue(self.original())
        with patch.object(self.delivery.inbox, 'ack', side_effect=RuntimeError('crash before reply enqueue')):
            self.delivery.inbox.process()
        first = self.record()
        self.assertEqual(len(self.slack.messages), 0)
        self.g.db.execute('UPDATE slack_ingress SET next_attempt=0')
        self.delivery.inbox.process()
        self.assertEqual(self.record(), first)
        self.assertEqual(len(self.slack.messages), 1)
        self.assertIn('Recorded as decision record', self.slack.messages[0]['text'])

    def test_accepted_original_survives_temporary_workspace_disconnect(self):
        self.enqueue(self.original())
        self.enqueue(self.delete())
        self.g.set_setting('slack_team_id', 'TOTHER')
        self.delivery.inbox.process()
        row = self.g.db.execute("SELECT * FROM slack_ingress WHERE id='original'").fetchone()
        self.assertEqual(row['state'], 'queued')
        self.assertNotEqual(row['payload'], '{}')
        self.g.set_setting('slack_team_id', 'TTEST')
        self.g.db.execute('UPDATE slack_ingress SET next_attempt=0')
        self.delivery.inbox.process()
        self.assertEqual(self.record()['availability'], 'deleted')

    def test_disconnected_workspace_does_not_starve_other_workspace_pending_updates(self):
        self.accept()
        self.enqueue(self.edit())
        self.g.set_setting('slack_team_id', 'TOTHER')
        other = self.original('other-original'); other['team_id'] = 'TOTHER'
        self.enqueue(other)
        self.delivery.inbox.process()
        event = self.edit('other-edit'); event['team_id'] = 'TOTHER'
        self.enqueue(event)
        self.assertEqual(capture.process(self.delivery, limit=1), 1)
        self.assertEqual(self.record(workspace='TOTHER')['body'], 'Keep for seven days.')
        self.assertEqual(self.record()['body'], 'Keep for thirty days.')
        self.assertEqual(len(self.pending()), 1)

    def test_two_mutation_workers_commit_one_version_and_receipt(self):
        self.accept()
        self.enqueue(self.edit())
        other = Store(self.store.path)
        self.addCleanup(other.graph.close)
        worker = other.connect_delivery(self.slack)
        gate = threading.Barrier(2)
        results = []
        def run(delivery):
            try:
                gate.wait(timeout=3)
                results.append(capture.process(delivery))
            finally:
                delivery.store.graph.close_thread()
        threads = [threading.Thread(target=run, args=(d,)) for d in (self.delivery, worker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.record()['head_sequence'], 2)
        self.assertEqual(self.g.db.execute("SELECT count(*) FROM webhook_receipts WHERE id='edit'").fetchone()[0], 1)

    def test_restarted_background_worker_drains_accepted_mutation_without_new_callback(self):
        self.accept()
        self.enqueue(self.edit())
        other = Store(self.store.path)
        self.addCleanup(other.graph.close)
        worker = other.connect_delivery(self.slack)
        self.addCleanup(worker.close)
        worker.start(interval=.05)
        deadline = time.monotonic() + 3
        while self.pending() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.record()['body'], 'Keep for seven days.')

    def test_operator_retry_clears_mutation_backoff(self):
        self.accept()
        self.enqueue(self.edit())
        with patch.object(self.store, 'add_record', side_effect=RuntimeError('synthetic transient failure')):
            self.delivery.inbox.process()
        self.assertGreater(self.pending()[0]['next_attempt'], time.time())
        self.assertEqual(self.delivery.retry_inbound('edit')['state'], 'queued')
        self.delivery.inbox.process()
        self.assertEqual(self.record()['body'], 'Keep for seven days.')

    def signed(self, record, title):
        self.store.add_owner({'name': 'Ada Example', 'team': 'Policy', 'patterns': 'policy/*'})
        task = self.store.add_run({'title': title, 'repo': REPO})['id']
        row = dict(self.g.db.execute('SELECT i.*,s.id AS record_id,s.head_id AS source_version_id FROM intents i '
            'JOIN source_records s ON s.intent_id=i.id WHERE s.id=?', (record['id'],)).fetchone())
        did = self.g.add_decision(task, 'How long should records remain?', 'policy', 'pending',
                                  repo=REPO, owner='Ada Example', path='policy/retention.py')
        self.g.publish_evidence(did, [row], source='record', status='resolved', kind='evidence',
                                answer='Keep for thirty days.', signoff='required')
        canvas.sign_off(self.store, did, {'by': 'Ada Example', 'expected_updated_at': self.store.get_decision(did)['updated_at']})
        return task, did

    def test_agent_cannot_pin_a_pending_capture_and_noop_recovery_stays_unsigned(self):
        source = self.accept()
        task = self.store.add_run({'title': 'Review a captured fact', 'repo': REPO})['id']
        did = self.g.add_decision(task, 'How long should records remain?', 'policy', 'pending',
                                  repo=REPO, path='policy/retention.py')
        pin = {'record_id': source['id'], 'source_version_id': source['head_id'], 'role': 'support'}
        args = {'task_id': task, 'node_id': did, 'answer': 'Keep for thirty days.',
                'source_evidence': [pin]}
        self.enqueue(self.edit(text='record: Keep for thirty days.'))
        before = self.store.get_decision(did)
        with self.assertRaisesRegex(Invalid, 'fresh|unavailable'):
            canvas.settle_node(self.store, args)
        self.assertEqual(self.store.get_decision(did), before)
        self.delivery.inbox.process()
        self.assertEqual(self.record()['head_id'], source['head_id'])
        result = canvas.settle_node(self.store, args)
        self.assertFalse(result['authorized'])
        self.assertEqual(result['signoff'], 'required')
        self.assertEqual([{k: edge[k] for k in pin} for edge in result['sources']], [pin])

    def test_metadata_only_edit_preserves_evidence_and_material_edit_invalidates_with_immutable_proof(self):
        first = self.accept()
        historical_task, historical = self.signed(first, 'Completed retention change')
        self.store.update_run(historical_task, {'status': 'completed'})
        bundle = proof.create(self.store, historical_task, 'diff --git a/policy/a b/policy/a\n+retention\n')
        task, active = self.signed(first, 'Active retention change')
        history = cm.history(self.g.db, historical)
        signatures = self.store.get_decision(active)['signatures']
        version = dict(self.g.db.execute('SELECT * FROM source_versions WHERE id=?', (first['head_id'],)).fetchone())
        self.enqueue(self.edit(text='record: Keep for thirty days.'))
        self.delivery.inbox.process()
        identical = self.record()
        self.assertEqual(identical['head_id'], first['head_id'])
        self.assertNotEqual(identical['newest_sequence'], first['newest_sequence'])
        self.assertEqual(self.store.get_decision(active)['signatures'], signatures)
        self.assertTrue(self.store.get_decision(active)['authorized'])
        self.enqueue(self.edit('material', at='1700000011.000001'))
        self.delivery.inbox.process()
        self.assertFalse(self.store.get_decision(active)['authorized'])
        self.assertTrue(self.store.get_decision(active)['needs_review'])
        self.assertEqual(cm.history(self.g.db, historical)[:len(history)], history)
        self.assertEqual(proof.export(self.store, {'task_id': historical_task})['bundle'], bundle)
        self.assertTrue(proof.export(self.store, {'task_id': historical_task})['stale'])
        self.assertEqual(dict(self.g.db.execute('SELECT * FROM source_versions WHERE id=?', (first['head_id'],)).fetchone()), version)

    def test_deletion_invalidates_active_source_support(self):
        first = self.accept()
        _, did = self.signed(first, 'Active deletion check')
        self.enqueue(self.delete())
        self.delivery.inbox.process()
        self.assertFalse(self.store.get_decision(did)['authorized'])
        self.assertTrue(self.store.get_decision(did)['needs_review'])


class CapturedSlackFreshnessTests(CaptureCase):
    signed = CapturedSlackTests.signed

    def assert_uncertain(self, task, did, source, store=None):
        from bridge.context_connectors import fresh
        store = store or self.store
        graph = store.graph
        current = store.get_decision(did)
        self.assertFalse(current['authorized'])
        self.assertTrue(current['source_refresh_required'])
        self.assertFalse(fresh(graph.db, source['id']))
        self.assertFalse(cm.citation(graph.db, source['id'], source['head_id'])['current'])
        self.assertTrue(graph.source_review_reason(did))
        self.assertTrue(graph.blocking_nodes(task))
        self.assertEqual(graph.linked_answers([source['id']], REPO), [])
        self.assertEqual(graph.memory_search('How long should records remain?', repo=REPO), [])
        self.assertEqual(graph._intent_corpus(REPO)[0], [])
        self.assertFalse(current['source_revalidation']['available'])
        with self.assertRaises(Invalid):
            store.get_record(source['id'], REPO)
        with self.assertRaises(Invalid):
            cm.check_current(graph.db, did)
        with self.assertRaises(Invalid):
            canvas.sign_off(store, did, {'by': 'Ada Example', 'expected_updated_at': current['updated_at']})
        with self.assertRaises(Invalid):
            canvas.finish_task(store, {'task_id': task})

    def test_accepted_edit_blocks_source_reuse_signoff_and_actual_finish_before_processing(self):
        source = self.accept()
        task, did = self.signed(source, 'Queued edit freshness')
        raw = dict(self.g.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone())
        history = cm.history(self.g.db, did)
        self.assertTrue(self.g.memory_search('How long should records remain?', repo=REPO))
        self.assertTrue(self.g._intent_corpus(REPO)[0])  # Prime same-process caches.
        self.enqueue(self.edit())
        self.assert_uncertain(task, did, source)
        self.enqueue(self.edit('older-pending', at='1700000009.000001'))
        self.enqueue(self.edit())
        self.assert_uncertain(task, did, source)
        self.assertEqual(dict(self.g.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()), raw)
        self.assertEqual(cm.history(self.g.db, did), history)
        self.assertEqual(self.record()['head_id'], source['head_id'])
        self.delivery.inbox.process()
        self.assertFalse(self.store.get_decision(did)['authorized'])
        self.assertTrue(self.store.get_decision(did)['needs_review'])
        self.assertNotEqual(self.record()['head_id'], source['head_id'])

    def test_accepted_delete_blocks_before_tombstone_import_and_remains_unavailable_after(self):
        source = self.accept()
        task, did = self.signed(source, 'Queued deletion freshness')
        self.enqueue(self.delete())
        self.assert_uncertain(task, did, source)
        self.assertEqual(self.record()['availability'], 'available')  # Immutable accepted snapshot stays intact.
        self.delivery.inbox.process()
        self.assertEqual(self.record()['availability'], 'deleted')
        self.assertFalse(self.store.get_decision(did)['authorized'])
        with self.assertRaises(Invalid):
            canvas.finish_task(self.store, {'task_id': task})

    def test_noop_recovery_after_backoff_and_restart_restores_existing_approval_without_new_version(self):
        source = self.accept()
        task, did = self.signed(source, 'No-op recovery freshness')
        before = self.store.get_decision(did)
        history = cm.history(self.g.db, did)
        versions = [dict(v) for v in self.g.db.execute('SELECT * FROM source_versions')]
        self.enqueue(self.edit(text='record: Keep for thirty days.'))
        with patch.object(self.store, 'add_record', side_effect=RuntimeError('synthetic importer downtime')):
            self.delivery.inbox.process()
        self.assertGreater(self.pending()[0]['next_attempt'], time.time())
        self.assert_uncertain(task, did, source)
        other = Store(self.store.path)
        self.addCleanup(other.graph.close)
        worker = other.connect_delivery(self.slack)
        worker.inbox.process()  # Persisted backoff has not elapsed.
        self.assert_uncertain(task, did, source, other)
        other.graph.db.execute('UPDATE slack_capture_mutations SET next_attempt=0')
        worker.inbox.process()
        restored = other.get_decision(did)
        self.assertTrue(restored['authorized'])
        self.assertTrue(other.graph._intent_corpus(REPO)[0])
        self.assertTrue(self.g._intent_corpus(REPO)[0])
        self.assertEqual(restored['signatures'], before['signatures'])
        self.assertEqual(cm.history(other.graph.db, did), history)
        self.assertEqual([dict(v) for v in other.graph.db.execute('SELECT * FROM source_versions')], versions)
        self.assertTrue(other.get_record(source['id'], REPO)['source']['current'])
        self.assertEqual(other.graph.source_review_reason(did), '')
        self.assertTrue(other.graph.memory_search('How long should records remain?', repo=REPO))
        self.assertEqual(other.update_run(task, {'status': 'completed'})['status'], 'completed')

    def test_pending_delete_remains_blocked_during_backoff_and_after_restart(self):
        source = self.accept()
        task, did = self.signed(source, 'Deletion retry freshness')
        self.enqueue(self.delete())
        with patch.object(self.store, 'add_record', side_effect=RuntimeError('synthetic importer downtime')):
            self.delivery.inbox.process()
        self.assert_uncertain(task, did, source)
        other = Store(self.store.path)
        self.addCleanup(other.graph.close)
        worker = other.connect_delivery(self.slack)
        worker.inbox.process()
        self.assert_uncertain(task, did, source, other)
        other.graph.db.execute('UPDATE slack_capture_mutations SET next_attempt=0')
        worker.inbox.process()
        self.assertEqual(self.record()['availability'], 'deleted')
        self.assertFalse(other.get_decision(did)['authorized'])
        with self.assertRaises(Invalid):
            canvas.finish_task(other, {'task_id': task})

    def test_pending_source_blocks_through_completed_intermediate_and_preserves_proofs(self):
        source = self.accept()
        historical_task, historical = self.signed(source, 'Historical source approval')
        self.store.update_run(historical_task, {'status': 'completed'})
        historical_bundle = proof.create(self.store, historical_task, 'diff --git a/policy/a b/policy/a\n+history\n')
        def dependent(parent, title):
            task = self.store.add_run({'title': title, 'repo': REPO})['id']
            did = self.g.add_decision(task, 'How long should records remain?', 'policy', 'pending',
                                      repo=REPO, owner='Ada Example', path='policy/retention.py')
            self.store.answer(did, {'answer': 'Keep for thirty days.'})
            with self.g.transaction():
                self.g.add_link(did, parent, 'depends')
            canvas.sign_off(self.store, did, {'by': 'Ada Example', 'expected_updated_at': self.store.get_decision(did)['updated_at']})
            return task, did
        middle_task, middle = dependent(historical, 'Completed intermediate')
        self.store.update_run(middle_task, {'status': 'completed'})
        middle_bundle = proof.create(self.store, middle_task, 'diff --git a/policy/b b/policy/b\n+middle\n')
        active_task, active = dependent(middle, 'Active consumer')
        # Freeze after the dependency graph is complete; adding those links is
        # itself visible in the historical task's live comparison.
        historical_bundle = proof.create(self.store, historical_task, 'diff --git a/policy/a b/policy/a\n+history\n')
        middle_bundle = proof.create(self.store, middle_task, 'diff --git a/policy/b b/policy/b\n+middle\n')
        self.assertFalse(proof.export(self.store, {'task_id': historical_task})['stale'])
        self.assertFalse(proof.export(self.store, {'task_id': middle_task})['stale'])
        before = {did: dict(self.g.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone())
                  for did in (historical, middle, active)}
        histories = {did: cm.history(self.g.db, did) for did in before}
        self.enqueue(self.edit(text='record: Keep for thirty days.'))
        self.assert_uncertain(active_task, active, source)
        for task, bundle in ((historical_task, historical_bundle), (middle_task, middle_bundle)):
            exported = proof.export(self.store, {'task_id': task})
            self.assertEqual(exported['bundle'], bundle)
            self.assertTrue(exported['stale'])
            self.assertTrue(exported['integrity']['valid'])
        for did in before:
            self.assertEqual(dict(self.g.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()), before[did])
            self.assertEqual(cm.history(self.g.db, did), histories[did])
        self.delivery.inbox.process()
        self.assertTrue(self.store.get_decision(active)['authorized'])
        self.assertFalse(proof.export(self.store, {'task_id': historical_task})['stale'])
        self.assertFalse(proof.export(self.store, {'task_id': middle_task})['stale'])
        for did in before:
            self.assertEqual(dict(self.g.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()), before[did])
            self.assertEqual(cm.history(self.g.db, did), histories[did])
        self.store.update_run(active_task, {'status': 'completed'})

    def test_older_duplicate_and_unknown_callbacks_do_not_create_uncertainty(self):
        source = self.accept()
        task, did = self.signed(source, 'No spurious uncertainty')
        self.enqueue(self.edit(text='record: Keep for thirty days.'))
        self.delivery.inbox.process()
        for event in (self.edit(), self.edit('older', at='1700000009.000001'),
                      self.edit('unknown', ts='1700000000.000099'), self.edit('bad-clock', at='invalid')):
            self.assertIn('ignored', self.enqueue(event))
            self.assertEqual(self.pending(), [])
            self.assertTrue(self.store.get_decision(did)['authorized'])
            self.assertTrue(self.store.get_record(source['id'], REPO)['source']['current'])
        self.assertEqual(self.record()['head_id'], source['head_id'])
        self.store.update_run(task, {'status': 'completed'})

    def test_rolled_back_queue_acceptance_does_not_leave_a_freshness_barrier(self):
        source = self.accept()
        task, did = self.signed(source, 'Atomic acceptance uncertainty')
        with self.assertRaises(RuntimeError):
            with self.g.transaction():
                self.enqueue(self.edit())
                self.assertFalse(self.store.graph.source_review_reason(did) == '')
                raise RuntimeError('roll back callback acceptance')
        self.assertEqual(self.pending(), [])
        self.assertTrue(self.store.get_decision(did)['authorized'])
        self.assertTrue(self.store.get_record(source['id'], REPO)['source']['current'])
        self.store.update_run(task, {'status': 'completed'})

    def test_accepted_pending_mutation_blocks_finish_writer_after_other_connection_commits(self):
        source = self.accept()
        task, did = self.signed(source, 'Concurrent pending freshness')
        other = Store(self.store.path)
        self.addCleanup(other.graph.close)
        # Prime the other store's readers before this connection accepts a callback.
        self.assertTrue(other.get_decision(did)['authorized'])
        self.assertTrue(other.graph._intent_corpus(REPO)[0])
        accepted = threading.Event()
        release = threading.Event()
        def accept_while_locked():
            try:
                with self.g.transaction():
                    self.enqueue(self.edit())
                    accepted.set()
                    release.wait(3)
            finally:
                self.g.close_thread()
        writer = threading.Thread(target=accept_while_locked)
        writer.start()
        self.assertTrue(accepted.wait(2))
        outcomes = []
        def finish():
            try:
                other.update_run(task, {'status': 'completed'})
                outcomes.append('finished')
            except Invalid:
                outcomes.append('blocked')
            finally:
                other.graph.close_thread()
        finisher = threading.Thread(target=finish)
        finisher.start()
        release.set()
        writer.join(4); finisher.join(4)
        self.assertFalse(writer.is_alive())
        self.assertFalse(finisher.is_alive())
        self.assertEqual(outcomes, ['blocked'])
        self.assert_uncertain(task, did, source, other)


class CapturedSlackHTTPTests(CaptureCase):
    def setUp(self):
        super().setUp()
        self.secret = 'synthetic-slack-signing-secret'
        self.server = ready_server(self.store, port=0, slack_signing_secret=self.secret)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), self.thread.join()))

    def callback(self, event, signature=True):
        body = json.dumps(event).encode()
        at = str(int(time.time()))
        digest = hmac.new(self.secret.encode(), b'v0:' + at.encode() + b':' + body, hashlib.sha256).hexdigest()
        request = Request(f'http://127.0.0.1:{self.server.server_port}/webhooks/slack', data=body,
            headers={'Content-Type': 'application/json', 'X-Slack-Request-Timestamp': at,
                     'X-Slack-Signature': 'v0=' + digest if signature else 'v0=invalid'})
        try:
            with urlopen(request) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_signed_callback_tracks_only_explicit_capture_and_ignores_wrong_signature(self):
        self.assertEqual(self.callback(self.original(), signature=False)[0], 401)
        self.assertEqual(self.callback(self.edit())[1]['ignored'], 'uncaptured_message')
        self.assertEqual(self.callback(self.original())[0], 200)
        self.assertEqual(self.callback(self.edit())[0], 200)
        self.assertEqual(self.g.db.execute('SELECT count(*) FROM source_records').fetchone()[0], 0)
        self.delivery.inbox.process()
        self.assertEqual(self.record()['body'], 'Keep for seven days.')
        self.assertEqual(self.callback(self.delete())[0], 200)
        self.delivery.inbox.process()
        self.assertEqual(self.record()['availability'], 'deleted')

    def test_full_mutation_queue_returns_retryable_http_error_without_losing_first_callback(self):
        self.callback(self.original())
        self.callback(self.original('second', ts='1700000000.000002'))
        with patch.object(capture, 'MAX_PENDING', 1):
            self.assertEqual(self.callback(self.edit())[0], 200)
            self.assertEqual(self.callback(self.delete(ts='1700000000.000002'))[0], 503)
        self.assertEqual(len(self.pending()), 1)
        self.delivery.inbox.process()
        self.assertEqual(self.callback(self.delete(ts='1700000000.000002'))[0], 200)
        self.delivery.inbox.process()
        self.assertEqual(self.record(ts='1700000000.000002')['availability'], 'deleted')


if __name__ == '__main__':
    import unittest
    unittest.main()
