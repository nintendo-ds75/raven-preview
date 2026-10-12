"""Reasoning nominates evidence and questions; it never expands authority."""
import json
import threading
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import canvas, investigation, semantic_index as si
from bridge.config import Config
from bridge.store import Store, Invalid


class LocalEncoder:
    def passage_embed(self, texts):
        return ([1., 0.] if 'carry forward' in text else [0., 1.] for text in texts)

    def query_embed(self, text):
        return iter([[1., 0.]])


class ReasoningTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'reasoning.db')
        self.g = self.store.graph
        self.task = canvas.start_task(self.store, Config(model_api='none'), {
            'title': 'Handle prepaid balances', 'goal': 'Preserve prepaid value at renewal.', 'repo': 'example/service'})['task_id']
        self.addCleanup(self.g.close)

    def test_learned_search_finds_nonlexical_source_and_does_not_restore_withdrawn_content(self):
        self.store.add_record({'repo': 'example/service', 'kind': 'doc', 'ref': 'A',
                               'title': 'Contract', 'body': 'Unspent amounts carry forward at renewal.', 'resolved': True})
        self.store.add_record({'repo': 'example/service', 'kind': 'doc', 'ref': 'B',
                               'title': 'Deployment', 'body': 'Restart servers on Tuesday.', 'resolved': True})
        with patch.object(si, 'enabled', return_value=True), patch.object(si, '_model', return_value=('test-learned', LocalEncoder())):
            found, status = si.sources(self.g, 'unused credit rollover', 'example/service')
            self.assertEqual(found[0]['ref'], 'A')
            self.assertEqual(status['backend'], 'local')
            self.assertTrue(self.g.db.execute('SELECT count(*) AS n FROM semantic_vectors').fetchone()['n'])
            self.g.db.execute("UPDATE source_records SET availability='inaccessible' WHERE intent_id=?", (found[0]['id'],))
            self.g._bump('example/service')
            again, _ = si.sources(self.g, 'unused credit rollover', 'example/service')
            self.assertNotIn('A', [r['ref'] for r in again])
            other, _ = si.sources(self.g, 'unused credit rollover', 'other/service')
            self.assertEqual(other, [])

    def test_model_failure_is_visible_and_preserves_lexical_results(self):
        self.store.add_record({'repo': 'example/service', 'kind': 'doc', 'ref': 'A', 'title': 'Retry delay budget',
                               'body': 'Retry delay budget is set by callers.'})
        with patch.object(si, 'enabled', return_value=True), patch.object(si, '_model', side_effect=ImportError):
            found, status = si.sources(self.g, 'retry delay budget', 'example/service')
        self.assertEqual(found[0]['ref'], 'A')
        self.assertEqual(status['backend'], 'unavailable')

    def test_long_documents_are_chunked_and_updated_content_is_reembedded(self):
        with patch.object(si, 'enabled', return_value=True), patch.object(si, '_model', return_value=('test-learned', LocalEncoder())):
            matches, status = si.rank(self.g, 'rollover', [('a', 'irrelevant ' * 500 + 'carry forward'), ('b', 'restart')])
            self.assertEqual(matches[0][0], 'a')
            self.assertGreater(status['chunks'], 2)
            new, _ = si.rank(self.g, 'rollover', [('a', 'restart'), ('b', 'carry forward')])
            self.assertEqual(new[0][0], 'b')

    def review(self, **extra):
        return investigation.review(self.store, {'task_id': self.task,
            'observations': 'billing/credits.py at revision abc keeps a balance; renewal currently resets it.',
            'paths': ['billing/credits.py'], **extra}, Config(model_api='none'))

    def test_review_challenges_then_records_disposition_without_creating_authority(self):
        reading = {'summary': 'One policy question remains.', 'findings': [
            {'kind': 'judgment', 'question': 'Should unused prepaid value survive renewal?',
             'why': 'Resetting it changes the customer contract.', 'basis': ['host'], 'paths': ['billing/credits.py']}]}
        with patch.object(Config, 'semantic_retrieval', property(lambda _: True)), patch('bridge.investigation.Client.complete_json', return_value=reading) as model:
            result = self.review()
            again = self.review()
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result, again)
        self.assertEqual(self.g.db.execute('SELECT count(*) n FROM decisions').fetchone()['n'], 0)
        self.assertEqual(len(result['unaddressed']), 1)
        addressed = self.review(review_id=result['review_id'], dispositions=[{
            'finding_id': result['unaddressed'][0], 'reason': 'The task only changes reporting; renewal behavior stays unchanged.'}])
        self.assertFalse(addressed['unaddressed'])
        self.assertEqual(self.g.count_events('discovery_addressed', self.task), 1)
        self.assertTrue(canvas.get_tree(self.store, self.task)['discovery_reviews'])
        exported = self.store.state(full_history=True)['discovery_reviews'][0]
        self.assertIn('billing/credits.py', json.loads(exported['input'])['evidence']['host'])
        self.assertEqual(json.loads(exported['dispositions'])[result['unaddressed'][0]]['reason'],
                         'The task only changes reporting; renewal behavior stays unchanged.')

    def test_fabricated_basis_is_inconclusive_and_cross_task_adoption_is_refused(self):
        reading = {'findings': [{'kind': 'judgment', 'question': 'Invented?', 'why': 'Unknown policy', 'basis': ['made-up']}]}
        with patch.object(Config, 'semantic_retrieval', property(lambda _: True)), patch('bridge.investigation.Client.complete_json', return_value=reading):
            bad = self.review()
        self.assertEqual(bad['status'], 'unavailable')
        self.assertFalse(bad['findings'])
        other = self.store.add_run({'title': 'Other task', 'repo': 'example/service'})['id']
        with self.assertRaises(Invalid):
            investigation.address(self.store, other, {'review_id': bad['review_id'], 'dispositions': []})

    def test_finish_rechecks_changed_diff_and_signed_decision_versions(self):
        self.g.db.execute("UPDATE runs SET verdict='engage',discovery=? WHERE id=?", (json.dumps({"investigation_required": True}), self.task))
        with patch.object(Config, 'semantic_retrieval', property(lambda _: True)), patch('bridge.investigation.Client.complete_json', return_value={'findings': []}):
            self.assertFalse(investigation.finish_guard(self.store, self.task, 'patch')['finished'])
            self.review(phase='finish', diff='patch')
            self.assertIsNone(investigation.finish_guard(self.store, self.task, 'patch'))
            self.assertFalse(investigation.finish_guard(self.store, self.task, 'new patch')['finished'])
            self.g.add_decision(self.task, 'A newly discovered choice?', '', 'pending', repo='example/service')
            self.assertFalse(investigation.finish_guard(self.store, self.task, 'patch')['finished'])

    def test_contact_model_selects_cited_candidate_and_cannot_invent_person(self):
        from bridge.contact_reasoning import choose
        self.g.add_person('Ari Example', slack_id='UARI')
        self.g.add_person('Bo Example', slack_id='UBO')
        ranked = [('Ari Example', ['Authored many changes'], 1), ('Bo Example', ['Reviewed renewal policy'], .7)]
        with patch('bridge.routing.rank_for_decision', return_value=ranked), patch.object(Config, 'semantic_retrieval', property(lambda _: True)), patch('bridge.contact_reasoning.Client.complete_json') as model:
            model.return_value = {'candidate': 'c2', 'evidence': ['c2:e1'], 'why': 'Reviewed this policy'}
            selected = choose(self.g, Config(model_api='none'), 'example/service', 'Who should decide rollover?', task_id=self.task)
            self.assertEqual(selected[0][0], 'Bo Example')
            model.return_value = {'candidate': 'invented-person', 'evidence': ['c2:e1']}
            self.assertEqual(choose(self.g, Config(model_api='none'), 'example/service', 'Who decides?', task_id=self.task), ranked)
        self.assertFalse(self.g.authority_rows('example/service'))
        self.assertTrue(json.loads(self.g.db.execute("SELECT detail FROM events WHERE kind='contact_reasoned' LIMIT 1").fetchone()['detail'])['candidates'])

    def test_background_review_returns_pending_deduplicates_and_wakes_waiter(self):
        entered, release = threading.Event(), threading.Event()
        args = {'task_id': self.task, 'observations': 'The current implementation resets prepaid balances.',
                'paths': ['billing/credits.py'], 'phase': 'finish', 'diff': 'patch'}
        self.g.db.execute("UPDATE runs SET verdict='engage',discovery=? WHERE id=?",
                          (json.dumps({'investigation_required': True}), self.task))
        def read(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('The caller did not release the background review')
            return {'summary': 'No additional question in this supplied context.', 'findings': []}
        with patch.object(Config, 'semantic_retrieval', property(lambda _: True)), patch('bridge.investigation.Client.complete_json', side_effect=read) as model:
            try:
                pending = investigation.review(self.store, args, Config(model_api='none'), defer=True)
                self.assertTrue(pending['model_pending'])
                self.assertTrue(entered.wait(2))
                retry = investigation.review(self.store, args, Config(model_api='none'), defer=True)
                self.assertEqual(retry['review_id'], pending['review_id'])
                self.assertEqual(model.call_count, 1)
                self.assertTrue(canvas.get_tree(self.store, self.task)['model_pending'])
                self.assertFalse(investigation.finish_guard(self.store, self.task, 'patch')['finished'])
            finally:
                release.set()
                self.assertTrue(canvas.wait_for_background(5))
            self.assertIsNone(investigation.finish_guard(self.store, self.task, 'patch'))
        result = canvas.wait(self.store, {'task_id': self.task}, cap=.1, interval=.01)
        self.assertFalse(result['discovery_reviews'][0]['model_pending'])
        self.assertEqual(self.g.count_events('discovery_reviewed', self.task), 1)
        from bridge.host_sessions import WAKE_EVENTS
        self.assertIn('discovery_reviewed', WAKE_EVENTS)

    def test_failed_review_can_be_retried_without_changing_the_investigation(self):
        from bridge.llm import LLMError
        with patch.object(Config, 'semantic_retrieval', property(lambda _: True)), patch('bridge.investigation.Client.complete_json', side_effect=[LLMError('unavailable'), {'findings': []}]) as model:
            failed = self.review()
            retried = self.review()
        self.assertEqual(failed['status'], 'unavailable')
        self.assertEqual(retried['status'], 'complete')
        self.assertEqual(failed['review_id'], retried['review_id'])
        self.assertEqual(model.call_count, 2)

    def test_killed_worker_becomes_visible_and_can_retry_from_just_review_id(self):
        with patch.object(Config, 'semantic_retrieval', property(lambda _: True)), patch('bridge.canvas._start_background'):
            pending = investigation.review(self.store, {'task_id': self.task, 'observations': 'Host checked renewal code.'},
                                           Config(model_api='none'), defer=True)
        self.g.db.execute("UPDATE investigation_reviews SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?", (pending['review_id'],))
        tree = canvas.get_tree(self.store, self.task)
        self.assertFalse(tree['model_pending'])
        self.assertEqual(tree['discovery_reviews'][0]['error'], 'Interrupted')
        with patch.object(Config, 'semantic_retrieval', property(lambda _: True)), patch('bridge.investigation.Client.complete_json', return_value={'findings': []}):
            result = investigation.review(self.store, {'task_id': self.task, 'review_id': pending['review_id'], 'retry': True}, Config(model_api='none'))
        self.assertEqual(result['status'], 'complete')

    def test_verified_decider_bypasses_contact_model(self):
        from bridge.contact_reasoning import choose
        with patch('bridge.routing.rank_for_decision', return_value=[('Ari Example', ['verified'], 1)]), patch('bridge.signals._authority_matches', return_value=[{'strong': True, 'role': 'decides'}]), patch('bridge.contact_reasoning.Client.complete_json') as model:
            result = choose(self.g, Config(model_api='none'), 'example/service', 'Who decides?', task_id=self.task)
        self.assertEqual(result[0][0], 'Ari Example')
        model.assert_not_called()

    def test_contact_already_nominated_by_history_receives_retrieved_source_text(self):
        from bridge.contact_reasoning import choose
        self.g.add_person('Mira Example', slack_id='UMIRA')
        self.store.add_record({'repo': 'example/service', 'kind': 'doc', 'ref': 'RUNTIME',
            'title': 'Retry delay policy', 'body': 'A zero limit skips sleep but preserves the retry.',
            'author': 'Mira Example', 'status': 'Approved', 'resolved': True})
        ranked = [('Mira Example', ['authored a record for this path'], .2)]
        with patch('bridge.routing.rank_for_decision', return_value=ranked), patch.object(Config, 'semantic_retrieval', property(lambda _: True)), patch('bridge.contact_reasoning.Client.complete_json', return_value={'candidate': 'c1', 'evidence': ['c1:e3']}) as model:
            result = choose(self.g, Config(model_api='none'), 'example/service', 'What is the retry delay policy?', task_id=self.task)
        payload = json.loads(model.call_args.args[2])
        self.assertIn('A zero limit skips sleep but preserves the retry.', payload['candidates'][0]['evidence'].values())
        self.assertIn('A zero limit skips sleep but preserves the retry.', result[0][1])
