/* For you: a call that reached the wrong person is handed on from the card
 * itself. The card offers "Not mine: hand it on"; opened, it lists everyone
 * but the signed-in person, takes a note, and binds the revision the card
 * showed. This exercises the shipped attention.js with DOM doubles. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/attention.js'), 'utf8');

function harness(state, hand = []) {
  const c = {
    state, attention: {person: {name: 'Priya Shah'}, needs_you: []},
    esc: v => String(v ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch])),
    callRefs: v => String(v ?? ''), ago: () => '3 min ago',
  };
  Object.assign(c, {attOpen: new Set(), attForm: new Set(), attHand: new Set(), attVia: {},
    attFirst: name => String(name || '').split(/\s+/)[0], attWait: () => '3 min',
    attHref: (runId, id) => `#runs/${runId}${id ? '/' + id : ''}`});
  vm.createContext(c);
  const start = source.indexOf('function attNeeds(item)'), end = source.indexOf('function attHandled(item)');
  assert.ok(start > 0 && end > start, 'attention.js shape changed');
  vm.runInContext(source.slice(start, end) + '\n;globalThis.fns = {attNeeds, attHandOn};', c);
  for (const id of hand) c.attHand.add(id);
  return c.fns;
}

const text = html => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
const me = {id: 'p-priya', name: 'Priya Shah', kind: 'session'};
const owners = [{id: 'o-priya', name: 'Priya Shah', team: 'Founders'}, {id: 'p-sam', name: 'Sam Rivera', team: 'Customer success'},
  {id: 'p-marcus', name: 'Marcus Lee', team: 'Billing'}];
const item = {id: 'bloom', run_id: 'r1', task: 'Start charging merchants for the assistant', agent: 'Claude Code', requester: 'Maya Chen',
  question: 'Is Bloom & Co excluded from assistant charges until Sam clears their order form?', why: 'No earlier call covers this. You decide for customer decisions.',
  guess: '', options: [], meanwhile: 'Claude Code is building the parts that do not depend on this.', context: '', gaps: [],
  created_at: '2026-11-02T20:00:00Z', updated_at: '2026-11-02T20:05:00Z'};

// Closed: the card offers the hand-on beside the answer and the call, nothing more.
{
  const {attNeeds} = harness({me, owners, auth: {enabled: true}});
  const html = attNeeds(item);
  assert.match(html, /data-att="handon" data-id="bloom" aria-expanded="false">Not mine: hand it on</);
  assert.ok(!html.includes('att-handon'), 'the row stays closed until asked for');
  assert.match(html, /Record my answer/);
}

// Open: everyone but me, a note, and the revision the card showed.
{
  const {attNeeds} = harness({me, owners, auth: {enabled: true}}, ['bloom']);
  const html = attNeeds(item);
  assert.match(html, /<form class="att-handon" data-id="bloom" data-updated="2026-11-02T20:05:00Z">/);
  assert.match(html, /aria-expanded="true"/);
  const options = [...html.matchAll(/<option value="([^"]*)">([^<]*)<\/option>/g)].map(m => [m[1], m[2]]);
  assert.deepEqual(options, [['', 'Pick a person'], ['Sam Rivera', 'Sam Rivera · Customer success'], ['Marcus Lee', 'Marcus Lee · Billing']]);
  assert.match(html, /<input name="note" maxlength="200" placeholder="A word for them \(optional\)">/);
  assert.match(text(html), /Hand this to .* Hand on Cancel/);
  assert.match(html, /data-att="cancel-handon" data-id="bloom"/);
}

// Names are escaped, and with no owners loaded the row still renders with only the empty choice.
{
  const {attNeeds} = harness({me, owners: [{id: 'x', name: 'O\'Brien <QA>', team: ''}], auth: {enabled: true}}, ['bloom']);
  const html = attNeeds(item);
  assert.match(html, /<option value="O&#39;Brien &lt;QA&gt;">O&#39;Brien &lt;QA&gt;<\/option>/);
  const {attNeeds: bare} = harness({me, auth: {enabled: true}}, ['bloom']);
  assert.match(bare(item), /<option value="">Pick a person<\/option><\/select>/);
}

// The submit handler sends the hand-on to the refer API with the note and the revision.
{
  const handler = source.slice(source.indexOf("document.addEventListener('submit'"));
  assert.match(handler, /closest\('\.att-answer, \.att-correct, \.att-handon'\)/);
  assert.match(handler, /att-handon'\)\) \{\s*if \(!data\.person\) throw new Error\('Pick the person to hand it to first\.'\);\s*result = await api\(`\/api\/decisions\/\$\{encodeURIComponent\(form\.dataset\.id\)\}\/refer`, \{person: data\.person, \.\.\.\(data\.note \? \{note: data\.note\} : \{\}\), expected_updated_at: form\.dataset\.updated\}\)/);
}
console.log('for-you-handon-ui-unit: ok');
