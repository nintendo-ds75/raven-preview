/* The task page has no Today view and reads its link token from the
 * fragment once: an "Open in Today" link on it would only replace that
 * token with "#today/<id>", so the page shows the proposed scope without
 * the link. The server card still carries it for the inbox. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2] || 'web/brief.js', 'utf8');

const cards = [];
const context = {location: {hash: '#rvn_synthetic'}, console,
  addEventListener() {}, setInterval() {}, fetch: () => new Promise(() => {}),
  ResizeObserver: class {observe() {} disconnect() {}},
  document: {addEventListener() {}},
  matchMedia: () => ({matches: false, addEventListener() {}}),
  approvalScope: () => '<section class="decision-scope"></section>',
  objectiveScopeCard: (card, id) => { cards.push(card); return `<div class="objective-scope" id="${id}">${card.link_to_today ? `<a class="objective-scope-link" href="${card.link_to_today}">Open in Today</a>` : ''}</div>`; },
  taskDisplayLabel: () => 'Synthetic task'};
context.window = context;
vm.createContext(context);
vm.runInContext(source, context);
vm.runInContext(`workItemContext = () => ''`, context);
context.fixture = {task: {id: 't1', title: 'Synthetic task', repo: 'acme/platform', status: 'active'},
  viewer: {name: 'Lina', role: 'member'}, requester: {name: 'Requester'}, progress: {signed: 0, total: 1},
  nodes: [], contacts: [], history: [], notes: [],
  focus: {node_id: 'd1', question: 'Should Raven take point on: Draft the renewal note?', status: 'pending', can_act: true,
    asked: true, updated_at: 'r1', brief: 'Raven proposes to take point on Draft the renewal note.', context: '',
    options: [], paths: [], owner: 'Lina', found: [], approval_scope: {id: 'd1', facts: {}}, approval_scope_text: 'scope',
    why: {heading: 'Why you', summary: 'You decide billing/.'},
    objective: {title: 'Renewal', outcome: 'Draft the renewal note', cases: {}, evidence: [], link_to_today: '#today/p1'}}};
vm.runInContext('data = fixture', context);
const html = vm.runInContext('focusCard(data.focus)', context);

assert.equal(cards.length, 1, 'the proposed scope is rendered once');
assert.equal(cards[0].link_to_today, '', 'the page passes the card without the Today link');
assert.equal(cards[0].outcome, 'Draft the renewal note', 'everything else on the card is kept');
assert.doesNotMatch(html, /href="#today\//, 'no link on the page can replace the link token in the fragment');
assert.match(html, /class="objective-scope"/, 'the proposed scope is still shown');
// The hand-on select groups people in plain words; no internal noun reaches the page.
assert.match(source, /<optgroup label="Other people">/);
assert.doesNotMatch(source, /authority map/i);
console.log('brief scope link: ok');
