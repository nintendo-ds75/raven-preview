"""Ordinary web dialog lifecycle; no browser, provider, or authority probes."""
import shutil
import subprocess
import unittest
from pathlib import Path


class ModalLifecycleTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for DOM lifecycle checks')
    def test_queued_dialog_lifecycle(self):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [shutil.which('node'), str(root / 'tests' / 'modal-lifecycle-ui-unit.cjs')],
            cwd=root, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('checks passed (queued DOM/API doubles, not Chromium).', result.stdout)
