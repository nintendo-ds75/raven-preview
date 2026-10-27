/* Add evidence panel contracts with DOM/API doubles; not a browser test. */
'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const LIMIT = 20000;

function harness({role = 'member', me = {id: 'p1', name: 'Pat', role: 'member'}, records = []} = {}) {
  const handlers = {}, requests = [];
  const app = {innerHTML: '', dataset: {}, querySelectorAll: () => [], contains: () => false, querySelector: () => null};
  // The Today aside carries <div id="evidence-mount"></div>; the panel renders into it.
  const mount = {id: 'evidence-mount', innerHTML: '', querySelector: () => null, contains: () => false};
  const head = {appendChild() {}};
  const c = {console, URL, crypto: require('node:crypto').webcrypto, view: 'today', location: {hash: '#today'},
    state: {auth: {enabled: true}, me: {...me, role}, workbench: {hosts: [], repositories: []}, question_repositories: [{id: 'sample/repo', name: 'sample/repo'}, {id: 'other/repo', name: 'other/repo'}]},
    document: {activeElement: {}, head, createElement: () => ({}), getElementById: id => id === 'app' ? app : id === 'evidence-mount' ? mount : null,
      addEventListener: (type, fn) => (handlers[type] ||= []).push(fn)},
    esc: x => String(x ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;')};
  c.fixture = {objectives: [], proposals: [], capabilities: [], repositories: [{id: 'sample/repo'}]};
  c.api = async (url, data) => {
    requests.push({url, data});
    if (data) return url === '/api/records' ? {repo: data.repo, ref: data.ref} : {withdrawn: true};
    if (url.startsWith('/api/records?')) return {namespace: 'raven:paste', records};
    return c.fixture;
  };
  vm.createContext(c);
  for (const file of ['objectives.js', 'evidence.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, '../web', file), 'utf8'), c);
  const run = code => vm.runInContext(code, c);
  run('objectiveData = fixture');
  const submit = async (id, data = {}, dataset = {}) => {
    const form = {id, dataset, reportValidity: () => true, querySelectorAll: () => Object.entries(data).map(([name, value]) => ({name, value}))};
    for (const fn of handlers.submit || []) await fn({target: {closest: () => form, ...form}, preventDefault() {}});
  };
  const click = async (action, id = '') => {
    const button = {disabled: false, dataset: {evidenceAction: action, id}};
    for (const fn of handlers.click || []) await fn({target: {closest: selector => selector.includes('evidence') ? button : null}});
  };
  const settle = () => new Promise(resolve => setTimeout(resolve, 0));
  return {c, app, mount, run, submit, click, requests, settle};
}

(async () => {
  // The panel mounts into the Today aside for a member, with a label on every field.
  let h = harness();
  h.run('renderObjectives()');
  await h.settle();
  assert.match(h.mount.innerHTML, /<h2 id="evidence-heading">Add evidence<\/h2>/);
  for (const id of ['evidence-repo', 'evidence-kind', 'evidence-title', 'evidence-body', 'evidence-date', 'evidence-url']) {
    assert.match(h.mount.innerHTML, new RegExp(`<label for="${id}">`), id);
    assert.match(h.mount.innerHTML, new RegExp(`id="${id}"`), id);
  }
  assert.match(h.mount.innerHTML, /<option value="sample\/repo"/);
  assert.match(h.mount.innerHTML, /<option value="other\/repo"/);
  assert.match(h.mount.innerHTML, /maxlength="20000"/);
  assert.match(h.mount.innerHTML, /0 \/ 20,000 characters/);
  assert.equal(h.requests.filter(r => r.url.startsWith('/api/records?namespace=raven%3Apaste&limit=10')).length, 1);

  // The form posts the fields as one pasted record in the raven:paste namespace.
  await h.submit('evidence-form', {repo: 'sample/repo', kind: 'email', title: 'Customer asks for yearly billing', body: 'Could we pay yearly?', created_at: '2026-10-01', url: 'https://mail.example/1'});
  const post = h.requests.find(r => r.data && r.url === '/api/records');
  assert.ok(post, 'posted');
  assert.equal(post.data.repo, 'sample/repo');
  assert.equal(post.data.kind, 'email');
  assert.equal(post.data.title, 'Customer asks for yearly billing');
  assert.equal(post.data.body, 'Could we pay yearly?');
  assert.equal(post.data.created_at, '2026-10-01');
  assert.equal(post.data.url, 'https://mail.example/1');
  assert.equal(post.data.provider, 'generic');
  assert.equal(post.data.namespace, 'raven:paste');
  assert.match(post.data.ref, /^paste:[0-9a-f-]{36}$/);
  assert.equal(post.data.author, undefined);
  assert.match(h.mount.innerHTML, /Saved as evidence for sample\/repo/);
  assert.equal(h.requests.filter(r => r.url.startsWith('/api/records?namespace')).length, 2, 'the list reloads after a save');

  // 20,001 characters are refused with the hint, and nothing is posted.
  h = harness();
  h.run('renderObjectives()');
  await h.settle();
  await h.submit('evidence-form', {repo: 'sample/repo', kind: 'note', title: 'Long thread', body: 'x'.repeat(LIMIT + 1)});
  assert.equal(h.requests.filter(r => r.data).length, 0);
  assert.match(h.mount.innerHTML, /Split it, or import it as a meeting transcript\./);
  await h.submit('evidence-form', {repo: 'sample/repo', kind: 'note', title: 'Long thread', body: 'x'.repeat(LIMIT)});
  assert.equal(h.requests.filter(r => r.data).length, 1, 'exactly 20,000 is accepted');

  // Withdraw is offered to the author of a record and to an admin, to nobody else.
  const records = [
    {record_id: 'r-mine', repo: 'sample/repo', kind: 'email', title: 'Mine', author: 'Pat', attested_by: 'p1', availability: 'available', observed_at: '2026-10-01T00:00:00Z'},
    {record_id: 'r-theirs', repo: 'sample/repo', kind: 'slack', title: 'Theirs', author: 'Oli', attested_by: 'p2', availability: 'available', observed_at: '2026-10-01T00:00:00Z'},
    {record_id: 'r-gone', repo: 'sample/repo', kind: 'doc', title: 'Gone', author: 'Pat', attested_by: 'p1', availability: 'inaccessible', observed_at: '2026-10-01T00:00:00Z'},
  ];
  h = harness({records});
  h.run('renderObjectives()');
  await h.settle();
  assert.match(h.mount.innerHTML, /data-evidence-action="withdraw" data-id="r-mine"/);
  assert.doesNotMatch(h.mount.innerHTML, /data-id="r-theirs"/);
  assert.doesNotMatch(h.mount.innerHTML, /data-evidence-action="withdraw" data-id="r-gone"/);
  assert.match(h.mount.innerHTML, /Gone<\/strong>[\s\S]*withdrawn/);
  await h.click('withdraw', 'r-mine');
  assert.match(h.mount.innerHTML, /id="evidence-withdraw-form" data-id="r-mine"/);
  assert.match(h.mount.innerHTML, /<label for="evidence-reason">/);
  await h.submit('evidence-withdraw-form', {reason: 'Shared by mistake'}, {id: 'r-mine'});
  const withdraw = h.requests.find(r => r.data && r.url.endsWith('/withdraw'));
  assert.equal(withdraw.url, '/api/records/r-mine/withdraw');
  assert.equal(withdraw.data.reason, 'Shared by mistake');

  h = harness({records, me: {id: 'p9', name: 'Ada', role: 'admin'}, role: 'admin'});
  h.run('renderObjectives()');
  await h.settle();
  assert.match(h.mount.innerHTML, /data-evidence-action="withdraw" data-id="r-mine"/);
  assert.match(h.mount.innerHTML, /data-evidence-action="withdraw" data-id="r-theirs"/);

  // A viewer gets no panel at all, and a submit from one posts nothing.
  h = harness({role: 'viewer'});
  h.run('renderObjectives()');
  await h.settle();
  assert.equal(h.mount.innerHTML, '');
  await h.submit('evidence-form', {repo: 'sample/repo', kind: 'note', title: 'x', body: 'y'});
  assert.equal(h.requests.filter(r => r.data).length, 0);

  // Markup escapes what people pasted.
  h = harness({records: [{record_id: 'r', repo: 'sample/repo', kind: 'note', title: '<img onerror=bad()>', author: 'Pat', attested_by: 'p1', availability: 'available', url: 'javascript:alert(1)'}]});
  h.run('renderObjectives()');
  await h.settle();
  assert.match(h.mount.innerHTML, /&lt;img/);
  assert.doesNotMatch(h.mount.innerHTML, /<img onerror|href="javascript:/);
  console.log('Add evidence UI: 7 contract scenarios passed (DOM/API doubles).');
})().catch(error => { console.error(error); process.exitCode = 1; });
