"""Citation repair never bypasses exact evidence, support, or access checks."""
import copy
import json
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import inquiry, source_access
from bridge.auth import Identity
from bridge.config import Config
from bridge.llm import Client, LLMError
from bridge.store import Invalid, Store


class InquiryCitationTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.cfg = Config()
        self.evidence = [{'id': 'policy', 'text': 'Current answer: Internal test traffic is excluded.\n'
            'Signed by: Mira Security\nSignoff: signed\nScope: production', 'kind': 'task_decision'}]
        self.good = {'outcome': 'supported_answer', 'answer': 'Mira signed the production exclusion.',
            'citations': [{'id': 'policy', 'quote': 'Internal test traffic is excluded.'},
                          {'id': 'policy', 'quote': 'Signed by: Mira Security'},
                          {'id': 'policy', 'quote': 'Scope: production'}], 'open': []}
        self.bad = copy.deepcopy(self.good)
        self.bad['citations'][1]['quote'] = 'Signed by: Mira Security; Scope: production'
        self.support = {'claims': [{'claim': self.good['answer'], 'support': 'stated'}]}
        self.enterContext(patch.object(Config, 'has_backend', return_value=True))
        self.enterContext(patch.dict('os.environ', {'BRIDGE_SEMANTIC': '1'}))

    def read(self):
        return inquiry.read_evidence('Who signed the exclusion, and for which scope?', self.evidence, self.cfg)

    def test_good_citations_do_not_add_a_generation_call(self):
        with patch.object(Client, 'complete_json', return_value=self.good) as complete, \
                patch('bridge.llm.check_support', return_value=self.support):
            result = self.read()
        self.assertTrue(result['model_used'])
        self.assertFalse(result['authorized'])
        complete.assert_called_once()
        self.assertNotIn('citation_anchors', json.loads(complete.call_args.args[2]))
        self.assertIn('one contiguous', complete.call_args.args[1])

    def test_original_and_correction_prompts_keep_answers_focused_on_requested_facts(self):
        with patch.object(Client, 'complete_json', side_effect=[self.bad, self.good]) as complete, \
                patch('bridge.llm.check_support', return_value=self.support):
            self.assertTrue(self.read()['model_used'])
        self.assertEqual(complete.call_count, 2)
        for call in complete.call_args_list:
            system = ' '.join(call.args[1].split())
            self.assertIn('focused on facts needed to answer the current question', system)
            self.assertIn('evidence IDs and source references only in citations, not in the answer prose', system)
            self.assertIn('Do not add sentences explaining which evidence item contains the answer', system)
            self.assertIn('Include implementation details or decision history only when the question asks for them', system)
            self.assertIn('Do not infer implementation from a recorded policy', system)
            self.assertIn('Use the recorded author, signer and rationale when asked who decided and why', system)

    def test_supported_policy_and_exact_citations_do_not_excuse_unsupported_extra_claims(self):
        for extra in ('The production answer is provided in evidence id policy.',
                      'The production implementation has been verified.',
                      'The owner previously approved this policy for all environments.'):
            raw = {**self.good, 'answer': self.good['answer'] + ' ' + extra}
            checked = {'claims': [*self.support['claims'], {'claim': extra, 'support': 'inferred'}]}
            with self.subTest(extra=extra), patch.object(Client, 'complete_json', return_value=raw) as complete, \
                    patch('bridge.llm.check_support', return_value=checked) as support:
                result = self.read()
            complete.assert_called_once()
            support.assert_called_once_with(self.cfg, 'Who signed the exclusion, and for which scope?',
                                            json.dumps(self.evidence), raw['answer'])
            self.assertFalse(result['model_used'])
            self.assertEqual(result['outcome'], 'missing_fact')
            self.assertEqual(result['citations'], [])
            self.assertNotIn(extra, result['answer'])

    def test_joined_passages_get_one_bounded_copy_only_correction(self):
        self.bad['answer'] = 'Rejected draft must not become evidence.'
        with patch.object(Client, 'complete_json', side_effect=[self.bad, self.good]) as complete, \
                patch('bridge.llm.check_support', return_value=self.support) as support:
            result = self.read()
        self.assertTrue(result['model_used'])
        self.assertEqual(result['citations'], self.good['citations'])
        self.assertEqual(complete.call_count, 2)
        for call in complete.call_args_list:
            self.assertEqual(call.args[0], 'inquiry')
            self.assertEqual(call.kwargs, {'max_tokens': 1600, 'bounded': True})
        retry = json.loads(complete.call_args.args[2])
        self.assertEqual(retry['evidence'], self.evidence)
        self.assertEqual(retry['citation_repair']['invalid_citation_indexes'], [1])
        self.assertIn({'id': 'policy', 'quote': 'Signed by: Mira Security'}, retry['citation_anchors'])
        self.assertIn({'id': 'policy', 'quote': 'Scope: production'}, retry['citation_anchors'])
        self.assertNotIn(self.bad['answer'], json.dumps(retry))
        support.assert_called_once_with(self.cfg, 'Who signed the exclusion, and for which scope?',
                                        json.dumps(self.evidence), self.good['answer'])

    def test_paraphrases_hallucinated_sources_and_fabrication_still_fail_closed(self):
        for citation in ({'id': 'policy', 'quote': 'Internal testing traffic is excluded.'},
                         {'id': 'invented-source', 'quote': 'Internal test traffic is excluded.'},
                         {'id': 'policy', 'quote': 'All real customers receive free service.'},
                         {'id': 'policy', 'quote': 'Signed by: Mira Security; Scope: production'},
                         {'id': ['policy'], 'quote': 'Scope: production'}):
            with self.subTest(citation=citation):
                bad = {**self.bad, 'citations': [citation]}
                with patch.object(Client, 'complete_json', return_value=bad) as complete, \
                        patch('bridge.llm.check_support') as support:
                    result = self.read()
                self.assertEqual(complete.call_count, 2)
                support.assert_not_called()
                self.assertFalse(result['model_used'])
                self.assertEqual(result['outcome'], 'missing_fact')
                self.assertEqual(result['citations'], [])
                self.assertEqual(result['evidence'], self.evidence)
                self.assertNotIn(bad['answer'], result['answer'])

    def test_exact_correction_still_requires_independent_claim_support(self):
        for checked in (None, {'claims': []}, {'claims': [{'claim': 'Claim', 'support': 'inferred'}]}):
            with self.subTest(checked=checked), \
                    patch.object(Client, 'complete_json', side_effect=[self.bad, self.good]) as complete, \
                    patch('bridge.llm.check_support', return_value=checked) as support:
                result = self.read()
            self.assertEqual(complete.call_count, 2)
            support.assert_called_once()
            self.assertFalse(result['model_used'])
            self.assertEqual(result['citations'], [])

    def test_failed_or_malformed_correction_retains_only_fallback(self):
        for correction in (LLMError('bounded provider failure'), {}, {**self.good, 'citations': []}):
            with self.subTest(correction=correction), \
                    patch.object(Client, 'complete_json', side_effect=[self.bad, correction]) as complete, \
                    patch('bridge.llm.check_support') as support:
                result = self.read()
            self.assertEqual(complete.call_count, 2)
            support.assert_not_called()
            self.assertFalse(result['model_used'])
            self.assertEqual(result['evidence'], self.evidence)
            self.assertTrue(result['open'])

    def test_initial_provider_failure_or_malformed_schema_does_not_retry(self):
        for response in (LLMError('failure'), {}, {**self.good, 'open': 'wrong type'}):
            with self.subTest(response=response), \
                    patch.object(Client, 'complete_json', side_effect=[response]) as complete, \
                    patch('bridge.llm.check_support') as support:
                self.assertFalse(self.read()['model_used'])
            complete.assert_called_once()
            support.assert_not_called()

    def test_offline_reading_keeps_original_fallback_without_generation(self):
        with patch.object(Config, 'has_backend', return_value=False), \
                patch.object(Client, 'complete_json') as complete:
            result = self.read()
        complete.assert_not_called()
        self.assertFalse(result['model_used'])
        self.assertEqual(result['evidence'], self.evidence)

    def test_anchors_are_bounded_verbatim_and_never_rewrite_the_packet(self):
        evidence = [{'id': 'long', 'text': ('  ' + 'word ' * 150 + '\n') * 100}]
        original = copy.deepcopy(evidence)
        anchors = inquiry._citation_anchors(evidence)
        self.assertLessEqual(len(anchors), 64)
        self.assertLessEqual(sum(len(c['quote']) for c in anchors), 12000)
        self.assertTrue(all(c['quote'] in evidence[0]['text'] for c in anchors))
        self.assertEqual(evidence, original)


class InquiryCitationRaceTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'inquiry.db')
        self.addCleanup(self.store.graph.close)
        self.actor = Identity('reader', 'Reader', 'member', 'session')
        self.repo = 'fixture/access'
        self.task = self.store.add_run({'title': 'Read the billing policy', 'repo': self.repo})['id']
        self.node = self.store.graph.add_decision(self.task, 'Which billing policy applies?', '', 'pending', repo=self.repo)
        self.store.graph.db.execute("UPDATE decisions SET answer='Internal test traffic is excluded.' WHERE id=?", (self.node,))
        self.good = {'outcome': 'supported_answer', 'answer': 'Internal test traffic is excluded.',
            'citations': [{'id': self.node, 'quote': 'Internal test traffic is excluded.'}], 'open': []}
        self.bad = {**self.good, 'citations': [{'id': self.node, 'quote': 'Internal testing is excluded.'}]}
        self.enterContext(patch.object(Config, 'has_backend', return_value=True))
        self.enterContext(patch.dict('os.environ', {'BRIDGE_SEMANTIC': '1'}))
        self.enterContext(patch('bridge.llm.check_support', return_value={
            'claims': [{'claim': self.good['answer'], 'support': 'stated'}]}))

    def read(self, **kwargs):
        return inquiry.ask_task(self.store, self.task, {'question': 'Which billing policy applies?'},
                                self.actor, cfg=Config(), **kwargs)

    def correction_with(self, action):
        yield self.bad
        action()
        yield self.good

    def test_changed_evidence_during_correction_is_withheld(self):
        def change():
            self.store.graph.db.execute("UPDATE decisions SET answer='Changed concurrently' WHERE id=?", (self.node,))
        with patch.object(Client, 'complete_json', side_effect=self.correction_with(change)) as complete:
            with self.assertRaisesRegex(Invalid, 'evidence changed'):
                self.read()
        self.assertEqual(complete.call_count, 2)

    def test_reader_acl_is_revalidated_after_correction(self):
        revoked = []
        def revalidate():
            if revoked:
                raise Invalid('Reader access revoked')
        with patch.object(Client, 'complete_json', side_effect=self.correction_with(lambda: revoked.append(True))):
            with self.assertRaisesRegex(Invalid, 'revoked'):
                self.read(revalidate=revalidate)

    def test_native_source_revocation_during_correction_is_withheld(self):
        self.store.graph.db.execute('INSERT INTO source_connections(id,kind,repo,target,options,created_at) VALUES(?,?,?,?,?,?)',
            ('connection', 'jira', self.repo, 'BILL', '{"host":"fixture.invalid"}', source_access.stamp()))
        source = self.store.add_record({'repo': self.repo, 'provider': 'jira', 'namespace': 'fixture.invalid',
            'kind': 'jira', 'external_id': '10001', 'ref': 'BILL-1', 'title': 'Billing policy',
            'body': self.good['answer'], 'access_scope': '{"jira_project":"BILL","audience":"workspace-shared"}'})['source']
        source_access.observe(self.store, 'connection', source['record_id'])
        row = dict(self.store.graph.db.execute('SELECT i.*,s.id AS record_id,s.head_id AS source_version_id FROM intents i '
            'JOIN source_records s ON s.intent_id=i.id WHERE s.id=?', (source['record_id'],)).fetchone())
        self.store.graph.publish_evidence(self.node, [row], status='resolved', source='record',
            answer=self.good['answer'], kind='evidence', signoff='required')
        def revoke():
            source_access.observe(self.store, 'connection', source['record_id'], state='revoked')
        with patch.object(Client, 'complete_json', side_effect=self.correction_with(revoke)) as complete:
            with self.assertRaisesRegex(Invalid, 'withheld'):
                self.read()
        self.assertEqual(complete.call_count, 2)

    def test_successful_correction_is_read_only_and_creates_no_authority(self):
        tables = ('runs', 'decisions', 'host_sessions')
        before = [self.store.graph.db.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in tables]
        decision = self.store.get_decision(self.node)
        with patch.object(Client, 'complete_json', side_effect=[self.bad, self.good]):
            result = self.read()
        self.assertTrue(result['model_used'])
        self.assertFalse(result['authorized'])
        self.assertEqual(self.store.get_decision(self.node), decision)
        self.assertEqual(before, [self.store.graph.db.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in tables])
