"""Contact-only observations; unique synthetic people, no providers or authority assertions."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
from bridge.routing_memory import candidates
from bridge.routing import route_ranked
from bridge.store import Store


class ContactFixture(OfflineCase):
    repo = 'contact-fixture/widgets'
    question = 'Should widget load test traffic count toward metered usage?'
    path = 'widgets/meter.py'
    facts = {'customer': 'Synthetic-Aster', 'domain': 'metering', 'org': 'Synthetic-North'}

    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'contacts.db')
        self.graph = self.store.graph
        self.people = {name: self.graph.add_person(name, email=f'{name.lower()}@synthetic.invalid')
                       for name in ('ContactAlpha', 'ContactBeta', 'ContactGamma', 'ContactDelta')}

    def tearDown(self):
        self.graph.close()
        super().tearDown()

    def decision(self, owner='ContactAlpha', **scope):
        task = self.graph.create_task('Synthetic contact question', repo=scope.get('repo', self.repo))
        did = self.graph.add_decision(task, scope.get('question', self.question), 'billing', 'pending',
                                     owner=owner, repo=scope.get('repo', self.repo), path=scope.get('path', self.path))
        self.graph.db.execute('UPDATE decisions SET facts=? WHERE id=?',
                              (json.dumps(scope.get('facts', self.facts)), did))
        return did

    def refer(self, did, person, **extra):
        self.store.refer(did, {'person': self.people[person], 'scope_kind': 'contact',
                              'note': f'Synthetic referral to {person}', **extra})

    def answer(self, did):
        self.store.answer(did, {'answer': 'Exclude synthetic load tests.', 'rationale': 'Synthetic response'})

    def contacts(self, **extra):
        args = {'path': self.path, 'facts': self.facts, **extra}
        return candidates(self.graph, args.pop('repo', self.repo), args.pop('question', self.question), **args)


class ContactLearningTests(ContactFixture):
    def test_completed_chain_retains_helpful_connectors(self):
        did = self.decision()
        self.refer(did, 'ContactBeta')
        self.refer(did, 'ContactGamma')
        self.answer(did)
        found = {row[0]['name']: row[1] for row in self.contacts()}
        self.assertEqual(found, {'ContactAlpha': 'connector', 'ContactBeta': 'connector', 'ContactGamma': 'answered'})
        with patch('bridge.signals.signal_route', return_value=[]):
            ranked = route_ranked(self.graph, self.repo, self.question, path=self.path, facts=self.facts)
        self.assertEqual(ranked[0][0], 'ContactGamma')
        self.assertEqual({r[0] for r in ranked[1:]}, {'ContactAlpha', 'ContactBeta'})

    def test_later_optout_excludes_the_whole_chain(self):
        did = self.decision()
        self.refer(did, 'ContactBeta')
        self.refer(did, 'ContactGamma', scope_kind='none')
        self.answer(did)
        self.assertEqual(self.contacts(), [])

    def test_stale_contact_is_ineligible_without_changing_its_answer(self):
        did = self.decision()
        self.answer(did)
        future = (datetime.now(timezone.utc) + timedelta(days=181)).isoformat()
        with patch('bridge.routing_memory.now_iso', return_value=future):
            self.assertEqual(self.contacts(), [])

    def test_referrals_have_ordered_structured_contact_events(self):
        did = self.decision()
        self.refer(did, 'ContactBeta')
        self.refer(did, 'ContactGamma')
        self.answer(did)
        events = [json.loads(row['detail']) for row in self.graph.db.execute(
            "SELECT detail FROM events WHERE decision_id=? AND kind='contact_observed' ORDER BY id", (did,))]
        self.assertEqual([row['outcome'] for row in events], ['referred', 'referred', 'answered'])
        self.assertEqual([(row['from_person_id'], row['to_person_id']) for row in events[:2]],
                         [(self.people['ContactAlpha'], self.people['ContactBeta']),
                          (self.people['ContactBeta'], self.people['ContactGamma'])])

    def test_pending_chain_has_no_reusable_contact_evidence(self):
        did = self.decision()
        self.refer(did, 'ContactBeta')
        self.assertEqual(self.contacts(), [])


    def test_scope_and_facts_do_not_generalize_contacts(self):
        did = self.decision()
        self.refer(did, 'ContactBeta')
        self.answer(did)
        variants = [{'repo': 'other-fixture/widgets'}, {'path': 'widgets/other.py'},
                    {'question': 'Should credentials be encrypted?'}, {'category': 'security'}, {'facts': {}}]
        variants += [{'facts': {**self.facts, key: 'Synthetic-Elsewhere'}} for key in self.facts]
        for change in variants:
            with self.subTest(change=change):
                self.assertEqual(self.contacts(**change), [])

    def test_changed_scope_does_not_credit_earlier_connectors(self):
        did = self.decision()
        self.refer(did, 'ContactBeta')
        new_facts = {**self.facts, 'org': 'Synthetic-South'}
        self.graph.db.execute('UPDATE decisions SET facts=? WHERE id=?', (json.dumps(new_facts), did))
        self.refer(did, 'ContactGamma')
        self.answer(did)
        self.assertEqual({r[0]['name'] for r in self.contacts(facts=new_facts)}, {'ContactBeta', 'ContactGamma'})
        self.assertEqual(self.contacts(), [])

    def test_explicit_unsuitability_is_not_a_connector_reward(self):
        did = self.decision()
        self.refer(did, 'ContactBeta', contact_outcome='declined', note='Explicitly unsuitable for this scope')
        self.answer(did)
        self.assertEqual({r[0]['name']: r[1] for r in self.contacts()},
                         {'ContactAlpha': 'declined', 'ContactBeta': 'answered'})
        with patch('bridge.signals.signal_route', return_value=[('ContactAlpha', ['inferred fixture'], .5)]):
            ranked = route_ranked(self.graph, self.repo, self.question, path=self.path, facts=self.facts)
        self.assertEqual([r[0] for r in ranked], ['ContactBeta'])

    def test_newer_matching_contrary_evidence_overrides_success(self):
        self.answer(self.decision())
        did = self.decision()
        self.refer(did, 'ContactBeta', contact_outcome='declined')
        self.answer(did)
        self.assertEqual({r[0]['name']: r[1] for r in self.contacts()}['ContactAlpha'], 'declined')
        self.answer(self.decision())
        self.assertEqual({r[0]['name']: r[1] for r in self.contacts()}['ContactAlpha'], 'answered')

    def test_silence_reminders_and_escalations_are_not_contact_outcomes(self):
        did = self.decision()
        for kind in ('reminder', 'escalation', 'conversation'):
            self.graph.append_event(kind, {'decision_id': did, 'text': 'No response yet'})
        self.assertEqual(self.contacts(), [])

    def test_loop_does_not_create_helpful_connectors(self):
        did = self.decision()
        self.refer(did, 'ContactBeta')
        self.refer(did, 'ContactAlpha')
        self.refer(did, 'ContactGamma')
        self.answer(did)
        self.assertEqual([(r[0]['name'], r[1]) for r in self.contacts()], [('ContactGamma', 'answered')])
        self.assertEqual(self.graph.count_events('contact_observed', decision_id=did), 4)

    def test_disconnected_path_does_not_credit_unsuccessful_segment(self):
        did = self.decision()
        self.refer(did, 'ContactBeta')
        # An unrelated assignment breaks the observed referral path.
        self.graph.db.execute('UPDATE decisions SET owner_id=? WHERE id=?',
                              (self.graph.owner_id_for_person(self.people['ContactDelta']), did))
        self.refer(did, 'ContactGamma')
        self.answer(did)
        self.assertEqual({r[0]['name'] for r in self.contacts()}, {'ContactGamma', 'ContactDelta'})

    def test_duplicate_boundary_does_not_refresh_expired_evidence(self):
        from bridge.routing_memory import observe
        did = self.decision()
        self.answer(did)
        original = dict(self.graph.db.execute('SELECT * FROM contact_observations WHERE decision_id=?', (did,)).fetchone())
        decision = dict(self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone())
        future = (datetime.fromisoformat(original['created_at']) + timedelta(days=181)).isoformat()
        with patch('bridge.routing_memory.now_iso', return_value=future), self.graph.transaction():
            observe(self.graph, decision, 'answered', to_person_id=self.people['ContactAlpha'],
                    source_revision=original['source_revision'])
            self.assertEqual(self.contacts(), [])
        self.assertEqual(dict(self.graph.db.execute('SELECT * FROM contact_observations WHERE decision_id=?', (did,)).fetchone()), original)
        self.assertEqual(self.graph.count_events('contact_observed', decision_id=did), 1)

    def test_self_referral_adds_no_contact_occurrence(self):
        did = self.decision()
        self.refer(did, 'ContactBeta')
        original = dict(self.graph.db.execute('SELECT * FROM contact_observations WHERE decision_id=?', (did,)).fetchone())
        self.refer(did, 'ContactBeta')
        self.assertEqual(dict(self.graph.db.execute('SELECT * FROM contact_observations WHERE decision_id=?', (did,)).fetchone()), original)
        self.assertEqual(self.graph.count_events('contact_observed', decision_id=did), 1)

    def test_freshness_includes_exact_boundary_and_rejects_future_dates(self):
        from bridge.routing_memory import CONTACT_FRESH_DAYS
        self.assertEqual(CONTACT_FRESH_DAYS, 180)
        did = self.decision()
        self.answer(did)
        row = self.graph.db.execute('SELECT * FROM contact_observations WHERE decision_id=?', (did,)).fetchone()
        stamp = datetime.fromisoformat(row['created_at'])
        for delta, expected in [(timedelta(0), True), (timedelta(days=180), True),
                                (timedelta(days=180, microseconds=1), False), (timedelta(seconds=-1), False)]:
            with self.subTest(delta=delta), patch('bridge.routing_memory.now_iso', return_value=(stamp + delta).isoformat()):
                self.assertEqual(bool(self.contacts()), expected)
        self.graph.db.execute("UPDATE contact_observations SET created_at='not-a-date' WHERE decision_id=?", (did,))
        self.assertEqual(self.contacts(), [])

    def test_fresher_equally_similar_connectors_rank_first(self):
        from bridge.routing_memory import observe
        now = datetime.now(timezone.utc)
        did = self.decision()
        snapshot = dict(self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone())
        with self.graph.transaction():
            for offset, source, target in [(30, 'ContactAlpha', 'ContactBeta'), (10, 'ContactBeta', 'ContactGamma')]:
                observe(self.graph, snapshot, 'referred', self.people[source], self.people[target],
                        source_revision=(now - timedelta(days=offset)).isoformat())
            observe(self.graph, snapshot, 'answered', to_person_id=self.people['ContactGamma'],
                    source_revision=(now - timedelta(days=1)).isoformat())
        self.assertEqual([r[0]['name'] for r in self.contacts()], ['ContactGamma', 'ContactBeta', 'ContactAlpha'])

    def test_legacy_is_labeled_and_not_reconstructed_or_refreshed(self):
        from bridge.routing_memory import record
        did = self.decision()
        stamp = (datetime.now(timezone.utc) - timedelta(days=179)).isoformat()
        with patch('bridge.routing_memory.now_iso', return_value=stamp):
            record(self.graph, did, self.people['ContactAlpha'], 'answered')
        with patch('bridge.signals.signal_route', return_value=[]):
            ranked = route_ranked(self.graph, self.repo, self.question, path=self.path, facts=self.facts)
        self.assertIn('legacy learned first contact', ranked[0][1][0])
        record(self.graph, did, self.people['ContactAlpha'], 'answered')
        self.assertEqual(self.graph.db.execute('SELECT updated_at FROM routing_feedback WHERE decision_id=?', (did,)).fetchone()['updated_at'], stamp)
        self.assertEqual(self.graph.count_events('contact_observed', decision_id=did), 0)
        with patch('bridge.routing_memory.now_iso', return_value=(datetime.fromisoformat(stamp) + timedelta(days=181)).isoformat()):
            self.assertEqual(self.contacts(), [])

    def test_legacy_optout_and_review_flags_exclude_contacts(self):
        from bridge.routing_memory import record
        did = self.decision()
        record(self.graph, did, self.people['ContactAlpha'], 'answered')
        self.graph.db.execute('UPDATE decisions SET needs_review=1 WHERE id=?', (did,))
        self.assertEqual(self.contacts(), [])
        self.graph.db.execute('UPDATE decisions SET needs_review=0 WHERE id=?', (did,))
        self.graph.append_event('route_learning_optout', {'decision_id': did})
        self.assertEqual(self.contacts(), [])

    def test_scope_clarification_excludes_optout_and_stale_contacts(self):
        from bridge.routing_memory import clarify
        did = self.decision()
        self.answer(did)
        task = self.graph.create_task('Scope clarification fixture', repo=self.repo)
        result = clarify(self.graph, task, self.repo, self.question, 'one', path=self.path)
        self.assertEqual(result['scope_clarifications'][0]['missing_keys'], sorted(self.facts))
        future = (datetime.now(timezone.utc) + timedelta(days=181)).isoformat()
        with patch('bridge.routing_memory.now_iso', return_value=future):
            self.assertIsNone(clarify(self.graph, task, self.repo, self.question, 'two', path=self.path))
        self.graph.append_event('route_learning_optout', {'decision_id': did})
        self.assertIsNone(clarify(self.graph, task, self.repo, self.question, 'three', path=self.path))

    def test_failed_referral_transaction_keeps_no_contact_evidence(self):
        did = self.decision()
        original = self.graph.append_event
        def fail(kind, payload, **kwargs):
            result = original(kind, payload, **kwargs)
            if kind == 'contact_observed':
                raise RuntimeError('Synthetic transaction failure')
            return result
        with patch.object(self.graph, 'append_event', side_effect=fail), self.assertRaises(RuntimeError):
            self.refer(did, 'ContactBeta')
        self.assertEqual(self.graph.count_events('contact_observed', decision_id=did), 0)
        self.assertEqual(self.contacts(), [])
        self.assertEqual(self.graph.db.execute('SELECT COUNT(*) AS n FROM contact_observations').fetchone()['n'], 0)

    def test_failed_answer_transaction_keeps_no_contact_evidence(self):
        did = self.decision()
        original = self.graph.append_event
        def fail(kind, payload, **kwargs):
            result = original(kind, payload, **kwargs)
            if kind == 'contact_observed':
                raise RuntimeError('Synthetic transaction failure')
            return result
        with patch.object(self.graph, 'append_event', side_effect=fail), self.assertRaises(RuntimeError):
            self.answer(did)
        self.assertEqual(self.graph.count_events('contact_observed', decision_id=did), 0)
        self.assertEqual(self.contacts(), [])
        self.assertEqual(self.graph.db.execute('SELECT COUNT(*) AS n FROM contact_observations').fetchone()['n'], 0)


    def test_answer_correction_preserves_the_referral_trail_and_connector_age(self):
        did = self.decision()
        self.refer(did, 'ContactBeta')
        self.answer(did)
        before = {r[0]['name']: r[4] for r in self.contacts()}
        self.answer(did)
        after = {r[0]['name']: r[4] for r in self.contacts()}
        self.assertEqual(set(after), {'ContactAlpha', 'ContactBeta'})
        self.assertEqual(after['ContactAlpha'], before['ContactAlpha'])
        self.assertGreater(after['ContactBeta'], before['ContactBeta'])
        self.assertEqual(self.graph.count_events('contact_observed', decision_id=did), 3)

    def test_observation_retains_typed_work_item_context_and_source_times(self):
        from bridge.context_memory import add_anchor
        did = self.decision()
        task = self.graph.db.execute('SELECT run_id FROM decisions WHERE id=?', (did,)).fetchone()['run_id']
        source = self.graph.upsert_intent(self.repo, 'jira', 'SYN-101', 'Synthetic widget work item', '',
            'Synthetic Reporter', '2026-01-01T00:00:00+00:00', metadata={
                'provider': 'jira', 'namespace': 'synthetic-workspace', 'object_kind': 'issue',
                'external_id': 'SYN-101', 'source_updated_at': '2026-01-02T00:00:00+00:00', 'source_sequence': 7})
        add_anchor(self.graph.db, task, source['id'], source['version_id'])
        self.refer(did, 'ContactBeta')
        row = self.graph.db.execute('SELECT * FROM contact_observations WHERE decision_id=?', (did,)).fetchone()
        context = json.loads(row['source_context'])
        self.assertEqual(context['task_id'], task)
        self.assertEqual(len(context['anchors']), 1)
        anchor = context['anchors'][0]
        self.assertEqual((anchor['binding'], anchor['role'], anchor['provider'], anchor['namespace'], anchor['external_id']),
                         ('task', 'work_item', 'jira', 'synthetic-workspace', 'SYN-101'))
        self.assertEqual(anchor['source_sequence'], 7)
        self.assertEqual(anchor['source_updated_at'], '2026-01-02T00:00:00+00:00')
        self.assertEqual(row['source_revision'], self.store.get_decision(did)['updated_at'])
        event = json.loads(self.graph.db.execute("SELECT detail FROM events WHERE decision_id=? AND kind='contact_observed'", (did,)).fetchone()['detail'])
        self.assertEqual(event['source_context'], context)
        self.assertEqual(event['occurred_at'], row['created_at'])
        self.assertEqual(event['observed_at'], row['observed_at'])


    def test_explicit_declines_override_success_without_a_completed_path(self):
        for mode in ('pending', 'disconnected', 'loop'):
            with self.subTest(mode=mode):
                self.answer(self.decision())
                did = self.decision()
                self.refer(did, 'ContactBeta', contact_outcome='declined')
                if mode == 'disconnected':
                    self.graph.db.execute('UPDATE decisions SET owner_id=? WHERE id=?',
                        (self.graph.owner_id_for_person(self.people['ContactDelta']), did))
                    self.refer(did, 'ContactGamma')
                    self.answer(did)
                elif mode == 'loop':
                    self.refer(did, 'ContactAlpha')
                    self.refer(did, 'ContactGamma')
                    self.answer(did)
                self.assertEqual({r[0]['name']: r[1] for r in self.contacts()}['ContactAlpha'], 'declined')

    def test_pending_decline_still_respects_scope_freshness_and_optout(self):
        did = self.decision()
        self.refer(did, 'ContactBeta', contact_outcome='declined')
        self.assertEqual([(r[0]['name'], r[1]) for r in self.contacts()], [('ContactAlpha', 'declined')])
        self.assertEqual(self.contacts(facts={**self.facts, 'org': 'Other-synthetic-org'}), [])
        future = (datetime.now(timezone.utc) + timedelta(days=181)).isoformat()
        with patch('bridge.routing_memory.now_iso', return_value=future):
            self.assertEqual(self.contacts(), [])
        self.graph.append_event('route_learning_optout', {'decision_id': did})
        self.assertEqual(self.contacts(), [])

    def test_source_sequence_orders_tied_positive_and_negative_occurrences(self):
        from bridge.routing_memory import observe
        did = self.decision()
        snapshot = dict(self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone())
        with self.graph.transaction():
            observe(self.graph, snapshot, 'declined', self.people['ContactAlpha'], self.people['ContactBeta'])
            observe(self.graph, snapshot, 'answered', to_person_id=self.people['ContactAlpha'])
        self.assertEqual([(r[0]['name'], r[1]) for r in self.contacts()], [('ContactAlpha', 'answered')])

    def test_mixed_legacy_answer_is_visible_without_fabricating_connector_success(self):
        from bridge.routing_memory import record
        did = self.decision()
        self.refer(did, 'ContactBeta')
        record(self.graph, did, self.people['ContactBeta'], 'answered')
        self.assertEqual([(r[0]['name'], r[1]) for r in self.contacts()], [('ContactBeta', 'answered')])
        with patch('bridge.signals.signal_route', return_value=[]):
            ranked = route_ranked(self.graph, self.repo, self.question, path=self.path, facts=self.facts)
        self.assertIn('legacy learned first contact', ranked[0][1][0])
        self.assertEqual(self.graph.count_events('contact_observed', decision_id=did), 1)
        self.graph.append_event('route_learning_optout', {'decision_id': did})
        self.assertEqual(self.contacts(), [])

    def test_conversation_decline_is_visible_in_readback_and_preserved_to_apply(self):
        from types import SimpleNamespace
        from bridge.slack_chat import respond, apply
        did = self.decision()
        person = self.graph.get_person(self.people['ContactAlpha'])
        delivery = SimpleNamespace(store=self.store, channel='slack', fallback_channel='',
                                   _reading=lambda *args: None, _stale=lambda *args: None)
        action = {'kind': 'handoff', 'to': 'ContactBeta', 'scope_kind': 'contact', 'contact_outcome': 'declined'}
        with patch('bridge.slack_chat.load', return_value=SimpleNamespace(semantic_retrieval=True)), \
                patch('bridge.slack_chat.validated_reading', return_value=action), \
                patch('bridge.readback.save', side_effect=lambda *args, **kwargs: args[7]) as save:
            result = respond(delivery, {'external_ref': 'SYNTHETIC:1'}, self.store.get_decision(did), person,
                             'I am unsuitable for similar metering questions in this scope; ask ContactBeta.', None)
        self.assertIn('not a suitable first contact for similar questions in this scope', result)
        self.assertIn('contact suggestions only', result)
        self.assertEqual(self.graph.count_events('contact_observed', decision_id=did), 0)
        confirmed_action = json.loads(save.call_args.args[6]['answer'])
        self.assertEqual(confirmed_action['contact_outcome'], 'declined')
        apply(delivery, self.store.get_decision(did), person, confirmed_action, None)
        self.assertEqual([(r[0]['name'], r[1]) for r in self.contacts()], [('ContactAlpha', 'declined')])

    def test_ordinary_and_temporary_conversation_handoffs_do_not_infer_decline(self):
        from types import SimpleNamespace
        from bridge.slack_chat import respond, apply
        for temporary in (False, True):
            with self.subTest(temporary=temporary):
                did = self.decision()
                person = self.graph.get_person(self.people['ContactAlpha'])
                delivery = SimpleNamespace(store=self.store, channel='slack', fallback_channel='',
                                           _reading=lambda *args: None, _stale=lambda *args: None)
                action = {'kind': 'handoff', 'to': 'ContactBeta', 'contact_outcome': 'declined' if temporary else ''}
                message = 'I am on vacation this week; ask ContactBeta.' if temporary else 'Please ask ContactBeta.'
                with patch('bridge.slack_chat.load', return_value=SimpleNamespace(semantic_retrieval=True)), \
                        patch('bridge.slack_chat.validated_reading', return_value=action), \
                        patch('bridge.readback.save', side_effect=lambda *args, **kwargs: args[7]) as save:
                    result = respond(delivery, {'external_ref': f'SYNTHETIC:{did}'}, self.store.get_decision(did), person, message, None)
                self.assertNotIn('not a suitable first contact', result)
                confirmed_action = json.loads(save.call_args.args[6]['answer'])
                apply(delivery, self.store.get_decision(did), person, confirmed_action, None)
                row = self.graph.db.execute('SELECT outcome FROM contact_observations WHERE decision_id=?', (did,)).fetchone()
                self.assertEqual(row['outcome'], 'referred')

    def test_conversation_contact_outcome_schema_is_bounded(self):
        from bridge.config import Config
        from bridge.slack_chat import reading, ReadingShapeError
        for value in ('', None, 'referred', 'declined'):
            with self.subTest(value=value), patch('bridge.slack_chat.Client.complete_json',
                    return_value={'kind': 'handoff', 'to': 'ContactBeta', 'contact_outcome': value}):
                self.assertEqual(reading(Config(model_api='none'), {})['contact_outcome'], value or '')
        for action in ({'kind': 'handoff', 'contact_outcome': 'expert'},
                       {'kind': 'handoff', 'contact_outcome': {'private': 'value'}},
                       {'kind': 'chat', 'contact_outcome': 'declined'}):
            with self.subTest(action=action), patch('bridge.slack_chat.Client.complete_json', return_value=action), \
                    self.assertRaises(ReadingShapeError):
                reading(Config(model_api='none'), {})


    def test_conversation_self_unsuitability_does_not_attach_to_somebody_else(self):
        from types import SimpleNamespace
        from bridge.slack_chat import respond
        did = self.decision()
        speaker = self.graph.get_person(self.people['ContactGamma'])
        delivery = SimpleNamespace(store=self.store, channel='slack', fallback_channel='',
                                   _reading=lambda *args: None, _stale=lambda *args: None)
        action = {'kind': 'handoff', 'to': 'ContactBeta', 'contact_outcome': 'declined'}
        with patch('bridge.slack_chat.load', return_value=SimpleNamespace(semantic_retrieval=True)), \
                patch('bridge.slack_chat.validated_reading', return_value=action), \
                patch('bridge.readback.save', side_effect=lambda *args, **kwargs: args[7]) as save:
            result = respond(delivery, {'external_ref': 'SYNTHETIC:other-speaker'}, self.store.get_decision(did),
                             speaker, 'I am unsuitable for similar questions; please ask ContactBeta.', None)
        self.assertNotIn('not a suitable first contact', result)
        self.assertEqual(json.loads(save.call_args.args[6]['answer'])['contact_outcome'], 'referred')
        self.assertEqual(self.graph.count_events('contact_observed', decision_id=did), 0)
