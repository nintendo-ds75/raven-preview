/* What a person feels between a click and the next frame, with DOM and API
 * doubles: an action says what it is doing before its request leaves; a poll
 * that finds nothing new does not rebuild the page; a tab shown again catches
 * up at once; a model reading is capped instead of waiting on one line of
 * text. Measured on the prototype (ScyllaDB pilot): the inbox was rebuilt
 * every five seconds whether or not anything had changed, and an answer,
 * sign-off or hand-on only dimmed its button. This exercises the shipped
 * app.js, not a browser. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');

async function harness() {
  const elements = new Map(), requests = [], stateRequests = [], timers = [];
  let draws = 0, stateResponse = null;
  const eventTarget = () => {
    const handlers = new Map();
    return {
      addEventListener(type, handler) { if (!handlers.has(type)) handlers.set(type, []); handlers.get(type).push(handler); },
      removeAttribute(name) { delete this.attributes[name]; },
      dispatch(type, event = {}) {
        event.target ??= this;
        event.preventDefault ??= () => { event.defaultPrevented = true; };
        return Promise.all((handlers.get(type) || []).map(fn => fn(event)));
      },
    };
  };
  const element = (name, extra = {}) => ({
    ...eventTarget(), name, dataset: {}, attributes: {}, innerHTML: '', textContent: '', open: false, hidden: false, tagName: 'DIV',
    children: [], found: new Map(), isConnected: true,
    showModal() { this.open = true; }, close() { this.open = false; }, focus() {},
    contains(el) { return this.children.includes(el); },
    querySelector(selector) { return this.found.get(selector) || null; },
    querySelectorAll: () => [], getBoundingClientRect: () => ({left: 10, right: 100, top: 10, bottom: 100}),
    classList: {add() {}, remove() {}, toggle() {}}, setAttribute(name, value) { this.attributes[name] = value; },
    getAttribute(name) { return this.attributes[name] ?? null; }, reportValidity: () => true, ...extra,
  });
  const get = selector => { if (!elements.has(selector)) elements.set(selector, element(selector)); return elements.get(selector); };
  const app = get('#app');
  let html = '';
  Object.defineProperty(app, 'innerHTML', {get: () => html, set: value => { html = value; draws += 1; }});
  const body = element('body', {tagName: 'BODY'});
  const document = {...eventTarget(), querySelector: get, querySelectorAll: () => [], body, hidden: false,
    getElementById: id => get('#' + id), activeElement: body,
    createElement: type => get('created-' + type), documentElement: {lang: 'en'}};
  const context = {
    console, document, URLSearchParams, AbortController, location: {hash: '#inbox', pathname: '/', search: ''},
    history: {replaceState() {}}, taskId: () => '', taskTab: '', taskDetail: null, taskError: '',
    FormData: class { constructor(form) { return Object.entries(form.values || {}); } },
    setTimeout: (fn, ms) => { timers.push({fn, ms}); return timers.length; }, clearTimeout() {}, setInterval() {},
    fetch(url, init = {}) {
      if (url === '/api/state') {
        stateRequests.push(init);
        if (!stateResponse) return Promise.reject(new Error('no workspace yet'));
        const {status, etag, body} = stateResponse;
        return Promise.resolve({status, ok: status === 200, headers: {get: name => name === 'ETag' ? etag : null}, json: async () => body});
      }
      return new Promise((resolve, reject) => {
        if (init.signal) init.signal.addEventListener('abort', () => reject(Object.assign(new Error('The user aborted a request.'), {name: 'AbortError'})));
        requests.push({url, init, resolve: row => resolve({ok: true, json: async () => row}),
          fail: message => resolve({ok: false, status: 400, json: async () => ({error: message})})});
      });
    },
    ...eventTarget(),
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/presentation.js'), 'utf8'), context);
  vm.runInContext(source, context);
  // The app's own first load has failed and drawn its error by now; what follows is the test's.
  await new Promise(resolve => setImmediate(resolve));
  const base = draws;
  stateRequests.length = 0;
  const run = code => vm.runInContext(code, context);
  const workspace = (decisions = []) => ({auth: {enabled: false}, me: null, decisions, runs: [], owners: [], events: [], csrf_token: 't'});
  const click = target => document.dispatch('click', {target: {closest: selector => selector === '[data-action]' ? target : null}});
  return {run, element, get, document, requests, stateRequests, timers, click, workspace,
    draws: () => draws - base, respond(status, etag, body) { stateResponse = {status, etag, body}; }};
}

(async () => {
  const failures = [];
  let passed = 0;
  const test = async (name, fn) => {
    let timer;
    try {
      await Promise.race([fn(), new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('unresolved fixture request')), 2000); })]);
      passed++;
    } catch (error) { failures.push(name + ': ' + error.message); }
    finally { clearTimeout(timer); }
  };

  await test('a sign-off says it is signing before its request leaves, and comes back on failure', async () => {
    const h = await harness();
    h.run("state = Object.assign(state, {auth: {enabled: false}, decisions: [{id: 'd1', owner_name: 'Wes Chen', updated_at: 'u1'}]})");
    const button = h.element('signoff', {tagName: 'BUTTON', dataset: {action: 'signoff', id: 'd1', updated: 'u1'}, innerHTML: 'Sign as Wes Chen <svg></svg>', textContent: 'Sign as Wes Chen'});
    const pending = h.click(button);
    assert.equal(h.requests.length, 1, 'the request left');
    assert.equal(button.disabled, true);
    assert.equal(button.textContent, 'Signing…');
    assert.equal(button.getAttribute('aria-busy'), 'true');
    h.requests[0].fail('The decision changed under you.');
    await pending;
    assert.equal(button.disabled, false, 'usable again after the failure');
    assert.equal(button.innerHTML, 'Sign as Wes Chen <svg></svg>', 'its label is back, icon and all');
    assert.equal(button.getAttribute('aria-busy'), null);
    assert.match(h.get('#toast').textContent, /changed under you/);
  });

  for (const [action, label] of [['refer', 'Handing on…'], ['assign', 'Assigning…'], ['followups', 'Adding…']]) {
    await test(`${action} shows progress within the click`, async () => {
      const h = await harness();
      h.run("state = Object.assign(state, {auth: {enabled: false}, owners: [{id: 'o1', name: 'Marisol Vega'}], decisions: [{id: 'd1'}]})");
      h.get('#assign-owner').value = 'o1';
      h.get('#followups').value = 'Should credits expire?';
      const button = h.element(action, {tagName: 'BUTTON', dataset: {action, id: 'd1', updated: 'u1'}, textContent: 'Hand on', innerHTML: 'Hand on'});
      const pending = h.click(button);
      assert.equal(button.textContent, label);
      assert.equal(button.disabled, true);
      h.requests[0].fail('no'); await pending;
      assert.equal(button.innerHTML, 'Hand on');
      assert.equal(button.disabled, false);
    });
  }

  await test('recording an answer says so before the request leaves', async () => {
    const h = await harness();
    h.run("state = Object.assign(state, {auth: {enabled: false}, decisions: [{id: 'd1', owner_name: 'Wes Chen'}]})");
    const button = h.element('submit', {tagName: 'BUTTON', textContent: 'Record my answer', innerHTML: 'Record my answer'});
    const errorEl = h.element('error', {hidden: true});
    const form = h.element('form', {id: 'answer-form', dataset: {id: 'd1'}, values: {answer: 'Yes', rationale: 'Because', expected_updated_at: 'u1'}});
    form.found.set('[type="submit"]', button); form.found.set('#form-error', errorEl);
    const pending = h.document.dispatch('submit', {target: form});
    assert.equal(h.requests.length, 1);
    assert.equal(h.requests[0].url, '/api/decisions/d1/answer');
    assert.equal(button.textContent, 'Recording…');
    assert.equal(button.disabled, true);
    h.requests[0].fail('Stale form.'); await pending;
    assert.equal(button.innerHTML, 'Record my answer');
    assert.equal(button.disabled, false);
    assert.equal(errorEl.textContent, 'Stale form.');
  });

  await test('a poll that finds nothing new does not redraw the page', async () => {
    const h = await harness();
    h.respond(200, 'W/"one"', h.workspace([{id: 'd1', status: 'pending', question: 'Q', context: '', owner_name: 'Wes', created_at: '2026-01-01T00:00:00Z'}]));
    await h.run('refresh()');
    assert.equal(h.draws(), 1, 'the first load draws');
    assert.equal(h.stateRequests[0].headers['If-None-Match'], undefined, 'nothing to revalidate yet');
    h.respond(304, 'W/"one"', null);
    await h.run('refresh({quiet: true})');
    assert.equal(h.stateRequests[1].headers['If-None-Match'], 'W/"one"', 'the poll names the state it has');
    assert.equal(h.draws(), 1, 'an unchanged workspace is not redrawn');
    assert.equal(h.run('state.decisions.length'), 1, 'the state on screen is kept');
    h.respond(200, 'W/"two"', h.workspace([]));
    await h.run('refresh({quiet: true})');
    assert.equal(h.draws(), 2, 'a change redraws');
    await h.run('refresh({quiet: true})');
    assert.equal(h.stateRequests[3].headers['If-None-Match'], 'W/"two"', 'the new tag is the one sent back');
    assert.equal(h.draws(), 3, 'the tag changed, so the body came and was drawn');
  });

  await test('an unchanged workspace is still redrawn once a minute so relative times move', async () => {
    const h = await harness();
    h.respond(200, 'W/"one"', h.workspace());
    await h.run('refresh()');
    h.respond(304, 'W/"one"', null);
    await h.run('refresh({quiet: true})');
    assert.equal(h.draws(), 1);
    h.run('const realNow = Date.now; Date.now = () => realNow() + 61000;');
    await h.run('refresh({quiet: true})');
    assert.equal(h.draws(), 2);
  });

  await test('a redraw asked for by the person is never skipped', async () => {
    const h = await harness();
    h.respond(200, 'W/"one"', h.workspace());
    await h.run('refresh()');
    h.respond(304, 'W/"one"', null);
    await h.run('refresh()');
    assert.equal(h.draws(), 2, 'Try again, a closed dialog and the like redraw even when nothing changed');
  });

  await test('a tab shown again refreshes at once', async () => {
    const h = await harness();
    h.respond(200, 'W/"one"', h.workspace());
    await h.run('refresh()');
    const before = h.stateRequests.length;
    h.document.hidden = true;
    await h.document.dispatch('visibilitychange');
    assert.equal(h.stateRequests.length, before, 'hiding the tab asks for nothing');
    h.document.hidden = false;
    await h.document.dispatch('visibilitychange');
    assert.equal(h.stateRequests.length, before + 1);
  });

  await test('a model reading in the decision dialog is capped at 45 seconds', async () => {
    const h = await harness();
    h.run("state = Object.assign(state, {auth: {enabled: false}})");
    const button = h.element('ask', {tagName: 'BUTTON', ariaDisabled: 'false'});
    const output = h.element('result');
    const form = h.element('form', {dataset: {decisionInquiry: 'd1', task: 't1'}});
    form.found.set('button', button); form.found.set('.decision-inquiry-result', output); form.found.set('textarea', {value: 'What is open?'});
    const pending = h.document.dispatch('submit', {target: form});
    assert.equal(output.textContent, 'Reading task context…');
    assert.equal(h.requests[0].url, '/api/tasks/t1/inquiry');
    const cap = h.timers.find(t => t.ms === 45000);
    assert(cap, 'a 45 second cap is armed with the request');
    cap.fn();
    await pending;
    assert.match(output.textContent, /No reading came back within 45 seconds/);
    assert.equal(button.ariaDisabled, 'false', 'the person can ask again');
  });

  console.log(`${passed} passed, ${failures.length} failed`);
  for (const failure of failures) console.error('FAIL ' + failure);
  process.exit(failures.length ? 1 : 0);
})();
