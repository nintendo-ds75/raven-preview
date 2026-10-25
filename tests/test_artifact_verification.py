"""Verification contracts: real git/candidate/database, explicitly fake sandbox."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from fixtures import OfflineCase
from bridge import artifact_verification as av, workbench_fs as fs
from bridge.auth import Identity
from bridge.store import Store, Invalid


class Sandbox:
    def __init__(self, callback=None, code=0):
        self.callback, self.code, self.calls = callback, code, []

    def run(self, root, command):
        self.calls.append((root, command))
        if self.callback:
            self.callback(root)
        return {'exit_code': self.code, 'timed_out': False, 'output': 'Observed test output'}


class VerificationTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'state.db')
        av.migrate(self.store.graph.db)
        task = self.store.add_run({'title': 'Candidate', 'repo': 'sample', 'agent': 'test'})
        self.task = task['id']
        self.copies = self.root / 'copies'
        self.candidate = self.copies / self.task
        self.candidate.mkdir(parents=True)
        (self.candidate / 'module.py').write_text('VALUE = 1\n')
        fs.git(self.candidate, 'init', '--template=', '-b', 'main')
        fs.git(self.candidate, 'add', '.')
        fs.git(self.candidate, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'baseline')
        self.base = fs.git(self.candidate, 'rev-parse', 'HEAD').decode().strip()
        self.store.graph.db.execute("UPDATE runs SET status='completed' WHERE id=?", (self.task,))
        self.row = {'task_id': self.task, 'repository': 'sample', 'status': 'completed', 'revision': self.base}
        self.sandbox = Sandbox()
        self.workbench = SimpleNamespace(root=self.copies, sandbox=self.sandbox, store=self.store, row=lambda ident: self.row)
        self.profile = self.root / 'profiles.json'
        self.profiles = [{'id': 'behavior', 'repository': 'sample', 'commands': ['python -m unittest discover'],
                          'coverage': 'Sample behavior only'}]
        self.save_profiles()
        self.actor = Identity('human', 'Human', 'member', 'session')
        self.verifier = av.Verifier(self.workbench, self.profile)

    def save_profiles(self):
        self.profile.write_text(json.dumps(self.profiles))

    def verify(self):
        return self.verifier.verify(self.task, 'behavior', self.actor)

    def test_observation_binds_actual_tree_patch_profile_and_core_without_mutating_candidate(self):
        (self.candidate / 'module.py').write_text('VALUE = 2\n')
        receipt = self.verify()
        self.assertEqual(receipt['status'], 'passed')
        self.assertEqual(receipt['binding']['base_revision'], self.base)
        self.assertEqual(receipt['assurance'], 'independent_profile')
        self.assertEqual(receipt['coverage_basis'], 'repository-owned-tests-or-commands')
        self.assertEqual((self.candidate / 'module.py').read_text(), 'VALUE = 2\n')
        self.assertNotEqual(self.sandbox.calls[0][0], self.candidate)
        self.assertEqual(self.verifier.list(self.task)[0]['usable_pass'], True)
        self.assertEqual(receipt['id'], av._digest({k:v for k,v in receipt.items() if k != 'id'}))

    def test_changed_candidate_during_check_makes_receipt_stale(self):
        self.sandbox.callback = lambda root: (self.candidate / 'module.py').write_text('VALUE = 3\n')
        receipt = self.verify()
        self.assertEqual(receipt['status'], 'stale')
        self.assertFalse(receipt['independently_verified'])

    def test_changed_candidate_after_receipt_revokes_usable_pass_without_rewriting_history(self):
        receipt = self.verify()
        (self.candidate / 'module.py').write_text('VALUE = 4\n')
        listed = self.verifier.list(self.task)[0]
        self.assertFalse(listed['usable_pass'])
        self.assertEqual(listed['status'], 'passed')
        self.assertEqual(listed['id'], receipt['id'])

    def test_test_mutation_of_executed_source_is_not_success_for_returned_bytes(self):
        self.sandbox.callback = lambda root: (root / 'module.py').write_text('VALUE = 99\n')
        receipt = self.verify()
        self.assertEqual(receipt['status'], 'invalidated')
        self.assertEqual((self.candidate / 'module.py').read_text(), 'VALUE = 1\n')

    def test_operator_fixture_overlay_is_bound_and_mutation_detected(self):
        overlay = self.root / 'held-out'
        overlay.mkdir()
        (overlay / 'held_out_test.py').write_text('assert True\n')
        self.profiles[0]['overlay_dir'] = str(overlay)
        self.save_profiles()
        receipt = self.verify()
        self.assertEqual(receipt['coverage_basis'], 'operator-held-fixtures')
        self.assertIn('held_out_test.py', receipt['trusted_fixture_paths'])
        self.assertFalse((self.candidate / 'held_out_test.py').exists())
        self.sandbox.callback = lambda root: (root / 'held_out_test.py').unlink()
        self.assertEqual(self.verify()['status'], 'invalidated')

    def test_profile_change_during_command_is_stale(self):
        def change(root):
            self.profiles[0]['commands'] = ['python -m unittest other_test']
            self.save_profiles()
        self.sandbox.callback = change
        self.assertEqual(self.verify()['status'], 'stale')

    def test_failed_command_and_missing_sandbox_never_pass(self):
        self.sandbox.code = 1
        self.assertEqual(self.verify()['status'], 'failed')
        self.workbench.sandbox = None
        self.assertEqual(self.verify()['status'], 'unavailable')
        self.assertFalse(self.verifier.config()['ready'])

    def test_task_must_be_idle_and_completed_and_agent_cannot_request(self):
        self.row['status'] = 'running'
        with self.assertRaises(Invalid):
            self.verify()
        self.row['status'] = 'completed'
        with self.assertRaises(Invalid):
            self.verifier.verify(self.task, 'behavior', Identity('human','Human','admin','agent'))

    def test_profiles_cannot_be_agent_editable_or_escape_repository_scope(self):
        unsafe = self.candidate / 'profiles.json'
        unsafe.write_text(self.profile.read_text())
        with self.assertRaises(Invalid):
            av.Verifier(self.workbench, unsafe).verify(self.task, 'behavior', self.actor)
        self.profiles[0]['repository'] = 'different'
        self.save_profiles()
        with self.assertRaises(Invalid):
            self.verify()
        self.assertEqual(self.sandbox.calls, [])

    def test_unsigned_core_judgment_refuses_verification(self):
        from bridge import canvas
        from bridge.config import Config
        self.store.graph.db.execute("UPDATE runs SET status='working' WHERE id=?", (self.task,))
        canvas.add_node(self.store, Config(model_api='none'), {'task_id': self.task,
            'question': 'Which limit should apply?', 'paths': 'module.py'})
        with self.assertRaises(Invalid):
            self.verify()
        self.assertEqual(self.sandbox.calls, [])

    def test_new_core_judgment_during_command_invalidates_receipt(self):
        from bridge import canvas
        from bridge.config import Config
        def change(root):
            self.store.graph.db.execute("UPDATE runs SET status='working' WHERE id=?", (self.task,))
            canvas.add_node(self.store, Config(model_api='none'), {'task_id': self.task,
                'question': 'Which limit should apply?', 'paths': 'module.py'})
        self.sandbox.callback = change
        self.assertEqual(self.verify()['status'], 'stale')

    def test_source_revocation_withholds_receipt_and_output(self):
        from bridge import context_memory as cm
        data = {'repo': 'sample', 'kind': 'note', 'ref': 'customer', 'body': 'Customer constraint'}
        source = self.store.add_record(data)['source']
        cm.add_anchor(self.store.graph.db, self.task, source['record_id'], source['source_version_id'], 'context')
        self.verify()
        self.store.add_record({**data, 'availability': 'inaccessible'})
        with self.assertRaises(Invalid):
            self.verifier.list(self.task)
        with self.assertRaises(Invalid):
            self.verify()

    def test_source_revoked_during_check_does_not_return_copied_output(self):
        from bridge import context_memory as cm
        data = {'repo': 'sample', 'kind': 'note', 'ref': 'customer', 'body': 'Customer constraint'}
        source = self.store.add_record(data)['source']
        cm.add_anchor(self.store.graph.db, self.task, source['record_id'], source['source_version_id'], 'context')
        self.sandbox.callback = lambda root: self.store.add_record({**data, 'availability': 'inaccessible'})
        with self.assertRaises(Invalid):
            self.verify()
        saved = self.store.graph.db.execute('SELECT status FROM artifact_verification_receipts').fetchone()
        self.assertEqual(saved['status'], 'stale')

    def test_invalid_sandbox_result_is_unavailable_not_pass(self):
        self.workbench.sandbox = SimpleNamespace(run=lambda root, command: {'success': True})
        self.assertEqual(self.verify()['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
