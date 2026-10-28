"""Metadata-only disclosure dependencies do not masquerade as full body reads."""
from fixtures import OfflineCase
from bridge import objectives as obj, objective_head as head, objective_research as research, canvas
from bridge.auth import Identity
from bridge.store import Invalid


class ObjectiveMetadataTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store('objective-metadata')
        self.actor = Identity('', 'Local operator', 'admin', 'operator')
        self.repo = 'sample/project'
        self.store.add_owner({'name':'Owner', 'team':'Policy', 'patterns':'*'})
        self.payload = {'request_key':'create', 'title':'Pilot', 'outcome':'Prepare the CSV path',
            'success_criteria':'Customer completes report', 'stop_criteria':'Customer declines',
            'guardrails':'No external sends', 'repository':self.repo, 'provider':'claude',
            'why_now':'A current source shows friction', 'cases':{}, 'judgment_kind':'interpretation_tradeoff'}
        self.full = self.record('full', 'The CSV example is broken.', 'Full read')
        self.metadata = self.record('metadata', 'Unread body.', 'Confidential roadmap')
        self.pins = [self.pin(self.full), {**self.pin(self.metadata), 'metadata_only':True,
            'metadata_sha256':research.metadata_fingerprint(self.metadata)}]

    def record(self, ref, body, title, **fields):
        return self.store.add_record({'repo':self.repo, 'kind':'note', 'ref':ref, 'body':body, 'title':title, **fields})['source']

    def pin(self, source):
        return {k:source[k] for k in ('record_id','source_version_id')}

    def accepted(self):
        proposal = obj.propose(self.store, self.payload, self.pins)
        review = obj.proposal_action(self.store, proposal['id'], 'accept',
            {'request_key':'review', 'expected_revision':1}, self.actor)
        decision = self.store.get_decision(review['decision_id'])
        canvas.sign_off(self.store, review['decision_id'], {'by':'Owner', 'expected_updated_at':decision['updated_at']})
        head.activate_reviews(self.store)
        objective = obj.overview(self.store)['objectives'][0]
        return objective, proposal['id']

    def test_activation_keeps_metadata_out_of_full_read_table(self):
        objective, proposal = self.accepted()
        sources = obj._sources(self.store, objective['id'])
        metadata = next(s for s in sources if s['record_id'] == self.metadata['record_id'])
        self.assertTrue(metadata['metadata_only'])
        self.assertEqual(metadata['metadata_sha256'], research.metadata_fingerprint(self.metadata))
        full = list(self.store.graph.db.execute('SELECT record_id FROM objective_sources WHERE objective_id=?', (objective['id'],)))
        self.assertEqual([r['record_id'] for r in full], [self.full['record_id']])
        obj.assert_objective_authority(self.store, objective['id'], proposal)

    def test_unread_body_revision_does_not_stale_accepted_scope(self):
        objective, proposal = self.accepted()
        self.record('metadata', 'Completely changed unread body.', 'Confidential roadmap')
        obj.assert_objective_authority(self.store, objective['id'], proposal)
        obj.sources_current(self.store, obj._latest_sources(self.store, objective['id']), self.repo, exact=True)
        saved = obj.get(self.store, objective['id'])
        self.assertFalse(saved['proposals'][0]['evidence_stale'])
        self.assertFalse(next(s for s in saved['sources'] if s['record_id'] == self.metadata['record_id'])['current'])
        # Historical observed version remains for audit, not a false current-body claim.

    def test_shown_title_change_blocks_current_authority(self):
        objective, proposal = self.accepted()
        self.record('metadata', 'Unread body.', 'Different strategic roadmap')
        with self.assertRaisesRegex(Invalid, 'evidence changed'):
            obj.assert_objective_authority(self.store, objective['id'], proposal)
        self.assertTrue(obj.get(self.store, objective['id'])['proposals'][0]['evidence_stale'])

    def test_metadata_revocation_withholds_events_and_blocks_linked_task(self):
        objective, proposal = self.accepted()
        obj.event(self.store, objective['id'], 'researched', 'Confidential roadmap paraphrase')
        self.store.graph.db.execute('INSERT INTO objective_run_authority VALUES(?,?,?)', ('worker',objective['id'],proposal))
        self.record('metadata', 'Unread body.', 'Confidential roadmap', availability='inaccessible')
        with self.assertRaises(Invalid):
            obj.get(self.store, objective['id'])
        with self.assertRaises(Invalid):
            obj.assert_task_authority(self.store, 'worker')
        self.assertTrue(obj.overview(self.store)['objectives'][0]['withheld'])

    def test_factual_remembering_preserves_metadata_and_full_reads_dominate(self):
        objective = obj.create(self.store, {**self.payload,'source_ids':[]}, self.actor)
        obj.remember_sources(self.store, objective['id'], self.pins)
        self.assertTrue(next(s for s in obj._sources(self.store, objective['id']) if s['record_id']==self.metadata['record_id'])['metadata_only'])
        obj.remember_sources(self.store, objective['id'], [self.pin(self.metadata)])
        sources = [s for s in obj._latest_sources(self.store, objective['id']) if s['record_id']==self.metadata['record_id']]
        self.assertEqual(sources, [self.pin(self.metadata)])
        self.assertEqual(len([s for s in obj._sources(self.store, objective['id']) if s['record_id']==self.metadata['record_id']]), 2)
        self.record('metadata', 'Body now changed.', 'Confidential roadmap')
        with self.assertRaises(Invalid):
            obj.sources_current(self.store, obj._latest_sources(self.store, objective['id']), self.repo, exact=True)

    def test_historical_metadata_remains_withheld_after_newer_full_read(self):
        objective = obj.create(self.store, {**self.payload, 'source_ids':[]}, self.actor)
        obj.remember_sources(self.store, objective['id'], [self.pins[1]])
        obj.event(self.store, objective['id'], 'researched', 'Confidential roadmap paraphrase')
        newer = self.record('metadata', 'Now read fully.', 'Public roadmap')
        obj.remember_sources(self.store, objective['id'], [self.pin(newer)])
        self.assertEqual(obj._latest_sources(self.store, objective['id']), [self.pin(newer)])
        self.assertEqual(len(obj._sources(self.store, objective['id'])), 2)
        self.assertTrue(obj.get(self.store, objective['id'])['events'])
        self.store.graph.db.execute('INSERT INTO objective_run_authority VALUES(?,?,?)',
                                   ('worker', objective['id'], ''))
        self.store.graph.db.execute('INSERT INTO withheld_source_versions VALUES(?,?,?)',
                                   (self.metadata['source_version_id'], 'historical restriction', '2026-10-25T00:00:00Z'))
        # The current body remains readable, but an old derived event is not.
        self.assertEqual(self.store.get_record(newer['record_id'], self.repo)['source']['source_version_id'], newer['source_version_id'])
        with self.assertRaisesRegex(Invalid, 'withheld'):
            obj.get(self.store, objective['id'])
        with self.assertRaisesRegex(Invalid, 'source access'):
            obj.assert_task_authority(self.store, 'worker')
        self.assertTrue(obj.overview(self.store)['objectives'][0]['withheld'])

    def test_metadata_checks_run_on_selected_writer_connection(self):
        objective, proposal = self.accepted()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE source_records SET availability='inaccessible' WHERE id=?", (self.metadata['record_id'],))
            with self.assertRaises(Invalid):
                obj.assert_objective_authority(self.store, objective['id'], proposal, db=db)

    def test_cached_watch_rechecks_admin_role_and_enabled_revision(self):
        person = self.store.add_person({'name':'Watch Admin', 'role':'admin'})
        actor = Identity(person['id'],person['name'],'admin','session')
        head.configure_watch(self.store, {'repository':self.repo,'enabled':True}, actor)
        revision = self.store.graph.db.execute('SELECT updated_at FROM objective_watches').fetchone()['updated_at']
        class FactualClient:
            def complete_json(inner, *args, **kwargs):
                return {'action':'finish','disposition':'no_material_move','observations':[], 'reason':'No new material work'}
        data = {'repository':self.repo,'request_key':'watch','watch_revision':revision}
        first = head.scan(self.store, data, actor, client=FactualClient())
        self.assertEqual(first['status'], 'no_change')
        self.store.graph.db.execute("UPDATE people SET role='member' WHERE id=?", (actor.id,))
        with self.assertRaisesRegex(Invalid, 'required workspace role'):
            head.scan(self.store, data, actor, client=FactualClient())
        self.store.graph.db.execute("UPDATE people SET role='admin' WHERE id=?", (actor.id,))
        self.store.graph.db.execute('UPDATE objective_watches SET enabled=0 WHERE repository=?', (self.repo,))
        with self.assertRaisesRegex(Invalid, 'watch changed'):
            head.scan(self.store, data, actor, client=FactualClient())
        self.store.graph.db.execute("UPDATE objective_watches SET enabled=1,updated_at='different' WHERE repository=?", (self.repo,))
        with self.assertRaisesRegex(Invalid, 'watch changed'):
            head.scan(self.store, data, actor, client=FactualClient())

    def test_background_worker_survives_transient_tick_failure(self):
        from unittest.mock import patch
        from bridge.config import Config
        # A workbench means the worker has work, so the loop ticks instead of idling.
        worker = head.Worker(self.store, object(), cfg=Config(model_api='none'))
        calls = []
        def tick():
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError('sensitive provider detail must not be logged')
            worker.stop.set()
        with patch.object(worker, 'tick', side_effect=tick), patch.object(worker.stop, 'wait', return_value=False), patch('sys.stderr') as stderr:
            worker.start()
            worker.thread.join(timeout=2)
        self.assertEqual(len(calls), 2)
        self.assertFalse(worker.thread.is_alive())
        logged = ''.join(str(call.args[0]) for call in stderr.write.call_args_list)
        self.assertNotIn('sensitive provider', logged)
        self.assertIn('retrying saved work', logged)
