/* Keyboard focus across redraws, with queued DOM and API doubles.
 * Measured in Chromium before the fix: the inbox is redrawn every five
 * seconds and after every action, and a Review button a keyboard user had
 * reached was dropped to <body> each time; Escape during a slow hand-on
 * and the dialog reopened by itself when the response landed.
 * This exercises the shipped app.js, not a browser. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');

function harness() {
  const queue = [], requests = [], elements = new Map(), focused = [];
  const eventTarget = () => {
    const handlers = new Map();
    return {
      addEventListener(type, handler) { if (!handlers.has(type)) handlers.set(type, []); handlers.get(type).push(handler); },
      dispatch(type, event = {}) {
        event.target ??= this;
        event.preventDefault ??= () => { event.defaultPrevented = true; };
        const results = (handlers.get(type) || []).map(fn => fn(event));
        if (type === 'cancel' && !event.defaultPrevented) this.close();
        return Promise.all(results);
      },
    };
  };
  const element = (name, extra = {}) => ({
    ...eventTarget(), name, dataset: {}, innerHTML: '', textContent: '', open: false, hidden: false, tagName: 'DIV',
    children: [], found: new Map(),
    showModal() { this.open = true; },
    close() { if (!this.open) return; this.open = false; queue.push(() => this.dispatch('close')); },
    focus() { focused.push(this.name); },
    contains(el) { return this.children.includes(el); },
    querySelector(selector) { return this.found.get(selector) || null; },
    querySelectorAll: () => [], getBoundingClientRect: () => ({left: 10, right: 100, top: 10, bottom: 100}),
    classList: {add() {}, remove() {}, toggle() {}}, setAttribute() {}, ...extra,
  });
  const get = selector => { if (!elements.has(selector)) elements.set(selector, element(selector)); return elements.get(selector); };
  const body = element('body');
  const document = {...eventTarget(), querySelector: get, querySelectorAll: () => [], body,
    getElementById: id => get('#' + id), activeElement: body,
    createElement: type => get('created-' + type), documentElement: {lang: 'en'}};
  const context = {
    console, document, URLSearchParams, location: {hash: '#inbox', pathname: '/', search: ''},
    history: {replaceState() {}}, taskId: () => '', taskTab: '', taskDetail: null, taskError: '',
    setTimeout: () => 1, clearTimeout() {}, setInterval() {},
    fetch(url) {
      if (url === '/api/state') return new Promise(() => {});
      return new Promise(resolve => requests.push({url, resolve: row => resolve({ok: true, json: async () => row})}));
    },
    ...eventTarget(),
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/presentation.js'), 'utf8'), context);
  vm.runInContext(source, context);
  const run = code => vm.runInContext(code, context);
  const click = (action, id) => document.dispatch('click', {target: {closest: selector => selector === '[data-action]' ? {dataset: {action, id}} : null}});
  const flushClose = async () => { while (queue.length) await queue.shift()(); };
  // A workspace with sign-in off, loaded, on the inbox.
  run("state.auth = {enabled: false}; state.decisions = []; state.runs = []; state.owners = []; state.events = []; refresh = async () => {};");
  return {run, click, requests, focused, element, get, document, flushClose, modal: get('#modal')};
}

(async () => {
  const failures = [];
  let passed = 0;
  const test = async (name, body) => {
    let timer;
    try {
      await Promise.race([body(), new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('unresolved fixture request')), 2000); })]);
      passed++;
    } catch (error) { failures.push(name + ': ' + error.message); }
    finally { clearTimeout(timer); }
  };

  await test('focusKey names a control by id, else by its action and target', () => {
    const h = harness();
    const key = el => h.run('focusKey')(el);
    assert.equal(key(h.element('x', {id: 'inbox-search'})), '[id="inbox-search"]');
    assert.equal(key(h.element('x', {tagName: 'BUTTON', dataset: {action: 'review', id: 'dec"1'}})), 'button[data-action="review"][data-id="dec\\"1"]');
    assert.equal(key(h.element('x', {tagName: 'BUTTON', dataset: {action: 'tab', tab: 'all'}})), 'button[data-action="tab"][data-tab="all"]');
    assert.equal(key(h.element('x', {tagName: 'SPAN'})), '', 'a plain element has no key');
    assert.equal(key(h.document.body), '');
    // The page head's keyless controls: a More summary, and a link by where it goes (a menu item, a task row).
    assert.equal(key(h.element('x', {tagName: 'SUMMARY', closest: s => s === 'details.more-menu' ? {} : null})), 'details.more-menu > summary');
    assert.equal(key(h.element('x', {tagName: 'SUMMARY', closest: () => null})), '', 'a summary outside a More menu is not one');
    assert.equal(key(h.element('x', {tagName: 'A', getAttribute: name => name === 'href' ? '#runs/r"1' : null})), 'a[href="#runs/r\\"1"]');
    assert.equal(key(h.element('x', {tagName: 'A', getAttribute: () => null})), '');
  });

  await test('render keeps focus on a page-head control (More, a menu item) and falls back to main when it is gone', () => {
    const h = harness(), actions = h.get('#page-actions');
    const summary = h.element('old summary', {tagName: 'SUMMARY', closest: s => s === 'details.more-menu' ? {} : null});
    const newSummary = h.element('new summary');
    actions.children.push(summary);
    actions.found.set('details.more-menu > summary', newSummary);
    h.document.activeElement = summary;
    h.run('render()');
    assert.deepEqual(h.focused, ['new summary']);
    const item = h.element('old item', {tagName: 'A', getAttribute: name => name === 'href' ? '#today' : null});
    const newItem = h.element('new item');
    actions.children.push(item);
    actions.found.set('a[href="#today"]', newItem);
    h.document.activeElement = item;
    h.run('render()');
    assert.deepEqual(h.focused, ['new summary', 'new item']);
    actions.found.delete('a[href="#today"]');
    h.document.activeElement = item;
    h.run('render()');
    assert.deepEqual(h.focused, ['new summary', 'new item', '#main'], 'a page-head control that is gone sends focus to main, not to an inbox tab');
  });

  await test('the More menu closes on a press outside it and after one of its items is used', async () => {
    const h = harness(), menu = h.get('details.more-menu[open]');
    const inside = h.element('item');
    menu.children.push(inside); menu.open = true;
    await h.document.dispatch('pointerdown', {target: inside});
    assert.equal(menu.open, true, 'a press inside the menu leaves it open');
    await h.document.dispatch('pointerdown', {target: h.element('page text')});
    assert.equal(menu.open, false, 'a press on plain page text closes it, with no focus change needed');
    menu.open = true;
    const button = {dataset: {action: 'close'}, closest: s => s === 'details.more-menu' ? menu : null};
    await h.document.dispatch('click', {target: {closest: s => s === '[data-action]' ? button : null}});
    assert.equal(menu.open, false, 'an item used closes the menu');
    menu.open = true;
    const link = {closest: s => s === 'details.more-menu' ? menu : null, getAttribute: () => '#today'};
    await h.document.dispatch('click', {target: {closest: s => s === 'a[href^="#"]' ? link : null}});
    assert.equal(menu.open, false, 'a menu item that is a link closes it too');
  });

  await test('render keeps focus on the Review button a keyboard user had reached', () => {
    const h = harness(), app = h.get('#app');
    const before = h.element('old review', {tagName: 'BUTTON', dataset: {action: 'review', id: 'd1'}});
    const after = h.element('new review', {tagName: 'BUTTON', dataset: {action: 'review', id: 'd1'}});
    app.children.push(before);
    app.found.set('button[data-action="review"][data-id="d1"]', after);
    h.document.activeElement = before;
    h.run('render()');
    assert.deepEqual(h.focused, ['new review']);
  });

  await test('render moves focus to the active tab when the control is gone, and to main without one', () => {
    const h = harness(), app = h.get('#app');
    const before = h.element('answered review', {tagName: 'BUTTON', dataset: {action: 'review', id: 'd1'}});
    app.children.push(before);
    h.document.activeElement = before;
    const tab = h.element('active tab');
    h.get('#app .toolbar .tab.active'); h.document.querySelector = s => s === '#app .toolbar .tab.active' ? tab : h.get(s);
    h.run('render()');
    assert.deepEqual(h.focused, ['active tab']);
    h.document.querySelector = s => s === '#app .toolbar .tab.active' ? null : h.get(s);
    h.document.activeElement = before;
    h.run('render()');
    assert.deepEqual(h.focused, ['active tab', '#main']);
  });

  await test('render leaves focus alone outside the redrawn region', () => {
    const h = harness();
    h.document.activeElement = h.element('sidebar link', {tagName: 'A', id: 'elsewhere'});
    h.run('render()');
    assert.deepEqual(h.focused, []);
  });

  await test('an open dialog redrawn in place keeps focus on a surviving control, else starts at its title', () => {
    const h = harness(), modal = h.get('#modal');
    h.run("openModal('First', '', '<button data-action=\"assign\">Assign</button>')");
    assert.equal(modal.open, true);
    assert.deepEqual(h.focused, [], 'showModal places focus itself');
    assert.match(h.get('#modal-content').innerHTML, /<h2 id="modal-title" tabindex="-1">/);
    const assign = h.element('assign', {tagName: 'BUTTON', dataset: {action: 'assign', id: 'd1'}});
    const assignAgain = h.element('assign again');
    modal.children.push(assign);
    modal.found.set('button[data-action="assign"][data-id="d1"]', assignAgain);
    h.document.activeElement = assign;
    h.run("openModal('Second', '', '')");
    assert.deepEqual(h.focused, ['assign again']);
    const signoff = h.element('signoff', {tagName: 'BUTTON', dataset: {action: 'signoff', id: 'd1'}});
    modal.children.push(signoff);
    h.document.activeElement = signoff;
    h.run("openModal('Third', '', '')");
    assert.deepEqual(h.focused, ['assign again', '#modal-title']);
  });

  await test('a sign-off response that lands after Escape does not reopen the dialog', async () => {
    const h = harness();
    h.run("openModal('Decision', '', '')");
    const pending = h.click('signoff', 'd1');
    assert.equal(h.requests.length, 1);
    assert.match(h.requests[0].url, /\/api\/decisions\/d1\/signoff$/);
    await h.modal.dispatch('cancel');
    assert.equal(h.modal.open, false);
    h.requests[0].resolve({notice: 'Signed off.'});
    await pending;
    await h.flushClose();
    assert.equal(h.requests.length, 1, 'no read of the decision after the dialog was closed');
    assert.equal(h.modal.open, false);
    assert.equal(h.get('#toast').textContent, 'Signed off.');
  });

  await test('a sign-off response with the dialog still open redraws the decision', async () => {
    const h = harness();
    h.run("openModal('Decision', '', '')");
    const pending = h.click('signoff', 'd1');
    h.requests[0].resolve({notice: 'Signed off.'});
    await new Promise(r => setImmediate(r));
    assert.equal(h.requests.length, 2);
    assert.match(h.requests[1].url, /\/api\/decisions\/d1$/);
    h.requests[1].resolve({id: 'd1', question: 'Signed question', agent: 'Agent', run_title: 'Task', owner_name: 'Owner', status: 'resolved', signoff: 'signed', allowed_actions: [], events: []});
    await pending;
    assert.equal(h.modal.open, true);
    assert.match(h.get('#modal-content').innerHTML, /Signed question/);
  });

  await test('a route change and a search are announced through one small status element, not by re-reading the page', async () => {
    const h = harness(), live = h.get('#announce');
    assert.equal(live.textContent, 'Inbox', 'the landing page is announced once');
    h.run("location.hash = '#owners'; navigate()");
    assert.equal(live.textContent, 'People');
    h.run("location.hash = '#inbox'; navigate(); state.decisions = [{id:'a', status:'pending', question:'Tokens', context:'', owner_name:'Wes', agent:'Demo', repo:'r', created_at:new Date().toISOString(), run_title:'t', run_id:'t1', path:'p'}, {id:'b', status:'pending', question:'Other', context:'', owner_name:'Wes', agent:'Demo', repo:'r', created_at:new Date().toISOString(), run_title:'t', run_id:'t1', path:'p'}]");
    await h.document.dispatch('input', {target: {id: 'inbox-search', value: 'tok'}});
    assert.equal(live.textContent, '1 decision match');
    await h.document.dispatch('input', {target: {id: 'inbox-search', value: 'zzz'}});
    assert.equal(live.textContent, '0 decisions match');
    await h.document.dispatch('input', {target: {id: 'inbox-search', value: ''}});
    assert.equal(live.textContent, '', 'clearing the search says nothing; the list is back');
  });

  await test('the hand-on checkbox switches assign and refer, except on an admin override row', async () => {
    const h = harness(), button = h.get('#hand-on'), row = h.get('#refer-scope-row');
    button.dataset = {action: 'refer', id: 'd1'};
    await h.document.dispatch('change', {target: {id: 'refer-learn', checked: false}});
    assert.equal(button.dataset.action, 'assign');
    assert.equal(row.hidden, true);
    await h.document.dispatch('change', {target: {id: 'refer-learn', checked: true}});
    assert.equal(button.dataset.action, 'refer');
    button.dataset = {action: 'refer', id: 'd1', override: '1'};
    await h.document.dispatch('change', {target: {id: 'refer-learn', checked: false}});
    assert.equal(button.dataset.action, 'refer', 'an override is recorded as a refer; /assign would be refused');
  });

  await test('every action that redraws the decision goes through the guarded path', () => {
    assert.equal((source.match(/await refresh\(\); await review\(target\.dataset\.id\)/g) || []).length, 0);
    assert.equal((source.match(/await rereview\(target\.dataset\.id, version\)/g) || []).length, 6);
  });

  await test('an empty inbox tab says what it holds, and the tabs are Needs me, All open, Decided', () => {
    const h = harness();
    h.run("state.decisions = [{id:'a', status:'pending', question:'Q', context:'', owner_name:'Wes', agent:'Demo', repo:'r', created_at:new Date().toISOString(), run_title:'t', run_id:'t1', path:'p'}]; state.auth = {enabled:true}; state.me = {name:'Ada', role:'member', id:'p1'}");
    const text = (tabName) => { h.run(`tab = '${tabName}'`); return h.run('inbox()'); };
    const pending = text('pending');
    for (const label of ['Needs me', 'All open', 'Decided']) assert.match(pending, new RegExp(`data-tab="[a-z]+" aria-pressed="(true|false)">${label}<span>`));
    assert.doesNotMatch(pending, /Overdue|Suggested|From evidence|All decisions|Needs review|Assigned to me/);
    assert.match(pending, /Waiting on Wes/);
    // Headings in order under the page's h1: the question is the card's h2; the button names it for a screen reader.
    assert.match(pending, /<h2>Q<\/h2>/);
    assert.doesNotMatch(pending, /<h3>|<h4>/);
    assert.match(pending, /data-action="review" data-id="a" aria-label="Answer: Q">Answer<\/button>/);
    // The bottom line is the avatar and one pill that names the owner; the name is not written beside it.
    assert.match(pending, /<div class="card-bottom"><span class="owner" title="Wes"><span class="avatar">W<\/span><\/span><span class="pill waiting">/);
    assert.doesNotMatch(pending, /<\/span>Wes<span class="pill/);
    assert.doesNotMatch(pending, /id="owner-filter"/, 'the owner filter is for the Decided tab');
    assert.match(text('mine'), /Nothing waits on you\./);
    assert.doesNotMatch(text('mine'), /all caught up|elsewhere in the workspace/);
    assert.match(text('decided'), /No decisions recorded yet\./);
    assert.match(text('decided'), /id="owner-filter"/);
    h.run("state.decisions[0].owner_name = ''");
    assert.equal((text('pending').match(/No owner yet/g) || []).length, 2, 'the avatar title and the pill; no third mention');
    assert.match(text('pending'), /title="No owner yet"><span class="avatar empty" aria-hidden="true"><\/span><\/span><span class="pill waiting">(?:<span class="status-dot"><\/span>)?No owner yet</, 'no name: an empty circle, not a question mark');
    h.run("state.decisions[0].owner_name = 'Wes'; query = 'zzz'");
    assert.match(text('mine'), /No matching decisions\./);
    assert.match(text('mine'), /Try a different search\./);
    h.run("query = ''; state.decisions = [{id:'b', status:'approved', signoff:'signed', signed_by:'Wes', question:'Q', context:'', answer:'A', owner_name:'Wes', agent:'Demo', repo:'r', created_at:new Date().toISOString(), updated_at:'u', run_title:'t', run_id:'t1', path:'p'}]");
    assert.match(text('pending'), /Nothing needs a person\./);
    assert.match(text('pending'), /When an agent needs a person, it appears here\./);
    assert.doesNotMatch(text('pending'), /Open Settings/, 'a workspace with decisions is set up');
    assert.match(text('decided'), /Signed by Wes/);
    h.run("state.decisions = []");
    assert.match(text('pending'), /data-action="help-connect">Open Settings</);
    // An old tab key (a bookmark, a stale redraw) falls back to the default tab instead of an empty list:
    // a member's own tab, the workspace's open tab for everyone else.
    assert.match(text('overdue'), /data-tab="mine" aria-pressed="true"/);
    h.run("state.me = {name:'Ada', role:'admin', id:'p1'}");
    assert.match(text('overdue'), /data-tab="pending" aria-pressed="true"/);
  });

  if (failures.length) throw new Error(`${failures.length} failed; ${passed} passed:\n${failures.join('\n')}`);
  console.log(`Focus return: ${passed} checks passed (DOM/API doubles, not Chromium).`);
})().catch(error => { console.error(error); process.exitCode = 1; });
