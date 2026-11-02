// What's been done on a task card: the rows /api/state carries on each
// live task, drawn as a timeline. Folded to the last two rows with "Show
// all N" until opened; an earlier call or a flagged question in a sentence
// becomes a link to it, a record its pill; the clock is the viewer's local
// time and the newest row says how long ago. This exercises the shipped
// home.js with DOM doubles, not a browser.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/home.js'), 'utf8');

function harness() {
  const c = {
    esc: v => String(v ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch])),
    ago: () => '2m ago',
    askDate: value => new Date(value).toLocaleDateString('en-US', {month: 'short', day: 'numeric', timeZone: 'UTC'}),
    callRefs: v => String(v ?? ''),
  };
  vm.createContext(c);
  const start = source.indexOf('const buildTimelineOpen'), end = source.indexOf('function buildCard(run)');
  assert.ok(start > 0 && end > start, 'home.js shape changed');
  vm.runInContext(source.slice(start, end) + '\n;globalThis.fns = {buildTimeline, buildTimelineText, buildTimelineOpen, buildClock};', c);
  return c.fns;
}

const text = html => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
const today = new Date();
const stamp = (h, m) => new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate(), h, m)).toISOString();
const rows = [
  {kind: 'engaged', at: stamp(11, 52), text: "Engaged: Alex's prompt read; 14 judgment calls identified", refs: [], people: ['Alex Moreno'], count: 1},
  {kind: 'settled', at: stamp(11, 53), text: 'Settled “What counts as one Pilot use” from Raj\'s Sep 17 call; PR #7 agrees; nobody asked',
    refs: [{kind: 'call', id: 'p1', run_id: 'old', label: "Raj's Sep 17 call"}, {kind: 'record', id: 'PR #7', label: 'PR #7'}], people: ['Raj Patel'], count: 1},
  {kind: 'flagged', at: stamp(11, 54), text: 'Flagged “What is the per-use rate” to Dana, who decides for pricing decisions; waiting on Dana',
    refs: [{kind: 'decision', id: 'd1', run_id: 'r1', label: 'What is the per-use rate'}], people: ['Dana Whitfield'], count: 1},
  {kind: 'guess', at: stamp(11, 54), text: 'Raven\'s guess on “Is Northwind charged” from Dana\'s Oct 29 call; Noah confirms',
    refs: [{kind: 'call', id: 'p2', run_id: 'r1', label: "Dana's Oct 29 call"}], people: ['Noah Kim'], count: 1},
  {kind: 'settled_record', at: stamp(11, 56), text: 'Settled “Is Northwind charged” from the repo\'s own record: PR #36 · #pricing; Noah signs it',
    refs: [{kind: 'pr', id: '36', label: 'PR #36'}, {kind: 'slack', id: 'pricing', label: '#pricing'}, {kind: 'ticket', id: 'BILL-9', label: 'BILL-9'}], people: [], count: 1},
  {kind: 'answered', at: stamp(11, 58), text: 'Dana answered “What is the per-use rate” on Slack <b>', refs: [], people: ['Dana Whitfield'], count: 1},
];

// Folded: the last two rows, how many came before, and the way to open it.
{
  const {buildTimeline} = harness();
  const html = buildTimeline('r1', rows);
  assert.match(html, /class="build-tl-cut">4 earlier</);
  assert.equal((html.match(/<li class="build-tl-row/g) || []).length, 2);
  assert.match(html, /Show all 6/);
  assert.match(html, /aria-expanded="false"/);
  assert.match(html, /What’s been done/);
  assert.doesNotMatch(html, /Engaged:/);
  // The sentence is escaped; the newest row says how long ago.
  assert.match(html, /on Slack &lt;b&gt; <small class="build-tl-ago">2m ago<\/small>/);
  assert.doesNotMatch(html, /<b>/);
  // A short timeline is simply shown, with nothing to open.
  const short = buildTimeline('r1', rows.slice(0, 3));
  assert.equal((short.match(/<li class="build-tl-row/g) || []).length, 3);
  assert.doesNotMatch(short, /Show all|earlier/);
  assert.equal(buildTimeline('r1', []), '');
  assert.equal(buildTimeline('r1', undefined), '');
}

// Opened: every row, oldest first, with its references marked in place.
{
  const {buildTimeline, buildTimelineOpen} = harness();
  buildTimelineOpen.add('r1');
  const html = buildTimeline('r1', rows);
  assert.equal((html.match(/<li class="build-tl-row/g) || []).length, 6);
  assert.match(html, /Show less/);
  assert.doesNotMatch(html, /earlier</);
  const order = [...html.matchAll(/<li class="build-tl-row ([a-z_]+)"/g)].map(m => m[1]);
  assert.deepEqual(order, ['engaged', 'settled', 'flagged', 'guess', 'settled_record', 'answered']);
  // An earlier call links to it, a flagged question to its decision, a record is a pill.
  assert.match(html, /from <a class="build-tl-ref" href="#runs\/old\/p1">Raj&#39;s Sep 17 call<\/a>; <span class="build-tl-pill">PR #7<\/span> agrees/);
  assert.match(html, /Flagged “<a class="build-tl-ref" href="#runs\/r1\/d1">What is the per-use rate<\/a>” to Dana/);
  assert.match(html, /<span class="build-tl-pill">PR #36<\/span> · <span class="build-tl-pill">#pricing<\/span>; Noah signs it <span class="build-tl-pill">BILL-9<\/span>/, 'a record not named in the sentence trails it as a pill');
  // The whole thing, with no head, for the task page.
  const full = buildTimeline('r1', rows, {all: true, head: false});
  assert.doesNotMatch(full, /build-tl-head|Show less/);
  assert.equal((full.match(/<li class="build-tl-row/g) || []).length, 6);
  // The clock is HH:MM in the viewer's zone; the stored ISO stays on the element.
  assert.match(html, new RegExp(`<time datetime="${rows[0].at}" title="${rows[0].at}">\\d\\d:\\d\\d</time>`));
}

// A row from another day carries its date once, above the first of them.
{
  const {buildTimeline} = harness();
  const old = [{...rows[0], at: '2026-10-29T14:02:00.000Z'}, {...rows[1], at: '2026-10-29T14:05:00.000Z'}, rows[5]];
  const html = buildTimeline('r1', old, {all: true});
  assert.equal((html.match(/class="build-tl-day"/g) || []).length, 1);
  assert.match(html, /<li class="build-tl-day">Oct 29<\/li>/);
}

// Marking a reference twice never nests: the second label is found after the first.
{
  const {buildTimelineText} = harness();
  const html = buildTimelineText({text: "preferred Dana's Oct 29 call over Dana's Sep 24 call", refs: [
    {kind: 'call', id: 'a', run_id: 'x', label: "Dana's Oct 29 call"}, {kind: 'call', id: 'b', run_id: 'y', label: "Dana's Sep 24 call"}]});
  assert.equal(html, 'preferred <a class="build-tl-ref" href="#runs/x/a">Dana&#39;s Oct 29 call</a> over <a class="build-tl-ref" href="#runs/y/b">Dana&#39;s Sep 24 call</a>');
  assert.equal(buildTimelineText({text: 'plain', refs: [{kind: 'call', id: '', label: 'nothing'}]}), 'plain');
}

console.log('timeline ui unit checks passed');
