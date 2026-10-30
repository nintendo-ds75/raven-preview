"""PR summary, source pointers, meeting learning and the acting user."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import OfflineCase
from bridge import artifacts, auth, mcp, meeting_learn, muse, people_context, pr_summary
from bridge.config import Config
from bridge.store import Invalid

REPO = 'acme/app'


class FakeClient:
    reply = {}

    def __init__(self, cfg):
        pass

    def complete_json(self, purpose, system, prompt, max_tokens=0, **_):
        return FakeClient.reply


class Summary(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store('pr.db')
        self.g = self.store.graph
        self.addCleanup(self.g.close)
        self.store.add_person({'name': 'Marcus Lee', 'email': 'marcus@acme.test'})
        self.store.add_person({'name': 'Priya Shah', 'email': 'priya@acme.test'})
        old = self.store.add_run({'title': 'Stale-rate incident follow-up', 'repo': REPO})['id']
        self.prior = self.g.add_decision(old, 'Can a label be bought before the merchant sees the price?', 'policy',
                                         'pending', repo=REPO, owner='Marcus Lee')
        self.store.answer(self.prior, {'answer': 'Never buy a label before the merchant sees the price.',
                                       'rationale': 'We bought $4,100 of labels at a stale rate.', 'by': 'Marcus Lee'})
        self.task = self.store.add_run({'title': 'Bulk label purchase', 'repo': REPO})['id']
        self.cached = self.g.add_decision(self.task, 'May bulk buy skip the price check?', 'policy', 'pending',
                                          repo=REPO, owner='Marcus Lee')
        with self.g.transaction():
            self.g.update_decision(self.cached, status='resolved', source='memory', source_id=self.prior,
                                   answer='Marcus Lee recorded the answer.', answered_by='Marcus Lee')
        self.live = self.g.add_decision(self.task, "Which rate do Bloom's bulk buys use?", 'pricing', 'pending',
                                        repo=REPO, owner='Priya Shah')
        self.store.answer(self.live, {'answer': 'Bloom pays the negotiated rate on bulk buys.', 'by': 'Priya Shah'})
        self.open = self.g.add_decision(self.task, 'Should USPS bulk buys batch by carrier?', 'ops', 'pending',
                                        repo=REPO, owner='Priya Shah')

    def test_each_decision_says_where_its_answer_came_from(self):
        result = pr_summary.summary(self.store, self.task)
        kinds = {x['node_id']: x['kind'] for x in result['items']}
        self.assertEqual(kinds[self.cached], 'cache')
        self.assertEqual(kinds[self.live], 'live')
        self.assertEqual(kinds[self.open], 'open')
        hit = next(x for x in result['items'] if x['kind'] == 'cache')
        self.assertEqual(hit['who'], 'Marcus Lee')
        self.assertEqual(hit['source']['task'], 'Stale-rate incident follow-up')
        self.assertEqual(hit['answer'], 'Never buy a label before the merchant sees the price.')
        self.assertTrue(hit['person_id'])
        md = result['markdown']
        self.assertIn('`Raven cache hit` Marcus Lee decided this in “Stale-rate incident follow-up”', md)
        self.assertIn('`Live reply` New judgment from Priya Shah', md)
        self.assertIn('`Still open`', md)
        self.assertFalse(result['signed_off'])

    def test_the_whole_answer_and_reason_reach_the_pr(self):
        # Only the first sentence is bold; the condition after it is what the owner signed too.
        task = self.store.add_run({'title': 'Bulk label purchase, staging', 'repo': REPO})['id']
        node = self.g.add_decision(task, 'How fast may bulk buys call USPS?', 'ops', 'pending', repo=REPO,
                                   owner='Priya Shah')
        self.store.answer(node, {'answer': 'At most 40 a minute. Yes, run a full 200-label batch in staging first.',
                                 'rationale': 'USPS throttles past 40 a minute, and they have suspended an account '
                                              'before, so staging must show the limiter holds under a full batch.',
                                 'by': 'Priya Shah'})
        md = pr_summary.summary(self.store, task)['markdown']
        self.assertIn('- **At most 40 a minute.** Yes, run a full 200-label batch in staging first.', md)
        self.assertIn('so staging must show the limiter holds under a full batch.', md)

    def test_over_mcp_and_one_line_per_node(self):
        self.assertIn('bridge_pr_summary', mcp.READ_ONLY_TOOLS)
        out = mcp.call_tool(self.store, 'bridge_pr_summary', {'task_id': self.task})
        self.assertIn('## Decisions behind this change', out['markdown'])
        self.assertTrue(pr_summary.node_line(self.store, self.cached).startswith('Answered from memory: Marcus Lee'))
        self.assertEqual(pr_summary.node_line(self.store, self.open), 'Asking Priya Shah.')

    def test_a_decision_points_at_its_source(self):
        page = people_context.profile(self.store, self.g.find_person('Marcus Lee')['id'])
        decided = next(t for t in page['timeline'] if t['kind'] == 'decided')
        self.assertEqual(decided['pointer']['ref'], 'decision:' + self.prior)
        self.assertIn('Marcus Lee decided', decided['pointer']['summary'])
        with self.assertRaises(Invalid):
            artifacts.resolve(self.store, 'nothing', 'x')


class Meetings(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store('meet.db')
        self.addCleanup(self.store.graph.close)
        self.priya = self.store.add_person({'name': 'Priya Shah', 'email': 'priya@acme.test'})['id']

    def test_a_transcript_becomes_quoted_lines_that_point_back(self):
        FakeClient.reply = {'summary': 'Bloom wants bulk buys at their negotiated rate.', 'learned': [
            {'person': 'Priya Shah', 'text': 'Bloom asked for bulk buys at their negotiated rate.',
             'quote': 'at our negotiated rate, not list price', 'topic': 'current'},
            {'person': 'Priya Shah', 'text': 'Invented.', 'quote': 'words nobody said', 'topic': 'other'},
            {'person': 'Dana', 'text': 'A customer is not a teammate.', 'quote': 'bulk buying', 'topic': 'other'}]}
        with patch('bridge.llm.Client', FakeClient), patch.object(Config, 'has_backend', return_value=True):
            out = meeting_learn.import_and_learn(self.store, {'repo': REPO, 'title': 'Bloom check-in', 'transcript':
                'Dana (Bloom): The big thing is bulk buying.\nDana (Bloom): Yes, at our negotiated rate, not list price.\n'
                'Priya Shah: We will carry the negotiated rate over.'}, cfg=Config(model_api='claude-cli'))
        self.assertEqual([x['text'] for x in out['learned']], ['Bloom asked for bulk buys at their negotiated rate.'])
        page = people_context.profile(self.store, self.priya)
        line = page['timeline'][0]
        self.assertEqual(line['source'], 'from a meeting')
        self.assertEqual(line['pointer']['kind'], 'meeting')
        self.assertEqual(line['pointer']['summary'], 'Bloom wants bulk buys at their negotiated rate.')
        full = artifacts.resolve(self.store, 'meeting', out['meeting']['id'], 'at our negotiated rate, not list price')
        self.assertEqual([t['who'] for t in full['turns']], ['Dana (Bloom)', 'Dana (Bloom)', 'Priya Shah'])
        self.assertEqual([t['quoted'] for t in full['turns']], [False, True, False])
        self.assertEqual(full['turns'][2].get('person_id'), self.priya)


class ActingUser(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store('act.db')
        self.addCleanup(self.store.graph.close)
        self.maya = self.store.add_person({'name': 'Maya Chen', 'email': 'maya@parcelwise.example'})['id']

    def test_the_operator_shows_as_the_chosen_person(self):
        self.assertEqual(auth.acting(self.store).name, 'Local operator')
        self.store.update_settings({'act_as': 'maya@parcelwise.example'})
        me = auth.acting(self.store)
        self.assertEqual((me.name, me.email, me.id, me.kind), ('Maya Chen', 'maya@parcelwise.example', self.maya, 'operator'))
        self.assertEqual(self.store.settings()['act_as'], self.maya)
        self.store.update_settings({'act_as': ''})
        with patch.dict(os.environ, {'RAVEN_ACT_AS': 'Maya Chen'}):
            self.assertEqual(auth.acting(self.store).name, 'Maya Chen')

    def test_a_task_started_over_mcp_is_requested_by_them(self):
        self.store.update_settings({'act_as': self.maya})
        out = mcp.call_tool(self.store, 'bridge_start_task', {'title': 'Bulk labels', 'goal': 'Let merchants buy labels in bulk.',
                                                              'repo': REPO, 'agent': 'Claude Code'})
        self.assertEqual(self.store.graph.get_task(out['task_id'])['requester'], 'Maya Chen')


class Muse(unittest.TestCase):
    def test_connect_needs_two_people(self):
        items = [{'id': 'n:1', 'who': 'Sam Rivera', 'person_id': 's', 'said': 'Bloom wants SKU grouping', 'at': ''},
                 {'id': 'n:2', 'who': 'Priya Shah', 'person_id': 'p', 'said': 'Bloom pays a negotiated rate', 'at': ''}]
        one = {'kind': 'connect', 'title': 'Sam and Priya both talk to Bloom', 'body': 'Two asks. Same account.',
               'cites': [{'id': 'n:1', 'quote': 'SKU grouping'}]}
        two = {**one, 'cites': [{'id': 'n:1', 'quote': 'SKU grouping'}, {'id': 'n:2', 'quote': 'negotiated rate'}]}
        idea = {'kind': 'idea', 'title': 'Bulk buy at the negotiated rate', 'body': 'Bloom asked. It is worth it.',
                'cites': [{'id': 'n:2', 'quote': 'negotiated rate'}]}
        kept = muse._valid({'findings': [one, two, idea]}, items, set(), {})
        self.assertEqual([k['kind'] for k in kept], ['connect', 'idea'])

    def test_the_same_finding_in_new_words_is_one(self):
        a = {'kind': 'bug', 'ids': {'d1'}, 'paths': {'app/orders/fulfil.py'}, 'person': 'p'}
        self.assertTrue(muse._same_finding({**a, 'ids': {'d1', 'n2'}}, a))
        self.assertTrue(muse._same_finding({**a, 'ids': {'n9'}}, a))
        self.assertFalse(muse._same_finding({**a, 'ids': {'n9'}, 'paths': {'app/labels/print.py'}}, a))

    def test_nudges_are_spaced(self):
        with patch.object(muse, 'run_in_background') as ran:
            muse._last_nudge[0] = float('-inf')
            self.assertTrue(muse.nudge(None, every=3600))
            self.assertFalse(muse.nudge(None, every=3600))
        self.assertEqual(ran.call_count, 1)


if __name__ == '__main__':
    unittest.main()
