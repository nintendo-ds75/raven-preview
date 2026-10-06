import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from test_delivery import DeliveryCase
from test_slack_conversation import ConversationTests
from bridge import canvas
from bridge.authz import Actor
from bridge.delivery import _handon_scope


class TemporaryCoverTests(ConversationTests):
    def test_vacation_substitute_is_for_this_question_only(self):
        offered = self.say('I am on vacation this week; ask Marisol Vega to cover.',
                           {'kind': 'handoff', 'to': 'Marisol Vega'})
        self.assertIn('for this question only', offered)
        self.say('yes')
        d = self.store.get_decision(self.n['node_id'])
        self.assertEqual(d['owner_name'], 'Marisol Vega')
        self.store.answer(d['id'], {'answer': 'Exclude internal tests.', 'rationale': 'Temporary cover'},
                          actor=Actor.person(self.graph.get_person(self.marisol)))
        self.assertFalse(self.graph.db.execute('SELECT 1 FROM routing_feedback WHERE decision_id=?', (d['id'],)).fetchone())
        self.assertFalse([a for a in self.graph.authority_rows() if a['person_id'] == self.marisol])

    def test_explicit_temporary_command_does_not_learn_permanent_route(self):
        self.assertEqual(_handon_scope('while I am on leave'), {'scope_kind': 'none'})
        self.assertEqual(_handon_scope('for this week'), {'scope_kind': 'none'})

    def test_reframing_in_slack_requires_explicit_readback_confirmation(self):
        offered = self.say('The question is wrong. Ask whether only internal calls should be excluded.',
                          {'kind': 'reframe', 'answer': 'Should only internal calls be excluded?', 'rationale': ''})
        self.assertIn('Clear its old signatures', offered)
        self.assertNotEqual(self.store.get_decision(self.n['node_id'])['question'], 'Should only internal calls be excluded?')
        self.say('yes')
        d = self.store.get_decision(self.n['node_id'])
        self.assertEqual(d['question'], 'Should only internal calls be excluded?')
        self.assertFalse(d['authorized'])


class EscalationTests(DeliveryCase):
    def test_unanswered_question_contacts_one_alternate_without_transferring_authority(self):
        node = self.node(self.task())
        old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        self.graph.db.execute('UPDATE decisions SET created_at=? WHERE id=?', (old, node['node_id']))
        self.store.update_settings({'overdue_hours': 2})
        before = self.store.get_decision(node['node_id'])
        with patch('bridge.routing.rank_for_decision', return_value=[('Wes Chen', ['primary'], 3),
                                                                   ('Marisol Vega', ['review history'], 2)]):
            self.delivery.remind_overdue()
            self.delivery.remind_overdue()
        notes = [r for r in self.delivery.list() if r['kind'] == 'escalation']
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]['person_name'], 'Marisol Vega')
        self.assertIn('not an ownership transfer', json.loads(self.delivery.get(notes[0]['id'])['payload'])['text'])
        after = self.store.get_decision(node['node_id'])
        self.assertEqual(before['owner_id'], after['owner_id'])
        self.assertFalse(after['authorized'])
        self.delivery.deliver_now()
        message = next(m for m in self.slack.messages if 'help unblock' in m['text'])
        response = self.reply(message, 'UMAR', 'answer: Waive everything because I saw the message')
        self.assertIn('Not recorded', response)
        self.assertFalse(self.store.get_decision(node['node_id'])['authorized'])
        self.assertIn('Context added', self.reply(message, 'UMAR', 'context: Wes returns tomorrow; keep billing open.'))
        self.assertIn('Wes returns', str(canvas.get_tree(self.store, node['task_id'])['notes']))
