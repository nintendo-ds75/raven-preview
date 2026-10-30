"""Intro chats: prepared from the record, broad to specific, honest notes."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import OfflineCase
from bridge import intro
from bridge.authz import Actor, Refused
from bridge.config import Config
from bridge.store import Invalid, Store

OFF = Config(model_api='none')


class FakeClient:
    """Stands in for the model: replies are queued per purpose."""
    replies = {}

    def __init__(self, cfg):
        pass

    def complete_json(self, purpose, system, prompt, max_tokens=0, **_):
        queue = FakeClient.replies.get(purpose) or []
        return queue.pop(0) if queue else {}


class IntroTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'intro.db')
        add = self.store.add_person
        self.jordan = add({'name': 'Jordan Okafor', 'email': 'jordan@example.test', 'team': 'Engineering'})['id']
        self.lena = add({'name': 'Lena Park', 'email': 'lena@example.test', 'team': 'Engineering'})['id']
        self.sam = add({'name': 'Sam Rivera', 'email': 'sam@example.test', 'team': 'Customer success'})['id']
        self.store.add_team({'name': 'Engineering', 'members': ['Jordan Okafor', 'Lena Park']})
        self.store.add_authority({'person': 'Jordan Okafor', 'scope_kind': 'path', 'scope': 'app/labels/*', 'role': 'decides'})
        self.me = Actor(id=self.jordan, name='Jordan Okafor', kind='session')

    def run_chat(self, cfg=OFF, answers=None):
        row = intro.prepare(self.store, self.jordan, self.me, cfg)
        row = intro.start(self.store, row['id'], {'expected_version': row['version']}, self.me)
        for text in answers or []:
            if row['status'] != 'active':
                break
            row = intro.answer(self.store, row['id'], {'expected_version': row['version'], 'answer': text}, self.me, cfg)
        return row

    def test_dossier_states_what_the_record_knows_and_skips_it(self):
        d = intro.dossier(self.store, self.jordan)
        text = ' '.join(f['text'] for f in d['facts'])
        self.assertIn('Engineering team with Lena Park', text)
        self.assertIn('`app/labels/*`', text)
        self.assertEqual({c['name'] for c in d['roster']}, {'Lena Park', 'Sam Rivera'})

    def test_guided_chat_follows_the_arc_and_saves_on_its_own(self):
        row = self.run_chat(answers=[f'Answer number {i}. With detail.' for i in range(12)])
        self.assertEqual(row['status'], 'saved')
        stages = [t['stage'] for t in row['turns']]
        order = [intro.STAGE_KEYS.index(s) for s in stages]
        self.assertEqual(order, sorted(order))
        self.assertEqual(stages[0], 'background')
        self.assertEqual(stages[-1], 'wrap')
        # Without a model every note is the person's own words.
        for note in row['summary']['notes']:
            self.assertIn(note['quote'], row['turns'][note['turn']]['answer'])
        # Nobody reviews the notes: they are on the profile when the call ends.
        self.assertEqual([c['text'] for c in intro.context_for(self.store, self.jordan)],
                         [n['text'] for n in row['summary']['notes']])

    def test_only_the_person_takes_their_chat(self):
        other = Actor(id=self.lena, name='Lena Park', kind='session')
        with self.assertRaises(Refused):
            intro.prepare(self.store, self.jordan, other, OFF)
        operator = Actor(name='Local operator', role='admin', kind='operator')
        self.assertEqual(intro.prepare(self.store, self.jordan, operator, OFF)['status'], 'ready')

    def test_stale_version_is_refused(self):
        row = self.run_chat()
        with self.assertRaises(Invalid):
            intro.answer(self.store, row['id'], {'expected_version': row['version'] - 1, 'answer': 'x'}, self.me, OFF)

    def test_model_follow_ups_are_capped_and_ungrounded_notes_dropped(self):
        cfg = Config(model_api='claude-cli')
        plan = {'opening': 'Hi Jordan, I am Raven. Fifteen minutes on what is not written down.',
                'questions': [{'stage': 'background', 'text': 'Before this?', 'why': 'w'},
                              {'stage': 'role', 'text': 'Your week?', 'why': 'w'},
                              {'stage': 'team', 'text': 'Who helps?', 'why': 'w'},
                              {'stage': 'current', 'text': 'Batch buying?', 'why': 'w'},
                              {'stage': 'unwritten', 'text': 'Gotchas?', 'why': 'w'},
                              {'stage': 'wrap', 'text': 'Anything else?', 'why': 'w'}]}
        follow = {'action': 'follow_up', 'ack': '', 'question': 'Tell me more?', 'why': 'w'}
        FakeClient.replies = {'intro_plan': [plan], 'intro_next': [follow] * 5,
                              'intro_summary': [{'notes': [
                                  {'topic': 'background', 'text': 'Was at Shippo.', 'quote': 'at Shippo', 'turn': 0},
                                  {'topic': 'team', 'text': 'Invented.', 'quote': 'never said this', 'turn': 0}],
                                  'still_unknown': ['How work is split']}]}
        with patch.object(intro, '_client', lambda cfg, fast=False: FakeClient(cfg)), \
                patch.object(Config, 'has_backend', lambda self: True):
            row = self.run_chat(cfg, ['I was at Shippo.', 'More.', 'Even more.'])
            kinds = [t['kind'] for t in row['turns']]
            self.assertEqual(kinds[:4], ['planned', 'follow_up', 'follow_up', 'planned'])
            row = intro.finish(self.store, row['id'], {'expected_version': row['version']}, self.me, cfg)
        self.assertEqual([n['text'] for n in row['summary']['notes']], ['Was at Shippo.'])
        self.assertEqual(row['summary']['dropped'], 1)

    def test_save_rewords_and_drops_notes_after_the_call(self):
        row = self.run_chat(answers=[f'Answer {i}. Detail.' for i in range(12)])
        first, second = row['summary']['notes'][:2]
        saved = intro.save(self.store, row['id'], {'expected_version': row['version'],
                           'notes': [{'id': first['id'], 'text': 'Edited by me.'}],
                           'added': [{'text': 'I also cover on-call.'}]}, self.me)
        self.assertEqual(saved['status'], 'saved')
        context = intro.context_for(self.store, self.jordan)
        self.assertEqual([c['text'] for c in context], ['Edited by me.', 'I also cover on-call.'])
        self.assertEqual(context[0]['quote'], first['quote'])
        self.assertNotIn(second['text'], [c['text'] for c in context])
        with self.assertRaises(Invalid):
            intro.save(self.store, row['id'], {'expected_version': saved['version'] - 1, 'notes': []}, self.me)


if __name__ == '__main__':
    unittest.main()
