/* Full review-dialog regression, using DOM/API doubles without Chromium. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');

function harness() {
  const requests = [], elements = new Map();
  const element = () => ({dataset: {}, innerHTML: '', textContent: '', open: false, hidden: false, children: [],
    showModal() { this.open = true; }, close() { this.open = false; }, focus() {}, contains: () => false,
    querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, setAttribute() {},
    classList: {add() {}, remove() {}, toggle() {}}});
  const get = selector => { if (!elements.has(selector)) elements.set(selector, element()); return elements.get(selector); };
  const body = element();
  const context = {
    console, URLSearchParams, location: {hash: '#inbox', pathname: '/', search: ''}, history: {replaceState() {}},
    document: {querySelector: get, querySelectorAll: () => [], getElementById: id => get('#' + id), body, activeElement: body,
      addEventListener() {}, createElement: () => element(), documentElement: {lang: 'en'}},
    taskId: () => '', taskTab: '', taskDetail: null, taskError: '', setTimeout: () => 1, clearTimeout() {}, setInterval() {},
    fetch: url => url === '/api/state' ? new Promise(() => {}) : new Promise(resolve => requests.push({url, resolve: row => resolve({ok: true, json: async () => row})})),
    addEventListener() {},
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/presentation.js'), 'utf8'), context);
  vm.runInContext(source, context);
  return {run: code => vm.runInContext(code, context), requests, content: get('#modal-content'), toast: get('#toast')};
}

// Exercise the complete shipped review(), not only the individual helpers:
// a partial decision clone formerly rendered fresh snapshots and work-item
// associations a second time inside the recorded background.
const pin = {record_id:'policy-record', source_version_id:'policy-v1', role:'support'};
const decision = {id:'d1', run_id:'t1', question:'May we retain the export?', agent:'Fixture',
  run_title:'Review retention', owner_name:'Owner', owner_id:'o1', events:[], updated_at:'u1',
  brief:'Original fixture brief', context:'Original fixture context', evidence:'Unique evidence description',
  answer:'Retain for seven days.', source_revalidation:{available:true, has_reliance:true,
    sources:[{...pin, snapshot:{body:'Exact policy snapshot', title:'Retention policy'}}], pins:[pin]},
  sources:[{...pin, ref:'POLICY-1', sequence:1}],
  work_item_association:{declared:'CASE-1',status:'linked',links:[{record_id:'case-record',
    source_version_id:'case-v1',ref:'CASE-1',role:'work_item',origin:'task',current:true}]}};

(async () => {
  for (const mode of ['pending','unsigned','settled','read-only']) {
    const h = harness();
    h.run("state.auth = {enabled:false}; state.owners = []; state.decisions = [];");
    const d = {...decision, status:mode === 'pending' ? 'pending' : 'resolved',
      signoff:mode === 'settled' ? 'signed' : 'required',
      allowed_actions:mode === 'read-only' ? ['note'] : ['answer','assign']};
    const before = JSON.stringify(d);
    const render = async () => {
      const pending = h.run("review('d1')"); h.requests.shift().resolve(d); await pending;
      assert.equal(h.toast.textContent, '', `${mode}: rendering must not throw`);
      const html = h.content.innerHTML;
      assert.equal((html.match(/class="source-evidence-review"/g) || []).length, 1, `${mode}: one evidence review`);
      assert.equal((html.match(/class="history source-snapshot"/g) || []).length, 1, `${mode}: one exact source snapshot`);
      assert.equal((html.match(/class="history work-item-context"/g) || []).length, 1, `${mode}: one work-item context`);
      assert.equal((html.match(/Unique evidence description/g) || []).length, 1, `${mode}: one evidence description`);
      for (const value of ['Original fixture brief','Original fixture context','Exact policy snapshot','policy-v1','case-record','case-v1']) {
        assert(html.includes(value), `${mode}: retain ${value}`);
      }
      const background = html.indexOf('class="history decision-background"');
      const evidence = html.indexOf('class="source-evidence-review"');
      const association = html.indexOf('class="history work-item-context"');
      if (['pending','unsigned'].includes(mode)) {
        assert(evidence < background && association < background, `${mode}: review evidence stays upfront`);
      } else {
        assert(evidence > background && association > background, `${mode}: retain evidence in recorded background`);
      }
      assert.equal(JSON.stringify(d), before, `${mode}: rendering does not mutate the decision`);
      return html;
    };
    const first = await render();
    h.run('closeModal()');
    assert.equal(await render(), first, `${mode}: close/reopen does not accumulate duplicate content`);
  }
  console.log('Decision evidence: exactly one snapshot and work-item context in pending, unsigned, settled and read-only reviews; close/reopen and record preservation pass.');
})().catch(error => { console.error(error); process.exitCode = 1; });
