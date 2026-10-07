"""Typed-context contact ranking only: no authority or eligibility conclusions."""
import json
from unittest.mock import patch

from test_contact_learning import ContactFixture
from bridge.routing import rank_for_decision
from bridge.routing_memory import candidates, clarify, record


class ContactContextTests(ContactFixture):
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
