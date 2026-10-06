"""Combined source-review, approval-scope and standing-grant regressions.

Only deterministic public store/canvas/graph flows and the existing offline
Slack and verified Teams callback fixtures are used. No provider calls or real
messages are sent. Historical snapshots are asserted unchanged, not upgraded.
"""
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
import test_delivery as slack
import test_readback_generations as generations
import test_source_review_readbacks as source_fixtures
import test_teams as teams
from test_rules import RuleCase
import test_rule_invalidation as rule_fixtures
from bridge import canvas, context_memory as cm, proof, source_review
from bridge.approval_scope import snapshot as approval_scope, review_epoch, transport_text
from bridge.config import Config
from bridge.delivery import _sections, complete_chat_text
from bridge.store import Invalid, Store
from bridge.teams import ConnectorAuth, TeamsBotTransport, TeamsConfig, TeamsDelivery


class CombinedSourceScopeCases:
    source = source_fixtures.SourceReviewCases.source
    source_row = source_fixtures.SourceReviewCases.source_row
    setup_source = source_fixtures.SourceReviewCases.setup_source
    decision = source_fixtures.SourceReviewCases.decision
    sign = source_fixtures.SourceReviewCases.sign
    held = source_fixtures.SourceReviewCases.held
    offer = source_fixtures.SourceReviewCases.offer
    confirm = source_fixtures.SourceReviewCases.confirm
    consent_count = source_fixtures.SourceReviewCases.consent_count

    def set_scope(self):
        scope = {
            'question': 'ROOT_QUESTION_735: bill this release?',
            'context': 'ROOT_CONTEXT_921 applies to this rollout only.',
            'facts': json.dumps({'customer': 'ROOT_CUSTOMER_319', 'release': 'r17'}),
            'scope_paths': json.dumps(['billing/usage.py', 'billing/rollout.py']),
            'scope_key': 'root-specific-release', 'category': 'billing-policy',
            'options': json.dumps(['Bill scoped traffic', 'Exclude scoped tests']),
            'applicability': json.dumps({'requires': {'release': 'r17'}}),
        }
        for key, value in scope.items():
            self.graph.db.execute(f'UPDATE decisions SET {key}=? WHERE id=?', (value, self.node_id))
        return scope

    def refresh_notification(self):
        note = self.store.notify(self.node_id, 'signoff', to='Wes Chen')
        self.assertIsNotNone(note)
        self.delivery.deliver_now()
        delivered = self.delivery.get(note['id'])
        self.assertEqual(delivered['state'], 'sent')
        self.channel_id, self.thread_id = delivered['external_ref'].split(':', 1)
        if self.delivery.channel == 'slack':
            self.message = {'channel': self.channel_id, 'ts': self.thread_id}
        else:
            self.thread = dict(self.graph.db.execute('SELECT * FROM teams_threads WHERE id=?',
                                                     (self.thread_id,)).fetchone())

    def semantic_offer(self, answer, peer=False):
        with generations.interpretation(answer, True):
            self.chat(answer, peer=peer)
        held = self.held(peer)
        self.assertIsNotNone(held, self.last_text())
        self.assertTrue(held['source_review'])
        self.assertEqual(held['kind'], 'conversation')
        return held

    def test_complete_root_is_displayed_once_beside_current_sources(self):
        scope = self.set_scope()
        held = self.offer('sign off')
        payload = json.loads(held['source_review'])
        self.assertEqual(set(payload['decision']), set(source_review.DECISION_CONTEXT_FIELDS))
        for key in source_review.DECISION_CONTEXT_FIELDS:
            self.assertEqual(payload['decision'][key], self.decision()[key], key)
        for marker in ('ROOT_QUESTION_735', 'ROOT_CONTEXT_921', 'ROOT_CUSTOMER_319',
                       'billing/rollout.py', 'root-specific-release', 'billing-policy',
                       'Bill scoped traffic', 'Exclude scoped tests'):
            self.assertEqual(held['prompt'].count(marker), 1, marker)
        self.assertEqual(payload['decision']['facts'], scope['facts'])
        self.assertEqual(payload['source_evidence'], self.decision()['source_revalidation']['pins'])
        self.assertIn(self.changed['source']['source_version_id'], held['prompt'])
        self.assertEqual(held['prompt'], self.last_text())
        self.confirm(held)
        self.assertTrue(self.decision()['authorized'], self.last_text())
        self.assertEqual(json.loads(self.decision()['signatures'])[0]['scope']['facts'],
                         json.loads(scope['facts']))

    def test_source_and_action_markup_are_escaped_exactly_once(self):
        source_text = 'SOURCE_DATA <!channel> <at>Everyone</at> & *source* `source`'
        answer = 'ACTION_DATA <https://example.test|approved> & *answer* `answer`'
        self.source(body=source_text)
        held = self.offer('answer: ' + answer)
        displayed = held['prompt']
        self.assertEqual(json.loads(held['source_review'])['sources'][0]['snapshot']['body'], source_text)
        for raw in (source_text, answer):
            self.assertNotIn(raw, displayed)
        self.assertIn(source_review.escape_text(source_text), displayed)
        self.assertIn(transport_text(answer), displayed)
        for char in ('&', '<', '>', '*', '`'):
            self.assertNotIn('\\u005cu%04x' % ord(char), displayed)
        self.assertEqual(displayed.count('```'), 2)
        self.confirm(held)
        self.assertTrue(self.decision()['authorized'], self.last_text())
        self.assertEqual(self.decision()['answer'], answer)
        self.assertEqual(self.consent_count(), 1)

    def test_complete_combined_prompt_obeys_utf8_and_block_limits(self):
        self.set_scope()
        self.source(body='COMPLETE_SOURCE ' + '界' * 700 + ' SOURCE_END')
        answer = 'COMPLETE_ACTION ' + '界' * 700 + ' ACTION_END'
        held = self.offer('answer: ' + answer)
        self.assertEqual(held['prompt'], self.last_text())
        self.assertTrue(complete_chat_text(held['prompt']))
        self.assertLessEqual(len(held['prompt'].encode('utf-8')), 12000)
        self.assertLessEqual(len(_sections(held['prompt'], 2900)), 50)
        self.assertEqual(''.join(_sections(held['prompt'], 2900)), held['prompt'])
        self.assertIn('SOURCE_END', held['prompt'])
        self.assertIn(transport_text('ACTION_END'), held['prompt'])
        # The source and action each fit; their complete composition does not.
        oversized = 'COMPLETE_ACTION ' + '界' * 3000 + ' ACTION_END'
        self.assertLess(len(oversized.encode('utf-8')), 12000)
        self.chat('answer: ' + oversized)
        self.assertIsNone(self.held())
        self.assertIn('too large', self.last_text().lower())
        self.assertNotIn('confirm ', self.last_text())
        self.confirm(held)
        self.assertFalse(self.decision()['authorized'])
        self.assertEqual(self.consent_count(), 0)

    def test_final_block_budget_applies_to_combined_rendering(self):
        held = self.offer('sign off')
        observed = []

        def fifty_one_lossless_sections(text, _size):
            observed.append(text)
            # Force the otherwise-independent provider block ceiling while
            # preserving every byte. Normal 12 KB prompts need fewer blocks.
            return [text[i:i + 1] for i in range(50)] + [text[50:]]

        with patch('bridge.delivery._sections', side_effect=fifty_one_lossless_sections):
            self.chat('answer: BLOCK_BUDGET_ACTION')
        self.assertTrue(any(transport_text('BLOCK_BUDGET_ACTION') in text and 'Current-source review:' in text
                            and 'Billing policy' in text for text in observed))
        self.assertIsNone(self.held())
        self.assertIn('No shortened read-back can be confirmed', self.last_text())
        self.confirm(held)
        self.assertFalse(self.decision()['authorized'])
        self.assertEqual(self.consent_count(), 0)

    def test_source_pin_change_refuses_complete_scoped_proposal_atomically(self):
        self.set_scope()
        held = self.offer('answer: Apply only to ROOT_CUSTOMER_319 for this task.')
        pinned = json.loads(held['source_review'])['source_evidence']
        self.source(body='A newer policy arrived after the complete scoped reading.')
        before = self.decision()
        self.assertNotEqual(pinned, before['source_revalidation']['pins'])
        self.confirm(held)
        after = self.decision()
        self.assertFalse(after['authorized'])
        for key in ('answer', 'signatures', 'sources', 'updated_at', 'reusable'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(self.consent_count(), 0)

    def test_material_scope_change_refuses_current_source_proposal(self):
        self.set_scope()
        held = self.offer('sign off')
        before = self.decision()
        self.graph.db.execute('UPDATE decisions SET facts=? WHERE id=?',
                              (json.dumps({'customer': 'DIFFERENT_CUSTOMER', 'release': 'r18'}), self.node_id))
        self.assertEqual(self.decision()['updated_at'], before['updated_at'])
        self.assertEqual(self.decision()['source_revalidation']['pins'],
                         json.loads(held['source_review'])['source_evidence'])
        self.confirm(held)
        self.assertFalse(self.decision()['authorized'])
        self.assertEqual(self.decision()['signatures'], before['signatures'])
        self.assertEqual(self.consent_count(), 0)

    def test_fresh_same_answer_cosign_keeps_scope_and_current_source_pins(self):
        self.set_scope()
        self.graph.db.execute('UPDATE decisions SET required_signers=? WHERE id=?',
                              (json.dumps(['Wes Chen', self.peer_name]), self.node_id))
        self.refresh_notification()
        answer = self.decision()['answer']
        first = self.semantic_offer(answer)
        second = self.semantic_offer(answer, peer=True)
        self.assertNotIn('Proposed answer applicability: {}', first['prompt'])
        self.confirm(first)
        self.assertFalse(self.decision()['authorized'])
        signatures = json.loads(self.decision()['signatures'])
        self.assertEqual([s['by'] for s in signatures], ['Wes Chen'])
        self.confirm(second, peer=True)
        self.assertFalse(self.decision()['authorized'])
        self.assertEqual(json.loads(self.decision()['signatures']), signatures)
        fresh = self.semantic_offer(answer, peer=True)
        self.assertNotEqual(second['proposal_id'], fresh['proposal_id'])
        self.assertEqual(json.loads(first['source_review'])['source_evidence'],
                         json.loads(fresh['source_review'])['source_evidence'])
        self.assertNotIn('Proposed answer applicability: {}', fresh['prompt'])
        self.confirm(fresh, peer=True)
        final = self.decision()
        self.assertTrue(final['authorized'], self.last_text())
        self.assertEqual(final['answer'], answer)
        self.assertEqual(json.loads(final['applicability']), {'requires': {'release': 'r17'}})
        self.assertEqual({s['by'] for s in json.loads(final['signatures'])}, {'Wes Chen', self.peer_name})
        for signature in json.loads(final['signatures']):
            self.assertEqual(signature['scope']['facts']['customer'], 'ROOT_CUSTOMER_319')
        self.assertEqual(self.consent_count(), 2)

    def grant_current_rule(self):
        held = self.offer('sign off')
        self.confirm(held)
        self.assertTrue(self.decision()['authorized'], self.last_text())
        self.store.make_rule(self.node_id, {'by': 'Wes Chen', 'scope': 'same',
                                          'expected_updated_at': self.decision()['updated_at']})
        self.assertTrue(self.decision()['reusable'])

    def test_changed_source_signoff_discloses_retirement_and_signs_post_action_scope(self):
        self.set_scope()
        self.grant_current_rule()
        previous_versions = cm.history(self.graph.db, self.node_id)
        self.source(body='New source premise requires a fresh standing grant.')
        before = self.decision()
        self.assertTrue(before['source_revalidation']['retires_rule'])
        held = self.offer('sign off')
        self.assertIn('retire the existing standing rule', held['prompt'])
        self.assertIn('fresh explicit make-rule action', held['prompt'])
        self.assertEqual(json.loads(held['source_review'])['decision']['reusable'], 1)
        self.confirm(held)
        after = self.decision()
        self.assertTrue(after['authorized'], self.last_text())
        self.assertEqual(after['answer'], before['answer'])
        self.assertEqual(after['applicability'], before['applicability'])
        self.assertFalse(after['reusable'])
        self.assertTrue(after['rule_ended_at'])
        signature = json.loads(after['signatures'])[0]
        self.assertEqual(signature['scope'], approval_scope(after))
        self.assertEqual(signature['scope']['reusable'], 0)
        self.assertEqual(cm.history(self.graph.db, self.node_id)[:len(previous_versions)], previous_versions)
        self.store.make_rule(self.node_id, {'by': 'Wes Chen', 'scope': 'same',
                                          'expected_updated_at': after['updated_at']})
        self.assertTrue(self.decision()['reusable'])
        self.assertFalse(self.decision()['rule_ended_at'])

    def test_identical_source_signoff_preserves_standing_grant_and_scope(self):
        self.set_scope()
        self.grant_current_rule()
        before = self.decision()
        self.assertFalse(before['source_revalidation']['retires_rule'])
        held = self.offer('sign off')
        self.assertNotIn('retire the existing standing rule', held['prompt'])
        self.confirm(held)
        after = self.decision()
        self.assertTrue(after['authorized'], self.last_text())
        for key in ('reusable', 'rule_conditions', 'rule_scope', 'rule_expires', 'rule_ended_at',
                    'applicability', 'facts', 'scope_paths', 'signatures'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after['source_revalidation']['pins'], before['source_revalidation']['pins'])
        self.assertEqual(self.graph.count_events('rule_ended', decision_id=self.node_id), 0)

    def test_final_combined_byte_budget_includes_rule_effect_and_confirmation(self):
        self.grant_current_rule()
        self.source(body='Changed source premise for the final combined message budget.')
        base_answer = 'Final combined budget '
        initial = self.offer('answer: ' + base_answer + 'x')
        self.assertIn('retire the existing standing rule', initial['prompt'])
        padding = 12000 - len(initial['prompt'].encode('utf-8'))
        self.assertGreater(padding, 0)
        answer = base_answer + 'x' * (padding + 1)
        held = self.offer('answer: ' + answer)
        self.assertEqual(len(held['prompt'].encode('utf-8')), 12000)
        self.assertLessEqual(len(_sections(held['prompt'], 2900)), 50)
        self.assertEqual(held['prompt'], self.last_text())
        self.chat('answer: ' + answer + 'x')
        self.assertIsNone(self.held())
        self.assertIn('No shortened read-back can be confirmed', self.last_text())
        self.assertTrue(self.decision()['reusable'])
        self.confirm(held)
        self.assertFalse(self.decision()['authorized'])
        self.assertEqual(self.consent_count(), 1)  # Only the original pre-rule approval.

    def test_old_ordinary_notification_cannot_authorize_changed_sources(self):
        # The initial notification predates source attachment and source v2.
        before = self.decision()
        self.chat('yes')
        self.assertFalse(self.decision()['authorized'])
        self.assertEqual(self.decision()['sources'], before['sources'])
        self.assertEqual(self.consent_count(), 0)
        held = self.offer('sign off')
        self.assertEqual(json.loads(held['source_review'])['source_evidence'],
                         before['source_revalidation']['pins'])
        self.assertFalse(self.decision()['authorized'])
        self.confirm(held)
        self.assertTrue(self.decision()['authorized'], self.last_text())

    def test_old_ordinary_notification_cannot_authorize_changed_scope(self):
        self.set_scope()
        self.chat('yes')
        self.assertFalse(self.decision()['authorized'])
        self.assertEqual(self.consent_count(), 0)
        held = self.offer('sign off')
        self.assertIn('ROOT_CUSTOMER_319', held['prompt'])
        self.assertFalse(self.decision()['authorized'])
        self.confirm(held)
        self.assertTrue(self.decision()['authorized'], self.last_text())


class SlackCombinedSourceScopeTests(CombinedSourceScopeCases, slack.DeliveryCase):
    event = generations.SlackReadbackGenerationTests.event
    chat = source_fixtures.SlackSourceReviewTests.chat
    last_text = source_fixtures.SlackSourceReviewTests.last_text

    def setUp(self):
        super().setUp()
        self.node_id = self.node(self.task())['node_id']
        self.delivery.deliver_now()
        self.message = self.slack.messages[0]
        self.channel_id, self.thread_id = self.message['channel'], self.message['ts']
        self.counter = 0
        self.peer, self.peer_name = self.marisol, 'Marisol Vega'
        self.addCleanup(self.delivery.close)
        self.setup_source()


@unittest.skipUnless(teams.jwt, 'Install requirements-teams.txt for verified Teams callbacks')
class TeamsCombinedSourceScopeTests(CombinedSourceScopeCases, OfflineCase):
    activity = teams.TeamsTests.activity
    token = teams.TeamsTests.token
    submit = teams.TeamsTests.submit
    stamped = generations.TeamsReadbackGenerationTests.stamped
    chat = source_fixtures.TeamsSourceReviewTests.chat
    last_text = source_fixtures.TeamsSourceReviewTests.last_text

    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'teams-combined-source-scope.db')
        self.graph = self.store.graph
        with self.graph.transaction():
            self.wes = self.graph.add_person('Wes Chen', email='wes@example.test')
            self.peer_name = 'Other Person'
            self.peer = self.graph.add_person(self.peer_name, email='other@example.test')
            self.graph.add_authority('path', 'billing/*', 'decides', person_id=self.wes)
        self.config = TeamsConfig(teams.APP, teams.TENANT, teams.CHANNEL, 'fixture-secret',
                                  {teams.OWNER: self.wes, teams.OTHER: self.peer})
        self.microsoft = generations.UniqueReplyMicrosoftFixture()
        self.transport = TeamsBotTransport(self.graph, self.config, http=self.microsoft)
        self.auth = ConnectorAuth(self.config, http=self.microsoft)
        self.delivery = TeamsDelivery(self.store, self.config, self.transport, self.auth,
                                      base_url='https://bridge.acme.test')
        self.store._delivery = self.delivery
        quiet = patch.object(self.delivery.inbox, 'start')
        quiet.start()
        self.addCleanup(quiet.stop)
        self.addCleanup(self.delivery.close)
        task = canvas.start_task(self.store, Config(model_api='none'),
            {'title': 'Usage billing', 'repo': 'acme/platform', 'paths': 'billing/usage.py'})['task_id']
        self.node_id = canvas.add_node(self.store, Config(model_api='none'), {'task_id': task,
            'question': 'Bill the usage spike?', 'paths': 'billing/usage.py',
            'context': 'Enterprise-two load test'})['node_id']
        self.delivery.deliver_now()
        self.thread = dict(self.graph.db.execute('SELECT * FROM teams_threads').fetchone())
        self.channel_id, self.thread_id = self.config.destination, self.thread['id']
        self.counter = 0
        self.epoch = time.time() + 1
        self.setup_source()


class SourcedRuleIntegrationTests(RuleCase):
    save_proof = rule_fixtures.RuleInvalidationTests.save_proof

    def record(self, body='Round half up to whole cents', **changes):
        return self.store.add_record({'repo': 'acme/ledger', 'kind': 'jira', 'ref': 'ROUND-1',
            'title': 'Rounding policy', 'body': body, 'author': 'Source Writer',
            'status': 'Done', 'url': 'https://example.invalid/ROUND-1', 'paths': ['billing/rates.py'], **changes})

    def record_row(self, record):
        return dict(self.graph.db.execute('SELECT i.*,s.id AS record_id,s.head_id AS source_version_id '
            'FROM intents i JOIN source_records s ON s.intent_id=i.id WHERE s.id=?',
            (record['source']['record_id'],)).fetchone())

    def source_rule(self):
        task = self.task('source-rule')
        source = self.node(task)['node_id']
        record = self.record()
        self.graph.publish_evidence(source, [self.record_row(record)], status='resolved', source='record',
            answer='Round half up to whole cents', kind='evidence', signoff='required')
        current = self.store.get_decision(source)
        self.store.answer(source, {'answer': current['answer'], 'signed_by': 'Priya Natarajan',
                                  'expected_updated_at': current['updated_at']})
        self.rule(source)
        canvas.finish_task(self.store, {'task_id': task})
        return source

    def review(self, source):
        current = self.store.get_decision(source)
        review = current['source_revalidation']
        self.assertTrue(review['available'], review['notice'])
        return {'expected_updated_at': current['updated_at'], 'source_evidence': review['pins'],
                'source_decision_pins': review['decision_pins']}

    def test_sourced_human_correction_retires_rule_until_explicit_regrant_and_keeps_history(self):
        source = self.source_rule()
        completed_task = self.task('completed-consumer')
        completed = self.node(completed_task, ref='completed-consumer')
        self.assertTrue(completed['authorized'])
        old_proof = self.save_proof(completed_task)
        historical = self.store.get_decision(completed['node_id'])
        active = self.node(self.task('active-consumer'), ref='active-consumer')
        self.assertTrue(active['authorized'])
        versions = [dict(row) for row in self.graph.db.execute('SELECT * FROM decision_versions ORDER BY id')]
        old_events = [dict(row) for row in self.graph.db.execute('SELECT * FROM events ORDER BY id')]
        changed_record = self.record('Round half even for new invoices')
        changed = self.store.answer(source, {'answer': 'Round half even', 'signed_by': 'Priya Natarajan',
                                             **self.review(source)})
        self.assertTrue(changed['authorized'])
        self.assertFalse(changed['reusable'])
        self.assertTrue(changed['rule_ended_at'])
        self.assertEqual(changed['sources'][0]['source_version_id'], changed_record['source']['source_version_id'])
        self.assertEqual(json.loads(changed['signatures'])[0]['scope']['reusable'], 0)
        self.assertFalse(canvas.node_view(self.store, active['node_id'])['authorized'])
        before_grant = self.node(self.task('before-regrant'), ref='before-regrant')
        self.assertFalse(before_grant['authorized'])
        self.rule(source)
        self.assertTrue(self.store.get_decision(source)['reusable'])
        self.assertFalse(self.store.get_decision(source)['rule_ended_at'])
        self.assertTrue(self.node(self.task('after-regrant'), ref='after-regrant')['authorized'])
        exported = proof.export(self.store, {'task_id': completed_task})
        self.assertEqual(exported['bundle'], old_proof)
        self.assertTrue(exported['integrity']['valid'])
        current_history = self.store.get_decision(completed['node_id'])
        for key in ('answer', 'signatures', 'signed_hash', 'signed_revision'):
            self.assertEqual(current_history[key], historical[key], key)
        for row in versions:
            self.assertEqual(dict(self.graph.db.execute('SELECT * FROM decision_versions WHERE id=?',
                                                        (row['id'],)).fetchone()), row)
        for row in old_events:
            self.assertEqual(dict(self.graph.db.execute('SELECT * FROM events WHERE id=?',
                                                        (row['id'],)).fetchone()), row)

    def test_sourced_correction_rolls_back_grant_pins_history_and_dependents_together(self):
        source = self.source_rule()
        active = self.node(self.task('rollback-consumer'), ref='rollback-consumer')
        self.assertTrue(active['authorized'])
        before = self.store.get_decision(source)
        history = cm.history(self.graph.db, source)
        events = self.graph.count_events('rule_ended', decision_id=source)
        with patch.object(self.graph, 'flag_dependents', side_effect=RuntimeError('synthetic sourced rollback')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic sourced rollback'):
                self.store.answer(source, {'answer': 'Round half even', **self.review(source)})
        after = self.store.get_decision(source)
        for key in ('answer', 'signatures', 'updated_at', 'reusable', 'rule_ended_at', 'sources'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(cm.history(self.graph.db, source), history)
        self.assertEqual(self.graph.count_events('rule_ended', decision_id=source), events)
        self.assertTrue(canvas.node_view(self.store, active['node_id'])['authorized'])

    def test_changed_source_signoff_rolls_back_grant_pins_and_history_together(self):
        source = self.source_rule()
        self.record('New version of the rounding premise')
        before = self.store.get_decision(source)
        self.assertTrue(before['source_revalidation']['retires_rule'])
        history = cm.history(self.graph.db, source)
        events = self.graph.count_events('rule_ended', decision_id=source)
        with patch.object(self.graph, 'invalidate_rule_dependents',
                          side_effect=RuntimeError('synthetic signoff retirement rollback')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic signoff retirement rollback'):
                canvas.sign_off(self.store, source, {'by': 'Priya Natarajan', **self.review(source)})
        after = self.store.get_decision(source)
        for key in ('answer', 'signatures', 'updated_at', 'reusable', 'rule_ended_at', 'sources'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(cm.history(self.graph.db, source), history)
        self.assertEqual(self.graph.count_events('rule_ended', decision_id=source), events)

    def test_source_content_reversion_never_revives_historical_pins_or_standing_grant(self):
        source = self.source_rule()
        original = self.store.get_decision(source)
        old_review = self.review(source)
        old_pin = old_review['source_evidence'][0]
        old_source_version = dict(self.graph.db.execute('SELECT * FROM source_versions WHERE id=?',
                                                        (old_pin['source_version_id'],)).fetchone())
        old_versions = [dict(row) for row in self.graph.db.execute(
            'SELECT * FROM decision_versions WHERE decision_id=? ORDER BY sequence', (source,))]
        old_proof = proof.create(self.store, original['run_id'],
            'diff --git a/billing/rates.py b/billing/rates.py\n+rounding = "half up"\n')
        active = self.node(self.task('reversion-active'), ref='reversion-active')
        self.assertTrue(active['authorized'])
        previous_epoch = review_epoch(original)
        seen = {old_pin['source_version_id']}
        for body, status in (('Round half up to whole cents', 'Superseded'),
                             ('Round half even for new invoices', 'Done'),
                             ('Round half up to whole cents', 'Done')):
            with self.subTest(body=body, status=status):
                imported = self.record(body, status=status)
                self.assertTrue(imported['changed'])
                version = imported['source']['source_version_id']
                self.assertNotIn(version, seen)
                seen.add(version)
                current = self.store.get_decision(source)
                epoch = review_epoch(current)
                self.assertEqual(epoch[:-1], previous_epoch)
                self.assertEqual(len(epoch), len(previous_epoch) + 1)
                previous_epoch = epoch
                self.assertFalse(canvas.node_view(self.store, active['node_id'])['authorized'])
                self.assertEqual(cm.pin(current['sources'][0]), old_pin)
        current = self.store.get_decision(source)
        current_pin = current['source_revalidation']['pins'][0]
        self.assertNotEqual(current_pin['source_version_id'], old_pin['source_version_id'])
        reverted = dict(self.graph.db.execute('SELECT * FROM source_versions WHERE id=?',
                                              (current_pin['source_version_id'],)).fetchone())
        self.assertEqual(reverted['fingerprint'], old_source_version['fingerprint'])
        self.assertGreater(reverted['sequence'], old_source_version['sequence'])
        # Even with a fresh root timestamp, the old A pin is not today's A.
        with self.assertRaises(Invalid):
            canvas.sign_off(self.store, source, {'by': 'Priya Natarajan', **old_review,
                                                 'expected_updated_at': current['updated_at']})
        self.assertFalse(self.node(self.task('reversion-before-review'), ref='reversion-before-review')['authorized'])
        canvas.sign_off(self.store, source, {'by': 'Priya Natarajan', **self.review(source)})
        self.assertFalse(self.store.get_decision(source)['reusable'])
        self.assertTrue(self.store.get_decision(source)['rule_ended_at'])
        self.assertFalse(self.node(self.task('reversion-before-grant'), ref='reversion-before-grant')['authorized'])
        self.rule(source)
        self.assertTrue(self.node(self.task('reversion-after-grant'), ref='reversion-after-grant')['authorized'])
        self.assertEqual(dict(self.graph.db.execute('SELECT * FROM source_versions WHERE id=?',
                                                    (old_pin['source_version_id'],)).fetchone()), old_source_version)
        for version in old_versions:
            self.assertEqual(dict(self.graph.db.execute('SELECT * FROM decision_versions WHERE id=?',
                                                        (version['id'],)).fetchone()), version)
        exported = proof.export(self.store, {'task_id': original['run_id']})
        self.assertEqual(exported['bundle'], old_proof)
        self.assertTrue(exported['integrity']['valid'])

    def test_noop_current_head_import_preserves_grant_pins_and_review_epoch(self):
        source = self.source_rule()
        active = self.node(self.task('noop-import-active'), ref='noop-import-active')
        self.assertTrue(active['authorized'])
        before = self.store.get_decision(source)
        before_active = self.store.get_decision(active['node_id'])
        pin = cm.pin(before['sources'][0])
        version_rows = [dict(row) for row in self.graph.db.execute(
            'SELECT * FROM source_versions WHERE record_id=? ORDER BY sequence', (pin['record_id'],))]
        history = cm.history(self.graph.db, source)
        imported = self.record()
        self.assertFalse(imported['changed'])
        self.assertEqual(imported['affected_decisions'], [])
        self.assertEqual(imported['source']['source_version_id'], pin['source_version_id'])
        after = self.store.get_decision(source)
        for key in ('answer', 'updated_at', 'reusable', 'rule_ended_at', 'rule_conditions',
                    'rule_scope', 'rule_expires', 'signatures', 'signed_revision'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(cm.pin(after['sources'][0]), pin)
        self.assertEqual(review_epoch(after), review_epoch(before))
        self.assertEqual(review_epoch(self.store.get_decision(active['node_id'])), review_epoch(before_active))
        self.assertTrue(canvas.node_view(self.store, active['node_id'])['authorized'])
        self.assertEqual(cm.history(self.graph.db, source), history)
        self.assertEqual([dict(row) for row in self.graph.db.execute(
            'SELECT * FROM source_versions WHERE record_id=? ORDER BY sequence', (pin['record_id'],))], version_rows)

    def test_noop_graph_evidence_refresh_preserves_standing_grant_and_source_pins(self):
        source = self.source_rule()
        before = self.store.get_decision(source)
        pin = cm.pin(before['sources'][0])
        imported = self.record()
        self.assertFalse(imported['changed'])
        source_version = dict(self.graph.db.execute('SELECT * FROM source_versions WHERE id=?',
                                                    (pin['source_version_id'],)).fetchone())
        history = [dict(row) for row in self.graph.db.execute(
            'SELECT * FROM decision_versions WHERE decision_id=? ORDER BY sequence', (source,))]
        self.graph.publish_evidence(source, [self.record_row(imported)],
            answer=before['answer'], evidence='Refreshed explanatory metadata; same answer and source pins.')
        after = self.store.get_decision(source)
        self.assertTrue(after['authorized'])
        for key in ('answer', 'reusable', 'rule_ended_at', 'rule_conditions', 'rule_scope',
                    'rule_expires', 'signatures', 'signed_revision'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(cm.pin(after['sources'][0]), pin)
        self.assertEqual(review_epoch(after), review_epoch(before))
        self.assertEqual(self.graph.count_events('rule_ended', decision_id=source), 0)
        self.assertTrue(self.node(self.task('noop-graph-reuse'), ref='noop-graph-reuse')['authorized'])
        self.assertEqual(dict(self.graph.db.execute('SELECT * FROM source_versions WHERE id=?',
                                                    (pin['source_version_id'],)).fetchone()), source_version)
        for version in history:
            self.assertEqual(dict(self.graph.db.execute('SELECT * FROM decision_versions WHERE id=?',
                                                        (version['id'],)).fetchone()), version)

    def test_direct_graph_update_preserves_metadata_only_grant_but_retires_changed_answer(self):
        source = self.source_rule()
        active = self.node(self.task('direct-update-active'), ref='direct-update-active')
        self.assertTrue(active['authorized'])
        before = self.store.get_decision(source)
        pins = before['source_revalidation']['pins']
        self.graph.update_decision(source, evidence='Metadata-only explanatory refresh.')
        metadata_only = self.store.get_decision(source)
        self.assertTrue(metadata_only['authorized'])
        for key in ('reusable', 'rule_ended_at', 'rule_conditions', 'rule_scope', 'rule_expires', 'signatures'):
            self.assertEqual(metadata_only[key], before[key], key)
        self.assertEqual(metadata_only['source_revalidation']['pins'], pins)
        self.assertEqual(review_epoch(metadata_only), review_epoch(before))
        self.assertEqual(self.graph.count_events('rule_ended', decision_id=source), 0)
        self.graph.update_decision(source, answer='Round half even')
        changed = self.store.get_decision(source)
        self.assertFalse(changed['reusable'])
        self.assertTrue(changed['rule_ended_at'])
        self.assertEqual(changed['source_revalidation']['pins'], pins)
        self.assertFalse(canvas.node_view(self.store, active['node_id'])['authorized'])
        canvas.sign_off(self.store, source, {'by': 'Priya Natarajan', **self.review(source)})
        signed = self.store.get_decision(source)
        self.assertTrue(signed['authorized'])
        self.assertFalse(signed['reusable'])
        self.assertEqual(json.loads(signed['signatures'])[0]['scope'], approval_scope(signed))
        self.assertFalse(self.node(self.task('direct-before-grant'), ref='direct-before-grant')['authorized'])
        self.rule(source)
        self.assertTrue(self.node(self.task('direct-after-grant'), ref='direct-after-grant')['authorized'])

    def test_graph_source_answer_change_requires_fresh_explicit_rule_grant(self):
        source = self.source_rule()
        changed_record = self.record('Round half even for new invoices')
        self.graph.publish_evidence(source, [self.record_row(changed_record)], status='resolved', source='record',
            answer='Round half even', kind='evidence', signoff='required')
        proposed = self.store.get_decision(source)
        self.assertFalse(proposed['reusable'])
        self.assertTrue(proposed['rule_ended_at'])
        canvas.sign_off(self.store, source, {'by': 'Priya Natarajan', **self.review(source)})
        signed = self.store.get_decision(source)
        self.assertTrue(signed['authorized'])
        self.assertFalse(signed['reusable'])
        self.assertFalse(self.node(self.task('graph-before-grant'), ref='graph-before-grant')['authorized'])
        self.rule(source)
        self.assertTrue(self.node(self.task('graph-after-grant'), ref='graph-after-grant')['authorized'])


if __name__ == '__main__':
    unittest.main()
