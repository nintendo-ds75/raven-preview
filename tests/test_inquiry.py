import json
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
from bridge.auth import Identity
from bridge.config import Config
from bridge.inquiry import ask
from bridge.llm import Client, LLMError, NoAPIKey
from bridge.store import Store


class InquiryTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'question.db')
        self.actor = Identity('reader', 'Reader', 'member', 'session')
        self.source = self.store.add_record({'repo': 'example/billing', 'kind': 'doc', 'ref': 'metering',
            'title': 'Billing traffic', 'body': 'Internal test traffic is excluded from billing.', 'resolved': True})['source']

    def question(self, **kwargs):
        return ask(self.store, {'question': 'Which traffic is excluded from billing?', 'repository': 'example/billing'},
                   self.actor, **kwargs)

    def test_question_reads_real_sources_without_creating_a_task_or_authorization(self):
        result = self.question(cfg=Config(model_api='none'))
        self.assertFalse(result['authorized'])
        self.assertTrue(any(e['id'] == self.source['record_id'] for e in result['evidence']))
        self.assertEqual(self.store.graph.db.execute('SELECT count(*) FROM runs').fetchone()[0], 0)
        self.assertEqual(self.store.graph.db.execute('SELECT count(*) FROM decisions').fetchone()[0], 0)

    def test_fabricated_citation_is_rejected_before_display(self):
        raw = {'outcome': 'supported_answer', 'answer': 'Invented claim',
               'citations': [{'id': self.source['record_id'], 'quote': 'Bill all internal traffic'}], 'open': []}
        with patch.object(Config, 'has_backend', return_value=True), patch.dict('os.environ', {'BRIDGE_SEMANTIC': '1'}), \
             patch.object(Client, 'complete_json', return_value=raw):
            result = self.question(cfg=Config())
        self.assertNotIn('Invented claim', result['answer'])
        self.assertFalse(result['model_used'])

    def test_prior_answer_keeps_the_person_and_reason_that_explain_it(self):
        from bridge.authz import Actor
        person = self.store.add_person({'name': 'Mira Security', 'role': 'member'})
        task = self.store.add_run({'title': 'Billing rollout', 'repo': 'example/billing'})['id']
        node = self.store.graph.add_decision(task, 'Which traffic is excluded from billing?', '',
                                             'pending', owner=person['name'])
        row = self.store.get_decision(node)
        self.store.answer(node, {'answer': 'Exclude internal test traffic.',
            'rationale': 'Customers must never pay for our synthetic load tests.',
            'expected_updated_at': row['updated_at']}, actor=Actor.person(person, kind='session'))
        result = self.question(cfg=Config(model_api='none'))
        evidence = next(e for e in result['evidence'] if e['id'] == node)
        self.assertIn('Mira Security', evidence['text'])
        self.assertIn('Customers must never pay for our synthetic load tests.', evidence['text'])
        self.assertEqual(evidence['task_id'], task)
        self.assertFalse(result['authorized'])

    def test_unverified_support_never_becomes_a_supported_answer(self):
        raw = {'outcome': 'supported_answer', 'answer': 'Internal traffic is excluded.',
               'citations': [{'id': self.source['record_id'], 'quote': 'Internal test traffic is excluded from billing.'}], 'open': []}
        with patch.object(Config, 'has_backend', return_value=True), patch.dict('os.environ', {'BRIDGE_SEMANTIC': '1'}), \
             patch.object(Client, 'complete_json', return_value=raw), patch('bridge.llm.check_support', return_value=None):
            result = self.question(cfg=Config())
        self.assertFalse(result['model_used'])
        self.assertEqual(result['outcome'], 'missing_fact')

    def test_citation_may_wrap_whitespace_but_must_keep_the_source_words(self):
        raw = {'outcome': 'supported_answer', 'answer': 'Internal traffic is excluded.',
               'citations': [{'id': self.source['record_id'],
                              'quote': 'Internal test traffic is\nexcluded from billing.'}], 'open': []}
        with patch.object(Config, 'has_backend', return_value=True), patch.dict('os.environ', {'BRIDGE_SEMANTIC': '1'}), \
             patch.object(Client, 'complete_json', return_value=raw), \
             patch('bridge.llm.check_support', return_value={'claims': [{'support': 'applied'}]}):
            self.assertTrue(self.question(cfg=Config())['model_used'])
            raw['citations'][0]['quote'] = 'Internal test traffic is not excluded from billing.'
            self.assertFalse(self.question(cfg=Config())['model_used'])

    def test_missing_api_key_does_not_borrow_cli_login(self):
        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': ''}), patch('bridge.llm.find_claude', return_value='/fake/claude'), \
             patch('bridge.llm._claude_cli', side_effect=AssertionError('implicit fallback')):
            self.assertFalse(Config().has_backend())
            with self.assertRaises(NoAPIKey):
                Client(Config()).complete('test', 'system', 'prompt')

    def test_openai_internal_call_has_no_host_tools_and_is_not_stored(self):
        payload = {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'Supported.'}]}]}
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'internal-canary', 'RAVEN_HOST_CODEX_API_KEY': 'different-host-canary'}), \
             patch.object(Client, '_post_json', return_value=payload) as post:
            self.assertEqual(Client(Config(model_api='openai', model='fixture-model')).complete('test', 'system', 'prompt'), 'Supported.')
        url, headers, body = post.call_args.args
        self.assertEqual(headers['Authorization'], 'Bearer internal-canary')
        self.assertEqual(body['tools'], [])
        self.assertFalse(body['store'])
        self.assertNotIn('different-host-canary', json.dumps(post.call_args.args))

    def test_incomplete_openai_response_is_never_used(self):
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'unit-test'}), patch.object(Client, '_post_json', return_value={'status': 'incomplete'}):
            with self.assertRaises(LLMError):
                Client(Config(model_api='openai')).complete('test', 'system', 'prompt')
