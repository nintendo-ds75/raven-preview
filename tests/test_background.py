"""Raven in the background: answers people give are reused without anyone
signing again, and a question names the people who hold its context."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import OfflineCase
from bridge.authz import Actor
from bridge.config import Config
from bridge.inquiry import _plain_route, ask
from bridge.mcp import call_tool
from bridge.store import Store

REPO = 'acme/app'


class BackgroundTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'background.db')
        self.jordan = self.store.add_person({'name': 'Jordan Okafor', 'email': 'jordan@example.test', 'team': 'Engineering'})
        self.store.add_authority({'person': 'Jordan Okafor', 'scope_kind': 'path', 'scope': 'app/labels/*', 'role': 'decides',
                                  'repo': REPO, 'note': 'Label purchasing'})

    def answer(self, question, client_key):
        task = call_tool(self.store, 'bridge_start_task', {'title': question, 'goal': question, 'repo': REPO,
                                                           'client_key': client_key, 'paths': 'app/labels/purchase.py'})
        node = call_tool(self.store, 'bridge_add_node', {'task_id': task['task_id'], 'question': question,
                                                         'paths': 'app/labels/', 'client_ref': client_key})
        return task['task_id'], node

    def test_setting_turns_on_rules_with_it(self):
        self.assertFalse(self.store.settings()['background'])
        self.store.update_settings({'background': True})
        settings = self.store.settings()
        self.assertTrue(settings['background'])
        self.assertTrue(settings['auto_rules'])

    def test_an_answer_is_reused_without_a_fresh_signature(self):
        self.store.update_settings({'background': True})
        question = 'May any code path other than purchase_label() buy a label?'
        _, node = self.answer(question, 'first')
        decision = self.store.get_decision(node['node_id'])
        actor = Actor.person(self.jordan, kind='session')
        answered = self.store.answer(decision['id'], {'answer': 'No. Every purchase goes through purchase_label().',
                                                      'expected_updated_at': decision['updated_at']}, actor=actor)
        self.assertEqual(self.store.get_decision(answered['id'])['reusable'], 1)
        _, again = self.answer(question, 'second')
        reused = self.store.get_decision(again['node_id'])
        self.assertEqual(reused['signoff'], 'rule')
        self.assertEqual(reused['signed_by'], 'Jordan Okafor')

    def test_without_the_setting_answers_stay_request_specific(self):
        question = 'May any code path other than purchase_label() buy a label?'
        _, node = self.answer(question, 'first')
        decision = self.store.get_decision(node['node_id'])
        answered = self.store.answer(decision['id'], {'answer': 'No.', 'expected_updated_at': decision['updated_at']},
                                     actor=Actor.person(self.jordan, kind='session'))
        self.assertFalse(self.store.get_decision(answered['id'])['reusable'])

    def test_a_question_names_who_holds_the_context(self):
        result = ask(self.store, {'question': 'Who decides how app/labels/purchase.py buys labels?', 'repository': REPO},
                     Actor(name='Local operator', role='admin', kind='operator'), cfg=Config(model_api='none'))
        self.assertEqual(result['people'][0]['name'], 'Jordan Okafor')
        self.assertTrue(result['people'][0]['why'].startswith('Decides app/labels'))

    def test_route_lines_read_as_plain_words(self):
        self.assertEqual(_plain_route('Jordan Okafor decides for app/labels/* (matches app/labels/) (config): Label purchasing'),
                         'Decides app/labels: Label purchasing')
        self.assertEqual(_plain_route('authored 8 of the last 27 changes under app/ (32% recency weighted)'),
                         'Wrote 8 of the last 27 changes under app/')


if __name__ == '__main__':
    unittest.main()
