"""The two-way loop with a person in Slack: a Stop is held while a person
has not answered, a stopped session is resumed in its host's automatic
mode when they do, and the resume is reported back to the task's Slack
threads, over real HTTP MCP with the installed adapter."""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from fixtures import run_git
from bridge import host_client as hc
from bridge.setup import install_host_hooks
from test_auth import SharedServer, BOOTSTRAP


class FakeSlack:
    def __init__(self):
        self.posts = []

    def post_message(self, channel, text, blocks=None, thread_ts=''):
        self.posts.append((channel, thread_ts, text))
        return '1.0'


class HostWakeTests(SharedServer):
    def setUp(self):
        super().setUp()
        self.project = Path(self.temp.name) / 'project'; self.project.mkdir()
        run_git(self.project, 'init', '-q')
        (self.project / 'usage.py').write_text('LIMIT = 1\n')
        run_git(self.project, 'add', 'usage.py')
        run_git(self.project, '-c', 'user.name=Example', '-c', 'user.email=dev@example.invalid', 'commit', '-qm', 'initial')
        ada = self.post('/api/people', {'name': 'Ada Example', 'role': 'member'}, token=BOOTSTRAP)
        self.agent = self.post('/api/tokens', {'person_id': ada['id']}, token=BOOTSTRAP)['token']
        wes = self.post('/api/people', {'name': 'Wes Chen', 'email': 'wes@example.invalid'}, token=BOOTSTRAP)
        self.post('/api/authority', {'person': wes['id'], 'scope_kind': 'path', 'scope': 'billing/*',
                                     'role': 'decides'}, token=BOOTSTRAP)
        self.wes = self.post('/api/tokens', {'person_id': wes['id'], 'kind': 'human'}, token=BOOTSTRAP)['token']
        self.config = {'url': self.base + '/mcp', 'token': self.agent, 'project': str(self.project),
                       'repo': 'example/service'}
        self.session = 'session-wake'
        prompt = {'hook_event_name': 'UserPromptSubmit', 'session_id': self.session, 'cwd': str(self.project),
                  'prompt': 'Add usage-based pricing to billing.'}
        hc.hook(self.config, 'claude', prompt)
        self.task = self.store.graph.db.execute('SELECT id FROM runs').fetchone()['id']
        self.node = self.mcp('bridge_add_node', {'task_id': self.task, 'question': 'Should we bill the usage spike?',
                                                 'paths': 'billing/usage.py'}, self.agent)['result']
        self.assertEqual(self.node['status'], 'pending')

    def stop(self):
        return hc.hook(self.config, 'claude', {'hook_event_name': 'Stop', 'session_id': self.session,
                                               'cwd': str(self.project), 'stop_hook_active': False})

    def stop_for_good(self):
        with patch.dict(os.environ, {'BRIDGE_STOP_HOLD_MINUTES': '0'}):
            self.assertEqual(self.stop(), {})

    def answer(self, text='Bill it, but cap at 2x the plan limit.'):
        current = self.get(f"/api/decisions/{self.node['node_id']}", token=self.wes)
        return self.post(f"/api/decisions/{self.node['node_id']}/answer",
                         {'answer': text, 'rationale': 'policy', 'expected_updated_at': current['updated_at']},
                         token=self.wes)

    def poll(self, worker='w'):
        return hc.call(self.config, 'bridge_host_event', hc.binding(self.config, 'claude', self.session, 'poll',
                                                                    worker=worker))

    def test_stop_is_held_with_a_wait_while_a_person_has_not_answered(self):
        held = self.stop()
        self.assertEqual(held['decision'], 'block')
        self.assertIn('bridge_wait', held['reason'])
        self.assertIn(self.task, held['reason'])
        # Ids and counts only: the question text stays on the tree.
        self.assertNotIn('usage spike', held['reason'])
        listed = hc.call(self.config, 'bridge_host_event', hc.binding(self.config, 'claude', 'sessions', 'sessions'))
        self.assertEqual(listed['sessions'], [])  # held: it reads its answers itself
        self.stop_for_good()
        listed = hc.call(self.config, 'bridge_host_event', hc.binding(self.config, 'claude', 'sessions', 'sessions'))
        self.assertEqual(listed['sessions'], [self.session])

    def test_nothing_waiting_lets_the_agent_stop(self):
        self.answer()
        self.assertEqual(self.stop(), {})

    def test_installed_stop_hook_fails_open_when_raven_is_unreachable(self):
        install_host_hooks(self.project, str(self.project), self.config['url'], self.agent, ['claude'],
                           repo=self.config['repo'])
        settings = json.loads((self.project / '.claude/settings.local.json').read_text())
        self.assertIn('Stop', settings['hooks'])
        self.assertIn('SessionEnd', settings['hooks'])
        config_path = self.project / '.raven/host-config.json'
        config = json.loads(config_path.read_text())
        config_path.write_text(json.dumps({**config, 'url': 'http://127.0.0.1:9/mcp'}))
        command = [sys.executable, str(self.project / '.raven/host.py'), 'hook', '--host', 'claude']
        stop = {'hook_event_name': 'Stop', 'session_id': self.session, 'cwd': str(self.project)}
        result = subprocess.run(command, input=json.dumps(stop), capture_output=True, text=True, cwd=self.project,
                                timeout=60)
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))
        prompt = {**stop, 'hook_event_name': 'UserPromptSubmit', 'prompt': 'x'}
        result = subprocess.run(command, input=json.dumps(prompt), capture_output=True, text=True, cwd=self.project,
                                timeout=60)
        self.assertEqual(result.returncode, 2)  # registration still blocks visibly

    def test_an_answer_the_agent_already_read_does_not_resume_it(self):
        self.stop()  # held, so it waits on the tree
        self.answer()
        self.mcp('bridge_get_tree', {'task_id': self.task}, self.agent)
        self.stop_for_good()
        result = self.poll()
        self.assertIsNone(result['message'])
        self.assertIn('already read', result['reason'])

    def test_a_stopped_session_is_resumed_in_auto_mode_and_reported_to_slack(self):
        slack = FakeSlack()
        self.store.connect_delivery(slack, base_url='http://raven.example')
        with self.store.graph.transaction():
            self.store.graph.db.execute(
                "INSERT INTO notifications(id, decision_id, run_id, kind, channel, dedupe_key, external_ref, state, "
                "created_at) VALUES('n1', ?, ?, 'ask', 'slack', 'k1', 'D123:1700.5', 'sent', '')",
                (self.node['node_id'], self.task))
        self.stop_for_good()
        self.answer()
        calls = []

        def resumed(command, **kw):
            calls.append((command, kw))
            (self.project / 'usage.py').write_text('LIMIT = 2\nCAP = 2\n')
            out = {'result': 'Applied the 2x cap in usage.py. <!channel> All billing tests pass.',
                   'permission_denials': [{'tool_name': 'Bash', 'tool_input': {'command': 'git push --force'}}]}
            return subprocess.CompletedProcess(command, 0, json.dumps(out), '')

        results = hc.watch_all(self.config, 'claude', 'worker-a', resumed)
        self.assertEqual(len(results), 1)
        command, kw = calls[0]
        self.assertEqual(command[:7], ['claude', '--print', '--resume', self.session, '--permission-mode', 'auto',
                                       '--output-format'])
        self.assertEqual(kw['cwd'], str(self.project))
        self.assertNotIn('shell', kw)
        self.assertEqual((results[0]['report']['state'], results[0]['report']['posted']), ('blocked', 1))
        channel, thread, text = slack.posts[0]
        self.assertEqual((channel, thread), ('D123', '1700.5'))
        self.assertIn('Wes Chen', text)
        self.assertIn('cap at 2x', text)
        self.assertIn('Applied the 2x cap', text)
        self.assertNotIn('<!channel>', text)
        self.assertIn('usage.py (+2 -1)', text)
        self.assertIn('git push --force', text)
        self.assertIn(f'claude --resume {self.session}', text)
        self.assertIn(f'http://raven.example/#runs/{self.task}', text)
        # Acknowledged and reported once: nothing more to resume or post.
        self.assertTrue(all(r['message'] is None for r in hc.watch_all(self.config, 'claude', 'worker-a', resumed)))
        self.assertEqual(len(calls), 1)
        self.assertIsNone(self.poll()['message'])
        self.assertEqual(len(slack.posts), 1)
        kinds = [r['kind'] for r in self.store.graph.db.execute('SELECT kind FROM events WHERE run_id=?', (self.task,))]
        self.assertIn('host_resume_report', kinds)

    def test_a_failed_resume_is_released_and_said_so(self):
        slack = FakeSlack()
        self.store.connect_delivery(slack)
        with self.store.graph.transaction():
            self.store.graph.db.execute(
                "INSERT INTO notifications(id, decision_id, run_id, kind, channel, dedupe_key, external_ref, state, "
                "created_at) VALUES('n1', ?, ?, 'ask', 'slack', 'k1', 'D123:1700.5', 'sent', '')",
                (self.node['node_id'], self.task))
        self.stop_for_good()
        self.answer()
        failed = lambda command, **kw: subprocess.CompletedProcess(command, 1, '', 'session is already open')
        result = hc.watch_once(self.config, 'claude', self.session, 'worker-a', failed)
        self.assertFalse(result['resumed'])
        self.assertEqual(result['report']['state'], 'failed')
        self.assertIn('could not be resumed', slack.posts[0][2])
        # Released, so a later pass can try again.
        self.assertIsNotNone(self.poll('worker-b')['message'])

    def test_codex_resume_uses_its_sandbox_and_last_message_file(self):
        command = hc.resume_command('codex', 'thread-1', 'go', '/tmp/last.txt')
        self.assertEqual(command, ['codex', 'exec', '--sandbox', 'workspace-write', '-o', '/tmp/last.txt',
                                   'resume', 'thread-1', 'go'])
