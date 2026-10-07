import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from evals.audit.fixture import freeze, git
from evals.chronological.boundary import select_cutoff, verify_chronological_fixture, verify_history
from evals.chronological.filter_context import admit


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.gold = self.root / 'gold'
        self.gold.mkdir()
        subprocess.run(['git', 'init', str(self.gold)], check=True, capture_output=True)
        self.cutoff = '2025-01-02T00:00:00Z'

    def tearDown(self):
        self.temp.cleanup()

    def commit(self, date, body, *, author_date=None):
        (self.gold / 'source.txt').write_text(body)
        env = {**os.environ, 'GIT_AUTHOR_NAME': 'Synthetic author',
               'GIT_AUTHOR_EMAIL': 'author@example.invalid',
               'GIT_COMMITTER_NAME': 'Synthetic author',
               'GIT_COMMITTER_EMAIL': 'author@example.invalid',
               'GIT_AUTHOR_DATE': author_date or date, 'GIT_COMMITTER_DATE': date}
        subprocess.run(['git', '-C', str(self.gold), 'add', 'source.txt'], check=True, env=env)
        subprocess.run(['git', '-C', str(self.gold), '-c', 'core.hooksPath=/dev/null',
                        'commit', '-m', 'Synthetic change'], check=True, env=env, capture_output=True)
        return git(self.gold, 'rev-parse', 'HEAD')

    def frozen(self, baseline):
        brief = 'Clarify the current behavior and necessary decisions.'
        return freeze(self.gold, baseline, self.cutoff, self.root / 'sealed', brief,
                      brief_provenance={'kind': 'blinded_reconstruction',
                                        'reviewer': 'synthetic-test',
                                        'reviewed_brief_sha256': hashlib.sha256(brief.encode()).hexdigest(),
                                        'no_solution': True, 'no_expected_people': True,
                                        'no_future_references': True})

    def test_exact_boundary_selects_earlier_parent(self):
        past = self.commit('2025-01-01T00:00:00Z', 'past')
        self.commit(self.cutoff, 'at cutoff')
        self.assertEqual(select_cutoff(self.gold, self.cutoff), past)

    def test_future_objects_physically_absent(self):
        past = self.commit('2025-01-01T00:00:00Z', 'past')
        future = self.commit('2025-01-03T00:00:00Z', 'FUTURE_CANARY')
        future_blob = git(self.gold, 'rev-parse', future + ':source.txt')
        self.frozen(past)
        result = verify_chronological_fixture(self.root / 'sealed')
        objects = git(self.root / 'sealed/checkout', 'cat-file', '--batch-all-objects',
                      '--batch-check=%(objectname)').splitlines()
        self.assertNotIn(future, objects)
        self.assertNotIn(future_blob, objects)
        self.assertEqual(result['chronological_boundary']['baseline'], past)

    def test_future_author_clock_rejected(self):
        past = self.commit('2025-01-01T00:00:00Z', 'past', author_date='2025-01-03T00:00:00Z')
        self.frozen(past)
        with self.assertRaisesRegex(ValueError, 'author or committer'):
            verify_chronological_fixture(self.root / 'sealed')

    def test_unexpected_ref_rejected(self):
        past = self.commit('2025-01-01T00:00:00Z', 'past')
        self.frozen(past)
        git(self.root / 'sealed/checkout', 'update-ref', 'refs/heads/unexpected', past)
        with self.assertRaisesRegex(ValueError, 'Unexpected branch'):
            verify_history(self.root / 'sealed/checkout', self.cutoff, past)

    def test_promisor_route_rejected(self):
        past = self.commit('2025-01-01T00:00:00Z', 'past')
        self.frozen(past)
        pack_dir = self.root / 'sealed/checkout/.git/objects/pack'
        (pack_dir / 'unexpected.promisor').write_text('')
        with self.assertRaisesRegex(ValueError, 'Promisor'):
            verify_history(self.root / 'sealed/checkout', self.cutoff, past)

    def test_naive_cutoff_rejected(self):
        self.commit('2025-01-01T00:00:00Z', 'past')
        with self.assertRaisesRegex(ValueError, 'timezone'):
            select_cutoff(self.gold, '2025-01-02')

    def test_git_quoted_escaping_symlink_rejected(self):
        self.commit('2025-01-01T00:00:00Z', 'past')
        (self.gold / 'quoted\tlink').symlink_to('../outside-host')
        subprocess.run(['git', '-C', str(self.gold), 'add', '--', 'quoted\tlink'], check=True)
        past = self.commit('2025-01-01T12:00:00Z', 'past with link')
        # The general freezer historically missed Git-quoted path names.
        # A future upstream fix may reject during freeze instead, also safe.
        try:
            self.frozen(past)
        except ValueError as error:
            self.assertIn('escaping symlink', str(error))
            return
        with self.assertRaisesRegex(ValueError, 'escaping symlink'):
            verify_chronological_fixture(self.root / 'sealed')


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.record = {'provider': 'github', 'namespace': 'public', 'kind': 'issue',
                       'external_id': 'synthetic-1', 'body': 'Historical need',
                       'created_at': '2025-01-01T00:00:00Z',
                       'updated_at': '2025-01-01T12:00:00Z',
                       'provider_access': {'authorized': True}}
        self.cut = '2025-01-02T00:00:00Z'

    def result(self):
        return admit(self.record, self.cut, {'past'})

    def test_historical_snapshot_accepted(self):
        self.assertTrue(self.result()['accepted'])

    def test_late_edit_rejected(self):
        self.record['updated_at'] = '2025-01-03T00:00:00Z'
        self.assertFalse(self.result()['accepted'])

    def test_unknown_revision_rejected(self):
        self.record.pop('updated_at')
        self.assertFalse(self.result()['accepted'])

    def test_future_review_commit_rejected(self):
        self.record['review_commit_id'] = 'future'
        self.assertFalse(self.result()['accepted'])

    def test_missing_access_rejected(self):
        self.record['provider_access'] = None
        self.assertFalse(self.result()['accepted'])

    def test_future_git_rejected(self):
        self.record.update(version_basis='git_immutable', commit_id='future')
        self.assertFalse(self.result()['accepted'])

    def test_cutoff_git_accepted(self):
        self.record.update(version_basis='git_immutable', commit_id='past')
        self.assertTrue(self.result()['accepted'])

    def test_self_declared_receipt_rejected(self):
        self.record.update(version_basis='immutable_event', immutable_snapshot_sha256='made-up',
                           occurred_at='2025-01-01T00:00:00Z')
        self.assertFalse(self.result()['accepted'])

    def test_malformed_access_rejected(self):
        self.record['provider_access'] = 'public'
        self.assertFalse(self.result()['accepted'])

    def test_exact_cutoff_rejected(self):
        self.record['updated_at'] = self.cut
        self.assertFalse(self.result()['accepted'])


if __name__ == '__main__':
    unittest.main()
