/* Dependency-free lifecycle checks with queued native-dialog event/API doubles.
 * This exercises the shipped app, not a real browser or any authority logic. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const interviewSource = fs.readFileSync(path.join(__dirname, '../web/interview.js'), 'utf8');

function harness() {
  const queue = [], requests = [], elements = new Map();
  const eventTarget = () => {
    const handlers = new Map();
    return {
      addEventListener(type, handler) {
        if (!handlers.has(type)) handlers.set(type, []);
        handlers.get(type).push(handler);
      },
      dispatch(type, event = {}) {
        event.target ??= this;
        event.preventDefault ??= () => { event.defaultPrevented = true; };
        const results = (handlers.get(type) || []).map(fn => fn(event));
        if (type === 'cancel' && !event.defaultPrevented) this.close();
        return Promise.all(results);
      },
    };
  };
  const get = selector => {
    if (!elements.has(selector)) elements.set(selector, {
      ...eventTarget(), dataset: {}, innerHTML: '', textContent: '', open: false,
      showModal() { this.open = true; },
      close() {
        if (!this.open) return;
        this.open = false;
        // HTMLDialogElement.close() removes open now and queues close later.
        queue.push(() => this.dispatch('close'));
      },
      getBoundingClientRect: () => ({left: 10, right: 100, top: 10, bottom: 100}),
      classList: {add() {}, remove() {}, toggle() {}},
      querySelector: () => null, setAttribute() {},
    });
    return elements.get(selector);
  };
  const document = {...eventTarget(), querySelector: get, querySelectorAll: () => [],
    getElementById: id => get('#' + id), activeElement: {id: '', tagName: 'BODY'},
    createElement: type => get('created-' + type), body: {append() {}}, documentElement: {lang: 'en'}};
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
  const click = (action, id) => document.dispatch('click', {target: {
    closest: selector => selector === '[data-action]' ? {dataset: {action, id}} : null,
  }});
  const finish = (request, title) => request.resolve({id: title, question: title, agent: 'Agent', run_title: 'Task',
    owner_name: 'Example Owner', status: 'pending', allowed_actions: [], events: []});
  const flushClose = async () => { while (queue.length) await queue.shift()(); };
  const modal = get('#modal');
  const dismiss = mode => mode === 'escape' ? modal.dispatch('cancel') : mode === 'backdrop'
    ? modal.dispatch('click', {target: modal, clientX: 0, clientY: 0}) : click('close');
  const loadInterview = () => {
    vm.runInContext(interviewSource, context);
    // Only the ordinary-modal handoff is under test; do not exercise interview
    // rendering, identity, edits, or confirmation.
    run("state.auth = {enabled: true}; state.me = {id: 'person', kind: 'session'}; interviewBody = () => {};");
  };
  return {run, click, finish, requests, modal, get, queue, dismiss, flushClose, loadInterview};
}

(async () => {
  const failures = [];
  let passed = 0;
  const test = async (name, body) => {
    let timeout;
    try {
      await Promise.race([body(), new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new Error('unresolved fixture request')), 2000);
      })]);
      passed++;
    } catch (error) { failures.push(name + ': ' + error.message); }
    finally { clearTimeout(timeout); }
  };
  for (const mode of ['close', 'cancel', 'escape', 'backdrop']) {
    // Close and Cancel use the same shipped data-action, including the form button.
    await test(mode + ': close then reopen before old close event', async () => {
      const h = harness();
      h.run(mode === 'cancel' ? 'newRequest()' : "openModal('Original', '', '')");
      if (mode === 'cancel') assert.match(h.get('#modal-content').innerHTML, /data-action="close">Cancel/);
      await h.dismiss(mode);
      assert.equal(h.modal.open, false);
      const pending = h.click('review', 'new');
      const version = h.run('modalVersion');
      await h.flushClose();
      h.finish(h.requests[0], 'New review');
      await pending;
      assert.equal(h.modal.open, true, 'a queued prior close must not cancel the new request');
      assert.match(h.get('#modal-content').innerHTML, /New review/);
      assert(h.run('modalVersion') > version);
    });
    await test(mode + ': dismiss invalidates pending read before close event', async () => {
      const h = harness(); h.run("openModal('Original', '', '')");
      const pending = h.click('source', 'old');
      await h.dismiss(mode);
      h.finish(h.requests[0], 'Old review');
      await pending;
      assert.equal(h.modal.open, false, 'late old response must not reopen a dismissed dialog');
      assert.doesNotMatch(h.get('#modal-content').innerHTML, /Old review/);
      await h.flushClose();
    });
    await test(mode + ': prior close event cannot invalidate rendered reopen', async () => {
      const h = harness(); h.run("openModal('Original', '', '')"); await h.dismiss(mode);
      const pending = h.click('review', 'new'); h.finish(h.requests[0], 'New review'); await pending;
      const version = h.run('modalVersion');
      await h.flushClose();
      assert.equal(h.run('modalVersion'), version, 'prior close event belongs to the dismissed generation');
      assert.equal(h.modal.open, true);
    });
  }
  await test('newer review wins when responses arrive out of order', async () => {
    const h = harness();
    const old = h.click('review', 'old'), current = h.click('review', 'new');
    h.finish(h.requests[1], 'Newest review'); await current;
    h.finish(h.requests[0], 'Obsolete review'); await old;
    assert.equal(h.modal.open, true);
    assert.match(h.get('#modal-content').innerHTML, /Newest review/);
    assert.doesNotMatch(h.get('#modal-content').innerHTML, /Obsolete review/);
  });
  await test('another modal invalidates pending review', async () => {
    const h = harness(); const pending = h.click('review', 'old');
    h.run("openModal('New dialog', '', '')");
    h.finish(h.requests[0], 'Obsolete review'); await pending;
    assert.match(h.get('#modal-content').innerHTML, /New dialog/);
  });
  await test('repeated rapid reopen survives multiple queued closes and old responses', async () => {
    const h = harness(); h.run("openModal('Initial', '', '')");
    const stale = [];
    for (let i = 0; i < 3; i++) {
      stale.push(h.click('review', 'old-' + i));
      await h.dismiss('close');
      h.run("openModal('Interim', '', '')");
    }
    await h.dismiss('close');
    const current = h.click('review', 'current');
    await h.flushClose();
    for (let i = 0; i < 3; i++) { h.finish(h.requests[i], 'Obsolete-' + i); await stale[i]; }
    h.finish(h.requests[3], 'Current review'); await current;
    assert.equal(h.modal.open, true);
    assert.match(h.get('#modal-content').innerHTML, /Current review/);
  });
  await test('interview handoff invalidates the ordinary modal synchronously', async () => {
    const h = harness(); h.loadInterview(); h.run("openModal('Original', '', '')");
    const old = h.click('review', 'old');
    const opening = h.run("openInterview('task', 'interview-decision')");
    h.requests[1].resolve({interviews: [{id: 'interview', decision_id: 'interview-decision',
      status: 'draft', capture_method: 'typed', turns: []}]});
    await opening;
    assert.equal(h.modal.open, false);
    assert.equal(h.get('created-dialog').open, true);
    h.finish(h.requests[0], 'Obsolete review'); await old;
    assert.equal(h.modal.open, false, 'old ordinary review must not reopen over interview');
    const version = h.run('modalVersion'); await h.flushClose();
    assert.equal(h.run('modalVersion'), version);
  });
  await test('close helper cancels a request even before its dialog is visible', async () => {
    const h = harness(); const pending = h.click('review', 'old');
    h.run('closeModal()');
    h.finish(h.requests[0], 'Obsolete review'); await pending;
    assert.equal(h.modal.open, false);
  });
  await test('all ordinary dialog close callers use the synchronous helper', async () => {
    assert.equal((source.match(/\$\('#modal'\)\.close\(\)/g) || []).length, 1);
    assert.match(source, /function closeModal\(\) \{[\s\S]*?modalVersion \+= 1;\s*\$\('#modal'\)\.close\(\);/);
    assert.doesNotMatch(interviewSource, /\$\('#modal'\)\.close\(/);
    assert.match(interviewSource, /if \(\$\('#modal'\)\?\.open\) closeModal\(\);/);
  });
  if (failures.length) throw new Error(`${failures.length} failed; ${passed} passed:\n${failures.join('\n')}`);
  console.log(`Modal lifecycle: ${passed} checks passed (queued DOM/API doubles, not Chromium).`);
})().catch(error => { console.error(error); process.exitCode = 1; });
