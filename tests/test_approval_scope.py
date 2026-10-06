"""Current-main structured consent scope through offline real callback paths."""
import json
import unittest
from unittest.mock import patch

from fixtures import OfflineCase
import test_delivery as slack_fixtures
import test_teams as teams_fixtures
import test_readback_generations as generations
from bridge import canvas, interview
from bridge.store import Invalid
import test_interview as interviews


SCOPE = {
    'facts': json.dumps({'customer': 'Synthetic North', 'environment': 'staging', 'release': 'r17'}),
    'scope_paths': json.dumps(['billing/usage.py', 'billing/policy.py']),
    'scope_key': 'synthetic-request-scope', 'category': 'billing-policy',
    'options': json.dumps(['Bill', 'Exclude']),
    'applicability': json.dumps({'requires': {'environment': 'staging'}, 'excludes': {'customer': 'synthetic south'},
                                  'paths': ['billing/'], 'valid_until': '2099-01-01T00:00:00+00:00'}),
}
CHANGES = {**SCOPE, 'repo': 'synthetic/other', 'path': 'billing/other.py',
           'facts': json.dumps({'customer': 'Synthetic South', 'environment': 'production', 'release': 'r18'}),
           'scope_paths': '["billing/other.py"]', 'scope_key': 'other-scope', 'category': 'security',
           'options': '["Publish"]', 'applicability': '{}', 'required_signers': '["Other Person"]',
           'followup_required': 1, 'reusable': 1, 'rule_conditions': 'environment=production',
           'rule_scope': 'any', 'rule_expires': '2099-01-02', 'rule_ended_at': '2026-10-06'}


def set_scope(case):
    for key, value in SCOPE.items():
        case.graph.db.execute(f'UPDATE decisions SET {key}=? WHERE id=?', (value, case.decision_id))


class ScopeCallbacks:
    def test_initial_notification_and_exact_readback_display_structured_scope(self):
        set_scope(self)
        row = self.store.get_decision(self.decision_id)
        from bridge.delivery import render
        initial = render(row, 'ask', 'Wes Chen', '', task_link='https://example.test/task')['text']
        for semantic in (False, True):
            with self.subTest(semantic=semantic):
                held = self.propose(semantic)
                for prompt in (initial, held['prompt']):
                    for value in ('Synthetic North', 'staging', 'r17', 'billing/policy.py', 'billing-policy',
                                  'synthetic-request-scope', 'synthetic south', '2099-01-01', 'Exclude'):
                        self.assertIn(value, prompt)
                self.assertEqual(held['prompt'], self.sent_prompt())

    def test_same_timestamp_structured_scope_changes_reject_both_readback_parsers(self):
        set_scope(self)
        for semantic in (False, True):
            for key, replacement in CHANGES.items():
                with self.subTest(semantic=semantic, field=key):
                    old = self.store.get_decision(self.decision_id)
                    held = self.propose(semantic)
                    self.graph.db.execute(f'UPDATE decisions SET {key}=? WHERE id=?', (replacement, self.decision_id))
                    self.assertEqual(self.store.get_decision(self.decision_id)['updated_at'], old['updated_at'])
                    self.confirm_proposal(held, semantic)
                    current = self.store.get_decision(self.decision_id)
                    self.assertFalse(current['authorized'])
                    self.assertFalse(current['answer'])
                    self.assertEqual(self.graph.count_events('readback_confirmed', decision_id=self.decision_id), 0)
                    self.assertIsNone(self.held())
                    self.graph.db.execute(f'UPDATE decisions SET {key}=? WHERE id=?', (old[key], self.decision_id))

    def test_unchanged_answer_cosign_preserves_existing_standing_grant(self):
        set_scope(self)
        self.store.answer(self.decision_id, {'answer': 'Exclude the load test.',
                                            'applicability': json.loads(SCOPE['applicability'])})
        before = self.store.get_decision(self.decision_id)
        self.store.make_rule(self.decision_id, {'by': 'Wes Chen', 'scope': 'same',
                            'expected_updated_at': before['updated_at']})
        before = self.store.get_decision(self.decision_id)
        self.refresh_message()
        held = self.propose(True)
        self.assertNotIn('retire the existing standing rule', held['prompt'])
        self.confirm_proposal(held, True)
        current = self.store.get_decision(self.decision_id)
        self.assertTrue(current['reusable'])
        self.assertEqual(current['applicability'], before['applicability'])
        self.assertEqual(current['rule_ended_at'], before['rule_ended_at'])

    def test_task_only_callback_replacement_visibly_retires_existing_rule(self):
        for semantic in (False, True):
            with self.subTest(semantic=semantic):
                self.store.answer(self.decision_id, {'answer': 'Exclude the load test.'})
                before = self.store.get_decision(self.decision_id)
                self.store.make_rule(self.decision_id, {'by': 'Wes Chen', 'scope': 'same',
                                    'expected_updated_at': before['updated_at']})
                self.refresh_message()
                answer = 'Bill the load test for this task only; this is not a standing rule.'
                self.send_unheld(answer, semantic)
                held = self.held()
                self.assertIsNotNone(held)
                self.assertIn('retire the existing standing rule', held['prompt'])
                self.confirm_proposal(held, semantic)
                current = self.store.get_decision(self.decision_id)
                self.assertTrue(current['authorized'])
                self.assertEqual(current['answer'], answer)
                self.assertFalse(current['reusable'])
                self.assertEqual(json.loads(current['signatures'])[0]['scope']['reusable'], 0)

    def test_action_summary_markup_is_literal_and_recorded_answer_is_exact(self):
        from bridge.approval_scope import transport_text
        answer = '<https://example.test/permit-production|Permit staging only> <!channel> *bold*'
        self.send_unheld(answer, True)
        held = self.held()
        self.assertIsNotNone(held)
        self.assertNotIn(answer, self.sent_prompt())
        self.assertIn(transport_text(answer), self.sent_prompt())
        self.confirm_proposal(held, True)
        self.assertEqual(self.store.get_decision(self.decision_id)['answer'], answer)

    def test_scope_markup_is_literal_but_bound_values_remain_exact(self):
        from bridge.approval_scope import transport_text
        malicious = '<@UWES> <!channel> <https://example.test|link> [link](https://example.test) `code` *bold*'
        set_scope(self)
        self.graph.db.execute('UPDATE decisions SET facts=? WHERE id=?',
                             (json.dumps({'customer': malicious}), self.decision_id))
        held = self.propose(True)
        self.assertNotIn(malicious, self.sent_prompt())
        self.assertIn(transport_text(malicious), self.sent_prompt())
        from bridge.delivery import render
        initial = render(self.store.get_decision(self.decision_id), 'ask', 'Wes Chen', '')['text']
        self.assertNotIn(malicious, initial)
        self.assertIn(transport_text(malicious), initial)
        self.confirm_proposal(held, True)
        signature = json.loads(self.store.get_decision(self.decision_id)['signatures'])[0]
        self.assertEqual(signature['scope']['facts']['customer'], malicious)

    def test_oversize_complete_prompt_retires_old_proposal_without_truncation(self):
        held = self.propose(True)
        # Multibyte values prove the budget is bytes after display escaping.
        self.graph.db.execute('UPDATE decisions SET facts=? WHERE id=?',
                             (json.dumps({'customer': '界' * 5000}), self.decision_id))
        self.send_unheld('Exclude the load test.', True)
        self.assertIsNone(self.held())
        self.assertIn('No shortened read-back can be confirmed', self.sent_prompt())
        self.confirm_proposal(held, True)
        self.assertFalse(self.store.get_decision(self.decision_id)['authorized'])

    def test_replacement_displays_new_applicability_and_records_signature_scope(self):
        set_scope(self)
        held = self.propose(True)
        self.confirm_proposal(held, True)
        current = self.store.get_decision(self.decision_id)
        self.assertTrue(current['authorized'])
        self.assertIn('Proposed answer applicability: {}', held['prompt'])
        self.assertEqual(json.loads(current['applicability']), {})
        signature = json.loads(current['signatures'])[0]
        self.assertEqual(signature['scope']['facts'], json.loads(SCOPE['facts']))
        self.assertEqual(signature['scope']['applicability'], {})
        consent = next(e for e in current['events'] if e['kind'] == 'readback_confirmed')
        self.assertEqual(json.loads(consent['detail'])['proposal']['prompt'], held['prompt'])

    def test_cosigner_signature_alone_keeps_other_persons_proposal_current(self):
        set_scope(self)
        self.graph.add_person('Third Reviewer', email='third@example.test')
        self.graph.db.execute('UPDATE decisions SET required_signers=? WHERE id=?',
                             (json.dumps(['Wes Chen', 'Other Person', 'Third Reviewer']), self.decision_id))
        self.store.answer(self.decision_id, {'answer': 'Exclude the load test.', 'signed_by': 'Third Reviewer',
                                            'applicability': json.loads(SCOPE['applicability'])})
        self.refresh_message()
        held = self.propose(True)
        before = self.store.get_decision(self.decision_id)
        canvas.sign_off(self.store, self.decision_id, {'by': 'Other Person',
                        'expected_updated_at': before['updated_at']})
        self.assertEqual(self.held(), held)
        self.assertNotIn('Proposed answer applicability: {}', held['prompt'])
        self.confirm_proposal(held, True)
        self.assertTrue(self.store.get_decision(self.decision_id)['authorized'])
        self.assertEqual(self.graph.count_events('readback_confirmed', decision_id=self.decision_id), 1)
        self.assertEqual(json.loads(self.store.get_decision(self.decision_id)['applicability']), json.loads(SCOPE['applicability']))


class SlackApprovalScopeTests(ScopeCallbacks, slack_fixtures.DeliveryCase):
    event = generations.SlackReadbackGenerationTests.event
    send = generations.SlackReadbackGenerationTests.send
    held = generations.SlackReadbackGenerationTests.held
    offer = generations.SlackReadbackGenerationTests.offer

    def setUp(self):
        super().setUp()
        self.node_id = self.node(self.task())['node_id']
        self.delivery.deliver_now()
        self.message = self.slack.messages[0]
        self.counter = 0
        self.addCleanup(self.delivery.close)
        self.decision_id = self.node_id

    def propose(self, semantic):
        return self.offer(semantic=semantic)

    def confirm_proposal(self, held, semantic):
        with generations.interpretation('Exclude the load test.', semantic):
            self.send('confirm ' + held['proposal_id'])

    def send_unheld(self, answer, semantic):
        with generations.interpretation(answer, semantic):
            self.send(answer)

    def sent_prompt(self):
        return self.slack.messages[-1]['text']

    def refresh_message(self):
        self.store.notify(self.decision_id, 'signoff', to='Wes Chen')
        self.delivery.deliver_now()
        last = self.slack.messages[-1]
        self.message = {**last, 'ts': last.get('thread_ts') or last['ts']}


@unittest.skipUnless(teams_fixtures.jwt, 'Install requirements-teams.txt for verified Teams callbacks')
class TeamsApprovalScopeTests(ScopeCallbacks, generations.TeamsReadbackGenerationTests):
    activity = generations.TeamsReadbackGenerationTests.activity
    token = generations.TeamsReadbackGenerationTests.token
    submit = generations.TeamsReadbackGenerationTests.submit
    decision = generations.TeamsReadbackGenerationTests.decision
    held = generations.TeamsReadbackGenerationTests.held
    stamped = generations.TeamsReadbackGenerationTests.stamped
    offer = generations.TeamsReadbackGenerationTests.offer

    def setUp(self):
        # Call the fixture implementation with its own lexical super context.
        generations.TeamsReadbackGenerationTests.setUp(self)
        self.decision_id = self.node
        self.scope_clock = 0

    def propose(self, semantic):
        self.scope_clock += 2
        return self.offer(seconds=self.scope_clock, semantic=semantic)

    def confirm_proposal(self, held, semantic):
        self.scope_clock += 1
        with generations.interpretation('Exclude the load test.', semantic):
            self.submit(self.stamped('confirm ' + held['proposal_id'], self.scope_clock))

    def send_unheld(self, answer, semantic):
        self.scope_clock += 2
        with generations.interpretation(answer, semantic):
            self.submit(self.stamped(answer, self.scope_clock))

    def sent_prompt(self):
        return self.microsoft.messages[-1]['text']

    def refresh_message(self):
        self.store.notify(self.decision_id, 'signoff', to='Wes Chen')
        self.delivery.deliver_now()
        self.thread = dict(self.graph.db.execute('SELECT * FROM teams_threads ORDER BY id DESC').fetchone())


class InterviewApprovalScopeTests(interviews.InterviewTests):
    def test_interview_preserves_scope_and_rejects_same_timestamp_fact_change(self):
        self.graph = self.store.graph
        self.decision_id = self.node
        set_scope(self)
        from bridge import briefing
        row = self.store.get_decision(self.node)
        self.assertIn('Synthetic North', row['approval_scope_text'])
        link = briefing.resolve(self.graph, briefing.mint(self.graph, self.person, self.task, self.node))
        focus = briefing.overview(self.store, link)['focus']
        self.assertEqual(focus['approval_scope_text'], row['approval_scope_text'])
        for key, replacement in CHANGES.items():
            with self.subTest(field=key):
                row = self.draft(self.create(key='scope-' + key))
                self.assertEqual(row['scope']['decision_scope']['facts'], json.loads(SCOPE['facts']))
                self.assertIn('Synthetic North', row['scope']['decision_scope_text'])
                old = self.store.get_decision(self.node)
                self.graph.db.execute(f'UPDATE decisions SET {key}=? WHERE id=?', (replacement, self.node))
                self.assertEqual(self.store.get_decision(self.node)['updated_at'], old['updated_at'])
                with self.assertRaisesRegex(Invalid, 'decision changed'):
                    self.confirm(row)
                self.assertFalse(self.store.get_decision(self.node)['authorized'])
                self.assertEqual(self.graph.count_events('interview_confirmed', decision_id=self.node), 0)
                self.graph.db.execute(f'UPDATE decisions SET {key}=? WHERE id=?', (old[key], self.node))



class ApprovalScopeCanonicalTests(unittest.TestCase):
    def test_json_encoding_is_stable_but_fact_values_are_exact(self):
        from bridge.approval_scope import revision, snapshot
        a = {'facts': '{"customer": "Synthetic  North", "release": "r17"}'}
        b = {'facts': '{ "release":"r17", "customer":"Synthetic  North" }'}
        self.assertEqual(revision(a), revision(b))
        self.assertNotEqual(revision(a), revision({'facts': '{"customer":"Synthetic North","release":"r17"}'}))
        self.assertEqual(snapshot({'context': 'customer=Other; environment=production'})['facts'], {})
