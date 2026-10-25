from unittest.mock import patch
from test_meeting_records import FakeVexa
from fixtures import OfflineCase
from bridge import meeting_records as mr, meeting_worker as mw
from bridge.auth import OPERATOR, Identity
from bridge.store import Invalid


class Observer(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store('observer.db')
        self.addCleanup(self.store.graph.close)
        mr.migrate(self.store.graph.db)
        self.client = FakeVexa()
        self.mid = mr.create(self.store, {'repo': 'acme/app', 'provider': 'vexa', 'namespace': 'selfhost-one',
            'platform': 'google_meet', 'native_meeting_id': 'abc-defg-hij', 'title': 'Planning'})['id']
        mw.migrate(self.store.graph.db)
        self.consent()
        self.t = 1000
        self.worker = mw.Worker(self.store, client_factory=lambda: self.client, clock=lambda: self.t, allow_operator=True)

    def consent(self):
        return mr.set_consent(self.store, self.mid, 'acme/app', {'join': True, 'transcribe': True,
            'record': False, 'notice_confirmed': True, 'by': 'Operator'})

    def enable(self, actor=OPERATOR):
        return mw.configure_sync(self.store, self.mid, 'acme/app', {'enabled': True, 'workspace_shared': True}, actor)

    def joined(self):
        mr.command(self.store, self.mid, 'acme/app', 'join', client=self.client)
        self.enable()

    def test_background_never_joins(self):
        self.enable()
        self.worker.tick()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(mw.status(self.store, self.mid)['status'], 'waiting_for_occurrence')

    def test_disclosure_and_transcription_required(self):
        with self.assertRaises(Invalid):
            mw.configure_sync(self.store, self.mid, 'acme/app', {'enabled': True}, OPERATOR)
        mr.set_consent(self.store, self.mid, 'acme/app', {'join': True, 'transcribe': False,
            'record': False, 'notice_confirmed': True, 'by': 'Operator'})
        with self.assertRaises(Invalid):
            self.enable()

    def test_observe_imports_and_truthfully_waits_for_complete(self):
        self.joined()
        self.worker.tick()
        self.assertTrue(mr.get(self.store, self.mid, 'acme/app')['record_id'])
        self.assertFalse(mr.get(self.store, self.mid, 'acme/app')['transcript_complete'])
        self.t += 60
        original = self.client.transcript
        with patch.object(self.client, 'transcript', side_effect=lambda m: dict(original(m), status='completed')):
            self.worker.tick()
        self.assertEqual(mw.status(self.store, self.mid)['status'], 'complete')
        self.assertFalse(mw.status(self.store, self.mid)['enabled'])
        self.assertEqual(self.client.calls, ['join'])

    def test_inflight_revoke_and_disable_fence_import(self):
        for action in ('disable', 'revoke'):
            self.joined() if action == 'disable' else self.enable()
            original = self.client.transcript
            def race(m):
                if action == 'disable':
                    mw.configure_sync(self.store, self.mid, 'acme/app', {'enabled': False}, OPERATOR)
                else:
                    mr.revoke(self.store, self.mid, 'acme/app')
                return original(m)
            with patch.object(self.client, 'transcript', side_effect=race):
                self.worker.tick()
            self.assertFalse(mr.get(self.store, self.mid, 'acme/app')['record_id'])

    def test_expired_lease_restart_recovers_without_old_publication(self):
        self.joined()
        original = self.client.transcript
        def expired(m):
            self.t += 91
            return original(m)
        with patch.object(self.client, 'transcript', side_effect=expired):
            self.worker.tick()
        self.assertFalse(mr.get(self.store, self.mid, 'acme/app')['record_id'])
        mw.Worker(self.store, client_factory=lambda: self.client, clock=lambda: self.t, allow_operator=True).tick()
        self.assertTrue(mr.get(self.store, self.mid, 'acme/app')['record_id'])

    def test_current_admin_required_before_and_after_network(self):
        self.store.graph.db.execute("INSERT INTO people(id,name,role) VALUES('admin','Admin','admin')")
        actor = Identity('admin', 'Admin', 'admin', 'session')
        mr.command(self.store, self.mid, 'acme/app', 'join', client=self.client)
        self.enable(actor)
        original = self.client.transcript
        def demote(m):
            self.store.graph.db.execute("UPDATE people SET role='viewer' WHERE id='admin'")
            return original(m)
        with patch.object(self.client, 'transcript', side_effect=demote):
            self.worker.tick()
        self.assertFalse(mr.get(self.store, self.mid, 'acme/app')['record_id'])
        self.assertFalse(mw.status(self.store, self.mid)['enabled'])
        with self.assertRaises(Invalid):
            self.enable(actor)

    def test_mismatched_occurrence_never_imports(self):
        self.joined()
        with patch.object(self.client, 'transcript', return_value={'id': 999, 'segments': [{'text': 'wrong'}]}):
            self.worker.tick()
        self.assertFalse(mr.get(self.store, self.mid, 'acme/app')['record_id'])
        self.assertEqual(mw.status(self.store, self.mid)['status'], 'transcript_retry_pending')

    def test_failed_capture_is_not_complete(self):
        self.joined()
        with patch.object(self.client, 'status', return_value={'meeting': {'id': 123, 'status': 'failed'}}):
            self.worker.tick()
        self.assertEqual(mw.status(self.store, self.mid)['status'], 'incomplete')
        self.assertFalse(mr.get(self.store, self.mid, 'acme/app')['transcript_complete'])

    def test_revoke_during_status_prevents_transcript_read(self):
        self.joined()
        original = self.client.status
        def revoke(m):
            mr.revoke(self.store, self.mid, 'acme/app')
            return original(m)
        with patch.object(self.client, 'status', side_effect=revoke), patch.object(self.client, 'transcript') as transcript:
            self.worker.tick()
            transcript.assert_not_called()

    def test_operator_grant_not_valid_in_authenticated_deployment(self):
        self.joined()
        worker = mw.Worker(self.store, client_factory=lambda: self.client, clock=lambda: self.t)
        with patch.object(self.client, 'status') as remote:
            worker.tick()
            remote.assert_not_called()
        self.assertFalse(mw.status(self.store, self.mid)['enabled'])

    def test_disable_and_lease_loss_between_reads_prevent_transcript_read(self):
        self.joined()
        original = self.client.status
        for action in ('disable', 'lease'):
            self.enable()
            def race(m):
                if action == 'disable':
                    mw.configure_sync(self.store, self.mid, 'acme/app', {'enabled': False}, OPERATOR)
                else:
                    self.store.graph.db.execute("UPDATE meeting_observers SET lease_owner='replacement' WHERE meeting_id=?", (self.mid,))
                return original(m)
            with patch.object(self.client, 'status', side_effect=race), patch.object(self.client, 'transcript') as transcript:
                self.worker.tick()
                transcript.assert_not_called()

    def test_stop_during_fetch_fences_snapshot_but_retains_final_sync_consent(self):
        self.joined()
        original = self.client.transcript
        def stop(m):
            mr.command(self.store, self.mid, 'acme/app', 'stop', client=self.client)
            return original(m)
        with patch.object(self.client, 'transcript', side_effect=stop):
            self.worker.tick()
        self.assertFalse(mr.get(self.store, self.mid, 'acme/app')['record_id'])
        self.assertTrue(mw.status(self.store, self.mid)['enabled'])
        self.t += 60
        self.worker.tick()
        self.assertTrue(mr.get(self.store, self.mid, 'acme/app')['record_id'])

    def test_confirmed_capture_status_survives_transcript_failure(self):
        self.joined()
        for remote in ('completed', 'failed'):
            self.enable()
            with patch.object(self.client, 'status', return_value={'meeting': {'id': 123, 'status': remote}}), patch.object(self.client, 'transcript', side_effect=Invalid('404')):
                self.worker.tick()
            value = mr.get(self.store, self.mid, 'acme/app')
            self.assertEqual(value['state'], remote)
            self.assertEqual(value['remote_state'], remote)
            self.assertFalse(value['transcript_complete'])
            self.assertEqual(mw.status(self.store, self.mid)['status'], 'incomplete' if remote == 'failed' else 'transcript_retry_pending')

    def test_explicit_empty_completed_transcript_is_terminal_without_fabricated_source(self):
        self.joined()
        with patch.object(self.client, 'transcript', return_value={'id': 123, 'status': 'completed', 'segments': []}):
            self.worker.tick()
        value = mr.get(self.store, self.mid, 'acme/app')
        self.assertTrue(value['transcript_complete'])
        self.assertFalse(value['record_id'])
        self.assertEqual(mw.status(self.store, self.mid)['status'], 'complete_empty')
        self.assertFalse(mw.status(self.store, self.mid)['enabled'])

    def test_operational_projection_exposes_sync_without_evidence_or_identity(self):
        self.joined()
        self.worker.tick()
        value = mr.operational_view(self.store, mr.get(self.store, self.mid, 'acme/app'))
        self.assertTrue(value['background_sync']['enabled'])
        self.assertEqual(value['background_sync']['status'], 'waiting_for_transcript')
        for key in ('record_id', 'chunks', 'remote_id', 'last_error'):
            self.assertNotIn(key, value)
        for key in ('actor_id', 'actor_name', 'lease_owner'):
            self.assertNotIn(key, value['background_sync'])
        calls = list(self.client.calls)
        revision = value['background_sync']['revision']
        mr.revoke(self.store, self.mid, 'acme/app')
        value = mr.operational_view(self.store, value)
        self.assertTrue(value['evidence_withheld'])
        self.assertIn('background_sync', value)
        self.assertFalse(value['background_sync']['enabled'])
        self.assertEqual(value['background_sync']['status'], 'authorization_required')
        self.assertGreater(value['background_sync']['revision'], revision)
        self.assertEqual(self.client.calls, calls)
        self.assertEqual(value['title'], 'Meeting capture (transcript withheld)')
