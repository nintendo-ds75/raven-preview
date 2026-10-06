"""Reconcile kickoff prompts with the live tree without granting authority.

The deep=1 humanize campaign returned fresh-looking kickoff candidates in a
wait after the host had already asked a broad parent covering both topics.
The host copied them to children before reading the complete parent answer.
These are protocol regressions, not a model or synthetic-human quality score.
"""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import canvas
from bridge.config import Config
from bridge.store import Store


PATH = 'src/humanize/number.py'
TITLE = 'Add compact ordinal-range formatter for leaderboard display (Northstar)'
GOAL = ('Add a compact formatter for displaying an inclusive range of ordinal positions in a leaderboard. '
        'This is a bounded downstream adaptation for the synthetic Northstar team, not an upstream request. '
        'The exact public interface and edge-case policy need the current Northstar owner decision.')
BROAD = ('What is the exact public interface (function name, signature, module location) and edge-case policy '
         '(equal start/end, descending/reversed ranges, non-integer or non-finite input, gender/suffix handling) '
         'for a compact ordinal-range formatter for leaderboard display?')
INTERFACE = ('What should the public interface be for the compact ordinal-range formatter '
             '(function name, parameters, return type)?')
EDGES = ('How should the formatter handle edge cases like empty ranges, single positions, '
         'reversed bounds, or out-of-order inputs?')
ANSWER = ('Export humanize.ordinal_range(start, stop, *, separator=" to "). Accept nonnegative integer '
          'endpoints, rejecting bool and all other types with ValueError. Reject a descending range with '
          'ValueError. Equal endpoints collapse to one ordinal; otherwise format both using the existing '
          'localized ordinal() and join using separator. separator must be a string, including an empty '
          'string; otherwise TypeError. The approved default separator is " to ", not a hyphen. Do not '
          'alter ordinal() or locale state. This approval is only for this synthetic Northstar local '
          'evaluation task, not upstream endorsement or a reusable standing rule.')
CFG = Config(model_api='none', deterministic=True)


class DiscoveryGuidanceTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'guidance.db')
        self.graph = self.store.graph
        self.graph.add_person('Nisha Bell')
        self.graph.add_person('Devon Hale')
        self.graph.add_authority('path', 'src/humanize/*', 'decides',
                                 person_id=self.graph.find_person('Nisha Bell')['id'])
        self.task = canvas.start_task(self.store, CFG, {
            'title': TITLE, 'goal': GOAL, 'repo': 'python-humanize/humanize', 'paths': PATH,
            'requester': 'Rowan Exam', 'client_key': 'northstar-ordinal-range'})['task_id']
        self.discovery = {
            'areas': [{'path': PATH}], 'people': [{'name': 'Nisha Bell'}],
            'named_decisions': [{'question': q, 'from': 'The exact public interface and edge-case policy'}
                                for q in (INTERFACE, EDGES)]}
        self.save_discovery()

    def save_discovery(self):
        self.graph.db.execute('UPDATE runs SET discovery=? WHERE id=?',
                              (json.dumps(self.discovery), self.task))

    def node(self, question=BROAD, **extra):
        return canvas.add_node(self.store, CFG, {
            'task_id': self.task, 'question': question, 'paths': PATH, 'category': 'definition', **extra})

    def resume(self):
        return canvas.start_task(self.store, CFG, {'task_id': self.task})

    def test_wait_reconciles_completed_discovery_with_existing_broad_parent(self):
        parent = self.node(context=GOAL)
        self.discovery['model_pending'] = 'running-triage'
        self.save_discovery()

        def complete_discovery(_seconds):
            self.discovery.pop('model_pending', None)
            self.save_discovery()

        result = canvas.wait(self.store, {'task_id': self.task, 'node_id': parent['node_id'], 'timeout': 5},
                             sleep=complete_discovery)
        task = result['task']
        # Mere overlap does not silently eliminate possibly distinct decisions.
        self.assertEqual([c['question'] for c in task['candidates']], [INTERFACE, EDGES])
        self.assertEqual(task['represented_candidates'], [])
        self.assertEqual(task['existing_nodes'][0]['question'], BROAD)
        self.assertFalse(task['existing_nodes'][0]['authorized'])
        self.assertIn('Reconcile', task['next'])
        self.assertIn('before adding more questions', task['next'])
        self.assertNotIn('write the ones that are real', task['next'])
        self.assertEqual(len(self.graph.decisions_for_task(self.task)), 1)

    def test_exact_same_scope_candidate_is_a_reference_not_new_work(self):
        node = self.node(INTERFACE, context=GOAL)
        # Both supported resume routes must reconcile the live tree.
        resumes = [self.resume(), canvas.start_task(self.store, CFG, {
            'title': TITLE, 'goal': GOAL, 'repo': 'python-humanize/humanize',
            'client_key': 'northstar-ordinal-range'})]
        for task in resumes:
            self.assertEqual([c['question'] for c in task['candidates']], [EDGES])
            self.assertEqual(task['represented_candidates'][0]['node_ids'], [node['node_id']])
            self.assertEqual([c['question'] for c in task['discovery']['named_decisions']], [EDGES])
        # Discovery history is not rewritten by a read.
        saved = json.loads(self.graph.get_task(self.task)['discovery'])
        self.assertEqual(len(saved['named_decisions']), 2)

    def test_wait_includes_answer_that_lands_before_candidate_guidance(self):
        parent = self.node(context=GOAL)
        self.discovery['model_pending'] = 'running-triage'
        self.save_discovery()

        def answer_and_complete_discovery(_seconds):
            self.store.answer(parent['node_id'], {'answer': ANSWER})
            self.discovery.pop('model_pending', None)
            self.save_discovery()

        result = canvas.wait(self.store, {'task_id': self.task, 'node_id': parent['node_id'], 'timeout': 5},
                             sleep=answer_and_complete_discovery)
        existing = result['task']['existing_nodes'][0]
        self.assertEqual(existing['answer'], ANSWER)
        self.assertTrue(existing['authorized'])
        self.assertEqual(result['task']['represented_candidates'], [])
        self.assertIn('Reconcile', result['task']['next'])
        self.assertEqual(self.graph.get_task(self.task)['agent_read_at'], '')

    def test_different_paths_facts_or_named_customer_do_not_suppress_candidates(self):
        for extra in ({'paths': 'docs/number.md'}, {'facts': 'customer=Globex'},
                      {'context': 'For customer Globex, use this interface.'},
                      {'context': 'for customer globex, use this interface.'},
                      {'context': 'Use this interface only in staging.'},
                      {'context': 'Options: for customer Globex only'},
                      {'paths': PATH + ', docs/number.md'}):
            with self.subTest(extra=extra):
                node = self.node(INTERFACE, client_ref=str(extra), **extra)
                task = self.resume()
                self.assertIn(INTERFACE, [c['question'] for c in task['candidates']])
                self.assertEqual(task['represented_candidates'], [])
                self.graph.db.execute("UPDATE decisions SET status='withdrawn' WHERE id=?", (node['node_id'],))

    def test_another_task_or_unadopted_followup_is_not_representation(self):
        node = self.node(INTERFACE)
        self.graph.db.execute("UPDATE decisions SET status='suggested' WHERE id=?", (node['node_id'],))
        self.assertIn(INTERFACE, [c['question'] for c in self.resume()['candidates']])
        other = self.store.add_run({'title': 'Another task', 'repo': 'python-humanize/humanize'})['id']
        self.graph.db.execute("UPDATE decisions SET run_id=?, status='pending' WHERE id=?", (other, node['node_id']))
        self.assertIn(INTERFACE, [c['question'] for c in self.resume()['candidates']])

    def test_parent_answer_visible_on_child_without_copying_authority(self):
        parent = self.node(context=GOAL)
        self.store.refer(parent['node_id'], {'person': 'Devon Hale', 'by': 'Nisha Bell',
                                          'scope_kind': 'this', 'note': 'Cover this question only.'})
        self.store.answer(parent['node_id'], {'answer': ANSWER, 'rationale': 'Task-only contract.'})
        child = self.node('What should the new audit logging behavior be?', parent_id=parent['node_id'],
                          category='ops', facts='customer=Globex', context='For customer Globex only.')
        self.assertEqual(child['parent']['answer'], ANSWER)
        self.assertEqual(child['parent']['signed_by'], 'Devon Hale')
        self.assertTrue(child['parent']['authorized'])
        self.assertEqual(child['owner'], 'Nisha Bell')
        self.assertFalse(child['authorized'])
        self.assertFalse(child['reusable'])
        self.assertEqual(child['signatures'], [])
        self.assertIn('does not authorize this child', child['next'])
        self.assertEqual(child['facts'], {'customer': 'Globex'})
        self.assertEqual(child['parent']['facts'], {})
        self.assertEqual(self.graph.get_task(self.task)['agent_read_at'], '')

    def test_reused_parent_evidence_still_requires_child_signoff(self):
        parent = self.node(context=GOAL)
        self.store.answer(parent['node_id'], {'answer': ANSWER})
        child = self.node('How should the formatter handle edge cases like equal start/end (single position), '
                          'reversed/descending bounds, and non-integer or non-finite input?',
                          category='policy', parent_id=parent['node_id'], context=GOAL)
        self.assertEqual(child['status'], 'resolved')
        self.assertEqual(child['signoff'], 'required')
        self.assertFalse(child['authorized'])
        self.assertTrue(child['blocking'])
        self.assertEqual(child['parent']['answer'], ANSWER)
        self.assertTrue(child['parent']['authorized'])

    def test_wait_changed_child_includes_the_parent_context_its_guidance_names(self):
        parent = self.node(context=GOAL)
        self.store.answer(parent['node_id'], {'answer': ANSWER})
        child = self.node('What monitoring is needed?', parent_id=parent['node_id'], category='ops')
        self.store.answer(child['node_id'], {'answer': 'Record errors only.'})
        result = canvas.wait(self.store, {'task_id': self.task, 'node_id': child['node_id'], 'timeout': 0})
        changed = result['changed'][0]
        self.assertEqual(changed['parent']['node_id'], parent['node_id'])
        self.assertEqual(changed['parent']['answer'], ANSWER)
        self.assertIn('Read parent node ' + parent['node_id'], changed['next'])

    def test_tree_does_not_repeat_a_long_parent_answer_for_every_sibling(self):
        parent = self.node(context=GOAL)
        long_answer = ('A complete task-only contract. ' * 250).strip()
        self.store.answer(parent['node_id'], {'answer': long_answer})
        children = []
        # Isolate response rendering from retrieval: each distinct child still
        # waits on its own decision, with no derived answer or prediction.
        for i in range(24):
            did = self.graph.add_decision(self.task, f'Independent decision {i}?', 'ops', 'pending',
                                          repo='python-humanize/humanize', path=PATH)
            self.graph.update_decision(did, parent_id=parent['node_id'], depth=1, owner='Nisha Bell')
            children.append(did)
        tree = canvas.get_tree(self.store, self.task)
        encoded = json.dumps(tree)
        self.assertEqual(encoded.count(long_answer), 1)
        self.assertLess(len(encoded), len(long_answer) * 10)
        for child in tree['nodes'][0]['children']:
            self.assertEqual(child['parent']['node_id'], parent['node_id'])
            self.assertTrue(child['parent']['authorized'])
            self.assertNotIn('answer', child['parent'])
            self.assertNotIn('context', child['parent'])
        self.assertEqual(canvas.node_view(self.store, children[-1])['parent']['answer'], long_answer)

    def test_current_parent_revision_and_bounds_replace_old_context(self):
        parent = self.node(context=GOAL)
        self.store.answer(parent['node_id'], {'answer': ANSWER, 'applicability': {'requires': {'customer': 'Acme'}}})
        child = self.node('What monitoring is needed?', parent_id=parent['node_id'], category='ops')
        self.store.answer(parent['node_id'], {'answer': 'Revised: keep the existing interface.',
                                            'applicability': {'requires': {'customer': 'Acme'}}})
        seen = canvas.node_view(self.store, child['node_id'])
        self.assertEqual(seen['parent']['answer'], 'Revised: keep the existing interface.')
        self.assertEqual(seen['parent']['applicability'], {'requires': {'customer': 'acme'}})
        self.assertFalse(seen['authorized'])
        self.assertEqual(self.resume()['existing_nodes'][0]['answer'], seen['parent']['answer'])

    def test_unsigned_parent_context_and_additional_child_signer_stay_unsigned(self):
        parent = self.node(context=GOAL)
        canvas.settle_node(self.store, {'task_id': self.task, 'node_id': parent['node_id'],
                                       'answer': ANSWER, 'rationale': 'Proposed, not yet signed.'})
        reviewer = self.graph.add_person('Nora Reviewer')
        self.graph.add_authority('path', 'docs/*', 'approves', person_id=reviewer)
        child = self.node('What should the documentation example show?', parent_id=parent['node_id'],
                          paths='docs/number.md', category='ux')
        self.assertFalse(child['parent']['authorized'])
        self.assertEqual(child['parent']['signoff'], 'required')
        self.assertFalse(child['authorized'])
        self.assertIn('Nora Reviewer', child['required_signers'])
        self.assertEqual(child['signatures'], [])

    def test_closed_task_does_not_advise_new_candidate_writes(self):
        self.graph.db.execute("UPDATE runs SET status='completed' WHERE id=?", (self.task,))
        task = self.resume()
        self.assertEqual(task['candidates'], [])
        self.assertEqual(task['discovery']['named_decisions'], [])
        self.assertIn('closed', task['next'])

    def test_completed_review_wait_keeps_proof_refresh_on_the_same_task(self):
        self.graph.db.execute("UPDATE runs SET status='completed' WHERE id=?", (self.task,))
        tree = {'nodes': [], 'counts': {}, 'notes': [], 'observed_at': '2026-10-06T10:00:00+00:00',
                'verdict': 'engage', 'next': 'no node waits on anyone'}
        running = {**tree, 'review': {'status': 'running'}}
        done = {**tree, 'review': {'status': 'done'}}
        with patch('bridge.canvas.get_tree', side_effect=[running, done]):
            result = canvas.wait(self.store, {'task_id': self.task, 'timeout': 5}, sleep=lambda _: None)
        self.assertEqual(result['task']['status'], 'completed')
        self.assertEqual(result['task']['candidates'], [])
        for guidance in (result['next'], result['task']['next']):
            self.assertIn('bridge_finish_task', guidance)
            self.assertIn('same complete diff and checks', guidance)
            self.assertIn('proof', guidance)
        self.assertIn('this same task', result['task']['next'])


if __name__ == '__main__':
    unittest.main()
