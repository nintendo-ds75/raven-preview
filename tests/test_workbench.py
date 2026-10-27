"""Browser execution contracts; fake inference, real git and decision graph."""
import json
import tempfile
from pathlib import Path
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from fixtures import OfflineCase, ready_server
from bridge import canvas, workbench_fs as fs
from bridge.auth import Auth, Identity
from bridge.config import Config
from bridge.store import Store, Invalid
from bridge.workbench import Workbench, TOOLS


class ScriptedAgent:
    def __init__(self, *turns):
        self.turns = list(turns)
        self.seen = []

    def respond(self, messages):
        self.seen.append(json.loads(json.dumps(messages)))
        return self.turns.pop(0), {'input_tokens': 10, 'output_tokens': 5}


def call(name, args=None, ident='call-1'):
    return {'type': 'tool_use', 'id': ident, 'name': name, 'input': args or {}}


class WorkbenchTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'policy.py').write_text('limit = 1\n')
        fs.git(self.source, 'init', '--template=', '-b', 'main')
        fs.git(self.source, 'add', '.')
        fs.git(self.source, '-c', 'user.name=Fixture', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'baseline')
        self.store = Store(self.root / 'state.db')
        self.actor = Identity('requester', 'Requester', 'member', 'session')
        self.agent = ScriptedAgent([call('read_file', {'path': 'policy.py'})], [{'type': 'text', 'text': 'What limit should apply?'}])
        self.service = Workbench(self.store, [{'id': 'sample/project', 'path': str(self.source)}], self.root / 'copies',
                                 cfg=Config(model_api='none'), agent=self.agent)

    def submit(self, **data):
        return self.service.submit({'prompt': 'Review the policy change', 'repository': 'sample/project', 'request_key': 'one', **data}, self.actor)['task_id']

    def test_submission_is_durable_idempotent_and_attributed(self):
        task = self.submit()
        self.assertEqual(task, self.submit())
        self.assertEqual(self.service.row(task)['requester_id'], self.actor.id)
        self.assertEqual(len(self.service.list()), 1)
        with self.assertRaises(Invalid):
            self.submit(prompt='Different work')
        self.assertFalse((self.service.root / task).exists())

    def test_snapshot_contains_committed_files_only_and_original_stays_unchanged(self):
        (self.source / 'policy.py').write_text('uncommitted = 42\n')
        (self.source / '.env').write_text('SECRET=not-for-agent\n')
        task = self.submit()
        self.service.step(task)
        copy = self.service.root / task
        self.assertEqual((copy / 'policy.py').read_text(), 'limit = 1\n')
        self.assertFalse((copy / '.env').exists())
        fs.file_tool(copy, 'edit_file', {'path': 'policy.py', 'old': '1', 'new': '2'})
        self.assertIn('+limit = 2', fs.diff(copy))
        self.assertEqual((self.source / 'policy.py').read_text(), 'uncommitted = 42\n')
        self.assertEqual(self.service.get(task)['activity'][-1]['label'], 'read_file')

    def test_path_escape_symlink_and_git_access_are_refused(self):
        for name in ('../outside', '/etc/passwd', '.git/config', '.GiT/config', 'a/../../b', 'a\\b', 'a\x00b', 'ok\x00/../..'):
            with self.subTest(name=name), self.assertRaises(Invalid):
                fs.safe_path(self.source, name)
        (self.source / 'link').symlink_to(self.root)
        with self.assertRaises(Invalid):
            fs.safe_path(self.source, 'link/outside')
        fs.git(self.source, 'add', 'link')
        fs.git(self.source, '-c', 'user.name=Fixture', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'symlink')
        with self.assertRaises(Invalid):
            fs.snapshot(self.source, self.root / 'unsafe')

    def test_snapshot_keeps_deliberately_tracked_ignored_files(self):
        (self.source / '.gitignore').write_text('policy.py\n')
        fs.git(self.source, 'add', '.')
        fs.git(self.source, '-c', 'user.name=Fixture', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'ignore tracked file')
        task = self.submit()
        self.service.step(task)
        fs.file_tool(self.service.root / task, 'edit_file', {'path': 'policy.py', 'old': '1', 'new': '2'})
        self.assertIn('+limit = 2', fs.diff(self.service.root / task))

    def test_resume_records_the_person_who_actually_clicked_it(self):
        task = self.submit()
        teammate = Identity('colleague', 'Colleague', 'member', 'session')
        self.service.control(task, 'pause', self.actor)
        self.service.control(task, 'resume', teammate)
        saved = self.service.get(task)
        self.assertEqual(saved['pending_messages'][0]['actor'], 'Colleague')
        self.assertEqual(saved['activity'][-1]['detail']['actor_id'], 'colleague')

    def test_edit_requires_unique_observed_text(self):
        with self.assertRaises(Invalid):
            fs.file_tool(self.source, 'edit_file', {'path': 'policy.py', 'old': 'not present', 'new': '2'})
        with self.assertRaises(Invalid):
            fs.file_tool(self.source, 'write_file', {'path': 'policy.py', 'previous': None, 'content': '2'})

    def test_agent_cannot_approve_or_mutate_another_task(self):
        task = self.submit()
        with self.assertRaises(Invalid):
            self.service.execute(self.service.row(task), call('bridge_sign_off', {'by': 'Owner'}))
        other = self.submit(request_key='other')
        returned = self.service.execute(self.service.row(task), call('bridge_add_node', {
            'task_id': other, 'question': 'Should the limit be two?', 'paths': 'policy.py'}))
        self.assertEqual(self.store.get_decision(returned['node_id'])['run_id'], task)
        self.assertFalse(returned['authorized'])

    def test_unsigned_decision_cannot_finish(self):
        task = self.submit()
        self.service.step(task)
        node = canvas.add_node(self.store, Config(model_api='none'), {'task_id': task, 'question': 'Who approves two?', 'paths': 'policy.py'})
        self.service.execute(self.service.row(task), call('bridge_get_tree'))
        try:
            result = self.service.execute(self.service.row(task), call('finish_task', {'checks': 'No tests', 'summary': 'Done'}))
            self.assertNotEqual(result.get('status'), 'completed')
        except Invalid:
            pass
        self.assertNotEqual(self.service.row(task)['status'], 'completed')
        self.assertFalse(self.store.get_decision(node['node_id'])['authorized'])

    def test_no_sandbox_never_falls_back_to_host_execution(self):
        task = self.submit()
        with patch('subprocess.run', side_effect=AssertionError('Must not run on host')):
            with self.assertRaises(Invalid):
                self.service.execute(self.service.row(task), call('run_command', {'command': 'echo bad'}))

    def test_finish_discovery_uses_workspace_patch_not_model_transcription(self):
        task = self.submit()
        self.service.step(task)
        fs.file_tool(self.service.root / task, 'edit_file', {'path': 'policy.py', 'old': '1', 'new': '2'})
        spec = next(t for t in TOOLS if t['name'] == 'bridge_review_discovery')
        self.assertNotIn('diff', spec['input_schema']['properties'])
        with patch('bridge.workbench.mcp.call_tool', return_value={}) as invoke:
            self.service.execute(self.service.row(task), call('bridge_review_discovery', {'phase': 'finish', 'diff': 'invented patch', 'observations': 'Investigated policy.py', 'paths': ['policy.py']}))
        sent = invoke.call_args.args[2]
        self.assertEqual(sent['diff'], fs.diff(self.service.root / task))
        self.assertIn('+limit = 2', sent['diff'])
        self.assertTrue(invoke.call_args.kwargs['principal'].is_agent)

    def test_finish_review_completed_before_wait_is_not_lost(self):
        task = self.submit()
        self.service.step(task)
        with patch('bridge.workbench.mcp.call_tool', return_value={'status': 'completed', 'review': {'status': 'running'}}):
            self.service.execute(self.service.row(task), call('finish_task', {'summary': 'Ready', 'checks': 'Not run'}))
        # The current tree has no pending review: never subscribe to its already
        # completed state and wait forever for another transition.
        self.assertNotEqual(self.service.row(task)['status'], 'waiting')

    def test_final_summary_requires_receiving_review_and_rechecks_actual_patch(self):
        task = self.submit()
        self.service.step(task)
        with self.assertRaisesRegex(Invalid, 'successful finish_task'):
            self.service.execute(self.service.row(task), call('present_result', {'summary': 'Done'}))
        self.service.agent = ScriptedAgent([call('finish_task', {'checks': 'Tests passed'}, ident='finish')],
                                          [call('present_result', {'summary': 'Tests passed; model review is advisory.'}, ident='present')])
        self.service.update(task, status='queued')
        with patch('bridge.workbench.mcp.call_tool', return_value={'status': 'completed', 'review': {'status': 'complete', 'concerns': ['Inspect a boundary']}}) as invoke:
            self.service.step(task)
            self.assertEqual(self.service.row(task)['status'], 'queued')
            # Presentation must recheck a changed patch, not blindly reuse success.
            fs.file_tool(self.service.root / task, 'edit_file', {'path': 'policy.py', 'old': '1', 'new': '2'})
            self.service.step(task)
            self.assertEqual(invoke.call_count, 2)
            self.assertIn('+limit = 2', invoke.call_args.args[2]['diff'])
        self.assertIn('Inspect a boundary', json.dumps(self.service.agent.seen[-1]))
        self.assertEqual(self.service.row(task)['status'], 'completed')
        self.assertIn('advisory', self.service.row(task)['summary'])
        with self.assertRaisesRegex(Invalid, 'finished'):
            self.service.execute(self.service.row(task), call('edit_file', {'path': 'policy.py', 'old': '2', 'new': '3'}))

    def test_incomplete_review_pauses_instead_of_publishing_completion(self):
        task = self.submit()
        self.service.step(task)
        with patch('bridge.workbench.mcp.call_tool', return_value={'status': 'completed', 'review': {'status': 'failed'}}):
            result = self.service.execute(self.service.row(task), call('finish_task', {'checks': 'Tests passed'}))
        self.assertEqual(result['status'], 'review_required')
        self.assertFalse(result['finished'])
        self.assertEqual(self.service.row(task)['status'], 'paused')
        self.assertIn('did not complete', self.service.row(task)['error'])

    def test_new_blocker_during_presentation_prevents_ready_status(self):
        task = self.submit()
        self.service.step(task)
        self.service.record(task, 'tool', 'finish_task', {'input': {'checks': 'Tests passed'}, 'result': {'status': 'completed'}})
        canvas.add_node(self.store, self.service.cfg, {'task_id': task, 'question': 'Which policy?', 'paths': 'policy.py'})
        with patch('bridge.workbench.mcp.call_tool', return_value={'status': 'completed'}):
            with self.assertRaisesRegex(Invalid, 'changed during completion'):
                self.service.execute(self.service.row(task), call('present_result', {'summary': 'Done'}))
        self.assertNotEqual(self.service.row(task)['status'], 'completed')

    def test_new_factual_evidence_resumes_same_objective_worker_once(self):
        from bridge import objectives, objective_head
        self.store.graph.db.execute("INSERT INTO people(id,name,role,active) VALUES(?,?,?,1)", (self.actor.id, self.actor.name, 'member'))
        source_data = {'repo': 'sample/project', 'kind': 'note', 'ref': 'pilot', 'body': 'Pilot needs a CSV example.'}
        first = self.store.add_record(source_data)['source']
        objective = objectives.create(self.store, {'request_key': 'owned-continuity', 'title': 'Pilot outcome',
            'outcome': 'Prepare the pilot CSV path', 'success_criteria': 'Customer gets a report',
            'stop_criteria': 'Scope changes', 'guardrails': 'Private preparation only',
            'repository': 'sample/project', 'provider': 'claude', 'source_ids': [first['record_id']]}, self.actor)
        dispatched = objectives.act(self.store, objective['id'], 'dispatch', {'request_key': 'dispatch-owned', 'expected_revision': 1}, self.actor, self.service)
        task = next(r['task_id'] for r in dispatched['runs'] if r['browser_task'])
        self.service.step(task)
        self.service.update(task, status='waiting', wait_revision=self.service.fingerprint(task))
        source = self.store.add_record({**source_data, 'body': 'The accepted CSV header is account_id.'})['source']
        class FactualReader:
            def complete_json(self, purpose, system, prompt, **kwargs):
                if purpose != 'objective factual research':
                    raise AssertionError('A factual source answer must not request new judgment')
                return {'action': 'finish', 'disposition': 'factual_answer', 'reason': 'Header established from the updated source.',
                    'observations': [{'kind': 'fact', 'statement': 'Use the accepted header account_id.', 'citations': [{
                        'record_id': source['record_id'], 'source_version_id': source['source_version_id'],
                        'quote': 'The accepted CSV header is account_id.'}]}]}
        result = objective_head.scan(self.store, {'repository': 'sample/project', 'request_key': 'new-fact'}, self.actor,
                                     client=FactualReader(), objective_id=objective['id'])
        worker = objective_head.Worker(self.store, self.service, Config(model_api='none'))
        worker.resume_with_facts(objective, result)
        worker.resume_with_facts(objective, result)
        self.assertEqual(self.service.row(task)['status'], 'queued')
        pending = self.service.get(task)['pending_messages']
        self.assertEqual(len(pending), 1)
        self.assertIn('account_id', pending[0]['body'])
        self.service.step(task)
        self.assertEqual(len(self.service.list()), 1)
        self.assertEqual(objectives.get(self.store, objective['id'])['lifecycle'], 'active')
        self.assertEqual(self.service.get(task)['pending_messages'], [])

    def test_objective_pause_during_presentation_prevents_final_publication(self):
        from bridge import objectives
        task = self.submit()
        self.service.step(task)
        objective = objectives.create(self.store, {'request_key': 'owned', 'title': 'Owned outcome',
            'outcome': 'Prepare policy safely', 'success_criteria': 'Observed adoption',
            'stop_criteria': 'Owner changes scope', 'guardrails': 'Private preparation only',
            'repository': 'sample/project', 'provider': 'claude'}, self.actor)
        self.store.graph.db.execute('INSERT INTO objective_run_authority VALUES(?,?,?)', (task, objective['id'], ''))
        self.service.record(task, 'tool', 'finish_task', {'input': {'checks': 'Tests passed'}, 'result': {'status': 'completed'}})
        def pause_during_review(*args, **kwargs):
            self.store.graph.db.execute("UPDATE objectives SET lifecycle='paused',revision=revision+1 WHERE id=?", (objective['id'],))
            return {'status': 'completed'}
        with patch('bridge.workbench.mcp.call_tool', side_effect=pause_during_review):
            with self.assertRaisesRegex(Invalid, 'paused or closed'):
                self.service.execute(self.service.row(task), call('present_result', {'summary': 'Done'}))
        self.assertNotEqual(self.service.row(task)['status'], 'completed')

    def test_incomplete_advisory_review_needs_explicit_acknowledgment_and_warning(self):
        task = self.submit()
        self.service.step(task)
        review = {'id': 'same-inputs', 'status': 'failed'}
        self.service.record(task, 'tool', 'finish_task', {'input': {'checks': 'Tests passed'},
                            'result': {'status': 'review_required', 'review': review}})
        with self.assertRaises(Invalid):
            self.service.execute(self.service.row(task), call('present_result', {'summary': 'Done'}))
        with patch('bridge.workbench.mcp.call_tool', return_value={'status': 'completed', 'review': review}), patch('bridge.workbench.canvas.get_tree', return_value={'nodes': [], 'review': review}):
            result = self.service.execute(self.service.row(task), call('present_result', {
                'summary': 'Seven independent checks passed; code review still needed.', 'review_limitations_acknowledged': True}))
        self.assertTrue(result['manual_review_required'])
        self.assertTrue(self.service.row(task)['summary'].startswith('Automatic review is incomplete.'))

    def test_incomplete_review_acknowledgment_does_not_cover_new_inputs(self):
        task = self.submit()
        self.service.step(task)
        self.service.record(task, 'tool', 'finish_task', {'input': {'checks': 'Tests passed'},
                            'result': {'status': 'review_required', 'review': {'id': 'old', 'status': 'failed'}}})
        with patch('bridge.workbench.mcp.call_tool', return_value={'status': 'completed', 'review': {'id': 'new', 'status': 'failed'}}):
            result = self.service.execute(self.service.row(task), call('present_result', {
                'summary': 'Done', 'review_limitations_acknowledged': True}))
        self.assertEqual(self.service.row(task)['status'], 'paused')
        self.assertEqual(result['status'], 'review_required')

    def test_wait_sleeps_without_inference_and_wakes_on_a_note(self):
        self.service.agent = ScriptedAgent([call('wait_for_people', {'reason': 'Waiting for policy'})], [call('bridge_get_tree', ident='read')])
        task = self.submit()
        canvas.add_node(self.store, self.service.cfg, {'task_id': task, 'question': 'Which policy should apply?', 'paths': 'policy.py'})
        self.service.step(task)
        self.assertEqual(self.service.row(task)['status'], 'waiting')
        self.service.tick()
        self.assertEqual(len(self.service.agent.seen), 1)
        canvas.add_note(self.store, task, {'text': 'Please check compatibility too.', 'by': 'Requester'})
        self.service.tick()
        self.assertEqual(len(self.service.agent.seen), 2)

    def test_already_completed_review_cannot_strand_a_waiter(self):
        task = self.submit()
        result = self.service.execute(self.service.row(task), call('wait_for_people', {'reason': 'Review was pending earlier'}))
        self.assertFalse(result['waiting'])
        self.assertNotEqual(self.service.row(task)['status'], 'waiting')

    def test_proposing_an_answer_on_another_task_is_refused(self):
        task, other = self.submit(), self.submit(request_key='other')
        node = canvas.add_node(self.store, self.service.cfg, {'task_id': other, 'question': 'Which policy?', 'paths': 'policy.py'})
        with self.assertRaisesRegex(Invalid, 'another task'):
            self.service.execute(self.service.row(task), call('bridge_settle_node', {'node_id': node['node_id'], 'answer': 'Changed', 'rationale': 'Not allowed'}))
        with self.assertRaisesRegex(Invalid, 'another task'):
            self.service.execute(self.service.row(task), call('bridge_add_node', {'parent_id': node['node_id'], 'question': 'Cross task child'}))

    def test_reply_during_last_inference_is_not_stranded(self):
        task = self.submit()
        def respond(messages):
            self.service.message(task, {'text': 'One more thing', 'request_key': 'late'}, self.actor)
            return [{'type': 'text', 'text': 'Here is my current answer.'}], {}
        with patch.object(self.agent, 'respond', side_effect=respond):
            self.service.step(task)
        self.assertEqual(self.service.row(task)['status'], 'queued')
        self.assertEqual(self.service.get(task)['pending_messages'][0]['body'], 'One more thing')

    def test_pause_during_inference_defers_tools_until_resume(self):
        task = self.submit()
        def respond(messages):
            self.service.control(task, 'pause')
            return [call('edit_file', {'path': 'policy.py', 'old': '1', 'new': '2'})], {}
        with patch.object(self.agent, 'respond', side_effect=respond):
            self.service.step(task)
        self.assertEqual(self.service.row(task)['status'], 'paused')
        self.assertEqual((self.service.root / task / 'policy.py').read_text(), 'limit = 1\n')
        self.service.control(task, 'resume')
        self.service.step(task)
        self.assertEqual((self.service.root / task / 'policy.py').read_text(), 'limit = 2\n')

    def test_later_task_change_invalidates_ready_result_without_spending_inference(self):
        task = self.submit()
        self.service.update(task, status='completed', wait_revision=self.service.fingerprint(task))
        canvas.add_note(self.store, task, {'text': 'The policy has changed.', 'by': 'Requester'})
        self.service.tick()
        self.assertEqual(self.service.row(task)['status'], 'paused')
        self.assertIn('changed after completion', self.service.row(task)['error'])
        self.assertFalse(self.agent.seen)

    def test_authenticated_viewers_and_agent_tokens_cannot_start_browser_agents(self):
        auth = Auth(self.store, enabled=True)
        server = ready_server(self.store, 0, auth=auth, workbench=self.service)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        try:
            for role, kind, allowed in [('viewer', 'human', False), ('member', 'agent', False), ('member', 'human', True)]:
                person = self.store.add_person({'name': role + kind, 'role': role})
                token = auth.create_token(person['id'], kind=kind)['token']
                payload = json.dumps({'prompt': 'Fix the policy', 'repository': 'sample/project', 'request_key': role + kind, 'requester': 'Spoofed'}).encode()
                req = Request(base + '/api/workbench', data=payload, headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
                if allowed:
                    tid = json.load(urlopen(req))['task_id']
                    self.assertEqual(self.service.row(tid)['requester_id'], person['id'])
                    self.assertNotEqual(self.service.row(tid)['requester'], 'Spoofed')
                else:
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(req)
                    self.assertEqual(caught.exception.code, 403)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_followup_is_idempotent_and_reaches_model_with_attribution(self):
        task = self.submit()
        self.service.step(task)
        data = {'text': 'Also update docs.', 'request_key': 'message-1'}
        self.service.message(task, data, self.actor)
        self.service.message(task, data, self.actor)
        self.service.step(task)
        self.assertIn({'role': 'user', 'content': 'Requester: Also update docs.'}, self.agent.seen[-1])
        self.assertEqual(self.service.row(task)['status'], 'input')

    def test_restart_keeps_receipts_and_does_not_reexecute_completed_tool(self):
        task = self.submit()
        self.service.step(task)
        row = self.service.row(task)
        messages = json.loads(row['messages'])
        self.service.update(task, messages=json.dumps(messages[:-1]))  # crash before tool-result persistence
        reopened = Workbench(self.store, self.service.repositories, self.service.root, cfg=self.service.cfg, agent=self.agent)
        with patch.object(reopened, 'execute', side_effect=AssertionError('receipt must be replayed')):
            reopened.step(task)
        self.assertEqual(reopened.row(task)['status'], 'queued')

    def test_model_failure_is_safe_and_resumable(self):
        task = self.submit()
        with patch.object(self.agent, 'respond', side_effect=RuntimeError('sensitive-provider-body')):
            self.service.step(task)
        row = self.service.row(task)
        self.assertEqual(row['status'], 'paused')
        self.assertNotIn('sensitive-provider-body', row['error'])
        self.service.control(task, 'resume')
        self.service.step(task)
        self.assertEqual(self.service.row(task)['status'], 'queued')

    def test_http_auth_csrf_and_task_creation(self):
        auth = Auth(self.store, enabled=False)
        server = ready_server(self.store, 0, auth=auth, workbench=self.service)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        try:
            state = json.load(urlopen(base + '/api/state'))
            self.assertTrue(state['workbench']['enabled'])
            payload = json.dumps({'prompt': 'Fix the policy', 'repository': 'sample/project', 'request_key': 'http'}).encode()
            req = Request(base + '/api/workbench', data=payload, headers={'Content-Type': 'application/json'})
            with self.assertRaises(HTTPError) as caught:
                urlopen(req)
            self.assertEqual(caught.exception.code, 403)
            req.add_header('X-Bridge-CSRF', state['csrf_token'])
            task = json.load(urlopen(req))['task_id']
            self.assertEqual(json.load(urlopen(base + '/api/workbench/' + task))['prompt'], 'Fix the policy')
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()


class SymlinkedRootTests(unittest.TestCase):
    """A task copy may live under a symlinked directory (macOS /tmp and /var
    point into /private): only symlinks inside the copy are an escape."""

    def test_symlinked_ancestor_of_the_root_is_not_an_escape(self):
        from bridge import workbench_fs as fs
        with tempfile.TemporaryDirectory() as temp:
            real = Path(temp) / 'real'
            (real / 'copy').mkdir(parents=True)
            (real / 'copy' / 'a.py').write_text('x = 1\n')
            link = Path(temp) / 'link'
            link.symlink_to(real, target_is_directory=True)
            root = link / 'copy'
            self.assertEqual(fs.safe_path(root, 'a.py').read_text(), 'x = 1\n')
            (real / 'copy' / 'out').symlink_to(real)
            with self.assertRaises(fs.Invalid):
                fs.safe_path(root, 'out/copy/a.py')
