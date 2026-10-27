"""Real durable objective/core contracts; inference and coding agents are doubles."""

from fixtures import OfflineCase
from bridge import objectives as obj, objective_head as head, canvas
from bridge.auth import Identity
from bridge.config import Config
from bridge.store import Invalid, Store


class HeadDouble:
    def __init__(self, result, effect=None):
        self.result, self.effect, self.calls = result, effect, 0

    def complete_json(self, *args, **kwargs):
        if args[0] == 'objective factual research':
            return {'action': 'finish', 'disposition': 'needs_judgment', 'observations': [], 'reason': 'Fixture decision remains after reading source'}
        self.calls += 1
        if self.effect:
            self.effect()
        return self.result


class ObjectiveTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store('objectives')
        self.actor = Identity('', 'Local operator', 'admin', 'operator')
        self.payload = {'request_key': 'create-one', 'title': 'Pilot to value', 'outcome': 'Prepare the CSV path',
            'success_criteria': 'Customer independently completes first report', 'stop_criteria': 'Customer declines pilot',
            'guardrails': 'No custom integration or external sending', 'repository': 'sample/project', 'provider': 'claude'}
        self.record_data = {'repo': 'sample/project', 'kind': 'note', 'ref': 'pilot-call', 'body': 'The CSV example does not work.', 'title': 'Pilot call'}
        source = self.store.add_record(self.record_data)['source']
        self.pin = {k: source[k] for k in ('record_id','source_version_id')}
        self.payload['source_ids'] = [source['record_id']]

    def create(self):
        return obj.create(self.store, self.payload, self.actor)

    def action(self, value, action, **data):
        return obj.act(self.store, value['id'], action, {'request_key': action + str(value['revision']),
            'expected_revision': value['revision'], **data}, self.actor)

    def proposal(self):
        return {**self.payload, 'why_now': 'Pilot is blocked', 'judgment_kind': 'implementation_uncertainty',
            'cases': {'positive': 'Fix example CSV', 'negative': 'Do not create a custom integration',
                      'boundary': 'Ask if CSV changes billable meaning', 'non_implications': 'No external sending'},
            'citations': [{**self.pin, 'quote': 'The CSV example does not work.'}]}

    def test_large_research_binds_digest_without_truncating_signed_scope(self):
        payload = {**self.proposal(), 'research': {'coverage': 'Observed metadata ' * 2000}}
        proposal = obj.propose(self.store, payload, [self.pin])
        reviewed = obj.proposal_action(self.store, proposal['id'], 'accept', {
            'request_key': 'large-research', 'expected_revision': proposal['revision']}, self.actor)
        decision = self.store.get_decision(reviewed['decision_id'])
        import json
        scope = json.loads(decision['context'])
        self.assertEqual(scope['cases'], payload['cases'])
        self.assertEqual(scope['research_digest'], obj.digest(payload['research']))
        self.assertEqual(scope['source_dependencies_digest'], obj.digest([self.pin]))
        self.assertNotIn('research', scope)

    def test_oversized_complete_scope_never_persists_a_stuck_proposal(self):
        with self.assertRaisesRegex(Invalid, 'smaller bounded move'):
            obj.propose(self.store, {**self.proposal(), 'cases': {'positive': 'x' * 13000}}, [self.pin])
        self.assertFalse(list(self.store.graph.db.execute('SELECT id FROM objective_proposals')))

    def test_persistently_invalid_head_stops_after_three_bounded_attempts(self):
        client = HeadDouble({'unexpected': 'invalid result'})
        data = {'repository': 'sample/project', 'request_key': 'retry-bound'}
        for attempt in range(3):
            with self.assertRaises(Invalid):
                head.scan(self.store, data, self.actor, client=client)
            row = self.store.graph.db.execute('SELECT attempts,lease_until FROM objective_evaluations').fetchone()
            self.assertEqual(row['attempts'], attempt + 1)
            self.store.graph.db.execute('UPDATE objective_evaluations SET lease_until=0')
        for _ in range(2):
            self.assertEqual(head.scan(self.store, data, self.actor, client=client)['status'], 'blocked')
        self.assertEqual(client.calls, 3)
        self.store.add_record({**self.record_data, 'body': 'New evidence changes the factual basis.'})
        with self.assertRaises(Invalid):
            head.scan(self.store, data, self.actor, client=client)
        self.assertEqual(client.calls, 4)
        with self.assertRaises(Invalid):
            head.scan(self.store, {**data, 'retry_failed': True, 'request_key': 'explicit-human-retry'}, self.actor, client=client)
        self.assertEqual(client.calls, 5)

    def test_overview_exposes_sanitized_inconclusive_state_not_provider_errors(self):
        with self.assertRaises(Invalid):
            head.scan(self.store, {'repository': 'sample/project', 'request_key': 'bad-model'}, self.actor,
                      client=HeadDouble({'invalid': True}))
        self.store.graph.db.execute("UPDATE objective_evaluations SET error='fixture-private-provider-detail'")
        overview = obj.overview(self.store)
        self.assertEqual(overview['research_status'][0]['state'], 'failed')
        self.assertEqual(overview['research_status'][0]['attempts'], 1)
        self.assertNotIn('fixture-private-provider-detail', str(overview))

    def test_explicit_take_point_idempotent_durable_and_not_an_outcome(self):
        created = self.create()
        self.assertEqual(created['id'], self.create()['id'])
        self.assertEqual(created['status'], 'waiting')
        reopened = Store(self.store.path)
        self.assertEqual(obj.get(reopened, created['id'])['outcome'], self.payload['outcome'])
        reopened.graph.close()
        with self.assertRaises(Invalid):
            obj.create(self.store, {**self.payload, 'outcome': 'different'}, self.actor)
        self.assertFalse(created['events'][0]['detail']['external_actions_authorized'])

    def test_agent_cannot_accept_or_control_objective(self):
        actor = Identity('bot', 'Bot', 'admin', 'agent')
        with self.assertRaises(Invalid):
            obj.create(self.store, self.payload, actor)
        created = self.create()
        with self.assertRaises(Invalid):
            obj.act(self.store, created['id'], 'achieve', {'request_key': 'a', 'expected_revision': 1, 'summary': 'done'}, actor)

    def test_today_without_a_model_lists_followups_reviews_checkpoints_and_new_records(self):
        """Without a model key Today still says what is open: unadopted
        follow-ups, answers put in doubt, due checkpoints and evidence recorded
        since the last investigation; closed objectives leave the main list."""
        run = canvas.start_task(self.store, Config(model_api='none'), {'title': 'Pilot CSV path', 'goal': 'Fix the example',
            'repo': 'sample/project', 'agent': 'Fixture agent', 'requester': 'Local operator', 'client_key': 'open-loops'})
        with self.store.graph.transaction() as db:
            suggested = self.store.graph.add_decision(run['task_id'], 'Which trial limit applies?', 'followup', 'suggested')
            doubted = self.store.graph.add_decision(run['task_id'], 'Keep the CSV example?', 'design', 'approved', answer='Yes', answered_by='Local operator')
            db.execute("UPDATE decisions SET needs_review=1,review_reason='The trial policy it leaned on was corrected' WHERE id=?", (doubted,))
        created = self.create()
        self.action(created, 'checkpoint', summary='Waiting on the pilot', next_check_at='2020-01-01T00:00:00+00:00')
        overview = obj.overview(self.store)
        open_loops = overview['without_model']
        self.assertEqual([(f['id'], f['task_title']) for f in open_loops['followups']], [(suggested, 'Pilot CSV path')])
        self.assertEqual(open_loops['needs_review'][0]['id'], doubted)
        self.assertEqual(open_loops['needs_review'][0]['review_reason'], 'The trial policy it leaned on was corrected')
        self.assertEqual([c['id'] for c in open_loops['checkpoints_due']], [created['id']])
        self.assertEqual(open_loops['new_records'], [{'repository': 'sample/project', 'count': 1}])
        self.assertEqual(open_loops['totals'], {'followups': 1, 'needs_review': 1, 'checkpoints_due': 1, 'new_records': 1})
        from bridge.store import now
        with self.store.graph.transaction() as db:
            db.execute("INSERT INTO objective_evaluations(id,objective_id,repository,fingerprint,state,result,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       ('eval-1', '', 'sample/project', 'fp-1', 'completed', '{}', now(), now()))
        self.assertEqual(obj.overview(self.store)['without_model']['new_records'], [])
        self.store.add_record({**self.record_data, 'ref': 'second-call', 'body': 'The customer asked again.'})
        self.assertEqual(obj.overview(self.store)['without_model']['new_records'], [{'repository': 'sample/project', 'count': 1}])
        self.action(obj.get(self.store, created['id']), 'cancel')
        overview = obj.overview(self.store)
        self.assertEqual(overview['objectives'], [])
        self.assertEqual([c['id'] for c in overview['closed']], [created['id']])
        self.assertEqual(overview['without_model']['checkpoints_due'], [])

    def test_objective_scope_fallback_setting_chooses_the_unrouted_owner(self):
        """No authority map covers the scope review: the setting says whether
        it goes to the accepter, the coordinator, or waits for an admin. Each
        mode starts from a fresh workspace: once a node has an owner, the
        next like it is routed to that owner before any fallback."""
        def review(mode, *, coordinator=False):
            store = self.fresh_store('fallback-' + mode + ('' if coordinator else '-unset'))
            self.addCleanup(store.graph.close)
            source = store.add_record(self.record_data)['source']
            pin = {k: source[k] for k in ('record_id', 'source_version_id')}
            with store.graph.transaction():
                member_id = store.graph.add_person('Member', role='member')
                coordinator_id = store.graph.add_person('Coordinator', role='admin')
            member = Identity(member_id, 'Member', 'member', 'session')
            if mode:
                store.graph.set_setting('objective_scope_fallback', mode)
            if coordinator:
                store.graph.set_setting('coordinator', coordinator_id)
            proposal = obj.propose(store, {**self.proposal(), 'citations': [{**pin, 'quote': 'The CSV example does not work.'}]}, [pin])
            reviewed = obj.proposal_action(store, proposal['id'], 'accept', {'request_key': 'review', 'expected_revision': proposal['revision']}, member)
            return store, store.get_decision(reviewed['decision_id']), member_id, coordinator_id
        for mode in ('', 'accepter'):
            with self.subTest(mode=mode or 'default'):
                store, decision, member_id, _ = review(mode)
                self.assertEqual(decision['owner_id'], store.graph.owner_id_for_person(member_id))
                self.assertEqual(decision['actor_basis'], 'objective-accepter')
        # The core already hands what nobody owns to a coordinator when one is
        # set; the setting keeps the accepter out of it either way.
        store, decision, _, coordinator_id = review('coordinator', coordinator=True)
        self.assertEqual(decision['owner_id'], store.graph.owner_id_for_person(coordinator_id))
        self.assertNotEqual(decision['actor_basis'], 'objective-accepter')
        store, decision, _, _ = review('coordinator')
        self.assertFalse(decision['owner_id'])
        self.assertIn('no coordinator is set', decision['routing_reason'])
        store, decision, _, _ = review('none')
        self.assertFalse(decision['owner_id'])
        self.assertIn('administrator must assign', decision['routing_reason'])

    def test_revision_retry_and_closed_objective(self):
        created = self.create()
        data = {'request_key': 'pause', 'expected_revision': 1}
        paused = obj.act(self.store, created['id'], 'pause', data, self.actor)
        self.assertEqual(paused, obj.act(self.store, created['id'], 'pause', data, self.actor))
        with self.assertRaises(Invalid):
            self.action(created, 'resume')
        closed = self.action(paused, 'achieve', summary='Founder observed the real report')
        self.assertEqual(closed['status'], 'achieved')
        self.assertEqual(closed['events'][-1]['detail']['provenance'], 'human-attested')
        with self.assertRaises(Invalid):
            self.action(closed, 'resume')

    def test_due_checkpoint_survives_restart_without_inference(self):
        created = self.create()
        value = self.action(created, 'checkpoint', summary='Await report', next_check_at='2020-01-01T00:00:00Z', waiting_on='Customer')
        worker = head.Worker(self.store, cfg=Config(model_api='none'))
        worker.tick()
        current = obj.get(self.store, value['id'])
        self.assertEqual(current['status'], 'needs_you')
        self.assertEqual(current['next_check_at'], '')
        worker.tick()
        self.assertEqual(len([e for e in obj.get(self.store, value['id'])['events'] if e['kind'] == 'checkpoint_due']), 1)

    def test_revocation_withholds_copied_objective_and_proposal(self):
        created = self.create()
        proposal = obj.propose(self.store, self.proposal(), [self.pin])
        self.store.add_record({**self.record_data, 'availability': 'inaccessible'})
        with self.assertRaises(Invalid):
            obj.get(self.store, created['id'])
        overview = obj.overview(self.store)
        self.assertTrue(overview['objectives'][0]['withheld'])
        self.assertEqual(overview['proposals'][0]['id'], proposal['id'])
        self.assertTrue(overview['proposals'][0]['withheld'])
        self.assertNotIn('outcome', overview['objectives'][0])

    def test_scan_validates_citations_and_deduplicates(self):
        client = HeadDouble({'proposal': self.proposal(), 'reason': 'Observed friction'})
        data = {'repository': 'sample/project', 'request_key': 'scan'}
        first = head.scan(self.store, data, self.actor, client=client)
        self.assertEqual(first, head.scan(self.store, data, self.actor, client=client))
        self.assertEqual(client.calls, 1)
        self.assertEqual(first['proposal']['cases']['boundary'], 'Ask if CSV changes billable meaning')

    def test_source_race_does_not_publish_proposal(self):
        client = HeadDouble({'proposal': self.proposal(), 'reason': 'friction'},
            lambda: self.store.add_record({**self.record_data, 'body': 'The example now works.'}))
        with self.assertRaises(Invalid):
            head.scan(self.store, {'repository': 'sample/project', 'request_key': 'scan'}, self.actor, client=client)
        self.assertEqual(obj.overview(self.store)['proposals'], [])

    def test_no_model_is_honest_and_noop_is_not_fake_work(self):
        with self.assertRaises(Invalid):
            head.scan(self.store, {'repository': 'sample/project', 'request_key': 'scan'}, self.actor, cfg=Config(model_api='none'))
        client = HeadDouble({'proposal': None, 'reason': 'No useful next move in current evidence'})
        result = head.scan(self.store, {'repository': 'sample/project', 'request_key': 'scan'}, self.actor, client=client)
        self.assertEqual(result['status'], 'no_change')
        self.assertEqual(obj.overview(self.store)['proposals'], [])

    def test_rejects_hallucinated_quote(self):
        proposal = self.proposal()
        proposal['citations'][0]['quote'] = 'Please deploy now'
        with self.assertRaises(Invalid):
            head.scan(self.store, {'repository': 'sample/project', 'request_key': 'scan'}, self.actor,
                      client=HeadDouble({'proposal': proposal}))

    def test_review_uses_real_core_unsigned_node_and_only_matching_signoff_activates(self):
        self.store.add_owner({'name': 'Policy Owner', 'team': 'Policy', 'paths': '*', 'repo': 'sample/project'})
        proposal = obj.propose(self.store, self.proposal(), [self.pin])
        review = obj.proposal_action(self.store, proposal['id'], 'accept',
            {'request_key': 'review', 'expected_revision': 1}, self.actor)
        self.assertEqual(review['state'], 'review')
        self.assertTrue(review['decision_id'])
        self.assertFalse(canvas.node_view(self.store, review['decision_id'])['authorized'])
        head.activate_reviews(self.store)
        self.assertEqual(obj.overview(self.store)['objectives'], [])
        decision = self.store.get_decision(review['decision_id'])
        canvas.sign_off(self.store, review['decision_id'], {'by': 'Policy Owner', 'expected_updated_at': decision['updated_at']})
        head.activate_reviews(self.store)
        self.assertEqual(len(obj.overview(self.store)['objectives']), 1)
        head.activate_reviews(self.store)
        self.assertEqual(len(obj.overview(self.store)['objectives']), 1)

    def test_signed_negative_answer_does_not_activate_proposal(self):
        self.store.add_owner({'name': 'Policy Owner', 'team': 'Policy', 'paths': '*', 'repo': 'sample/project'})
        proposal = obj.propose(self.store, self.proposal(), [self.pin])
        review = obj.proposal_action(self.store, proposal['id'], 'accept', {'request_key': 'review', 'expected_revision': 1}, self.actor)
        decision = self.store.get_decision(review['decision_id'])
        self.store.answer(review['decision_id'], {'answer': 'No, do not proceed', 'by': 'Policy Owner', 'expected_updated_at': decision['updated_at']})
        head.activate_reviews(self.store)
        self.assertEqual(obj.overview(self.store)['objectives'], [])
        raw = self.store.graph.db.execute('SELECT state FROM objective_proposals WHERE id=?', (proposal['id'],)).fetchone()
        self.assertEqual(raw['state'], 'reconcile')

    def test_failed_dispatch_does_not_create_run_or_change_revision(self):
        created = self.create()
        with self.assertRaises(Invalid):
            self.action(created, 'dispatch')
        self.assertEqual(obj.get(self.store, created['id'])['revision'], 1)
        self.assertEqual(obj.get(self.store, created['id'])['runs'], [])

    def test_expired_or_stolen_lease_cannot_publish_or_overwrite_new_attempt(self):
        def steal():
            self.store.graph.db.execute("UPDATE objective_evaluations SET lease_owner='new-worker',state='running',result='{}'")
        with self.assertRaisesRegex(Invalid, 'lost its lease'):
            head.scan(self.store, {'repository': 'sample/project', 'request_key': 'scan'}, self.actor,
                      client=HeadDouble({'proposal': self.proposal()}, steal))
        saved = self.store.graph.db.execute('SELECT state,lease_owner FROM objective_evaluations').fetchone()
        self.assertEqual((saved['state'], saved['lease_owner']), ('running', 'new-worker'))
        self.assertEqual(obj.overview(self.store)['proposals'], [])

    def test_head_cache_does_not_reuse_different_selected_harness(self):
        client = HeadDouble({'proposal': None, 'reason': 'Nothing material'})
        for provider in ('claude', 'codex'):
            head.scan(self.store, {'repository': 'sample/project', 'request_key': provider, 'provider': provider}, self.actor, client=client)
        self.assertEqual(client.calls, 2)

    def test_changed_signed_governance_blocks_linked_worker(self):
        self.store.add_owner({'name': 'Policy Owner', 'team': 'Policy', 'patterns': '*'})
        proposal = obj.propose(self.store, self.proposal(), [self.pin])
        review = obj.proposal_action(self.store, proposal['id'], 'accept', {'request_key': 'review', 'expected_revision': 1}, self.actor)
        decision = self.store.get_decision(review['decision_id'])
        canvas.sign_off(self.store, review['decision_id'], {'by': 'Policy Owner', 'expected_updated_at': decision['updated_at']})
        head.activate_reviews(self.store)
        objective = obj.overview(self.store)['objectives'][0]
        self.store.graph.db.execute('INSERT INTO objective_run_authority VALUES(?,?,?)', ('worker', objective['id'], proposal['id']))
        obj.assert_task_authority(self.store, 'worker')
        decision = self.store.get_decision(review['decision_id'])
        self.store.answer(review['decision_id'], {'answer': 'Stop this work', 'by': 'Policy Owner', 'expected_updated_at': decision['updated_at']})
        with self.assertRaisesRegex(Invalid, 'judgment changed'):
            obj.assert_task_authority(self.store, 'worker')

    def test_dispatch_conveys_exact_signed_move_cases_and_narrower_bounds(self):
        self.store.add_owner({'name': 'Policy Owner', 'team': 'Policy', 'patterns': '*'})
        objective = self.create()
        payload = self.proposal()
        payload.update(guardrails='Only repair fixture formatting; never change prices',
                       stop_criteria='Stop when a billable outcome changes')
        proposal = obj.propose(self.store, payload, [self.pin], objective_id=objective['id'])
        review = obj.proposal_action(self.store, proposal['id'], 'accept', {'request_key': 'review', 'expected_revision': 1}, self.actor)
        decision = self.store.get_decision(review['decision_id'])
        canvas.sign_off(self.store, review['decision_id'], {'by': 'Policy Owner', 'expected_updated_at': decision['updated_at']})
        head.activate_reviews(self.store)
        store = self.store
        class CapturingWorkbench:
            def submit(self, data, actor):
                self.prompt = data['prompt']
                store.graph.db.execute("INSERT INTO runs(id,title,agent,repo,status,updated_at) VALUES('captured','Capture','test','sample/project','queued','now')")
                return {'task_id': 'captured'}
        workbench = CapturingWorkbench()
        current = obj.get(store, objective['id'])
        obj.act(store, objective['id'], 'dispatch', {'request_key': 'dispatch', 'expected_revision': current['revision']}, self.actor, workbench)
        for text in (payload['guardrails'], payload['stop_criteria'], payload['cases']['boundary'], payload['cases']['negative']):
            self.assertIn(text, workbench.prompt)
        self.assertIn(self.payload['guardrails'], workbench.prompt)

    def test_stale_proposal_can_be_dismissed_so_new_evidence_can_progress(self):
        objective = self.create()
        proposal = obj.propose(self.store, self.proposal(), [self.pin], objective_id=objective['id'])
        # A dismissal teaches the next investigation, so it needs a reason:
        # none, a blank one, or the old default are refused and change nothing.
        for data in ({}, {'rationale': '  '}, {'rationale': 'No'}, {'rationale': 'Not pursuing this proposal'}):
            with self.assertRaisesRegex(Invalid, 'reason of at least 3 characters'):
                obj.proposal_action(self.store, proposal['id'], 'dismiss', {'expected_revision': 1, 'request_key': 'dismiss-old', **data}, self.actor)
        self.assertEqual(obj.get(self.store, objective['id'])['proposals'][0]['state'], 'proposed')
        dismissed = obj.proposal_action(self.store, proposal['id'], 'dismiss', {'expected_revision': 1, 'request_key': 'dismiss-old', 'rationale': 'The CSV issue is already fixed on the pilot branch'}, self.actor)
        self.assertEqual(dismissed['state'], 'dismissed')
        self.assertEqual(dismissed['rationale'], 'The CSV issue is already fixed on the pilot branch')
        # The identical proposal on the same evidence is the dismissed one, not a new card.
        again = obj.propose(self.store, self.proposal(), [self.pin], objective_id=objective['id'])
        self.assertEqual((again['id'], again['state']), (proposal['id'], 'dismissed'))
        self.assertEqual([p['state'] for p in obj.get(self.store, objective['id'])['proposals']], ['dismissed'])
        # Newer evidence makes the dismissed card stale and can carry a new one.
        source = self.store.add_record({**self.record_data, 'body': 'A different issue now blocks the pilot.'})['source']
        self.assertTrue(obj.get(self.store, objective['id'])['proposals'][0]['evidence_stale'])
        fresh = obj.propose(self.store, self.proposal(), [{k: source[k] for k in ('record_id','source_version_id')}], objective_id=objective['id'])
        self.assertNotEqual(fresh['id'], proposal['id'])
        self.assertEqual(fresh['state'], 'proposed')

    def test_core_completion_refuses_paused_owning_objective(self):
        objective = self.create()
        task = self.store.add_run({'title': 'Work', 'repo': 'sample/project'})['id']
        self.store.graph.db.execute('INSERT INTO objective_run_authority VALUES(?,?,?)', (task, objective['id'], ''))
        self.action(objective, 'pause')
        with self.assertRaisesRegex(Invalid, 'paused or closed'):
            self.store.update_run(task, {'status': 'completed'})
        self.assertEqual(self.store.graph.db.execute('SELECT status FROM runs WHERE id=?', (task,)).fetchone()['status'], 'working')

    def test_idempotent_dismissal_rechecks_disclosure_after_source_revocation(self):
        proposal = obj.propose(self.store, self.proposal(), [self.pin])
        data = {'expected_revision': 1, 'request_key': 'dismiss', 'rationale': 'Already covered elsewhere'}
        self.assertIn('outcome', obj.proposal_action(self.store, proposal['id'], 'dismiss', data, self.actor))
        self.store.add_record({**self.record_data, 'availability': 'inaccessible'})
        replay = obj.proposal_action(self.store, proposal['id'], 'dismiss', data, self.actor)
        self.assertTrue(replay['withheld'])
        self.assertNotIn('outcome', replay)

    def test_factual_research_returns_grounded_answer_without_a_judgment_proposal(self):
        pin = self.pin
        class FactualReader:
            def complete_json(self, purpose, system, prompt, **kwargs):
                self.assert_purpose = purpose
                if purpose != 'objective factual research':
                    raise AssertionError('Resolved fact must not call proposal generator')
                return {'action': 'finish', 'disposition': 'factual_answer', 'reason': 'The example CSV is the observed blocker.',
                    'observations': [{'statement': 'The example does not work.', 'kind': 'fact',
                        'citations': [{**pin, 'quote': 'The CSV example does not work.'}]}]}
        objective = self.create()
        result = head.scan(self.store, {'repository': 'sample/project', 'request_key': 'research-fact'}, self.actor,
                           objective_id=objective['id'], client=FactualReader())
        self.assertEqual(result['status'], 'no_change')
        self.assertEqual(result['research']['disposition'], 'factual_answer')
        self.assertEqual(obj.get(self.store, objective['id'])['proposals'], [])
        self.assertTrue(any(e['kind'] == 'researched' for e in obj.get(self.store, objective['id'])['events']))
        self.assertEqual(obj.overview(self.store)['research'][0]['observations'][0]['citations'][0]['quote'], 'The CSV example does not work.')

    def test_watch_disabled_during_inference_cannot_publish(self):
        head.configure_watch(self.store, {'repository': 'sample/project', 'enabled': True}, self.actor)
        revision = self.store.graph.db.execute('SELECT updated_at FROM objective_watches').fetchone()['updated_at']
        def disable():
            head.configure_watch(self.store, {'repository': 'sample/project', 'enabled': False}, self.actor)
        with self.assertRaisesRegex(Invalid, 'watch changed'):
            head.scan(self.store, {'repository': 'sample/project', 'request_key': 'watch', 'watch_revision': revision}, self.actor,
                      client=HeadDouble({'proposal': self.proposal()}, disable))
        self.assertEqual(obj.overview(self.store)['proposals'], [])

    def test_inactive_requester_blocks_research_before_and_after_model_latency(self):
        person = self.store.graph.add_person('Researcher', role='admin')
        actor = Identity(person, 'Researcher', 'admin', 'session')
        def disable():
            self.store.graph.db.execute('UPDATE people SET active=0 WHERE id=?', (person,))
        with self.assertRaisesRegex(Invalid, 'active workspace'):
            head.scan(self.store, {'repository': 'sample/project', 'request_key': 'deactivate'}, actor,
                      client=HeadDouble({'proposal': self.proposal()}, disable))
        self.assertEqual(obj.overview(self.store)['proposals'], [])
        client = HeadDouble({'proposal': None, 'reason': 'unused'})
        with self.assertRaisesRegex(Invalid, 'active workspace'):
            head.scan(self.store, {'repository': 'sample/project', 'request_key': 'already-inactive'}, actor, client=client)
        self.assertEqual(client.calls, 0)

    def test_background_watch_uses_current_role_not_saved_admin_label(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        person = self.store.graph.add_person('Watch admin', role='admin')
        actor = Identity(person, 'Watch admin', 'admin', 'session')
        head.configure_watch(self.store, {'repository': 'sample/project', 'enabled': True}, actor)
        self.store.graph.db.execute("UPDATE people SET role='viewer' WHERE id=?", (person,))
        worker = head.Worker(self.store, cfg=SimpleNamespace(semantic_retrieval=True))
        with patch('bridge.objective_head.scan') as scan:
            worker.investigate_watches()
        scan.assert_not_called()
        self.assertEqual(self.store.graph.db.execute('SELECT last_corpus FROM objective_watches').fetchone()['last_corpus'], '')
