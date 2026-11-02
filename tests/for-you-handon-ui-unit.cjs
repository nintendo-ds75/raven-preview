/* For you: a call that reached the wrong person is handed on from the card
 * itself. The card offers "Not mine: hand it on"; opened, it lists everyone
 * but the signed-in person, takes a note, and binds the revision the card
 * showed. The row is not a form and never submits one: on the Parcelwise
 * demo (Nov 2, Chromium) a hand-on from the card was followed by the
 * browser's own "Please fill out this field." on an answer box. This
 * exercises the shipped attention.js with DOM doubles. */
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
  vm.runInContext(source.slice(start, end) + '\n;globalThis.fns = {attNeeds, attHandOn, attSendHandOn: typeof attSendHandOn === "function" ? attSendHandOn : null};', c);
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
  assert.match(html, /att-composer/); assert.match(html, /Send</);
}

// Open: everyone but me, a note, and the revision the card showed.
{
  const {attNeeds} = harness({me, owners, auth: {enabled: true}}, ['bloom']);
  const html = attNeeds(item);
  assert.match(html, /<div class="att-handon" role="group" aria-label="Hand this call on" data-id="bloom" data-updated="2026-11-02T20:05:00Z">/);
  assert.match(html, /aria-expanded="true"/);
  // The row is beside the answer form, not inside it, and nothing in it is a form or a submit
  // button: a browser attaches a submit button to the nearest open form, and a "Hand on" that
  // submitted the answer form would raise the browser's "Please fill out this field." bubble.
  const answerEnd = html.indexOf('</form>'), row = html.indexOf('class="att-handon"');
  assert.ok(answerEnd > 0 && row > answerEnd, 'the hand-on row follows the closed answer form');
  assert.equal(html.lastIndexOf('</form>'), answerEnd, 'the card has one form, the answer');
  const rowHtml = html.slice(row);
  assert.ok(!/<form/.test(rowHtml), 'the hand-on row is not a form');
  assert.ok(!/type="submit"/.test(rowHtml), 'no submit button after the answer form');
  assert.match(rowHtml, /<button class="button small primary" type="button" data-att="send-handon" data-id="bloom">Hand on<\/button>/);
  assert.ok(!/<select[^>]* required/.test(rowHtml), 'the row says itself when nobody is picked, not the browser');
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

// Handing on posts to the refer API with the person, the note and the revision the card showed,
// closes the row and says what happened; with nobody picked it says so in the row and posts nothing.
(async () => {
  const calls = [], notices = [];
  const row = (person, note) => {
    const fields = {'select[name=person]': {value: person, focus() { fields.focused = true; }}, 'input[name=note]': {value: note},
      '.ask-error': {textContent: '', hidden: true}, '[data-att="send-handon"]': {disabled: false, textContent: 'Hand on'}};
    return Object.assign(fields, {dataset: {id: 'bloom', updated: '2026-11-02T20:05:00Z'}, querySelector: sel => fields[sel]});
  };
  const c = vm.createContext({attHand: new Set(['bloom']), attentionKey: 'stale', loaded: [], refreshed: 0,
    api: async (path, body) => { calls.push([path, body]); return {notice: 'Handed to Sam Rivera'}; },
    notify: m => notices.push(m), loadAttention: force => c.loaded.push(force), refresh: async () => { c.refreshed++; }});
  const start = source.indexOf('async function attSendHandOn(row)'), end = source.indexOf('// What the source left open');
  assert.ok(start > 0 && end > start, 'attention.js shape changed');
  vm.runInContext(source.slice(start, end) + '\n;globalThis.send = attSendHandOn;', c);
  const sent = row('Sam Rivera', ' Yours, I think ');
  const done = c.send(sent);
  assert.equal(sent['[data-att="send-handon"]'].disabled, true, 'the button is busy before the request leaves');
  assert.equal(sent['[data-att="send-handon"]'].textContent, 'Handing on…');
  await done;
  // Values cross from the vm realm: compare their shape, not their prototypes.
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [['/api/decisions/bloom/refer', {person: 'Sam Rivera', note: 'Yours, I think', expected_updated_at: '2026-11-02T20:05:00Z'}]]);
  assert.deepEqual(notices, ['Handed to Sam Rivera']);
  assert.equal(c.attHand.has('bloom'), false, 'the row closes');
  assert.equal(c.attentionKey, ''); assert.deepEqual([...c.loaded], [true]); assert.equal(c.refreshed, 1);
  const empty = row('', '');
  await c.send(empty);
  assert.equal(calls.length, 1, 'nothing posted without a person');
  assert.equal(empty['.ask-error'].textContent, 'Pick the person to hand it to first.');
  assert.equal(empty['.ask-error'].hidden, false);
  assert.equal(empty.focused, true, 'the select gets focus back');
  assert.equal(empty['[data-att="send-handon"]'].disabled, false);

  // The submit handler owns the answer and the correction only; the row's button and Enter in
  // its fields go through attSendHandOn.
  const handler = source.slice(source.indexOf("document.addEventListener('submit'"));
  assert.match(handler, /closest\('\.att-answer, \.att-correct'\)/);
  assert.ok(!handler.includes('att-handon'), 'no hand-on branch in the submit handler');
  assert.match(source, /att === 'send-handon'\) \{ const row = button\.closest\('\.att-handon'\); if \(row\) attSendHandOn\(row\); return; \}/);
  assert.match(source, /event\.key !== 'Enter'[^]*closest\?\.\('\.att-handon'\)[^]*event\.preventDefault\(\);\s*attSendHandOn\(row\);/);
  console.log('for-you-handon-ui-unit: ok');
})().catch(error => { console.error(error); process.exit(1); });
