'use strict';

function restoreLoginRoute() {
  try {
    const raw = sessionStorage.getItem('raven-return-decision');
    if (!raw) return;
    sessionStorage.removeItem('raven-return-decision');
    const saved = JSON.parse(raw);
    if (!['', '#inbox', '#runs'].includes(location.hash) || !/^#runs\/[a-f0-9]{12,32}\/[a-f0-9]{12,32}$/.test(saved.route || '') ||
        !Number.isFinite(saved.savedAt) || Date.now() - saved.savedAt < 0 || Date.now() - saved.savedAt > 30 * 60 * 1000) return;
    history.replaceState(null, '', location.pathname + location.search + saved.route);
  } catch {} // Malformed/blocked storage never breaks workspace access.
}
restoreLoginRoute();


const paths = {
  inbox: '<path d="M4 4h16v16H4zM4 13h5l2 3h2l2-3h5"/>',
  activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  layers: '<path d="m12 3 10 5-10 5L2 8Zm-9 10 9 5 9-5M3 18l9 5 9-5"/>',
  users: '<circle cx="9" cy="7" r="3"/><path d="M3 20v-3a6 6 0 0 1 12 0v3M16 4a3 3 0 0 1 0 6m2 4a5 5 0 0 1 3 4v2"/>',
  terminal: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m7 9 3 3-3 3m6 0h4"/>',
  settings: '<path d="m9 3-1 3-3 1 1 3-2 2 2 2-1 3 3 1 1 3h4l1-3 3-1-1-3 2-2-2-2 1-3-3-1-1-3Z"/><circle cx="11" cy="12" r="3"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 3 2.5L12 14m0 3h.01"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  spark: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5ZM20 2v4m-2-2h4"/>',
  arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
};
const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.inbox}</svg>`;
document.querySelectorAll('[data-icon]').forEach(el => { el.innerHTML = icon(el.dataset.icon); });
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const plural = (count, word, words = word + 's') => `${count} ${count === 1 ? word : words}`;
const CLIENT_NAMES = {claude: 'Claude Code', codex: 'Codex', cursor: 'Cursor'};
const clientName = id => CLIENT_NAMES[id] || id;
// A task's status as a row or the task page says it; the raw value
// ("working") stays in the API. Four words cover every lifecycle: a task
// is done, failed, closed or still in progress; who it waits on comes from
// its decisions (runStatusSentence), never from the lifecycle.
const RUN_STATUS = {completed: 'Done', failed: 'Failed', cancelled: 'Closed', abandoned: 'Closed'};
const runStatusLabel = status => RUN_STATUS[status] || 'In progress';
const initials = name => (name || '?').split(/\s+/).map(word => word[0]).slice(0, 2).join('');
const avatar = (name, index = 0) => `<span class="avatar ${['sage','lilac','sand'][index % 3]}">${esc(initials(name))}</span>`;
const ago = value => {
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(value)) / 60000));
  return minutes < 1 ? 'just now' : minutes < 60 ? `${minutes}m ago` : minutes < 1440 ? `${Math.floor(minutes / 60)}h ago` : `${Math.floor(minutes / 1440)}d ago`;
};
const pill = (text, color = '') => `<span class="pill ${color}"><span class="status-dot"></span>${esc(text)}</span>`;
// The one word each page is called, in the sidebar, the h1 and the window title.
const titles = {inbox: ['Inbox'], runs: ['Tasks'], owners: ['People'], connect: ['Settings'], today: ['Outcomes'], work: ['Raven']};
let state = {decisions: [], runs: [], owners: [], events: []};
let workspaceError = '';
// The ownership graph and the directory (people, teams, the authority map, the coordinator) are not in the
// polled state: the owners view fetches them when it renders.
let ownership = null, ownershipLoading = false;
let savings = null, savingsLoading = false, savingsAt = 0;
let directory = null, directoryLoading = false;
let view = 'inbox', tab = 'pending', tabChosen = false, query = '', ownerFilter = '', fetching = false, inboxShown = 0;
// The ETag of the state on screen, sent back with each poll; and when the
// list was last drawn, so relative times still move while nothing changes.
let stateTag = '', lastDraw = 0, lastFetchedAt = 0;
const STALE_DRAW_MS = 60000, MODEL_WAIT_MS = 45000;
let toastTimer;
let modalVersion = 0, navigationVersion = 0;
let resetScrollOnNavigation = false;
let launcherPair = null;
const pairing = new URLSearchParams(location.hash.split('?')[1] || '');
if (/^\d{1,5}$/.test(pairing.get('launcher_port') || '') && pairing.get('launcher_key')) {
  launcherPair = {port: pairing.get('launcher_port'), key: pairing.get('launcher_key')};
  history.replaceState(null, '', location.pathname + location.search + '#connect');
}

async function localLauncher(path, data = {}) {
  if (!launcherPair) throw new Error('Start the local launcher in your project first. See Local launcher below.');
  const response = await fetch(`http://127.0.0.1:${launcherPair.port}/${path}`, {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-Bridge-Launcher': launcherPair.key},
    body: JSON.stringify(data), signal: AbortSignal.timeout(10000),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Launcher unavailable');
  return result;
}

function agentCards() {
  return `<p class="context"><strong>Connect</strong> creates a limited credential and shows the one command for your client. Start and Resume open a terminal through the optional local launcher.</p><div class="agent-cards">${[['claude', 'Claude Code'], ['codex', 'Codex'], ['cursor', 'Cursor']].map(([id, name]) => `<div class="agent-card"><span class="agent-logo ${id}"><img src="/${id === 'codex' ? 'openai' : id}.svg" alt="${id === 'codex' ? 'OpenAI' : name} logo" width="28" height="28"></span><h3>${name}</h3><button class="button primary" data-action="quick-agent" data-client="${id}">${id === 'cursor' ? 'Add to Cursor' : 'Connect'}</button>${id !== 'cursor' ? `<button class="button" data-action="launch-agent" data-client="${id}" data-mode="new">Start session</button><button class="button" data-action="launch-agent" data-client="${id}" data-mode="resume">Resume session</button>` : '<p>Opens Cursor’s installation confirmation.</p>'}</div>`).join('')}</div><details class="history" id="local-launcher-help"><summary>Local launcher · enable terminal buttons</summary><p>One-time per work session: run this from your Raven checkout, selecting the project folder agents should start in. It opens a paired Raven tab. Keep it running; closing it disables launches. Requires Python 3, installed agent CLIs, and macOS or Linux.</p><pre>./agents /path/to/your/project</pre><p>A browser cannot inspect local processes or start a CLI by itself. The paired helper is the permission boundary for terminal launches. Resume opens the agent’s own session picker, not an attachment to an arbitrary running terminal. Agent approval prompts remain enabled.</p></details>`;
}

function updateAgentConnectPrompt() {
  const prompt = $('#agent-connect-prompt');
  if (!prompt) return;
  prompt.hidden = true; // Setup stays in Workspace tools, away from the conversation.
}

function markAgentConnected() {
  const prompt = $('#agent-connect-prompt');
  if (prompt) prompt.hidden = true;
}

const AGENT_RULE = '## Raven\n\nThis team uses Raven (the `bridge` MCP server) to route the judgment calls in a task to the person who owns them. '
  + 'At the start of every task, before reading or editing anything, call `bridge_start_task`, then follow what Raven returns for the rest of the task.';

async function quickAgent(client) {
  const created = await api('/api/tokens', {label: clientName(client) + ' · quick connect'});
  markAgentConnected();
  const entry = structuredClone(state.mcp_config.mcpServers.bridge);
  entry.headers = {Authorization: 'Bearer ' + created.token};
  let content;
  if (client === 'cursor') {
    const config = {url: entry.url, headers: entry.headers};
    const encoded = btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify(config))));
    content = `<a class="button primary" href="cursor://anysphere.cursor-deeplink/mcp/install?name=bridge&amp;config=${encodeURIComponent(encoded)}">Open Cursor and approve</a><p>This passes a limited Raven credential directly to Cursor. Do not share the installation link.</p>`;
  } else {
    const quote = value => "'" + value.replaceAll("'", "'\\''") + "'";
    const command = client === 'claude' ? 'claude mcp add --transport http bridge ' + quote(entry.url) + ' --header ' + quote(entry.headers.Authorization.replace('Bearer ', 'Authorization: Bearer ')) : null;
    content = command ? `<p>Run once in your project terminal, then restart or reconnect MCP in Claude Code.</p><pre id="mcp-config">${esc(command)}</pre>` : `<p>Add this entry in your Codex MCP configuration. Preserve other existing settings.</p><pre id="mcp-config">${esc('[mcp_servers.bridge]\nurl = ' + JSON.stringify(entry.url) + '\nrequired = true\nhttp_headers = { Authorization = ' + JSON.stringify(entry.headers.Authorization) + ' }')}</pre>`;
    content += '<button class="button" data-action="copy">Copy setup</button>';
  }
  // Connected is not the same as used. Measured on a real task, Claude
  // Code had Raven's tools and instructions and never called one; a line
  // in the file the agent reads for every task is what made it start there.
  const file = client === 'claude' ? 'CLAUDE.md' : 'AGENTS.md';
  content += `<h3>Then tell the agent to use it</h3><p>Add this to <code>${file}</code> at the root of the repository. Without it an agent can hold Raven's tools and never call them.</p><pre id="agent-rule">${esc(AGENT_RULE)}</pre><button class="button" data-action="copy" data-target="#agent-rule">Copy instruction</button>`;
  openModal('Connect ' + clientName(client), 'Limited agent access; no decision-approval permissions.', content);
}

async function launchAgent(client, action) {
  const status = await localLauncher('status');
  if (!status.clients[client]) throw new Error('Install and sign into this agent CLI first.');
  openModal(action === 'resume' ? 'Resume agent session' : 'Start agent session', status.project,
    `<p>Open a local terminal in this project with Raven configured. Your agent’s normal permission prompts remain enabled.</p><button class="button primary" data-action="confirm-launch" data-client="${esc(client)}" data-mode="${esc(action)}">Open terminal</button>`);
}

function notify(message) {
  clearTimeout(toastTimer);
  $('#toast').textContent = message;
  $('#toast').hidden = false;
  toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 4500);
}

// Progress the moment a control is used: the button is disabled and says
// what is happening before the request leaves, and goes back to its label
// when the request fails or the control is still on screen. Measured on
// the prototype: a sign-off, a hand-on or an answer only dimmed the button,
// and the person waited with no sign that anything had been recorded.
const busyLabels = {signoff: 'Signing…', refer: 'Handing on…', assign: 'Assigning…', followups: 'Adding…', reframe: 'Recording…',
  'end-rule': 'Ending rule…', 'end-authority': 'Ending…', 'slack-sync': 'Refreshing…', 'github-refresh': 'Refreshing…',
  'retry-delivery': 'Queuing…', token: 'Creating…', 'confirm-launch': 'Opening…', 'quick-agent': 'Connecting…',
  'task-brief': 'Opening…', retry: 'Connecting…', 'task-refresh': 'Refreshing…', 'agent-status': 'Checking…',
  export: 'Exporting…', fallback: 'Saving…', 'github-connect': 'Connecting…', 'github-authorize': 'Starting…'};
function busy(control, label = 'Working…') {
  if (!control || !control.dataset || control.dataset.busy) return () => {};
  const html = control.innerHTML;
  control.dataset.busy = '1';
  control.disabled = true;
  if (control.setAttribute) control.setAttribute('aria-busy', 'true');
  control.textContent = label;
  return () => {
    delete control.dataset.busy;
    if (control.removeAttribute) control.removeAttribute('aria-busy');
    control.innerHTML = html;
    control.disabled = false;
  };
}

// The poll sends back the ETag of the state it has. An unchanged workspace
// answers 304 with no body, and nothing is parsed or redrawn.
async function fetchState() {
  const body = await api('/api/state', undefined, {conditional: stateTag});
  if (body === null) return false;
  stateTag = body.etag || '';
  state = body;
  return true;
}

// conditional: the ETag of the copy already on screen ('' for none). The
// request then carries If-None-Match, answers 304 with null, and a body
// that does come back carries its own ETag as a non-enumerable `etag`.
async function api(path, data, {signal, conditional} = {}) {
  const headers = data === undefined ? {} : {'Content-Type': 'application/json', 'X-Bridge-CSRF': state.csrf_token};
  if (conditional) headers['If-None-Match'] = conditional;
  // Everything that fails between here and a parsed body is a connection
  // problem, marked so the poll can tell it from an error while drawing.
  let response, body;
  try {
    response = await fetch(path, data === undefined ? {headers, signal, ...(conditional ? {cache: 'no-store'} : {})} : {
      method: 'POST', headers, body: JSON.stringify(data), signal,
    });
    if (conditional && response.status === 304) return null;
    body = await response.json();
  } catch (error) {
    const failure = error && typeof error === 'object' ? error : new Error(String(error));
    failure.fetchFailed = true;
    if (response) failure.status = response.status;
    throw failure;
  }
  if (conditional !== undefined && response.ok && body && typeof body === 'object') {
    Object.defineProperty(body, 'etag', {value: (response.headers && response.headers.get && response.headers.get('ETag')) || ''});
  }
  if (!response.ok) { const error = new Error(body.error || 'Request failed. Please try again.'); error.status = response.status; error.fetchFailed = true; throw error; }
  return body;
}

async function connectGitHub() {
  const connection = await api('/api/github/connect', {});
  if (connection.mode === 'device') {
    openModal('Connect GitHub', 'Choose repositories, then approve this Raven on GitHub.',
      `<p><a class="button primary" href="${esc(connection.installation_url)}" target="_blank" rel="noopener noreferrer">Choose repositories on GitHub ↗</a></p><p>After installing Raven for your chosen repositories, return here to authorize your account.</p><button class="button" data-action="github-authorize">Authorize connection</button>${connection.authorized ? '<button class="button" data-action="github-refresh">Refresh connected repositories</button>' : ''}`);
    return;
  }
  if (connection.url) { window.location.assign(connection.url); return; }
}

async function authorizeGitHub() {
  const flow = await api('/api/github/device/start', {});
  openModal('Approve Raven on GitHub', 'Enter this one-time code on GitHub. Keep this window open.',
    `<pre>${esc(flow.user_code)}</pre><p><a class="button primary" href="${esc(flow.verification_uri)}" target="_blank" rel="noopener noreferrer">Open GitHub authorization ↗</a></p><p id="github-progress">Waiting for your approval…</p>`);
  const version = modalVersion;
  const poll = async () => {
    if (!$('#modal').open || modalVersion !== version) return;
    try {
      const result = await api('/api/github/device/poll', {flow_id: flow.flow_id});
      if (!$('#modal').open || modalVersion !== version) return;
      if (result.pending) { setTimeout(poll, result.interval * 1000); return; }
      if (!result.repositories.length) {
        $('#github-progress').textContent = 'Account authorized. Choose repositories on GitHub, then use Refresh connected repositories in Connect GitHub.';
        await refresh(); return;
      }
      closeModal(); await refresh(); notify('GitHub connected. Repository sync has started.');
    } catch (error) {
      if (modalVersion === version && $('#github-progress')) $('#github-progress').textContent = error.message;
    }
  };
  setTimeout(poll, flow.interval * 1000);
}

// The top-bar pill names the workspace: the workspace's own name when
// sign-in is on, otherwise 'Local workspace'. After a failed fetch the same
// pill says how old the data on screen is, until the next poll succeeds.
function updateWorkspaceName() {
  const label = $('#workspace-name');
  if (!label) return;
  label.textContent = state.auth?.enabled && state.workspace?.name ? state.workspace.name : 'Local workspace';
  label.title = label.textContent;
}

function connectionLost() {
  const at = lastFetchedAt ? new Date(lastFetchedAt) : null;
  const clock = at ? `${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}` : '';
  $('#connection').classList.add('offline');
  if ($('#workspace-name')) $('#workspace-name').textContent = clock ? `Connection lost · showing data from ${clock}` : 'Connection lost';
}

// A fetch failure is one api() marked (a network error, a non-2xx status or
// an unreadable body); anything else thrown while drawing is a render error.
// Not `instanceof TypeError`: 'x.map is not a function' from a draw is one too.
const fetchFailed = error => !!(error && (error.fetchFailed || error.status));

function renderError(error) {
  console.error(error);
  $('#app').innerHTML = `<div class="empty" role="alert"><h3>Something went wrong drawing this page.</h3><p>${esc(error && error.message)}</p><button class="button" data-action="retry">Try again</button></div>`;
}

async function refresh({quiet = false} = {}) {
  if (fetching) return;
  fetching = true;
  try {
    const changed = await fetchState();
    workspaceError = '';
    lastFetchedAt = Date.now();
    $('#connection').classList.remove('offline');
    updateWorkspaceName();
    if (changed) {
      updateAgentConnectPrompt();
      if ($('#agent-status')) $('#agent-status').innerHTML = agentStatus();
      if (state.me) {
        $('#profile-name').textContent = state.me.name;
        $('#profile-avatar').textContent = initials(state.me.name);
        $('#profile-note').innerHTML = state.auth && state.auth.enabled ? `${esc(state.me.role)} · <a href="/auth/logout">Sign out</a>` : 'Self-hosted workspace';
      }
      if (personalInbox() && !tabChosen) tab = 'mine';
      $('#inbox-count').textContent = personalInbox() ? state.decisions.filter(assignedToMe).length
        : state.counts ? state.counts.needs_you : state.decisions.filter(needsYou).length;
    }
    settingsDotState();
    if (taskId()) await loadTask({quiet});
    if (view === 'work' && typeof loadWorkbench === 'function') await loadWorkbench();
    if (view === 'today' && typeof loadObjectives === 'function') await loadObjectives();
    if ((changed || !quiet) && typeof renderWorkRecents === 'function') renderWorkRecents();
    // A quiet poll that found nothing new leaves the page alone, apart from
    // a redraw a minute so "3 minutes ago" keeps moving. Measured in
    // Chromium: the inbox was rebuilt every five seconds whether or not
    // anything had changed, and text selection and hover state went with it.
    const settled = quiet && !changed && Date.now() - lastDraw < STALE_DRAW_MS;
    if (!taskId() && !settled && (!quiet || (!$('#modal').open && !['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)))) render();
  } catch (error) {
    if (!fetchFailed(error)) { renderError(error); return; }
    connectionLost();
    if ([401,403].includes(error.status)) {
      if (typeof rememberTaskView === 'function') rememberTaskView();
      state.auth = null; workspaceError = error.message; taskDetail = null;
      if ($('#modal').open) closeModal();
      render();
    } else if (taskId()) { taskError = error.message; render(); }
    else if (!quiet) $('#app').innerHTML = `<div class="empty"><h3>Couldn’t load your workspace.</h3><p>${esc(error.message)}</p><button class="button" data-action="retry">Try again</button></div>`;
  } finally { fetching = false; }
}

// Workspace-level notices live on the Settings page; everywhere else they
// are one dot on the Settings nav item. Read from /api/state; nothing here
// probes a provider. The server writes a model notice for every backend;
// it names a gap only when no backend can make suggestions
// (state.model.proposals false: no key, or the claude-cli backend).
function settingsDotState() {
  const dot = $('#settings-dot');
  if (!dot) return;
  const incomplete = !!state.auth && ((state.model?.proposals === false && !!state.model.notice) || state.delivery?.enabled === false
    || (state.readiness || []).some(item => item && item.level === 'blocker'));
  dot.hidden = !incomplete;
  // The link's name says it too: a colour and a hover title reach nobody using a screen reader or a keyboard.
  const link = dot.closest?.('a');
  if (link) link.setAttribute('aria-label', incomplete ? 'Settings, setup incomplete' : 'Settings');
}

function loadOwnership() {
  if (ownershipLoading) return;
  ownershipLoading = true;
  api('/api/ownership').then(result => { ownership = result.ownership; if (view === 'connect') render(); })
    .catch(error => notify(error.message)).finally(() => { ownershipLoading = false; });
}

function loadDirectory() {
  if (directoryLoading) return;
  directoryLoading = true;
  api('/api/people').then(result => { directory = result; if (['owners', 'connect'].includes(view)) render(); })
    .catch(error => notify(error.message)).finally(() => { directoryLoading = false; });
}

// Old bookmarks keep working: #memory is the inbox's Decided tab, and #work
// without a task is the task list unless this server runs tasks in the browser.
function navigate() {
  navigationVersion += 1;
  const parts = location.hash.slice(1).split('/');
  let next = parts[0];
  if (next === 'memory') { tab = 'decided'; tabChosen = true; next = 'inbox'; history.replaceState(null, '', location.pathname + location.search + '#inbox'); }
  if (next === 'work' && !parts[1] && state.auth && !state.workbench?.ready) { next = 'runs'; history.replaceState(null, '', location.pathname + location.search + '#runs'); }
  view = titles[next] ? next : 'inbox';
  query = ''; ownerFilter = ''; ownership = null; directory = null;
  const [title] = titles[view];
  $('#page-title').textContent = title;
  document.title = view === 'work' ? 'Raven' : `Raven · ${title}`;
  announce(view === 'work' ? 'Start a task' : title);
  document.querySelectorAll('nav a').forEach(link => {
    link.classList.toggle('active', link.dataset.view === view);
    if (link.dataset.view === view) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  taskTab = typeof taskView === 'function' && taskId() ? taskView().tab : 'overview';
  if (typeof taskRequest !== 'undefined') taskRequest += 1;
  taskDetail = null; taskError = '';
  if (taskId()) loadTask();
  if (view === 'work' && typeof loadWorkbench === 'function') loadWorkbench();
  if (view === 'today' && typeof loadObjectives === 'function') loadObjectives();
  render();
  if (resetScrollOnNavigation && typeof window.scrollTo === 'function') window.scrollTo(0, 0);
  resetScrollOnNavigation = false;
}
// What changed, said once through one small live region. The page body is
// not one: with #app live, every redraw (each search keystroke, each
// changed poll) re-read the whole page and drowned the status lines.
const announce = text => { const live = $('#announce'); if (live) live.textContent = text; };

function metrics() {
  const pending = state.decisions.filter(needsYou);
  const active = state.runs.filter(r => !['completed','result_ready','failed','cancelled','review_required'].includes(r.status));
  const approved = state.decisions.filter(d => settledStatuses.includes(d.status));
  const predictions = state.decisions.filter(d => (d.status === 'pending' && d.prediction) || ['assumed','proposed'].includes(d.status));
  return `<div class="metrics">${[
    ['Awaiting judgment', state.counts ? state.counts.needs_you : pending.length, state.counts && state.counts.overdue ? `${state.counts.overdue} open longer than three days` : 'Questions and sign-offs for a person', 'clock'],
    ['Active tasks', active.length, 'Tasks in progress', 'activity'],
    ['Decisions captured', approved.length, 'Signed or resolved from evidence', 'layers'],
    ['Suggested answers', predictions.length, 'Awaiting owner review', 'spark'],
  ].map(([label, count, note, glyph]) => `<div class="metric"><div class="metric-label">${label}${icon(glyph)}</div><div class="metric-number">${count}</div><div class="metric-note ${glyph === 'layers' ? 'green' : ''}">${note}</div></div>`).join('')}</div>`;
}

// Text a person decides from is shown whole: folded after a few lines with
// a Show more button, never cut off. A short text has no button. The fold
// is a class the button toggles; `shown` keeps the choice across redraws.
const shown = new Set();
function clamped(text, cls, key) {
  const long = String(text || '').length > 220;
  const folded = long && !shown.has(key);
  return `<p class="${cls}${folded ? ' is-clamped' : ''}">${esc(text || '')}</p>${long ? `<button type="button" class="button text more" data-action="more" data-key="${esc(key)}">${folded ? 'Show more' : 'Show less'}</button>` : ''}`;
}

// The one box a card or the dialog shows for what is on the table: a
// suggestion, a default, evidence, the agent's own answer, or an answer a
// person stands behind. Label and body only; the pill says the state.
function answerBox(d, actions = '') {
  const body = d.answer || d.prediction || '';
  let label = '', tone = 'prediction';
  if (d.needs_review || d.source_refresh_required) label = `Needs a second look · ${d.review_reason || d.source_notice || 'an answer it leaned on was corrected'}`;
  else if (d.status === 'approved') { label = `Answered by ${d.answered_by || d.owner_name || 'a person'}`; tone = ''; }
  else if (['assumed', 'proposed'].includes(d.status) && signedNow(d)) { label = `Signed by ${d.signed_by || nameList(signersOf(d)) || 'a person'} · began as ${beganAs(d)}`; tone = ''; }
  else if (halfSigned(d)) label = `Signed by ${nameList(signersOf(d))}, still waiting on ${nameList(stillToSign(d)) || 'a required approver'}`;
  else if (d.status === 'resolved' || d.status === 'partial') { label = d.kind === 'answer' ? `Answered by ${d.answered_by || 'a person'}` : d.kind === 'agent' ? 'The agent decided' : 'Found in evidence'; tone = signedNow(d) ? '' : 'prediction'; }
  else if (d.status === 'assumed') label = 'Default used';
  else if (d.status === 'proposed' || (d.status === 'pending' && d.prediction)) label = predictionLabel(d);
  if (!label || !body) return '';
  return `<div class="context-box ${tone}"><span class="label">${esc(label)}</span>${esc(body)}${actions}</div>`;
}

function decisionCard(d, index) {
  // An objective's proposed scope is stored as JSON; the card reads it as a scope, never as JSON.
  const scope = typeof objectiveScope === 'function' ? objectiveScope(d) : null;
  const settled = settledNow(d);
  const overdueMs = ((state.settings && state.settings.overdue_hours) || 72) * 3600e3;
  const age = Date.now() - new Date(d.created_at).getTime();
  const open = d.status === 'pending' && age > overdueMs ? `<span class="muted">open ${plural(Math.floor(age / 864e5), 'day')}</span>` : '';
  const wantsSign = d.needs_review || (d.signoff === 'required' && d.status !== 'pending');
  const button = scope ? 'Review and accept' : wantsSign ? 'Sign' : settled ? 'View' : 'Answer';
  const taskHref = `#${(state.browser_tasks || []).some(t => t.task_id === d.run_id) ? 'work' : 'runs'}/${encodeURIComponent(d.run_id || '')}`;
  return `<article class="decision-card">
    <div class="card-top"><span class="agent-symbol">${(d.agent || '').includes('Claude') ? '✳' : (d.agent || '').includes('Codex') ? '⌘' : '◇'}</span><span>${esc(d.agent)}</span><span>·</span><span class="repo">${esc(d.repo)}</span><time datetime="${esc(d.created_at)}">${ago(d.created_at)}</time>${open}</div>
    <h2>${esc(d.question)}</h2>${d.run_title ? `<a class="card-task" href="${esc(taskHref)}" data-action="run" data-id="${esc(d.run_id)}">${esc(d.run_title)}</a>` : ''}
    ${scope ? objectiveScopeCard(scope, `scope-${d.id}`) : clamped(d.context, 'context', `card-${d.id}`)}
    ${answerBox(d)}
    <div class="card-bottom"><span class="owner" title="${esc(d.owner_name || 'No owner yet')}">${avatar(d.owner_name, index)}</span>${statusPill(d)}<button class="button small ${settled ? '' : 'soft'}" data-action="review" data-id="${esc(d.id)}" aria-label="${button}: ${esc(d.question)}">${button}</button></div>
  </article>`;
}

const eventLabels = {signed_in:'Person signed in', conformance_read:'Code review saved', task_note:'Context added', expand:'Decision explored', run_complete:'Evidence search finished', memory_reranked:'Prior answers compared', brief_withheld:'Unreliable brief withheld',task_started:'Task kicked off', node_added:'Node written to the tree', node_settled:'Settled by the agent', followup_added:'Follow-up question added', signoff:'Signed off', run_started:'Task registered', judgment_requested:'Judgment requested', prediction_suggested:'Prior answer suggested', owner_approved:'Decision recorded', answer_corrected:'Answer corrected', previous_answer:'Previous answer retained', owner_changed:'Owner reassigned', run_updated:'Run status updated', resolve:'Resolved from evidence', assume:'Default assumed', proposed:'Unratified proposal found', dedup_open:'Duplicate of an open question', twin_closed:'Twin question settled', route_unknown:'No owner in the graph', ask_drafted:'Routed from the ownership graph', index:'Repository ingested', conflict_resolved:'Newest answer won a conflict', memory_record_conflict:'Memory and record disagree', superseded:'Superseded', superseded_record:'Stale record replaced by newer', ownership_invalidated:'Ownership changed', followup_context:'Follow-up context resolved', route_learned:'Owner learned', overturn:'Answer overturned', answer:'Answer recorded'};
const quietEvents = new Set(['probe','expand','decompose','run_complete','previous_answer','memory_noop','memory_updated']);
// A signature, or a rule a person made, is what authorizes an answer; where the answer came from
// (evidence, a prediction, an assumed default) stays visible, and never reads as unconfirmed once signed.
const signedNow = d => ['signed','rule'].includes(d.signoff) && !d.needs_review;
// How a suggested answer relates to what it leans on, and the scope that answer was given for when this
// request is outside it: "the same policy" and "an earlier decision" must read differently to the owner.
const predictionScope = d => { const m = /(?:^|; )prediction scope: ([^;]+)/.exec(d.evidence || ''); return m ? m[1].trim() : ''; };
const predictionLabel = d => /how they decide \(the same policy/.test(d.evidence || '') ? 'Your earlier policy, applied here' : 'Suggested answer · from an earlier decision';
const beganAs = d => d.kind === 'agent' ? 'the agent’s own answer' : d.status === 'assumed' ? 'a default Raven assumed' : d.status === 'proposed' ? 'a prediction from an earlier answer' : d.status === 'partial' ? 'a partial answer from evidence' : 'an answer from evidence';
// Who a decision still waits on: each required approver without a
// signature on it. Measured live: a decision its owner had answered read
// "Resolved from evidence", with no word of the reviewer it still needed.
const listOf = raw => { try { const v = Array.isArray(raw) ? raw : JSON.parse(raw || '[]'); return Array.isArray(v) ? v : []; } catch { return []; } };
const signersOf = d => listOf(d.signatures).map(s => String(s?.by || '')).filter(Boolean);
const stillToSign = d => { const done = signersOf(d).map(n => n.toLowerCase()); return listOf(d.required_signers).map(String).filter(n => !done.includes(n.toLowerCase())); };
const halfSigned = d => d.signoff === 'required' && signersOf(d).length > 0;
// At most two names, then how many more: 'Alex Morgan, Wes Chen and 1 more'.
function nameList(names) {
  const unique = [...new Set(names.filter(Boolean))];
  return unique.length > 2 ? `${unique[0]}, ${unique[1]} and ${unique.length - 2} more` : unique.join(', ');
}

// Status is one sentence that names a person, never a count or a term:
// the same words on the card, the task page and the dialog. Two names at
// most, then "and N more"; an empty name is never rendered.
function statusSentence(d) {
  if (d.needs_review || d.source_refresh_required) return {text: 'Needs a second look', tone: 'waiting'};
  if (d.status === 'suggested') return {text: 'Follow-up for the agent', tone: 'neutral'};
  if (d.status === 'adopted') return {text: 'Adopted by the agent', tone: 'neutral'};
  if (d.status === 'duplicate') return {text: 'Duplicate', tone: 'neutral'};
  if (d.status === 'pending') return {text: d.owner_name ? `Waiting on ${d.owner_name}` : 'No owner yet', tone: 'waiting'};
  if (d.signoff === 'rule') return {text: 'Covered by a rule', tone: 'done'};
  if (d.signoff === 'signed') { const by = d.signed_by || nameList(signersOf(d)); return {text: by ? `Signed by ${by}` : 'Signed', tone: 'done'}; }
  if (halfSigned(d)) { const left = stillToSign(d); return {text: `Signed by ${nameList(signersOf(d))}${left.length ? `, waiting on ${nameList(left)}` : ''}`, tone: 'waiting'}; }
  if (d.status === 'approved') return {text: `Answered by ${d.answered_by || d.owner_name || 'a person'}`, tone: 'done'};
  if (needsYou(d)) return {text: d.owner_name ? `Sign-off wanted from ${d.owner_name}` : 'No owner yet', tone: 'waiting'};
  const word = String(d.status || '').replaceAll('_', ' ');
  return {text: word ? word[0].toUpperCase() + word.slice(1) : 'Recorded', tone: 'neutral'};
}
const statusPill = d => { const s = statusSentence(d); return pill(s.text, s.tone); };
const settledStatuses = ['approved','resolved','partial'];
const settledNow = d => !needsYou(d) && (settledStatuses.includes(d.status) || signedNow(d));
// A person is needed for an open question, for a node Raven or the agent resolved without one, and for a
// node put in doubt by a correction upstream. Evidence is not authorization.
const needsYou = d => d.status === 'pending' || !!d.needs_review || (['resolved','partial','assumed','proposed'].includes(d.status) && !['signed','rule'].includes(d.signoff));
// What is assigned to the signed-in person is what the server says they may act on, by person id.
// Measured: an invited member who shared a seeded owner's name saw "Assigned to me 2" and could act on neither.
const assignedToMe = d => needsYou(d) && d.can_act === true;
// A signed-in member's inbox is theirs: the badge counts what waits on
// them and the inbox opens on it. Measured: a person who had just made an
// account from a task link, with nothing waiting on him, saw "Inbox 1" and
// someone else's decision with a Review button. Admins and viewers keep
// the workspace view.
const personalInbox = () => Boolean(state?.auth?.enabled && state.me?.id && state.me.role === 'member');
function activity() {
  const items = state.events.filter(e => !quietEvents.has(e.kind)).slice(0, 5);
  return `<div class="side-section"><div class="side-section-head">Across your workspace <a href="#runs">View runs ↗</a></div><div class="activity-list">${items.length ? items.map(event => {
    const decision = state.decisions.find(d => d.id === event.decision_id);
    const run = state.runs.find(r => r.id === event.run_id);
    return `<div class="activity-item"><span class="activity-symbol">${icon(event.kind.includes('approved') ? 'check' : event.kind.includes('prediction') ? 'spark' : 'activity')}</span><div class="activity-text"><strong>${esc(eventLabels[event.kind] || event.kind)}</strong>${esc(decision ? (decision.owner_name || decision.question.slice(0, 60)) : run?.title || 'Workspace updated')}<small>${ago(event.created_at)}</small></div></div>`;
  }).join('') : '<p class="context">Activity appears as your agents start working.</p>'}</div></div>`;
}

function empty(title, description = '', action = '', button = '') {
  return `<div class="empty">${icon('inbox')}<h2>${esc(title)}</h2>${description ? `<p>${esc(description)}</p>` : ''}${action ? `<button class="button soft" data-action="${action}">${esc(button)}</button>` : ''}</div>`;
}

// An empty tab says what it would hold. Measured in Chromium: an empty
// Assigned-to-me tab read "You’re all caught up" above a workspace with
// four open decisions.
function emptyInbox(query, tab) {
  if (query || (tab === 'decided' && ownerFilter)) return empty('No matching decisions.', 'Try a different search.');
  if (tab === 'mine') return empty('Nothing waits on you.');
  if (tab === 'decided') return empty('No decisions recorded yet.');
  return state.decisions.length ? empty('Nothing needs a person.', 'When an agent needs a person, it appears here.')
    : empty('Nothing needs a person.', 'When an agent needs a person, it appears here.', 'help-connect', 'Open Settings');
}

const decidedNow = d => settledStatuses.includes(d.status) && !d.superseded_by;
function inbox() {
  const tabs = [...(state.auth?.enabled && state.me?.role !== 'viewer' ? [['mine', 'Needs me', state.decisions.filter(assignedToMe).length]] : []),
    ['pending', 'All open', state.decisions.filter(needsYou).length], ['decided', 'Decided', state.decisions.filter(decidedNow).length]];
  if (!tabs.some(([key]) => key === tab)) tab = personalInbox() ? 'mine' : 'pending';
  const q = query.toLowerCase();
  const rows = tab === 'decided'
    ? state.decisions.filter(d => decidedNow(d) && (!ownerFilter || d.owner_id === ownerFilter) && `${d.question} ${d.answer} ${d.rationale} ${d.owner_name} ${d.evidence}`.toLowerCase().includes(q))
      .sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')))
    : state.decisions.filter(d => (tab === 'mine' ? assignedToMe(d) : needsYou(d)) && `${d.question} ${d.context} ${d.owner_name}`.toLowerCase().includes(q));
  inboxShown = rows.length;
  return `<div class="toolbar">${tabs.map(([key, label, count]) => `<button class="tab ${tab === key ? 'active' : ''}" data-action="tab" data-tab="${key}" aria-pressed="${tab === key}">${label}<span>${count}</span></button>`).join('')}<label class="search" aria-label="Search decisions">${icon('search')}<input id="inbox-search" type="search" placeholder="Search" value="${esc(query)}"></label>${tab === 'decided' ? `<select id="owner-filter" aria-label="Filter by owner"><option value="">All owners</option>${state.owners.map(o => `<option value="${esc(o.id)}" ${ownerFilter === o.id ? 'selected' : ''}>${esc(o.name)}</option>`).join('')}</select>` : ''}</div>
    <div class="decision-list">${rows.length ? rows.map(decisionCard).join('') : emptyInbox(query, tab)}</div>`;
}

function durationLabel(seconds) {
  if (seconds == null) return '—';
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return '<1m';
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 360) / 10}h`.replace(/\.0h$/, 'h');
  return `${Math.round(s / 8640) / 10}d`.replace(/\.0d$/, 'd');
}

function loadSavings() {
  if (savingsLoading) return;
  savingsLoading = true;
  api('/api/metrics?days=30').then(result => { savings = result; savingsAt = Date.now(); if (view === 'connect') render(); })
    // The numbers are a member's to read; a viewer gets no strip and no error.
    .catch(error => { if (error.status === 403) { savings = false; savingsAt = Date.now(); if (view === 'connect') render(); } else notify(error.message); })
    .finally(() => { savingsLoading = false; });
}

function savedStrip() {
  // One line of numbers from the ledger: what Raven reused, how proposals fared, how long people took.
  if (savings === false) return '';
  if (savings === null || Date.now() - savingsAt > STALE_DRAW_MS) loadSavings();
  if (!savings) return '<p class="saved-strip context" role="status">Measuring what Raven saved…</p>';
  const m = savings, decided = m.proposals.signed_unchanged + m.proposals.corrected;
  if (!m.decisions) return `<p class="saved-strip context" role="status"><strong>What Raven saved</strong> · last ${m.window_days} days · no decisions recorded yet.</p>`;
  const parts = [`<strong>${m.reuse.total}</strong> of ${m.decisions} answered from memory or a rule`];
  if (decided) parts.push(`<strong>${m.proposals.signed_unchanged} of ${decided}</strong> proposals signed unchanged`);
  if (m.time_to_answer.count) parts.push(`answers in <strong>${durationLabel(m.time_to_answer.median_seconds)}</strong> median, ${durationLabel(m.time_to_answer.p90_seconds)} p90`);
  if (m.handon.routed) parts.push(`<strong>${m.handon.handed_on} of ${m.handon.routed}</strong> handed on`);
  if (m.reask.asked) parts.push(`<strong>${m.reask.repeated}</strong> asked again after an answer existed`);
  if (m.wait_saved.decisions) parts.push(`<strong>${m.wait_saved.estimated ? '≈' : ''}${durationLabel(m.wait_saved.seconds)}</strong> of agent waiting avoided`);
  return `<p class="saved-strip" role="status"><strong>What Raven saved</strong> · last ${m.window_days} days · ${parts.join(' · ')}</p>`;
}

// The one sentence a task row carries: who it waits on, else its lifecycle.
function runStatusSentence(run, decisions) {
  const pending = decisions.filter(needsYou);
  if (pending.length) {
    const names = nameList(pending.map(d => d.owner_name));
    return {text: names ? `Waiting on ${names}` : 'No owner yet', tone: 'waiting'};
  }
  const text = runStatusLabel(run.status);
  return {text, tone: text === 'Done' ? 'done' : 'neutral'};
}

// One row per task: the whole row is the link, with one pill and no inner buttons.
function taskRow(run, href = `#runs/${encodeURIComponent(run.id)}`) {
  const status = runStatusSentence(run, state.decisions.filter(d => d.run_id === run.id));
  const repos = new Set(state.runs.map(r => r.repo));
  const meta = [repos.size > 1 ? run.repo : '', run.agent, ago(run.updated_at)].filter(Boolean).map(esc).join(' · ');
  return `<a class="task-row" href="${href}"><div><strong>${esc(run.title)}</strong><span class="muted">${meta}</span></div>${pill(status.text, status.tone)}</a>`;
}

function runs() {
  const browser = new Map((state.browser_tasks || []).map(t => [t.task_id, t]));
  const rows = state.runs.map(run => taskRow(run, browser.has(run.id) ? `#work/${encodeURIComponent(run.id)}` : undefined));
  for (const t of browser.values()) {
    if (state.runs.some(run => run.id === t.task_id)) continue;
    rows.push(taskRow({id: t.task_id, title: t.prompt, repo: t.repository, agent: 'Browser', status: t.status, updated_at: t.updated_at}, `#work/${encodeURIComponent(t.task_id)}`));
  }
  const viewer = state.auth?.enabled && state.me?.role === 'viewer';
  const member = !state.auth?.enabled || ['admin', 'member'].includes(state.me?.role);
  const start = viewer ? '' : state.execution_config?.enabled ? '<button class="button primary" data-action="task">Start a task</button>'
    : state.workbench?.ready ? '<a class="button primary" href="#work">Start a task</a>' : '';
  // Outcomes stays reachable without a model backend while something there waits on a person.
  const todayCount = (state.today?.proposals_open || 0) + (state.today?.objectives_needing_you || 0);
  const outcomes = state.model?.proposals !== false || todayCount > 0;
  const items = [member ? '<button class="button text" data-action="new">Ask a question</button>' : '', outcomes ? '<a href="#today">Outcomes</a>' : ''].filter(Boolean);
  const actions = $('#page-actions');
  if (actions) actions.innerHTML = `<span class="page-count">${plural(rows.length, 'task')}</span>${start}${items.length ? `<details class="more-menu"><summary aria-label="More actions">More</summary><div class="more-menu-items">${items.join('')}</div></details>` : ''}`;
  return rows.length ? `<div class="task-list">${rows.join('')}</div>` : empty('No tasks yet.', 'Connect a coding agent in Settings and give it a task.', 'help-connect', 'Open Settings');
}

function ownershipGraph() {
  if (ownership === null) loadOwnership();
  const rows = ownership || [];
  const repos = [...new Set(rows.map(r => r.repo))];
  const table = rows.length ? `<p class="muted">${plural(repos.length, 'repository', 'repositories')} · ${plural(rows.length, 'signal')}</p><div class="table-wrap" role="region" aria-label="Ownership signals" tabindex="0"><table><thead><tr><th>REPO</th><th>PATH</th><th>ENGINEER</th><th>SIGNAL</th><th>WEIGHT</th><th>EVIDENCE</th></tr></thead><tbody>${rows.map(r => `<tr><td>${esc(r.repo)}</td><td><code>${esc(r.path_prefix || '(repo-wide)')}</code></td><td>${esc(r.engineer)}</td><td>${esc(r.source)}</td><td>${Number(r.weight).toFixed(2)}</td><td><small>${esc(r.evidence)}</small></td></tr>`).join('')}</tbody></table></div>` : ownership === null ? '<p class="context">Loading…</p>' : '<p class="context">No repositories yet.</p>';
  const syncRepos = (state.sync && state.sync.repos) || [];
  const synced = syncRepos.map(s => `<li><strong>${esc(s.repo)}</strong><span>${s.last_error ? esc(s.last_error) : s.last_success_at ? 'Synced ' + ago(s.last_success_at) : 'Sync pending'}</span></li>`).join('');
  return `${synced ? `<details class="sync-details" id="github-sync-status"><summary>GitHub sync status · ${plural(syncRepos.length, 'repository', 'repositories')}</summary><ul>${synced}</ul></details>` : ''}${table}`;
}

const roleLabels = {knows:'knows', decides:'decides', approves:'must approve'};
// Where an authority row came from, as a person reads it.
const sourceLabels = {answer: 'an answer', referral: 'a hand-on'};

const canAdminister = () => state.auth?.enabled === false || state.me?.role === 'admin';
const sameName = (a, b) => String(a || '').trim().toLowerCase() === String(b || '').trim().toLowerCase();
// A row whose date has passed decides nothing; it read the same as a live
// one, which is how a map can look set up and route nowhere.
const authorityEnded = a => Boolean(a.effective_to) && a.effective_to.slice(0, 10) < new Date().toISOString().slice(0, 10);
const authorityOf = (a, p) => a.person_id ? a.person_id === p.id : !a.is_team && sameName(a.who, p.name);

// One line per authority row: what they decide, where, and where the record
// came from. Only a live row can be ended; an ended one stays, muted.
function authorityRow(a, who = '') {
  const ended = authorityEnded(a);
  const parts = [`<strong>${esc(roleLabels[a.role] || a.role)} ${esc(a.scope_kind === 'repo' ? 'everything' : a.scope)}</strong>`,
    esc(a.repo || 'every repository'), `from ${esc(sourceLabels[a.source] || a.source)}`];
  if (a.asserted_by) parts.push(`asserted by ${esc(a.asserted_by)}`);
  if (a.note) parts.push(esc(a.note));
  if (!a.accepted) parts.push('(not yet accepted)');
  if (ended) parts.push(`ended ${esc(a.effective_to.slice(0, 10))}`);
  if (who) parts.unshift(esc(who));
  const what = `${roleLabels[a.role] || a.role} ${a.scope_kind === 'repo' ? 'everything' : a.scope}`;
  const end = canAdminister() && !ended ? `<button class="button small soft" data-action="end-authority" data-id="${esc(a.id)}" aria-label="End: ${esc(a.who || 'this record')} ${esc(what)} in ${esc(a.repo || 'every repository')}">End</button>` : '';
  return `<div class="authority-row${ended ? ' ended' : ''}"><span>${parts.join(' · ')}</span>${end}</div>`;
}

function authorityRows(rows, who) {
  if (!rows.length) return '<p class="muted">Nothing recorded yet</p>';
  const live = rows.filter(a => !authorityEnded(a)), ended = rows.filter(authorityEnded);
  return live.concat(ended).map(a => authorityRow(a, who ? `${a.who || 'Unknown'}${a.is_team ? ' (team)' : ''}` : '')).join('');
}

// A person, what the pending decisions wait on them for, and every authority
// row recorded for them. Authority ids and data-action=end-authority are the
// table's; only where they are shown changed.
function personCard(p, index, rows = []) {
  const team = `${esc(p.team || '')}${p.teams && p.teams.length ? `${p.team ? ' · ' : ''}${esc(p.teams.join(', '))}` : ''}`;
  const identities = [p.email, p.github_login ? '@' + p.github_login : '', p.slack_id ? 'Slack ' + p.slack_id : ''].filter(Boolean).map(esc).join(' · ');
  const waiting = state.decisions.filter(d => d.status === 'pending' && (d.owner_id === p.id || sameName(d.owner_name, p.name))).length;
  return `<article class="person-card${p.active ? '' : ' inactive'}">${avatar(p.name, index)}<h2>${esc(p.name)}</h2>${team ? `<p>${team}</p>` : ''}${identities ? `<small>${identities}</small>` : ''}${p.active ? '' : '<small>inactive</small>'}${waiting ? `<p class="person-waiting">Waiting on them: ${waiting}</p>` : ''}<h3>Decides</h3>${authorityRows(rows)}${canAdminister() ? `<button class="button text" data-action="authority" data-person="${esc(p.id)}" aria-label="Say what they decide: ${esc(p.name)}">Say what they decide</button>` : ''}</article>`;
}

// Rows the directory records for a team or a name no person card carries:
// shown under one last card so no record disappears with the table.
function otherCard(rows, index) {
  return `<article class="person-card">${avatar('Other', index)}<h2>Other</h2><h3>Decides</h3>${authorityRows(rows, true)}</article>`;
}

function owners() {
  const actions = $('#page-actions');
  if (directory === null) {
    loadDirectory();
    return '<p class="context" role="status">Loading…</p>';
  }
  const people = directory.people || [], rows = directory.authority || [];
  if (actions) actions.innerHTML = `<span class="page-count muted">${plural(people.length, 'person', 'people')}</span>${canAdminister() ? '<button class="button primary" data-action="person">Add person</button><details class="more-menu"><summary aria-label="More actions">More</summary><div class="more-menu-items"><button class="button text" data-action="team">Add team</button></div></details>' : ''}`;
  if (!people.length && !rows.length) return empty('Nobody yet.', 'Add the people who decide, or connect a repository in Settings and Raven finds them.', canAdminister() ? 'person' : '', 'Add person');
  const shown = new Set();
  const cards = people.map((p, i) => {
    const mine = rows.filter(a => !shown.has(a.id) && authorityOf(a, p));
    mine.forEach(a => shown.add(a.id));
    return personCard(p, i, mine);
  });
  const other = rows.filter(a => !shown.has(a.id));
  if (other.length) cards.push(otherCard(other, people.length));
  return `<div class="people-grid">${cards.join('')}</div>`;
}

function newPerson() {
  openModal('Add a person', '', `<form id="person-form"><label for="person-name">Name</label><input id="person-name" name="name" maxlength="100" placeholder="Marisol Vega" required><div class="form-row"><div><label for="person-email">Email</label><input id="person-email" name="email" maxlength="200" placeholder="marisol@acme.example"></div><div><label for="person-team">Team</label><input id="person-team" name="team" maxlength="100" placeholder="Pricing"></div></div><div class="form-row"><div><label for="person-github">GitHub login</label><input id="person-github" name="github_login" maxlength="100" placeholder="mvega"></div><div><label for="person-slack">Slack member id</label><input id="person-slack" name="slack_id" maxlength="100" placeholder="U0123ABC"></div></div><label for="person-aliases">Other names, emails or handles (comma separated)</label><input id="person-aliases" name="aliases" maxlength="1000" placeholder="M. Vega, marisol@old.example"><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button type="button" class="button" data-action="close">Cancel</button><button class="button primary" type="submit">Add person</button></div></form>`);
}

function newTeam() {
  openModal('Add a team', '', `<form id="team-form"><label for="team-name">Name</label><input id="team-name" name="name" maxlength="100" placeholder="Payments" required><label for="team-handle">Handle as CODEOWNERS writes it</label><input id="team-handle" name="handle" maxlength="100" placeholder="acme/payments-team"><label for="team-members">Members (names, emails or logins, comma separated)</label><input id="team-members" name="members" maxlength="2000" placeholder="Marisol Vega, wes@acme.example"><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button type="button" class="button" data-action="close">Cancel</button><button class="button primary" type="submit">Add team</button></div></form>`);
}

// The authority form, with the person whose card opened it already chosen.
function newAuthority(personId = '') {
  const people = (directory && directory.people) || [];
  const teams = (directory && directory.teams) || [];
  openModal('Say what they decide', '', `<form id="authority-form"><label for="authority-who">Person or team</label><select id="authority-who" name="who" required>${people.map(p => `<option value="person:${esc(p.id)}"${personId && p.id === personId ? ' selected' : ''}>${esc(p.name)}</option>`).join('')}${teams.map(t => `<option value="team:${esc(t.id)}">Team · ${esc(t.name)}</option>`).join('')}</select><div class="form-row"><div><label for="authority-role">Role</label><select id="authority-role" name="role"><option value="decides">decides</option><option value="approves">must approve</option><option value="knows">knows the area</option></select></div><div><label for="authority-kind">Scope</label><select id="authority-kind" name="scope_kind"><option value="path">path pattern</option><option value="category">decision category</option><option value="repo">whole repository</option></select></div></div><label for="authority-scope">Path pattern or category</label><input id="authority-scope" name="scope" maxlength="300" placeholder="billing/* or pricing"><div class="form-row"><div><label for="authority-repo">Repository (blank for every repository)</label><input id="authority-repo" name="repo" maxlength="300" placeholder="acme/platform"></div><div><label for="authority-until">Until (optional date)</label><input id="authority-until" name="effective_to" maxlength="40" placeholder="2027-01-01"></div></div><label for="authority-note">Note</label><input id="authority-note" name="note" maxlength="500" placeholder="Owns commercial credit exceptions"><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button type="button" class="button" data-action="close">Cancel</button><button class="button primary" type="submit">Save</button></div></form>`);
}

function deliveriesCard() {
  // Card 3: Slack. The inbox works without it, so the card says so in one line.
  const d = state.delivery || {};
  const head = '<h2><span class="step">3</span>Slack</h2>';
  const line = '<p>People can answer in the inbox without it.</p>';
  if (!d.enabled) return `<section class="setup-card" id="settings-slack">${head}<p>${pill('Not connected', 'gray')}</p>${line}<p><a href="https://github.com/nintendo-ds75/raven-preview/blob/main/docs/slack.md" target="_blank" rel="noopener">How to connect ↗</a></p><details class="history" id="slack-bot"><summary>Server bot (optional)</summary><p>An operator can configure a bot for notification-only delivery. Workspace app policies may require approval.</p><a href="https://github.com/nintendo-ds75/raven-preview/blob/main/docs/slack.md" target="_blank" rel="noopener">Bot setup</a></details></section>`;
  const failed = (d.failed || 0);
  const contacts = d.directory?.synced_at ? ` · ${plural(d.directory.people || 0, 'contact')} synced` : '';
  return `<section class="setup-card" id="settings-slack">${head}<p>${pill('Connected' + contacts, 'green')}</p>${line}${d.directory?.error ? `<p class="error">${esc(d.directory.error)}</p>` : ''}<button class="button soft" data-action="slack-sync">Refresh Slack contacts</button><p>${d.sent || 0} sent · ${d.queued || 0} queued · ${failed} failed${d.signing_secret ? '' : ' · signed source-change callbacks are not configured'}</p><div class="form-row"><div><label for="fallback-channel">Triage channel (when no contact can be reached)</label><input id="fallback-channel" maxlength="100" placeholder="C0123ABC" value="${esc(d.fallback_channel || '')}"></div><div><button class="button" data-action="fallback">Save</button></div></div>${failed ? `<button class="button soft" data-action="deliveries">Show failed deliveries</button><div id="deliveries"></div>` : ''}</section>`;
}

function githubCard() {
  const github = state.github_app || {};
  const repos = github.repositories || [];
  const canConnect = !!state.github_app && state.me && state.me.role === 'admin';
  const githubOn = !!github.configured && repos.length > 0 && (github.mode !== 'device' || !!github.authorized);
  // A server GITHUB_TOKEN syncs without the GitHub App. The row reads the
  // sync itself: which repositories, when each last synced, and what failed.
  const synced = (state.sync && state.sync.repos) || [];
  const tokenSync = !!(state.sync && state.sync.github) && !githubOn;
  const tokenOk = tokenSync && synced.some(r => r.last_success_at);
  const on = githubOn || tokenOk;
  const failing = synced.filter(r => r.last_error);
  const status = tokenOk ? '<p>Syncing with the server’s GITHUB_TOKEN: pull requests, reviews and ownership evidence come into Raven.</p>'
    : tokenSync ? '<p>The server has a GITHUB_TOKEN; no repository has synced yet.</p>' : '';
  const appList = repos.length ? `<details class="github-repositories" id="github-repositories"><summary><strong>${repos.length}</strong> ${githubOn ? 'connected' : 'selected'} ${repos.length === 1 ? 'repository' : 'repositories'}<span>View repositories</span></summary><ul>${repos.map(repo => `<li><span class="repo-owner">${esc(repo.split('/')[0])} /</span> <strong>${esc(repo.split('/').slice(1).join('/') || repo)}</strong></li>`).join('')}</ul></details>` : '';
  const syncList = !repos.length && synced.length ? `<details class="github-repositories" id="github-repositories"><summary><strong>${synced.length}</strong> synced ${synced.length === 1 ? 'repository' : 'repositories'}<span>View repositories</span></summary><ul>${synced.map(r => `<li><span class="repo-owner">${esc(r.repo.split('/')[0])} /</span> <strong>${esc(r.repo.split('/').slice(1).join('/') || r.repo)}</strong> <small>${r.last_error ? `failing: ${esc(r.last_error)}` : r.last_success_at ? `synced ${esc(ago(r.last_success_at))}` : 'not synced yet'}</small></li>`).join('')}</ul></details>` : '';
  return `<div class="github-heading"><h3>GitHub</h3>${pill(on ? 'Connected' : 'Not connected', on ? 'green' : 'gray')}</div>${status}${appList || syncList || '<p>No repositories yet.</p>'}${failing.length ? `<p class="error">${failing.length} ${failing.length === 1 ? 'repository is' : 'repositories are'} failing to sync: ${esc(failing[0].repo)}: ${esc(failing[0].last_error)}</p>` : ''}${!github.configured && !tokenSync ? '<p>GitHub connection is awaiting setup by the Raven operator.</p>' : ''}${canConnect && github.configured ? `<button class="button${githubOn ? ' soft' : ''}" data-action="github-connect">${repos.length ? 'Manage repositories' : 'Connect GitHub'}</button>` : ''}`;
}

function agentStatus() {
  const tokens = state.agent_connections || [];
  return tokens.length ? tokens.map(t => `<p>${esc(t.label)} · ${pill(t.last_used_at ? 'Activity detected' : 'Waiting for first use', t.last_used_at ? 'green' : 'gray')}${t.last_used_at ? ` · Last seen ${esc(ago(t.last_used_at))}` : ''}</p>`).join('') : '<p>No active agent credentials yet.</p>';
}

// What is not set up yet, one line each with its fix. The server's notices
// are shown as it wrote them; the level is a word, not a colour. A model
// notice is a gap only without a backend that can make suggestions; with a
// key configured it is information, and not something to fix.
function setupChecklist() {
  const item = (level, text) => `<li><strong>${esc(level)}</strong><span>${esc(text)}</span></li>`;
  const slackLine = state.delivery?.enabled === false;
  const items = [
    state.model?.proposals === false && state.model.notice ? item('Worth fixing', state.model.notice) : '',
    slackLine ? item('Note', 'Slack not connected: people answer in the inbox.') : '',
    readiness(slackLine ? ['no_slack'] : []),
  ].join('');
  return items ? `<h2 id="setup-checklist-title">Not set up yet</h2><ul class="setup-list" aria-labelledby="setup-checklist-title">${items}</ul>` : '';
}

function readiness(skip = []) {
  // The server's readiness findings, in the order they bite, as checklist
  // rows: the level word, what is missing, what to do.
  const label = {blocker: 'Set this up first', warning: 'Worth fixing', warn: 'Worth fixing', note: 'Note', info: 'Note'};
  return (state.readiness || []).filter(r => r && !skip.includes(r.key))
    .map(r => `<li><strong>${esc(label[r.level] || r.level)}</strong><span>${esc([r.what, r.do].filter(Boolean).join(' '))}</span></li>`).join('');
}

function connections() {
  const shared = !!state.auth?.enabled;
  const step = (n, title) => `<h2><span class="step">${n}</span>${title}</h2>`;
  let agentStep;
  if (shared) {
    agentStep = `<section class="setup-card">${step(1, 'Connect your coding agent')}<p>Pick your client.</p>${agentCards()}<h3>Your agent connections</h3><div id="agent-status" aria-live="polite">${agentStatus()}</div><p class="context">Refreshes every five seconds while this tab is visible. Last seen confirms the credential was used, not that a session is running.</p><button class="button" data-action="agent-status">Refresh status</button><details class="history" id="manual-agent-setup"><summary>Other MCP clients</summary><p>Create a limited credential and add the endpoint and header in that client’s MCP settings.</p><label for="agent-client">Agent client</label><select id="agent-client"><option>Claude Code</option><option>Cursor</option><option>Codex</option><option>Other MCP client</option></select><button class="button primary" data-action="agent-setup">Create connection details</button><p class="context">The client must support HTTP MCP with an authorization header.</p></details>${workbenchSetupCard()}</section>`;
  } else {
    const {remote, notice, ...config} = state.mcp_config || {};
    agentStep = `<section class="setup-card">${step(1, 'Connect your coding agent')}<p>Add this to your agent’s MCP settings.</p><pre id="mcp-config">${esc(JSON.stringify(config, null, 2))}</pre><button class="button primary" data-action="copy">Copy configuration</button><p>Then add this to <code>CLAUDE.md</code> or <code>AGENTS.md</code>.</p><pre id="agent-rule">${esc(AGENT_RULE)}</pre><button class="button" data-action="copy" data-target="#agent-rule">Copy instruction</button>${workbenchSetupCard()}</section>`;
  }
  // A repository ingested at startup (./setup --repo) is context already
  // there; the card names it so the person can tell.
  const graph = state.graph || {};
  const indexed = [...new Set((graph.repos || []).filter(r => typeof r === 'string' && r))];
  const indexedLine = indexed.length ? `<p class="context"><strong>Already indexed:</strong> ${indexed.map(esc).join(', ')} · ${plural(graph.engineers || 0, 'person', 'people')} from its history${graph.intents ? `, ${plural(graph.intents, 'record')}` : ''}. Raven can name them now.</p>` : '';
  // The evidence panel (evidence.js) draws for members and admins only; a viewer gets no empty disclosure.
  const member = !shared || ['admin', 'member'].includes(state.me?.role);
  const contextStep = `<section class="setup-card" id="settings-context">${step(2, 'Context')}${githubCard()}${indexedLine}${contextSourcesCard()}${canAdminister() ? '<p><button class="button text" data-action="ingest">Add a local clone</button></p>' : ''}${member ? '<details class="history" id="settings-evidence"><summary>Paste a message or email</summary><div id="evidence-mount"></div></details>' : ''}${canAdminister() ? '<details class="history" id="settings-meetings"><summary>Meetings</summary><div id="meetings-mount"></div></details>' : ''}</section>`;
  const peopleStep = shared
    ? (canAdminister() ? `<section class="setup-card" id="settings-people">${step(4, 'People access')}${workspaceAccountCard()}</section>` : '')
    : `<section class="setup-card" id="settings-people">${step(4, 'People access')}<p>Sign-in is off: single-operator workspace. Turn it on before sharing this server.</p></section>`;
  const advanced = `<details class="history settings-advanced" id="repository-signals"><summary>Advanced</summary>${savedStrip()}<h3>Who git thinks owns what</h3>${ownershipGraph()}<p><button class="button" data-action="export">Export history</button></p></details>`;
  return `${setupChecklist()}<div class="settings-page">${agentStep}${contextStep}${deliveriesCard()}${peopleStep}${routingCard()}${advanced}</div>`;
}

// The monthly routing knobs, with the ids and PUTs the change handler
// below listens for. Non-admins read them inside a disabled fieldset.
function routingCard() {
  const head = '<h2>Routing</h2>';
  if (directory === null) {
    loadDirectory();
    return `<section class="setup-card" id="settings-routing">${head}<p class="context" role="status">Loading…</p></section>`;
  }
  const settings = directory.settings || {};
  const people = directory.people || [];
  const coordinator = settings.coordinator;
  const control = `<section class="setup-card" id="settings-routing">${head}<label for="coordinator-select">Fallback person</label><select id="coordinator-select"><option value="">Nobody</option>${people.map(p => `<option value="${esc(p.id)}" ${coordinator && coordinator.id === p.id ? 'selected' : ''}>${esc(p.name)}${p.team ? ` · ${esc(p.team)}` : ''}</option>`).join('')}</select><p class="field-help">Gets questions nobody is known to own.</p><div class="routing-options"><label class="routing-option" for="verified-only"><input type="checkbox" id="verified-only" ${settings.require_verified_route ? 'checked' : ''}><span>Only ask verified owners</span></label><label class="routing-option" for="auto-rules"><input type="checkbox" id="auto-rules" ${settings.auto_rules ? 'checked' : ''}><span>Reuse signed answers automatically</span></label></div><label for="brief-mode">Slack links open</label><select id="brief-mode">${[['off', 'The inbox'], ['static', 'The task page']].map(([v, l]) => `<option value="${v}" ${(settings.brief_mode || 'static') === v ? 'selected' : ''}>${l}</option>`).join('')}</select></section>`;
  return canAdminister() ? control : `<fieldset class="read-only-settings" disabled><legend>Managed by an administrator</legend>${control}</fieldset>`;
}

function contextSourcesCard() {
  const report = state.context_sources || {};
  const rows = (report.sources || []).map(s => {
    const refused = Object.entries(s.refused || {}).map(([k, n]) => `${esc(k.replace(/_/g, ' '))}: ${n}`).join(', ');
    return `<li><strong>${esc(s.kind)}</strong> ${esc(s.label || s.target)} → ${esc(s.repo)} · <span class="status-${esc(s.state)}">${esc(s.state.replace(/_/g, ' '))}</span> · ${s.records} records${s.last_success_at ? ' · synced ' + esc(new Date(s.last_success_at).toLocaleString()) : ''}${refused ? `<br><small>Not imported — ${refused}</small>` : ''}${s.do ? `<br><small>${esc(s.do)}</small>` : ''}</li>`;
  }).join('');
  const missing = (report.not_configured || []).map(s => `<li><strong>${esc(s.kind)}</strong> not configured${s.credentials_present ? '' : ' (credentials not set)'}<br><code>${esc(s.enable)}</code></li>`).join('');
  return `<div class="github-heading" id="context-sources"><h3>Other sources</h3>${rows ? '' : '<span class="muted">none yet</span>'}</div>${rows ? `<ul>${rows}</ul>` : ''}${missing ? `<details class="history" id="context-sources-available"><summary>Available to add</summary><ul>${missing}</ul></details>` : ''}`;
}

function workspaceAccountCard() {
  if (!state.auth?.enabled || state.me?.role !== 'admin') return '';
  return `${state.workspace?.needs_setup ? '<p>Create your owner login and name this workspace.</p><a class="button" href="/auth/setup">Create workspace</a>' : ''}<p>Invite teammates to review context, ask questions and sign off on decisions in Raven. Default notification links require a Raven account. Invite links are private and expire after 48 hours.</p><button class="button" data-action="invite-person">Invite teammate</button>`;
}

async function setupAgent() {
  const client = $('#agent-client').value;
  if (!state.auth?.enabled) throw new Error('Enable server authentication before creating an HTTP agent connection.');
  openModal('Connect ' + client, 'Add Raven in your agent’s MCP settings.', `<p>Create a limited agent credential, then copy the endpoint and authorization header into your client. No local checkout path is needed.</p><button class="button primary" data-action="agent-credential" data-client="${esc(client)}">Create connection details</button><div id="agent-details"></div>`);
}

async function agentCredential(target) {
  target.disabled = true;
  const created = await api('/api/tokens', {label: target.dataset.client + ' · web setup'});
  markAgentConnected();
  const config = structuredClone(state.mcp_config.mcpServers.bridge);
  config.headers = {Authorization: 'Bearer ' + created.token};
  $('#agent-details').innerHTML = `<p>Keep these details private. The credential is shown only in this dialog; Raven stores its hash.</p><label>HTTP MCP endpoint</label><pre>${esc(config.url)}</pre><label>Authorization header</label><pre>${esc(config.headers.Authorization)}</pre><p>For clients accepting an MCP JSON configuration:</p><pre id="mcp-config">${esc(JSON.stringify({mcpServers: {bridge: config}}, null, 2))}</pre><button class="button" data-action="copy">Copy configuration</button><p>Save the connection in your agent, then return here. Clients with another configuration format use the same endpoint and header.</p>`;
  target.textContent = 'Credential created';
}

function render() {
  lastDraw = Date.now();
  settingsDotState();
  if (!state.auth) {
    $('#app').innerHTML = workspaceError ? `<div class="empty" role="alert"><h2>Workspace access unavailable</h2><p>${esc(workspaceError)}</p><button class="button" data-action="retry">Try again</button></div>` : '<p class="context" role="status">Loading workspace…</p>';
    return;
  }
  // #work with no task is the task list unless this server runs tasks in
  // the browser; the state that says so arrives after the first navigate().
  if (view === 'work' && !location.hash.slice(1).split('/')[1] && !state.workbench?.ready) {
    history.replaceState(null, '', location.pathname + location.search + '#runs');
    navigate();
    return;
  }
  if (typeof rememberTaskView === 'function') rememberTaskView();
  // Each page draws its own buttons into the h1 row; a page without any
  // must not inherit the previous page's. An open More menu stays open
  // across the five-second redraw. Where focus is is read before the row
  // is cleared: clearing it drops focus to <body>, and a keyboard user on
  // More, Start a task or a menu item lost their place every five seconds.
  const actions = $('#page-actions');
  const menuWasOpen = !!actions?.querySelector('details.more-menu[open]');
  const active = document.activeElement;
  const focusRoot = $('#app').contains(active) ? $('#app') : actions?.contains(active) ? actions : null;
  const focusWas = focusRoot ? focusKey(active) : '';
  if (actions) actions.innerHTML = '';
  const noteDraft = $('#note-text')?.value;
  const selection = active.tagName === 'INPUT' ? active.selectionStart : null;
  const disclosures = [...document.querySelectorAll('#app details[id]')].map(el => ({id: el.id, open: el.open, scroll: el.querySelector('ul')?.scrollTop || 0}));
  $('.page-head').hidden = !!taskId() || view === 'work';
  document.body.classList.toggle('work-view', view === 'work');
  if (view === 'today' && typeof renderObjectives === 'function') { $('#app').dataset.taskId = ''; renderObjectives(); return; }
  if (view === 'work' && typeof renderWorkbench === 'function') { $('#app').dataset.taskId = ''; renderWorkbench(); return; }
  $('#app').innerHTML = taskId() ? taskOverview() : ({inbox, runs, owners, connect: connections})[view]();
  $('#app').dataset.taskId = taskId();
  const menu = actions?.querySelector('details.more-menu');
  if (menu && menuWasOpen) menu.open = true;
  if (taskId() && typeof restoreTaskView === 'function') { restoreTaskView(); return; }
  if (noteDraft && $('#note-text')) $('#note-text').value = noteDraft;
  for (const saved of disclosures) {
    const el = document.getElementById(saved.id);
    if (el) {
      el.open = saved.open;
      if (el.querySelector('ul')) el.querySelector('ul').scrollTop = saved.scroll;
    }
  }
  // The list is redrawn every five seconds and after every action. A
  // person moving through it by keyboard keeps their place; when the
  // control they were on is gone (the decision they answered left the
  // tab), focus goes to the tab above the list, not to the top of the page.
  const replacement = keepFocus(focusRoot || $('#app'), focusWas, () => (focusRoot === actions ? null : $('#app .toolbar .tab.active')) || $('#main'));
  if (replacement && selection !== null && ['search','text'].includes(replacement.type)) replacement.setSelectionRange(selection, selection);
}

// Where focus is, as a selector that survives a redraw: an id, the
// action a control performs on the thing it names, a page's More summary,
// or a link by where it goes (a task row, a menu item). Measured in
// Chromium: a Review button a keyboard user had reached was dropped to
// <body> by the next poll, every five seconds, on every list in the app.
function focusKey(el) {
  if (!el || el === document.body) return '';
  const quote = value => `"${String(value).replace(/["\\]/g, '\\$&')}"`;
  if (el.id) return `[id=${quote(el.id)}]`;
  if (el.tagName === 'SUMMARY' && el.closest?.('details.more-menu')) return 'details.more-menu > summary';
  const data = el.dataset || {};
  if (!data.action) return el.tagName === 'A' && el.getAttribute?.('href') ? `a[href=${quote(el.getAttribute('href'))}]` : '';
  return `${el.tagName.toLowerCase()}[data-action=${quote(data.action)}]`
    + ['id', 'tab', 'client', 'mode', 'key'].filter(k => data[k] !== undefined).map(k => `[data-${k}=${quote(data[k])}]`).join('');
}

// Put focus back on the control `key` names inside `root`, or on the
// fallback when the redraw removed it. Returns the control focused from
// the key, so a text field can get its caret back.
function keepFocus(root, key, fallback) {
  if (!key) return null;
  const next = root.querySelector(key);
  const target = next || (typeof fallback === 'function' ? fallback() : fallback);
  if (target && typeof target.focus === 'function') target.focus({preventScroll: true});
  return next;
}

function showHelp() {
  openModal('Help', '',
    `<p>The guide covers setup, tasks, answering and signing. <a href="${HELP_URL}" target="_blank" rel="noopener">Read the guide ↗</a></p><div class="modal-actions"><button class="button primary" data-action="help-connect">Open Settings</button><button class="button" data-action="close">Close</button></div>`);
}
const HELP_URL = 'https://github.com/nintendo-ds75/raven-preview/blob/main/docs/help.md';
function openModal(title, subtitle, body) {
  saveRequestDraft();
  modalVersion += 1;
  delete $('#modal').dataset.runId;
  // An open dialog redrawn in place (after a sign-off, a hand-on, a
  // follow-up) keeps focus on the control the person was using, or
  // starts them at its title when that control is gone: a focus dropped
  // to <body> inside a modal dialog leaves a keyboard user nowhere.
  const wasOpen = $('#modal').open;
  const focusWas = wasOpen && $('#modal').contains(document.activeElement) ? focusKey(document.activeElement) : '';
  $('#modal-content').innerHTML = `<div class="modal-head"><div><h2 id="modal-title" tabindex="-1">${esc(title)}</h2><p>${esc(subtitle)}</p></div><button class="icon-button" data-action="close" aria-label="Close dialog">${icon('close')}</button></div><div class="modal-body">${body}</div>`;
  if (!wasOpen) $('#modal').showModal();
  else keepFocus($('#modal'), focusWas || '#modal-title', () => $('#modal-title'));
}

// After an action on an open decision, show it as it is now; unless the
// person closed the dialog while the request was in flight, in which
// case a late response must not reopen it. Measured in Chromium: Escape
// during a slow hand-on, and the dialog came back by itself.
async function rereview(id, version) {
  await refresh();
  if (modalVersion === version && $('#modal').open) await review(id);
}

function closeModal() {
  saveRequestDraft();
  // Native close events are queued. Invalidate now so a late response cannot
  // reopen this dialog, and an old close event cannot cancel a newer review.
  modalVersion += 1;
  $('#modal').close();
}

function ownerOptions(selected, auto = false) {
  return `${auto ? '<option value="">Route automatically by path</option>' : '<option value="">Select an owner</option>'}${state.owners.map(o => `<option value="${esc(o.id)}" ${selected === o.id ? 'selected' : ''}>${esc(o.name)} · ${esc(o.team)}</option>`).join('')}`;
}

let requestDraft = null;
const requestKey = () => 'ui-' + ((typeof ravenUuid === 'function' ? ravenUuid() : crypto.randomUUID()) || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`);

function saveRequestDraft() {
  const form = $('#request-form');
  if (!requestDraft || !form || form.dataset.clientKey !== requestDraft.clientKey) return;
  if (!requestDraft.payload) requestDraft.values = Object.fromEntries(new FormData(form));
  if (requestDraft.scopeEditable && !requestDraft.pending) requestDraft.factValues = Object.fromEntries([...form.querySelectorAll('[data-fact-key]')].map(el => [el.dataset.factKey,el.value]));
}

function requestScopeKeys(draft) {
  return [...new Set((draft.scope?.scope_clarifications || []).flatMap(s => s.missing_keys || []).filter(key => typeof key === 'string'))];
}

function requestScopeFields(draft) {
  if (!draft.scope || draft.complete) return '';
  const keys = requestScopeKeys(draft);
  const namespaces = (draft.scope.scope_clarifications || []).flatMap(s => s.missing_source_namespaces || []);
  return `<details id="request-scope" open><summary>Confirm missing scope</summary><p>${draft.scopeEditable && !draft.pending ? 'The last response needed scope. No question was added or sent by that attempt.' : 'The latest result is pending or unconfirmed. Retry uses the same question and facts.'}</p>${keys.map((key,i) => `<label for="request-fact-${i}">Current value for ${esc(key)} (if known)</label><input id="request-fact-${i}" data-fact-key="${esc(key)}" maxlength="2000" value="${esc(Object.hasOwn(draft.factValues || {},key) ? draft.factValues[key] : '')}" ${draft.scopeEditable && !draft.pending ? '' : 'readonly'}>`).join('')}${keys.length ? '<p class="context">Enter only current facts you know from this task; leave unknown values blank. The run can remain blocked. Historical values have not been copied into these fields.</p>' : ''}${namespaces.length ? `<p class="task-warning">Source context is also missing: ${namespaces.map(value => esc(typeof value === 'string' ? value : JSON.stringify(value))).join(', ')}. The originating workflow must supply the required source context; these fields cannot create source anchors.</p>` : ''}<p class="context">Retry checks the same task and question. It may stay blocked until all required context is present.</p></details>`;
}

function newRequest() {
  saveRequestDraft();
  if (!requestDraft) requestDraft = {clientKey:requestKey(), clientRef:requestKey(), values:{}, taskId:'', payload:null, nodePayload:null, facts:{}, factValues:{}, scope:null, scopeEditable:false, pending:false, complete:false, error:''};
  const d = requestDraft, v = d.values, locked = !!d.payload;
  const repos = [...new Set((state.graph?.repos || []).filter(r => typeof r === 'string' && r))];
  const canAsk = !!state.auth && (!state.auth.enabled || ['admin','member'].includes(state.me?.role));
  const selected = v.repo || (repos.length === 1 ? repos[0] : '');
  openModal('Ask Raven', 'Raven records your question and may contact its routed owner. This does not launch an external coding agent.', `<form id="request-form" data-client-key="${esc(d.clientKey)}" data-client-ref="${esc(d.clientRef)}" data-task-id="${esc(d.taskId)}">
    <label for="question">Question or prompt</label><textarea id="question" name="question" maxlength="2000" rows="4" placeholder="What do you need to know or decide?" ${locked ? 'readonly' : ''} required>${esc(v.question || '')}</textarea>
    <label for="repo">Repository scope</label><select id="repo" name="repo" ${locked ? 'disabled' : ''} required><option value="">Choose a repository</option>${repos.map(r => `<option value="${esc(r)}" ${selected === r ? 'selected' : ''}>${esc(r)}</option>`).join('')}${v.repo && !repos.includes(v.repo) ? `<option value="${esc(v.repo)}" selected>${esc(v.repo)}</option>` : ''}</select>
    <details id="request-context" ${v.context || v.path ? 'open' : ''}><summary>Extra context (optional)</summary><label for="context">Context & evidence</label><textarea id="context" name="context" maxlength="12000" ${locked ? 'readonly' : ''}>${esc(v.context || '')}</textarea><label for="path">Relevant file path</label><input id="path" name="path" maxlength="1000" placeholder="src/example.py" value="${esc(v.path || '')}" ${locked ? 'readonly' : ''}></details>
    ${!canAsk ? `<p class="context">${state.auth ? 'You have read-only access to this workspace.' : 'Loading workspace access…'}</p>` : !repos.length && !locked ? '<p class="context">No repository scope is available yet. Connect or ingest a repository in Settings.</p>' : ''}
    <p class="context">${locked ? 'Retry uses this same question and scope to avoid duplicate tasks or questions.' : 'Ask one question, up to 2,000 characters. Add details in Extra context.'}</p>
    <div id="request-scope-area">${requestScopeFields(d)}</div>
    <p class="error" id="form-error" role="alert" ${d.error ? '' : 'hidden'}>${esc(d.error)}</p><p id="request-progress" role="status">${d.pending ? 'Recording your question…' : d.complete ? 'Your question is recorded.' : d.scope ? d.scopeEditable ? 'Scope is still needed. No question was added or sent by the last attempt.' : 'The latest question result is unconfirmed. Retry uses the same facts.' : d.taskId ? 'The task is recorded. Retry to finish adding its question.' : ''} ${d.taskId ? `<a id="request-run-link" href="#runs/${encodeURIComponent(d.taskId)}" data-action="request-view-run">Open recorded run</a>` : ''}</p>
    <div class="modal-actions"><button type="button" class="button" data-action="close">Cancel</button>${d.pending ? '<button type="button" class="button" data-action="request-status">Check request status</button>' : ''}${d.complete ? '<button type="button" class="button primary" data-action="request-new">Ask another question</button>' : `<button class="button primary" type="submit" ${d.pending || !canAsk || (!repos.length && !locked) ? 'disabled' : ''}>${d.scope ? 'Retry with current scope' : locked ? 'Retry question' : 'Ask Raven'}</button>`}</div></form>`);
}

async function submitRequest(form) {
  const draft = requestDraft;
  if (!draft || form.dataset.clientKey !== draft.clientKey || $('#request-form') !== form || !$('#modal').open || draft.pending || draft.complete) return;
  if (!state.auth || (state.auth.enabled && !['admin','member'].includes(state.me?.role))) return;
  if (!form.reportValidity()) return;
  const errorEl = form.querySelector('#form-error'), progress = form.querySelector('#request-progress');
  if (!draft.payload) {
    const data = Object.fromEntries(new FormData(form));
    if (!data.question?.trim() || data.question.length > 2000 || !state.graph?.repos?.includes(data.repo)) {
      errorEl.textContent = 'Enter a question of up to 2,000 characters and choose an available repository.';
      errorEl.hidden = false; return;
    }
    draft.values = data;
    draft.payload = {question:data.question, repo:data.repo, context:data.context || '', path:data.path || ''};
  }
  if (draft.scopeEditable) {
    draft.factValues = Object.fromEntries([...form.querySelectorAll('[data-fact-key]')].map(el => [el.dataset.factKey,el.value]));
    const missing = new Set(requestScopeKeys(draft));
    draft.facts = Object.fromEntries([...Object.entries(draft.facts || {}).filter(([key]) => !missing.has(key)), ...Object.entries(draft.factValues).filter(([,value]) => value.trim())]);
    draft.nodePayload = null; draft.scopeEditable = false;
  }
  const data = draft.payload, version = modalVersion, navigation = navigationVersion, route = location.hash;
  if (!draft.nodePayload) draft.nodePayload = {question:data.question, context:data.context, paths:data.path, client_ref:draft.clientRef, ...(Object.keys(draft.facts || {}).length ? {facts:{...draft.facts}} : {})};
  const current = () => $('#modal').open && modalVersion === version && navigationVersion === navigation && $('#request-form') === form && location.hash === route;
  const button = form.querySelector('[type="submit"]');
  draft.pending = true; draft.error = ''; errorEl.hidden = true;
  const done = busy(button, 'Recording…');
  form.querySelectorAll('input,textarea').forEach(el => { el.readOnly = true; });
  form.querySelector('select').disabled = true;
  progress.textContent = 'Recording your question…';
  if (draft.scope) form.querySelector('#request-scope-area').innerHTML = requestScopeFields(draft);
  try {
    if (!draft.taskId) {
      const result = await api('/api/tasks/start', {title:Array.from(data.question).slice(0,300).join(''), goal:data.question, agent:'Raven UI', repo:data.repo, paths:data.path, client_key:draft.clientKey});
      if (!result.task_id) throw new Error('No task ID was returned. Retry this same question.');
      draft.taskId = result.task_id; form.dataset.taskId = draft.taskId;
    }
    const result = await api(`/api/tasks/${encodeURIComponent(draft.taskId)}/nodes`, draft.nodePayload);
    if (result.status === 'needs_scope_clarification') {
      draft.scope = result; draft.scopeEditable = true; draft.nodePayload = null;
      if (current()) {
        // This is a definite non-creation response, so additional facts may be entered.
        draft.pending = false;
        form.querySelector('#request-scope-area').innerHTML = requestScopeFields(draft);
        progress.innerHTML = `Scope is still needed. No question was added or sent. <a id="request-run-link" href="#runs/${encodeURIComponent(draft.taskId)}" data-action="request-view-run">Open blocked run</a>`;
      }
      return;
    }
    if (!result.node_id) throw new Error('No question ID was returned. Retry this same question to check its recorded result.');
    draft.complete = true; draft.scope = null; draft.scopeEditable = false;
    if (current()) {
      closeModal(); requestDraft = null;
      location.hash = 'runs/' + encodeURIComponent(draft.taskId);
      notify('Question recorded. Follow its answers and next action in this run.');
      await refresh({quiet:true});
    }
  } catch (error) {
    draft.error = error.message;
    if (current()) {
      errorEl.textContent = error.message; errorEl.hidden = false;
      progress.innerHTML = draft.taskId ? `${draft.scope ? 'The latest question result is unconfirmed. Retry uses the same facts.' : 'The task is recorded. Retry to check or finish adding its question.'} <a id="request-run-link" href="#runs/${encodeURIComponent(draft.taskId)}" data-action="request-view-run">Open recorded run</a>` : 'Your prompt is preserved. Retry uses the same request key.';
    }
  } finally {
    draft.pending = false;
    done();
    if (current()) button.textContent = draft.scopeEditable ? 'Retry with current scope' : 'Retry question';
  }
}

function newTask() {
  openModal('Start a task.', 'Describe the work. Raven will bring unresolved decisions to the inbox.', `<form id="task-form" data-submission-key="${esc((typeof ravenUuid === 'function' ? ravenUuid() : crypto.randomUUID()))}"><label for="task">Task</label><textarea id="task" name="task" maxlength="12000" placeholder="Add usage-based pricing" required></textarea><label for="repository">Repository</label><select id="repository" name="repository">${(state.execution_config?.repositories || []).map(r => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join('')}</select><p class="context">Runs in a disposable hosted workspace. Changes and test results appear in the run.</p><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button type="button" class="button" data-action="close">Cancel</button><button class="button primary" type="submit">Start task</button></div></form>`);
}

function newIngest() {
  openModal('Ingest a repository.', 'Build the ownership map and record graph from a local git checkout.', `<form id="ingest-form"><label for="ingest-path">Absolute path to the checkout</label><input id="ingest-path" name="path" placeholder="/home/you/src/platform" maxlength="1000" required><div class="form-row"><div><label for="ingest-repo">Repository name (optional)</label><input id="ingest-repo" name="repo" placeholder="platform" maxlength="100"></div><div><label for="ingest-commits">History depth (0 = all)</label><input id="ingest-commits" name="max_commits" placeholder="2000" maxlength="8"></div></div><p class="context">Reads git log, blame shares, CODEOWNERS, and Reviewed-by trailers on this machine. Nothing leaves it. Re-running refreshes the map.</p><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button type="button" class="button" data-action="close">Cancel</button><button class="button primary" type="submit">Ingest</button></div></form>`);
}

function ruleBox(d) {
  if (d.reusable) {
    return `<div class="context-box"><span class="label">Reusable rule${d.rule_by ? ` · made by ${esc(d.rule_by)}` : ''}</span>Questions memory matches to this answer ${state.settings && state.settings.auto_rules ? 'resolve without a fresh signature' : 'are shown as covered by this rule (automatic rules are off, so a person still signs)'}${d.rule_conditions ? ` when they satisfy <strong>${esc(d.rule_conditions)}</strong>` : ''}${d.rule_scope === 'any' ? ', in any scope' : ', in this scope only'}${d.rule_expires ? `, until ${esc(d.rule_expires.slice(0, 10))}` : ''}. Ending it puts every outstanding node it authorized back in front of a person.<div class="modal-actions"><button class="button small" data-action="end-rule" data-id="${esc(d.id)}" data-updated="${esc(d.updated_at)}">End the rule</button></div></div>`;
  }
  return `<details class="history"><summary>Make this a rule</summary><form id="rule-form" data-id="${esc(d.id)}" data-updated="${esc(d.updated_at)}"><label for="rule-conditions">Applies when the question mentions (optional; one phrase per line)</label><textarea id="rule-conditions" name="conditions" maxlength="500" placeholder="enterprise plan"></textarea><label for="rule-expires">Expires (optional, YYYY-MM-DD)</label><input id="rule-expires" name="expires" maxlength="25" placeholder="2027-01-01"><label for="rule-scope"><input type="checkbox" id="rule-scope" name="scope" value="any"> Applies anywhere (other customers, other files); otherwise only in this decision's own scope</label><p class="context">By default every decision is request-specific: a later task that asks the same thing gets this answer as evidence and still needs your signature. A rule skips that signature while its conditions hold, once automatic rules are on in Settings. Conditions are checked against what the agent states as facts (<code>plan=enterprise</code>) or phrases the question carries and does not deny; a missing fact means a person decides.</p><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button class="button primary small" type="submit">Make it a rule</button></div></form></details>`;
}

function sourceSnapshots(sources) {
  return (sources || []).map(source => {
    const snapshot = source.snapshot || {};
    const {body, ...metadata} = snapshot;
    return `<details class="history source-snapshot" open data-source-record="${esc(source.record_id)}" data-source-version="${esc(source.source_version_id)}"><summary>${esc(snapshot.display_ref || source.record_id)} · ${esc(source.role)} · ${source.changed ? 'changed revision' : 'attached revision'}</summary><p>${esc(snapshot.title || '')}</p><p class="context">${esc(snapshot.author || 'Author not supplied')} · ${esc(snapshot.status || 'Status not supplied')} · ${esc(snapshot.source_updated_at || 'Update time not supplied')}</p><pre class="modal-context source-snapshot-body">${esc(body || '')}</pre><p class="context">Record ID: ${esc(source.record_id)}<br>Exact source version: ${esc(source.source_version_id)}<br>Role: ${esc(source.role)}<br>Paths: ${esc((snapshot.paths || []).join(', ') || 'None recorded')}</p><details class="history"><summary>Complete source metadata</summary><pre class="modal-context">${esc(JSON.stringify(metadata, null, 2))}</pre></details></details>`;
  }).join('');
}

function sourceEvidence(sources, review = null) {
  if (!sources?.length && !review?.sources?.length) return '';
  return `<details class="history" open><summary>Versioned sources · ${review?.sources?.length || (sources || []).length}</summary>${(sources || []).map(source => {
    const label = `${source.ref} · version ${source.sequence} · ${source.role}`;
    const url = /^https?:\/\//i.test(source.url || '') ? source.url : '';
    return `<p class="context">${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>` : esc(label)}${source.stale ? ' · historical or unavailable; review before reuse' : ''}<br><small>${esc(source.namespace)} · ${esc(source.source_version_id)}<br>Record ID: ${esc(source.record_id)}</small></p>`;
  }).join('')}${review?.sources?.length ? `<section class="source-evidence-review"><h3>Source snapshots for this answer</h3>${!review.available ? `<p class="context">${esc(review.notice)}</p>` : ''}${sourceSnapshots(review.sources)}</section>` : ''}<p class="context">Sources are evidence. Their authors and statuses do not grant approval. Context and work-item links do not require source revalidation.</p></details>`;
}

function workItemContext(d) {
  const a = d.work_item_association;
  const sources = a?.links || [...(d.source_anchors || []).map(s => ({...s, origin:'task'})),
    ...(d.sources || []).filter(s => ['work_item','context'].includes(s.role)).map(s => ({...s, origin:'decision'}))];
  if (!a?.declared && !sources.length) return '';
  const labels = {unlinked:'Unlinked', linked:'Linked association', ambiguous:'Ambiguous association', undeclared:'No declared ID'};
  const warning = a?.historical && ['unlinked','ambiguous'].includes(a.status)
    ? '<p class="context">Historical declaration on a closed task. Preserve its recorded history; start a new task for a new association.</p>'
    : a?.status === 'unlinked' ? '<p class="context">This declared ID has no explicit work-item link. Historical records are not reconstructed from text.</p>'
    : a?.status === 'ambiguous' ? '<p class="context">More than one linked record matches this ID. Inspect the canonical identities below.</p>' : '';
  return `<details class="history work-item-context" open><summary>Work-item context${a?.declared ? ` · ${esc(a.declared)} · ${a.historical ? 'Historical ' : ''}${esc(labels[a.status] || a.status)}` : ''}</summary><p class="context">Context associations only. These links are not supporting evidence or approval.</p>${warning}${sources.map(s => `<p class="context" data-work-item-record="${esc(s.record_id)}" data-work-item-origin="${esc(s.origin)}">${s.origin === 'decision' ? 'Decision association' : 'Task association'} · ${esc(s.ref)} · ${esc(s.role)} · ${s.current ? 'current observed version' : 'historical or unavailable version'}<br><small>${esc(s.provider)} / ${esc(s.namespace)} / ${esc(s.kind)} / ${esc(s.external_id)}<br>Record ID: ${esc(s.record_id)}<br>Recorded source version: ${esc(s.source_version_id)}</small></p>`).join('')}${d.work_item_history?.length ? `<details class="history"><summary>Task work-item association history</summary>${d.work_item_history.map(e => `<p class="context">${esc(new Date(e.created_at).toLocaleString())}<br>${esc(e.detail)}</p>`).join('')}</details>` : ''}</details>`;
}

function sourceRevalidationFields(d, prefix) {
  const review = d.source_revalidation;
  if (!d.needs_review || !review) return '';
  const dependencies = (review.dependencies || []).map(source => `<details class="history"><summary>${source.historical ? 'Historical signed precedent' : 'Source decision'} ${esc(source.decision_id)}</summary><p>${esc(source.question)}</p><p>${esc(source.answer)}</p><p class="context">Current scope, rationale, applicability, authority constraints, and exact dependency snapshot:</p><pre class="modal-context">${esc(JSON.stringify(source.reviewed_snapshot || source, null, 2))}</pre><p class="context">Exact human-source version: ${esc(source.source_version_id || 'Current observation; frozen on confirmation')}<br>Snapshot SHA-256: ${esc(source.source_snapshot_sha256 || '')}</p><button type="button" class="button small" data-action="source" data-id="${esc(source.decision_id)}">Review source decision</button></details>`).join('');
  return `<section class="source-revalidation"><h3>Review current source evidence</h3><p class="context">${esc(review.notice)}</p>${review.retires_rule ? '<p class="context">Confirming these changed source versions retires the existing standing rule. This approval covers this decision only; future automatic reuse needs a fresh explicit make-rule action.</p>' : ''}${dependencies}${sourceSnapshots(review.sources)}${review.available ? `<input type="hidden" id="source-pins-${prefix}" name="current_source_pins" value="${esc(JSON.stringify(review.pins))}"><input type="hidden" id="source-decisions-${prefix}" name="current_source_decisions" value="${esc(JSON.stringify(review.decision_pins || []))}"><label><input type="checkbox" id="source-review-${prefix}" name="use_current_sources" value="yes"> I reviewed these complete source snapshots and source decisions. Bind this answer to these exact revisions.</label>` : ''}</section>`;
}

function takeSourceRevalidation(data) {
  if (data.use_current_sources === 'yes') {
    data.source_evidence = JSON.parse(data.current_source_pins || '[]');
    data.source_decision_pins = JSON.parse(data.current_source_decisions || '[]');
  }
  delete data.use_current_sources; delete data.current_source_pins; delete data.current_source_decisions;
}

function sourceReplacementField(d) {
  const reliesOnSources = (d.sources || []).length || d.source_revalidation?.has_reliance || d.source_reuse_state === 'unknown';
  return reliesOnSources ? '<label><input type="checkbox" name="evidence_mode" value="independent"> This is my independent replacement decision. Retire the previous source reliance and preserve it in history.</label>' : '';
}

function applicabilityFields(d, prefix) {
  let spec = {};
  try { spec = typeof d.applicability === 'string' ? JSON.parse(d.applicability || '{}') : (d.applicability || {}); } catch (_) { /* Old rows have no declaration. */ }
  const pairs = obj => Object.entries(obj || {}).map(([k, v]) => `${k}=${v}`).join(', ');
  return `<label for="${prefix}-requires">Required facts</label><input id="${prefix}-requires" name="applies_when" value="${esc(pairs(spec.requires))}" placeholder="plan=enterprise, region=us" maxlength="2000">
    <label for="${prefix}-excludes">Exceptions</label><input id="${prefix}-excludes" name="excludes_when" value="${esc(pairs(spec.excludes))}" placeholder="contract=monthly" maxlength="2000">
    <label for="${prefix}-paths">Repository paths</label><input id="${prefix}-paths" name="applies_to_paths" value="${esc((spec.paths || []).join(', '))}" placeholder="billing/, pricing/rules.py" maxlength="2000">
    <label for="${prefix}-until">Valid until</label><input id="${prefix}-until" name="valid_until" type="date" value="${esc((spec.valid_until || '').slice(0, 10))}">`;
}

function takeApplicability(data) {
  const facts = text => {
    const out = {};
    for (const part of String(text || '').split(',').map(x => x.trim()).filter(Boolean)) {
      const at = part.indexOf('=');
      if (at <= 0 || !part.slice(at + 1).trim()) throw new Error('Applicability facts must be comma-separated key=value pairs.');
      out[part.slice(0, at).trim()] = part.slice(at + 1).trim();
    }
    return out;
  };
  const applicability = {requires: facts(data.applies_when), excludes: facts(data.excludes_when),
    paths: String(data.applies_to_paths || '').split(',').map(x => x.trim()).filter(Boolean),
    valid_until: data.valid_until || ''};
  delete data.applies_when; delete data.excludes_when; delete data.applies_to_paths; delete data.valid_until;
  data.applicability = applicability;
}

function followupControls(d) {
  if (!(d.allowed_actions || []).includes('followup')) return '';
  return `<h3>Follow-up questions for the agent</h3><textarea id="followups" aria-label="Follow-up questions" placeholder="One question per line." maxlength="4000"></textarea><label for="followups-required"><input type="checkbox" id="followups-required"> Required before the task can finish</label><div class="modal-actions"><button class="button small" data-action="followups" data-id="${esc(d.id)}">Add follow-up questions</button></div>`;
}

function reframeControls(d) {
  if (!(d.allowed_actions || []).includes('correct') || ['withdrawn','duplicate','adopted','suggested'].includes(d.status)) return '';
  return `<h3>Change the question</h3>
    <label for="reframe-question">Corrected question</label><textarea id="reframe-question" maxlength="2000">${esc(d.question)}</textarea>
    <label for="reframe-reason">Why</label><textarea id="reframe-reason" maxlength="4000"></textarea>
    <div class="modal-actions"><button class="button small" data-action="reframe" data-id="${esc(d.id)}" data-updated="${esc(d.updated_at)}">Replace the question</button></div>`;
}

// Closed by default: the person opened the decision to answer it, and the
// answer form is what should be on screen; the question box is one line
// away for the times the context is not enough. Absent when the server has
// no model to read with.
function decisionInquiry(d) {
  if (state.model?.investigation === false) return '';
  return `<details class="decision-inquiry" id="decision-inquiry-${esc(d.id)}"><summary>Ask Raven</summary><form data-decision-inquiry="${esc(d.id)}" data-task="${esc(d.run_id)}"><label class="sr-only" for="decision-inquiry-text">Question for Raven</label><textarea id="decision-inquiry-text" name="question" maxlength="2000" required placeholder="What would change if we chose the other option?"></textarea><button class="button small" type="submit">Ask</button><div class="decision-inquiry-result" role="status"></div></form></details>`;
}

// The current answer (when nothing newer is on the table) and the context
// the question was asked with, folded after four lines.
function decisionCurrentSummary(d, withAnswer = true) {
  const scope = typeof objectiveScope === 'function' ? objectiveScope(d) : null;
  return `<section class="decision-current" aria-label="Current decision">${withAnswer && d.answer ? `<p class="decision-current-answer">${esc(d.answer)}</p>${d.rationale ? `<p class="decision-current-reason"><strong>Why:</strong> ${esc(d.rationale)}</p>` : ''}` : ''}${scope ? objectiveScopeCard(scope, `current-${d.id}`) : clamped(d.context, 'modal-context', `modal-${d.id}`)}</section>`;
}

function decisionReviewScope(d) {
  // During approval, keep the complete contract visible. For a settled record,
  // its original question/context belongs below the current answer and limits.
  if (!settledNow(d) || d.needs_review || d.source_refresh_required) return approvalScope(d);
  const scope = d.approval_scope;
  if (!scope || typeof scope !== 'object' || Array.isArray(scope)) return approvalScope(d);
  const labels = d.approval_scope_labels || {};
  const fields = Object.entries(scope).filter(([key,value]) => !['id','run_id','scope_key','question','context','options'].includes(key) && value !== '' && !(Array.isArray(value) && !value.length));
  // Preserve the literal fallback for numbers JavaScript cannot represent.
  const unsafe = value => typeof value === 'number' ? !Number.isFinite(value) || Object.is(value,-0) || Number.isInteger(value) && !Number.isSafeInteger(value) : value !== null && typeof value === 'object' && Object.values(value).some(unsafe);
  if (unsafe(scope)) return approvalScope(d);
  return `<section class="decision-current-scope" aria-label="Current recorded scope"><h3>Current recorded scope</h3><dl class="scope-fields">${fields.map(([key,value]) => `<dt>${esc(labels[key] || key)}</dt><dd data-scope-field="${esc(key)}">${scopeValue(value,key)}</dd>`).join('')}</dl><details class="history decision-original-scope"><summary>Original request & complete recorded scope</summary>${approvalScope(d)}</details></section>`;
}

function decisionBackground(d) {
  const scope = typeof objectiveScope === 'function' ? objectiveScope(d) : null;
  return `<details class="history decision-background" ${!d.answer && !d.prediction ? 'open' : ''}><summary>Recorded background & evidence</summary>${d.brief ? `<p><strong>Original brief:</strong> ${esc(d.brief)}</p>` : ''}${scope ? objectiveScopeCard(scope, `background-${d.id}`) : d.context ? `<p class="modal-context">${esc(d.context)}</p>` : ''}${d.evidence ? `<p class="context"><strong>Recorded evidence:</strong> ${esc(d.evidence)}</p>` : ''}${workItemContext(d)}${sourceEvidence(d.sources, d.needs_review ? null : d.source_revalidation)}</details>`;
}

function inviteForm(people, preset = {}) {
  const unlinked = people.filter(p => p.active && !p.email && !p.github_id);
  const picker = unlinked.length ? `<label>Who this is for<select name="person_id"><option value="">A new teammate</option>${unlinked.map(p => `<option value="${esc(p.id)}"${preset.person_id === p.id ? ' selected' : ''}>${esc(p.name)}${p.team ? ` · ${esc(p.team)}` : ''} — already on the map, no email</option>`).join('')}</select></label><p class="field-help">A person already on the map with this email is linked on their own. Someone on the map without an email is linked only when you name them here; a matching name is not enough.</p>` : '';
  return `<form id="invite-form"><label>Email<input name="email" type="email" required maxlength="254" value="${esc(preset.email || '')}"></label>${picker}<label>Access<select name="role"><option value="member">Member — contribute and answer assigned decisions</option><option value="viewer">Viewer — read only</option></select></label><p class="error" id="form-error" hidden></p><button class="button primary" type="submit">Create invitation</button></form>`;
}

// A routed owner who cannot sign in yet. Measured on a demo run: the admin
// opened a decision routed to a teammate who had not joined and read only
// that they could add a note or a follow-up; nobody could sign it. Who may
// sign is unchanged: the admin is offered the invitation and the hand-on,
// the hand-on recorded as their override.
function ownerAccountNotice(d, withActions) {
  const account = d.owner_account;
  if (!account || account.state === 'account' || !d.owner_name) return '';
  const who = esc(d.owner_name), first = esc(String(d.owner_name).trim().split(/\s+/)[0]);
  const admin = state.me?.role === 'admin';
  const actions = !withActions ? '' : admin
    ? `<div class="modal-actions"><button class="button small" data-action="invite-owner" data-person="${esc(account.person_id || '')}" data-email="${esc(account.email || '')}">${account.state === 'invited' ? 'Invite again' : `Invite ${first}`}</button></div>${handOnRow(d, true)}<p class="context">Recorded as your admin override. It signs nothing.</p>`
    : '<p class="context">An administrator can invite them or hand this on.</p>';
  return `<div class="context-box prediction"><span class="label">${who} is not in Raven yet</span>${account.state === 'invited' ? 'They were invited but have not joined yet.' : 'Nobody can sign this until they join.'}${actions}</div>`;
}

// One row hands a decision on. Unchecked, the button assigns this request
// only (/assign); checked, it also teaches Raven that the person decides
// the chosen scope (/refer). An admin's override is a refer and nothing
// else: a plain assign without standing is refused by the server, so the
// override row keeps the checkbox on and locked.
function handOnRow(d, override = false) {
  const options = d.handon?.options || [];
  const chosen = d.handon?.default || {};
  return `<div class="modal-owner">${avatar(d.owner_name)}<select id="assign-owner" aria-label="Hand on to">${ownerOptions(d.owner_id)}</select><button class="button small" id="hand-on" data-action="${override ? 'refer' : 'assign'}" data-id="${esc(d.id)}" data-updated="${esc(d.updated_at)}"${override ? ' data-override="1"' : ''}>Hand on</button></div>
    <label class="refer-learn" for="refer-learn"><input type="checkbox" id="refer-learn"${override ? ' checked disabled' : ''}> They decide this from now on</label>
    <div class="refer-scope" id="refer-scope-row"${override ? '' : ' hidden'}>${options.length ? `<label for="refer-scope">What they decide</label><select id="refer-scope">${options.map(o => `<option value="${esc(o.scope_kind)}:${esc(o.scope)}" ${o.scope_kind === chosen.scope_kind && o.scope === chosen.scope ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}</select>` : ''}${d.handon?.why_none ? `<p class="field-help">${esc(d.handon.why_none)}; pick a scope to teach one.</p>` : ''}</div>`;
}

function readOnlyReason(d) {
  const owner = d.owner_name ? `${esc(d.owner_name)} decides this.` : 'No owner yet.';
  if (state.me?.role === 'viewer') return `Read-only. ${owner}`;
  return `${owner} You can read it and add notes.`;
}

async function review(id) {
  const requestVersion = ++modalVersion;
  try {
    const d = await api(`/api/decisions/${encodeURIComponent(id)}`);
    if (requestVersion !== modalVersion) return;
    const canDecide = (d.allowed_actions || []).includes('answer');
    const canRoute = (d.allowed_actions || []).includes('assign');
    const readOnly = !canDecide && !canRoute;
    const approved = d.status === 'approved';
    const settled = settledNow(d);
    // Signed or covered by a rule, but not a recorded answer: it began as evidence, a prediction or an
    // assumed default, a person stands behind it now, and what is left is to correct it or make it a rule.
    const signed = signedNow(d) && !approved;
    // Three shapes: a node waiting for sign-off gets one block (sign or correct); a follow-up,
    // adopted or duplicate node takes no owner and no answer; everything else takes the answer form.
    const signoffWanted = canDecide && (d.signoff === 'required' || d.needs_review) && !approved && !['pending','suggested','adopted','duplicate'].includes(d.status);
    const inert = ['suggested','adopted','duplicate'].includes(d.status);
    const answerForm = canDecide && !inert && !signoffWanted && !signed;
    // A node nobody was routed to is the one that most needs placing, so
    // a node waiting for sign-off is assignable too, not only an open question.
    const assignable = canRoute && (d.status === 'pending' || (d.signoff === 'required' && !inert));
    // Signed in, the answer and the signature are the signed-in person's;
    // only the local workspace records on behalf of the named owner.
    const me = state.auth?.enabled ? state.me?.name || '' : '';
    // Locally, a decision one approver signed is signed next as the one
    // it still waits on, not again as its owner.
    const signer = me || (halfSigned(d) && stillToSign(d)[0]) || d.owner_name || 'the local operator';
    const recordedAs = `<p class="context">${me ? `Recorded as ${esc(me)}.` : `Recorded by the local operator on behalf of ${esc(signer)}.`}</p>`;
    const scope = typeof objectiveScope === 'function' ? objectiveScope(d) : null;
    const stale = d.needs_review || d.source_refresh_required;
    // What is on the table, as one box: absent once a person stands behind the answer,
    // which the current-answer block above then carries.
    const onTable = d.answer || d.prediction || '';
    const box = (approved || signedNow(d)) && !stale ? '' : answerBox(d, `${predictionScope(d) ? `<p class="context"><strong>Scope:</strong> ${esc(predictionScope(d))}</p>` : ''}${answerForm && onTable ? `<button class="button text small" data-action="use-suggestion" data-text="${esc(onTable)}">Use this answer</button>` : ''}${d.source_id ? `<button class="button text small" data-action="source" data-id="${esc(d.source_id)}">Open the earlier decision</button>` : ''}`);
    const canonical = d.canonical ? `<div class="context-box"><span class="label">Same decision as ${esc(d.canonical.id)} · ${esc(statusSentence(d.canonical).text)}${d.canonical.answered_by ? ` · ${esc(d.canonical.answered_by)}` : ''}</span>${esc(d.canonical.answer || 'Not answered yet.')}<br><button class="button text small" data-action="source" data-id="${esc(d.canonical.id)}">Open the earlier decision</button></div>` : '';
    // Two sources that disagree, and the records the question names with their status, stated up front rather
    // than left in the evidence. Measured live on 63eb671: the brief said no policy was given beside a conflict.
    const conflicts = (d.evidence || '').split(/;\s+(?=[a-z]+:\s)/).filter(p => p.startsWith('conflict:')).map(p => p.slice(9).trim());
    const upfront = `${conflicts.length && !settled ? `<div class="context-box prediction"><span class="label">Conflict · a person decides which stands</span>${esc(conflicts.join(' '))}</div>` : ''}${d.records_named ? `<p class="context"><strong>Records it names:</strong> ${esc(d.records_named)}.</p>` : ''}${d.source_notice && !stale ? `<p class="context">${esc(d.source_notice)}</p>` : ''}`;
    const moreOptions = prefix => `<details class="history"><summary>More options</summary>${applicabilityFields(d, prefix)}${prefix === 'answer' ? `<label for="supersedes">Supersedes decision (optional id)</label><input id="supersedes" name="supersedes" maxlength="100" value="${esc(d.supersedes || '')}">` : ''}</details>`;
    const correctForm = (prefilled, primary) => `<form id="correct-form" data-id="${esc(d.id)}"><input type="hidden" name="expected_updated_at" value="${esc(d.updated_at)}"><label for="correction">Correct it</label><textarea id="correction" name="answer" maxlength="12000" required>${esc(prefilled ? d.answer || '' : '')}</textarea><label for="correction-rationale">Why</label><textarea id="correction-rationale" name="rationale" maxlength="12000" required></textarea>${sourceRevalidationFields(d, 'correction')}${sourceReplacementField(d)}${moreOptions('correction')}<p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button class="button ${primary ? 'primary' : ''}" type="submit">Save correction</button></div>${primary ? recordedAs : ''}</form>`;
    const form = readOnly || inert ? '' : signoffWanted
      ? `<div class="signoff-block">${sourceRevalidationFields(d, 'signoff')}<div class="modal-actions"><button class="button primary" data-action="signoff" data-id="${esc(d.id)}" data-updated="${esc(d.updated_at)}">Sign as ${esc(signer)}</button></div>${recordedAs}</div>${correctForm(false, false)}`
      : signed && canDecide ? correctForm(true, true)
      : answerForm ? `<form id="answer-form" data-id="${esc(d.id)}"><input type="hidden" name="expected_updated_at" value="${esc(d.updated_at)}"><label for="answer">Your answer</label><textarea id="answer" name="answer" placeholder="Give the agent a clear decision and any conditions." maxlength="12000" required>${esc(approved ? d.answer : '')}</textarea><label for="rationale">Why</label><textarea id="rationale" name="rationale" maxlength="12000" required>${esc(d.rationale || '')}</textarea>${sourceRevalidationFields(d, 'answer')}${sourceReplacementField(d)}${moreOptions('answer')}<p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button class="button primary" type="submit" ${d.owner_id ? '' : 'disabled'}>${approved ? 'Save correction' : 'Record my answer'}</button></div>${recordedAs}</form>` : '';
    const details = `<details class="history" id="decision-details"><summary>Details</summary>${d.signed_by || d.answered_by ? `<p class="context">${d.signed_by ? `Recorded signer: ${esc(d.signed_by)}. ` : ''}${d.answered_by ? `Answer recorded by: ${esc(d.answered_by)}.` : ''}</p>` : ''}${decisionReviewScope(d)}${d.consequences ? `<p class="context"><strong>Consequences:</strong> ${esc(typeof d.consequences === 'string' ? d.consequences : JSON.stringify(d.consequences))}</p>` : ''}${d.routing_reason ? `<p class="context"><strong>Routing:</strong> ${esc(d.routing_reason)}</p>` : ''}${d.owner_evidence ? `<p class="context"><strong>Why this owner:</strong> ${esc(d.owner_evidence)}</p>` : ''}${workItemContext(d)}${sourceEvidence(d.sources, d.needs_review ? null : d.source_revalidation)}${decisionBackground({...d, sources: [], source_revalidation: null, evidence: '', context: scope ? '' : d.context})}${canonical}${d.evidence ? `<p class="context"><strong>Evidence:</strong> ${esc(d.evidence)}</p>` : ''}${d.source_id && !box ? `<button class="button text small" data-action="source" data-id="${esc(d.source_id)}">Open the earlier decision</button>` : ''}${(d.allowed_actions || []).includes('rule') && (approved || (signed && d.signoff === 'signed')) && !inert ? ruleBox(d) : ''}</details>`;
    const more = [reframeControls(d), followupControls(d)].filter(Boolean).join('');
    openModal(d.question, `${d.agent} · ${d.run_title}`, `<div class="decision-status">${statusPill(d)}${readOnly ? `<span class="context">${readOnlyReason(d)}</span>` : ''}</div>
      ${readOnly ? ownerAccountNotice(d, true) : inert || settled ? '' : ownerAccountNotice(d, false)}
      ${decisionCurrentSummary(d, !box)}${upfront}${box}
      ${form}
      ${assignable ? handOnRow(d) : ''}
      ${details}
      ${decisionInquiry(d)}
      ${canDecide && !inert && state.auth?.enabled && state.me?.id && ['session','token'].includes(state.me.kind) ? `<button class="button text" data-action="interview-start" data-task="${esc(d.run_id)}" data-id="${esc(d.id)}">Talk it through instead</button>` : ''}
      ${more ? `<details class="history"><summary>More</summary>${more}</details>` : ''}
      <details class="history"><summary>History · ${plural(d.events.length, 'event')}</summary>${d.events.map(e => `<div class="history-item"><strong>${esc(eventLabels[e.kind] || e.kind)}</strong>${esc(e.detail)}<br><small>${esc(new Date(e.created_at).toLocaleString())}</small></div>`).join('')}</details>`);
  } catch (error) { notify(error.message); }
}

function showRun(id) {
  const run = state.runs.find(r => r.id === id);
  if (!run) return;
  const execution = (state.executions || []).find(r => r.run_id === id);
  const jobs = (state.deliveries || []).filter(d => d.run_id === id);
  const decisions = state.decisions.filter(d => d.run_id === id);
  const snapshot = execution?.snapshot || {};
  const progress = execution ? `<p class="modal-context">${esc(execution.task)}</p><div class="metadata">${pill(execution.status.replaceAll('_', ' '))}${execution.review_required ? pill('Correction requires re-evaluation') : ''}</div>${execution.last_error ? `<p class="error">${esc(execution.last_error)}</p>` : ''}<h3>Answer delivery</h3>${jobs.length ? jobs.map(j => `<p>${esc(j.kind)} · ${esc(j.state)} · ${j.attempts} ${j.attempts === 1 ? 'attempt' : 'attempts'}${j.last_error ? `<br>${esc(j.last_error)}` : ''}</p>`).join('') : '<p class="context">No answers queued.</p>'}<h3>Execution output</h3>${(snapshot.items || []).filter(i => ['message','command_execution'].includes(i.type)).map(i => `<details class="history"><summary>${esc(i.type === 'command_execution' ? `${i.command} · ${i.exit_code == null ? (i.status || 'pending') + ' · exit code unavailable' : 'exit ' + i.exit_code}` : `${i.role} · ${i.phase || 'message'}`)}</summary><pre>${esc(i.output || (i.content || []).map(c => c.text || '').join('\n'))}</pre></details>`).join('')}<h3>Files</h3>${(snapshot.artifacts || []).map(a => `<p><a href="/api/executions/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(a.id)}" download>${esc(a.path)}</a></p>`).join('')}<p class="context">Result ready means the turn ended and its output is available for inspection. Test commands and exit codes are shown above.</p>` : '<div class="notice">Run status is reported by the agent. Recording a decision makes the answer available; it does not execute or complete the agent’s code.</div>';
  const expanded = [...document.querySelectorAll('#modal details[open]')].map(d => d.querySelector('summary')?.textContent);
  const scroll = $('#modal').scrollTop;
  const verdict = run.verdict ? `<div class="context-box"><span class="label">Kickoff verdict · ${esc(run.verdict)}${run.requester ? ` · asked by ${esc(run.requester)}` : ''}</span>${esc(run.verdict_why || '')}</div>` : '';
  const ordered = [];
  const byParent = new Map();
  decisions.forEach(d => { const key = decisions.some(p => p.id === d.parent_id) ? d.parent_id : ''; if (!byParent.has(key)) byParent.set(key, []); byParent.get(key).push(d); });
  const walk = (parent, depth) => (byParent.get(parent) || []).forEach(d => { ordered.push([d, depth]); walk(d.id, depth + 1); });
  walk('', 0);
  const noteForm = `<details class="history"><summary>Add context for the agent</summary><form id="note-form" data-id="${esc(run.id)}"><label for="note-text">A note the agent reads on its tree</label><textarea id="note-text" name="text" maxlength="4000" placeholder="The Globex contract renews in March; do not change their rate before then." required></textarea><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button class="button primary small" type="submit">Add note</button></div></form></details>`;
  openModal(run.title, `${run.agent} · ${run.repo}`, `${progress}${verdict}${noteForm}<div class="decision-list">${ordered.length ? ordered.map(([d, depth], i) => `<div class="tree-node depth-${Math.min(depth, 6)}">${depth ? `<span class="muted">Level ${depth}${d.origin === 'human' ? ' · added by a person' : ''}${d.followup_required && d.status === 'suggested' ? ' · required before the task finishes' : ''}</span>` : ''}${decisionCard(d, i)}</div>`).join('') : '<p class="context">No decisions requested for this run.</p>'}</div>`);
  $('#modal').dataset.runId = id;
  document.querySelectorAll('#modal details').forEach(d => { d.open = expanded.includes(d.querySelector('summary')?.textContent); });
  $('#modal').scrollTop = scroll;
}

async function exportWorkspace() {
  const result = await api('/api/export');
  const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], {type:'application/json'}));
  const link = document.createElement('a'); link.href = url; link.download = `bridge-workspace-${new Date().toISOString().slice(0,10)}.json`; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  notify('Workspace history exported.');
}

document.addEventListener('click', async event => {
  const link = event.target.closest('a[href^="#"]');
  if (link && !event.defaultPrevented && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey && (!event.button || event.button === 0)) {
    const hash = link.getAttribute('href');
    if (hash && hash !== location.hash) resetScrollOnNavigation = true;
  }
  // A More menu item that is a link (Outcomes, Open conversation) closes the menu as a button does.
  const linkMenu = link?.closest?.('details.more-menu');
  if (linkMenu) linkMenu.open = false;
  const target = event.target.closest('[data-action]');
  if (!target || target.disabled) return;
  const action = target.dataset.action;
  // An item used closes its More menu before the redraw, which would otherwise keep it open.
  const itemMenu = target.closest?.('details.more-menu');
  if (itemMenu) itemMenu.open = false;
  // The dialog as it was when the action began: a response that lands
  // after the person closed it redraws nothing.
  const version = modalVersion;
  const done = busyLabels[action] ? busy(target, busyLabels[action]) : () => {};
  try {
    if (action === 'close') closeModal();
    if (action === 'new' || action === 'request-status') newRequest();
    if (action === 'request-new') { requestDraft = null; newRequest(); }
    if (action === 'request-view-run') closeModal();
    if (action === 'person') newPerson();
    if (action === 'team') newTeam();
    if (action === 'authority') newAuthority(target.dataset.person || '');
    if (action === 'end-authority') {
      target.disabled = true;
      await api(`/api/authority/${target.dataset.id}/end`, {});
      directory = null; render(); notify('Authority ended. Routing no longer relies on it.');
    }
    if (action === 'ingest') newIngest();
    if (action === 'github-connect') {
      target.disabled = true;
      await connectGitHub(); return;
    }
    if (action === 'github-authorize') { target.disabled = true; await authorizeGitHub(); return; }
    if (action === 'github-refresh') {
      target.disabled = true;
      const result = await api('/api/github/repositories/refresh', {});
      closeModal(); await refresh();
      notify(result.repositories.length ? 'Connected repositories refreshed. Sync has started.' : 'No repositories selected for Raven yet.');
      return;
    }
    if (action === 'use-suggestion') { const ta = $('#answer'); if (ta) { ta.value = target.dataset.text; ta.focus(); } }
    if (action === 'help') showHelp();
    if (action === 'help-connect') {
      closeModal();
      if (location.hash === '#connect') navigate();
      else location.hash = '#connect';
    }
    if (action === 'task') newTask();
    if (action === 'retry') await refresh();
    if (action === 'tab') { tab = target.dataset.tab; tabChosen = true; render(); }
    if (action === 'more') {
      const key = target.dataset.key;
      if (shown.has(key)) shown.delete(key); else shown.add(key);
      target.previousElementSibling?.classList.toggle('is-clamped', !shown.has(key));
      target.textContent = shown.has(key) ? 'Show less' : 'Show more';
    }
    if (action === 'review' || action === 'source') await review(target.dataset.id);
    if (action === 'run') location.hash = ((state.browser_tasks || []).some(t => t.task_id === target.dataset.id) ? 'work/' : 'runs/') + encodeURIComponent(target.dataset.id);
    if (action === 'task-tab') { taskTab = target.dataset.tab; taskView().tab = taskTab; render(); }
    if (action === 'task-brief-toggle') { taskView().briefOpen = !taskView().briefOpen; render(); }
    if (action === 'task-review') { taskTab = 'decisions'; taskView().tab = taskTab; render(); const heading = $('#task-review h2'); if (heading) { heading.focus(); heading.scrollIntoView({block: 'start'}); } }
    if (action === 'task-more') taskMore(target.dataset.key, Number(target.dataset.step) || 12);
    if (action === 'task-refresh') await loadTask();
    if (action === 'task-brief') { const r = await api(`/api/tasks/${encodeURIComponent(taskId())}/link`, {decision_id: taskBriefDecision()}); location.assign(r.url); }
    if (action === 'task-share') { await navigator.clipboard.writeText(location.href); notify('App link copied. Workspace access is required on shared instances.'); }
    if (action === 'execution-details') showRun(target.dataset.id);
    if (action === 'assign') {
      target.disabled = true;
      await api(`/api/decisions/${target.dataset.id}/assign`, {owner_id: $('#assign-owner').value});
      await rereview(target.dataset.id, version); notify('Decision reassigned.');
    }
    if (action === 'refer') {
      const owner = state.owners.find(o => o.id === $('#assign-owner').value);
      if (!owner) { notify('Pick the person to hand it to first.'); return; }
      target.disabled = true;
      const [scopeKind, ...scopeRest] = ($('#refer-scope')?.value || '').split(':');
      const result = await api(`/api/decisions/${target.dataset.id}/refer`, {person: owner.name, by: state.me?.name || 'Local operator', expected_updated_at: target.dataset.updated, ...(scopeKind ? {scope_kind: scopeKind, scope: scopeRest.join(':')} : {}), ...(target.dataset.override ? {override: true} : {})});
      await rereview(target.dataset.id, version); notify(result.notice);
    }
    if (action === 'end-rule') {
      target.disabled = true;
      const result = await api(`/api/decisions/${target.dataset.id}/rule`, {end: true, by: state.me?.name || 'Local operator', expected_updated_at: target.dataset.updated});
      await rereview(target.dataset.id, version); notify(result.notice);
    }
    if (action === 'signoff') {
      target.disabled = true;
      const d = state.decisions.find(x => x.id === target.dataset.id);
      const revalidation = $('#source-review-signoff')?.checked ? {
        source_evidence: JSON.parse($('#source-pins-signoff').value),
        source_decision_pins: JSON.parse($('#source-decisions-signoff').value),
      } : {};
      const signed = await api(`/api/decisions/${target.dataset.id}/signoff`, {by: (d && halfSigned(d) && stillToSign(d)[0]) || d?.owner_name || 'Local operator', expected_updated_at: target.dataset.updated || d?.updated_at || '', ...revalidation});
      await rereview(target.dataset.id, version); notify(signed?.notice || 'Signed off. The agent sees it on the tree.');
    }
    if (action === 'reframe') {
      const question = ($('#reframe-question')?.value || '').trim();
      const rationale = ($('#reframe-reason')?.value || '').trim();
      if (!question || !rationale) { notify('Write the corrected question and why it should change.'); return; }
      target.disabled = true;
      const result = await api(`/api/decisions/${target.dataset.id}/reframe`, {question, rationale, expected_updated_at: target.dataset.updated});
      await rereview(target.dataset.id, version); notify(result.notice);
    }
    if (action === 'followups') {
      const text = ($('#followups')?.value || '').trim();
      if (!text) { notify('Write one question per line first.'); return; }
      target.disabled = true;
      const d = state.decisions.find(x => x.id === target.dataset.id);
      const result = await api(`/api/decisions/${target.dataset.id}/followups`, {questions: text, required: !!$('#followups-required')?.checked, by: d?.owner_name || 'Local operator'});
      await rereview(target.dataset.id, version); notify(`${result.nodes.length} follow-up ${result.nodes.length === 1 ? 'question' : 'questions'} added to the agent's tree.`);
    }
    if (action === 'slack-sync') {
      target.disabled = true; await api('/api/slack/sync', {}); directory = null; await refresh(); notify('Slack contacts refreshed.');
    }
    if (action === 'fallback') {
      await api('/api/settings', {slack_fallback_channel: $('#fallback-channel').value.trim()});
      await refresh(); notify('Fallback channel saved.');
    }
    if (action === 'deliveries') {
      const result = await api('/api/deliveries?state=failed');
      $('#deliveries').innerHTML = result.notifications.length ? `<div class="table-wrap" role="region" aria-label="Failed deliveries" tabindex="0"><table><thead><tr><th>TO</th><th>KIND</th><th>ERROR</th><th></th></tr></thead><tbody>${result.notifications.map(n => `<tr><td>${esc(n.person_name)}</td><td>${esc(n.kind)}</td><td><small>${esc(n.last_error)}</small></td><td><button class="button small" data-action="retry-delivery" data-id="${esc(n.id)}">Retry</button></td></tr>`).join('')}</tbody></table></div>` : '<p class="context">Nothing failed.</p>';
    }
    if (action === 'retry-delivery') {
      target.disabled = true;
      await api(`/api/deliveries/${target.dataset.id}/retry`, {});
      notify('Queued again.'); await refresh();
    }
    if (action === 'export') await exportWorkspace();
    if (action === 'invite-person') openModal('Invite teammate', 'Invite-only access. No email is sent automatically.', inviteForm((await api('/api/people')).people || []));
    if (action === 'invite-owner') openModal('Invite teammate', 'Invite-only access. No email is sent automatically.', inviteForm((await api('/api/people')).people || [], {person_id: target.dataset.person, email: target.dataset.email}));
    if (action === 'agent-setup') await setupAgent();
    if (action === 'quick-agent') { target.disabled = true; await quickAgent(target.dataset.client); target.disabled = false; }
    if (action === 'launch-agent') await launchAgent(target.dataset.client, target.dataset.mode);
    if (action === 'confirm-launch') {
      target.disabled = true;
      const created = await api('/api/tokens', {label: target.dataset.client + ' · local launcher'});
      try {
        await localLauncher('launch', {client: target.dataset.client, action: target.dataset.mode, token: created.token});
        markAgentConnected();
      } catch (error) {
        if (created.id) await api(`/api/tokens/${created.id}/revoke`, {}).catch(() => {});
        throw error;
      }
      closeModal(); notify('Terminal launch requested. Approve the connection in your agent.');
    }
    if (action === 'agent-credential') await agentCredential(target);
    if (action === 'agent-status') {
      await refresh();
    }
    if (action === 'token') {
      target.disabled = true;
      const created = await api('/api/tokens', {label: ($('#token-label')?.value || '').trim() || 'agent'});
      const box = $('#token-value');
      box.textContent = `${created.token}\n\n${created.notice}`;
      box.hidden = false;
      target.disabled = false;
      notify('Token created. Copy it now; it is not shown again.');
    }
    if (action === 'copy') {
      const source = $(target.dataset.target || '#mcp-config');
      await navigator.clipboard.writeText(source.textContent);
      notify(target.dataset.target ? 'Instruction copied.' : 'Configuration copied for this workspace.');
    }
  } catch (error) { notify(error.message); done(); target.disabled = false; }
});

document.addEventListener('input', event => {
  if (event.target.id === 'inbox-search') { query = event.target.value; render(); announce(query ? `${plural(inboxShown, 'decision')} match` : ''); }
});
document.addEventListener('change', async event => {
  if (event.target.id === 'owner-filter') { ownerFilter = event.target.value; render(); }
  if (event.target.id === 'refer-learn') {
    const button = $('#hand-on'), row = $('#refer-scope-row');
    if (button && !button.dataset.override) button.dataset.action = event.target.checked ? 'refer' : 'assign';
    if (row) row.hidden = !event.target.checked;
  }
  try {
    if (event.target.id === 'coordinator-select') {
      await api('/api/settings', {coordinator: event.target.value});
      directory = null; render(); notify(event.target.value ? 'Fallback person set. Questions nobody is known to own go to them.' : 'Fallback person cleared.');
    }
    if (event.target.id === 'verified-only') {
      await api('/api/settings', {require_verified_route: event.target.checked});
      directory = null; render(); notify(event.target.checked ? 'Only verified owners are asked now.' : 'Git history can suggest owners again.');
    }
    if (event.target.id === 'brief-mode') {
      await api('/api/settings', {brief_mode: event.target.value});
      directory = null; render(); notify({off:'Task links are off: messages link to the inbox.', static:'Messages link to the static task page.'}[event.target.value]);
    }
    if (event.target.id === 'auto-rules') {
      await api('/api/settings', {auto_rules: event.target.checked});
      directory = null; render(); notify(event.target.checked ? 'Automatic rules on: a matching rule authorizes without a fresh signature.' : 'Automatic rules off: a matching rule is shown, and a person still signs.');
    }
  } catch (error) { notify(error.message); directory = null; render(); }
});
document.addEventListener('submit', async event => {
  const form = event.target;
  if (!['invite-form','request-form','answer-form','correct-form','task-form','ingest-form','person-form','team-form','authority-form','rule-form','note-form','task-inquiry-form'].includes(form.id)) return;
  event.preventDefault();
  if (form.id === 'request-form') { await submitRequest(form); return; }
  if (form.id === 'note-form') { await submitTaskNote(form); return; }
  if (form.id === 'task-inquiry-form') { await submitTaskInquiry(form); return; }
  const button = form.querySelector('[type="submit"]');
  if (button.disabled || !form.reportValidity()) return;
  const done = busy(button, form.id === 'task-form' ? 'Starting…' : form.id === 'ingest-form' ? 'Ingesting…'
    : ['answer-form', 'correct-form', 'rule-form'].includes(form.id) ? 'Recording…' : 'Saving…');
  const errorEl = form.querySelector('#form-error');
  errorEl.hidden = true;
  const data = Object.fromEntries(new FormData(form));
  try {
    if (form.id === 'invite-form') {
      const invitation = await api('/api/invitations', data);
      openModal('Invitation ready', 'Share privately with ' + data.email + '. Expires in 48 hours.', `<pre id="mcp-config">${esc(invitation.url)}</pre><button class="button" data-action="copy">Copy invitation link</button><p>${invitation.person ? `Whoever accepts it signs in as ${esc(invitation.person.name)}, with the authority recorded for them. ` : ''}The recipient sets their own password. This link grants access; creating another invitation for this email invalidates the previous one.</p>`);
      return;
    }
    if (form.id === 'task-form') {
      await api('/api/tasks', {...data, submission_key:form.dataset.submissionKey});
      notify('Task queued. Follow progress in Tasks.');
      location.hash = 'runs';
    } else if (form.id === 'rule-form') {
      const d = state.decisions.find(x => x.id === form.dataset.id);
      const result = await api(`/api/decisions/${form.dataset.id}/rule`, {by: state.me?.name || d?.signed_by || d?.owner_name || 'Local operator', expected_updated_at: form.dataset.updated, ...data});
      notify(result.notice);
    } else if (form.id === 'correct-form') {
      const d = state.decisions.find(x => x.id === form.dataset.id);
      takeSourceRevalidation(data);
      takeApplicability(data);
      await api(`/api/decisions/${form.dataset.id}/signoff`, {by: d?.owner_name || 'Local operator', ...data});
      notify('Correction signed. The agent sees it on the tree.');
    } else if (form.id === 'person-form') {
      await api('/api/people', data); notify('Person added. Routing, Slack and sign-off now agree on who they are.'); directory = null;
    } else if (form.id === 'team-form') {
      await api('/api/teams', data); notify('Team added. CODEOWNERS entries naming it now route to its members.'); directory = null;
    } else if (form.id === 'authority-form') {
      const [kind, id] = String(data.who || '').split(':');
      const payload = {scope_kind: data.scope_kind, scope: data.scope, role: data.role, repo: data.repo, note: data.note, effective_to: data.effective_to, by: 'Local operator'};
      if (kind === 'team') payload.team = id; else payload.person = id;
      await api('/api/authority', payload); notify('Authority recorded. It outranks git history from now on.'); directory = null;
    } else if (form.id === 'ingest-form') {
      const stats = await api('/api/ingest', data); notify(`Ingested ${esc(stats.repo)}: ${stats.commits} commits, ${stats.owners} ownership signals.`);
      ownership = null;
    } else {
      if (!data.supersedes) delete data.supersedes;
      takeSourceRevalidation(data);
      takeApplicability(data);
      await api(`/api/decisions/${form.dataset.id}/answer`, data); notify('Decision saved. The answer is available to the agent.');
    }
    closeModal();
    await refresh();
  } catch (error) {
    errorEl.textContent = error.message;
    errorEl.hidden = false;
    done();
  }
});
// A page's More menu (details.more-menu) closes on Escape, giving focus
// back to its summary, when focus leaves it, and on a press anywhere
// outside it: on a phone it is a sheet over the bottom of the page, and a
// tap on plain text moves no focus, so focusout alone left it open.
document.addEventListener('pointerdown', event => {
  const menu = document.querySelector('details.more-menu[open]');
  if (menu && !menu.contains(event.target)) menu.open = false;
});
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  const menu = document.querySelector('details.more-menu[open]');
  if (!menu) return;
  menu.open = false;
  menu.querySelector('summary')?.focus();
});
document.addEventListener('focusout', event => {
  const menu = document.querySelector('details.more-menu[open]');
  if (menu && event.relatedTarget && !menu.contains(event.relatedTarget)) menu.open = false;
});
$('#modal').addEventListener('cancel', event => { event.preventDefault(); closeModal(); });
$('#modal').addEventListener('click', event => { if (event.target === $('#modal')) { const r = $('#modal').getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) closeModal(); } });
window.addEventListener('hashchange', navigate);
navigate();
refresh().then(() => {
  const params = new URLSearchParams(window.location.search);
  if (params.has('github_connected')) notify('GitHub connected. Repository sync has started.');
  if (params.has('github_error')) notify(`GitHub connection stopped: ${params.get('github_error')}`);
  if (params.has('github_connected') || params.has('github_error') || params.has('github_connect'))
    window.history.replaceState(null, '', window.location.pathname + window.location.hash);
  if (params.get('github_connect') === '1') {
    if (state.me?.role !== 'admin') notify('An administrator must connect GitHub.');
    else connectGitHub().catch(error => notify(`GitHub connection stopped: ${error.message}`));
  }
});
setInterval(() => { if (!document.hidden) refresh({quiet:true}); }, 5000);
// A hidden tab is not polled; the moment it is shown again it catches up,
// rather than showing stale rows for up to five more seconds.
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh({quiet:true}); });

// Under step 1: the hosts that can run a task from the browser, and what
// the server needs for that. objectiveCapabilities() (objectives.js) is the
// source of the host rows when it returns them as data.
function workbenchSetupCard() {
  const config = state.workbench || {};
  const reported = typeof objectiveCapabilities === 'function' ? objectiveCapabilities() : null;
  const hosts = Array.isArray(reported) ? reported : (config.hosts || []);
  const rows = hosts.map(host => `<p class="host-row">${esc(host.name || host.id)} · ${host.ready ? 'Ready' : 'Set up'}</p>`).join('');
  return `<details class="history" id="browser-tasks"><summary>Run tasks from the browser</summary>${rows}${config.ready ? '<p><a class="button" href="#work">Start a task</a></p>' : '<pre>python -m bridge serve --workbench \\\n  --managed-repo your-org/project=/path/to/checkout</pre><p>Set ANTHROPIC_API_KEY and BRIDGE_MODEL_API=anthropic on the server.</p>'}<p><a href="https://github.com/nintendo-ds75/raven-preview/blob/main/docs/browser-tasks.md" target="_blank" rel="noopener">Browser setup and isolated tests ↗</a></p></details>`;
}

document.addEventListener('submit', async event => {
  const form = event.target;
  if (!form.dataset.decisionInquiry) return;
  event.preventDefault();
  const button = form.querySelector('button'), output = form.querySelector('.decision-inquiry-result');
  if (button.ariaDisabled === 'true' || !form.reportValidity()) return;
  button.ariaDisabled = 'true'; output.textContent = 'Reading task context…';
  const requestVersion = modalVersion;
  // A reading that needs the model is capped: with a slow or unreachable
  // model endpoint the dialog otherwise waits for minutes on one line of text.
  const wait = typeof AbortController === 'function' ? new AbortController() : null;
  const cap = setTimeout(() => wait && wait.abort(), MODEL_WAIT_MS);
  try {
    const result = await api(`/api/tasks/${encodeURIComponent(form.dataset.task)}/inquiry`, {question:form.querySelector('textarea').value.trim(),focus_id:form.dataset.decisionInquiry}, {signal: wait && wait.signal});
    if (requestVersion !== modalVersion || !form.isConnected) return;
    output.innerHTML = `<p>${esc(result.answer)}</p><small>${esc(result.notice || 'Clarification does not authorize work.')}</small>${inquiryCitations(result, 'decision-inquiry-citations')}${(result.evidence || []).length ? `<details><summary>Evidence used</summary>${result.evidence.map(e => `<h4>${esc(e.title || e.id)}</h4><p>${esc(e.text)}</p>`).join('')}</details>` : ''}`;
  } catch (error) { if (requestVersion === modalVersion && form.isConnected) output.textContent = modelWaitMessage(error); }
  finally { clearTimeout(cap); button.ariaDisabled = 'false'; }
});

function modelWaitMessage(error) {
  return error && error.name === 'AbortError'
    ? `No reading came back within ${Math.round(MODEL_WAIT_MS / 1000)} seconds. The records stand as shown; ask again in a moment.`
    : error.message;
}
