/* The read-only decision dialog says who decides and what the signed-in
 * person can still do, in one sentence. Measured in a shared workspace: a
 * member whose name matched the owner's saw "This decision belongs to
 * another owner. Alex Morgan owns this decision." under their own name.
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
  assert.match(member, /Alex Morgan decides this\. You can read it and add notes\./);
  assert.doesNotMatch(member, /belongs to another owner|owns this decision|record your authority/);
  assert.doesNotMatch(member, /id="answer-form"|data-action="signoff"|id="hand-on"/, 'still read-only');
  assert.match(member, /Waiting on Alex Morgan/, 'the status line names the owner');
  const admin = await open('admin');
  assert.match(admin, /Alex Morgan decides this\. You can read it and add notes\./);
  const viewer = await open('viewer');
  assert.match(viewer, /Read-only\. Alex Morgan decides this\./);
  assert.doesNotMatch(viewer, /add notes|record your authority/);
  const h = harness();
  h.run("state.auth = {enabled: true}; state.me = {id: 'p2', name: 'Pat', role: 'member', kind: 'session'}; state.decisions = []; state.owners = [];");
  const pending = h.run("review('d1')");
  h.requests[0].resolve({...decision, owner_name: '', owner_id: ''});
  await pending;
  assert.match(h.content.innerHTML, /No owner yet\. You can read it and add notes\./);
  assert.doesNotMatch(h.content.innerHTML, /Waiting on undefined|Waiting on </);
  // A routed owner who has not joined. Measured on a demo run: the admin
  // read only that they could add a note or a follow-up, with no way on.
  // The admin is told so and offered the invitation and the hand-on (as
  // their recorded override); a member is told who can; nobody gains a
  // signature.
  const unjoined = {...decision, owner_name: 'Dana Okafor', owner_id: 'o9', updated_at: 'u1',
    owner_account: {state: 'none', person_id: 'p9', email: '', name: 'Dana Okafor'}};
  const openAs = async (role, d) => {
    const h = harness();
    h.run(`state.auth = {enabled: true}; state.me = {id: 'p2', name: 'Sam Rivera', role: '${role}', kind: 'session'}; state.decisions = []; state.owners = [{id: 'o9', name: 'Dana Okafor', team: ''}, {id: 'o1', name: 'Alex Morgan', team: 'Billing'}];`);
    const pending = h.run("review('d1')");
    h.requests[0].resolve(d);
    await pending;
    return h.content.innerHTML;
  };
  const adminUnjoined = await openAs('admin', unjoined);
  assert.match(adminUnjoined, /<span class="label">Dana Okafor is not in Raven yet<\/span>Nobody can sign this until they join\./);
  assert.match(adminUnjoined, /data-action="invite-owner" data-person="p9"[^>]*>Invite Dana</);
  // The override hand-on is a refer, so its row starts with the checkbox on; unchecking it assigns instead.
  assert.match(adminUnjoined, /id="hand-on" data-action="refer" data-id="d1" data-updated="u1" data-override="1">Hand on</);
  assert.match(adminUnjoined, /id="refer-learn" checked> They decide this from now on/);
  assert.match(adminUnjoined, /aria-label="Hand on to"/);
  assert.match(adminUnjoined, /Recorded as your admin override\. It signs nothing\./);
  assert.doesNotMatch(adminUnjoined, /id="answer-form"|data-action="signoff"/, 'who may sign is unchanged');
  const memberUnjoined = await openAs('member', unjoined);
  assert.match(memberUnjoined, /Dana Okafor is not in Raven yet/);
  assert.match(memberUnjoined, /An administrator can invite them or hand this on\./);
  assert.doesNotMatch(memberUnjoined, /invite-owner|data-override|id="hand-on"/);
  const invited = await openAs('admin', {...unjoined, owner_account: {...unjoined.owner_account, state: 'invited'}});
  assert.match(invited, /They were invited but have not joined yet\./);
  assert.match(invited, /Invite again/);
  const joined = await openAs('admin', {...unjoined, owner_account: {...unjoined.owner_account, state: 'account'}});
  assert.doesNotMatch(joined, /is not in Raven yet|invite-owner/);
  const h2 = harness();
  assert.match(h2.run("inviteForm([{id: 'p9', name: 'Dana Okafor', active: true, email: '', github_id: ''}], {person_id: 'p9', email: ''})"), /<option value="p9" selected>/);
  assert.match(h2.run("inviteForm([], {email: 'dana@example.test'})"), /value="dana@example.test"/);
  console.log('Read-only decision copy: member, admin, viewer, unassigned and unjoined-owner wording pass.');
})().catch(error => { console.error(error); process.exitCode = 1; });
