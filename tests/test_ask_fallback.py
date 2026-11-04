"""Ask Raven without a matching record, misspelled names, and background asks."""
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from raven import inquiry, people_context
from raven.config import Config
from raven.store import Invalid, Store

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'parcelwise-demo.db'
REPO = 'parcelwise/parcelwise-app'


@unittest.skipUnless(FIXTURE.exists(), 'Parcelwise demo fixture not present')
class AskFallbackTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        shutil.copy(FIXTURE, Path(self.dir) / 'raven.db')
        self.store = Store(str(Path(self.dir) / 'raven.db'))
        self.cfg = Config(deterministic=True)
        self.me = SimpleNamespace(id='p-maya', name='Maya Chen', kind='operator', role='admin')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_misspelled_name_finds_the_person(self):
        names = lambda q: [p['name'] for p in people_context.named_people(self.store, q)]
        self.assertEqual(names('What does leena park do'), ['Lena Park'])
        self.assertEqual(names('what does jordon do'), ['Jordan Okafor'])
        self.assertEqual(names('who decides pricing'), [])
        self.assertEqual(names('What should I work on next'), [])

    def test_question_about_a_person_reads_their_profile(self):
        result = inquiry.ask(self.store, {'question': 'What does leena park do', 'repository': REPO}, self.me, cfg=self.cfg)
        profile = next(e for e in result['evidence'] if e['kind'] == 'profile')
        self.assertIn('Lena Park decides web', profile['text'])
        self.assertIn('Lena Park', [p['name'] for p in result['people']])

    def test_no_record_still_answers_from_the_workspace(self):
        result = inquiry.ask(self.store, {'question': 'What do you think I should work on as a next step right now',
                                          'repository': REPO}, self.me, cfg=self.cfg)
        self.assertEqual(result['grounding'], 'workspace')
        self.assertEqual(result['evidence'], [])
        self.assertIn('Bulk label purchase from Orders page', result['answer'])
        self.assertNotIn('Nothing your team has said', result['answer'])

    def test_background_ask_returns_a_job_and_then_the_answer(self):
        job = inquiry.start_ask(self.store, {'question': 'What does leena park do', 'repository': REPO}, self.me, cfg=self.cfg)
        self.assertEqual(job['status'], 'thinking')
        for _ in range(100):
            out = inquiry.ask_job(job['job'], self.me)
            if out['status'] != 'thinking':
                break
            time.sleep(0.05)
        self.assertEqual(out['status'], 'done')
        self.assertTrue(out['result']['evidence'])
        with self.assertRaises(Invalid):
            inquiry.ask_job(job['job'], SimpleNamespace(id='someone-else'))
        with self.assertRaises(Invalid):
            inquiry.start_ask(self.store, {'question': '', 'repository': REPO}, self.me)


if __name__ == '__main__':
    unittest.main()
