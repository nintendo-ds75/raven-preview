/* An objective-scope decision (Review and accept on Outcomes) stores its
 * proposed scope as JSON in the node's context. Measured with customers:
 * the inbox card and the decision dialog showed that JSON, up to 12,000
 * characters of it. The shipped decisionCard and decisionBackground show
 * a card instead: the question, what the move will and won't do, the
 * bound, the evidence and a link to the outcome; an ordinary decision renders
 * exactly as before. Exercises the shipped functions with DOM doubles. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function harness() {
  const element = () => ({dataset: {}, innerHTML: '', textContent: '', hidden: false, children: [],
    addEventListener() {}, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [],
    classList: {add() {}, remove() {}, toggle() {}}});
  const context = {console, URLSearchParams, location: {hash: '#inbox', pathname: '/', search: ''}, history: {replaceState() {}},
    document: {querySelector: element, querySelectorAll: () => [], getElementById: element, body: element(), activeElement: null,
      addEventListener() {}, createElement: element, documentElement: {lang: 'en'}},
    taskId: () => '', taskTab: '', taskDetail: null, taskError: '',
    setTimeout: () => 1, clearTimeout() {}, setInterval() {}, fetch: () => new Promise(() => {}), addEventListener() {}};
  context.window = context;
  vm.createContext(context);
  for (const file of ['presentation.js', 'app.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, '../web', file), 'utf8'), context);
  return code => vm.runInContext(code, context);
}

const gov = {title: 'Draft the renewal note for Acme', outcome: 'Acme renews before the 30th with the usage tier they asked for.',
  success_criteria: 'A note the account owner can send.', stop_criteria: 'Any pricing change or an unanswered legal question.',
  guardrails: 'Private preparation only; no sending, no pricing changes.', repository: 'acme/platform', provider: 'claude',
  why_now: 'The renewal is due in nine days.', judgment_kind: 'interpretation_tradeoff',
  cases: {positive: '- Draft the note\n- List the usage tier they asked for', negative: 'Send anything to Acme',
    boundary: 'A tier change needs a new judgment', non_implications: 'Does not mean the price <is> agreed'},
  citations: [{record_id: 'r1', source_version_id: 'v1', quote: 'We want to renew by the 30th at the usage tier.'}],
  research_digest: 'a'.repeat(64), source_dependencies_digest: 'b'.repeat(64)};
const base = {id: 'd1', run_id: 't1', agent: 'Raven objective head', repo: 'acme/platform', run_title: 'Draft the renewal note',
  owner_name: 'Wes Chen', status: 'pending', created_at: '2026-10-20T10:00:00Z', path: '', events: [], options: '[]'};
const objective = {...base, question: 'Should Raven take point on: Draft the renewal note for Acme?', category: 'objective_scope',
  client_ref: 'objective-acceptance:p1', context: JSON.stringify(gov) + '\nOptions: Yes, take point | Not now | Change the scope'};
const ordinary = {...base, id: 'd2', question: 'Bill the usage spike?', category: 'policy', client_ref: 'n1',
  context: 'Usage jumped 11x on enterprise-two during the rollout. {"not": "json to parse"}'};

const run = harness();
run('state.decisions = []; state.owners = []; state.me = {id: "p1", name: "Wes Chen", role: "member"};');
run('decision = ' + JSON.stringify(objective) + '; plain = ' + JSON.stringify(ordinary));
const card = run('decisionCard(decision, 0)');
assert.match(card, /<h2>Should Raven take point on: Draft the renewal note for Acme\?<\/h2>/);
assert.match(card, /<section class="objective-scope" aria-label="Proposed scope">/);
assert.match(card, /<strong>Outcome:<\/strong> Acme renews before the 30th with the usage tier they asked for\./);
assert.match(card, /<strong>Should Raven take point on Draft the renewal note for Acme\?<\/strong> <span class="muted">Yes, take point · Not now · Change the scope<\/span>/);
assert.match(card, /<h3>Will<\/h3><ul><li>Draft the note<\/li><li>List the usage tier they asked for<\/li><\/ul>/);
assert.match(card, /<h3>Won’t<\/h3><ul><li>Send anything to Acme<\/li><\/ul>/);
assert.match(card, /<h3>Edge<\/h3><ul><li>A tier change needs a new judgment<\/li><\/ul>/);
assert.match(card, /<h3>Does not imply<\/h3><ul><li>Does not mean the price &lt;is&gt; agreed<\/li><\/ul>/, 'case text is escaped');
assert.match(card, /<strong>Within:<\/strong> Private preparation only; no sending, no pricing changes\./);
assert.match(card, /<strong>Stop when:<\/strong> Any pricing change or an unanswered legal question\./);
assert.match(card, /<details class="objective-scope-evidence" id="scope-d1-evidence" open><summary>Evidence \(1\)<\/summary><ul><li><blockquote>We want to renew by the 30th at the usage tier\.<\/blockquote><\/li><\/ul><\/details>/);
assert.match(card, /<a class="objective-scope-link" href="#today\/p1">Open the outcome<\/a>/);
assert.doesNotMatch(card, /[{}]/, 'no JSON on the card');
assert.doesNotMatch(card, /research_digest|source_version_id/, 'digests and ids are not for a person');
assert.match(card, /data-action="review" data-id="d1"/, 'the review control is unchanged');
assert.doesNotMatch(card, /<p class="context">/, 'the raw context paragraph is replaced');

// A long quote folds; a short one is open.
const longCard = run('decisionCard({...decision, context: JSON.stringify({...' + JSON.stringify(gov) + ', citations: [{quote: "x".repeat(300)}]})}, 0)');
assert.match(longCard, /<details class="objective-scope-evidence" id="scope-d1-evidence" ><summary>Evidence \(1\)/);

// The decision dialog's background section shows the same card where the JSON was.
const background = run('decisionBackground(decision)');
assert.match(background, /<section class="objective-scope" aria-label="Proposed scope">/);
assert.match(background, /id="background-d1-evidence"/);
assert.doesNotMatch(background, /<p class="modal-context">/);
assert.doesNotMatch(background, /research_digest/);

// An ordinary decision renders exactly as before, braces in its context included.
const plain = run('decisionCard(plain, 0)');
assert.match(plain, /<p class="context">Usage jumped 11x on enterprise-two during the rollout\. \{&quot;not&quot;: &quot;json to parse&quot;\}<\/p>/);
assert.doesNotMatch(plain, /objective-scope/);
assert.match(run('decisionBackground(plain)'), /<p class="modal-context">Usage jumped 11x/);

// A marked decision whose context is not JSON, or JSON of the wrong shape, falls back to the context.
assert.match(run('decisionCard({...decision, context: "The proposal text was lost."}, 0)'), /<p class="context">The proposal text was lost\.<\/p>/);
assert.match(run('decisionCard({...decision, context: "[1, 2]"}, 0)'), /<p class="context">\[1, 2\]<\/p>/);
assert.match(run('decisionCard({...decision, context: "{\\"outcome\\": "}, 0)'), /<p class="context">/);
// Missing fields are left out, never invented.
const bare = run('decisionCard({...decision, context: "{}"}, 0)');
assert.match(bare, /<strong>Should Raven take point on this\?<\/strong>/);
assert.doesNotMatch(bare, /Outcome:|Within:|Stop when:|<h3>|Evidence/);
assert.match(bare, /href="#today\/p1"/, 'the proposal id in the client_ref still links to the outcome');
console.log('Objective decision card: question, cases, bound, evidence and outcome link without JSON; ordinary decisions unchanged.');
