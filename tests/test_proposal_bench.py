"""Proposal bench on the labelled synthetic corpus in tests/fixtures/proposals.

The model is a script, so this proves the plumbing between a model's
output and what a person reads, never model quality: a citation must be
the record's own words, a stored proposal carries the four cases and its
card two to four options, a repository has at most one open proposal, a
dismissed proposal is not pitched again unchanged, and the gate's
precision and recall over the labels when the script pitches on every
record. Standard library only, no key, no network."""
import json
import time

from fixtures import OfflineCase, ROOT
from bridge import canvas, objective_head as head, objective_text, objectives as obj
from bridge.auth import Identity
from bridge.store import Invalid

FIXTURES = ROOT / 'tests' / 'fixtures' / 'proposals'
CORPUS = json.loads((FIXTURES / 'corpus.json').read_text())
LABELS = json.loads((FIXTURES / 'labels.json').read_text())['records']
REPO = CORPUS['repository']
CASES = ('positive', 'negative', 'boundary', 'non_implications')
# The states objectives.propose treats as an open proposal for the repository.
OPEN = ('proposed', 'review', 'preparing', 'review_error', 'reconcile')


def load_corpus(store):
    """Every corpus record in the store, keyed by fixture id, with the
    record id and version the store assigned and the body as written."""
    loaded = {}
    for record in CORPUS['records']:
        data = {k: v for k, v in record.items() if k != 'id'} | {'repo': REPO}
        source = store.add_record(data)['source']
        loaded[record['id']] = {'record_id': source['record_id'], 'source_version_id': source['source_version_id'],
                                'body': record['body']}
    return loaded


def pin_of(loaded, fixture_id):
    return {k: loaded[fixture_id][k] for k in ('record_id', 'source_version_id')}


def proposal_for(title, pin, quote):
    """One bounded move in the shape objective_head validates, citing one quote."""
    return {'title': title, 'outcome': 'Prepare ' + title[0].lower() + title[1:] + ' privately for review',
            'success_criteria': 'A person can review the prepared change against the cited evidence',
            'stop_criteria': 'Stop if the evidence is withdrawn or a person declines',
            'guardrails': 'Private preparation only; nothing is sent, published or deployed',
            'why_now': 'The cited record asks for it', 'judgment_kind': 'interpretation_tradeoff',
            'cases': {'positive': 'Do: ' + title, 'negative': 'Do not change anything the evidence does not name',
                      'boundary': 'Ask first when the change would alter billing, access or retention',
                      'non_implications': 'No external communication is authorised by this move'},
            'citations': [{**pin, 'quote': quote}], 'repository': REPO, 'provider': 'claude'}


class ScriptedModel:
    """The model as a script. Research reads the one target record (the
    scan seeds it with the three newest records, which may not be the
    target) and finishes with the scripted disposition; the head returns
    the scripted proposal. Whatever else the prompt carries is ignored."""

    def __init__(self, target, *, proposal=None, disposition='needs_judgment', fact_quote=''):
        self.target, self.proposal, self.disposition, self.fact_quote = target, proposal, disposition, fact_quote
        self.calls = {'research': 0, 'head': 0}

    def complete_json(self, purpose, system, prompt, **kwargs):
        if purpose == 'objective factual research':
            self.calls['research'] += 1
            history = json.loads(prompt).get('history', [])
            read = {entry['result'].get('record_id') for entry in history
                    if entry.get('action') in ('read', 'initial_read') and isinstance(entry.get('result'), dict)}
            if self.target['record_id'] not in read:
                return {'action': 'read', 'record_id': self.target['record_id']}
            observations = []
            if self.disposition == 'factual_answer':
                observations = [{'statement': 'The record states it.', 'kind': 'fact',
                                 'citations': [{**self.target, 'quote': self.fact_quote}]}]
            return {'action': 'finish', 'disposition': self.disposition, 'observations': observations, 'reason': 'Scripted conclusion'}
        self.calls['head'] += 1
        return {'proposal': self.proposal, 'reason': 'Scripted pitch'}


class ProposalBenchCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store('proposal-bench')
        self.addCleanup(self.store.graph.close)
        self.actor = Identity('', 'Local operator', 'admin', 'operator')
        self.loaded = load_corpus(self.store)

    def scan(self, model, key):
        # A distinct checkpoint makes each scan a fresh evaluation instead of
        # the cached one; the administrator's override keeps the bench out of
        # the per-repository request budget, which test_objective_budget covers.
        return head.scan(self.store, {'repository': REPO, 'request_key': key, 'checkpoint': key, 'override': True},
                         self.actor, client=model)

    def rows(self, states=None):
        query = 'SELECT id,state FROM objective_proposals WHERE repository=?'
        return [dict(r) for r in self.store.graph.db.execute(query, (REPO,)) if states is None or r['state'] in states]

    def dismiss(self, proposal, reason):
        return obj.proposal_action(self.store, proposal['id'], 'dismiss',
            {'request_key': 'dismiss-' + proposal['id'], 'expected_revision': proposal['revision'], 'rationale': reason}, self.actor)

    def current_body(self, record_id):
        current = self.store.get_record(record_id, REPO)
        version = next(v for v in current['versions'] if v['id'] == current['source']['source_version_id'])
        return version['id'], version['snapshot']['body']


class ProposalShapeTests(ProposalBenchCase):
    def test_each_positive_stores_a_cited_four_case_proposal_with_one_open_at_a_time(self):
        self.store.add_owner({'name': 'Policy Owner', 'team': 'Policy', 'paths': '*', 'repo': REPO})
        positives = [(fid, label) for fid, label in LABELS.items() if label['worth_surfacing']]
        self.assertGreaterEqual(len(positives), 6)
        for index, (fid, label) in enumerate(positives):
            pin = pin_of(self.loaded, fid)
            model = ScriptedModel(pin, proposal=proposal_for(label['title'], pin, label['quote']))
            result = self.scan(model, 'positive-' + fid)
            self.assertEqual(result['status'], 'proposed', fid)
            stored = result['proposal']
            self.assertEqual((stored['title'], stored['state']), (label['title'], 'proposed'))
            self.assertEqual(set(stored['cases']), set(CASES), fid)
            self.assertTrue(all(stored['cases'][case].strip() for case in CASES), fid)
            self.assertEqual([c['quote'] for c in stored['citations']], [label['quote']])
            for citation in stored['citations']:
                version, body = self.current_body(citation['record_id'])
                self.assertEqual(citation['source_version_id'], version)
                self.assertIn(citation['quote'], body)
            self.assertEqual([r['id'] for r in self.rows(OPEN)], [stored['id']])
            if index + 1 < len(positives):
                # Another pitch while this one is open comes back as this one.
                other_fid, other = positives[index + 1]
                other_pin = pin_of(self.loaded, other_fid)
                crowded = self.scan(ScriptedModel(other_pin, proposal=proposal_for(other['title'], other_pin, other['quote'])), 'crowded-' + fid)
                self.assertEqual(crowded['proposal']['id'], stored['id'])
                self.assertEqual(len(self.rows(OPEN)), 1)
            review = obj.proposal_action(self.store, stored['id'], 'accept',
                {'request_key': 'accept-' + fid, 'expected_revision': stored['revision']}, self.actor)
            self.assertEqual(review['state'], 'review')
            node = canvas.node_view(self.store, review['decision_id'])
            self.assertFalse(node['authorized'])
            decision = self.store.get_decision(review['decision_id'])
            gov = objective_text.parse(decision['context'], decision.get('category', ''), decision.get('client_ref', ''))
            self.assertEqual(gov['cases'], stored['cases'])
            card = objective_text.card(gov)
            options = node['options'] or card['options']
            self.assertTrue(2 <= len(options) <= 4, options)
            self.assertTrue(all(card['cases'][key] for key, _label in objective_text.CASES), card['cases'])
            self.assertEqual([e['quote'] for e in card['evidence']], [label['quote']])
            self.assertEqual(len(self.rows(OPEN)), 1)
            canvas.sign_off(self.store, review['decision_id'], {'by': 'Policy Owner', 'expected_updated_at': decision['updated_at']})
            head.activate_reviews(self.store)
            self.assertEqual(self.rows(OPEN), [])
            self.assertEqual(self.store.graph.db.execute('SELECT state FROM objective_proposals WHERE id=?', (stored['id'],)).fetchone()['state'], 'accepted')
        self.assertEqual(len(obj.overview(self.store)['objectives']), len(positives))

    def test_an_invented_quote_is_refused_and_nothing_is_stored(self):
        pin = pin_of(self.loaded, 'r16')
        pitch = LABELS['r16']['pitch']
        self.assertNotIn(pitch['quote'], self.loaded['r16']['body'])
        model = ScriptedModel(pin, proposal=proposal_for(pitch['title'], pin, pitch['quote']))
        with self.assertRaisesRegex(Invalid, 'did not read'):
            self.scan(model, 'invented')
        self.assertEqual(model.calls['head'], 1)
        self.assertEqual(self.rows(), [])
        self.assertEqual(obj.overview(self.store)['proposals'], [])

    def test_a_dismissed_proposal_is_not_pitched_again_until_the_evidence_changes(self):
        pin = pin_of(self.loaded, 'r03')
        label = LABELS['r03']
        model = ScriptedModel(pin, proposal=proposal_for(label['title'], pin, label['quote']))
        first = self.scan(model, 'first')['proposal']
        self.assertEqual(first['state'], 'proposed')
        dismissed = self.dismiss(first, 'Already planned for the next release; nothing to prepare now')
        self.assertEqual(dismissed['state'], 'dismissed')
        again = self.scan(model, 'again')
        self.assertEqual(len(self.rows()), 1)
        if again.get('proposal') is not None:
            self.assertEqual((again['proposal']['id'], again['proposal']['state']), (first['id'], 'dismissed'))
        self.assertEqual(self.rows(OPEN), [])
        self.store.add_record({'repo': REPO, 'kind': 'email', 'ref': 'paste:orrin-webhooks-2', 'provider': 'generic',
            'namespace': 'raven:paste', 'title': 'Orrin Labs: webhook retries, again',
            'body': 'Following up: we lost another Sunday of events. Is a longer retry window possible?'})
        after = self.scan(model, 'after-new-evidence')
        self.assertEqual(self.store.graph.db.execute('SELECT state FROM objective_proposals WHERE id=?', (first['id'],)).fetchone()['state'], 'dismissed')
        if after.get('proposal') is not None:
            self.assertNotEqual(after['proposal']['id'], first['id'])
            self.assertEqual(after['proposal']['state'], 'proposed')
            self.assertEqual([c['quote'] for c in after['proposal']['citations']], [label['quote']])
        self.assertLessEqual(len(self.rows(OPEN)), 1)


class ProposalGateBenchTests(ProposalBenchCase):
    def test_precision_and_recall_of_the_gate_when_the_script_pitches_on_every_record(self):
        """The script proposes for every record, labelled or not. Only the
        disposition path (research says no move, or the records already
        answer it) and the citation check (the quote must be in the record)
        stand between a pitch and a stored proposal; the numbers measure
        that gate against the labels, not a model."""
        started = time.monotonic()
        surfaced, counts = set(), {'tp': 0, 'fp': 0, 'fn': 0, 'tn': 0}
        for fid, label in LABELS.items():
            pin = pin_of(self.loaded, fid)
            pitch = label.get('pitch', {})
            title = pitch.get('title') or label.get('title') or 'Act on ' + fid
            quote = pitch.get('quote') or label.get('quote') or self.loaded[fid]['body'][:40]
            model = ScriptedModel(pin, proposal=proposal_for(title, pin, quote),
                                  disposition=pitch.get('disposition', 'needs_judgment'), fact_quote=quote)
            try:
                result = self.scan(model, 'bench-' + fid)
            except Invalid:
                result = {'proposal': None}
            stored = result.get('proposal')
            if stored is not None and stored['state'] == 'proposed':
                surfaced.add(fid)
                self.assertIn(quote, self.current_body(pin['record_id'])[1])
                self.assertEqual(set(stored['cases']), set(CASES))
                # Clear the way for the next record: one open proposal per repository.
                self.dismiss(stored, 'Bench: judged, moving to the next record')
            self.assertLessEqual(len(self.rows(OPEN)), 0)
            expected = label['worth_surfacing']
            counts['tp' if expected and fid in surfaced else 'fn' if expected else 'fp' if fid in surfaced else 'tn'] += 1
        precision = counts['tp'] / max(1, counts['tp'] + counts['fp'])
        recall = counts['tp'] / max(1, counts['tp'] + counts['fn'])
        elapsed = time.monotonic() - started
        print(f"\nproposal bench: precision={precision:.2f} recall={recall:.2f} "
              f"tp={counts['tp']} fp={counts['fp']} fn={counts['fn']} tn={counts['tn']} "
              f"records={len(LABELS)} seconds={elapsed:.1f}")
        # The labels say which pitches the gate passes: every positive with a
        # verbatim quote, and the negatives whose pitch is verbatim and asks
        # for judgment (the gate cannot tell a support request from a move).
        expected = {fid for fid, label in LABELS.items()
                    if label.get('pitch', {}).get('disposition', 'needs_judgment') == 'needs_judgment'
                    and (label.get('pitch', {}).get('quote') or label.get('quote')) in self.loaded[fid]['body']}
        self.assertEqual(surfaced, expected)
        self.assertGreaterEqual(precision, 0.7)
        self.assertGreaterEqual(recall, 0.6)
        self.assertLess(elapsed, 20)
