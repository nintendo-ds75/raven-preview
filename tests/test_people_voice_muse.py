"""People context, server voice and "Raven noticed": what each promises."""
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import OfflineCase
from bridge import intro, mcp, muse, people_context, voice
from bridge.authz import Actor, Refused
from bridge.config import Config
from bridge.store import Invalid, Store

OFF = Config(model_api='none')
ON = Config(model_api='claude-cli')


class People(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'people.db')
        add = self.store.add_person
        self.priya = add({'name': 'Priya Shah', 'email': 'priya@example.test', 'team': 'Customer success'})['id']
        self.sam = add({'name': 'Sam Rivera', 'email': 'sam@example.test', 'team': 'Fulfillment'})['id']
        self.store.add_authority({'person': 'Priya Shah', 'scope_kind': 'path', 'scope': 'app/pricing/*', 'role': 'decides'})
        self.priya_me = Actor(id=self.priya, name='Priya Shah', kind='session')
        self.sam_me = Actor(id=self.sam, name='Sam Rivera', kind='session')

    def test_a_person_page_is_one_timeline_newest_first(self):
        people_context.remember(self.store, self.priya, 'Priya says Bloom pays a negotiated rate.', quote='Bloom pays a negotiated rate')
        time.sleep(0.01)
        people_context.remember(self.store, self.priya, 'Priya wants calls, not Slack, for pricing.')
        page = people_context.profile(self.store, self.priya)
        self.assertEqual([t['text'] for t in page['timeline']],
                         ['Priya wants calls, not Slack, for pricing.', 'Priya says Bloom pays a negotiated rate.'])
        self.assertEqual(page['decides'][0]['scope'], 'app/pricing/*')
        self.assertTrue(all(t['editable'] for t in page['timeline']))

    def test_only_the_person_or_an_admin_edits_it(self):
        with self.assertRaises(Refused):
            people_context.edit(self.store, self.priya, {'action': 'add', 'text': 'Not mine to say.'}, self.sam_me)
        page = people_context.edit(self.store, self.priya, {'action': 'add', 'text': 'Owns the Bloom account.'}, self.priya_me)
        note = page['timeline'][0]
        self.assertEqual(note['source'], 'added by them')
        page = people_context.edit(self.store, self.priya, {'action': 'edit', 'id': note['id'], 'text': 'Owns Bloom.'}, self.priya_me)
        self.assertEqual(page['timeline'][0]['text'], 'Owns Bloom.')
        admin = Actor(id='x', name='Admin', role='admin', kind='session')
        page = people_context.edit(self.store, self.priya, {'action': 'remove', 'id': note['id']}, admin)
        self.assertEqual(page['timeline'], [])

    def test_search_finds_people_by_what_they_said(self):
        people_context.remember(self.store, self.sam, 'Sam says labels must print grouped by SKU.')
        found = people_context.directory(self.store, 'sku')['people']
        self.assertEqual([p['name'] for p in found], ['Sam Rivera'])
        self.assertIn('grouped by SKU', found[0]['match'])
        self.assertEqual(len(people_context.directory(self.store)['people']), 2)

    def test_ask_reads_what_people_told_raven(self):
        people_context.remember(self.store, self.sam, 'Sam says Bloom labels must print grouped by SKU.')
        hits = people_context.relevant(self.store, 'How should Bloom labels print?')
        self.assertEqual([h['person_name'] for h in hits], ['Sam Rivera'])

    def test_agents_read_and_add_context_over_mcp(self):
        names = {t['name'] for t in mcp.TOOLS}
        self.assertTrue({'bridge_ask_raven', 'bridge_person_context', 'bridge_find_people', 'bridge_remember',
                         'bridge_raven_noticed'} <= names)
        self.assertNotIn('bridge_remember', mcp.READ_ONLY_TOOLS)
        kept = mcp.call_tool(self.store, 'bridge_remember', {'person': 'Sam Rivera', 'text': 'Sam needs SKU grouping.',
                                                             'quote': 'group them by SKU'})
        self.assertEqual(kept['kept']['source'], 'via an agent')
        page = mcp.call_tool(self.store, 'bridge_person_context', {'person': 'sam@example.test'})
        self.assertEqual(page['timeline'][0]['quote'], 'group them by SKU')
        with self.assertRaises(Invalid):
            mcp.call_tool(self.store, 'bridge_person_context', {'person': 'Nobody Here'})


class Voice(unittest.TestCase):
    def test_off_without_keys_or_models(self):
        with patch.dict(os.environ, {'RAVEN_VOICE_DIR': '/nonexistent', 'ELEVENLABS_API_KEY': '', 'OPENAI_API_KEY': '',
                                     'RAVEN_TTS': '', 'RAVEN_STT': ''}):
            self.assertEqual(voice.status()['tts'], '')
            self.assertEqual(voice.status()['stt'], '')
            with self.assertRaises(voice.VoiceOff):
                voice.speak('Hello there.')
            with self.assertRaises(voice.VoiceOff):
                voice.transcribe(b'abc')

    def test_hosted_engines_pick_from_keys(self):
        with patch.dict(os.environ, {'ELEVENLABS_API_KEY': 'k', 'RAVEN_TTS': '', 'RAVEN_STT': ''}):
            self.assertEqual(voice.tts_engine(), 'elevenlabs')
        with patch.dict(os.environ, {'ELEVENLABS_API_KEY': '', 'OPENAI_API_KEY': 'k', 'RAVEN_TTS': '', 'RAVEN_STT': ''}):
            self.assertEqual(voice.stt_engine(), 'openai')
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'k', 'RAVEN_TTS': 'off'}):
            self.assertEqual(voice.tts_engine(), '')

    def test_keys_go_only_to_the_provider_endpoints(self):
        with self.assertRaises(Invalid):
            voice._post('https://example.test/steal', {}, b'')

    def test_limits(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'k', 'RAVEN_TTS': ''}):
            with self.assertRaises(Invalid):
                voice.speak('x' * (voice.TEXT_LIMIT + 1))

    def test_multipart_carries_the_recording(self):
        body, ctype = voice._multipart({'model': 'm'}, b'\x00\x01audio', 'audio/webm;codecs=opus')
        self.assertIn(b'filename="answer.webm"', body)
        self.assertIn(b'\x00\x01audio', body)
        self.assertTrue(ctype.startswith('multipart/form-data; boundary='))


class Turns(OfflineCase):
    def test_a_slow_model_turn_falls_back_to_the_plan(self):
        with patch.dict(os.environ, {'BRIDGE_INTRO_TURN_SECONDS': '1'}):
            started = time.monotonic()
            self.assertIsNone(intro._within(intro._turn_seconds(), lambda: time.sleep(3)))
            self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(intro._within(1, lambda: {'ok': 1}), {'ok': 1})
        self.assertIsNone(intro._within(1, lambda: 1 / 0))


def _git(path, *args):
    subprocess.run(['git', '-C', str(path), *args], check=True, capture_output=True,
                   env={**os.environ, 'GIT_AUTHOR_NAME': 'T', 'GIT_AUTHOR_EMAIL': 't@x', 'GIT_COMMITTER_NAME': 'T',
                        'GIT_COMMITTER_EMAIL': 't@x'})


class FakeClient:
    reply = {}

    def __init__(self, cfg):
        pass

    def complete_json(self, purpose, system, prompt, max_tokens=0, **_):
        return FakeClient.reply


class Noticed(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'muse.db')
        self.sam = self.store.add_person({'name': 'Sam Rivera', 'email': 'sam@example.test'})['id']
        repo = Path(self.temp.name) / 'app'
        (repo / 'app' / 'labels').mkdir(parents=True)
        (repo / 'app' / 'labels' / 'print.py').write_text('def print_labels(labels):\n    return sorted(labels)\n')
        _git(repo.parent, 'init', '-q', str(repo))
        _git(repo, 'add', '.')
        _git(repo, 'commit', '-qm', 'labels')
        with self.store.graph.transaction():
            self.store.graph.set_source('acme/app', 'git', str(repo))

    def test_no_model_no_findings(self):
        people_context.remember(self.store, self.sam, 'Sam says labels print grouped by SKU.')
        with self.assertRaises(Invalid):
            muse.run(self.store, OFF)

    def test_findings_must_quote_a_person_and_name_real_files(self):
        note = people_context.remember(self.store, self.sam, 'Sam says Bloom needs labels grouped by SKU.',
                                       quote='Bloom wants them grouped by SKU')
        FakeClient.reply = {'findings': [
            {'kind': 'gap', 'title': 'Labels are not grouped by SKU', 'body': 'Sam said Bloom needs it. print.py only sorts.',
             'cites': [{'id': 'n:' + note['id'], 'quote': 'grouped by SKU'}], 'paths': ['app/labels/print.py'],
             'ask_person': 'Sam', 'ask': 'Should SKU grouping be per order or per batch?', 'agent_prompt': 'Group labels by SKU.'},
            {'kind': 'gap', 'title': 'Invented', 'body': 'Nobody said this.',
             'cites': [{'id': 'n:' + note['id'], 'quote': 'something Sam never said'}], 'paths': ['app/labels/print.py']},
            {'kind': 'bug', 'title': 'No files', 'body': 'Claims code without reading it.',
             'cites': [{'id': 'n:' + note['id'], 'quote': 'grouped by SKU'}], 'paths': ['made/up.py']},
        ]}
        with patch('bridge.llm.Client', FakeClient), patch.object(Config, 'has_backend', return_value=True):
            result = muse.run(self.store, ON)
        self.assertEqual([x['title'] for x in result['added']], ['Labels are not grouped by SKU'])
        found = result['added'][0]
        self.assertEqual(found['evidence'][0]['who'], 'Sam Rivera')
        self.assertEqual(found['person']['name'], 'Sam Rivera')
        self.assertIn('app/labels/print.py', result['read']['files'])
        # The same finding is not raised twice while it is open.
        with patch('bridge.llm.Client', FakeClient), patch.object(Config, 'has_backend', return_value=True):
            self.assertEqual(muse.run(self.store, ON)['added'], [])
        self.assertEqual(muse.act(self.store, found['id'], 'dismiss')['status'], 'dismissed')
        self.assertEqual(muse.listing(self.store)['insights'], [])



class CallWording(unittest.TestCase):
    def test_a_negotiated_rate_is_a_pricing_call(self):
        from bridge.scopes import decision_scopes
        self.assertIn('pricing', decision_scopes("Should bulk purchases use Bloom's negotiated rate?"))
        self.assertNotIn('pricing', decision_scopes('Should we raise the API rate limit?'))

    def test_spoken_questions_do_not_read_out_paths(self):
        from bridge.interview import _prompts
        prompts = _prompts({'question': 'Which rate?', 'repo': 'acme/app', 'paths': ['app/labels/purchase.py'], 'path': ''})
        self.assertFalse(any('/' in p for p in prompts[1:]))


if __name__ == '__main__':
    unittest.main()
