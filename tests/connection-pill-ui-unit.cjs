// The top-bar pill says 'Connection lost · showing data from HH:MM' only
// after a fetch failed, and goes back to the workspace name on the next
// success. An exception while drawing a page is a different thing: it is
// logged and drawn in #app, and never touches the pill. (DOM/API doubles,
// not Chromium.)
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const slice = (from, to) => source.slice(source.indexOf(from), source.indexOf(to, source.indexOf(from)));

// 1. api() marks every failure between the request and a parsed body as a fetch failure.
(async () => {
  const apiContext = {state: {csrf_token: 't'}, Object, Error, TypeError, String, JSON};
  vm.createContext(apiContext);
  vm.runInContext(slice('async function api(', 'async function connectGitHub('), apiContext);
  apiContext.fetch = async () => { throw new TypeError('Failed to fetch'); };
  await assert.rejects(apiContext.api('/api/state'), error => error.fetchFailed === true && error.message === 'Failed to fetch');
  apiContext.fetch = async () => ({ok: false, status: 503, json: async () => ({error: 'Down for a moment'})});
  await assert.rejects(apiContext.api('/api/state'), error => error.fetchFailed === true && error.status === 503 && error.message === 'Down for a moment');
  apiContext.fetch = async () => ({ok: true, status: 200, headers: {get: () => ''}, json: async () => { throw new SyntaxError('Unexpected token <'); }});
  await assert.rejects(apiContext.api('/api/state'), error => error.fetchFailed === true && error.status === 200);
  apiContext.fetch = async () => ({ok: true, status: 200, headers: {get: () => ''}, json: async () => ({decisions: []})});
  assert.deepEqual(await apiContext.api('/api/state'), {decisions: []});

  // 2. The poll: pill after a failed fetch, restored after a success, untouched by a render error.
  const nodes = {
    '#connection': {classList: new Set(), get className() { return [...this.classList].join(' '); }},
    '#workspace-name': {textContent: 'Local workspace', title: ''},
    '#inbox-count': {}, '#app': {innerHTML: '', dataset: {}, contains: () => false},
    '#modal': {open: false}, '#settings-dot': {hidden: true}, '#profile-name': {}, '#profile-avatar': {}, '#profile-note': {},
  };
  const classList = set => Object.assign(set, {add(c) { Set.prototype.add.call(set, c); }, remove(c) { set.delete(c); }});
  classList(nodes['#connection'].classList);
  const logged = [];
  let fetchResult = () => true, drawn = 0, drawError = null;
  const context = {
    state: {auth: {enabled: false}, decisions: [], workspace: {name: 'Acme'}}, nodes, Date, String, Error, Set, console: {error: e => logged.push(e)},
    $: key => nodes[key], esc: value => String(value), initials: () => 'LO', fetching: false, workspaceError: '', lastDraw: 0, lastFetchedAt: 0,
    STALE_DRAW_MS: 60000, tab: 'pending', tabChosen: false, view: 'inbox', taskDetail: null, taskError: '',
    fetchState: async () => fetchResult(), updateAgentConnectPrompt() {}, agentStatus: () => '', personalInbox: () => false,
    assignedToMe: () => false, needsYou: () => false, taskId: () => '', document: {activeElement: {tagName: 'BODY'}},
    render() { drawn += 1; if (drawError) throw drawError; }, closeModal() {}, loadTask() {},
  };
  vm.createContext(context);
  vm.runInContext(slice('function updateWorkspaceName()', 'function loadOwnership()'), context);
  vm.runInContext(slice('function restoreLoginRoute()', 'restoreLoginRoute();').replace('function restoreLoginRoute', 'function unused'), context);

  const pill = () => `${nodes['#connection'].className}|${nodes['#workspace-name'].textContent}`;
  const RealDate = Date;
  context.Date = class extends RealDate { constructor(...args) { super(...(args.length ? args : [RealDate.UTC(2026, 9, 28, 12, 27)])); } static now() { return RealDate.UTC(2026, 9, 28, 12, 27); } };
  await context.refresh();
  assert.equal(pill(), '|Local workspace', 'a healthy poll: no offline class, the workspace label');
  assert.equal(drawn, 1);
  context.state.auth = {enabled: true};
  await context.refresh();
  assert.equal(nodes['#workspace-name'].textContent, 'Acme', 'the workspace name when sign-in is on');
  context.state.auth = {enabled: false};
  // A render TypeError ('x.map is not a function') must not count as a fetch failure, so the mark is what api() sets.
  drawError = new TypeError('decisions.map is not a function');
  await context.refresh();
  assert.equal(pill(), '|Local workspace', 'a TypeError thrown while drawing is a render error, not a lost connection');
  assert.match(nodes['#app'].innerHTML, /Something went wrong drawing this page\./);
  drawError = null; logged.length = 0; nodes['#app'].innerHTML = '';
  const failed = Object.assign(new TypeError('Failed to fetch'), {fetchFailed: true});
  fetchResult = () => { throw failed; };
  await context.refresh();
  const expectedClock = new context.Date().getHours().toString().padStart(2, '0') + ':27';
  assert.equal(pill(), `offline|Connection lost · showing data from ${expectedClock}`, 'a failed fetch: offline, with the time of the data on screen');
  assert.match(nodes['#app'].innerHTML, /Couldn’t load your workspace\./);
  assert.match(nodes['#app'].innerHTML, /Failed to fetch/);
  assert.match(nodes['#app'].innerHTML, /data-action="retry">Try again/);
  nodes['#app'].innerHTML = '';
  await context.refresh({quiet: true});
  assert.equal(nodes['#app'].innerHTML, '', 'a quiet poll that fails leaves the page alone');
  assert.equal(pill(), `offline|Connection lost · showing data from ${expectedClock}`);
  fetchResult = () => true;
  await context.refresh({quiet: true});
  assert.equal(pill(), '|Local workspace', 'the next success restores the pill');
  assert.equal(logged.length, 0);

  // A render exception: drawn in #app, logged, pill untouched.
  drawError = new Error('decisions.map is not a function');
  await context.refresh();
  assert.equal(pill(), '|Local workspace', 'a render error never sets the pill offline');
  assert.match(nodes['#app'].innerHTML, /<h3>Something went wrong drawing this page\.<\/h3>/);
  assert.match(nodes['#app'].innerHTML, /decisions\.map is not a function/);
  assert.match(nodes['#app'].innerHTML, /data-action="retry">Try again/);
  assert.equal(logged.length, 1);
  assert.equal(logged[0], drawError);
  assert.equal(context.fetching, false, 'the poll is free to run again after a render error');
  drawError = null;
  await context.refresh();
  assert.equal(pill(), '|Local workspace');

  // A fetch that fails before any success says only that the connection is lost.
  const fresh = {...nodes, '#workspace-name': {textContent: 'Local workspace'}, '#connection': {classList: new Set()}};
  classList(fresh['#connection'].classList);
  context.$ = key => fresh[key]; context.lastFetchedAt = 0;
  fetchResult = () => { throw Object.assign(new Error('Service Unavailable'), {fetchFailed: true, status: 503}); };
  await context.refresh();
  assert.equal(fresh['#workspace-name'].textContent, 'Connection lost');
  assert.ok(fresh['#connection'].classList.has('offline'));
  console.log('Connection pill: fetch failures mark the pill with the data time, success restores it, render errors are drawn in #app and never touch it.');
})().catch(error => { console.error(error); process.exitCode = 1; });
