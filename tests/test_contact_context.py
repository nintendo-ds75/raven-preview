"""Typed-context contact ranking only: no authority or eligibility conclusions."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from test_contact_learning import ContactFixture
from bridge.routing import rank_for_decision
from bridge.routing_memory import candidates, clarify, record


class ContactContextTests(ContactFixture):
    def confirmation_fixture(self):
        """A normal referred owner reviews a sourced agent answer, unchanged."""
        from bridge.authz import Actor
        did = self.decision(facts={**self.facts, 'work_item': 'SYN-1'})
        task = self.task_for(did)
        item = self.anchor(task)
        self.graph.db.execute('UPDATE runs SET facts=? WHERE id=?',
                              (json.dumps({'environment': 'synthetic-staging'}), task))
        self.store.refer(did, {'person': self.people['ContactBeta'], 'scope_kind': 'contact',
            'note': 'Synthetic coordinator refers this question to its owner.'},
            actor=Actor.person(self.graph.get_person(self.people['ContactAlpha']), kind='session'))
        support = self.graph.upsert_intent(self.repo, 'jira', 'synthetic-north/POLICY-1',
            'Synthetic current widget policy', 'Exclude synthetic load tests.', 'Synthetic Writer',
            '2026-01-01T00:00:00+00:00', metadata={'provider': 'jira', 'namespace': 'synthetic-north',
            'object_kind': 'issue', 'external_id': 'POLICY-1', 'source_sequence': 1})
        self.graph.publish_evidence(did, [
            {'record_id': item['id'], 'source_version_id': item['version_id'], '_evidence_role': 'work_item'},
            {'record_id': support['id'], 'source_version_id': support['version_id']}],
            status='resolved', source='agent', answer='Exclude synthetic load tests.',
            rationale='The current policy supports this answer.', kind='evidence', signoff='required')
        return did, support

    def confirm_owner(self, did, channel='ui', **extra):
        from types import SimpleNamespace
        from bridge import canvas, slack_chat
        from bridge.authz import Actor
        decision = self.store.get_decision(did)
        person = self.graph.get_person(self.people['ContactBeta'])
        actor = Actor.person(person, kind='slack' if channel == 'slack' else 'session')
        data = {'source_evidence': decision['source_revalidation']['pins'],
                'source_decision_pins': decision['source_revalidation']['decision_pins'], **extra}
        if channel == 'slack':
            return slack_chat.apply(SimpleNamespace(store=self.store, channel='slack'), decision, person,
                                    {'kind': 'signoff'}, actor, review_data=data)
        return canvas.sign_off(self.store, did, {'by': person['name'], 'answer': '',
            'expected_updated_at': decision['updated_at'], **data}, actor=actor)

    def confirmed_contacts(self, namespace='synthetic-north', **facts):
        task = self.current(namespace)
        return candidates(self.graph, self.repo, self.question, path=self.path, task_id=task,
            facts={**self.facts, 'environment': 'synthetic-staging', 'work_item': 'SYN-2', **facts})

    def test_owner_confirmation_retains_typed_contact_provenance_without_authorship(self):
        from bridge import context_memory as cm
        did, support = self.confirmation_fixture()
        before = self.store.get_decision(did)
        pins = [cm.pin(edge, edge['role']) for edge in cm.edges(self.graph.db, did)]
        view = self.confirm_owner(did)
        after = self.store.get_decision(did)
        self.assertTrue(view['authorized'])
        self.assertEqual(after['actor_basis'], 'owner')
        self.assertEqual(after['signoff'], 'signed')
        for field in ('answer', 'answered_by', 'source', 'owner_id'):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(after['signed_by'], 'ContactBeta')
        signatures = json.loads(after['signatures'])
        self.assertEqual([row['by'] for row in signatures], ['ContactBeta'])
        self.assertEqual(pins, [cm.pin(edge, edge['role']) for edge in cm.edges(self.graph.db, did)])
        rows = [dict(row) for row in self.graph.db.execute(
            'SELECT * FROM contact_observations WHERE decision_id=? ORDER BY sequence', (did,))]
        self.assertEqual([row['outcome'] for row in rows], ['referred', 'owner_confirmed'])
        terminal = rows[-1]
        self.assertEqual(terminal['to_person_id'], self.people['ContactBeta'])
        self.assertEqual(terminal['from_person_id'], '')
        context = json.loads(terminal['source_context'])
        self.assertEqual(context['task_id'], self.task_for(did))
        self.assertEqual(context['facts'], {'environment': 'synthetic-staging'})
        self.assertEqual(json.loads(terminal['facts']), {**self.facts, 'work_item': 'SYN-1'})
        self.assertEqual({row['external_id'] for row in context['anchors']}, {'SYN-1'})
        version = self.graph.db.execute('SELECT snapshot FROM decision_versions WHERE id=?',
            (context['confirmation']['decision_version_id'],)).fetchone()
        snapshot = json.loads(version['snapshot'])
        self.assertIn(support['version_id'], [row['source_version_id'] for row in snapshot['sources']])
        self.assertEqual(snapshot['decision']['signatures'], after['signatures'])
        self.assertEqual(terminal['source_revision'], context['confirmation']['decision_version_id'])
        self.assertEqual(terminal['created_at'], after['signed_revision'])
        self.assertEqual({row[0]['name']: row[1] for row in self.confirmed_contacts()},
                         {'ContactBeta': 'owner_confirmed', 'ContactAlpha': 'connector'})
        current = self.current()
        with patch('bridge.signals.signal_route', return_value=[]):
            ranked = rank_for_decision(self.graph, self.repo, self.question, [self.path], task_id=current,
                facts={**self.facts, 'environment': 'synthetic-staging', 'work_item': 'SYN-2'})
        self.assertEqual([row[0] for row in ranked], ['ContactBeta', 'ContactAlpha'])
        self.assertIn('confirmed an existing answer', ' '.join(ranked[0][1]))
        self.assertNotIn('answered decision', ' '.join(ranked[0][1]))
        self.assertIn('helpful connector', ' '.join(ranked[1][1]))

    def test_owner_confirmation_slack_and_blank_ui_have_the_same_contact_outcome(self):
        for channel in ('ui', 'slack'):
            with self.subTest(channel=channel):
                did, _ = self.confirmation_fixture()
                self.confirm_owner(did, channel=channel)
                outcomes = [row['outcome'] for row in self.graph.db.execute(
                    'SELECT outcome FROM contact_observations WHERE decision_id=? ORDER BY sequence', (did,))]
                self.assertEqual(outcomes, ['referred', 'owner_confirmed'])
                self.assertFalse(self.store.get_decision(did)['answered_by'])

    def confirmation_rows(self, did):
        return [dict(row) for row in self.graph.db.execute(
            'SELECT * FROM contact_observations WHERE decision_id=? ORDER BY sequence', (did,))]

    def revise_confirmation_support(self):
        return self.graph.upsert_intent(self.repo, 'jira', 'synthetic-north/POLICY-1',
            'Synthetic current widget policy', 'Exclude synthetic load tests and internal canaries.',
            'Synthetic Writer', '2026-01-01T00:00:00+00:00', metadata={'provider': 'jira',
            'namespace': 'synthetic-north', 'object_kind': 'issue', 'external_id': 'POLICY-1',
            'source_sequence': 2})

    def test_owner_confirmation_retries_preserve_occurrence_age_and_signatures(self):
        from bridge.routing_memory import observe_owner_confirmation
        did, _ = self.confirmation_fixture()
        self.confirm_owner(did)
        before = self.confirmation_rows(did)
        decision = self.store.get_decision(did)
        later = (datetime.fromisoformat(before[-1]['created_at']) + timedelta(days=179)).isoformat()
        # An exact boundary callback and newly submitted unchanged reviews
        # both retain the original observation, despite new node timestamps.
        with self.graph.transaction() as db:
            observe_owner_confirmation(self.graph, db, before[-1]['source_revision'], later)
        for channel in ('ui', 'slack'):
            with patch('bridge.canvas.now', return_value=later):
                self.confirm_owner(did, channel=channel)
        after = self.store.get_decision(did)
        self.assertNotEqual(after['updated_at'], decision['updated_at'])
        self.assertEqual(after['signatures'], decision['signatures'])
        self.assertEqual(self.confirmation_rows(did), before)
        self.assertEqual(self.graph.count_events('contact_observed', decision_id=did), 2)
        expired = (datetime.fromisoformat(before[-1]['created_at']) + timedelta(days=181)).isoformat()
        with patch('bridge.routing_memory.now_iso', return_value=expired):
            self.assertEqual(self.confirmed_contacts(), [])

    def test_owner_confirmation_equal_times_keep_distinct_source_versions(self):
        did, support = self.confirmation_fixture()
        stamp = datetime.now(timezone.utc).isoformat()
        with patch('bridge.canvas.now', return_value=stamp):
            self.confirm_owner(did)
            changed = self.revise_confirmation_support()
            self.confirm_owner(did)
        rows = [row for row in self.confirmation_rows(did) if row['outcome'] == 'owner_confirmed']
        self.assertEqual(len(rows), 2)
        self.assertEqual([row['created_at'] for row in rows], [stamp, stamp])
        self.assertNotEqual(rows[0]['source_revision'], rows[1]['source_revision'])
        self.assertNotEqual(support['version_id'], changed['version_id'])
        for row, expected in zip(rows, (support['version_id'], changed['version_id'])):
            version = self.graph.db.execute('SELECT snapshot FROM decision_versions WHERE id=?',
                                             (row['source_revision'],)).fetchone()
            pins = json.loads(version['snapshot'])['sources']
            self.assertIn(expected, [pin['source_version_id'] for pin in pins])
        details = {}
        candidates(self.graph, self.repo, self.question, path=self.path, task_id=self.current(),
            facts={**self.facts, 'environment': 'synthetic-staging', 'work_item': 'SYN-2'}, details=details)
        self.assertEqual(details[(self.people['ContactBeta'], 'owner_confirmed', did)]['observation_id'], rows[-1]['id'])
        before = self.confirmation_rows(did)
        self.confirm_owner(did)
        self.assertEqual(self.confirmation_rows(did), before)

    def test_owner_confirmation_stale_version_callback_preserves_current_contact_trail(self):
        from bridge.routing_memory import observe_owner_confirmation
        did, _ = self.confirmation_fixture()
        self.confirm_owner(did)
        original = self.confirmation_rows(did)[-1]
        self.revise_confirmation_support()
        self.confirm_owner(did)
        before = self.confirmation_rows(did)
        self.assertNotEqual(original['source_revision'], before[-1]['source_revision'])
        decision = dict(self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone())
        versions = [dict(row) for row in self.graph.db.execute(
            'SELECT * FROM decision_versions WHERE decision_id=? ORDER BY sequence', (did,))]
        count = self.graph.count_events('contact_observed', decision_id=did)
        # Replay exactly the obsolete version and original occurrence time.
        # This callback must not append old evidence after a newer response.
        with self.graph.transaction() as db:
            observe_owner_confirmation(self.graph, db, original['source_revision'], original['created_at'])
        self.assertEqual(self.confirmation_rows(did), before)
        self.assertEqual(self.graph.count_events('contact_observed', decision_id=did), count)
        self.assertEqual(dict(self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()), decision)
        self.assertEqual([dict(row) for row in self.graph.db.execute(
            'SELECT * FROM decision_versions WHERE decision_id=? ORDER BY sequence', (did,))], versions)
        details = {}
        candidates(self.graph, self.repo, self.question, path=self.path, task_id=self.current(),
            facts={**self.facts, 'environment': 'synthetic-staging', 'work_item': 'SYN-2'}, details=details)
        self.assertEqual(details[(self.people['ContactBeta'], 'owner_confirmed', did)]['observation_id'], before[-1]['id'])

    def test_owner_confirmation_equal_time_task_facts_keep_each_material_transition(self):
        did, _ = self.confirmation_fixture()
        stamp = datetime.now(timezone.utc).isoformat()
        scopes = ('synthetic-staging', 'synthetic-production', 'synthetic-staging')
        states = []
        with patch('bridge.canvas.now', return_value=stamp):
            for environment in scopes:
                self.graph.db.execute('UPDATE runs SET facts=? WHERE id=?',
                    (json.dumps({'environment': environment}), self.task_for(did)))
                self.confirm_owner(did)
                states.append(dict(self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()))
        self.assertTrue(all(state == states[0] for state in states))
        rows = [row for row in self.confirmation_rows(did) if row['outcome'] == 'owner_confirmed']
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({row['source_revision'] for row in rows}), 1)
        self.assertEqual(len({row['id'] for row in rows}), 3)
        self.assertEqual([row['created_at'] for row in rows], [stamp] * 3)
        contexts = [json.loads(row['source_context']) for row in rows]
        self.assertEqual([context['facts']['environment'] for context in contexts], list(scopes))
        self.assertEqual([context['confirmation']['previous_observation_id'] for context in contexts],
                         ['', rows[0]['id'], rows[1]['id']])
        self.assertEqual(contexts[0]['confirmation']['response_fingerprint'], contexts[2]['confirmation']['response_fingerprint'])
        self.assertNotEqual(contexts[0]['confirmation']['response_fingerprint'], contexts[1]['confirmation']['response_fingerprint'])
        for row, context in zip(rows, contexts):
            self.assertEqual(row['source_revision'], context['confirmation']['decision_version_id'])
            self.assertIsNotNone(self.graph.db.execute('SELECT id FROM decision_versions WHERE id=?',
                                                      (row['source_revision'],)).fetchone())
        details = {}
        candidates(self.graph, self.repo, self.question, path=self.path, task_id=self.current(),
            facts={**self.facts, 'environment': 'synthetic-staging', 'work_item': 'SYN-2'}, details=details)
        self.assertEqual(details[(self.people['ContactBeta'], 'owner_confirmed', did)]['observation_id'], rows[-1]['id'])
        self.assertEqual(self.confirmed_contacts(environment='synthetic-production'), [])
        before = self.confirmation_rows(did)
        later = (datetime.fromisoformat(stamp) + timedelta(days=179)).isoformat()
        with patch('bridge.canvas.now', return_value=later):
            self.confirm_owner(did)
        self.assertEqual(self.confirmation_rows(did), before)

    def test_owner_confirmation_equal_time_task_anchor_change_keeps_new_context(self):
        did, _ = self.confirmation_fixture()
        stamp = datetime.now(timezone.utc).isoformat()
        with patch('bridge.canvas.now', return_value=stamp):
            self.confirm_owner(did)
            before = self.store.get_decision(did)
            extra = self.anchor(self.task_for(did), external_id='CTX-2', role='context')
            self.confirm_owner(did)
        after = self.store.get_decision(did)
        for field in ('updated_at', 'answer', 'answered_by', 'source', 'facts', 'signatures'):
            self.assertEqual(after[field], before[field], field)
        rows = [row for row in self.confirmation_rows(did) if row['outcome'] == 'owner_confirmed']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['source_revision'], rows[1]['source_revision'])
        self.assertNotEqual(rows[0]['id'], rows[1]['id'])
        self.assertEqual([row['created_at'] for row in rows], [stamp, stamp])
        contexts = [json.loads(row['source_context']) for row in rows]
        self.assertNotIn(extra['id'], [anchor['record_id'] for anchor in contexts[0]['anchors']])
        self.assertIn(extra['id'], [anchor['record_id'] for anchor in contexts[1]['anchors']])
        current = self.current()
        self.anchor(current, external_id='CTX-2', role='context')
        details = {}
        candidates(self.graph, self.repo, self.question, path=self.path, task_id=current,
            facts={**self.facts, 'environment': 'synthetic-staging', 'work_item': 'SYN-2'}, details=details)
        match = details[(self.people['ContactBeta'], 'owner_confirmed', did)]
        self.assertEqual(match['observation_id'], rows[-1]['id'])
        self.assertIn('shared_source: jira:synthetic-north:issue:CTX-2', match['reasons'])
        before = self.confirmation_rows(did)
        self.confirm_owner(did)
        self.assertEqual(self.confirmation_rows(did), before)

    def test_owner_confirmation_keeps_namespace_material_and_review_filters(self):
        did, _ = self.confirmation_fixture()
        self.confirm_owner(did)
        self.assertTrue(self.confirmed_contacts())
        self.assertEqual(self.confirmed_contacts(namespace='synthetic-south'), [])
        for key in ('org', 'customer', 'domain', 'environment'):
            with self.subTest(key=key):
                self.assertEqual(self.confirmed_contacts(**{key: 'Explicit-other-scope'}), [])
        self.revise_confirmation_support()
        self.assertTrue(self.store.get_decision(did)['needs_review'])
        self.assertEqual(self.confirmed_contacts(), [])

    def test_owner_confirmation_snapshots_task_facts_without_rewriting_prior_chain(self):
        did, _ = self.confirmation_fixture()
        self.confirm_owner(did)
        before = self.confirmation_rows(did)
        self.graph.db.execute('UPDATE runs SET facts=? WHERE id=?',
            (json.dumps({'environment': 'synthetic-production'}), self.task_for(did)))
        self.confirm_owner(did)
        after = self.confirmation_rows(did)
        self.assertEqual(after[:2], before)
        self.assertEqual(len(after), 3)
        self.assertEqual(json.loads(after[-1]['source_context'])['facts'], {'environment': 'synthetic-production'})
        self.assertEqual(self.confirmed_contacts(), [])
        self.assertEqual({row[0]['name']: row[1] for row in self.confirmed_contacts(environment='synthetic-production')},
                         {'ContactBeta': 'owner_confirmed'})

    def test_owner_confirmation_latest_contrary_and_optout_stay_effective(self):
        did, _ = self.confirmation_fixture()
        self.confirm_owner(did)
        before = self.confirmation_rows(did)
        negative = self.decision(owner='ContactBeta', facts={**self.facts, 'work_item': 'SYN-3',
                                                           'environment': 'synthetic-staging'})
        self.anchor(self.task_for(negative), external_id='SYN-3')
        self.refer(negative, 'ContactGamma', contact_outcome='declined')
        self.assertEqual({row[0]['name']: row[1] for row in self.confirmed_contacts()}['ContactBeta'], 'declined')
        self.graph.append_event('route_learning_optout', {'decision_id': negative})
        self.assertEqual({row[0]['name']: row[1] for row in self.confirmed_contacts()}['ContactBeta'], 'owner_confirmed')
        self.graph.append_event('route_learning_optout', {'decision_id': did})
        self.assertEqual(self.confirmed_contacts(), [])
        self.assertEqual(self.confirmation_rows(did), before)

    def test_owner_confirmation_stale_review_keeps_no_terminal_observation(self):
        from bridge.store import Invalid
        did, _ = self.confirmation_fixture()
        review = self.store.get_decision(did)
        self.revise_confirmation_support()
        before = dict(self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone())
        observations = self.confirmation_rows(did)
        versions = [dict(row) for row in self.graph.db.execute('SELECT * FROM decision_versions WHERE decision_id=? ORDER BY sequence', (did,))]
        with self.assertRaises(Invalid):
            self.confirm_owner(did, source_evidence=review['source_revalidation']['pins'])
        self.assertEqual(dict(self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()), before)
        self.assertEqual(self.confirmation_rows(did), observations)
        self.assertEqual([dict(row) for row in self.graph.db.execute('SELECT * FROM decision_versions WHERE decision_id=? ORDER BY sequence', (did,))], versions)
        self.assertEqual(self.confirmed_contacts(), [])

    def test_owner_confirmation_storage_failure_rolls_back_the_successful_writer(self):
        did, _ = self.confirmation_fixture()
        before = dict(self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone())
        observations = self.confirmation_rows(did)
        versions = [dict(row) for row in self.graph.db.execute('SELECT * FROM decision_versions WHERE decision_id=? ORDER BY sequence', (did,))]
        original = self.graph.append_event
        def fail(kind, payload, **kwargs):
            result = original(kind, payload, **kwargs)
            if kind == 'contact_observed':
                raise RuntimeError('Synthetic confirmation observation failure')
            return result
        with patch.object(self.graph, 'append_event', side_effect=fail), self.assertRaises(RuntimeError):
            self.confirm_owner(did)
        self.assertEqual(dict(self.graph.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()), before)
        self.assertEqual(self.confirmation_rows(did), observations)
        self.assertEqual([dict(row) for row in self.graph.db.execute('SELECT * FROM decision_versions WHERE decision_id=? ORDER BY sequence', (did,))], versions)
        self.assertEqual(self.graph.count_events('signoff', decision_id=did), 0)
        self.assertIsNone(self.graph.db.execute("SELECT 1 FROM routing_feedback WHERE decision_id=? AND outcome='answered'", (did,)).fetchone())

    def test_owner_confirmation_slack_outer_transaction_rollback_keeps_no_completion(self):
        did, _ = self.confirmation_fixture()
        before = self.confirmation_rows(did)
        with self.assertRaises(RuntimeError), self.graph.transaction():
            self.confirm_owner(did, channel='slack')
            self.assertEqual(self.confirmation_rows(did)[-1]['outcome'], 'owner_confirmed')
            raise RuntimeError('Synthetic outer readback rollback')
        self.assertEqual(self.confirmation_rows(did), before)
        self.assertEqual(self.store.get_decision(did)['signoff'], 'required')
        self.assertIsNone(self.graph.db.execute("SELECT 1 FROM routing_feedback WHERE decision_id=? AND outcome='answered'", (did,)).fetchone())

    def test_owner_confirmation_missing_scope_requests_clarification(self):
        did, _ = self.confirmation_fixture()
        self.confirm_owner(did)
        task = self.current()
        result = clarify(self.graph, task, self.repo, self.question, 'owner-missing-scope', path=self.path,
                         facts={'work_item': 'SYN-2'})
        self.assertEqual(result['scope_clarifications'][0]['prior_contact'], 'ContactBeta')
        self.assertEqual(result['scope_clarifications'][0]['decision_id'], did)
        self.assertIsNone(clarify(self.graph, task, self.repo, self.question, 'owner-complete-scope', path=self.path,
            facts={**self.facts, 'environment': 'synthetic-staging', 'work_item': 'SYN-2'}))

    def task_for(self, did):
        return self.graph.db.execute('SELECT run_id FROM decisions WHERE id=?', (did,)).fetchone()['run_id']

    def anchor(self, task, namespace='synthetic-north', external_id='SYN-1', provider='jira', role='work_item'):
        from bridge.context_memory import add_anchor
        source = self.graph.upsert_intent(self.repo, provider, f'{namespace}/{external_id}', 'Synthetic scope item', '',
            'Synthetic Reporter', '2026-01-01T00:00:00+00:00', metadata={'provider': provider,
            'namespace': namespace, 'object_kind': 'issue', 'external_id': external_id})
        add_anchor(self.graph.db, task, source['id'], source['version_id'], role=role)
        return source

    def history(self, namespace='synthetic-north'):
        did = self.decision()
        self.anchor(self.task_for(did), namespace)
        self.answer(did)
        return did

    def current(self, namespace='synthetic-north', external_id='SYN-2'):
        task = self.graph.create_task('Typed context current task', repo=self.repo)
        if namespace:
            self.anchor(task, namespace, external_id)
        return task

    def learned(self, task='', **kwargs):
        return candidates(self.graph, self.repo, self.question, path=self.path, facts=self.facts,
                          task_id=task, **kwargs)

    def ranked(self, task='', **kwargs):
        with patch('bridge.signals.signal_route', return_value=[]):
            return rank_for_decision(self.graph, self.repo, self.question, [self.path], facts=self.facts,
                                     task_id=task, **kwargs)

    def test_namespace_conflict_rejects_contact_despite_identical_question(self):
        self.history()
        self.assertEqual(self.learned(self.current('synthetic-south')), [])
        notes = []
        self.assertEqual(self.ranked(self.current('synthetic-west'), notes=notes), [])
        self.assertIn('namespace_conflict', ' '.join(notes))

    def test_different_work_item_in_same_namespace_generalizes(self):
        did = self.history()
        ranked = self.ranked(self.current())
        self.assertEqual(ranked[0][0], 'ContactAlpha')
        self.assertIn('compatible_namespace', ' '.join(ranked[0][1]))
        self.assertIn(did, ' '.join(ranked[0][1]))

    def test_shared_work_item_strengthens_match_and_cites_chain(self):
        did = self.history()
        other = self.ranked(self.current())
        shared = self.ranked(self.current(external_id='SYN-1'))
        self.assertGreater(shared[0][2], other[0][2])
        evidence = ' '.join(shared[0][1])
        self.assertIn('shared_work_item', evidence)
        self.assertIn('jira:synthetic-north:issue:SYN-1', evidence)
        observation = self.graph.db.execute('SELECT id FROM contact_observations WHERE decision_id=?', (did,)).fetchone()['id']
        self.assertIn(observation, evidence)

    def test_missing_namespace_requires_clarification_without_copying_history(self):
        self.history()
        task = self.current(namespace='')
        result = clarify(self.graph, task, self.repo, self.question, 'typed-scope', path=self.path, facts=self.facts)
        self.assertIsNotNone(result)
        self.assertIn('jira', result['scope_clarifications'][0]['missing_source_namespaces'])
        self.assertEqual(self.graph.db.execute('SELECT COUNT(*) AS n FROM task_source_anchors WHERE task_id=?', (task,)).fetchone()['n'], 0)
        self.assertEqual(self.graph.db.execute('SELECT facts FROM runs WHERE id=?', (task,)).fetchone()['facts'], '')

    def test_readable_canonical_context_needs_no_record_ids(self):
        self.history()
        context = {'anchors': [{'provider': 'jira', 'namespace': 'synthetic-north',
                              'object_kind': 'issue', 'external_id': 'SYN-2', 'role': 'work_item'}]}
        self.assertEqual(self.learned(contact_context=context)[0][0]['name'], 'ContactAlpha')
        context['anchors'][0]['namespace'] = 'synthetic-south'
        self.assertEqual(self.learned(contact_context=context), [])

    def test_shared_source_never_overrides_other_namespace_conflict(self):
        did = self.decision()
        task = self.task_for(did)
        self.anchor(task)
        self.anchor(task, 'shared-docs', 'DOC-1', provider='docs', role='context')
        self.answer(did)
        current = self.current('synthetic-south')
        self.anchor(current, 'shared-docs', 'DOC-1', provider='docs', role='context')
        self.assertEqual(self.learned(current), [])

    def test_legacy_unknown_context_is_visible_at_lower_confidence(self):
        did = self.decision()
        record(self.graph, did, self.people['ContactAlpha'], 'answered')
        current = self.current()
        legacy = self.ranked(current)
        self.assertIn('typed_context_unknown', ' '.join(legacy[0][1]))
        self.assertIn('legacy', ' '.join(legacy[0][1]))
        self.history()
        known = self.ranked(current)
        self.assertGreater(known[0][2], legacy[0][2])

    def test_org_alias_and_typed_material_conflicts(self):
        self.history()
        current = self.current()
        alias_facts = {key: value for key, value in self.facts.items() if key != 'org'}
        alias_facts['organization'] = self.facts['org']
        self.assertTrue(candidates(self.graph, self.repo, self.question, path=self.path, facts=alias_facts, task_id=current))
        for field in ('organization', 'customer', 'domain'):
            with self.subTest(field=field):
                self.assertEqual(self.learned(current, contact_context={'facts': {field: 'Explicit-other-scope'}}), [])

    def test_typed_scope_change_inside_chain_does_not_credit_earlier_connector(self):
        did = self.decision()
        task = self.task_for(did)
        self.anchor(task)
        self.refer(did, 'ContactBeta')
        self.graph.db.execute('DELETE FROM task_source_anchors WHERE task_id=?', (task,))
        self.anchor(task, 'synthetic-south')
        self.refer(did, 'ContactGamma')
        self.answer(did)
        self.assertEqual({r[0]['name'] for r in self.learned(self.current('synthetic-south'))}, {'ContactBeta', 'ContactGamma'})

    def test_shared_context_source_has_a_bounded_cited_bonus(self):
        did = self.decision()
        self.anchor(self.task_for(did), role='context')
        self.answer(did)
        current = self.current()
        other = self.ranked(current)
        self.anchor(current, external_id='SYN-1', role='context')
        shared = self.ranked(current)
        self.assertGreater(shared[0][2], other[0][2])
        self.assertIn('shared_source', ' '.join(shared[0][1]))
        self.assertNotIn('shared_work_item', ' '.join(shared[0][1]))

    def test_explicit_negative_does_not_cross_a_known_namespace(self):
        self.history()
        did = self.decision()
        self.anchor(self.task_for(did), 'synthetic-south')
        self.refer(did, 'ContactBeta', contact_outcome='declined')
        self.assertEqual({r[0]['name']: r[1] for r in self.learned(self.current())}['ContactAlpha'], 'answered')
        self.assertEqual({r[0]['name']: r[1] for r in self.learned(self.current('synthetic-south'))}['ContactAlpha'], 'declined')

    def test_unknown_historical_negative_does_not_suppress_current_typed_contact(self):
        did = self.decision()
        self.refer(did, 'ContactBeta', contact_outcome='declined')
        notes = []
        with patch('bridge.signals.signal_route', return_value=[('ContactAlpha', ['Independent fixture contact'], .2)]):
            ranked = rank_for_decision(self.graph, self.repo, self.question, [self.path], facts=self.facts,
                                      task_id=self.current(), notes=notes)
        self.assertEqual([r[0] for r in ranked], ['ContactAlpha'])
        self.assertIn('typed_context_unknown', ' '.join(notes))

    def test_current_decision_context_edges_reach_the_contact_matcher(self):
        from bridge.context_memory import attach
        self.history()
        did = self.decision(owner='ContactDelta')
        task = self.task_for(did)
        source = self.anchor(task, 'synthetic-south')
        self.graph.db.execute('DELETE FROM task_source_anchors WHERE task_id=?', (task,))
        attach(self.graph.db, did, [{'record_id': source['id'], 'source_version_id': source['version_id'], 'role': 'context'}])
        with patch('bridge.signals.signal_route', return_value=[]):
            self.assertEqual(rank_for_decision(self.graph, self.repo, self.question, [self.path], facts=self.facts,
                                              decision_id=did), [])
        source = self.anchor(task)
        attach(self.graph.db, did, [{'record_id': source['id'], 'source_version_id': source['version_id'], 'role': 'context'}], replace=True)
        with patch('bridge.signals.signal_route', return_value=[]):
            self.assertEqual(rank_for_decision(self.graph, self.repo, self.question, [self.path], facts=self.facts,
                                              decision_id=did)[0][0], 'ContactAlpha')

    def test_prose_urls_do_not_fill_missing_material_facts(self):
        self.history()
        self.assertEqual(candidates(self.graph, self.repo, self.question, path=self.path, facts={},
            task_id=self.current(), context='https://synthetic.invalid/Synthetic-North/Synthetic-Aster/metering'), [])

    def test_conflicting_material_aliases_fail_closed(self):
        self.history()
        self.assertEqual(candidates(self.graph, self.repo, self.question, path=self.path,
            facts={**self.facts, 'organization': 'Contradictory-synthetic-org'}, task_id=self.current()), [])

    def test_historical_task_facts_are_snapshotted_not_looked_up_later(self):
        did = self.decision(facts={})
        task = self.task_for(did)
        self.graph.db.execute('UPDATE runs SET facts=? WHERE id=?', (json.dumps(self.facts), task))
        self.anchor(task)
        self.answer(did)
        self.graph.db.execute('UPDATE runs SET facts=? WHERE id=?', (json.dumps({**self.facts, 'org': 'Changed-later'}), task))
        self.assertTrue(self.learned(self.current()))
        self.assertEqual(candidates(self.graph, self.repo, self.question, path=self.path,
            facts={**self.facts, 'org': 'Changed-later'}, task_id=self.current()), [])


    def test_changed_task_material_scope_does_not_complete_the_earlier_chain(self):
        did = self.decision(facts={})
        task = self.task_for(did)
        self.anchor(task)
        self.graph.db.execute('UPDATE runs SET facts=? WHERE id=?', (json.dumps(self.facts), task))
        self.refer(did, 'ContactBeta')
        changed = {**self.facts, 'org': 'Synthetic-South'}
        self.graph.db.execute('UPDATE runs SET facts=? WHERE id=?', (json.dumps(changed), task))
        self.refer(did, 'ContactGamma')
        self.answer(did)
        self.assertEqual(self.learned(self.current()), [])
        south = candidates(self.graph, self.repo, self.question, path=self.path, facts=changed, task_id=self.current())
        self.assertEqual({r[0]['name'] for r in south}, {'ContactBeta', 'ContactGamma'})

    def test_historical_alias_conflict_reports_material_exclusion_without_compatibility(self):
        did = self.decision(facts={**self.facts, 'org': 'Contradictory-old-org', 'organization': self.facts['org']})
        self.anchor(self.task_for(did))
        self.answer(did)
        notes = []
        self.assertEqual(self.learned(self.current(), notes=notes), [])
        reasons = ' '.join(notes)
        self.assertIn('material_fact_conflict', reasons)
        self.assertNotIn('compatible_namespace', reasons)
        self.assertNotIn('shared_work_item', reasons)

    def test_missing_provider_clarification_withholds_relationship_credit(self):
        did = self.decision()
        task = self.task_for(did)
        self.anchor(task)
        self.anchor(task, 'synthetic-docs', 'DOC-1', provider='docs', role='context')
        self.answer(did)
        current = self.current(external_id='SYN-1')
        self.assertEqual(self.learned(current), [])
        result = clarify(self.graph, current, self.repo, self.question, 'missing-provider-credit',
                         path=self.path, facts=self.facts)
        reasons = result['scope_clarifications'][0]['match_reasons']
        self.assertIn('missing_source_namespace: docs', reasons)
        self.assertFalse(any(r.startswith(('shared_work_item:', 'shared_source:')) for r in reasons))

    def test_missing_material_clarification_withholds_relationship_credit(self):
        self.history()
        current = self.current(external_id='SYN-1')
        result = clarify(self.graph, current, self.repo, self.question, 'missing-material-credit',
                         path=self.path, facts={})
        reasons = result['scope_clarifications'][0]['match_reasons']
        self.assertIn('missing_material_facts: customer, domain, org', reasons)
        self.assertFalse(any(r.startswith(('shared_work_item:', 'shared_source:')) for r in reasons))
