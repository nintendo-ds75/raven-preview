// Work-item context is visible and escaped, without evidence or approval controls.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const os = require('node:os');
const {execFileSync} = require('node:child_process');
const app = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const brief = fs.readFileSync(path.join(__dirname, '../web/brief.js'), 'utf8');
const source = {record_id:'stored-record', source_version_id:'exact-version', provider:'jira',
  namespace:'installation-a', kind:'jira', external_id:'object-1', ref:'CASE-1', role:'work_item', current:true};
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'raven-work-item-render-'));
let payloads;
try {
  payloads = JSON.parse(execFileSync(process.env.BRIDGE_PYTHON || 'python3',
    [path.join(__dirname, 'work_item_browser_server.py'), path.join(temp, 'render.db'), '--render-payloads'],
    {encoding:'utf8'}));
} finally { fs.rmSync(temp, {recursive:true, force:true}); }
for (const [text, end] of [[app, 'function sourceRevalidationFields('], [brief, 'function focusCard(']]) {
  const context = {};
  vm.createContext(context);
  vm.runInContext(app.slice(app.indexOf('const esc ='), app.indexOf('const plural =')), context);
  vm.runInContext(text.slice(text.indexOf('function workItemContext('), text.indexOf(end)), context);
  assert.equal(context.workItemContext({}), '');
  const unlinked = context.workItemContext({work_item_association:{declared:'CASE-1 <script>',status:'unlinked'}});
  assert(unlinked.includes('CASE-1 &lt;script&gt;'));
  assert(!unlinked.includes('<script>'));
  assert(unlinked.includes('no explicit work-item link'));
  assert(!unlinked.includes('stored-record'));
  const linked = context.workItemContext({source_anchors:[source],
    work_item_association:{declared:'CASE-1',status:'linked'},
    work_item_history:[{created_at:'2026-01-01T00:00:00Z',detail:'Explicit association <script>'}]});
  for (const value of ['stored-record','exact-version','installation-a','object-1','current observed version',
    'supporting evidence or approval']) assert(linked.includes(value), value);
  for (const control of ['<form', '<input', 'source-pins', 'source-confirm']) assert(!linked.includes(control), control);
  assert(!linked.includes('<script>'));
  const ambiguous = context.workItemContext({source_anchors:[source],work_item_association:{declared:'CASE-1',status:'ambiguous'}});
  assert(ambiguous.includes('canonical identities'));
  const old = context.workItemContext({source_anchors:[{...source,current:false,role:'context'}]});
  assert(old.includes('historical or unavailable version'));
  assert(old.includes('context'));
  const decisionOnly = context.workItemContext(payloads.decision_only);
  assert(decisionOnly.includes('Decision association'));
  assert(!decisionOnly.includes('Task association'));
  const ownLink = payloads.decision_only.work_item_association.links[0];
  for (const key of ['record_id','source_version_id','external_id','namespace']) assert(decisionOnly.includes(ownLink[key]), key);
  const ambiguousNode = context.workItemContext(payloads.ambiguous_node);
  assert(ambiguousNode.includes('canonical identities'));
  const taskLinks = payloads.ambiguous_node.work_item_association.links;
  assert.equal(taskLinks.length, 2);
  for (const link of taskLinks) assert(ambiguousNode.includes(link.record_id), link.record_id);
  assert(ambiguousNode.includes('Task association'));
  assert(!ambiguousNode.includes('Decision association'));
  const closed = context.workItemContext(payloads.historical_task);
  assert(closed.includes('Historical'));
  assert(closed.includes('start a new task'));
  assert(!closed.includes('bridge_link_work_item'));
}
assert(app.includes('${workItemContext(d)}'));
assert(brief.includes('${workItemContext(f)}'));
const task = fs.readFileSync(path.join(__dirname, '../web/task.js'), 'utf8');
assert.match(task, /taskDetails\(workItemContext\(t\),/);
assert.match(task, /taskDetails\(workItemContext\(n\),/);
console.log('Work-item context rendering checks passed');
