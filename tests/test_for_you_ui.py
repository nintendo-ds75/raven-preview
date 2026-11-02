"""For you card checks that run the shipped attention.js against DOM doubles."""
import shutil
import subprocess
import unittest
from pathlib import Path


class ForYouUiTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for DOM checks')
    def test_cards(self):
        root = Path(__file__).resolve().parent.parent
        for name in ('for-you-handon-ui-unit.cjs', 'for-you-conflict-ui-unit.cjs'):
            with self.subTest(name):
                result = subprocess.run([shutil.which('node'), str(root / 'tests' / name)],
                                        cwd=root, text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(': ok', result.stdout)
