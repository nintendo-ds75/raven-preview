/* Provider-free source-review state checks using shipped rendering functions.
 * Parsed-markup DOM doubles verify replacement/poll behavior, not Chromium.
 * Eligibility is an opaque constant. No submission or identity probes run. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const sourcePath = process.argv[2] || path.join(__dirname, '../web/brief.js');
const source = fs.readFileSync(sourcePath, 'utf8');
const clone = value => JSON.parse(JSON.stringify(value));
const decode = text => text.replace(/&(amp|lt|gt|quot|#39);/g,
  (_, entity) => ({amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'"})[entity]);

function parse(html) {
  const elements = [], stack = [];
  for (const token of html.match(/<[^>]*>|[^<]+/g) || []) {
    if (!token.startsWith('<')) {
      for (const el of stack) el.textContent += decode(token);
      continue;
    }
    const tag = token.match(/^<\/?([\w-]+)/)?.[1];
    if (!tag) continue;
    if (token.startsWith('</')) {
      const index = stack.findLastIndex(el => el.tagName === tag.toUpperCase());
      if (index >= 0) stack.length = index;
      continue;
    }
    const attrs = {};
    for (const match of token.slice(tag.length + 1, -1).matchAll(/([\w:-]+)(?:="([^"]*)")?/g)) {
      attrs[match[1]] = decode(match[2] || '');
    }
    const classes = new Set((attrs.class || '').split(/\s+/));
    const el = {tagName: tag.toUpperCase(), attrs, id: attrs.id || '', textContent: '',
      parentElement: stack.at(-1) || null, dataset: {}, open: 'open' in attrs,
      checked: 'checked' in attrs, hidden: 'hidden' in attrs, value: attrs.value || '',
      style: {}, scrollHeight: 20,
      classList: {contains: name => classes.has(name), remove: name => classes.delete(name)},
      focus() {},
    };
    for (const [key, value] of Object.entries(attrs)) if (key.startsWith('data-')) {
      el.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
    }
    elements.push(el);
    if (!['input', 'br', 'hr', 'img', 'wbr', 'meta', 'link'].includes(tag)) stack.push(el);
  }
  return elements;
}

function matches(el, selector) {
  const negative = selector.match(/:not\(([^)]+)\)/);
  if (negative) {
    if (matches(el, negative[1])) return false;
    selector = selector.replace(negative[0], '');
  }
  if (selector.endsWith(':checked')) {
    if (!el.checked) return false;
    selector = selector.slice(0, -8);
  }
  const tag = selector.match(/^[\w-]+/)?.[0];
  if (tag && el.tagName !== tag.toUpperCase()) return false;
  const id = selector.match(/#([\w-]+)/)?.[1];
  if (id && el.id !== id) return false;
  for (const [, name] of selector.matchAll(/\.([\w-]+)/g)) if (!el.classList.contains(name)) return false;
  for (const [, name, value] of selector.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)) {
    if (name === 'open' ? !el.open : !(name in el.attrs)) return false;
    if (value !== undefined && el.attrs[name] !== value) return false;
  }
  return true;
}

function harness() {
  let elements = [], html = '', reply = null, renders = 0;
  const fixed = new Map(['brief-workspace', 'brief-account'].map(id => [id, {textContent: '', innerHTML: ''}]));
  const root = {
    set innerHTML(value) { html = value; elements = parse(value); renders++; },
    get innerHTML() { return html; },
  };
  fixed.set('brief', root);
  const document = {
    activeElement: {tagName: 'BODY'}, addEventListener() {},
    getElementById: id => fixed.get(id) || elements.find(el => el.id === id) || null,
    querySelector(selector) {
      return selector.startsWith('#') ? this.getElementById(selector.slice(1)) : this.querySelectorAll(selector)[0] || null;
    },
    querySelectorAll: selector => elements.filter(el => matches(el, selector)),
  };
  const context = {console, document, location: {hash: '#synthetic-fixture'},
    matchMedia: () => ({matches: false, addEventListener() {}}),
    ResizeObserver: class {disconnect() {} observe() {}},
    getComputedStyle: () => ({lineHeight: '21px'}),
    addEventListener() {}, setInterval() {}, setTimeout() {}, clearTimeout() {},
    approvalScope: () => '', taskDisplayLabel: () => 'Synthetic source review',
    fetch(url, options) {
      assert.equal(url, '/api/brief', 'only the synthetic brief read is allowed');
      assert.equal(options.method, 'GET', 'no submit or mutation is exercised');
      // The initial startup read stays pending; each tested poll is local data.
      if (reply === null) return new Promise(() => {});
      const value = reply; reply = null;
      return Promise.resolve({ok: true, json: async () => clone(value)});
    },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(source, context, {filename: sourcePath});
  // All non-source cards and existing eligibility are outside this regression.
  vm.runInContext(`focusStanding = () => 'sign';
    viewerMenu = accountCta = noteCard = requesterCard = decisionsCard = peopleCard = historyCard = joinNote = heroLine = () => '';
    fitQuotes = syncOptions = () => {};`, context);
  return {
    document, get: id => document.getElementById(id),
    details: () => elements.filter(el => el.tagName === 'DETAILS'),
    source: record => elements.find(el => el.classList.contains('source-snapshot') && el.textContent.includes('Record ID: ' + record)),
    metadata(record) {
      const parent = this.source(record);
      return elements.find(el => el.tagName === 'DETAILS' && el.parentElement === parent);
    },
    dependency: question => elements.find(el => el.tagName === 'DETAILS' && el.textContent.startsWith('Source decision: ' + question)),
    render(value) {
      context.fixture = clone(value);
      vm.runInContext('data = fixture; render(); lastRead = JSON.stringify(data);', context);
    },
    async poll(value) { reply = value; await vm.runInContext('load({quiet: true})', context); },
    renderCount: () => renders,
  };
}

function fixture() {
  const record = (id, version) => ({record_id: id, source_version_id: version, role: 'support',
    snapshot: {title: 'Synthetic policy', author: 'Synthetic author', status: 'Current', body: 'Retain records for fourteen days.'}});
  const dependency = (id, question) => ({decision_id: id, question, answer: 'Keep fourteen days.',
    reviewed_snapshot: {id, question, answer: 'Keep fourteen days.', revision: 1}});
  return {task: {id: 'task-fixture', title: 'Synthetic review', repo: 'synthetic/review', status: 'active'},
    viewer: {name: 'Synthetic viewer'}, requester: {name: 'Synthetic requester'},
    progress: {signed: 0, total: 1}, nodes: [], contacts: [], history: [],
    focus: {node_id: 'decision-fixture', updated_at: 'revision-one', question: 'How long are records retained?',
      answer: 'Keep fourteen days.', rationale: 'Synthetic rationale.', context: 'Synthetic scope.',
      paths: ['policy/retention.py'], facts: {environment: 'staging'},
      approval_scope: {id: 'decision-fixture', facts: {environment: 'staging'}}, approval_scope_text: 'staging',
      source_revalidation: {has_reliance: true, available: true, notice: 'Review the exact synthetic evidence.',
        sources: [record('record-"A<&', 'version-A'), record('record-B', 'version-B')],
        dependencies: [dependency('dependency-A', 'Synthetic dependency A?'), dependency('dependency-B', 'Synthetic dependency B?')],
        pins: [{record_id: 'record-"A<&', source_version_id: 'version-A'}],
        decision_pins: [{decision_id: 'dependency-A', revision: 1}]}}};
}

(async () => {
  const failures = [];
  let passed = 0;
  const test = async (name, body) => {
    try { await body(); passed++; }
    catch (error) { failures.push(name + ': ' + error.message); }
  };
  await test('all source and decision disclosures have distinct exact keys', () => {
    const h = harness(); h.render(fixture());
    const keys = h.details().map(el => el.dataset.keep);
    assert.equal(keys.length, 10); // Includes persisted context, people, notes and recorded-scope disclosures.
    assert(keys.every(Boolean), 'source, metadata, and dependent-decision disclosures need keys');
    assert.equal(new Set(keys).size, keys.length);
  });
  await test('no checkbox stands between the person and Send; the evidence is shown and the answer is tied to it', () => {
    const h = harness(), value = fixture(); h.render(value);
    assert.equal(h.get('brief-source-confirm'), null);
    for (const missing of ['unavailable', 'removed', 'informational']) {
      const changed = clone(value);
      if (missing === 'unavailable') changed.focus.source_revalidation.available = false;
      if (missing === 'removed') delete changed.focus.source_revalidation;
      if (missing === 'informational') changed.focus.source_revalidation.has_reliance = false;
      h.render(changed);
      assert.equal(h.get('brief-source-confirm'), null);
    }
  });
  await test('switching away and back does not resurrect source disclosure state', () => {
    const h = harness(), value = fixture(); h.render(value);
    h.metadata('record-"A<&').open = true;
    const changed = clone(value); changed.focus.node_id = 'different-decision'; h.render(changed);
    assert.equal(h.metadata('record-"A<&').open, false);
    h.render(value);
    assert.equal(h.metadata('record-"A<&').open, false);
  });
  await test('identical quiet reads do not rebuild the DOM', async () => {
    const h = harness(), value = fixture(); h.render(value);
    const count = h.renderCount(), meta = h.metadata('record-"A<&');
    meta.open = true; await h.poll(value);
    assert.equal(h.renderCount(), count);
    assert.equal(h.metadata('record-"A<&'), meta);
    assert.equal(meta.open, true);
  });
  if (failures.length) {
    console.error(failures.join('\n'));
    console.error(`${failures.length} failed; ${passed} passed.`);
    process.exitCode = 1;
  } else console.log(`${passed} source review state checks passed (parsed DOM/API doubles, not Chromium).`);
})();
