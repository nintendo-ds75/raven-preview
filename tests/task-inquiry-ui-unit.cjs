'use strict';
// Actual task-page logic with queued API/DOM doubles; browser acceptance is separate.
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/task.js'), 'utf8');
const escape = v => String(v ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
(async () => {
  const requests = []; let renders = 0;
  const c = {view:'runs', location:{hash:'#runs/task-a'}, esc:escape,
    render:() => { renders++; }, api:(url,body) => new Promise((resolve,reject) => requests.push({url,body,resolve,reject}))};
  vm.createContext(c); vm.runInContext(source,c);
  const run = code => vm.runInContext(code,c);
  let html = run("taskInquiry('task-a')");
  assert.match(html,/Ask about this task/); assert.match(html,/maxlength="2000"/);
  assert.match(html,/Read-only/); assert.doesNotMatch(html,/signoff-form|data-action="signoff"/);
  assert.match(source,/\$\{progress\}\$\{taskInquiry\(t.task_id\)\}/,'composer must be on the primary task page');
  const input = {value:'  What is blocked?  '}, button = {disabled:false};
  c.form = {dataset:{id:'task-a'},isConnected:true,reportValidity:()=>true,
    querySelector:s=>s==='[name="question"]'?input:button};
  const pending = run('submitTaskInquiry(form)'); await run('submitTaskInquiry(form)');
  assert.equal(requests.length,1); assert.equal(button.disabled,false); assert.equal(button.ariaDisabled,'true');
  assert.equal(requests[0].url,'/api/tasks/task-a/inquiry');
  assert.deepEqual(JSON.parse(JSON.stringify(requests[0].body)),{question:'What is blocked?'});
  html=run("taskInquiry('task-a')"); assert.match(html,/Reading…/); assert.match(html,/id="task-inquiry-submit"/); assert.match(html,/aria-disabled="true"/); assert.doesNotMatch(html,/type="submit" disabled/);
  requests[0].resolve({answer:'<script>alert(1)</script>',notice:'<b>read-only</b>',evidence:[{title:'<img>',text:'<svg>'}]}); await pending;
  html=run("taskInquiry('task-a')"); assert.match(html,/&lt;script&gt;/); assert.doesNotMatch(html,/<script>|<svg>|<img>/);
  assert.equal(run("taskView('task-a').inquiryPending"),false);
  input.value='A second question'; const second=run('submitTaskInquiry(form)');
  c.location.hash='#runs/task-b'; const before=renders;
  requests[1].resolve({answer:'Task A private result'}); await second;
  assert.equal(renders,before,'late result must not rerender another task');
  assert.doesNotMatch(run("taskInquiry('task-b')"),/Task A private result|A second question/);
  c.location.hash='#runs/task-a'; const third=run('submitTaskInquiry(form)');
  requests[2].reject(Object.assign(new Error('Access revoked'),{status:403})); await third;
  html=run("taskInquiry('task-a')"); assert.match(html,/Access revoked/); assert.doesNotMatch(html,/Task A private result/);
  input.value='   '; await run('submitTaskInquiry(form)'); assert.equal(requests.length,3);
  c.form.isConnected=false; input.value='stale'; await run('submitTaskInquiry(form)'); assert.equal(requests.length,3);
  const app = fs.readFileSync(require('node:path').join(__dirname,'../web/app.js'),'utf8');
  assert.match(app,/form.id === 'task-inquiry-form'.*submitTaskInquiry\(form\)/);
  console.log('Task inquiry: visible composer, read-only payload, duplicate guard, escaped evidence, task isolation, revocation, empty and detached input checks pass.');
})().catch(e=>{console.error(e);process.exitCode=1;});
