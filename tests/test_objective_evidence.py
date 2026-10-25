"""Evidence cards show pinned provenance and genuine quotes, not invented excerpts."""
import json
from fixtures import OfflineCase
from bridge import objectives as obj, objective_head as head, canvas
from bridge.auth import Identity
from bridge.store import Invalid


class ObjectiveEvidenceTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store('objective-evidence')
        self.repo = 'sample/project'
        self.actor = Identity('', 'Local operator', 'admin', 'operator')
        self.store.add_owner({'name':'Owner', 'team':'Policy', 'patterns':'*'})
        self.payload = {'request_key':'create', 'title':'Pilot', 'outcome':'Prepare CSV',
            'success_criteria':'Report works', 'stop_criteria':'Customer declines',
            'guardrails':'No sends', 'repository':self.repo, 'provider':'claude',
            'why_now':'Reported friction', 'cases':{}, 'judgment_kind':'interpretation_tradeoff'}
        self.source = self.record('Original title', 'CSV export fails for large reports.', 'https://example.org/original')
        self.pin = {k:self.source[k] for k in ('record_id','source_version_id')}
        self.citation = {**self.pin, 'quote':'CSV export fails'}

    def record(self, title, body, url, **extra):
        return self.store.add_record({'repo':self.repo, 'kind':'note', 'ref':'customer-note',
            'title':title, 'body':body, 'url':url, **extra})['source']

    def proposal(self, citations=None, metadata=False):
        return obj.propose(self.store, {**self.payload, 'citations':citations if citations is not None else [self.citation]},
                           [{**self.pin, **({'metadata_only':True} if metadata else {})}])

    def test_overview_api_projection_hydrates_title_exact_quote_and_link(self):
        proposal = self.proposal()
        view = next(p for p in obj.overview(self.store)['proposals'] if p['id'] == proposal['id'])
        source = view['sources'][0]
        self.assertEqual(source['title'], 'Original title')
        self.assertEqual(source['ref'], 'customer-note')
        self.assertEqual(source['url'], 'https://example.org/original')
        self.assertEqual(source['quote'], 'CSV export fails')
        self.assertEqual(source['quotes'], ['CSV export fails'])
        self.assertEqual(source['source_version_id'], self.pin['source_version_id'])
        self.assertNotIn('body', source)
        saved = self.store.graph.db.execute('SELECT sources,payload FROM objective_proposals WHERE id=?', (proposal['id'],)).fetchone()
        self.assertEqual(json.loads(saved['sources']), [self.pin])
        self.assertEqual(json.loads(saved['payload'])['citations'], [self.citation])

    def test_no_invented_or_wrong_version_quote(self):
        view = self.proposal([{**self.citation,'quote':'Unsupported interpretation'},
                              {**self.citation,'source_version_id':'other-version'}])
        self.assertNotIn('quote', view['sources'][0])
        self.assertNotIn('quotes', view['sources'][0])
        self.assertNotIn('excerpt', view['sources'][0])

    def test_metadata_never_exposes_body_even_with_matching_candidate_quote(self):
        view = self.proposal(metadata=True)
        source = view['sources'][0]
        self.assertEqual(view['citations'], [])
        self.assertTrue(source['metadata_only'])
        self.assertEqual(source['title'], 'Original title')
        for key in ('quote','quotes','body','text','excerpt'):
            self.assertNotIn(key, source)

    def test_pinned_historical_title_url_quote_survive_head_revision(self):
        proposal = self.proposal()
        self.record('Revised title', 'New facts only.', 'https://example.org/revised')
        view = next(p for p in obj.overview(self.store)['proposals'] if p['id'] == proposal['id'])
        source = view['sources'][0]
        self.assertEqual(source['title'], 'Original title')
        self.assertEqual(source['url'], 'https://example.org/original')
        self.assertEqual(source['quote'], 'CSV export fails')
        self.assertTrue(view['evidence_stale'])
        self.assertFalse(source['current'])

    def test_revocation_and_historical_withholding_remove_copied_quotes(self):
        proposal = self.proposal()
        self.record('Current title', 'Current safe body.', 'https://example.org/current')
        self.store.graph.db.execute('INSERT INTO withheld_source_versions VALUES(?,?,?)',
            (self.pin['source_version_id'], 'withdrawn historical source', '2026-10-25T00:00:00Z'))
        view = obj.overview(self.store)['proposals'][0]
        self.assertEqual(view['id'], proposal['id'])
        self.assertTrue(view['withheld'])
        self.assertNotIn('CSV export fails', json.dumps(view))
        self.record('Current title', 'Current safe body.', 'https://example.org/current', availability='inaccessible')
        with self.assertRaises(Invalid):
            obj._evidence_view(self.store, [self.pin], self.repo, [self.citation])

    def test_manual_objective_has_provenance_without_fabricated_quote(self):
        objective = obj.create(self.store, {**self.payload, 'source_ids':[self.pin['record_id']]}, self.actor)
        self.assertEqual(objective['sources'][0]['title'], 'Original title')
        self.assertNotIn('quote', objective['sources'][0])

    def test_accepted_objective_uses_only_valid_exact_proposal_quotes(self):
        proposal = self.proposal()
        review = obj.proposal_action(self.store, proposal['id'], 'accept',
            {'request_key':'review','expected_revision':1}, self.actor)
        decision = self.store.get_decision(review['decision_id'])
        canvas.sign_off(self.store, review['decision_id'], {'by':'Owner','expected_updated_at':decision['updated_at']})
        head.activate_reviews(self.store)
        objective = obj.overview(self.store)['objectives'][0]
        self.assertEqual(objective['sources'][0]['quote'], 'CSV export fails')
