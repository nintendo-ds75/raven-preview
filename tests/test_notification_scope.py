"""Complete scoped notifications, stale direct commands, and atomic regrant."""
import json
import threading
from unittest.mock import patch

from bridge import canvas
from bridge.authz import Actor
from bridge.delivery import complete_chat_text
from test_delivery import DeliveryCase
from test_rules import RuleCase


class NotificationScopeTests(DeliveryCase):
    def test_public_large_scope_sends_bounded_notice_and_direct_signoff_refuses(self):
        self.addCleanup(self.delivery.close)
        facts = {f'fact{i:03d}': '&' * 200 for i in range(130)}
        task = self.task()
        node = self.node(task, facts=facts)['node_id']
        answer = 'Only exclude the staging load test; production is always billed.'
        canvas.settle_node(self.store, {'task_id': task, 'node_id': node, 'answer': answer,
                                       'rationale': 'A synthetic scoped policy.'})
        self.delivery.deliver_now()
        message = next(m for m in self.slack.messages if 'Sign-off wanted' in m['text'])
        self.assertTrue(complete_chat_text(message['text']))
        self.assertLessEqual(len(message['blocks']), 50)
        self.assertIn('Nothing can be approved from this notice', message['text'])
        note = self.delivery.notification_for_thread(message['channel'], message['ts'])
        self.assertFalse(json.loads(note['payload'])['approval_review']['complete'])
        reply = self.reply(message, 'UWES', 'sign off')
        self.assertIn('Not recorded', reply)
        self.assertFalse(self.store.get_decision(node)['authorized'])

    def test_new_scope_notification_does_not_rebind_old_thread(self):
        self.addCleanup(self.delivery.close)
        with self.graph.transaction():
            self.graph.add_authority('path', 'billing/usage.py', 'approves', person_id=self.marisol)
        node = self.node(self.task())['node_id']
        actor = Actor.person(self.graph.get_person(self.wes), kind='session')
        for environment in ('staging', 'production'):
            row = self.store.get_decision(node)
            self.store.answer(node, {'answer': 'Exclude the load test.',
                'applicability': {'requires': {'environment': environment}}, 'expected_updated_at': row['updated_at']}, actor=actor)
            self.delivery.deliver_now()
            message = [m for m in self.slack.messages if m['channel'] == 'DUMAR' and 'Sign-off wanted' in m['text']][-1]
            if environment == 'staging': old = message
        self.assertNotEqual(old['ts'], message['ts'])
        self.assertFalse(message.get('thread_ts'))
        self.assertIn('scope changed', self.reply(old, 'UMAR', 'sign off'))
        self.assertFalse(self.store.get_decision(node)['authorized'])
        self.assertIn('Signed off', self.reply(message, 'UMAR', 'sign off'))
        self.assertTrue(self.store.get_decision(node)['authorized'])

    def test_public_long_context_restriction_is_visible_before_direct_signoff(self):
        self.addCleanup(self.delivery.close)
        context = 'Background context. ' * 90 + '\nOptions: Staging only; production is prohibited.'
        self.assertGreater(context.index('Staging only'), 1500)
        task = self.task(); node = self.node(task, context=context)['node_id']
        answer = 'Exclude the load test.'
        canvas.settle_node(self.store, {'task_id': task, 'node_id': node, 'answer': answer, 'rationale': 'Test.'})
        self.delivery.deliver_now()
        message = [m for m in self.slack.messages if 'Sign-off wanted' in m['text']][-1]
        visible = ''.join(b['text']['text'] for b in message['blocks'])
        self.assertIn('Options: Staging only; production is prohibited.', visible)
        self.assertIn(answer, visible)
        self.assertEqual(visible, message['text'])
        self.assertTrue(complete_chat_text(visible))
        self.assertIn('Signed off', self.reply(message, 'UWES', 'sign off'))
        self.assertTrue(self.store.get_decision(node)['authorized'])

    def assert_context_is_exact_before_signoff(self, producer, context):
        from bridge.approval_scope import transport_text
        from bridge.config import Config
        from bridge.ladder import ask
        self.addCleanup(self.delivery.close)
        task = self.task('Complete authored context')
        question = 'Can we exclude this test usage?'
        if producer == 'api':
            # The exact function invoked by POST /api/decisions.
            row = ask(self.store, Config(model_api='none'), task, question,
                      context=context, path='billing/usage.py',
                      owner_id=self.graph.owner_id_for('Wes Chen'))
            node = row['id']
        else:
            node = self.node(task, question=question, context=context)['node_id']
        stored = self.store.get_decision(node)['context']
        self.assertIn(context, stored)
        canvas.settle_node(self.store, {'task_id': task, 'node_id': node,
            'answer': 'Exclude the test usage within the stated constraints.', 'rationale': 'Synthetic policy.'})
        self.delivery.deliver_now()
        message = [m for m in self.slack.messages if 'Sign-off wanted' in m['text']][-1]
        visible = ''.join(block['text']['text'] for block in message['blocks'])
        self.assertEqual(visible, message['text'])
        self.assertIn(transport_text(stored), visible)
        self.assertIn(transport_text(context), visible)
        note = self.delivery.notification_for_thread(message['channel'], message['ts'])
        self.assertEqual(note['decision_id'], node)
        self.assertTrue(json.loads(note['payload'])['approval_review']['complete'])
        self.assertFalse(self.store.get_decision(node)['authorized'])
        self.assertIn('Signed off', self.reply(message, 'UWES', 'sign off'))
        self.assertTrue(self.store.get_decision(node)['authorized'])

    def test_api_authored_paths_context_is_exact_before_signoff(self):
        self.assert_context_is_exact_before_signoff('api', 'Paths: staging only; production is prohibited.')

    def test_api_authored_options_context_is_exact_before_signoff(self):
        self.assert_context_is_exact_before_signoff('api', 'Options: keep production billed; only staging may be excluded.')

    def test_api_long_authored_context_is_exact_before_signoff(self):
        self.assert_context_is_exact_before_signoff('api', 'Background context. ' * 90 + '\nPaths: production is prohibited.')

    def test_canvas_authored_paths_context_is_exact_before_signoff(self):
        self.assert_context_is_exact_before_signoff('canvas', 'Paths: staging only; production is prohibited.')

    def test_canvas_authored_options_context_is_exact_before_signoff(self):
        self.assert_context_is_exact_before_signoff('canvas', 'Preserve  internal spacing.\nPaths: staging only.\nOptions: production is prohibited.')

    def test_canvas_long_authored_context_is_exact_before_signoff(self):
        self.assert_context_is_exact_before_signoff('canvas', 'Background context. ' * 90 + '\nOptions: production is prohibited.')

    def test_legacy_unscoped_notification_cannot_authorize(self):
        self.addCleanup(self.delivery.close)
        task = self.task()
        node = self.node(task)['node_id']
        canvas.settle_node(self.store, {'task_id': task, 'node_id': node, 'answer': 'Exclude only staging.', 'rationale': 'Test.'})
        self.delivery.deliver_now()
        message = self.slack.messages[-1]
        note = self.delivery.notification_for_thread(message['channel'], message['ts'])
        payload = json.loads(note['payload']); payload.pop('approval_review')
        self.graph.db.execute('UPDATE notifications SET payload=? WHERE id=?', (json.dumps(payload), note['id']))
        reply = self.reply(message, 'UWES', 'sign off')
        self.assertIn('complete bound scope review', reply)
        self.assertIn('Raven', reply)
        self.assertFalse(self.store.get_decision(node)['authorized'])

    def test_same_timestamp_scope_change_is_rechecked_inside_signoff_writer(self):
        self.addCleanup(self.delivery.close)
        task = self.task(); node = self.node(task)['node_id']
        canvas.settle_node(self.store, {'task_id': task, 'node_id': node, 'answer': 'Exclude staging.', 'rationale': 'Test.'})
        self.delivery.deliver_now(); message = self.slack.messages[-1]
        original = self.delivery._stale
        def change_after_check(note, decision, person):
            result = original(note, decision, person)
            self.assertFalse(result)
            self.graph.db.execute('UPDATE decisions SET facts=? WHERE id=?',
                                  (json.dumps({'environment': 'production'}), node))
            return result
        with patch.object(self.delivery, '_stale', side_effect=change_after_check):
            result = self.reply(message, 'UWES', 'sign off')
        self.assertIn('scope changed', result)
        self.assertFalse(self.store.get_decision(node)['authorized'])

    def test_chat_sections_preserve_every_character_without_hidden_tail(self):
        from bridge.delivery import _sections
        text = ('a' * 2899 + '  end\n') * 55
        parts = _sections(text, 2900)
        self.assertGreater(len(parts), 50)
        self.assertEqual(''.join(parts), text)
        self.assertFalse(complete_chat_text(text))

    def test_context_is_complete_or_review_online_not_silently_clipped(self):
        from bridge.delivery import render
        row = {'id': 'test', 'question': 'Apply policy?', 'context': 'a' * 1700 + ' END OF POLICY', 'facts': '{}'}
        payload = render(row, 'ask', 'Owner', '')
        self.assertTrue(payload['review_complete'])
        self.assertIn('END OF POLICY', payload['text'])
        row['context'] = '界' * 5000
        payload = render(row, 'ask', 'Owner', '')
        self.assertFalse(payload['review_complete'])
        self.assertTrue(complete_chat_text(payload['text']))


class RegrantRaceTests(RuleCase):
    def test_old_rule_request_cannot_regrant_replacement_after_preflight(self):
        self.addCleanup(self.delivery.close)
        node = self.signed_answer(); self.rule(node)
        old = self.store.get_decision(node)
        captured, release = threading.Event(), threading.Event()
        done = []
        original = self.store.get_decision
        def gated_get(decision_id):
            row = original(decision_id)
            if threading.current_thread().name == 'stale-rule' and not captured.is_set():
                captured.set()
                if not release.wait(10): raise RuntimeError('race synchronization timeout')
            return row
        def grant():
            try:
                self.store.make_rule(node, {'by': 'Priya Natarajan', 'scope': 'same', 'expected_updated_at': old['updated_at']})
                done.append('applied')
            except Exception as error: done.append(type(error).__name__)
            finally: self.graph.close_thread()
        with patch.object(self.store, 'get_decision', gated_get):
            thread = threading.Thread(target=grant, name='stale-rule'); thread.start()
            self.assertTrue(captured.wait(10))
            try:
                changed = self.store.answer(node, {'answer': 'Round half even for this task only.', 'expected_updated_at': old['updated_at']})
                self.assertFalse(changed['reusable'])
            finally: release.set()
            thread.join(10); self.assertFalse(thread.is_alive())
        self.assertEqual(done, ['Invalid'])
        self.assertFalse(self.store.get_decision(node)['reusable'])
