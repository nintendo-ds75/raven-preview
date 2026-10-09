"""An ordinary owner's complete correction of an unsigned assumed answer.

The shipped source renderer and submit handler run with small DOM doubles,
then call the real local /api/brief/answer endpoint. This is not a browser or
provider test. Assertions read a separate database connection after HTTP has
returned, rather than treating the endpoint's notice as proof of a commit.
"""
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from fixtures import ROOT, ready_server
from test_agent_source_web import HTMLNodes
from test_brief import BriefCase, Http
from bridge import context_memory as cm
from bridge.store import answer_hash


ASSUMED = 'Bill all enterprise-two traffic, including retries and internal load tests.'
CORRECTION = (
    'For enterprise-two during the October rollout only, bill a completed customer response '
    'only if the account has converted to a paid plan.\n'
    'Do not bill retries, failed responses, cached responses, or internal load tests; '
    'partner sandbox traffic is excluded unless the partner explicitly opted into paid usage.\n'
    'Keep the raw usage records for fourteen days, not thirty, because the signed pilot '
    'terms limit retention and synthetic traffic is not a completed customer response.\n'
    'This correction applies to this task only. It does not authorize a reusable billing rule.'
)
RATIONALE = (
    'The signed pilot terms apply only to enterprise-two in this rollout. '
    'The exemption must not erase real paid customer usage or extend to later releases.'
)


# Execute the actual renderer, HTTP client, and answer-form submit callback.
# Only DOM access and post-submit presentation are doubled. In particular,
# neither the acknowledgment guard nor the API payload is reimplemented here.
SUBMIT = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync('web/brief.js', 'utf8');
const elements = {
  '#answer-text': {value: input.answer},
  '#answer-why': {value: input.rationale, hidden: false},
  '#brief-source-confirm': {checked: input.acknowledge},
};
let submit;
const requests = [], responses = [];
const result = {error: '', notice: '', loaded: false};
const context = {
  data: input.page, token: input.token,
  $: selector => elements[selector] || null,
  document: {addEventListener(name, callback) {
    assert.equal(name, 'submit'); submit = callback;
  }},
  async fetch(path, options) {
    assert.equal(path, '/api/brief/answer');
    assert.equal(options.method, 'POST');
    requests.push(JSON.parse(options.body));
    const response = await fetch(input.base + path, options);
    responses.push({status: response.status, body: await response.clone().json()});
    return response;
  },
  clearDrafts() {},
  toast(message) {result.notice = message;},
  async load() {result.loaded = true;},
  async refused(error) {result.error = error.message;},
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('const esc ='), source.indexOf('const initials =')), context);
vm.runInContext(source.slice(source.indexOf('async function call('), source.indexOf('function toast(')), context);
vm.runInContext(source.slice(source.indexOf('function sourceReview('), source.indexOf('function sourceAcknowledgmentKey(')), context);
vm.runInContext(source.slice(source.indexOf("document.addEventListener('submit'"), source.indexOf('// Only a real change of layout')), context);
(async () => {
  const html = context.sourceReview(input.page.focus);
  await submit({preventDefault() {}, target: {id: 'answer-form',
    dataset: {id: input.page.focus.node_id, rev: input.page.focus.updated_at}}});
  console.log(JSON.stringify({...result, html, requests, responses}));
})().catch(error => {console.error(error); process.exitCode = 1;});
"""


@unittest.skipUnless(shutil.which('node'), 'Node is required for the shipped brief submit handler')
class AssumedOwnerCorrectionTests(BriefCase, Http):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.graph.close)
        for method in ('complete', 'complete_json'):
            guard = patch('bridge.llm.Client.' + method,
                          side_effect=AssertionError('Provider calls forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        self.task_id = self.task()
        self.did = self.node(self.task_id)['node_id']
        self.source_data = [
            {'repo': 'acme/platform', 'kind': 'jira', 'provider': 'jira',
             'namespace': 'billing.example', 'external_id': 'BILL-201', 'ref': 'BILL-201',
             'title': 'October paid pilot terms', 'author': 'Pilot Policy Writer', 'status': 'Done',
             'body': 'Earlier pilot terms assumed every response was billable.',
             'paths': ['billing/usage.py'], 'url': 'https://example.invalid/BILL-201',
             'source_version': 'pilot-v1', 'updated_at': '2026-10-01T12:00:00Z'},
            {'repo': 'acme/platform', 'kind': 'jira', 'provider': 'jira',
             'namespace': 'billing.example', 'external_id': 'BILL-202', 'ref': 'BILL-202',
             'title': 'Usage classification and retention', 'author': 'Usage Policy Writer', 'status': 'Done',
             'body': 'Earlier usage guidance included synthetic traffic and thirty-day retention.',
             'paths': ['billing/usage.py', 'billing/retention.py'],
             'url': 'https://example.invalid/BILL-202', 'source_version': 'usage-v1',
             'updated_at': '2026-10-01T13:00:00Z'},
        ]
        originals = [self.store.add_record(data) for data in self.source_data]
        rows = [dict(self.graph.db.execute(
            'SELECT i.*,s.id AS record_id,s.head_id AS source_version_id '
            'FROM intents i JOIN source_records s ON s.intent_id=i.id WHERE s.id=?',
            (item['source']['record_id'],)).fetchone()) for item in originals]
        self.graph.publish_evidence(self.did, rows, status='assumed', source='assumption',
                                    answer=ASSUMED, kind='assumption', signoff='required')
        self.original_history = cm.history(self.graph.db, self.did)
        self.original_versions = [dict(self.graph.db.execute(
            'SELECT * FROM source_versions WHERE id=?',
            (item['source']['source_version_id'],)).fetchone()) for item in originals]
        # Review must bind both complete current bodies, not the stale premise
        # already attached to the assumed answer or a preview of either body.
        self.current_data = [
            {**self.source_data[0], 'source_version': 'pilot-v2',
             'body': 'Paid usage requires conversion and a completed customer response.\n'
                     + 'These terms apply only to enterprise-two in the October rollout.\n' * 16
                     + 'END PILOT: partner sandboxes require an explicit paid-usage opt-in.'},
            {**self.source_data[1], 'source_version': 'usage-v2',
             'body': 'Exclude retries, failures, cached responses, and internal load tests.\n'
                     + 'Retain raw usage records for fourteen days under the pilot terms.\n' * 16
                     + 'END USAGE: do not retain raw records for thirty days.'},
        ]
        self.current_sources = [self.store.add_record(data)['source'] for data in self.current_data]
        self.token = self.mint(self.wes, self.task_id, self.did)
        self.serve(ready_server(self.store, port=0))
        row = self.decision(self.did)
        self.assertEqual((row['status'], row['signoff'], row['answer']), ('assumed', 'required', ASSUMED))
        self.assertFalse(row['authorized'])
        self.assertEqual(json.loads(row['signatures']), [])
        self.assertFalse(row['signed_by'])
        self.assertFalse(row['reusable'])

    def review(self):
        status, page, _ = self.brief(self.token)
        self.assertEqual(status, 200, page)
        focus = page['focus']
        self.assertEqual(focus['node_id'], self.did)
        self.assertTrue(focus['can_act'])
        self.assertEqual(focus['action'], 'sign')
        self.assertEqual(focus['updated_at'], self.decision(self.did)['updated_at'])
        self.assertTrue(focus['source_revalidation']['available'])
        self.assertTrue(focus['source_revalidation']['has_reliance'])
        self.assertEqual(focus['source_revalidation']['decision_pins'], [])
        return page

    def submit(self, page, *, acknowledge=True, answer=CORRECTION, rationale=RATIONALE):
        result = subprocess.run([shutil.which('node'), '-e', SUBMIT], cwd=ROOT,
            input=json.dumps({'page': page, 'base': f'http://127.0.0.1:{self.port}',
                'token': self.token, 'answer': answer, 'rationale': rationale,
                'acknowledge': acknowledge}), text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        observed = json.loads(result.stdout)
        parsed = HTMLNodes(observed['html'])
        blocks = parsed.with_class('source-snapshot')
        sources = page['focus']['source_revalidation']['sources']
        self.assertEqual(len(blocks), 2)
        for source in sources:
            block = next(block for block in blocks if source['record_id'] in block['text'])
            for text in (source['record_id'], source['source_version_id'], source['role'],
                         source['snapshot']['body']):
                self.assertIn(text, block['text'])
            metadata = {key: value for key, value in source['snapshot'].items() if key != 'body'}
            self.assertIn(json.dumps(metadata, indent=2, ensure_ascii=False), block['text'])
        checkbox = next(node for node in parsed.nodes if node['attrs'].get('id') == 'brief-source-confirm')
        self.assertIn('required', checkbox['attrs'])
        self.assertIn('Bind my answer to these exact revisions.', observed['html'])
        if observed['requests']:
            self.assertEqual(observed['requests'], [{
                'decision_id': self.did, 'expected_updated_at': page['focus']['updated_at'],
                'source_evidence': page['focus']['source_revalidation']['pins'],
                'source_decision_pins': page['focus']['source_revalidation']['decision_pins'],
                'answer': answer, 'rationale': rationale}])
        return observed

    def persisted(self):
        # A separate connection cannot see an uncommitted outer HTTP write.
        with self.store.connect() as db:
            return {
                'decision': dict(db.execute('SELECT * FROM decisions WHERE id=?', (self.did,)).fetchone()),
                'events': [dict(row) for row in db.execute(
                    'SELECT * FROM events WHERE decision_id=? ORDER BY id', (self.did,))],
                'history': cm.history(db, self.did),
                'sources': [dict(row) for row in db.execute(
                    'SELECT * FROM decision_source_edges WHERE decision_id=? ORDER BY id', (self.did,))],
            }

    def test_no_source_acknowledgment_refuses_without_submitting_or_signing(self):
        page = self.review()
        before = self.persisted()
        result = self.submit(page, acknowledge=False)
        self.assertIn('Review the complete current evidence before signing', result['error'])
        self.assertEqual(result['requests'], [])
        self.assertEqual(result['responses'], [])
        self.assertFalse(result['loaded'])
        self.assertEqual(self.persisted(), before)

    def test_complete_unicode_boundary_preserves_late_condition_and_reason(self):
        suffix = '\nKeep café counts 🧪. Never authorize production or future reuse.'
        prefix = 'For this task only, retain this complete review record: '
        answer = prefix + 'x' * (12000 - len((prefix + suffix).encode('utf-16-le')) // 2) + suffix
        reason_tail = '\nThis preserves exact owner acceptance evidence 🧪.'
        rationale = 'r' * (4000 - len(reason_tail.encode('utf-16-le')) // 2) + reason_tail
        self.assertEqual(len(answer.encode('utf-16-le')) // 2, 12000)
        self.assertEqual(len(rationale.encode('utf-16-le')) // 2, 4000)
        result = self.submit(self.review(), answer=answer, rationale=rationale)
        self.assertEqual(result['responses'][0]['status'], 200, result['error'])
        committed = self.persisted()['decision']
        self.assertEqual(committed['answer'], answer)
        self.assertEqual(committed['rationale'], rationale)
        self.assertTrue(self.decision(self.did)['authorized'])
        self.assertFalse(committed['reusable'])

    def test_oversize_answer_and_reason_refuse_whole_without_signing_a_prefix(self):
        for field, value in (('answer', 'x' * 12000 + ' Never ship.'),
                             ('rationale', 'r' * 4000 + ' Scope is limited.'),
                             ('answer', '🧪' * 6001), ('rationale', '🧪' * 2001)):
            with self.subTest(field=field, unicode=value.startswith('🧪')):
                before = self.persisted()
                args = {'answer': CORRECTION, 'rationale': RATIONALE, field: value}
                result = self.submit(self.review(), **args)
                self.assertEqual(result['responses'][0]['status'], 400)
                self.assertIn(field + ' must be at most', result['error'])
                self.assertEqual(self.persisted(), before)

    def test_nontext_answer_or_reason_is_not_stringified_and_signed(self):
        page = self.review()
        for field in ('answer', 'rationale'):
            for value in (['Do not ship'], {'policy': 'Hold'}, True, 17):
                with self.subTest(field=field, value_type=type(value).__name__):
                    before = self.persisted()
                    payload = {'decision_id': self.did, 'expected_updated_at': page['focus']['updated_at'],
                        'source_evidence': page['focus']['source_revalidation']['pins'],
                        'source_decision_pins': page['focus']['source_revalidation']['decision_pins'],
                        'answer': CORRECTION, 'rationale': RATIONALE, field: value}
                    status, body, _ = self.brief(self.token, '/api/brief/answer', payload)
                    self.assertEqual(status, 400)
                    self.assertIn(field + ' must be text', body['error'])
                    self.assertEqual(self.persisted(), before)

    def test_changed_source_version_rejects_old_pins_even_with_current_decision_revision(self):
        page = self.review()
        changed = self.store.add_record({**self.current_data[1], 'source_version': 'usage-v3',
                                         'access_scope': 'October pilot owners only'})
        self.assertNotEqual(changed['source']['source_version_id'],
                            self.current_sources[1]['source_version_id'])
        snapshot = json.loads(self.graph.db.execute('SELECT snapshot FROM source_versions WHERE id=?',
            (changed['source']['source_version_id'],)).fetchone()['snapshot'])
        self.assertEqual(snapshot['body'], self.current_data[1]['body'])
        # Isolate the source pin check from the separate decision revision gate.
        page['focus']['updated_at'] = self.decision(self.did)['updated_at']
        before = self.persisted()
        result = self.submit(page)
        self.assertEqual(result['responses'][0]['status'], 400, result)
        self.assertRegex(result['error'], 'changed|current|version')
        self.assertEqual(self.persisted(), before)

    def test_changed_decision_revision_rejects_acknowledged_correction(self):
        page = self.review()
        self.graph.update_decision(self.did, rationale='The rollout constraint changed after the owner read it.')
        self.assertNotEqual(self.decision(self.did)['updated_at'], page['focus']['updated_at'])
        before = self.persisted()
        result = self.submit(page)
        self.assertEqual(result['responses'][0]['status'], 400, result)
        self.assertIn('changed while you were reviewing', result['error'])
        self.assertEqual(self.persisted(), before)

    def test_incomplete_current_source_pins_cannot_drop_a_reviewed_premise(self):
        page = self.review()
        page['focus']['source_revalidation']['pins'] = page['focus']['source_revalidation']['pins'][:1]
        before = self.persisted()
        result = self.submit(page)
        self.assertEqual(result['responses'][0]['status'], 400, result)
        self.assertIn('retire a supporting or contradictory premise', result['error'])
        self.assertEqual(self.persisted(), before)

    def test_complete_owner_correction_commits_exact_text_once_and_preserves_history(self):
        page = self.review()
        expected_pins = [cm.pin(source) for source in self.current_sources]
        self.assertCountEqual(page['focus']['source_revalidation']['pins'], expected_pins)
        result = self.submit(page)
        self.assertEqual(result['responses'][0]['status'], 200, result)
        self.assertIn('Corrected and signed by Wes Chen', result['notice'])
        self.assertEqual(result['error'], '')
        self.assertTrue(result['loaded'])

        committed = self.persisted()
        row = committed['decision']
        self.assertEqual((row['status'], row['signoff'], row['answer'], row['rationale']),
                         ('approved', 'signed', CORRECTION, RATIONALE))
        self.assertEqual((row['answered_by'], row['signed_by'], row['actor_id'], row['actor_basis']),
                         ('Wes Chen', 'Wes Chen', self.wes, 'owner'))
        self.assertEqual(row['source'], 'human')
        self.assertEqual(row['signed_hash'], answer_hash(CORRECTION))
        signatures = json.loads(row['signatures'])
        self.assertEqual(len(signatures), 1)
        self.assertEqual((signatures[0]['by'], signatures[0]['hash'], signatures[0]['revision']),
                         ('Wes Chen', answer_hash(CORRECTION), row['updated_at']))
        self.assertEqual(row['signed_revision'], row['updated_at'])
        self.assertFalse(row['reusable'])
        self.assertFalse(row['needs_review'])
        self.assertFalse(any(event['kind'] == 'rule_made' for event in committed['events']))
        self.assertTrue(self.decision(self.did)['authorized'])
        self.assertCountEqual([cm.pin(source, source['role']) for source in committed['sources'] if source['active']],
                              expected_pins)

        previous = [json.loads(event['detail']) for event in committed['events'] if event['kind'] == 'previous_answer']
        self.assertEqual([event['answer'] for event in previous], [ASSUMED])
        approval = [json.loads(event['detail']) for event in committed['events'] if event['kind'] == 'owner_approved']
        self.assertEqual(len(approval), 1)
        self.assertEqual((approval[0]['answer'], approval[0]['rationale']), (CORRECTION, RATIONALE))
        self.assertEqual(committed['history'][:len(self.original_history)], self.original_history)
        historical = [json.loads(version['snapshot']) for version in self.original_history]
        self.assertTrue(any(version['decision']['status'] == 'assumed'
                            and version['decision']['answer'] == ASSUMED for version in historical))
        latest = json.loads(committed['history'][-1]['snapshot'])
        self.assertEqual(latest['decision']['answer'], CORRECTION)
        self.assertCountEqual([cm.pin(source, source['role']) for source in latest['sources']], expected_pins)
        with self.store.connect() as db:
            for old in self.original_versions:
                self.assertEqual(dict(db.execute('SELECT * FROM source_versions WHERE id=?',
                                                 (old['id'],)).fetchone()), old)

        # The original reviewed revision is a one-shot correction, not a
        # reusable authorization or a way to accumulate duplicate signatures.
        replay = self.submit(page)
        self.assertEqual(replay['responses'][0]['status'], 400, replay)
        self.assertIn('changed while you were reviewing', replay['error'])
        self.assertEqual(self.persisted(), committed)


if __name__ == '__main__':
    unittest.main()
