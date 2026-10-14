import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from evals.customer_test_credentials import save_key, read_saved_key, main


class SavedCredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'private' / 'key'

    def test_roundtrip_permissions_and_no_overwrite(self):
        save_key(self.path, 'synthetic-only')
        self.assertEqual(read_saved_key(self.path), 'synthetic-only')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        with self.assertRaises(OSError): save_key(self.path, 'replacement')

    def test_symlink_parent_and_file_refused(self):
        self.path.parent.symlink_to(Path(self.tmp.name), target_is_directory=True)
        with self.assertRaises(OSError): save_key(self.path, 'synthetic-only')
        self.path.parent.unlink(); self.path.parent.mkdir(mode=0o700)
        self.path.symlink_to(Path(self.tmp.name) / 'other')
        with self.assertRaises(OSError): save_key(self.path, 'synthetic-only')
        with self.assertRaises(OSError): read_saved_key(self.path)

    def test_permissions_and_hardlinks_refused(self):
        save_key(self.path, 'synthetic-only')
        self.path.chmod(0o644)
        with self.assertRaises(ValueError): read_saved_key(self.path)
        self.path.chmod(0o600); os.link(self.path, self.path.with_name('linked'))
        with self.assertRaises(ValueError): read_saved_key(self.path)

    def test_invalid_value_never_saved(self):
        for value in ('', 'two words', 'line\nbreak', 'x' * 4097):
            with self.assertRaises(ValueError): save_key(self.path, value)
        self.assertFalse(self.path.exists())

    def test_existing_file_refuses_before_prompt(self):
        save_key(self.path, 'synthetic-only')
        with patch('getpass.getpass', side_effect=AssertionError('no prompt')):
            self.assertEqual(main(['--install', str(self.path)]), 1)
