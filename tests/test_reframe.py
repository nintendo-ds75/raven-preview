from test_contract import ContractCase
from bridge import canvas, reframe
from bridge.authz import Actor, Refused
from bridge.store import Invalid


class ReframeTests(ContractCase):
    def test_corrected_question_clears_approval_and_stales_descendant(self):
        task = self.start()
        parent = self.node(task)
        self.answer(parent['node_id'], 'Waive the charge.')
        child = self.node(task, question='What should the invoice show?', parent_id=parent['node_id'])
        self.answer(child['node_id'], 'Show zero.')
        old = self.store.get_decision(parent['node_id'])
        result = reframe.apply(self.store, parent['node_id'], {'question': 'Should only internal test calls be excluded?',
                    'rationale': 'The usage spike mixes real and synthetic traffic.', 'expected_updated_at': old['updated_at']})
        self.assertEqual(result['status'], 'pending')
        self.assertFalse(result['authorized'])
        self.assertEqual(result['answer'], '')
        self.assertEqual(result['signatures'], [])
        self.assertIn(child['node_id'], result['invalidated'])
        dependent = canvas.node_view(self.store, child['node_id'])
        self.assertTrue(dependent['needs_review'])
        self.assertFalse(dependent['authorized'])
        events = self.store.get_decision(parent['node_id'])['events']
        self.assertTrue(any(e['kind'] == 'question_reframed' and 'Waive the charge.' in e['detail'] for e in events))
        with self.assertRaises(Invalid):
            canvas.finish_task(self.store, {'task_id': task})

    def test_agent_and_unrelated_person_cannot_reframe(self):
        task = self.start()
        node = self.node(task)
        data = {'question': 'May we collect more?', 'rationale': 'Change request',
                'expected_updated_at': node['updated_at']}
        other = self.store.graph.add_person('Unrelated', email='other@example.test')
        for actor in [Actor(id=other, name='Agent', kind='agent'), Actor.person(self.store.graph.get_person(other))]:
            with self.assertRaises(Refused):
                reframe.apply(self.store, node['node_id'], data, actor=actor)

    def test_stale_form_and_conflicting_customer_scope_are_refused(self):
        task = self.start()
        node = self.node(task, facts='customer=acme')
        with self.assertRaisesRegex(Invalid, 'conflicts with the task facts'):
            reframe.apply(self.store, node['node_id'], {'question': 'Should customer=globex get a discount?',
                'rationale': 'Different customer', 'expected_updated_at': node['updated_at']})
        self.answer(node['node_id'], 'No.')
        with self.assertRaisesRegex(Invalid, 'changed while'):
            reframe.apply(self.store, node['node_id'], {'question': 'Should internal traffic count?',
                'rationale': 'New detail', 'expected_updated_at': node['updated_at']})

    def test_new_question_does_not_keep_a_prior_rule(self):
        task = self.start()
        node = self.node(task)
        self.answer(node['node_id'], 'Waive the charge.')
        self.store.make_rule(node['node_id'], {'by': 'Wes', 'conditions': '', 'scope': 'same'})
        row = self.store.get_decision(node['node_id'])
        result = reframe.apply(self.store, node['node_id'], {'question': 'Should non-test traffic remain billable?',
                  'rationale': 'Narrow the question', 'expected_updated_at': row['updated_at']})
        self.assertFalse(result['reusable'])
        self.assertFalse(result['authorized'])

    def test_source_only_dependent_loses_its_old_signatures(self):
        original_task = self.start()
        node = self.node(original_task)
        self.answer(node['node_id'], 'Waive the charge.')
        other = self.node(self.start(title='Related task'))
        self.sign(other['node_id'])
        self.assertTrue(canvas.node_view(self.store, other['node_id'])['authorized'])
        current = self.store.get_decision(node['node_id'])
        reframe.apply(self.store, node['node_id'], {'question': 'Should only test traffic be excluded?',
                'rationale': 'Incorrect premise', 'expected_updated_at': current['updated_at']})
        dependent = canvas.node_view(self.store, other['node_id'])
        self.assertFalse(dependent['authorized'])
        self.assertEqual(dependent['signatures'], [])

    def test_mixed_source_and_parent_dependency_chain_is_invalidated(self):
        task = self.start()
        root = self.node(task)
        self.answer(root['node_id'], 'Waive the charge.')
        other_task = self.start(title='Related task')
        middle = self.node(other_task)
        self.sign(middle['node_id'])
        child = self.node(other_task, question='What should the invoice show?', parent_id=middle['node_id'])
        self.answer(child['node_id'], 'Zero.')
        last = self.node(self.start(title='Another task'), question='What should the invoice show?')
        self.sign(last['node_id'])
        current = self.store.get_decision(root['node_id'])
        result = reframe.apply(self.store, root['node_id'], {'question': 'Should internal usage alone be excluded?',
                'rationale': 'Correct the premise', 'expected_updated_at': current['updated_at']})
        for dependent in (middle, child, last):
            self.assertIn(dependent['node_id'], result['invalidated'])
            self.assertFalse(canvas.node_view(self.store, dependent['node_id'])['authorized'])
