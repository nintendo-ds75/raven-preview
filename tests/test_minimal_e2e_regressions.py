"""Failures discovered by a real host given a task without supplied paths."""
from unittest.mock import patch
from test_canvas import CanvasCase
from bridge import canvas
from bridge.store import Invalid


class MinimalHostTests(CanvasCase):
    def test_invalid_reference_does_not_publish_or_claim_a_node(self):
        graph = self.store.graph
        reviewer = graph.add_person('Release Reviewer', email='reviewer@minimal.invalid')
        graph.add_authority('path', 'hw/riscv/virt.c', 'approves', person_id=reviewer, repo='qemulike')
        task = self.start('Change default interrupt policy', paths='hw/riscv/virt.c')['task_id']
        parent = canvas.add_node(self.store, self.cfg, {'task_id': task, 'question': 'Who decides the boot policy?', 'paths': 'hw/riscv/virt.c'})
        baseline = self.store.list_decisions()['total']
        for field in ('depends_on', 'parent_id', 'adopt'):
            with self.subTest(field=field):
                data = {'task_id': task, 'question': 'Should this change preserve the existing boot default?',
                        'paths': 'hw/riscv/virt.c', 'client_ref': field, field: 'agent-client-ref-not-a-node-id'}
                with self.assertRaises(Invalid):
                    canvas.add_node(self.store, self.cfg, data)
                self.assertEqual(self.store.list_decisions()['total'], baseline)
                # No dangling in-flight claim may stall the corrected retry.
                self.assertEqual(graph.claim_node_ref(task, field), (True, ''))
                graph.release_node_ref(task, field)
        fixed = canvas.add_node(self.store, self.cfg, {'task_id': task, 'question': 'Should this change preserve the existing boot default?',
            'paths': 'hw/riscv/virt.c', 'client_ref': 'depends_on', 'depends_on': parent['node_id']})
        self.assertIn('Release Reviewer', fixed['required_signers'])
        self.assertFalse(fixed['authorized'])

    def test_wait_returns_required_followup_for_agent_instead_of_waiting_on_itself(self):
        task = self.start('Change default interrupt policy', paths='hw/riscv/virt.c')['task_id']
        node = canvas.add_node(self.store, self.cfg, {'task_id': task, 'question': 'Should we preserve the boot default?'})
        child = canvas.add_followups(self.store, self.cfg, node['node_id'], {
            'questions': ['How will the agent preserve the old configuration?'], 'required': True})['nodes'][0]
        with patch('time.sleep', side_effect=AssertionError('waited for work the agent must do')):
            result = canvas.wait(self.store, {'task_id': task, 'timeout': 30})
        self.assertFalse(result['timed_out'])
        self.assertIn(child['node_id'], [n['node_id'] for n in result['changed']])
        self.assertIn('adopt', result['next'])

    def test_node_wait_returns_answer_that_arrived_before_wait_without_cursor(self):
        task = self.start('Change default interrupt policy', paths='hw/riscv/virt.c')['task_id']
        node = canvas.add_node(self.store, self.cfg, {'task_id': task, 'question': 'Should we preserve the boot default?'})
        self.store.answer(node['node_id'], {'answer': 'Preserve it.', 'rationale': 'Compatibility.',
                                          'signed_by': node['owner']})
        with patch('time.sleep', side_effect=AssertionError('waited for a second unnecessary answer')):
            result = canvas.wait(self.store, {'task_id': task, 'node_id': node['node_id'], 'timeout': 30})
        self.assertFalse(result['timed_out'])
        self.assertEqual(result['changed'][0]['answer'], 'Preserve it.')
        self.assertTrue(result['changed'][0]['authorized'])

    def test_plain_language_compatibility_constraint_engages(self):
        task = self.start('Add optional total elapsed-time budget for retries', paths='hw/riscv/virt.c',
            goal='Add an optional total elapsed-time budget for retries so a slow upstream cannot keep a request retrying indefinitely. Keep existing behavior unless the caller enables it, and cover the interactions with server-requested waits. Include tests and documentation.')
        self.assertEqual(task['verdict'], 'engage', task)
        self.assertTrue(any('keep working' in c['question'] for c in task['candidates']), task['candidates'])
