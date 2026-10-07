"""Composed recipient routing through supported APIs, without providers.

Directory contacts and Store actions are synthetic single-operator fixtures,
not authenticated people, live delivery, or signer-eligibility tests.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import canvas, mcp
from bridge.config import Config
from bridge.routing import rank_for_decision
from bridge.store import Store


class ContactRecipientRoutingTests(OfflineCase):
    repo = 'synthetic/contact-recipients'
    path = 'usage/meter.py'
    question = 'Should synthetic batch requests count toward metered usage?'
    later = 'Should synthetic batch requests count toward metered usage during previews?'
    facts = {'org': 'Synthetic Juniper', 'customer': 'Synthetic Harbor', 'domain': 'metering'}

    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'recipients.db')
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.people = {name: self.store.add_person({'name': name,
                       'email': name.lower().replace(' ', '.') + '@synthetic.invalid'})['id']
                       for name in ('Synthetic Guide', 'Synthetic Respondent', 'Synthetic Successor')}
        for target in ('bridge.llm.Client.complete', 'bridge.llm.Client.complete_json',
                       'bridge.github.GitHubAPI.get', 'bridge.mcp.index_repo',
                       'socket.socket.connect', 'socket.create_connection'):
            guard = patch(target, side_effect=AssertionError('Provider/network calls forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        self.addCleanup(self.check_delivery)
        self.serial = 0
        self.source = self.import_source('SYN-1')
        self.same_namespace = self.import_source('SYN-2')
        self.foreign = self.import_source('SYN-3', 'synthetic-south')

    def check_delivery(self):
        self.assertFalse(self.store.delivery.enabled)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM notifications').fetchone()[0], 0)

    def tool(self, name, args):
        return mcp.call_tool(self.store, name, args)

    def import_source(self, ref, namespace='synthetic-north', **extra):
        return self.tool('bridge_import_record', {'repo': self.repo, 'provider': 'jira',
            'kind': 'jira', 'namespace': namespace, 'external_id': 'object-' + ref, 'ref': ref,
            'title': 'Synthetic usage investigation ' + ref, 'body': 'Open investigation; no policy answer.',
            'status': 'Open', 'updated_at': '2026-10-01T08:00:00+00:00', **extra})['source']

    def link(self, task, source, role='work_item'):
        found = self.tool('bridge_lookup_record', {'repo': self.repo, 'provider': source['provider'],
            'namespace': source['namespace'], 'object_kind': source['kind'], 'external_id': source['external_id']})
        read = self.tool('bridge_get_record', {'repo': self.repo,
            'record_id': found['source']['record_id']})['source']
        return self.tool('bridge_link_work_item', {'repo': self.repo, 'task_id': task,
            'record_id': read['record_id'], 'source_version_id': read['source_version_id'],
            'provider': read['provider'], 'namespace': read['namespace'], 'object_kind': read['kind'],
            'external_id': read['external_id'], 'role': role})

    def task(self, source=None, facts=None):
        self.serial += 1
        task = self.tool('bridge_start_task', {'repo': self.repo, 'title': 'Synthetic usage question',
            'client_key': str(self.serial), 'paths': self.path,
            'facts': json.dumps(self.facts if facts is None else facts)})['task_id']
        if source:
            self.link(task, source)
        return task

    def node(self, task, question=None, owner='', context=''):
        args = {'task_id': task, 'question': question or self.later, 'category': 'billing',
                'paths': self.path, 'client_ref': 'usage-question', 'context': context}
        if owner:
            args['owner_id'] = self.graph.owner_id_for_person(self.people[owner])
            return canvas.add_node(self.store, Config(model_api='none'), args)
        return self.tool('bridge_add_node', args)

    def refer(self, node, target, **extra):
        return self.store.refer(node['node_id'], {'person': self.people[target],
            'scope_kind': 'contact', 'note': 'Synthetic contact referral', **extra})

    def answer(self, node):
        return self.store.answer(node['node_id'], {'answer': 'Exclude synthetic batch requests.',
            'rationale': 'Synthetic single-operator response in the stated scope.'})

    def history(self):
        node = self.node(self.task(self.source), self.question, 'Synthetic Guide')
        self.refer(node, 'Synthetic Respondent')
        self.answer(node)
        rows = self.graph.db.execute("SELECT * FROM authority WHERE source='answer' AND role='knows'").fetchall()
        self.assertEqual(len(rows), 1)  # The actual answer generated the weak category hint.
        return node

    def ranked(self, task, notes=None):
        return rank_for_decision(self.graph, self.repo, self.later, [self.path],
                                 category='billing', task_id=task, notes=notes)

    def snapshot(self):
        # Order by the declared keys on both SQLite and PostgreSQL.
        keys = {'contact_observations': 'id', 'routing_feedback': 'decision_id,person_id',
                'authority': 'id'}
        return {table: [dict(r) for r in self.graph.db.execute(
                    'SELECT * FROM ' + table + ' ORDER BY ' + order)]
                for table, order in keys.items()}

    def test_compatible_same_and_different_work_items_select_the_respondent(self):
        self.history()
        before = self.snapshot()
        for source in (self.source, self.same_namespace):
            with self.subTest(ref=source['ref']):
                task = self.task(source)
                ranked = self.ranked(task)
                self.assertEqual(ranked[0][0], 'Synthetic Respondent')
                self.assertIn('learned first contact', ' '.join(ranked[0][1]))
                node = self.node(task)
                self.assertEqual(node['owner'], 'Synthetic Respondent')
                self.assertFalse(node['authorized'])
        self.assertEqual(self.snapshot(), before)

    def test_conflicting_namespace_selects_no_historical_contact(self):
        self.history()
        before = self.snapshot()
        task = self.task(self.foreign)
        notes = []
        self.assertEqual(self.ranked(task, notes), [])
        self.assertIn('namespace_conflict', ' '.join(notes))
        node = self.node(task)
        self.assertFalse(node.get('owner'))
        self.assertFalse(node['authorized'])
        self.assertEqual(self.snapshot(), before)

    def test_conflicting_material_facts_select_no_historical_contact(self):
        self.history()
        before = self.snapshot()
        for field in ('domain', 'customer'):
            with self.subTest(field=field):
                task = self.task(self.source, {**self.facts, field: 'Synthetic Elsewhere'})
                notes = []
                self.assertEqual(self.ranked(task, notes), [])
                self.assertIn('material_fact_conflict', ' '.join(notes))
                node = self.node(task)
                self.assertFalse(node.get('owner'))
                self.assertFalse(node['authorized'])
        self.assertEqual(self.snapshot(), before)

    def test_missing_scope_clarifies_without_selecting_the_broad_hint(self):
        self.history()
        for source, facts in ((None, self.facts), (self.source, {'org': self.facts['org']})):
            with self.subTest(linked=bool(source)):
                task = self.task(source, facts)
                before = self.graph.db.execute('SELECT facts FROM runs WHERE id=?', (task,)).fetchone()[0]
                self.assertEqual(self.ranked(task), [])
                node = self.node(task)
                self.assertEqual(node['status'], 'needs_scope_clarification')
                self.assertFalse(node.get('owner'))
                self.assertEqual(self.graph.db.execute('SELECT facts FROM runs WHERE id=?', (task,)).fetchone()[0], before)

    def test_current_decline_excludes_respondent_before_successor_answers(self):
        self.history()
        current = self.node(self.task(self.same_namespace))
        self.refer(current, 'Synthetic Successor', contact_outcome='declined',
                   note='I am no longer a suitable first contact for this scope.')
        later = self.task(self.source)
        self.assertNotIn('Synthetic Respondent', [r[0] for r in self.ranked(later)])
        node = self.node(later, self.later + ' During retries?')
        self.assertNotEqual(node.get('owner'), 'Synthetic Respondent')
        self.assertFalse(node['authorized'])

    def test_optout_does_not_reactivate_an_older_incompatible_answer_hint(self):
        self.history()
        temporary = self.node(self.task(self.foreign), self.question + ' During temporary cover?', 'Synthetic Guide')
        self.refer(temporary, 'Synthetic Respondent', scope_kind='none')
        self.answer(temporary)
        before = self.snapshot()
        task = self.task(self.foreign)
        self.assertEqual(self.ranked(task), [])
        node = self.node(task)
        self.assertFalse(node.get('owner'))
        self.assertFalse(node['authorized'])
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.graph.db.execute("SELECT 1 FROM events WHERE decision_id=? AND kind='route_learning_optout'",
                                            (temporary['node_id'],)).fetchone())

    def test_optout_preserves_independent_compatible_history(self):
        self.history()
        temporary = self.node(self.task(self.source), self.question + ' During temporary cover?', 'Synthetic Guide')
        self.refer(temporary, 'Synthetic Successor', scope_kind='none')
        self.answer(temporary)
        node = self.node(self.task(self.same_namespace))
        self.assertEqual(node['owner'], 'Synthetic Respondent')
        self.assertFalse(node['authorized'])

    def test_expired_contact_is_not_reactivated_by_the_broad_hint(self):
        self.history()
        future = (datetime.now(timezone.utc) + timedelta(days=181)).isoformat()
        with patch('bridge.routing_memory.now_iso', return_value=future):
            task = self.task(self.same_namespace)
            self.assertEqual(self.ranked(task), [])
            node = self.node(task)
            self.assertFalse(node.get('owner'))
            self.assertFalse(node['authorized'])

    def test_unknown_answer_hint_provenance_is_not_guessed_from_its_note(self):
        self.store.add_authority({'repo': self.repo, 'person': self.people['Synthetic Respondent'],
            'scope_kind': 'category', 'scope': 'billing', 'role': 'knows', 'source': 'answer',
            'note': 'answered decision unknown-history, which reached them on inference'})
        before = self.snapshot()
        task = self.task(self.source)
        notes = []
        self.assertEqual(self.ranked(task, notes), [])
        self.assertIn('unlinked legacy provenance is not inferred', ' '.join(notes))
        node = self.node(task)
        self.assertFalse(node.get('owner'))
        self.assertEqual(self.snapshot(), before)

    def test_independent_explicit_routing_map_survives_conflicting_contact_scope(self):
        self.history()
        self.store.add_authority({'repo': self.repo, 'person': self.people['Synthetic Respondent'],
            'scope_kind': 'category', 'scope': 'billing', 'role': 'knows', 'source': 'config',
            'note': 'Independent explicitly configured billing contact'})
        task = self.task(self.foreign)
        node = self.node(task)
        self.assertEqual(node['owner'], 'Synthetic Respondent')
        self.assertIn('Independent explicitly configured billing contact', node['owner_evidence'])
        self.assertNotIn('(answer,', node['owner_evidence'])
        self.assertFalse(node['authorized'])

    def test_declared_learned_referral_retains_its_routing_scope(self):
        history = self.node(self.task(self.source), self.question, 'Synthetic Guide')
        self.refer(history, 'Synthetic Respondent', scope_kind='category', scope='billing', role='knows')
        self.answer(history)
        task = self.task(self.foreign)
        node = self.node(task)
        self.assertEqual(node['owner'], 'Synthetic Respondent')
        self.assertIn('(referral,', node['owner_evidence'])
        self.assertFalse(node['authorized'])

    def declared_history(self, source=None, **facts):
        task = self.task(source, {**self.facts, 'work_item': 'SYN-1', **facts})
        node = self.node(task, self.question, 'Synthetic Guide')
        self.refer(node, 'Synthetic Respondent')
        self.answer(node)
        return node

    def test_distinct_declared_linked_items_generalize_contacts_but_not_the_answer(self):
        self.declared_history(self.source)
        before = self.snapshot()
        task = self.task(self.same_namespace, {**self.facts, 'work_item': 'SYN-2'})
        ranked = self.ranked(task)
        self.assertEqual([r[0] for r in ranked], ['Synthetic Respondent', 'Synthetic Guide'])
        reason = ' '.join(ranked[0][1])
        self.assertIn('declared_work_item_match', reason)
        self.assertIn('SYN-1', reason)
        self.assertIn('SYN-2', reason)
        node = self.node(task)
        self.assertEqual(node['owner'], 'Synthetic Respondent')
        self.assertEqual(node['status'], 'predicted')
        self.assertFalse(node['authorized'])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(json.loads(self.graph.db.execute('SELECT facts FROM runs WHERE id=?',
                                                        (task,)).fetchone()[0])['work_item'], 'SYN-2')

    def test_shared_declared_item_strengthens_the_same_contact(self):
        self.declared_history(self.source)
        different = self.ranked(self.task(self.same_namespace, {**self.facts, 'work_item': 'SYN-2'}))
        shared = self.ranked(self.task(self.source, {**self.facts, 'work_item': 'SYN-1'}))
        self.assertEqual(different[0][0], shared[0][0])
        self.assertGreater(shared[0][2], different[0][2])
        self.assertIn('shared_work_item', ' '.join(shared[0][1]))
        self.assertNotIn('shared_work_item', ' '.join(different[0][1]))

    def test_declared_item_namespace_is_not_masked_by_matching_mixed_context(self):
        # Supported imports deliberately attach opposite records as context,
        # so both tasks have equal aggregate provider/namespace sets.
        for other in (self.foreign, self.import_source('SYN-4', provider='generic')):
            with self.subTest(provider=other['provider'], namespace=other['namespace']):
                def context_anchor(task, source):
                    self.import_source(source['ref'], source['namespace'], provider=source['provider'],
                        external_id=source['external_id'], task_id=task, anchor_role='context')

                historical = self.task(self.source, {**self.facts, 'work_item': 'SYN-1'})
                context_anchor(historical, other)
                answered = self.node(historical, self.question, 'Synthetic Guide')
                self.refer(answered, 'Synthetic Respondent')
                self.answer(answered)
                current = self.task(other, {**self.facts, 'work_item': other['ref']})
                context_anchor(current, self.source)
                before = self.snapshot()
                notes = []
                self.assertEqual(self.ranked(current, notes), [])
                self.assertIn('work_item_namespace_conflict', ' '.join(notes))
                self.assertNotIn('declared_work_item_match', ' '.join(notes))
                node = self.node(current)
                self.assertFalse(node.get('owner'))
                self.assertFalse(node['authorized'])
                self.assertEqual(self.snapshot(), before)
                compatible = self.task(self.same_namespace, {**self.facts, 'work_item': 'SYN-2'})
                context_anchor(compatible, other)
                self.assertEqual(self.ranked(compatible)[0][0], 'Synthetic Respondent')
                self.assertEqual(self.node(compatible)['owner'], 'Synthetic Respondent')

    def test_declared_item_generalization_keeps_namespace_material_and_other_facts(self):
        self.declared_history(self.source, region='Synthetic East')
        cases = [(self.foreign, {**self.facts, 'region': 'Synthetic East', 'work_item': 'SYN-3'}, 'namespace_conflict')]
        for field in ('org', 'customer', 'domain', 'region'):
            facts = {**self.facts, 'region': 'Synthetic East', 'work_item': 'SYN-2', field: 'Synthetic Different'}
            cases.append((self.same_namespace, facts, 'material_fact_conflict' if field != 'region' else 'scope_fact_conflict'))
        for source, facts, reason in cases:
            with self.subTest(reason=reason, facts=facts):
                task = self.task(source, facts)
                notes = []
                self.assertEqual(self.ranked(task, notes), [])
                self.assertIn(reason, ' '.join(notes))
                node = self.node(task)
                self.assertFalse(node.get('owner'))
                self.assertFalse(node['authorized'])

    def test_missing_or_unlinked_declared_item_never_relaxes_the_fact_gate(self):
        self.declared_history(self.source)
        cases = [(self.same_namespace, self.facts), (None, {**self.facts, 'work_item': 'SYN-2'}),
                 (self.same_namespace, {**self.facts, 'work_item': 'Not the linked record'})]
        for source, facts in cases:
            with self.subTest(source=bool(source), facts=facts):
                task = self.task(source, facts)
                before = self.snapshot()
                notes = []
                self.assertEqual(self.ranked(task, notes), [])
                self.assertIn('work_item_declaration', ' '.join(notes))
                node = self.node(task)
                self.assertFalse(node.get('owner'))
                self.assertEqual(self.snapshot(), before)

    def test_context_only_and_ambiguous_work_item_anchors_do_not_bind_a_declaration(self):
        self.declared_history(self.source)
        duplicate = self.import_source('SYN-2', external_id='another-object-SYN-2')
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                task = self.task(facts={**self.facts, 'work_item': 'SYN-2'})
                self.link(task, self.same_namespace, role='work_item' if ambiguous else 'context')
                if ambiguous:
                    self.link(task, duplicate)
                notes = []
                self.assertEqual(self.ranked(task, notes), [])
                self.assertIn('current=' + ('ambiguous' if ambiguous else 'unlinked'), ' '.join(notes))
                self.assertFalse(self.node(task).get('owner'))

    def test_unlinked_historical_declaration_is_not_reconstructed_from_current_links(self):
        history = self.declared_history()
        self.link(history['task_id'], self.source)
        current = self.task(self.same_namespace, {**self.facts, 'work_item': 'SYN-2'})
        before = self.snapshot()
        notes = []
        self.assertEqual(self.ranked(current, notes), [])
        self.assertIn('historical=unlinked', ' '.join(notes))
        self.assertFalse(self.node(current).get('owner'))
        self.assertEqual(self.snapshot(), before)

    def test_pinned_work_item_ref_survives_later_source_rename(self):
        self.declared_history(self.source)
        before = self.snapshot()
        self.import_source('RENAMED-1', external_id=self.source['external_id'],
                           updated_at='2026-10-02T08:00:00+00:00')
        current = self.task(self.same_namespace, {**self.facts, 'work_item': 'SYN-2'})
        ranked = self.ranked(current)
        self.assertEqual(ranked[0][0], 'Synthetic Respondent')
        self.assertIn('SYN-1', ' '.join(ranked[0][1]))
        self.assertNotIn('RENAMED-1', ' '.join(ranked[0][1]))
        self.assertEqual(self.node(current)['owner'], 'Synthetic Respondent')
        self.assertEqual(self.snapshot(), before)

    def test_exact_external_ids_can_bind_declared_work_items(self):
        self.declared_history(self.source, work_item=self.source['external_id'])
        current = self.task(self.same_namespace, {**self.facts, 'work_item': self.same_namespace['external_id']})
        self.assertEqual(self.ranked(current)[0][0], 'Synthetic Respondent')
        self.assertEqual(self.node(current)['owner'], 'Synthetic Respondent')

    def test_supplied_anchors_cannot_create_the_declared_item_exception(self):
        self.declared_history(self.source)
        task = self.task(facts={**self.facts, 'work_item': 'SYN-2'})
        for pins in ({}, {'record_id': self.same_namespace['record_id'],
                          'source_version_id': self.same_namespace['source_version_id']}):
            with self.subTest(pins=bool(pins)):
                claimed = {'anchors': [{'provider': 'jira', 'namespace': 'synthetic-north',
                    'object_kind': 'jira', 'external_id': 'object-SYN-2', 'ref': 'SYN-2',
                    'binding': 'task', 'role': 'work_item', **pins}]}
                notes = []
                ranked = rank_for_decision(self.graph, self.repo, self.later, [self.path],
                    category='billing', task_id=task, contact_context=claimed, notes=notes)
                self.assertEqual(ranked, [])
                self.assertIn('current=unlinked', ' '.join(notes))
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM task_source_anchors WHERE task_id=?',
                                             (task,)).fetchone()[0], 0)

    def test_ambiguous_historical_declaration_never_generalizes(self):
        duplicate = self.import_source('SYN-1', external_id='another-object-SYN-1')
        task = self.task(self.source, {**self.facts, 'work_item': 'SYN-1'})
        self.link(task, duplicate)
        node = self.node(task, self.question, 'Synthetic Guide')
        self.refer(node, 'Synthetic Respondent')
        self.answer(node)
        current = self.task(self.same_namespace, {**self.facts, 'work_item': 'SYN-2'})
        notes = []
        self.assertEqual(self.ranked(current, notes), [])
        self.assertIn('historical=ambiguous', ' '.join(notes))
        self.assertFalse(self.node(current).get('owner'))

    def _check_declined_record_author_fallback(self, named):
        # Stored directory reachability only; no Slack credentials or delivery.
        person = self.store.add_person({'name': 'Synthetic Respondent',
            'email': 'synthetic.respondent@synthetic.invalid', 'slack_id': 'USYNTHETICRESPONDENT'})
        self.assertEqual(person['id'], self.people['Synthetic Respondent'])
        self.graph.set_setting('slack_discovery', '1')
        self.import_source('ROUTE-42', author='Synthetic Respondent',
            title='Unrelated investigation' if named else 'Synthetic batch requests and metered usage investigation')
        context = 'See ROUTE-42' if named else ''

        def ranked(task):
            return rank_for_decision(self.graph, self.repo, self.later, [self.path],
                                     category='billing', task_id=task, context=context)

        positive = ranked(self.task(self.source))
        self.assertEqual(positive[0][0], 'Synthetic Respondent')
        self.assertIn('authored', ' '.join(positive[0][1]))
        self.history()
        current = self.node(self.task(self.source))
        self.refer(current, 'Synthetic Successor', contact_outcome='declined')
        task = self.task(self.same_namespace)
        self.assertEqual(ranked(task), [])
        node = self.node(task, self.later + ' During retries?', context=context)
        self.assertFalse(node.get('owner'))
        self.assertFalse(node['authorized'])
        # The decline is scoped, and a separate explicit map keeps precedence.
        self.assertEqual(ranked(self.task(self.foreign))[0][0], 'Synthetic Respondent')
        self.store.add_authority({'repo': self.repo, 'person': self.people['Synthetic Respondent'],
            'scope_kind': 'category', 'scope': 'billing', 'role': 'decides', 'source': 'config',
            'note': 'Independent explicit routing map'})
        fixed = self.node(self.task(self.same_namespace), self.later + ' During staging?', context=context)
        self.assertEqual(fixed['owner'], 'Synthetic Respondent')
        self.assertIn('Independent explicit routing map', fixed['owner_evidence'])
        self.assertFalse(fixed['authorized'])

    def test_scoped_decline_survives_named_record_author_fallback(self):
        self._check_declined_record_author_fallback(named=True)

    def test_scoped_decline_survives_topical_record_author_fallback(self):
        self._check_declined_record_author_fallback(named=False)
