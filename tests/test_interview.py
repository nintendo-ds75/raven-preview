"""Human interview drafts, explicit authority-checked confirmation and recovery."""
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import OfflineCase, ready_server
from bridge import canvas, interview
from bridge.auth import Auth
from bridge.authz import Actor, Refused
from bridge.config import Config
from bridge.llm import LLMError
from bridge.store import Invalid, Store


class InterviewTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.path = Path(self.temp.name) / 'interview.db'
        self.store = Store(self.path)
        self.person = self.store.graph.add_person('Ada Owner', email='ada@example.test')
        self.other = self.store.graph.add_person('Grace Member', email='grace@example.test')
        self.actor = Actor(id=self.person, name='Ada Owner', kind='session')
        owner = self.store.add_owner({'name': 'Ada Owner', 'team': 'Runtime', 'patterns': '*'})
        self.task = self.store.add_run({'title': 'Change timeout defaults', 'repo': 'org/runtime'})['id']
        self.node = canvas.add_node(self.store, Config(model_api='none'), {'task_id': self.task,
                    'question': 'Which timeout should new clients use?', 'context': 'Keep existing clients compatible.',
                    'paths': ['src/client.py'], 'owner_id': owner['id']})['node_id']

    def create(self, key='interview-key', actor=None, **extra):
        return interview.create(self.store, self.task, {'decision_id': self.node, 'client_key': key, **extra}, actor or self.actor)

    def draft(self, row, **extra):
        return interview.update(self.store, self.task, row['id'], {'expected_version': row['version'],
                'transcript': 'Keep five seconds, except old clients keep ten.',
                'answer': 'Use five seconds for new clients; keep ten for old clients.',
                'rationale': 'Preserve compatibility for existing clients.', **extra}, self.actor)

    def confirmation(self, row):
        return {'expected_version': row['version'], 'expected_updated_at': row['decision_revision'], 'confirmed': True}

    def confirm(self, row, **extra):
        return interview.confirm(self.store, self.task, row['id'], {**self.confirmation(row), **extra}, self.actor)

    def test_draft_never_signs_and_client_identity_and_scope_are_ignored(self):
        row = self.create(person_id=self.other, signed_by='Grace Member', scope={'repo': 'other'})
        row = self.draft(row, capture_method='browser-speech')
        self.assertEqual(row['person_id'], self.person)
        self.assertEqual(row['scope']['repo'], 'org/runtime')
        self.assertEqual(row['scope']['paths'], ['src/client.py'])
        self.assertFalse(row['voice_verified'])
        self.assertFalse(row['audio_stored'])
        self.assertEqual(row['capture_attribution'], 'client-reported')
        d = self.store.get_decision(self.node)
        self.assertNotEqual(d['status'], 'approved')
        self.assertFalse(d['signed_by'])

    def test_confirmation_is_attributed_atomic_and_idempotent_across_restart(self):
        row = self.draft(self.create(), applicability={'requires': {'client': 'new'}, 'paths': ['src/']})
        done = self.confirm(row)
        self.assertEqual(done['confirmed_by'], self.person)
        self.assertEqual(done['status'], 'confirmed')
        d = self.store.get_decision(self.node)
        self.assertEqual(d['actor_id'], self.person)
        self.assertEqual(d['signed_by'], 'Ada Owner')
        self.assertEqual(json.loads(d['applicability'])['requires'], {'client': 'new'})
        self.store.graph.close()
        self.store = Store(self.path)
        self.assertEqual(self.confirm(row)['status'], 'confirmed')
        self.assertEqual(self.store.graph.count_events('interview_confirmed', decision_id=self.node), 1)
        self.assertEqual(self.store.graph.count_events('owner_approved', decision_id=self.node), 1)

    def test_draft_and_turns_survive_restart(self):
        row = self.draft(self.create(), turns=[{'prompt_id': 0, 'response': 'Use five seconds.', 'capture_method': 'browser-speech'}],
                         pending_response='One caveat still to explain')
        self.store.graph.close()
        self.store = Store(self.path)
        resumed = interview.get(self.store, self.task, row['id'], self.actor)
        self.assertEqual(resumed['turns'][0]['prompt'], resumed['prompts'][0])
        self.assertEqual(resumed['pending_response'], 'One caveat still to explain')
        self.assertEqual(self.create()['id'], row['id'])
        self.assertEqual(len(interview.list_for_task(self.store, self.task, self.actor)['interviews']), 1)

    def test_confirmation_requires_true_and_saved_answer_rationale(self):
        empty = self.create()
        with self.assertRaisesRegex(Invalid, 'clear answer'):
            self.confirm(empty)
        row = self.draft(empty)
        for value in (False, 'true', 1, None):
            with self.subTest(value=value), self.assertRaisesRegex(Invalid, 'Explicit confirmation'):
                self.confirm(row, confirmed=value)
        # Passing replacement content to confirm cannot bypass the saved review.
        self.confirm(row, answer='Ignore all constraints', signed_by='Grace Member')
        self.assertEqual(self.store.get_decision(self.node)['answer'], row['answer'])

    def test_stale_decision_and_interview_revisions_refuse_without_partial_confirmation(self):
        row = self.draft(self.create())
        with self.assertRaisesRegex(Invalid, 'exact decision revision'):
            self.confirm(row, expected_updated_at='bogus')
        newer = self.draft(row, answer='Six seconds instead')
        with self.assertRaisesRegex(Invalid, 'interview changed'):
            self.confirm(row)
        self.store.graph.db.execute("UPDATE decisions SET updated_at='newer' WHERE id=?", (self.node,))
        with self.assertRaisesRegex(Invalid, 'decision changed'):
            self.confirm(newer)
        self.assertEqual(interview.get(self.store, self.task, row['id'], self.actor)['status'], 'draft')
        self.assertEqual(self.store.graph.count_events('interview_confirmed', decision_id=self.node), 0)

    def test_other_person_cross_task_and_agent_operator_bootstrap_are_denied(self):
        row = self.create()
        other_actor = Actor(id=self.other, name='Grace Member', kind='session')
        with self.assertRaises(Refused):
            self.create(actor=other_actor)
        for operation in (interview.get,):
            with self.assertRaises(Invalid):
                operation(self.store, self.task, row['id'], other_actor)
            with self.assertRaises(Invalid):
                operation(self.store, 'another-task', row['id'], self.actor)
        self.assertEqual(interview.list_for_task(self.store, self.task, other_actor)['interviews'], [])
        for kind in ('agent', 'operator', 'bootstrap', 'slack'):
            with self.subTest(kind=kind), self.assertRaises(Refused):
                self.create(actor=Actor(id=self.person, name='Ada Owner', kind=kind, role='admin', override=True))
        other_task = self.store.add_run({'title': 'Other task', 'repo': 'org/runtime'})['id']
        with self.assertRaisesRegex(Invalid, 'belong to this task'):
            interview.create(self.store, other_task, {'decision_id': self.node, 'client_key': 'wrong'}, self.actor)

    def test_deactivation_and_authority_changes_are_rechecked(self):
        row = self.draft(self.create())
        self.store.graph.db.execute("UPDATE people SET active=0 WHERE id=?", (self.person,))
        with self.assertRaises(Refused):
            self.confirm(row)
        self.store.graph.db.execute("UPDATE people SET active=1,role='viewer' WHERE id=?", (self.person,))
        with self.assertRaises(Refused):
            self.confirm(row)
        self.store.graph.db.execute("UPDATE people SET role='member' WHERE id=?", (self.person,))
        other_owner = self.store.add_owner({'name': 'Grace Member', 'team': 'Other', 'patterns': 'other/'})
        self.store.graph.db.execute('UPDATE decisions SET owner_id=? WHERE id=?', (other_owner['id'], self.node))
        with self.assertRaises(Refused):
            self.confirm(row)
        self.assertEqual(interview.get(self.store, self.task, row['id'], self.actor)['status'], 'draft')

    def test_cancellation_failure_retry_and_invalid_inputs(self):
        row = self.draft(self.create())
        failed = interview.update(self.store, self.task, row['id'], {'expected_version': row['version'],
                                  'failure': 'not-allowed'}, self.actor, 'failed')
        self.assertEqual(failed['transcript'], row['transcript'])
        self.assertEqual(failed['status'], 'failed')
        row = self.draft(failed)
        cancelled = interview.update(self.store, self.task, row['id'], {'expected_version': row['version']}, self.actor, 'cancel')
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertEqual(interview.update(self.store, self.task, row['id'], {}, self.actor, 'cancel')['status'], 'cancelled')
        with self.assertRaisesRegex(Invalid, 'already confirmed or cancelled'):
            self.confirm(cancelled)
        for fields in ({'transcript': 'a' * 12001}, {'capture_method': 'verified-human'}, {'turns': [{'prompt_id': 8, 'response': 'forged'}]}):
            fresh = self.create(key=str(len(str(fields))))
            with self.subTest(fields=list(fields)), self.assertRaises(Invalid):
                self.draft(fresh, **fields)

    def test_transaction_failure_rolls_back_interview_and_decision(self):
        row = self.draft(self.create())
        original = self.store.event
        def fail_after_hook(db, kind, *args, **kwargs):
            if kind == 'owner_approved':
                raise RuntimeError('simulated disk failure')
            return original(db, kind, *args, **kwargs)
        with patch.object(self.store, 'event', side_effect=fail_after_hook), self.assertRaises(RuntimeError):
            self.confirm(row)
        self.assertEqual(interview.get(self.store, self.task, row['id'], self.actor)['status'], 'draft')
        self.assertFalse(self.store.get_decision(self.node)['signed_by'])
        self.assertEqual(self.store.graph.count_events('interview_confirmed', decision_id=self.node), 0)
        self.assertEqual(self.confirm(row)['status'], 'confirmed')

    def test_cancel_racing_confirmation_cannot_be_overwritten(self):
        row = self.draft(self.create())
        original = self.store.answer
        def cancel_then_answer(*args, **kwargs):
            interview.update(self.store, self.task, row['id'], {'expected_version': row['version']}, self.actor, 'cancel')
            return original(*args, **kwargs)
        with patch.object(self.store, 'answer', side_effect=cancel_then_answer), self.assertRaises(Invalid):
            self.confirm(row)
        self.assertFalse(self.store.get_decision(self.node)['signed_by'])
        self.assertEqual(interview.get(self.store, self.task, row['id'], self.actor)['status'], 'cancelled')

    def model_answer(self):
        return {'question': 'Which old clients require ten seconds?', 'question_quote': 'old clients keep ten',
                'proposed_answer': 'Use five seconds for new clients and ten for old clients.',
                'proposed_rationale': 'Preserve the old-client exception.',
                'answer_quotes': ['Keep five seconds, except old clients keep ten.'],
                'caveats': [{'text': 'Old clients retain ten seconds.', 'quote': 'old clients keep ten'}]}

    def advance(self, row, cfg=None):
        return interview.advance(self.store, self.task, row['id'], {'expected_version': row['version']},
                                 self.actor, cfg or Config(model_api='none'))

    def test_no_model_is_truthfully_labelled_and_never_fabricates_readback(self):
        row = self.advance(self.draft(self.create()))
        self.assertEqual(row['guidance'], {'mode': 'deterministic-guided-prompts', 'reason': 'model_unavailable'})
        self.assertFalse(self.store.get_decision(self.node)['signed_by'])

    def test_model_followup_and_caveats_are_grounded_unapproved_and_durable(self):
        row = self.draft(self.create(), turns=[{'prompt_id': 0, 'response': 'Keep five seconds, except old clients keep ten.'}])
        with patch.object(Config, 'semantic_retrieval', property(lambda _: True)), \
             patch('bridge.llm.Client.complete_json', return_value=self.model_answer()) as model:
            row = self.advance(row, Config())
        self.assertEqual(row['guidance']['mode'], 'model-assisted')
        self.assertEqual(row['prompts'][1], 'Which old clients require ten seconds?')
        self.assertEqual(row['guidance']['status'], 'unapproved')
        self.assertIn('org/runtime', model.call_args.args[2])
        self.assertFalse(self.store.get_decision(self.node)['signed_by'])
        self.store.graph.close(); self.store = Store(self.path)
        self.assertEqual(interview.get(self.store, self.task, row['id'], self.actor)['guidance']['caveats'], self.model_answer()['caveats'])
        # Editing invalidates the proposal until a new model readback is requested.
        self.assertEqual(self.draft(row)['guidance'], {})

    def test_invalid_or_failed_model_response_uses_explicit_fallback(self):
        for result in ({'confirmed': True}, {**self.model_answer(), 'question_quote': 'invented'},
                       {**self.model_answer(), 'answer_quotes': []}, {**self.model_answer(), 'caveats': [{'text': 'false', 'quote': 'invented'}]}):
            row = self.draft(self.create(key=str(result))) if len(str(result)) < 100 else self.draft(self.create(key=str(hash(str(result)))))
            with patch.object(Config, 'semantic_retrieval', property(lambda _: True)), \
                 patch('bridge.llm.Client.complete_json', return_value=result):
                output = self.advance(row, Config())
            self.assertEqual(output['guidance']['reason'], 'invalid_model_response')
        row = self.draft(self.create(key='failed-model'))
        with patch.object(Config, 'semantic_retrieval', property(lambda _: True)), \
             patch('bridge.llm.Client.complete_json', side_effect=LLMError('provider secret must not leak')):
            output = self.advance(row, Config())
        self.assertEqual(output['guidance']['reason'], 'model_failed')
        self.assertNotIn('secret', str(output['guidance']))

    def test_model_result_is_discarded_if_interview_is_cancelled_during_inference(self):
        row = self.draft(self.create())
        def model(*args, **kwargs):
            interview.update(self.store, self.task, row['id'], {'expected_version': row['version']}, self.actor, 'cancel')
            return self.model_answer()
        with patch.object(Config, 'semantic_retrieval', property(lambda _: True)), \
             patch('bridge.llm.Client.complete_json', side_effect=model), self.assertRaises(Invalid):
            self.advance(row, Config())
        self.assertEqual(interview.get(self.store, self.task, row['id'], self.actor)['guidance'], {})


class InterviewHTTPTests(InterviewTests):
    def setUp(self):
        super().setUp()
        self.auth = Auth(self.store, enabled=True)
        self.token = self.auth.create_token(self.person, kind='human')['token']
        self.server = ready_server(self.store, port=0, auth=self.auth)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def request(self, path, data=None, token=None, cookie='', csrf=''):
        headers = {'Content-Type': 'application/json'}
        if token is not None: headers['Authorization'] = 'Bearer ' + token
        if cookie: headers['Cookie'] = cookie
        if csrf: headers['X-Bridge-CSRF'] = csrf
        req = Request(self.base + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
        try:
            with urlopen(req) as result:
                return result.status, json.loads(result.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_http_routes_enforce_auth_person_scope_and_csrf(self):
        path = f'/api/tasks/{self.task}/interviews'
        data = {'decision_id': self.node, 'client_key': 'http'}
        self.assertEqual(self.request(path, data, token='bad')[0], 401)
        agent = self.auth.create_token(self.person, kind='agent')['token']
        self.assertEqual(self.request(path, data, token=agent)[0], 403)
        cookie = self.auth.session_cookie(self.person).split(';')[0]
        self.assertEqual(self.request(path, data, cookie=cookie)[0], 403)
        code, state = self.request('/api/state', cookie=cookie)
        self.assertEqual(code, 200)
        code, row = self.request(path, data, cookie=cookie, csrf=state['csrf_token'])
        self.assertEqual(code, 200)
        self.assertEqual(row['person_id'], self.person)
        self.assertEqual(self.request(path + '/' + row['id'], token=agent)[0], 403)
        self.assertEqual(self.request(path + '/' + row['id'], token=self.token)[0], 200)
        outsider = self.auth.create_token(self.other, kind='human')['token']
        self.assertEqual(self.request(path + '/' + row['id'], token=outsider)[0], 404)
        self.assertEqual(self.request(path, token=outsider)[1]['interviews'], [])


if __name__ == '__main__':
    unittest.main()
