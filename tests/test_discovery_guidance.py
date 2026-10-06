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
from bridge.mcp import call_tool
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


class InferredContactGuidanceTests(OfflineCase):
    """Imported authors are first-contact hints, not verified deciders."""

    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'contacts.db')
        self.graph = self.store.graph
        self.graph.add_person('Contract Reviewer', slack_id='UCONTRACT')
        self.graph.add_person('Review Coordinator', slack_id='UCOORDINATOR')
        self.graph.set_setting('slack_discovery', '1')
        self.repo = 'example/parser'
        self.path = 'src/parser.py'
        self.graph.upsert_artifact(self.repo, self.path)
        self.graph.add_change(self.repo, 'source-snapshot', '2026-09-01T00:00:00+00:00',
                              [self.path], [])
        for ref, author, date, body in (
            ('REQUEST-12', 'Contract Reviewer', '2026-09-01T00:00:00+00:00',
             'Contract Reviewer owns the public interface decision for this change.'),
            ('REVIEW-27', 'Review Coordinator', '2026-09-02T00:00:00+00:00',
             'Review Coordinator can coordinate a delay. Contract Reviewer remains accountable.'),
        ):
            self.store.add_record({'repo': self.repo, 'kind': 'ticket', 'ref': ref,
                'title': 'Parser accessor review', 'body': body, 'author': author,
                'created_at': date, 'status': 'Open', 'resolved': False, 'paths': [self.path]})

    def test_kickoff_does_not_promote_an_imported_author_to_owner(self):
        task = canvas.start_task(self.store, CFG, {
            'title': 'Add a parser accessor', 'repo': self.repo, 'paths': self.path,
            'goal': 'The public interface and edge-case policy need a current human decision.'})
        self.assertEqual(task['verdict'], 'engage')
        # No typed contact role is present: recency still decides this tie.
        people = task['discovery']['people']
        self.assertEqual([(p['name'], p['score']) for p in people],
                         [('Review Coordinator', 0.25), ('Contract Reviewer', 0.25)])
        self.assertIn('Review Coordinator as a first contact', task['why'])
        self.assertIn('confirm who decides or refer', task['why'])
        self.assertNotIn('who owns the area', task['why'])
        node = canvas.add_node(self.store, CFG, {
            'task_id': task['task_id'], 'question': 'What should the public accessor contract be?',
            'paths': self.path, 'category': 'definition'})
        self.assertEqual(node['owner'], 'Review Coordinator')
        self.assertIn('inferred first contact', node['owner_evidence'])
        self.assertFalse(node['authorized'])
        self.assertEqual(node['signatures'], [])
        self.assertEqual(self.graph.authority_rows(), [])

    def test_pass_verdict_also_describes_the_person_as_a_contact(self):
        task = canvas.start_task(self.store, CFG, {
            'title': 'Read the parser', 'goal': 'Inspect the parser implementation.',
            'repo': self.repo, 'paths': self.path})
        self.assertEqual(task['verdict'], 'pass', task['why'])
        self.assertIn('Review Coordinator is a first contact', task['why'])
        self.assertNotIn('owns the area', task['why'])
        self.assertEqual(self.graph.authority_rows(), [])

    def test_unavailable_author_fallback_is_still_only_a_first_contact(self):
        self.graph.set_setting('slack_unavailable', '["UCOORDINATOR"]')
        task = canvas.start_task(self.store, CFG, {
            'title': 'Add a parser accessor', 'repo': self.repo, 'paths': self.path,
            'goal': 'The public interface policy needs a current human decision.'})
        self.assertEqual(task['discovery']['people'][0]['name'], 'Contract Reviewer')
        self.assertIn('Contract Reviewer as a first contact', task['why'])
        self.assertNotIn('who owns the area', task['why'])
        self.assertEqual(self.graph.authority_rows(), [])


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

    def test_correction_after_tree_rows_were_read_remains_unread(self):
        parent = self.node(context=GOAL)
        self.store.answer(parent['node_id'], {'answer': ANSWER})
        child = self.node('What monitoring is needed?', parent_id=parent['node_id'], category='ops')
        self.store.answer(child['node_id'], {'answer': 'Record errors only.'})
        call_tool(self.store, 'bridge_get_tree', {'task_id': self.task})
        original_view = canvas._view
        corrected = False
        replacement = 'Correction: keep the existing API; do not add the formatter.'

        def correct_after_snapshot(row, *args, **kwargs):
            nonlocal corrected
            if row['id'] == parent['node_id'] and not corrected:
                corrected = True
                self.store.answer(parent['node_id'], {'answer': replacement})
            return original_view(row, *args, **kwargs)

        with patch('bridge.canvas._view', side_effect=correct_after_snapshot):
            tree = call_tool(self.store, 'bridge_get_tree', {'task_id': self.task})
        self.assertTrue(corrected)
        self.assertEqual(tree['nodes'][0]['answer'], ANSWER)
        self.assertNotIn('answer', tree['nodes'][0]['children'][0]['parent'])
        self.assertNotIn(replacement, json.dumps(tree))
        current = self.store.get_decision(parent['node_id'])
        self.assertLess(tree['observed_at'], current['updated_at'])
        self.assertIn(parent['node_id'], [n['node_id'] for n in canvas.unread_by_agent(self.store, self.task)])
        # The next complete tree carries the correction and may acknowledge it.
        reread = call_tool(self.store, 'bridge_get_tree', {'task_id': self.task})
        self.assertEqual(reread['nodes'][0]['answer'], replacement)
        self.assertEqual(canvas.unread_by_agent(self.store, self.task), [])

    def test_truncated_resume_does_not_acknowledge_an_omitted_parent_correction(self):
        for i in range(20):
            self.graph.add_decision(self.task, f'Other question {i}?', 'ops', 'pending')
        parent = self.node(context=GOAL)
        self.store.answer(parent['node_id'], {'answer': ANSWER})
        call_tool(self.store, 'bridge_get_tree', {'task_id': self.task})
        observed = self.graph.get_task(self.task)['agent_read_at']
        replacement = 'Correction after the first twenty nodes: preserve the original API.'
        self.store.answer(parent['node_id'], {'answer': replacement})
        resumed = call_tool(self.store, 'bridge_start_task', {'task_id': self.task})
        self.assertEqual(len(resumed['existing_nodes']), 20)
        self.assertFalse(resumed['existing_nodes_complete'])
        self.assertNotIn(replacement, json.dumps(resumed))
        self.assertEqual(self.graph.get_task(self.task)['agent_read_at'], observed)
        self.assertIn(parent['node_id'], [n['node_id'] for n in canvas.unread_by_agent(self.store, self.task)])

    def test_standalone_child_and_node_wait_do_not_acknowledge_the_whole_task(self):
        parent = self.node(context=GOAL)
        self.store.answer(parent['node_id'], {'answer': ANSWER})
        child = self.node('What monitoring is needed?', parent_id=parent['node_id'], category='ops')
        self.store.answer(child['node_id'], {'answer': 'Record errors only.'})
        call_tool(self.store, 'bridge_get_tree', {'task_id': self.task})
        observed = self.graph.get_task(self.task)['agent_read_at']
        replacement = 'Correction: preserve the original API.'
        self.store.answer(parent['node_id'], {'answer': replacement})
        alone = canvas.node_view(self.store, child['node_id'])
        self.assertEqual(alone['parent']['answer'], replacement)
        waited = call_tool(self.store, 'bridge_wait', {
            'task_id': self.task, 'node_id': child['node_id'], 'timeout': '0', 'since': observed})
        self.assertEqual(waited['changed'][0]['parent']['answer'], replacement)
        self.assertEqual(self.graph.get_task(self.task)['agent_read_at'], observed)
        self.assertIn(parent['node_id'], [n['node_id'] for n in canvas.unread_by_agent(self.store, self.task)])

    def _assert_empty_wait_cannot_acknowledge_an_unseen_answer(self, since=None):
        node = self.node()
        self.store.answer(node['node_id'], {'answer': ANSWER})
        args = {'task_id': self.task, 'timeout': '0'}
        if since is not None:
            args['since'] = since
        result = call_tool(self.store, 'bridge_wait', args)
        self.assertEqual(result['changed'], [])
        self.assertEqual(self.graph.get_task(self.task)['agent_read_at'], '')
        self.assertIn(node['node_id'], [n['node_id'] for n in canvas.unread_by_agent(self.store, self.task)])

    def test_whole_wait_without_since_cannot_acknowledge_an_omitted_answer(self):
        self._assert_empty_wait_cannot_acknowledge_an_unseen_answer()

    def test_whole_wait_with_future_since_cannot_acknowledge_an_omitted_answer(self):
        self._assert_empty_wait_cannot_acknowledge_an_unseen_answer('2999-01-01T00:00:00+00:00')

    def test_whole_wait_does_not_acknowledge_a_previous_unread_sibling(self):
        current = self.node('What monitoring should run?', category='ops')
        earlier = self.node('What is the rollout window?', category='rollout')
        self.store.answer(earlier['node_id'], {'answer': 'The rollout window is Tuesday.'})

        def answer_current(_seconds):
            self.store.answer(current['node_id'], {'answer': 'Monitor failures.'})

        result = call_tool(self.store, 'bridge_wait', {'task_id': self.task, 'timeout': '5'},
                           sleep=answer_current)
        self.assertEqual([n['node_id'] for n in result['changed']], [current['node_id']])
        self.assertEqual(self.graph.get_task(self.task)['agent_read_at'], '')
        self.assertIn(earlier['node_id'], [n['node_id'] for n in canvas.unread_by_agent(self.store, self.task)])

    def test_whole_wait_acknowledges_a_genuinely_complete_changed_set(self):
        nodes = [self.node('What monitoring should run?', category='ops'),
                 self.node('What is the rollout window?', category='rollout')]

        def answer_both(_seconds):
            for i, node in enumerate(nodes):
                self.store.answer(node['node_id'], {'answer': f'Task-only decision {i}.'})

        result = call_tool(self.store, 'bridge_wait', {'task_id': self.task, 'timeout': '5'}, sleep=answer_both)
        self.assertEqual({n['node_id'] for n in result['changed']}, {n['node_id'] for n in nodes})
        self.assertTrue(all(n['answer'] for n in result['changed']))
        self.assertEqual(canvas.unread_by_agent(self.store, self.task), [])

    def test_same_timestamp_correction_is_still_unread(self):
        stamp = '2026-10-06T10:00:00.000000+00:00'
        with patch('bridge.store.now', return_value=stamp), patch('bridge.graph.now_iso', return_value=stamp), \
                patch('bridge.canvas.now', return_value=stamp):
            node = self.node()
            self.store.answer(node['node_id'], {'answer': ANSWER})
            first = call_tool(self.store, 'bridge_get_tree', {'task_id': self.task})
            self.assertEqual(canvas.unread_by_agent(self.store, self.task), [])
            self.store.answer(node['node_id'], {'answer': 'Correction at the same timestamp.'})
            self.assertEqual(self.store.get_decision(node['node_id'])['updated_at'], first['observed_at'])
            self.assertIn(node['node_id'], [n['node_id'] for n in canvas.unread_by_agent(self.store, self.task)])

    def test_late_lower_id_human_event_is_still_unread_on_the_same_decision(self):
        node = self.node()
        self.store.answer(node['node_id'], {'answer': ANSWER})
        highest = self.graph.db.execute('SELECT max(id) FROM events').fetchone()[0]
        stamp = '2026-10-06T10:00:00.000000+00:00'
        insert = 'INSERT INTO events(id,decision_id,run_id,kind,detail,created_at) VALUES(?,?,?,?,?,?)'
        # Simulate PostgreSQL allocating N before N+1 but committing N later.
        self.graph.db.execute(insert, (highest + 2, node['node_id'], self.task, 'answer_corrected', '{}', stamp))
        before = call_tool(self.store, 'bridge_get_tree', {'task_id': self.task})['observed_revision'][node['node_id']]
        self.graph.db.execute(insert, (highest + 1, node['node_id'], self.task, 'answer_corrected', '{}', stamp))
        after = canvas.human_revisions(self.store, self.task)[node['node_id']]
        self.assertEqual(after['max_event_id'], before['max_event_id'])
        self.assertEqual(after['event_count'], before['event_count'] + 1)
        self.assertIn(node['node_id'], [n['node_id'] for n in canvas.unread_by_agent(self.store, self.task)])

    def test_human_revision_snapshot_uses_one_grouped_query_for_many_nodes(self):
        for i in range(24):
            did = self.graph.add_decision(self.task, f'Decision {i}?', 'ops', 'pending', owner='Nisha Bell')
            self.store.answer(did, {'answer': f'Task-only answer {i}.'})
        connection = self.graph.db
        statements = []

        class CountedConnection:
            def execute(self, sql, *args):
                statements.append(sql)
                return connection.execute(sql, *args)

        with patch.object(self.graph._local, 'db', CountedConnection()):
            revisions = canvas.human_revisions(self.store, self.task)
        self.assertEqual(len(revisions), 24)
        self.assertEqual(len(statements), 1)
        self.assertIn('GROUP BY decision_id', statements[0])


if __name__ == '__main__':
    unittest.main()
