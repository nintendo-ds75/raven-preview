"""Fresh minimal run UI contracts; synthetic DOM/API doubles, never Chromium."""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node is required for shipped JS checks')
class MinimalRunUITests(unittest.TestCase):
    def test_fresh_queued_ui_contracts(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which('node'), 'tests/minimal-run-ui-unit.cjs'],
                                cwd=root, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('checks passed (queued DOM/API doubles, not Chromium).', result.stdout)
