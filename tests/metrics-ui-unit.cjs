// Dependency-free checks of the "What Raven saved" strip and the per-task line, with DOM/API doubles.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const taskSource = fs.readFileSync(path.join(__dirname, '../web/task.js'), 'utf8');
const style = fs.readFileSync(path.join(__dirname, '../web/style.css'), 'utf8');

const summary = {
  window_days: 30, decisions: 7,
  proposals: {total: 4, signed_unchanged: 1, corrected: 1, by_rule: 1, open: 1, unchanged_rate: 0.5, corrected_rate: 0.5},
  time_to_answer: {count: 2, median_seconds: 25200, p90_seconds: 36000},
  time_to_signature: {count: 2, median_seconds: 7200, p90_seconds: 10800},
  reask: {asked: 3, repeated: 1, rate: 0.333},
  reuse: {total: 3, from_memory: 2, by_rule: 1, duplicates: 1, rate: 0.429},
  handon: {routed: 6, handed_on: 1, rate: 0.167},
  wait_saved: {seconds: 28800, decisions: 2, estimated: 0},
};
let requests = [], renders = 0, served = summary;
const context = {
  view: 'connect', notify: () => {}, render: () => { renders++; }, Date,
  api: async p => { requests.push(p); return served; },
};
vm.createContext(context);
vm.runInContext('let savings = null, savingsLoading = false, savingsAt = 0; const STALE_DRAW_MS = 60000;\n'
  + source.slice(source.indexOf('function durationLabel('), source.indexOf('function runs() {')), context);

(async () => {
  const {durationLabel} = context;
  assert.equal(durationLabel(null), '—');
  assert.equal(durationLabel(30), '<1m');
  assert.equal(durationLabel(90), '2m');
  assert.equal(durationLabel(5400), '1.5h');
  assert.equal(durationLabel(7200), '2h');
  assert.equal(durationLabel(129600), '1.5d');
  // The first draw asks once and says it is measuring; the answer redraws the page with the numbers.
  assert.match(context.savedStrip(), /Measuring what Raven saved/);
  assert.match(context.savedStrip(), /Measuring/);
  assert.deepEqual(requests, ['/api/metrics?days=30'], 'one request while it loads');
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(renders, 1);
  let strip = context.savedStrip();
  assert.match(strip, /^<p class="saved-strip" role="status"><strong>What Raven saved<\/strong> · last 30 days · /);
  assert.match(strip, /<strong>3<\/strong> of 7 answered from memory or a rule/);
  assert.match(strip, /<strong>1 of 2<\/strong> proposals signed unchanged/);
  assert.match(strip, /answers in <strong>7h<\/strong> median, 10h p90/);
  assert.match(strip, /<strong>1 of 6<\/strong> handed on/);
  assert.match(strip, /<strong>1<\/strong> asked again after an answer existed/);
  assert.match(strip, /<strong>8h<\/strong> of agent waiting avoided/);
  assert.equal((strip.match(/<p /g) || []).length, 1, 'one line');
  assert.equal(requests.length, 1, 'a fresh answer is not asked for again');
  // An estimate is marked as one; a window with nothing in it says so.
  const shown = value => vm.runInContext(`savings = ${JSON.stringify(value)}; savedStrip()`, context);
  assert.match(shown({...summary, wait_saved: {seconds: 3600, decisions: 1, estimated: 1}}), /<strong>≈1h<\/strong> of agent waiting avoided/);
  assert.match(shown({...summary, decisions: 0}), /no decisions recorded yet/);
  // A viewer is refused the numbers (403): the strip is empty and nothing is announced or asked again.
  let notified = 0; context.notify = () => { notified++; };
  context.api = async p => { requests.push(p); const error = new Error('Your role (viewer) does not allow this; it needs member'); error.status = 403; throw error; };
  vm.runInContext('savings = null; savingsAt = 0;', context);
  assert.match(context.savedStrip(), /Measuring/);
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(context.savedStrip(), '');
  assert.equal(notified, 0);
  assert.equal(requests.length, 2, 'the refusal is not asked again');
  // The strip opens Tasks and lives in Settings > Advanced.
  assert.match(source, /function runs\(\) \{\n  return savedStrip\(\) \+ `<div class="wide-toolbar"><h2>Tasks in this workspace<\/h2>/);
  assert.match(source, /<details class="history settings-advanced" id="repository-signals"><summary>Advanced<\/summary>\$\{savedStrip\(\)\}<h3>Who git thinks owns what<\/h3>\$\{ownershipGraph\(\)\}/);
  assert.match(style, /\.saved-strip\{/);

  // Per task: what came from memory or a rule, what a person signed as proposed, and how long people took.
  const taskContext = {esc: v => String(v), durationLabel, taskList: v => Array.isArray(v) ? v : []};
  vm.createContext(taskContext);
  vm.runInContext(taskSource.slice(taskSource.indexOf('function taskSaved('), taskSource.indexOf('function taskQuestions(')), taskContext);
  const nodes = [
    {node_id: 'a', status: 'approved', source: 'human', signoff: 'signed', created_at: '2026-10-01T08:00:00+00:00'},
    {node_id: 'b', status: 'resolved', source: 'memory', signoff: 'required', created_at: '2026-10-01T08:00:00+00:00'},
    {node_id: 'c', status: 'resolved', source: 'record', signoff: 'rule', created_at: '2026-10-01T08:00:00+00:00'},
    {node_id: 'd', status: 'duplicate', source: '', signoff: '', created_at: '2026-10-01T08:00:00+00:00'},
    {node_id: 'e', status: 'resolved', source: 'agent', signoff: 'signed', created_at: '2026-10-01T08:00:00+00:00'},
    {node_id: 'f', status: 'suggested', source: 'memory', signoff: '', created_at: '2026-10-01T08:00:00+00:00'},
  ];
  const trace = {events: [
    {kind: 'owner_approved', decision_id: 'a', at: '2026-10-01T10:00:00+00:00', detail: {answer: 'x'}},
    {kind: 'signoff', decision_id: 'e', at: '2026-10-01T08:30:00+00:00', detail: {corrected: false}},
    {kind: 'owner_approved', decision_id: 'e', at: '2026-10-01T09:30:00+00:00', detail: {answer: 'later correction'}},
  ]};
  const line = taskContext.taskSaved(nodes, trace);
  assert.equal(line, '<p class="task-saved">Raven saved: 3 answered from memory · 1 signed unchanged · 2.5h waited on people</p>');
  assert.equal(taskContext.taskSaved([{node_id: 'z', status: 'pending', source: '', signoff: '', created_at: '2026-10-01T08:00:00+00:00'}], {events: []}), '');
  assert.equal(taskContext.taskSaved([], null), '');
  assert.match(taskSource, /recorded questions<\/p>\$\{taskSaved\(nodes, trace\)\}<p class="task-next">/);
  console.log('Metrics UI checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
