/* The read-only decision dialog says what would let the signed-in person
 * act. Measured in a shared workspace: a member whose name matched the
 * owner's saw "This decision belongs to another owner. Alex Morgan owns
 * this decision." under their own name, and nothing about the authority
 * map, which is what lets a person answer a decision in another's scope.
 * Exercises the shipped review() with queued DOM/API doubles. */
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
  return {run: code => vm.runInContext(code, context), requests, content: get('#modal-content')};
}

const decision = {id: 'd1', run_id: 't1', question: 'Can we shorten service token expiry to 24 hours?', agent: 'Demo', run_title: 'Tighten service token access',
  owner_name: 'Alex Morgan', owner_id: 'o1', status: 'pending', allowed_actions: ['followup', 'note'], events: []};

async function open(role) {
  const h = harness();
  h.run(`state.auth = {enabled: true}; state.me = {id: 'p2', name: 'Alex Morgan', role: '${role}', kind: 'session'}; state.decisions = []; state.owners = [];`);
  const pending = h.run("review('d1')");
  h.requests[0].resolve({...decision});
  await pending;
  return h.content.innerHTML;
}

(async () => {
  const member = await open('member');
  assert.match(member, /Alex Morgan owns this decision; you can add follow-up questions here and context on the task\./);
  assert.match(member, /To answer or sign it yourself, ask an administrator to record your authority for its scope under People &amp; ownership\./);
  assert.doesNotMatch(member, /belongs to another owner/);
  assert.doesNotMatch(member, /id="answer-form"|data-action="signoff"/, 'still read-only');
  const admin = await open('admin');
  assert.match(admin, /To answer or sign it yourself, record your authority for its scope under People &amp; ownership\./);
  const viewer = await open('viewer');
  assert.match(viewer, /Read-only access\. Alex Morgan owns this decision\./);
  assert.doesNotMatch(viewer, /record your authority/);
  const h = harness();
  h.run("state.auth = {enabled: true}; state.me = {id: 'p2', name: 'Pat', role: 'member', kind: 'session'}; state.decisions = []; state.owners = [];");
  const pending = h.run("review('d1')");
  h.requests[0].resolve({...decision, owner_name: '', owner_id: ''});
  await pending;
  assert.match(h.content.innerHTML, /This decision has no owner yet; you can add follow-up questions/);
  console.log('Read-only decision copy: member, admin, viewer and unassigned wording pass.');
})().catch(error => { console.error(error); process.exitCode = 1; });
