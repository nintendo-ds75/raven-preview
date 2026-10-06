"""Omitted historical scope never silently changes the person being asked."""
from unittest.mock import patch

from test_delivery import DeliveryCase, CFG
from test_contract import ContractCase
from bridge import canvas
from bridge.authz import Actor
from bridge.routing_memory import pending_scopes
from bridge.store import Invalid, Store


class ScopeClarificationTests(DeliveryCase):
    def source(self):
        first = self.node(self.task(), facts='customer=Acme, release=2026-Q4')
        self.store.answer(first['node_id'], {'answer': 'Exclude internal load tests only.', 'rationale': 'Reviewed scope'},
                          actor=Actor.person(self.graph.get_person(self.wes)))
        self.delivery.deliver_now()
        return first

    def test_missing_scope_is_durable_blocks_finish_and_sends_no_dm(self):
        source = self.source()
        count = len(self.slack.messages)
        task = self.task('Second rollout')
        result = self.node(task, client_ref='usage-policy')
        self.assertEqual(result['status'], 'needs_scope_clarification')
        self.assertFalse(result['authorized'])
        self.assertEqual(result['scope_clarifications'][0]['decision_id'], source['node_id'])
        self.assertEqual(result['scope_clarifications'][0]['missing_keys'], ['customer', 'release'])
        self.assertNotIn('node_id', result)
        self.delivery.deliver_now()
        self.assertEqual(len(self.slack.messages), count)
        with self.assertRaisesRegex(Invalid, 'scope facts'):
            canvas.finish_task(self.store, {'task_id': task})
        with self.assertRaises(Invalid):
            self.store.update_run(task, {'status': 'completed'})
        reopened = Store(self.store.path)
        try:
            self.assertEqual(len(canvas.get_tree(reopened, task)['scope_clarifications']), 1)
            with patch('time.sleep', side_effect=AssertionError('waiting on work the agent must do')):
                self.assertFalse(canvas.wait(reopened, {'task_id': task, 'timeout': '30'})['timed_out'])
        finally:
            reopened.graph.close()

    def test_explicit_scope_retry_preserves_key_and_routes_prior_contact(self):
        self.source()
        task = self.task('Second rollout')
        self.node(task, client_ref='usage-policy')
        again = self.node(task, client_ref='usage-policy', facts='customer=Acme, release=2026-Q4')
        self.assertEqual(again['owner'], 'Wes Chen')
        self.assertFalse(again['authorized'])
        self.assertEqual(pending_scopes(self.graph, task), [])
        retried = self.node(task, client_ref='usage-policy', facts='customer=Acme, release=2026-Q4')
        self.assertEqual(again['node_id'], retried['node_id'])
        self.assertTrue(retried['repeated'])

    def test_current_task_facts_are_inherited_but_prior_facts_are_not(self):
        self.source()
        task = canvas.start_task(self.store, CFG, {'title': 'Third rollout', 'repo': 'acme/platform',
            'paths': 'billing/usage.py', 'facts': 'customer=Acme, release=2026-Q4'})['task_id']
        current = self.node(task)
        self.assertIn('node_id', current)
        self.assertEqual(current['facts'], {'customer': 'Acme', 'release': '2026-Q4'})

    def test_conflicting_customer_never_reuses_scoped_contact(self):
        self.source()
        from bridge.routing_memory import candidates
        task = self.task('Other customer')
        result = self.node(task, facts='customer=Globex, release=2026-Q4')
        self.assertIn('node_id', result)  # independent verified path authority may still route
        self.assertFalse(result['authorized'])
        self.assertFalse(candidates(self.graph, 'acme/platform', result['question'],
                                    facts={'customer': 'Globex', 'release': '2026-Q4'}))

    def test_clarification_client_ref_cannot_be_repurposed(self):
        self.source()
        task = self.task('Second rollout')
        self.node(task, client_ref='usage-policy')
        with self.assertRaisesRegex(Invalid, 'different scope clarification'):
            self.node(task, question='Should unrelated features ship?', client_ref='usage-policy')


class SignedProposalCitationTests(ContractCase):
    def test_agent_proposal_signed_by_human_cites_human_on_reuse(self):
        original_task = self.start()
        source = self.node(original_task)
        self.settle(original_task, source['node_id'], 'Waive the charge.')
        self.sign(source['node_id'])
        reuse = self.node(self.start(title='Second task'))
        self.assertIn('Wes approved this', reuse['evidence'])
        self.assertFalse(reuse['authorized'])
