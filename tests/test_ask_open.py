"""Ask Raven answers every question: records first, then an open reading
from the workspace and general knowledge, labelled, never an empty or
quotes-only reply, and never a wait on a slow claim check."""
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bridge import inquiry, llm
from bridge.config import Config
from bridge.store import Store

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'parcelwise-demo.db'
REPO = 'parcelwise/parcelwise-app'
RECORDED = 'Which system meters usage and sends the invoice?'


@unittest.skipUnless(FIXTURE.exists(), 'Parcelwise demo fixture not present')
class AskOpenReadingTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        shutil.copy(FIXTURE, Path(self.dir) / 'raven.db')
        self.store = Store(str(Path(self.dir) / 'raven.db'))
        self.me = SimpleNamespace(id='p-maya', name='Maya Chen', kind='operator', role='admin')
        self.enterContext(patch.object(Config, 'has_backend', return_value=True))
        self.enterContext(patch.dict('os.environ', {'BRIDGE_SEMANTIC': '1'}))
        self.calls = []
        self.strict = {'outcome': 'missing_fact', 'answer': 'The records do not say.', 'citations': [], 'open': []}
        self.open = {'answer': 'Licensed billing charges a fixed fee per seat; metered billing charges for usage.\n'
                               '- Your team bills Starter overage by usage through Stripe.', 'basis': 'general', 'citations': []}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def fake(self, purpose, system, prompt, *args, **kwargs):
        if system == inquiry.SYSTEM_OPEN:
            self.calls.append('open')
            value = self.open() if callable(self.open) else self.open
        elif system == inquiry.SYSTEM:
            self.calls.append('strict')
            value = self.strict() if callable(self.strict) else self.strict
        else:
            raise llm.LLMError('not part of this test')
        if isinstance(value, Exception):
            raise value
        return value

    def ask(self, question):
        with patch.object(llm.Client, 'complete_json', self.fake):
            return inquiry.ask(self.store, {'question': question, 'repository': REPO}, self.me, cfg=Config())

    def test_question_nobody_recorded_gets_a_labelled_general_answer(self):
        result = self.ask("what's the difference between metered and licensed billing for a hamster?")
        self.assertTrue(result['model_used'])
        self.assertEqual(result['grounding'], 'general')
        self.assertIn('Licensed billing', result['answer'])
        self.assertIn('general knowledge', result['notice'])
        self.assertFalse(result.get('retry'))

    def test_records_that_do_not_answer_hand_over_to_the_open_reading_without_a_claim_check(self):
        with patch('bridge.llm.check_support', side_effect=AssertionError('no claim check for an unanswered reading')):
            result = self.ask(RECORDED)
        self.assertTrue(result['evidence'])
        self.assertEqual(result['grounding'], 'general')
        self.assertNotIn('do not say', result['answer'])
        self.assertEqual(self.calls.count('strict'), 1)

    def test_a_verified_records_answer_wins(self):
        decision = self.store.search(RECORDED, REPO)['matches'][0]['id']
        quote = 'Stripe Billing meters it.'
        self.strict = {'outcome': 'supported_answer', 'answer': 'Stripe Billing meters usage.',
                       'citations': [{'id': decision, 'quote': quote}], 'open': []}
        support = {'claims': [{'claim': 'Stripe Billing meters usage.', 'support': 'stated'}]}
        with patch('bridge.llm.check_support', return_value=support):
            result = self.ask(RECORDED)
        self.assertEqual(result['outcome'], 'supported_answer')
        self.assertNotIn('grounding', result)
        self.assertEqual(result['citations'][0]['quote'], quote)

    def test_a_slow_claim_check_does_not_hold_up_the_answer(self):
        decision = self.store.search(RECORDED, REPO)['matches'][0]['id']
        self.strict = {'outcome': 'supported_answer', 'answer': 'Stripe Billing meters usage.',
                       'citations': [{'id': decision, 'quote': 'Stripe Billing meters it.'}], 'open': []}
        self.open = {'answer': 'Stripe Billing meters usage; Marcus Lee decided it.', 'basis': 'team',
                     'citations': [{'id': decision, 'quote': 'Stripe Billing meters it.'}]}

        def slow(*args, **kwargs):
            time.sleep(3)
            return {'claims': [{'claim': 'x', 'support': 'stated'}]}
        started = time.time()
        with patch.object(inquiry, 'STRICT_GRACE', 0.3), patch('bridge.llm.check_support', side_effect=slow):
            result = self.ask(RECORDED)
        self.assertLess(time.time() - started, 2.5)
        self.assertEqual(result['answer'], 'Stripe Billing meters usage; Marcus Lee decided it.')
        # Quoted word for word from what Marcus decided: a records answer.
        self.assertEqual(result['grounding'], 'records')
        self.assertEqual(result['citations'][0]['id'], decision)

    def test_open_citations_must_be_copied_and_may_quote_a_title(self):
        records = self.store.search(RECORDED, REPO)['matches']
        decision = records[0]['id']
        title = self.store.get_decision(decision)['question']
        self.open = {'answer': 'Marcus Lee decided this.', 'basis': 'team',
                     'citations': [{'id': decision, 'quote': title}, {'id': decision, 'quote': 'An invented line'},
                                   {'id': 'nope', 'quote': 'x'}]}
        result = self.ask(RECORDED)
        self.assertEqual([c['quote'] for c in result['citations']], [title])

    def test_when_every_reading_fails_the_page_is_told_to_retry(self):
        self.strict = self.open = llm.LLMError('backend down')
        recorded = self.ask(RECORDED)
        self.assertTrue(recorded['retry'])
        self.assertIn('ask again', recorded['answer'])
        self.assertTrue(recorded['evidence'])
        nothing = self.ask('what is the airspeed of an unladen swallow?')
        self.assertTrue(nothing['retry'])
        self.assertTrue(nothing['answer'].strip())
        # Prose instead of JSON is tried once more, never more.
        self.assertEqual(self.calls.count('open'), 4)

    def test_malformed_open_reading_is_not_shown(self):
        self.open = {'answer': 'No basis given'}
        result = self.ask('what is the airspeed of an unladen swallow?')
        self.assertNotIn('No basis given', result['answer'])

    def test_greeting_reads_the_workspace_with_the_asker(self):
        prompts = []

        def capture(purpose, system, prompt, *a, **k):
            prompts.append(json.loads(prompt.split('\n\nReturn ONLY')[0]))
            return {'answer': 'Hi Maya. The bulk label job is ready to build.', 'basis': 'team', 'citations': []}
        with patch.object(llm.Client, 'complete_json', side_effect=capture):
            result = inquiry.ask(self.store, {'question': 'hi', 'repository': REPO}, self.me, cfg=Config())
        self.assertEqual(result['grounding'], 'workspace')
        self.assertEqual(prompts[0]['asker'], 'Maya Chen')
        self.assertTrue(any(i['id'].startswith('task:') for i in prompts[0]['workspace']))

    def test_points_and_code_become_lines_and_a_code_block(self):
        self.open = {'answer': 'Group labels by merchant.', 'points': ['Join merchants for names.', '- Filter by month.'],
                     'code': 'SELECT merchant_id, count(*) FROM labels GROUP BY 1;', 'basis': 'general', 'citations': []}
        result = self.ask('can you write me a SQL query to count labels per merchant per hamster?')
        fence = '`' * 3
        self.assertEqual(result['answer'], 'Group labels by merchant.\n- Join merchants for names.\n- Filter by month.\n'
                         f'{fence}\nSELECT merchant_id, count(*) FROM labels GROUP BY 1;\n{fence}')

    def test_model_markdown_is_shown_as_plain_text(self):
        self.assertEqual(inquiry._plain('**Use** `stripe listen`\n## Steps\n- one'), 'Use stripe listen\nSteps\n- one')
        self.assertEqual(inquiry._plain('Bulk label purchase (task 5065df84d70a) is ready.'), 'Bulk label purchase is ready.')
        self.assertEqual(inquiry._plain('Count them:\n```sql\nSELECT `a`, count(*) FROM labels\n```'),
                         'Count them:\n```sql\nSELECT `a`, count(*) FROM labels\n```')


class SettleOnlyTest(unittest.TestCase):
    def test_settle_only_stops_at_an_unanswered_first_reading(self):
        evidence = [{'id': 'a', 'text': 'Something else entirely.', 'kind': 'source'}]
        raw = {'outcome': 'missing_fact', 'answer': 'Not covered.', 'citations': [], 'open': []}
        with patch.object(Config, 'has_backend', return_value=True), patch.dict('os.environ', {'BRIDGE_SEMANTIC': '1'}), \
                patch.object(llm.Client, 'complete_json', return_value=raw) as complete, \
                patch('bridge.llm.check_support') as support:
            result = inquiry.read_evidence('q?', evidence, Config(), settle_only=True)
        complete.assert_called_once()
        support.assert_not_called()
        self.assertEqual(result['outcome'], 'missing_fact')
        self.assertFalse(result['model_used'])


if __name__ == '__main__':
    unittest.main()
