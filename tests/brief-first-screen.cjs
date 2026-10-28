/* The task page's reading order on a phone, from the shipped render
 * functions: the decision and its answer form come before the question
 * box, what Raven checked and the recorded scope table; the agent's context
 * is shown once; both open on request below the form.
 * Measured at 390px before this order: the scope table and the work-item
 * record sat between the question and the brief, and the form began a
 * screen and a half down. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2] || 'web/brief.js', 'utf8');

function page(narrowMatches) {
  const context = {location: {hash: '#synthetic-fixture'}, console,
    addEventListener() {}, setInterval() {}, fetch: () => new Promise(() => {}),
    ResizeObserver: class {observe() {} disconnect() {}},
    document: {addEventListener() {}},
    matchMedia: () => ({matches: narrowMatches, addEventListener() {}}),
    approvalScope: () => '<section class="decision-scope"><h3>Scope of this decision</h3></section>',
    taskDisplayLabel: () => 'Synthetic task'};
  context.window = context;
  vm.createContext(context);
  vm.runInContext(source, context);
  // workItemContext is brief.js's own; stand in for it after the source ran.
  vm.runInContext(`workItemContext = () => '<details class="work-item-context"><summary>Work-item context</summary></details>'`, context);
  context.fixture = {task: {id: 't1', title: 'Synthetic task', repo: 'acme/platform', status: 'active'},
    viewer: {name: 'Lina', role: 'member'}, requester: {name: 'Requester'}, progress: {signed: 0, total: 1},
    nodes: [{node_id: 'd1', raw_status: 'pending', state: 'waiting', blocking: true, owner: 'Lina', signatures: [],
      question: 'Clamp at the cap?', can_act: true, action: 'answer'}], contacts: [], history: [], notes: [],
    focus: {node_id: 'd1', question: 'Clamp at the cap?', status: 'pending', can_act: true, asked: true, updated_at: 'r1',
      brief: 'The agent asks whether the cap is a hard ceiling.', context: 'Near the cap the jitter can exceed it.',
      options: ['Clamp after jitter', 'Jitter after clamp'], paths: ['billing/usage.py'], owner: 'Lina',
      found: [{kind: 'decision', lead: 'An earlier decision', quote: 'Keep jitter off by default.'}],
      approval_scope: {id: 'd1', facts: {}}, approval_scope_text: 'scope', why: {heading: 'Why you', summary: 'You decide billing/.'}}};
  vm.runInContext('data = fixture', context);
  return {html: vm.runInContext('focusCard(data.focus)', context), context};
}

const {html, context} = page(true);
const at = marker => { const i = html.indexOf(marker); assert(i >= 0, `missing ${marker}`); return i; };
const form = at('id="answer-form"');
assert(at('id="focus-question"') < at('class="brief-lead"'), 'the question comes first');
assert(at('class="brief-lead"') < form && at('Context from the coding agent') < form, 'the brief and the context are read before the form');
assert(at('brief-options') < at('id="answer-text"'), 'the options are the first thing in the form');
for (const later of ['What Raven checked', 'decision-scope', 'work-item-context', 'class="brief-where"', 'class="brief-reason"']) {
  assert(at(later) > form, `${later} belongs below the form`);
}
assert.match(html, /<details class="brief-why brief-checked"[^>]*>\s*<summary>What Raven checked<\/summary>/, 'what Raven checked opens on request');
assert.doesNotMatch(html, /brief-checked"[^>]*\sopen/, 'what Raven checked starts closed');
assert.match(html, /<details class="brief-extra brief-scope"[^>]*>\s*<summary>Details<\/summary>/, 'the scope table opens on request');
assert.doesNotMatch(html, /brief-scope"[^>]*\sopen/, 'the scope table starts closed');
assert(at('class="brief-reason"') > at('<summary>Details</summary>'), 'why it came to them is inside Details');
for (const gone of ['Next:', 'Record my decision', 'Prefer to talk it through', 'Recorded scope of this decision', 'not approved'])
  assert(!html.includes(gone), `${gone} is gone`);
assert(html.includes('>Answer and sign<') && html.includes('Signed as Lina.') && html.includes('>Talk it through instead<'), 'the form ends with the button, the note and the interview link');
assert.equal(html.split('Near the cap the jitter can exceed it.').length - 1, 1, 'the context is said once');

// A brief that only repeats the context is shown once.
const dup = page(true);
dup.context.fixture.focus.brief = dup.context.fixture.focus.context;
vm.runInContext('data = fixture', dup.context);
assert.equal(vm.runInContext('focusCard(data.focus)', dup.context).split('Near the cap the jitter can exceed it.').length - 1, 1);

// On a phone the decision card comes before the question box; on a desktop
// the box is the side column, so the main column is unchanged.
const stack = page(true);
const parts = [];
for (const name of ['focusCard', 'inquiryCard', 'noteCard', 'decisionsCard', 'requesterCard', 'peopleCard', 'historyCard'])
  vm.runInContext(`${name} = () => '<!--${name}-->'`, stack.context);
vm.runInContext('viewerMenu = accountCta = joinNote = () => ""; heroLine = () => "hero"; restoreDrafts = fitQuotes = () => {}; keepDrafts = () => ({})', stack.context);
stack.context.document = {activeElement: null, addEventListener() {}, querySelector: () => ({set innerHTML(v) { parts.push(v); }, set textContent(v) {}}),
  getElementById: () => null, querySelectorAll: () => []};
vm.runInContext('render()', stack.context);
const body = parts.join('');
const order = name => { const i = body.indexOf(`<!--${name}-->`); assert(i >= 0, name); return i; };
assert(order('focusCard') < order('inquiryCard') && order('inquiryCard') < order('decisionsCard'), 'phone: decision, then the question box, then the list');
assert(order('decisionsCard') < order('historyCard'), 'phone: history last');
console.log('Task page first-screen order checks passed');
