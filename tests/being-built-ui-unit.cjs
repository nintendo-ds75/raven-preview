/* Being built: a task card counts each judgment call once (a question
 * folded into an open node of the same task is not another call), shows the
 * goal under the title only when it says more than the title, and reads a
 * call settled from a meeting as "From the Oct 29 ... · nobody asked".
 * This exercises the shipped home.js with DOM doubles, not a browser. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/home.js'), 'utf8');

function harness(state) {
  const c = {
    state,
    esc: v => String(v ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch])),
    avatar: name => `<span class="avatar">${String(name || '?')[0]}</span>`,
    plural: (n, one, many = one + 's') => `${n} ${n === 1 ? one : many}`,
    ago: () => '2 min ago',
    askFirst: name => String(name || '').split(/\s+/)[0],
  };
  vm.createContext(c);
  const start = source.indexOf('function buildSources(d)'), end = source.indexOf('function buildAttention(stories)');
  assert.ok(start > 0 && end > start, 'home.js shape changed');
  vm.runInContext(source.slice(start, end) + '\n;globalThis.fns = {taskStory, buildCard, buildLine, judge, buildGoal};', c);
  return c.fns;
}

const text = html => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
const PROMPT = 'We want to start charging our Enterprise customers for Pilot based on how much they use it.';
const run = {id: 'r1', title: 'Start charging our Enterprise customers for Pilot', goal: PROMPT, agent: 'Claude Code',
  requester: 'Alex Moreno', status: 'needs_judgment', updated_at: '2026-11-02T10:00:00Z'};
const base = {run_id: 'r1', category: 'policy', signoff: '', source_id: '', supersedes: '', superseded_by: '', needs_review: 0,
  created_at: '2026-11-02T10:00:00Z', updated_at: '2026-11-02T10:00:00Z', answer: '', context: '', evidence: ''};
const decisions = [
  {...base, id: 'rate', question: 'What per-use rate does Enterprise pay for Pilot?', status: 'pending', owner_name: 'Dana Whitfield'},
  {...base, id: 'cents', question: 'How many cents does Enterprise pay per completed Pilot response?', status: 'duplicate',
    superseded_by: 'rate', owner_name: 'Dana Whitfield'},
  {...base, id: 'nw', question: 'Is Northwind charged for Pilot, and at what rate?', status: 'resolved', signoff: 'rule',
    source: 'meeting', answered_via: 'meeting', answered_by: 'Dana Whitfield', owner_name: 'Dana Whitfield',
    source_revision: '2026-10-29', rationale: 'Agreed. Northwind stays on their contract terms, no public rate for them until Noah clears it.',
    answer: 'Northwind stays on its contract terms; no public rate until Noah clears the MFN clause.',
    fit_reason: 'From the Oct 29 Pilot pricing sync (Dana, Elena): Dana Whitfield, who decides pricing, said “Agreed. Northwind stays on their contract terms, no public rate for them until Noah clears it.”; nobody was asked.'},
];

// A folded question is not another call: one waiting, one settled, two in all.
{
  const {taskStory, buildCard} = harness({runs: [run], decisions, me: null});
  const story = taskStory(run);
  assert.deepEqual(story.ds.map(d => d.id).sort(), ['nw', 'rate']);
  assert.equal(story.asked.length, 1);
  assert.equal(story.settled.length, 1);
  const card = text(buildCard(run));
  assert.match(card, /2 judgment calls the agent hit/);
  assert.match(card, /1 settled by Raven from earlier calls, nobody asked/);
  assert.match(card, /1 reached a person: Dana/);
  assert.match(card, /Claude Code · for Alex Moreno/);
  assert.doesNotMatch(card, /How many cents/);
}

// The goal shows under the title when it says more; not when it is the title again.
{
  const {buildCard, buildGoal} = harness({runs: [run], decisions, me: null});
  assert.equal(buildGoal(run), PROMPT);
  assert.match(text(buildCard(run)), /Start charging our Enterprise customers for Pilot We want to start charging/);
  const twice = {...run, title: PROMPT};
  assert.equal(buildGoal(twice), '');
  const card = text(buildCard(twice));
  assert.equal(card.split('We want to start charging').length - 1, 1, 'the prompt is shown once');
  assert.equal(buildGoal({...run, goal: ''}), '');
}

// A call settled from a meeting: settled, from the meeting, nobody asked.
{
  const {judge, buildLine} = harness({runs: [run], decisions, me: null});
  const nw = decisions.find(d => d.id === 'nw');
  const j = judge(nw);
  assert.equal(j.kind, 'settled');
  assert.equal(j.priorBy, 'Dana Whitfield');
  const line = text(buildLine(nw, j));
  assert.equal(line, 'From the Oct 29 Pilot pricing sync (Dana, Elena) · nobody asked');
  // An ordinary precedent still reads as before.
  const prior = {...base, id: 'p', question: 'Which table?', status: 'approved', answered_by: 'Raj Patel', created_at: '2026-01-22T12:00:00Z'};
  const settled = {...base, id: 's', question: 'Which table is read?', status: 'resolved', signoff: 'rule', source_id: 'p', source_revision: '2026-01-22T12:00:00Z'};
  const h = harness({runs: [run], decisions: [prior, settled], me: null});
  assert.equal(text(h.buildLine(settled, h.judge(settled))), 'From Raj’s call · Jan 22 · nobody asked');
}

console.log('being-built ui unit checks passed');
