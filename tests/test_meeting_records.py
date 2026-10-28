import hashlib
import hmac
import json
from unittest.mock import patch
from fixtures import OfflineCase
from bridge import meeting_records as mr
from bridge.meeting_providers import Vexa, OutcomeUnknown, verify_vexa_signature
from bridge.store import Invalid


class FakeVexa:
    provider = 'vexa'
    def __init__(self):
        self.calls = []
        self.fail = False
    def join(self, meeting, consent, options):
        self.calls.append('join')
        if self.fail:
            raise OutcomeUnknown('timeout')
        return {'status': 'requested', 'id': 122 + self.calls.count('join')}
    def stop(self, meeting):
        self.calls.append('stop')
        return {}
    def delete_remote(self, meeting):
        self.calls.append('delete')
        return {}
    def status(self, meeting):
        return {'meeting': {'id': meeting['remote_id'], 'status': 'completed'}} if meeting.get('remote_id') else {'running_bots': []}
    def transcript(self, meeting):
        return {'id': 123, 'segments': [{'segment_id': 'one', 'speaker': 'Ada', 'text': 'Use explicit consent.', 'start': 1, 'end': 2}]}


class Meetings(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store('meetings.db')
        self.addCleanup(self.store.graph.close)
        mr.migrate(self.store.graph.db)
        self.client = FakeVexa()
        self.meeting = mr.create(self.store, {'repo': 'acme/app', 'provider': 'vexa', 'namespace': 'selfhost-one',
            'platform': 'google_meet', 'native_meeting_id': 'abc-defg-hij', 'title': 'Planning'})
        self.mid = self.meeting['id']
    def consent(self):
        return mr.set_consent(self.store, self.mid, 'acme/app', {'join': True, 'transcribe': True,
            'record': False, 'notice_confirmed': True, 'by': 'Operator'})
    def imported(self):
        return mr.import_transcript(self.store, self.mid, 'acme/app', {'body': 'Private meeting evidence',
            'source_version': 'v1', 'workspace_shared': True, 'by': 'Operator'})
    def test_create_never_joins_and_scope_is_exact(self):
        self.assertEqual(self.meeting['state'], 'planned')
        self.assertEqual(self.client.calls, [])
        with self.assertRaises(Invalid):
            mr.get(self.store, self.mid, 'other/app')
        with self.assertRaises(Invalid):
            mr.command(self.store, self.mid, 'acme/app', 'join', client=self.client)
    def test_distinct_capture_permissions_and_no_blind_duplicate(self):
        self.consent()
        self.assertFalse(mr.get(self.store, self.mid, 'acme/app')['consent']['record'])
        self.client.fail = True
        result = mr.command(self.store, self.mid, 'acme/app', 'join', client=self.client)
        self.assertEqual(result['state'], 'unknown')
        with self.assertRaises(Invalid):
            mr.command(self.store, self.mid, 'acme/app', 'join', client=self.client)
        reconciled = mr.reconcile(self.store, self.mid, 'acme/app', client=self.client)
        self.assertEqual(reconciled['state'], 'unknown')
        self.assertEqual(reconciled['remote_state'], 'not_observed')
        self.assertEqual(self.client.calls, ['join'])
    def test_stop_and_delete_are_distinct(self):
        self.consent()
        mr.command(self.store, self.mid, 'acme/app', 'join', client=self.client)
        with self.assertRaises(Invalid):
            mr.command(self.store, self.mid, 'acme/app', 'delete_remote', client=self.client)
        result = mr.command(self.store, self.mid, 'acme/app', 'stop', client=self.client)
        self.assertEqual(result['state'], 'stopping')
        mr.reconcile(self.store, self.mid, 'acme/app', client=self.client)
        result = mr.command(self.store, self.mid, 'acme/app', 'delete_remote', client=self.client)
        self.assertTrue(result['revoked'])
        self.assertEqual(self.client.calls, ['join', 'stop', 'delete'])
    def test_import_requires_disclosure_and_is_canonical_idempotent(self):
        with self.assertRaises(Invalid):
            mr.import_transcript(self.store, self.mid, 'acme/app', {'body': 'secret', 'source_version': 'v1', 'by': 'Operator'})
        first, second = self.imported(), self.imported()
        self.assertFalse(second['changed'])
        self.assertEqual(first['source']['record_id'], second['source']['record_id'])
        self.assertEqual(self.store.graph.db.execute('SELECT count(*) n FROM source_records').fetchone()['n'], 1)
    def test_revoke_blocks_source_history_and_reimport_not_remote_stop(self):
        imported = self.imported()
        result = mr.revoke(self.store, self.mid, 'acme/app')
        self.assertTrue(result['revoked'])
        self.assertEqual(self.client.calls, [])
        with self.assertRaises(Invalid):
            self.store.get_record(imported['source']['record_id'], 'acme/app')
        with self.assertRaises(Invalid):
            self.imported()
    def test_sync_uses_source_version_and_never_silently_truncates(self):
        self.consent()
        mr.command(self.store, self.mid, 'acme/app', 'join', client=self.client)
        result = mr.sync_transcript(self.store, self.mid, 'acme/app', {'workspace_shared': True, 'by': 'Operator'}, client=self.client)
        self.assertTrue(result['source']['record_id'])
        with self.assertRaises(Invalid):
            mr.import_transcript(self.store, self.mid, 'acme/app', {'body': 'x'*1_000_001, 'source_version': 'v2', 'workspace_shared': True, 'by': 'Operator'})
    def test_revocation_invalidates_dependent_decision(self):
        imported = self.imported()
        task = self.store.add_run({'title': 'Implement planning', 'repo': 'acme/app'})['id']
        g = self.store.graph
        did = g.add_decision(task, 'Which planning rule?', 'policy', 'pending', repo='acme/app')
        g.publish_evidence(did, [imported['source']], status='resolved', source='record',
                           answer='Use explicit consent.', kind='evidence', signoff='required')
        mr.revoke(self.store, self.mid, 'acme/app')
        row = g.db.execute('SELECT needs_review FROM decisions WHERE id=?', (did,)).fetchone()
        self.assertEqual(row['needs_review'], 1)
    def test_intent_exists_before_remote_dispatch(self):
        self.consent()
        original = self.client.join
        def checked(meeting, consent, options):
            count = self.store.graph.db.execute("SELECT count(*) n FROM meeting_operations WHERE state='pending'").fetchone()['n']
            self.assertEqual(count, 1)
            return original(meeting, consent, options)
        self.client.join = checked
        result = mr.command(self.store, self.mid, 'acme/app', 'join', client=self.client)
        self.assertEqual(result['state'], 'requested')
    def test_signature_missing_or_invalid_timestamp(self):
        self.assertFalse(verify_vexa_signature(b'{}', {}, 'secret'))
        self.assertFalse(verify_vexa_signature(b'{}', {'X-Webhook-Timestamp': 'invalid'}, 'secret'))
    def test_adapter_contract_and_credentials_not_in_body(self):
        client = Vexa('http://localhost:18056', 'test-key')
        with patch.object(client, '_request', return_value={}) as request:
            client.join(self.meeting, {'transcribe': True, 'record': False})
            method, path, body = request.call_args.args
            self.assertEqual((method, path), ('POST', '/bots'))
            self.assertFalse(body['recording_enabled'])
            self.assertNotIn('test-key', json.dumps(body))
            client.stop(self.meeting)
            self.assertEqual(request.call_args.args, ('DELETE', '/bots/google_meet/abc-defg-hij'))
    def test_signature_replay_and_tamper_rejected(self):
        body, timestamp = b'{"event":"meeting.completed"}', '1000'
        signature = 'sha256=' + hmac.new(b'secret', b'1000.' + body, hashlib.sha256).hexdigest()
        headers = {'X-Webhook-Timestamp': timestamp, 'X-Webhook-Signature': signature}
        self.assertTrue(verify_vexa_signature(body, headers, 'secret', now=1001))
        self.assertFalse(verify_vexa_signature(body+b' ', headers, 'secret', now=1001))
        self.assertFalse(verify_vexa_signature(body, headers, 'secret', now=2000))

    def body_for(self, source):
        record = self.store.get_record(source['record_id'], 'acme/app')
        return record['versions'][-1]['snapshot']['body']

    def test_long_import_preserves_all_chars_and_idempotent_chunk_ids(self):
        body = '  ' + ('Long meeting evidence with unicode α and timestamps.\n' * 4000) + '\n '
        payload = {'body': body, 'source_version': 'export-1', 'workspace_shared': True, 'by': 'Operator', 'complete': True}
        first = mr.import_transcript(self.store, self.mid, 'acme/app', payload)
        self.assertGreater(len(body), 100000)
        self.assertGreater(len(first['sources']), 6)
        self.assertEqual(''.join(self.body_for(s) for s in first['sources']), body)
        self.assertTrue(first['manifest']['complete'])
        self.assertEqual(first['manifest']['characters'], len(body))
        second = mr.import_transcript(self.store, self.mid, 'acme/app', payload)
        self.assertEqual([s['record_id'] for s in first['sources']], [s['record_id'] for s in second['sources']])
        self.assertFalse(second['changed'])
        self.assertTrue(all(len(self.body_for(s)) <= mr.CHUNK_CHARS for s in first['sources']))

    def test_all_chunks_revoked_and_tail_dependent_invalidated(self):
        result = mr.import_transcript(self.store, self.mid, 'acme/app', {'body': 'x'*170000,
            'source_version': 'long-1', 'workspace_shared': True, 'by': 'Operator'})
        task = self.store.add_run({'title': 'Review last section', 'repo': 'acme/app'})['id']
        g = self.store.graph
        did = g.add_decision(task, 'What did the final speaker commit?', 'policy', 'pending', repo='acme/app')
        g.publish_evidence(did, [result['sources'][-1]], status='resolved', source='record',
            answer='Untrusted observation.', kind='evidence', signoff='required')
        mr.revoke(self.store, self.mid, 'acme/app')
        for source in result['sources']:
            with self.assertRaises(Invalid):
                self.store.get_record(source['record_id'], 'acme/app')
        self.assertEqual(g.db.execute('SELECT needs_review FROM decisions WHERE id=?', (did,)).fetchone()['needs_review'], 1)

    def test_shorter_revision_withholds_removed_chunks_and_restore_is_new_version(self):
        payload = {'body': 'x'*48000, 'source_version': 'v1', 'workspace_shared': True, 'by': 'Operator'}
        first = mr.import_transcript(self.store, self.mid, 'acme/app', payload)
        tail = first['sources'][-1]
        short = mr.import_transcript(self.store, self.mid, 'acme/app', dict(payload, body='x'*16000, source_version='v2'))
        self.assertEqual(short['manifest']['chunk_count'], 1)
        with self.assertRaises(Invalid):
            self.store.get_record(tail['record_id'], 'acme/app')
        restored = mr.import_transcript(self.store, self.mid, 'acme/app', dict(payload, source_version='v3'))
        self.assertEqual(restored['sources'][-1]['record_id'], tail['record_id'])
        self.assertNotEqual(restored['sources'][-1]['source_version_id'], tail['source_version_id'])
        self.assertEqual(self.body_for(restored['sources'][-1]), 'x'*16000)

    def test_segment_speaker_time_ids_and_completeness_retained(self):
        segments = [{'segment_id': 'speaker-window-' + str(i), 'speaker': 'Ada' if i % 2 else 'Lin',
            'start': i*2.0, 'end': i*2.0+1, 'completed': i != 1999, 'text': 'Evidence ' + 'x'*60} for i in range(2000)]
        result = mr.import_transcript(self.store, self.mid, 'acme/app', {'segments': segments, 'source_version': 'stream-1',
            'workspace_shared': True, 'by': 'Operator', 'complete': False})
        text = ''.join(self.body_for(s) for s in result['sources'])
        self.assertGreater(len(text), 100000)
        self.assertIn('[segment speaker-window-1999] [3998.0–3999.0s] [draft] Ada:', text)
        self.assertEqual(text.count('[segment speaker-window-'), 2000)
        self.assertFalse(result['manifest']['complete'])
        bad = segments + [segments[0]]
        with self.assertRaises(Invalid):
            mr.import_transcript(self.store, self.mid, 'acme/app', {'segments': bad, 'source_version': 'bad',
                'workspace_shared': True, 'by': 'Operator'})

    def test_recurring_room_is_distinct_occurrence_and_fresh_consent(self):
        first = self.imported()
        data = {'repo': 'acme/app', 'provider': 'vexa', 'namespace': 'selfhost-one', 'platform': 'google_meet',
                'native_meeting_id': 'abc-defg-hij', 'title': 'Next weekly planning', 'occurrence_id': 'weekly-2026-11-01'}
        other = mr.create(self.store, data)
        self.assertNotEqual(other['occurrence_id'], self.meeting['occurrence_id'])
        self.assertEqual(other['consent'], {})
        with self.assertRaises(Invalid):
            mr.create(self.store, data)
        second = mr.import_transcript(self.store, other['id'], 'acme/app', {'body': 'Second meeting', 'source_version': 'v1',
            'workspace_shared': True, 'by': 'Operator'})
        self.assertNotEqual(second['source']['record_id'], first['source']['record_id'])
        self.assertEqual(self.body_for(first['source']), 'Private meeting evidence')
        with self.assertRaises(Invalid):
            mr.command(self.store, other['id'], 'acme/app', 'join', client=self.client)

    def test_unresolved_room_blocks_overlap_but_completed_allows_new_occurrence(self):
        self.consent()
        mr.command(self.store, self.mid, 'acme/app', 'join', client=self.client)
        other = mr.create(self.store, {'repo': 'acme/app', 'provider': 'vexa', 'namespace': 'selfhost-one',
            'platform': 'google_meet', 'native_meeting_id': 'abc-defg-hij', 'title': 'Later'})
        mr.set_consent(self.store, other['id'], 'acme/app', {'join': True, 'transcribe': True, 'record': False,
            'notice_confirmed': True, 'by': 'Operator'})
        with self.assertRaises(Invalid):
            mr.command(self.store, other['id'], 'acme/app', 'join', client=self.client)
        mr.reconcile(self.store, self.mid, 'acme/app', client=self.client)
        result = mr.command(self.store, other['id'], 'acme/app', 'join', client=self.client)
        self.assertEqual(result['state'], 'requested')

    def test_sync_requires_exact_remote_occurrence_not_latest_room(self):
        with self.assertRaises(Invalid):
            mr.sync_transcript(self.store, self.mid, 'acme/app', {'workspace_shared': True, 'by': 'Operator'}, client=self.client)
        client = Vexa('http://localhost:18056', 'test-key')
        with patch.object(client, '_request', return_value={}) as request:
            client.transcript(dict(self.meeting, remote_id='123'))
            self.assertEqual(request.call_args.args, ('GET', '/transcripts/by-id/123'))

    def test_repeated_migration_retains_occurrences_and_chunks(self):
        first = self.imported()
        mr.migrate(self.store.graph.db)
        mr.migrate(self.store.graph.db)
        self.assertEqual(mr.get(self.store, self.mid, 'acme/app')['record_id'], first['source']['record_id'])
        self.assertEqual(len(mr.get(self.store, self.mid, 'acme/app')['chunks']), 1)

    def test_old_occurrence_stop_never_stops_new_room_session(self):
        self.consent()
        mr.command(self.store, self.mid, 'acme/app', 'join', client=self.client)
        mr.reconcile(self.store, self.mid, 'acme/app', client=self.client)
        before = list(self.client.calls)
        with self.assertRaises(Invalid):
            mr.command(self.store, self.mid, 'acme/app', 'stop', client=self.client)
        self.assertEqual(self.client.calls, before)

    def test_operational_metadata_survives_evidence_revocation(self):
        self.consent()
        mr.command(self.store, self.mid, 'acme/app', 'join', client=self.client)
        self.imported()
        mr.revoke(self.store, self.mid, 'acme/app')
        view = mr.operational_view(self.store, mr.get(self.store, self.mid, 'acme/app'))
        self.assertEqual(view['id'], self.mid)
        self.assertTrue(view['remote_stop_required'])
        self.assertTrue(view['evidence_withheld'])
        self.assertNotIn('record_id', view)
        self.assertNotIn('chunks', view)
        self.assertNotIn('Planning', json.dumps(view))
        self.assertEqual(mr.list_meetings(self.store, 'acme/app')['meetings'][0]['id'], self.mid)
        stopped = mr.command(self.store, self.mid, 'acme/app', 'stop', client=self.client)
        self.assertEqual(stopped['state'], 'stopping')

    def test_unknown_remote_delete_still_withholds_every_local_chunk(self):
        self.consent()
        mr.command(self.store, self.mid, 'acme/app', 'join', client=self.client)
        mr.reconcile(self.store, self.mid, 'acme/app', client=self.client)
        imported = mr.import_transcript(self.store, self.mid, 'acme/app', {'body': 'x'*100001,
            'source_version': 'long', 'workspace_shared': True, 'by': 'Operator'})
        def unavailable(meeting):
            raise OutcomeUnknown('timeout')
        self.client.delete_remote = unavailable
        result = mr.command(self.store, self.mid, 'acme/app', 'delete_remote', client=self.client)
        self.assertEqual(result['state'], 'unknown')
        self.assertTrue(result['revoked'])
        for source in imported['sources']:
            with self.assertRaises(Invalid):
                self.store.get_record(source['record_id'], 'acme/app')

    def test_initial_schema_migrates_without_losing_legacy_source_binding(self):
        source = self.imported()['source']
        db = self.store.graph.db
        db.executescript('''CREATE TABLE IF NOT EXISTS meeting_records (
            id TEXT PRIMARY KEY,repo TEXT,provider TEXT,namespace TEXT,platform TEXT,native_meeting_id TEXT,
            title TEXT,state TEXT,remote_id TEXT,remote_state TEXT,consent TEXT,record_id TEXT,revoked INTEGER,
            last_error TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE IF NOT EXISTS meeting_commands (id TEXT PRIMARY KEY,meeting_id TEXT,action TEXT,state TEXT,created_at TEXT,updated_at TEXT);''')
        db.execute("INSERT INTO meeting_records SELECT 'legacy-plan',repo,provider,namespace,platform,native_meeting_id,title,state,"
            "remote_id,remote_state,consent,record_id,revoked,last_error,created_at,updated_at FROM meeting_occurrences WHERE id=?", (self.mid,))
        db.execute("INSERT INTO meeting_commands VALUES('legacy-command','legacy-plan','join','unknown','then','then')")
        mr.migrate(db)
        mr.migrate(db)
        migrated = mr.get(self.store, 'legacy-plan', 'acme/app')
        self.assertEqual(migrated['occurrence_id'], 'legacy-plan')
        self.assertEqual(migrated['chunks'][0]['record_id'], source['record_id'])
        self.assertEqual(db.execute("SELECT count(*) n FROM meeting_operations WHERE id='legacy-command'").fetchone()['n'], 1)

    def test_failed_stop_cannot_reset_consent_or_rejoin(self):
        self.consent()
        mr.command(self.store, self.mid, 'acme/app', 'join', client=self.client)
        with patch.object(self.client, 'stop', side_effect=Invalid('403')):
            result = mr.command(self.store, self.mid, 'acme/app', 'stop', client=self.client)
        self.assertEqual(result['state'], 'requested')
        with self.assertRaises(Invalid):
            self.consent()
        with self.assertRaises(Invalid):
            mr.command(self.store, self.mid, 'acme/app', 'join', client=self.client)

    def test_room_lock_crosses_namespace_and_repo(self):
        self.consent()
        mr.command(self.store, self.mid, 'acme/app', 'join', client=self.client)
        second = mr.create(self.store, {'repo': 'other/app', 'provider': 'vexa', 'namespace': 'other',
            'platform': 'google_meet', 'native_meeting_id': 'abc-defg-hij', 'title': 'Other'})
        mr.set_consent(self.store, second['id'], 'other/app', {'join': True, 'transcribe': True,
            'record': False, 'notice_confirmed': True, 'by': 'Operator'})
        for action in ('join', 'stop'):
            with self.assertRaises(Invalid):
                mr.command(self.store, second['id'], 'other/app', action, client=self.client)
