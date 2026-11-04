"""The judgment log: how a sign-off arrived, why a precedent fits, and the
one action that corrects a settled call and makes the correction the next
precedent under the person's name."""
import json
from unittest.mock import patch

from fixtures import OfflineCase
from raven import canvas, judgment, ladder
from raven.authz import Actor, Refused
from raven.config import Config
from raven.store import Invalid


REPO = 'background/policy'
QUESTION = 'How long should policy audit records remain?'
ANSWER = 'Keep policy audit records for thirty days.'
CORRECTION = 'Keep policy audit records for ninety days; thirty was the pre-audit figure.'


class JudgmentLogTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store('judgment-log.db')
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.store.add_owner({'name': 'Policy Owner', 'team': 'Policy', 'patterns': 'policy/*'})
        self.graph.set_setting('auto_rules', '1')
        for method in ('complete', 'complete_json'):
            guard = patch('raven.llm.Client.' + method, side_effect=AssertionError('Provider calls forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def node(self, question=QUESTION):
        task = self.store.add_run({'title': 'Review audit retention', 'repo': REPO})['id']
        did = self.graph.add_decision(task, question, 'policy', 'pending', repo=REPO,
                                      owner='Policy Owner', path='policy/retention.py')
        return task, did

    def precedent(self, source='inbox: Policy Owner', rule=True):
        task, did = self.node()
        self.store.answer(did, {'answer': ANSWER, 'signed_by': 'Policy Owner', 'source': source})
        if rule:
            self.store.make_rule(did, {'by': 'Policy Owner', 'scope': 'any',
                                       'expected_updated_at': self.store.get_decision(did)['updated_at']})
        return task, did

    def settle_from_memory(self, did):
        """The background pass settles this node from memory, offline."""
        self.graph.db.execute('UPDATE decisions SET model_pending=1 WHERE id=?', (did,))
        current = self.store.get_decision(did)
        observed = {}
        original = ladder.ask

        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            observed.update(result)
            return result

        with patch('raven.ladder.ask', side_effect=read):
            canvas._model_node_pass(self.store, Config(model_api='none'), did, '',
                                    ['policy/retention.py'], [], '', current['owner_id'], '', current['updated_at'])
        return observed

    def events(self, did, kind):
        return [json.loads(e['detail']) for e in self.store.get_decision(did)['events'] if e['kind'] == kind]

    # ---- how the sign-off arrived ----

    def test_an_answer_records_the_channel_it_arrived_on(self):
        for source, via in (('inbox: Policy Owner', 'browser'), ('slack: Policy Owner (read back and confirmed)', 'slack'),
                            ('link: Policy Owner', 'browser'), ('teams: Policy Owner', 'teams'), ('local operator', 'operator')):
            with self.subTest(source=source):
                _, did = self.node()
                self.store.answer(did, {'answer': ANSWER, 'signed_by': 'Policy Owner', 'source': source})
                self.assertEqual(self.store.get_decision(did)['answered_via'], via)

    def test_a_confirmed_voice_interview_is_a_voice_call(self):
        task, did = self.node()
        person = self.store.add_person({'name': 'Policy Owner'})['id']
        db = self.graph.db
        for interview_id, method, via in (('iv-voice', 'browser-speech', 'voice call'), ('iv-typed', 'typed', 'call (typed)')):
            db.execute("INSERT INTO interviews(id, task_id, decision_id, person_id, client_key, decision_revision, scope, "
                       "capture_method, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (interview_id, task, did, person, interview_id, 'r', '{}', method, 'now', 'now'))
            self.assertEqual(judgment.received_via(db, f'confirmed interview: {interview_id}'), via)
        self.assertEqual(judgment.received_via(db, '', Actor(id='p1', name='Policy Owner', kind='slack')), 'slack')
        self.assertEqual(judgment.received_via(db, '', Actor(id='p1', name='Policy Owner', kind='session')), 'browser')
        self.assertEqual(judgment.received_via(db, 'something new', None), '')

    def test_a_plain_signature_records_its_channel_too(self):
        _, source = self.precedent(rule=False)
        _, did = self.node()
        self.settle_from_memory(did)
        self.assertEqual(self.store.get_decision(did)['signoff'], 'required')
        canvas.sign_off(self.store, did, {'by': 'Policy Owner', 'source': 'slack: Policy Owner',
                                          'expected_updated_at': self.store.get_decision(did)['updated_at']})
        self.assertEqual(self.store.get_decision(did)['answered_via'], 'slack')

    # ---- why the precedent fits ----

    def test_settling_from_a_rule_says_why_it_fits_and_asks_nobody(self):
        _, source = self.precedent()
        _, did = self.node()
        observed = self.settle_from_memory(did)
        self.assertEqual(observed['source'], 'memory')
        current = self.store.get_decision(did)
        self.assertEqual(current['source_id'], source)
        self.assertEqual(current['signoff'], 'rule')
        self.assertTrue(current['authorized'])
        self.assertIn('same question Policy Owner already answered', current['fit_reason'])
        self.assertIn('applies anywhere, so nobody needed to be asked', current['fit_reason'])

    def test_a_signed_answer_that_is_not_a_rule_says_a_person_still_signs(self):
        _, source = self.precedent(rule=False)
        _, did = self.node()
        self.settle_from_memory(did)
        current = self.store.get_decision(did)
        self.assertEqual(current['source_id'], source)
        self.assertFalse(current['authorized'])
        self.assertIn('did not make it a standing rule, so a person still signs it here', current['fit_reason'])

    # ---- the correction ----

    def test_a_correction_retires_the_precedent_and_becomes_the_next_one(self):
        _, source = self.precedent()
        task, did = self.node()
        self.settle_from_memory(did)
        before = self.store.get_decision(did)
        result = judgment.correct(self.store, did, {
            'answer': CORRECTION, 'why': 'Thirty days was the figure before the audit; the auditors asked for ninety.',
            'scope': 'everywhere', 'expected_updated_at': before['updated_at']})
        self.assertEqual(result['replaced'], source)
        self.assertIn('retired', result['notice'])
        corrected = self.store.get_decision(did)
        self.assertEqual(corrected['status'], 'approved')
        self.assertEqual(corrected['answer'], CORRECTION)
        self.assertEqual(corrected['answered_by'], 'Policy Owner')
        self.assertEqual(corrected['signoff'], 'signed')
        self.assertEqual(corrected['supersedes'], source)
        self.assertFalse(corrected['source_id'])
        self.assertEqual(corrected['answered_via'], 'browser')
        self.assertTrue(corrected['authorized'])
        # The correction holds the old call's standing: a rule that applies anywhere.
        self.assertTrue(corrected['reusable'])
        self.assertEqual(corrected['rule_scope'], 'any')
        old = self.store.get_decision(source)
        self.assertEqual(old['superseded_by'], did)
        self.assertEqual(self.events(did, 'precedent_corrected')[0]['replaced'], source)
        self.assertEqual(self.events(did, 'previous_answer')[0]['answer'], ANSWER)
        # The next similar question is settled from the correction, not the old call.
        _, later = self.node()
        observed = self.settle_from_memory(later)
        self.assertEqual(observed['source'], 'memory')
        after = self.store.get_decision(later)
        self.assertEqual(after['source_id'], did)
        self.assertEqual(after['answer'], CORRECTION)
        self.assertTrue(after['authorized'])
        self.assertFalse(self.graph.blocking_nodes(task))

    def test_a_correction_for_this_case_only_keeps_the_old_call(self):
        _, source = self.precedent()
        _, did = self.node()
        self.settle_from_memory(did)
        result = judgment.correct(self.store, did, {
            'answer': CORRECTION, 'why': 'This task is the audit itself; the usual window does not apply.',
            'scope': 'here', 'expected_updated_at': self.store.get_decision(did)['updated_at']})
        self.assertEqual(result['replaced'], '')
        self.assertIn('stays for other cases', result['notice'])
        self.assertEqual(self.store.get_decision(source)['superseded_by'], '')
        corrected = self.store.get_decision(did)
        self.assertEqual(corrected['answer'], CORRECTION)
        self.assertEqual(corrected['supersedes'], '')
        self.assertTrue(corrected['authorized'])
        # Being built reads the correction from state: a correction for this
        # case, and not a confirmation of what Raven had applied.
        listed = next(d for d in self.store.state()['decisions'] if d['id'] == did)
        self.assertEqual(listed['corrected_scope'], 'here')
        self.assertIs(listed['took_guess'], False)

    def test_a_correction_needs_standing_a_revision_and_a_reason(self):
        _, source = self.precedent()
        _, did = self.node()
        self.settle_from_memory(did)
        current = self.store.get_decision(did)
        with self.assertRaises(Refused):
            judgment.correct(self.store, did, {'answer': CORRECTION, 'why': 'no', 'expected_updated_at': current['updated_at']},
                             actor=Actor(id='somebody-else', name='Someone Else'))
        with self.assertRaises(Invalid):
            judgment.correct(self.store, did, {'answer': CORRECTION, 'why': 'no', 'expected_updated_at': 'stale'})
        with self.assertRaises(Invalid):
            judgment.correct(self.store, did, {'answer': CORRECTION, 'expected_updated_at': current['updated_at']})
        with self.assertRaises(Invalid):
            judgment.correct(self.store, did, {'answer': CORRECTION, 'why': 'no', 'scope': 'sometimes',
                                               'expected_updated_at': current['updated_at']})
        self.assertEqual(self.store.get_decision(did)['answer'], ANSWER)
        self.assertEqual(self.store.get_decision(source)['superseded_by'], '')

    def test_a_pending_question_is_answered_not_corrected(self):
        _, did = self.node()
        with self.assertRaises(Invalid):
            judgment.correct(self.store, did, {'answer': CORRECTION, 'why': 'no',
                                               'expected_updated_at': self.store.get_decision(did)['updated_at']})
