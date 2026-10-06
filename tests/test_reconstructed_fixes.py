"""Behavior reconstructed from the interrupted review, tested without inference."""
from review_source_fixtures import source_conformance

import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
from test_delivery import DeliveryCase
from bridge import canvas, llm
from bridge.auth import Auth
from bridge.bootstrap import dev_login
from bridge.config import Config
from bridge.store import Store, Invalid


class BackgroundTests(DeliveryCase):
    def test_node_returns_before_model_and_publishes_only_one_complete_question(self):
        task = self.task()
        entered, release = threading.Event(), threading.Event()
        def brief(*args, **kwargs):
            entered.set()
            release.wait(5)
            return 'Should the load test be excluded?'
        with patch.dict(os.environ, BRIDGE_SEMANTIC='1'), patch('bridge.llm.compose_brief', brief):
            started = time.monotonic()
            node = canvas.add_node(self.store, Config(model_api='none'), {
                'task_id':task, 'paths':'billing/usage.py', 'question':'Should billing exclude the load test?',
                'client_ref':'async'})
            self.assertLess(time.monotonic()-started, 1)
            self.assertTrue(node['model_pending'])
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.delivery.deliver_now(), 0)
            tree = canvas.get_tree(self.store, task)
            self.assertEqual(len(tree['nodes']), 1)
            self.assertTrue(tree['nodes'][0]['blocking'])
            release.set()
            self.assertTrue(canvas.wait_for_background(10))
        final = canvas.node_view(self.store, node['node_id'])
        self.assertFalse(final['model_pending'])
        self.assertEqual(final['brief'], 'Should the load test be excluded?')
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM decisions WHERE draft=1').fetchone()[0], 0)
        self.assertEqual(self.delivery.deliver_now(), 1)

    def test_a_persons_answer_wins_over_an_inflight_model(self):
        task = self.task()
        entered, release = threading.Event(), threading.Event()
        def brief(*args, **kwargs):
            entered.set(); release.wait(5)
            return 'The old question'
        with patch.dict(os.environ, BRIDGE_SEMANTIC='1'), patch('bridge.llm.compose_brief', brief):
            node = canvas.add_node(self.store, Config(model_api='none'), {
                'task_id':task, 'paths':'billing/usage.py', 'question':'Should billing exclude the load test?'})
            self.assertTrue(entered.wait(3))
            row=self.store.get_decision(node['node_id'])
            self.store.answer(node['node_id'], {'answer':'Exclude the load test', 'rationale':'It is ours',
                              'signed_by':'Wes Chen', 'expected_updated_at':row['updated_at']})
            self.delivery.deliver_now()
            sent=len(self.slack.messages)
            release.set(); self.assertTrue(canvas.wait_for_background(10))
        row=self.store.get_decision(node['node_id'])
        self.assertEqual(row['answer'], 'Exclude the load test')
        self.assertEqual(row['status'], 'approved')
        self.delivery.deliver_now()
        self.assertEqual(len(self.slack.messages), sent)

    def test_kickoff_returns_then_raises_its_verdict_and_resumes_by_id(self):
        entered, release=threading.Event(), threading.Event()
        def triage(*args):
            entered.set(); release.wait(5)
            return {'verdict':'engage','why':'A customer policy decision'}
        with patch.dict(os.environ, BRIDGE_SEMANTIC='1'), patch('bridge.llm.name_decisions', return_value=[]), patch('bridge.llm.model_triage', triage):
            start=time.monotonic()
            task=canvas.start_task(self.store,Config(model_api='none'),{'title':'Adjust a helper','repo':'acme/platform','paths':'billing/usage.py'})
            self.assertLess(time.monotonic()-start,1)
            self.assertTrue(task['model_pending'])
            self.assertTrue(entered.wait(3))
            self.assertFalse(canvas.finish_task(self.store, {'task_id':task['task_id']})['finished'])
            release.set()
            self.assertTrue(canvas.wait_for_background(10))
        resumed=canvas.start_task(self.store,Config(model_api='none'),{'task_id':task['task_id']})
        self.assertEqual(resumed['task_id'],task['task_id'])
        self.assertEqual(resumed['verdict'],'engage')
        self.assertFalse(resumed['model_pending'])

    def test_new_kickoff_gets_its_own_read_while_the_previous_one_is_in_flight(self):
        entered, release = threading.Event(), threading.Event()
        count = []
        def triage(*args):
            count.append(True)
            if len(count) == 1:
                entered.set(); release.wait(5)
                return {'verdict':'engage', 'why':'Old reading'}
            return {'verdict':'engage', 'why':'Current reading'}
        with patch.dict(os.environ, BRIDGE_SEMANTIC='1'), patch('bridge.llm.name_decisions', return_value=[]), patch('bridge.llm.model_triage', triage):
            cfg = Config(model_api='none')
            task = canvas.start_task(self.store, cfg, {'title':'Adjust billing helper','repo':'acme/platform','paths':'billing/usage.py'})
            self.assertTrue(entered.wait(3))
            canvas.kickoff(self.store, cfg, task['task_id'], 'Adjust billing helper', '', 'acme/platform', ['billing/usage.py'], '')
            release.set()
            self.assertTrue(canvas.wait_for_background(10))
        result = canvas.start_task(self.store, cfg, {'task_id':task['task_id']})
        self.assertEqual(len(count), 2)
        self.assertIn('Current reading', result['why'])
        self.assertFalse(result['model_pending'])

    def test_abandon_withdraws_notifications_and_late_replies(self):
        task=self.task();node=self.node(task)
        self.delivery.deliver_now();message=self.slack.messages[0]
        result=canvas.finish_task(self.store,{'task_id':task,'status':'abandoned','reason':'Duplicate kickoff'})
        self.assertEqual(result['status'],'abandoned')
        self.assertEqual(self.store.get_decision(node['node_id'])['status'],'withdrawn')
        self.assertIn('withdrawn',self.reply(message,'UWES','answer: Bill it because approved'))
        with self.assertRaises(Invalid):self.node(task)

    def test_a_task_a_person_answered_cannot_be_abandoned(self):
        task=self.task();node=self.node(task);self.delivery.deliver_now()
        self.reply(self.slack.messages[0],'UWES','answer: Exclude it because it was internal')
        with self.assertRaises(Invalid):
            canvas.finish_task(self.store,{'task_id':task,'status':'abandoned','reason':'Oops'})

    def test_interrupted_model_read_is_visible_and_the_contact_is_not_silenced(self):
        task=self.task();node=self.node(task)
        self.delivery.deliver_now()
        self.graph.db.execute("UPDATE decisions SET model_pending=1, updated_at='2020-01-01T00:00:00+00:00' WHERE id=?",(node['node_id'],))
        canvas.get_tree(self.store,task)
        self.assertFalse(self.store.get_decision(node['node_id'])['model_pending'])
        self.assertEqual(self.graph.count_events('model_read_interrupted',decision_id=node['node_id']),1)

    def test_reused_answer_prefers_its_signer_unless_current_authority_overrides(self):
        from bridge.routing import rank_for_decision
        task = self.task(); node = self.node(task)
        self.delivery.deliver_now()
        self.reply(self.slack.messages[0], 'UWES', 'answer: Exclude it because it is internal')
        run = self.graph.get_task(task)
        with patch('bridge.routing.rank_for_decision', return_value=[('Marisol Vega',['authored recent code'],1.0)]):
            ranked = canvas._route_signer(self.graph, run, node['question'], '', ['billing/usage.py'], '', None, '', {}, node['node_id'])
        self.assertEqual(ranked[0][0], 'Wes Chen')
        with patch('bridge.routing.rank_for_decision', return_value=[('Marisol Vega',['verified: current authority'],2.0)]):
            ranked = canvas._route_signer(self.graph, run, node['question'], '', ['billing/usage.py'], '', None, '', {}, node['node_id'])
        self.assertEqual(ranked[0][0], 'Marisol Vega')

    def test_possessive_slack_mention_resolves_to_contact(self):
        from bridge.slack_chat import recipient
        self.assertEqual(recipient(self.delivery, "<@UMAR>'s call", speaker=self.wes)['id'], self.marisol)
        self.assertEqual(recipient(self.delivery, 'someone else', "It is <@UMAR|Marisol>’s call", self.wes)['id'], self.marisol)

    def test_wait_honors_the_mcp_call_budget(self):
        from bridge.mcp import call_tool
        task = self.task(); self.node(task)
        with patch.dict(os.environ, BRIDGE_MCP_CALL_BUDGET='1'):
            started = time.monotonic()
            result = call_tool(self.store, 'bridge_wait', {'task_id':task,'timeout':'300'})
            self.assertLess(time.monotonic()-started, 2.5)
        self.assertTrue(result['timed_out'])

    def test_without_inference_slack_gives_working_commands(self):
        self.node(self.task());self.delivery.deliver_now()
        text=self.slack.messages[0]['text']
        self.assertIn('`answer:',text)
        self.assertNotIn('in your own words',text)


class ExtraConditionTests(OfflineCase):
    def check(self, condition):
        def complete(client,purpose,*args,**kwargs):
            if purpose=='conformance':return {'status': 'complete', 'requirements':[{'needs':'Exempt enterprise','kind':'must','found':'honored','at':'if enterprise: return 0'}]}
            if purpose=='counterexample':return {'status': 'complete', 'checks':[]}
            return {'status': 'complete', 'conditions':[condition]} if condition else {'status': 'complete', 'conditions':[]}
        diff='+if enterprise: return 0\n+if pro and override: return 0\n+return overage * 2\n'
        with patch.dict(os.environ,BRIDGE_SEMANTIC='1'),patch.object(llm.Client,'complete_json',complete):
            return source_conformance(Config(model_api='none'),'Who is exempt?','Only enterprise is exempt',diff)

    def test_located_unsigned_exemption_departs(self):
        read=self.check({'condition':'Pro accounts with an override are also exempt','at':'if pro and override: return 0',
            'allegation': {'kind': 'behavioral', 'authorized': 'Only enterprise is exempt',
                'input': 'A non-enterprise Pro account has an override and positive overage.',
                'sequence': ['Call the billing function.', 'Take the Pro override branch.'],
                'expected': 'Charge the non-enterprise overage.', 'observed': 'The function returns zero.'}})
        self.assertEqual(read['verdict'],'departs')
        self.assertIn('no signed answer',read['why'])

    def test_unlocated_exemption_is_unclear(self):
        read=self.check({'condition':'Other plans exempted','at':'if other_plan: return 0'})
        self.assertEqual(read['verdict'],'unclear')
        self.assertTrue(read['unexamined'])

    def test_no_extra_condition_preserves_supported_reading(self):
        self.assertEqual(self.check(None)['verdict'],'follows')


class DeveloperLoginTests(OfflineCase):
    def test_workspace_name_does_not_mean_someone_has_claimed_it(self):
        store=Store(Path(self.temp.name)/'dev.db');self.addCleanup(store.graph.close)
        person=store.graph.add_person('Local developer',role='admin')
        token=Auth(store,enabled=True).create_token(person,'development',kind='human')['token']
        path=Path(self.temp.name)/'token';path.write_text(token)
        store.graph.set_setting('workspace_name','Acme')
        with patch.dict(os.environ,BRIDGE_LOGIN_FILE=str(path)):
            self.assertIn(token,dev_login(store))
            store.graph.db.execute('INSERT INTO account_passwords(person_id,password_hash) VALUES (?,?)',(person,'test-hash'))
            self.assertNotIn(token,dev_login(store))

if __name__=='__main__': unittest.main()
