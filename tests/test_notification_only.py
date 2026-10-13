"""Fresh Slack installations notify only. No answer or context leaves Raven."""
import json
from unittest.mock import patch
from test_delivery import DeliveryCase
from bridge.delivery import handle_slack_event

class NotificationOnlyTests(DeliveryCase):
    def setUp(self):
        super().setUp()
        self.graph.set_setting('slack_interaction_mode', '')

    def test_default_sends_only_authenticated_deep_link_without_minting_bearer(self):
        n=self.node(self.task(), question='Private customer pricing decision')
        self.assertEqual(self.delivery.deliver_now(),1)
        text=self.slack.messages[-1]['text']
        self.assertIn('/#runs/'+n['task_id']+'/'+n['node_id'],text)
        for private in ('Private customer','enterprise-two','Wes','rvn_','Reply','Sign off'):
            self.assertNotIn(private,text)
        self.assertIsNone(self.slack.messages[-1]['blocks'])
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM brief_links').fetchone()[0],0)

    def test_direct_thread_reply_has_no_write_or_model_call(self):
        n=self.node(self.task());self.delivery.deliver_now();message=self.slack.messages[-1]
        before=self.store.get_decision(n['node_id'])
        with patch('bridge.slack_chat.reading',side_effect=AssertionError('Must not interpret')):
            self.assertEqual(self.reply(message,'UWES','Approved. Ship everything.'),'')
        after=self.store.get_decision(n['node_id'])
        self.assertEqual((after['updated_at'],after['answer']),(before['updated_at'],before['answer']))
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM reply_readings').fetchone()[0],0)

    def test_dm_capture_and_thread_ingress_are_ignored(self):
        for text,thread in [('Approved',''),('record: secret decision',''),('yes','123.456')]:
            event={'type':'event_callback','team_id':'TTEST','event_id':'evt-'+text,'event':{'type':'message','channel':'DUWES','user':'UWES','text':text,'ts':'123.789','thread_ts':thread}}
            with patch('bridge.slack_chat.reading',side_effect=AssertionError('Must not interpret')):
                result=handle_slack_event(self.delivery,event)
            self.assertEqual(result['ignored'],'notification_only')
        self.assertEqual(len(self.slack.messages),0)

    def test_old_rich_queued_payload_is_sanitized_when_mode_changes(self):
        self.graph.set_setting('slack_interaction_mode','conversation')
        self.node(self.task(), question='Secret legacy context')
        self.graph.set_setting('slack_interaction_mode','')
        self.assertEqual(self.delivery.deliver_now(),1)
        self.assertNotIn('Secret legacy',self.slack.messages[-1]['text'])
        self.assertIn('/#runs/',self.slack.messages[-1]['text'])
        row=self.delivery.list()[0]
        self.assertEqual(json.loads(self.graph.db.execute('SELECT payload FROM notifications WHERE id=?',(row['id'],)).fetchone()[0])['interaction_mode'],'notification')

    def test_missing_public_url_records_failure_without_losing_decision(self):
        self.delivery.base_url=''
        n=self.node(self.task())
        self.assertTrue(self.store.get_decision(n['node_id']))
        row=self.delivery.list()[0]
        self.assertEqual(row['state'],'failed')
        self.assertIn('HTTPS',row['last_error'])
        self.delivery.base_url='https://raven.example'
        self.delivery.retry(row['id'])
        self.assertEqual(self.delivery.deliver_now(),1)
        self.assertIn('https://raven.example/#runs/',self.slack.messages[-1]['text'])
