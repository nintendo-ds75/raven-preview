"""Opt-in actual Docker acceptance, never replaced with a fake sandbox.

Run after pre-pulling the image: BRIDGE_TEST_DOCKER=1 python -m unittest
 discover -s tests -p test_artifact_docker.py -v
"""
import os
import shutil
import unittest
from unittest.mock import patch

from bridge.workbench_fs import DockerSandbox


@unittest.skipUnless(os.environ.get('BRIDGE_TEST_DOCKER') == '1', 'actual Docker acceptance is opt-in')
class DockerVerificationTests(unittest.TestCase):
    def setUp(self):
        import test_artifact_verification as fixtures
        self.assertIsNotNone(shutil.which('docker'), 'BRIDGE_TEST_DOCKER=1 requires an actual Docker CLI/daemon')
        self.fixture = fixtures.VerificationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.fixture.workbench.sandbox = DockerSandbox(os.environ.get('BRIDGE_TEST_DOCKER_IMAGE', 'python:3.13-slim-bookworm'))

    def test_real_container_observes_behavior_and_no_network_secret_or_host_mount(self):
        fixture = self.fixture
        overlay = fixture.root / 'operator-fixture'
        overlay.mkdir()
        (overlay / 'verify_isolation.py').write_text('''import os
from pathlib import Path
import module
assert module.VALUE == 1
assert os.getuid() != 0
assert 'RAVEN_TEST_HOST_ONLY_SECRET' not in os.environ
assert not Path('/var/run/docker.sock').exists()
assert set(os.listdir('/sys/class/net')) == {'lo'}
root_mount = next(line.split() for line in Path('/proc/mounts').read_text().splitlines() if line.split()[1] == '/')
assert 'ro' in root_mount[3].split(',')
try:
    Path('/etc/raven-verification-write-probe').write_text('must not write')
except OSError:
    pass
else:
    raise AssertionError('Container root was writable')
print('Observed exact behavior; unprivileged, no network, no host key/socket, read-only root')
''')
        fixture.profiles[0].update(commands=['python -B verify_isolation.py'], overlay_dir=str(overlay))
        fixture.save_profiles()
        with patch.dict(os.environ, {'RAVEN_TEST_HOST_ONLY_SECRET': 'fixture-canary-not-a-real-key'}):
            receipt = fixture.verify()
        self.assertEqual(receipt['status'], 'passed', receipt)
        self.assertIn('no network', receipt['commands'][0]['output'])
        self.assertEqual(receipt['coverage_basis'], 'operator-held-fixtures')
        self.assertTrue(fixture.verifier.list(fixture.task)[0]['usable_pass'])
        self.assertFalse((fixture.candidate / 'verify_isolation.py').exists())

    def test_actual_nonzero_exit_never_passes(self):
        self.fixture.profiles[0]['commands'] = ['python -c "raise SystemExit(7)"']
        self.fixture.save_profiles()
        receipt = self.fixture.verify()
        self.assertEqual(receipt['status'], 'failed', receipt)
        self.assertEqual(receipt['commands'][0]['exit_code'], 7)

    def test_actual_test_mutation_invalidates_receipt_and_preserves_candidate(self):
        self.fixture.profiles[0]['commands'] = ["python -c \"open('module.py','w').write('VALUE = 99\\n')\""]
        self.fixture.save_profiles()
        receipt = self.fixture.verify()
        self.assertEqual(receipt['status'], 'invalidated', receipt)
        self.assertEqual((self.fixture.candidate / 'module.py').read_text(), 'VALUE = 1\n')

    def test_signed_behavior_case_is_checked_independently_of_candidate_tests(self):
        from bridge import canvas
        from bridge.config import Config
        fixture = self.fixture
        fixture.store.add_owner({'name': 'Policy Owner', 'team': 'Policy', 'patterns': 'module.py'})
        fixture.store.graph.db.execute("UPDATE runs SET status='working' WHERE id=?", (fixture.task,))
        node = canvas.add_node(fixture.store, Config(model_api='none'), {'task_id': fixture.task,
            'question': 'Which observable value must this candidate expose?', 'paths': ['module.py'],
            'context': 'Positive case: VALUE is 1. Negative case: VALUE is not 2. Boundary: no billing or external action is authorized.'})
        canvas.settle_node(fixture.store, {'task_id': fixture.task, 'node_id': node['node_id'],
            'answer': 'Expose VALUE = 1, never 2. No billing or external actions.', 'rationale': 'Bounded behavior case for this candidate'})
        decision = fixture.store.get_decision(node['node_id'])
        canvas.sign_off(fixture.store, node['node_id'], {'by': 'Policy Owner', 'expected_updated_at': decision['updated_at']})
        fixture.store.update_run(fixture.task, {'status': 'completed'})
        overlay = fixture.root / 'signed-behavior'
        overlay.mkdir()
        (overlay / 'check_signed_behavior.py').write_text('import module\nassert module.VALUE == 1\nassert module.VALUE != 2\nprint("Signed positive and negative behavior cases observed")\n')
        fixture.profiles[0].update(commands=['python -B check_signed_behavior.py'], overlay_dir=str(overlay),
            coverage='Operator-authored positive VALUE=1 and negative VALUE!=2 cases from the signed decision; no automatic semantic test synthesis')
        fixture.save_profiles()
        passed = fixture.verify()
        self.assertEqual(passed['status'], 'passed', passed)
        self.assertEqual(passed['binding']['decisions'][0]['id'], node['node_id'])
        (fixture.candidate / 'module.py').write_text('VALUE = 2\n')
        (fixture.candidate / 'test_agent_claim.py').write_text('assert True  # The agent-edited test would pass.\n')
        failed = fixture.verify()
        self.assertEqual(failed['status'], 'failed', failed)
        self.assertFalse(fixture.verifier.list(fixture.task)[1]['usable_pass'])
