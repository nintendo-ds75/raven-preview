"""Provider-free replacement and polling checks for source-review UI state."""
import shutil
import subprocess
import unittest
from pathlib import Path


class BriefSourceStateTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for DOM state checks')
    def test_source_review_state(self):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [shutil.which('node'), str(root / 'tests' / 'brief-source-state.cjs')],
            cwd=root, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('checks passed (parsed DOM/API doubles, not Chromium).', result.stdout)
