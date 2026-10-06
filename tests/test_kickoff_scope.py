"""A resume must not silently pretend to repair an existing task's repository."""
from bridge import canvas
from bridge.config import Config
from bridge.store import Invalid
from test_contract import ContractCase


class KickoffScopeTests(ContractCase):
    def setUp(self):
        super().setUp()
        self.store.graph.upsert_artifact('acme/platform', 'billing/usage.py')
        self.store.graph.add_change('acme/platform', 'indexed-source', '2026-01-01T00:00:00+00:00',
                                    ['billing/usage.py'], [])

    def begin(self, **extra):
        return canvas.start_task(self.store, Config(model_api='none'), {
            'title': 'Choose the public billing behavior', 'goal': 'Preserve the approved billing contract.',
            'repo': 'acme/platform', 'paths': 'billing/usage.py', **extra})

    def test_canonical_repo_retry_with_task_id_rejects_without_mutation(self):
        old = self.begin(repo='scratch/repo', client_key='wrong-repo')
        before = dict(self.store.graph.get_task(old['task_id']))
        events = [row['id'] for row in self.store.graph.db.execute('SELECT id FROM events ORDER BY id')]
        for key in ({}, {'client_key': 'wrong-repo'}):
            with self.subTest(key=key), self.assertRaisesRegex(Invalid, 'omit task_id.*new client_key'):
                self.begin(task_id=old['task_id'], facts='customer=another', **key)
        self.assertEqual(dict(self.store.graph.get_task(old['task_id'])), before)
        self.assertEqual([row['id'] for row in self.store.graph.db.execute('SELECT id FROM events ORDER BY id')], events)

    def test_unknown_repository_guidance_supports_a_fresh_scoped_task(self):
        old = self.begin(repo='scratch/repo', client_key='wrong-repo')
        self.assertIn('omit task_id', old['next'])
        self.assertIn('new client_key', old['next'])
        canvas.finish_task(self.store, {'task_id': old['task_id'], 'status': 'abandoned',
                                      'reason': 'Restart with the indexed repository'})
        fresh = self.begin(client_key='correct-repo')
        self.assertNotEqual(fresh['task_id'], old['task_id'])
        self.assertEqual(fresh['repo'], 'acme/platform')
        self.assertNotIn('unknown_repo', fresh['discovery'])
        self.assertEqual(self.store.graph.get_task(old['task_id'])['repo'], 'scratch/repo')
        self.assertEqual(self.store.graph.get_task(old['task_id'])['status'], 'abandoned')

    def test_abandoned_client_key_cannot_silently_ignore_a_new_repository(self):
        old = self.begin(repo='scratch/repo', client_key='closed-key')
        canvas.finish_task(self.store, {'task_id': old['task_id'], 'status': 'abandoned', 'reason': 'Wrong scope'})
        with self.assertRaisesRegex(Invalid, 'new client_key'):
            self.begin(client_key='closed-key')
        self.assertEqual(self.store.graph.get_task(old['task_id'])['repo'], 'scratch/repo')

    def test_same_repository_and_identity_only_resume_still_work(self):
        old = self.begin(client_key='current-scope')
        for data in ({'task_id': old['task_id']}, {'task_id': old['task_id'], 'repo': 'acme/platform'}):
            with self.subTest(data=data):
                resumed = canvas.start_task(self.store, Config(model_api='none'), data)
                self.assertEqual(resumed['task_id'], old['task_id'])
                self.assertEqual(resumed['repo'], 'acme/platform')

    def test_client_key_only_unknown_repo_correction_is_preserved(self):
        old = self.begin(repo='scratch/repo', client_key='legacy-correction')
        fixed = self.begin(client_key='legacy-correction')
        self.assertEqual(fixed['task_id'], old['task_id'])
        self.assertEqual(fixed['repo'], 'acme/platform')
