/* For you: a reused answer a record disagrees with is shown as the person's
 * words and Raven's note apart, never as one quoted paragraph. Measured on
 * the Parcelwise take 6 (Nov 2): Jordan's card quoted "CONFLICTED, do not
 * act on either side alone: …" as his Jun 1 call. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/attention.js'), 'utf8');

const c = {
  state: {auth: {enabled: false}, me: {}}, attention: {person: {name: 'Jordan Okafor'}, needs_you: []},
  esc: v => String(v ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch])),
  callRefs: v => String(v ?? ''), attOpen: new Set(), attForm: new Set(), attHand: new Set(), attVia: {},
  attFirst: name => String(name || '').split(/\s+/)[0], attWait: () => '3 min', attDate: () => 'Jun 1',
  attHref: (runId, id) => `#runs/${runId}${id ? '/' + id : ''}`,
};
vm.createContext(c);
const start = source.indexOf('function attNeeds(item)'), end = source.indexOf('function attHandled(item)');
assert.ok(start > 0 && end > start, 'attention.js shape changed');
vm.runInContext(source.slice(start, end) + '\n;globalThis.fns = {attNeeds};', c);
const text = html => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();

const item = {id: 'uses', run_id: 'r1', task: 'Start charging merchants for the assistant', agent: 'Claude Code', requester: 'Maya Chen',
  question: 'What counts as one billable assistant use for charging?', why: 'Your earlier call and pr #216 disagree, so Raven asks you which stands.',
  guess: 'One billable assistant use is one completed answer to one question.',
  conflict: {by: 'Jordan Okafor', record: 'pr #216', claim: 'Timeouts are a third of the answers merchants never get.', figure: false},
  guess_from: {id: 'd0', run_id: 'r0', task: 'One use of the assistant', by: 'Jordan Okafor', when: '2026-06-01T10:00:00Z', answered_via: ''},
  options: ['Completed answers only', 'Every attempt'], meanwhile: 'Claude Code is waiting on this.', context: '', gaps: [],
  created_at: '2026-11-02T20:00:00Z', updated_at: '2026-11-02T20:05:00Z'};
const html = c.fns.attNeeds(item);
const t = text(html);
assert.ok(!t.includes('CONFLICTED'), 'no composed paragraph on the card');
assert.match(t, /Two readings disagree: your call “One billable assistant use is one completed answer to one question\.”/);
assert.match(t, /But pr #216 says “Timeouts are a third of the answers merchants never get\.” Neither is corroborated over the other\./);
assert.match(t, /from your Jun 1 call on One use of the assistant/);
assert.match(html, /data-att="apply"/, 'the earlier answer can still be applied');
const plain = c.fns.attNeeds({...item, conflict: null});
assert.match(text(plain), /Raven would apply this, but isn’t sure: your call/);
assert.ok(!text(plain).includes('Neither is corroborated'));
console.log('for-you-conflict-ui-unit: ok');
