// The Being built page never shows a call as settled, or as waiting on
// someone, while Raven's model pass is still reading it. Measured on a dry
// run: the static first pass matched "per-use rate" to an earlier "which
// plans" call and the page said "nobody asked" until the model pass withdrew
// it a minute later. Dependency-free: the log's judge() and taskStory() are
// run in a vm context with doubles for the page helpers.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/home.js'), 'utf8');

const prior = {id: 'p1', run_id: 'old', question: 'Which plans are charged per use for Pilot?', answer: 'Enterprise only.',
  answered_by: 'Dana Whitfield', status: 'approved', created_at: '2026-09-24T15:45:00+00:00'};
const checking = {id: 'd1', run_id: 'r1', question: 'What is the per-use rate Enterprise pays for Pilot?', status: 'proposed',
  signoff: 'required', source_id: 'p1', source_revision: '2026-09-24T15:45:00+00:00', kind: 'prediction',
  owner_name: 'Dana Whitfield', model_pending: 1, created_at: '2026-11-02T10:00:00+00:00'};
const settled = {...checking, id: 'd2', status: 'resolved', signoff: 'rule', model_pending: 0};
const waiting = {id: 'd3', run_id: 'r1', question: 'From which date is Pilot usage charged?', status: 'pending',
  owner_name: 'Dana Whitfield', model_pending: 0, created_at: '2026-11-02T10:00:00+00:00'};
const context = {
  state: {decisions: [prior, checking, settled, waiting], me: null},
  askFirst: name => String(name || '').split(/\s+/)[0],
  esc: v => String(v ?? ''), ago: () => 'a minute ago', avatar: () => '', plural: (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`,
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('const buildById'), source.indexOf('function buildDetail(')), context);

const {judge, taskStory, buildLine} = context;
assert.equal(judge(checking).kind, 'checking', 'a model_pending match is a check in progress, not settled');
assert.equal(judge(settled).kind, 'settled');
assert.equal(judge(waiting).kind, 'waiting');
assert.equal(judge({...waiting, model_pending: 1}).kind, 'checking', 'a routed node is not "waiting on" anyone until the read finishes');
assert.equal(judge({...checking, status: 'approved', answered_by: 'Dana Whitfield'}).kind, 'answered', 'a person\'s answer stands whatever the flag says');

const story = taskStory({id: 'r1'});
assert.equal(story.settled.map(d => d.id).join(), 'd2');
assert.equal(story.checking.map(d => d.id).join(), 'd1');
assert.equal(story.asked.map(d => d.id).join(), 'd3');
assert.ok(!story.reached.includes('Dana Whitfield') || story.asked.length, 'checking alone reaches nobody');

const line = buildLine(checking, judge(checking));
assert.match(line, /still reading/);
assert.match(line, /nobody asked yet/);
assert.doesNotMatch(line, /From Dana/);
assert.match(buildLine(settled, judge(settled)), /From Dana.*nobody asked/);
console.log('build-checking-ui-unit: ok');
