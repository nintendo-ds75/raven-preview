'use strict';

function restoreLoginRoute() {
  try {
    const raw = sessionStorage.getItem('raven-return-decision');
    if (!raw) return;
    sessionStorage.removeItem('raven-return-decision');
    const saved = JSON.parse(raw);
    if (!['', '#inbox'].includes(location.hash) || !/^#runs\/[a-f0-9]{12,32}\/[a-f0-9]{12,32}$/.test(saved.route || '') ||
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
// A task's status as the page says it; the raw value ("working") stays in the API.
// A finished task reads "Completed", the word a viewer looks for; the task
// page keeps the caveat that the agent reported it and nobody verified it.
const RUN_STATUS = {working: 'In progress', needs_judgment: 'Waiting for decisions', pending: 'Starting',
  completed: 'Completed', abandoned: 'Closed', result_ready: 'Result ready for inspection',
  failed: 'Failed', cancelled: 'Cancelled', review_required: 'Review needed', delivery_pending: 'Delivering answers',
  environment_pending: 'Preparing the environment', duplicate: 'Duplicate'};
const runStatusLabel = status => RUN_STATUS[status] || String(status || '').replaceAll('_', ' ');
const initials = name => (name || '?').split(/\s+/).map(word => word[0]).slice(0, 2).join('');
const avatar = (name, index = 0) => `<span class="avatar ${['sage','lilac','sand'][index % 3]}">${esc(initials(name))}</span>`;
const ago = value => {
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(value)) / 60000));
  return minutes < 1 ? 'just now' : minutes < 60 ? `${minutes}m ago` : minutes < 1440 ? `${Math.floor(minutes / 60)}h ago` : `${Math.floor(minutes / 1440)}d ago`;
};
const pill = (text, color = '') => `<span class="pill ${color}"><span class="status-dot"></span>${esc(text)}</span>`;
const titles = {
  today: ['Today', '', 'Today', ''],
  work: ['Raven', '', 'What should we work on?', ''],
  inbox: ['Judgment inbox', '', 'Inbox', 'Decisions your coding agents need from a person. Answer, sign, or hand on.'],
  runs: ['Tasks', '', 'Tasks', 'Every task your agents registered: what is settled, what is waiting, and on whom.'],
  memory: ['Decision memory', '', 'Memory', 'Past answers and source evidence, with their owner, scope, and approval status.'],
  owners: ['People & ownership', '', 'People', 'Questions go to whoever this map says decides; where it is silent, to repository history and the coordinator. Readiness says what is missing.'],
  connect: ['Connections & setup', '', 'Set up Raven', 'Three steps: connect your coding agent, give Raven context, invite the people who decide.'],
};
let state = {decisions: [], runs: [], owners: [], events: []};
let workspaceError = '';
// The ownership graph and the directory (people, teams, the authority map, the coordinator) are not in the
// polled state: the owners view fetches them when it renders.
let ownership = null, ownershipLoading = false;
let savings = null, savingsLoading = false, savingsAt = 0;
let directory = null, directoryLoading = false;
let view = 'inbox', tab = 'pending', tabChosen = false, query = '', ownerFilter = '', fetching = false;
// The ETag of the state on screen, sent back with each poll; and when the
// list was last drawn, so relative times still move while nothing changes.
let stateTag = '', lastDraw = 0;
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
  const response = await fetch(path, data === undefined ? {headers, signal, ...(conditional ? {cache: 'no-store'} : {})} : {
    method: 'POST', headers, body: JSON.stringify(data), signal,
  });
  if (conditional && response.status === 304) return null;
  const body = await response.json();
  if (conditional !== undefined && response.ok && body && typeof body === 'object') {
    Object.defineProperty(body, 'etag', {value: (response.headers && response.headers.get && response.headers.get('ETag')) || ''});
  }
  if (!response.ok) { const error = new Error(body.error || 'Request failed. Please try again.'); error.status = response.status; throw error; }
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

function updateWorkspaceName() {
  const label = $('#workspace-name');
  label.textContent = state.workspace?.name || 'Workspace';
  label.title = label.textContent;
}

async function refresh({quiet = false} = {}) {
  if (fetching) return;
  fetching = true;
  try {
    const changed = await fetchState();
    workspaceError = '';
    $('#connection').classList.remove('offline');
    if (changed) {
      updateWorkspaceName();
      updateAgentConnectPrompt();
      if ($('#agent-status')) $('#agent-status').innerHTML = agentStatus();
      $('#connection').innerHTML = state.auth && state.auth.enabled ? '<i></i> Shared workspace' : '<i></i> Local workspace';
      if (state.me) {
        $('#profile-name').textContent = state.me.name;
        $('#profile-avatar').textContent = initials(state.me.name);
        $('#profile-note').innerHTML = state.auth && state.auth.enabled ? `${esc(state.me.role)} · <a href="/auth/logout">Sign out</a>` : 'Self-hosted workspace';
      }
      if (personalInbox() && !tabChosen) tab = 'mine';
      $('#inbox-count').textContent = personalInbox() ? state.decisions.filter(assignedToMe).length
        : state.counts ? state.counts.needs_you : state.decisions.filter(needsYou).length;
      // What Today wants from the person: open proposals and blocked objectives.
      const today = typeof objectiveTodayCount === 'function' ? objectiveTodayCount(state.today) : 0;
      if ($('#today-count')) { $('#today-count').textContent = today; $('#today-count').hidden = !today; }
    }
    renderModelStrip();
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
    $('#connection').classList.add('offline');
    $('#connection').innerHTML = '<i></i> Connection lost';
    if ([401,403].includes(error.status)) {
      if (typeof rememberTaskView === 'function') rememberTaskView();
      state.auth = null; workspaceError = error.message; taskDetail = null;
      if ($('#modal').open) closeModal();
      render();
    } else if (taskId()) { taskError = error.message; render(); }
    else if (!quiet) $('#app').innerHTML = `<div class="empty"><h3>We couldn’t reach your workspace.</h3><p>${esc(error.message)}</p><button class="button" data-action="retry">Try again</button></div>`;
  } finally { fetching = false; }
}

// One line under the page head on Today, Inbox and Connect: what the
// configured backend lets Raven do, and where questions wait when no Slack
// is connected. Read from /api/state; nothing here probes a provider.
function renderModelStrip() {
  const strip = $('#model-strip');
  if (!strip) return;
  const lines = [state.model?.notice || '', state.delivery && !state.delivery.enabled ? 'No Slack connected: questions wait in the inbox.' : ''].filter(Boolean);
  const shown = !!state.auth && ['today', 'inbox', 'connect'].includes(view) && lines.length > 0;
  strip.textContent = shown ? lines.join(' ') : '';
  strip.hidden = !shown;
}

function loadOwnership() {
  if (ownershipLoading) return;
  ownershipLoading = true;
  api('/api/ownership').then(result => { ownership = result.ownership; if (view === 'owners') render(); })
    .catch(error => notify(error.message)).finally(() => { ownershipLoading = false; });
}

function loadDirectory() {
  if (directoryLoading) return;
  directoryLoading = true;
  api('/api/people').then(result => { directory = result; if (view === 'owners') render(); })
    .catch(error => notify(error.message)).finally(() => { directoryLoading = false; });
}

function navigate() {
  navigationVersion += 1;
  const next = location.hash.slice(1).split('/')[0];
  view = titles[next] ? next : 'work';
  query = ''; ownerFilter = ''; ownership = null; directory = null;
  const [breadcrumb, eyebrow, title, description] = titles[view];
  $('#breadcrumb').textContent = breadcrumb;
  $('#eyebrow').textContent = eyebrow;
  $('#page-title').textContent = title;
  $('#page-description').textContent = description;
  document.title = view === 'work' ? 'Raven' : `Raven · ${breadcrumb}`;
  $('#new-request').hidden = view === 'today' || view === 'connect';
  $('#new-request').innerHTML = `${icon('plus')}${view === 'owners' ? 'Add owner' : 'Ask Raven'}`;
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

function kindBadge(d) {
  // A recorded answer is a person's answer, whatever it began as. Measured live on 5e967e4: a signed docs
  // card showed a Prediction badge beside "Answer recorded".
  const kind = d.status === 'approved' ? 'answer' : d.kind || (d.prediction ? 'prediction' : 'new');
  return `<span class="pill ${kind === 'evidence' ? 'green' : kind === 'prediction' ? '' : 'gray'}">${esc(kindLabels[kind] || kind)}</span>`;
}

function decisionCard(d, index) {
  const approved = d.status === 'approved';
  const settled = settledNow(d);
  // An objective's proposed scope is stored as JSON; the card reads it as a scope, never as JSON.
  const scope = typeof objectiveScope === 'function' ? objectiveScope(d) : null;
  let box;
  if (d.needs_review) box = `<div class="context-box prediction"><span class="label">Needs review · ${esc(d.review_reason || 'an answer it leaned on was corrected')}</span>${esc(d.answer || d.prediction || '')}</div>`;
  else if (approved) box = `<div class="context-box"><span class="label">Recorded answer · ${esc(d.answered_by || d.owner_name || '')}</span>${esc(d.answer)}</div>`;
  else if (d.status === 'resolved' || d.status === 'partial') box = `<div class="context-box"><span class="label">${resolvedLabel(d)} · ${esc(d.kind === 'agent' ? d.rationale || '' : d.evidence || '')}</span>${esc(d.answer)}</div>`;
  else if ((d.status === 'assumed' || d.status === 'proposed') && signedNow(d)) box = `<div class="context-box"><span class="label">${esc(signedAs(d))} · began as ${esc(beganAs(d))}</span>${esc(d.answer || d.prediction || '')}</div>`;
  else if (d.status === 'assumed' || d.status === 'proposed') box = `<div class="context-box prediction"><span class="label">${d.status === 'assumed' ? 'Default assumed · confirm or correct' : 'Prediction, unconfirmed · not a decision'}</span>${esc(d.answer || d.prediction || '')}</div>`;
  else if (d.prediction) box = `<div class="context-box prediction"><span class="label">Suggested from a prior decision · approval pending</span>${esc(d.prediction)}</div>`;
  else if (d.brief) box = `<div class="context-box"><span class="label">The brief · ${esc(d.run_title)}</span>${esc(d.brief)}</div>`;
  else box = `<div class="context-box"><span class="label">Working on</span>${esc(d.run_title)} <span class="muted">/ ${esc(d.path)}</span></div>`;
  return `<article class="decision-card">
    <div class="card-top"><span class="agent-symbol">${d.agent.includes('Claude') ? '✳' : d.agent.includes('Codex') ? '⌘' : '◇'}</span><span>${esc(d.agent)}</span><span>·</span><span class="repo">${esc(d.repo)}</span>${kindBadge(d)}<time datetime="${esc(d.created_at)}">${ago(d.created_at)}</time></div>
    <h3>${esc(d.question)}</h3>${scope ? objectiveScopeCard(scope, `scope-${d.id}`) : `<p class="context">${esc(d.context)}</p>`}
    ${box}
    <div class="card-bottom"><span class="owner">${avatar(d.owner_name, index)}${esc(d.owner_name || 'Unassigned')}</span>${pill(statusLabel(d), settled ? 'green' : '')}${signoffLabel(d)}<button class="button small ${settled ? '' : 'soft'}" data-action="review" data-id="${esc(d.id)}">${settled ? 'View decision' : 'Review'} ${icon('arrow')}</button></div>
  </article>`;
}

const eventLabels = {signed_in:'Person signed in', conformance_read:'Code review saved', task_note:'Context added', expand:'Decision explored', run_complete:'Evidence search finished', memory_reranked:'Prior answers compared', brief_withheld:'Unreliable brief withheld',task_started:'Task kicked off', node_added:'Node written to the tree', node_settled:'Settled by the agent', followup_added:'Follow-up question added', signoff:'Signed off', run_started:'Task registered', judgment_requested:'Judgment requested', prediction_suggested:'Prior answer suggested', owner_approved:'Decision recorded', answer_corrected:'Answer corrected', previous_answer:'Previous answer retained', owner_changed:'Owner reassigned', run_updated:'Run status updated', resolve:'Resolved from evidence', assume:'Default assumed', proposed:'Unratified proposal found', dedup_open:'Duplicate of an open question', twin_closed:'Twin question settled', route_unknown:'No owner in the graph', ask_drafted:'Routed from the ownership graph', index:'Repository ingested', conflict_resolved:'Newest answer won a conflict', memory_record_conflict:'Memory and record disagree', superseded:'Superseded', superseded_record:'Stale record replaced by newer', ownership_invalidated:'Ownership changed', followup_context:'Follow-up context resolved', route_learned:'Owner learned', overturn:'Answer overturned', answer:'Answer recorded'};
const quietEvents = new Set(['probe','expand','decompose','run_complete','previous_answer','memory_noop','memory_updated']);
const kindLabels = {evidence:'Evidence', prediction:'Prediction', new:'New judgment', agent:'Agent-settled', followup:'Follow-up', answer:'Answered by a person'};
const statusLabels = {pending:'Needs judgment', approved:'Answer recorded', resolved:'Resolved from evidence', partial:'Partly resolved', assumed:'Default assumed', proposed:'Prediction, unconfirmed', duplicate:'Duplicate', suggested:'Follow-up for the agent', adopted:'Adopted by the agent'};
// A signature, or a rule a person made, is what authorizes an answer; where the answer came from
// (evidence, a prediction, an assumed default) stays visible, and never reads as unconfirmed once signed.
const signedNow = d => ['signed','rule'].includes(d.signoff) && !d.needs_review;
const signedAs = d => d.signoff === 'rule' ? 'Covered by a reusable rule' : `Signed${d.signed_by ? ` · ${d.signed_by}` : ''}`;
// How a suggested answer relates to what it leans on, and the scope that answer was given for when this
// request is outside it: "the same policy", "an analogy" and a conflict must read differently to the owner.
const predictionScope = d => { const m = /(?:^|; )prediction scope: ([^;]+)/.exec(d.evidence || ''); return m ? m[1].trim() : ''; };
const predictionLabel = d => /how they decide \(the same policy/.test(d.evidence || '') ? 'Your earlier policy, applied here · confirm or correct' : /how they decide/.test(d.evidence || '') ? 'A guess from your earlier answers · confirm or correct' : 'Suggested from a prior answer · not approved';
const beganAs = d => d.kind === 'agent' ? 'the agent’s own answer' : d.status === 'assumed' ? 'a default Raven assumed' : d.status === 'proposed' ? 'a prediction from an earlier answer' : d.status === 'partial' ? 'a partial answer from evidence' : 'an answer from evidence';
// Who a decision still waits on: each required approver without a
// signature on it. Measured live: a decision its owner had answered read
// "Resolved from evidence", with no word of the reviewer it still needed.
const listOf = raw => { try { const v = Array.isArray(raw) ? raw : JSON.parse(raw || '[]'); return Array.isArray(v) ? v : []; } catch { return []; } };
const signersOf = d => listOf(d.signatures).map(s => String(s?.by || '')).filter(Boolean);
const stillToSign = d => { const done = signersOf(d).map(n => n.toLowerCase()); return listOf(d.required_signers).map(String).filter(n => !done.includes(n.toLowerCase())); };
const halfSigned = d => d.signoff === 'required' && signersOf(d).length > 0;
const statusLabel = d => d.needs_review ? 'Needs review' : halfSigned(d) ? 'Signed in part' : d.status === 'suggested' && d.followup_required ? 'Required follow-up' : d.status === 'proposed' && signedNow(d) ? 'Signed prediction' : d.status === 'assumed' && signedNow(d) ? 'Signed default' : d.kind === 'agent' && d.status === 'resolved' ? 'Settled by the agent' : statusLabels[d.status] || d.status;
const resolvedLabel = d => halfSigned(d) ? `${d.kind === 'answer' ? 'Answered and signed' : 'Signed'} by ${signersOf(d).join(', ')}, still waiting on ${stillToSign(d).join(', ') || 'a required approver'}` : d.kind === 'answer' ? `Answered by ${d.answered_by || 'a person'}` : d.kind === 'agent' ? 'Settled by the agent' : d.status === 'partial' ? 'Partly resolved from evidence' : d.status === 'assumed' ? 'Default assumed' : d.status === 'proposed' ? 'Prediction, unconfirmed' : 'Resolved from evidence';
const signoffLabel = d => d.signoff === 'required' ? pill(halfSigned(d) && stillToSign(d).length ? `Waiting on ${stillToSign(d).join(', ')}` : 'Sign-off wanted') : d.signoff === 'signed' ? pill(`Signed · ${d.signed_by || ''}`, 'green') : d.signoff === 'rule' ? pill('Covered by a reusable rule', 'green') : '';
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

function empty(title, description, action = '', button = '') {
  return `<div class="empty">${icon('inbox')}<h3>${esc(title)}</h3><p>${esc(description)}</p>${action ? `<button class="button soft" data-action="${action}">${esc(button)}</button>` : ''}</div>`;
}

// An empty tab says what it would hold and where the open work is. Measured
// in Chromium: an empty Overdue tab and an empty Assigned-to-me tab both
// read "You’re all caught up" above a workspace with four open decisions.
function emptyInbox(query, tab, open) {
  if (query) return empty('No matching decisions.', 'Try a different search.');
  if (!state.decisions.length) return empty('You’re all caught up.', 'When an agent needs a person, the decision appears here.', 'help-connect', 'Connect an agent');
  const elsewhere = open ? `${plural(open, 'decision')} elsewhere in the workspace ${open === 1 ? 'needs' : 'need'} a person: see Needs review.` : 'Nothing in the workspace waits on a person.';
  if (tab === 'overdue') return empty('Nothing is overdue.', `Decisions open longer than ${Math.round(((state.settings && state.settings.overdue_hours) || 72) / 24)} days appear here. ${elsewhere}`);
  if (tab === 'mine') return empty('Nothing waits on you.', elsewhere);
  if (tab === 'predicted') return empty('No suggestions awaiting review.', 'An answer Raven suggests from an earlier decision appears here until a person confirms it.');
  if (tab === 'evidence') return empty('Nothing resolved from evidence.', 'An answer Raven finds in connected records appears here for its owner to sign.');
  return empty('You’re all caught up.', 'When an agent needs a person, the decision appears here.');
}

function inbox() {
  const pending = state.decisions.filter(needsYou);
  const isPredicted = d => (d.status === 'pending' && d.prediction) || (['assumed','proposed'].includes(d.status) && !signedNow(d));
  const isEvidence = d => ['resolved','partial'].includes(d.status);
  const overdueMs = ((state.settings && state.settings.overdue_hours) || 72) * 3600e3;
  const isOverdue = d => d.status === 'pending' && (Date.now() - new Date(d.created_at).getTime()) > overdueMs;
  const mine = assignedToMe;
  const rows = state.decisions.filter(d => (tab === 'all' || (tab === 'mine' ? mine(d) : tab === 'predicted' ? isPredicted(d) : tab === 'evidence' ? isEvidence(d) : tab === 'overdue' ? isOverdue(d) : needsYou(d))) && `${d.question} ${d.context} ${d.owner_name}`.toLowerCase().includes(query.toLowerCase()));
  const demo = state.runs.some(r => r.agent.startsWith('Demo'));
  return `${state.execution_config?.enabled ? '<div class="wide-toolbar"><button class="button primary" data-action="task">Start a task</button></div>' : ''}${demo ? `<div class="demo-banner">${icon('help')}Sample workspace · Demo runs are illustrative. New agent requests will appear here in real time.</div>` : ''}
    <div class="work-grid"><section><div class="section-header"><h2>Across your workspace</h2>${pill(pending.length, 'gray')}<span class="muted">Each question names the person needed.</span></div>
    <div class="toolbar">${[...(state.auth?.enabled && state.me?.role !== 'viewer' ? [['mine','Assigned to me',state.decisions.filter(mine).length]] : []),['pending','Needs review',pending.length],['overdue','Overdue',state.decisions.filter(isOverdue).length],['predicted','Suggested',state.decisions.filter(isPredicted).length],['evidence','From evidence',state.decisions.filter(isEvidence).length],['all','All decisions',state.decisions.length]].map(([key,label,count]) => `<button class="tab ${tab === key ? 'active' : ''}" data-action="tab" data-tab="${key}" aria-pressed="${tab === key}">${label}<span>${count}</span></button>`).join('')}<label class="search" aria-label="Search decisions">${icon('search')}<input id="inbox-search" type="search" placeholder="Search…" value="${esc(query)}"></label></div>
    <div class="decision-list">${rows.length ? rows.map(decisionCard).join('') : emptyInbox(query, tab, pending.length)}</div>
    <div class="section-footnote">${icon('shield')}Every answer keeps its owner, context, and history.</div></section>
    </div>`;
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
  api('/api/metrics?days=30').then(result => { savings = result; savingsAt = Date.now(); if (['runs','owners'].includes(view)) render(); })
    // The numbers are a member's to read; a viewer gets no strip and no error.
    .catch(error => { if (error.status === 403) { savings = false; savingsAt = Date.now(); if (['runs','owners'].includes(view)) render(); } else notify(error.message); })
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

function runs() {
  return savedStrip() + `<div class="wide-toolbar"><h2>Tasks in this workspace</h2>${!state.auth?.enabled || ['admin','member'].includes(state.me?.role) ? '<button class="button primary" data-action="new">Ask Raven</button>' : ''}${state.execution_config?.enabled && state.me?.role !== 'viewer' ? '<button class="button" data-action="task">Start hosted execution</button>' : ''}<span class="muted">${plural(state.runs.length, 'task')}</span></div>${state.runs.length ? `<div class="table-wrap" role="region" aria-label="Tasks" tabindex="0"><table><thead><tr><th>TASK / REPOSITORY</th><th>AGENT</th><th>STATUS</th><th>DECISIONS</th><th>UPDATED</th><th></th></tr></thead><tbody>${state.runs.map(run => {
    const decisions = state.decisions.filter(d => d.run_id === run.id);
    const pending = decisions.filter(needsYou);
    return `<tr><td><strong>${esc(run.title)}</strong><small>${esc(run.repo)}</small></td><td>${esc(run.agent)}</td><td>${pill(pending.length ? 'Waiting for decisions' : runStatusLabel(run.status), pending.length ? '' : 'gray')}</td><td>${decisions.length} total${pending.length ? ` · ${pending.length} need a person` : ''}</td><td>${ago(run.updated_at)}</td><td><button class="button small" data-action="run" data-id="${esc(run.id)}">View run</button></td></tr>`;
  }).join('')}</tbody></table></div>` : empty('No tasks yet.', 'Connect your coding agent, then give it a task. It appears here with its decisions.', 'help-connect', 'Connect an agent')}`;
}

function memory() {
  const decisions = state.decisions.filter(d => settledStatuses.includes(d.status) && !d.superseded_by && (!ownerFilter || d.owner_id === ownerFilter) && `${d.question} ${d.answer} ${d.rationale} ${d.owner_name} ${d.evidence}`.toLowerCase().includes(query.toLowerCase()));
  return `<p class="notice">A past answer is evidence for the next task, not permission to use it. Fresh owner signoff is required unless an enabled standing rule covers the new case.</p><div class="filter-row"><input id="memory-search" type="search" placeholder="Search questions, answers, or rationale…" aria-label="Search decision memory" value="${esc(query)}"><select id="owner-filter" aria-label="Filter by owner"><option value="">All owners</option>${state.owners.map(o => `<option value="${esc(o.id)}" ${ownerFilter === o.id ? 'selected' : ''}>${esc(o.name)}</option>`).join('')}</select><button class="button" data-action="export">Export history ↗</button></div><div class="memory-list">${decisions.length ? decisions.map(d => `<article class="decision-card"><div class="card-top">${pill(statusLabel(d),signedNow(d) ? 'green' : '')}${signoffLabel(d)}${kindBadge(d)}<span>${esc(d.answered_by || d.owner_name || '')}</span><time>${ago(d.updated_at)}</time></div><h3>${esc(d.question)}</h3><p class="result-answer">${esc(d.answer)}</p><p class="context">${esc(d.rationale || d.evidence || '')}</p><button class="button small" data-action="review" data-id="${esc(d.id)}">Context & history ${icon('arrow')}</button></article>`).join('') : empty('A place for decisions worth remembering.', query || ownerFilter ? 'No decisions match these filters.' : 'Answers recorded in the inbox will appear here with their context and revision history.')}</div>`;
}

function ownershipGraph() {
  if (ownership === null) loadOwnership();
  const rows = ownership || [];
  const repos = [...new Set(rows.map(r => r.repo))];
  const table = rows.length ? `<div class="table-wrap" role="region" aria-label="Ownership signals" tabindex="0"><table><thead><tr><th>REPO</th><th>PATH</th><th>ENGINEER</th><th>SIGNAL</th><th>WEIGHT</th><th>EVIDENCE</th></tr></thead><tbody>${rows.map(r => `<tr><td>${esc(r.repo)}</td><td><code>${esc(r.path_prefix || '(repo-wide)')}</code></td><td>${esc(r.engineer)}</td><td>${esc(r.source)}</td><td>${Number(r.weight).toFixed(2)}</td><td><small>${esc(r.evidence)}</small></td></tr>`).join('')}</tbody></table></div>` : ownership === null ? '<p class="context">Loading the ownership graph…</p>' : '<p class="context">No repository ingested yet. Ingest a local git checkout to build the map from git history, blame shares, CODEOWNERS, and Reviewed-by trailers.</p>';
  const syncRepos = (state.sync && state.sync.repos) || [];
  const synced = syncRepos.map(s => `<li><strong>${esc(s.repo)}</strong><span>${s.last_error ? esc(s.last_error) : s.last_success_at ? 'Synced ' + ago(s.last_success_at) : 'Sync pending'}</span></li>`).join('');
  return `<div class="wide-toolbar"><h2>Ownership graph</h2><span class="muted">${plural(repos.length, 'repository', 'repositories')} · ${plural(rows.length, 'signal')}</span>${canAdminister() ? '<button class="button" data-action="ingest">Ingest a repository</button>' : ''}</div>${synced ? `<details class="sync-details"><summary>GitHub sync status · ${plural(syncRepos.length, 'repository', 'repositories')}</summary><ul>${synced}</ul></details>` : ''}${table}`;
}

const roleLabels = {knows:'knows', decides:'decides', approves:'must approve'};
const scopeLabels = {path:'path', category:'category', repo:'repository'};

const canAdminister = () => state.auth?.enabled === false || state.me?.role === 'admin';

function verifiedLayer() {
  if (directory === null) {
    loadDirectory();
    return '<p class="context" role="status">Loading people and authority…</p>';
  }
  const dir = directory;
  const settings = dir.settings || {};
  const people = dir.people || [];
  const coordinator = settings.coordinator;
  const control = `<section class="setup-card routing-settings"><div><span class="label">ROUTING SETTINGS</span><h2>Coordinator &amp; pilot mode</h2><p>Optional overrides. Raven normally discovers contacts from connected sources and learns from answers in Raven.</p><label for="coordinator-select">Coordinator</label><select id="coordinator-select" aria-label="Coordinator"><option value="">No coordinator — use the Slack triage channel</option>${people.map(p => `<option value="${esc(p.id)}" ${coordinator && coordinator.id === p.id ? 'selected' : ''}>${esc(p.name)}${p.team ? ` · ${esc(p.team)}` : ''}</option>`).join('')}</select><p class="field-help">Optional fallback before the Slack triage channel.</p></div><div class="routing-options"><label class="routing-option" for="verified-only"><input type="checkbox" id="verified-only" ${settings.require_verified_route ? 'checked' : ''}><span><strong>Pilot mode: route only verified owners</strong><small>Route git-history-only matches to the coordinator, with the candidates named.</small></span></label><label class="routing-option" for="auto-rules"><input type="checkbox" id="auto-rules" ${settings.auto_rules ? 'checked' : ''}><span><strong>Automatic rules</strong><small>Matching reusable rules authorize without a fresh signature. Leave off to review every request.</small></span></label><label class="routing-option" for="brief-mode"><span><strong>Task page in messages</strong><small>Legacy conversation mode only: optional recipient-specific task links. Default notifications use authenticated Raven links.</small><select id="brief-mode" aria-label="Task page in messages">${[['off','Off: messages link to the inbox'],['static','Task page: the task’s context, the answer form and notes']].map(([v,l]) => `<option value="${v}" ${(settings.brief_mode || 'static') === v ? 'selected' : ''}>${l}</option>`).join('')}</select></span></label></div></section>`;
  const cards = people.length ? `<div class="people-grid">${people.map((p, i) => `<article class="person-card ${p.active ? '' : 'inactive'}">${avatar(p.name, i)}<h3>${esc(p.name)}</h3><p>${esc(p.team || '')}${p.teams && p.teams.length ? ` · ${esc(p.teams.join(', '))}` : ''}</p><small>${[p.email, p.github_login ? '@' + p.github_login : '', p.slack_id ? 'Slack ' + p.slack_id : ''].filter(Boolean).map(esc).join(' · ') || 'no identities recorded'}</small><small>${(p.authority || []).length ? (p.authority || []).map(a => `${roleLabels[a.role] || a.role} ${a.scope_kind === 'repo' ? 'everything' : esc(a.scope)}${a.repo ? ` in ${esc(a.repo)}` : ''}`).join('; ') : 'Contact discovered · routing follows available evidence'}</small>${p.active ? '' : '<small>inactive</small>'}</article>`).join('')}</div>` : '<p class="context">Raven discovers contacts from connected sources. Default notification links require the expert to sign in to Raven; discovering a contact does not create their account.</p>';
  const rows = dir.authority || [];
  // A row whose date has passed decides nothing; it read the same as a
  // live one, which is how a map can look set up and route nowhere.
  const expired = a => Boolean(a.effective_to) && a.effective_to.slice(0, 10) < new Date().toISOString().slice(0, 10);
  const inForce = rows.filter(a => !expired(a)).length;
  const table = rows.length ? `<div class="table-wrap" role="region" aria-label="Authority map" tabindex="0"><table><thead><tr><th>WHO</th><th>ROLE</th><th>SCOPE</th><th>REPOSITORY</th><th>SOURCE</th><th>UNTIL</th><th></th></tr></thead><tbody>${rows.map(a => `<tr class="${expired(a) ? 'inactive' : ''}"><td><strong>${esc(a.who)}</strong>${a.is_team ? ' <small>(team)</small>' : ''}</td><td>${esc(roleLabels[a.role] || a.role)}${a.accepted ? '' : ' <small>(not yet accepted)</small>'}</td><td><code>${esc(a.scope_kind === 'repo' ? 'everything' : a.scope)}</code> <small>${esc(scopeLabels[a.scope_kind] || a.scope_kind)}</small></td><td>${esc(a.repo || 'every repository')}</td><td>${esc(a.source)}${a.asserted_by ? `<br><small>asserted by ${esc(a.asserted_by)}</small>` : ''}${a.note ? `<br><small>${esc(a.note)}</small>` : ''}</td><td>${a.effective_to ? `${esc(a.effective_to.slice(0, 10))}${expired(a) ? ' <small>expired: decides nothing now</small>' : ''}` : 'open'}</td><td>${canAdminister() ? `<button class="button small soft" data-action="end-authority" data-id="${esc(a.id)}">End</button>` : ''}</td></tr>`).join('')}</tbody></table></div>` : '<p class="context">No manual overrides. Raven infers contacts from connected sources and learns from replies.</p>';
  return `${canAdminister() ? control : `<fieldset class="read-only-settings" disabled><legend>Workspace settings · managed by an administrator</legend>${control}</fieldset>`}<div class="wide-toolbar"><h2>People</h2><span class="muted">${plural(people.length, 'contact')}</span>${canAdminister() ? '<button class="button" data-action="person">Add person</button>' : ''}${canAdminister() ? '<button class="button soft" data-action="team">Add team</button>' : ''}</div>${cards}<div class="wide-toolbar"><h2>Authority map</h2><span class="muted">${inForce} in force${rows.length > inForce ? `, ${rows.length - inForce} expired` : ''}</span>${canAdminister() ? '<button class="button" data-action="authority">Add authority</button>' : ''}</div>${table}<div class="notice">Verified authority outranks what git suggests: a person the map says decides or approves a path pattern, a decision category (billing, pricing, security, auth, privacy, compat, rollout, data, infra, legal, customer, ux, ops) or a whole repository is the owner whatever blame says. A team named in CODEOWNERS becomes its members once the team is known here.</div>`;
}

function readiness() {
  // What is not set up yet, in the order it bites. A pilot that ingests a
  // repository and stops finds every question landing on the coordinator,
  // and nothing said so.
  const items = state.readiness || [];
  if (!items.length) return '';
  const label = {blocker: 'Set this up first', warning: 'Worth fixing', note: 'Note'};
  return `<div class="readiness">${items.map(r => `<div class="readiness-row ${esc(r.level)}"><strong>${esc(label[r.level] || r.level)}</strong><span>${esc(r.what)}</span><em>${esc(r.do)}</em></div>`).join('')}</div>`;
}

function owners() {
  return `<div class="owners-page">${workspaceAccountCard()}${savedStrip()}${readiness()}${verifiedLayer()}<details class="history" id="repository-signals"><summary>Repository history and routing signals</summary>${ownershipGraph()}</details><div class="wide-toolbar"><h2>Owners by path pattern</h2><span class="muted">${state.owners.length} people</span></div><div class="people-grid">${state.owners.map((owner, i) => `<article class="person-card">${avatar(owner.name, i)}<h3>${esc(owner.name)}</h3><p>${esc(owner.team)}</p><code>${esc(owner.patterns)}</code><small>${state.decisions.filter(d => d.owner_id === owner.id && d.status === 'pending').length} pending · ${plural(state.decisions.filter(d => d.owner_id === owner.id && d.status === 'approved').length, 'answer')} captured</small></article>`).join('')}</div>${state.owners.length ? '<div class="notice">A configured owner is a person with verified authority for their path patterns. The patterns also route by repository-relative file path when no graph exists: simple globs, separated by commas, the last matching owner wins. You can reassign pending requests from the inbox.</div>' : empty('Start with the people who know.', 'Add an owner and the paths they are responsible for.', 'owner', 'Add your first owner')}</div>`;
}

function newPerson() {
  openModal('Add a person.', 'The identities the organization knows them by, so routing, Slack and sign-off agree on who they are.', `<form id="person-form"><label for="person-name">Name</label><input id="person-name" name="name" maxlength="100" placeholder="Marisol Vega" required><div class="form-row"><div><label for="person-email">Email</label><input id="person-email" name="email" maxlength="200" placeholder="marisol@acme.example"></div><div><label for="person-team">Team</label><input id="person-team" name="team" maxlength="100" placeholder="Pricing"></div></div><div class="form-row"><div><label for="person-github">GitHub login</label><input id="person-github" name="github_login" maxlength="100" placeholder="mvega"></div><div><label for="person-slack">Slack member id</label><input id="person-slack" name="slack_id" maxlength="100" placeholder="U0123ABC"></div></div><label for="person-aliases">Other names, emails or handles (comma separated)</label><input id="person-aliases" name="aliases" maxlength="1000" placeholder="M. Vega, marisol@old.example"><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button type="button" class="button" data-action="close">Cancel</button><button class="button primary" type="submit">Add person</button></div></form>`);
}

function newTeam() {
  openModal('Add a team.', 'A team CODEOWNERS names becomes its members once it is known here.', `<form id="team-form"><label for="team-name">Name</label><input id="team-name" name="name" maxlength="100" placeholder="Payments" required><label for="team-handle">Handle as CODEOWNERS writes it</label><input id="team-handle" name="handle" maxlength="100" placeholder="acme/payments-team"><label for="team-members">Members (names, emails or logins, comma separated)</label><input id="team-members" name="members" maxlength="2000" placeholder="Marisol Vega, wes@acme.example"><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button type="button" class="button" data-action="close">Cancel</button><button class="button primary" type="submit">Add team</button></div></form>`);
}

function newAuthority() {
  const people = (directory && directory.people) || [];
  const teams = (directory && directory.teams) || [];
  openModal('Record who decides.', 'Verified authority outranks anything git history suggests.', `<form id="authority-form"><label for="authority-who">Person or team</label><select id="authority-who" name="who" required>${people.map(p => `<option value="person:${esc(p.id)}">${esc(p.name)}</option>`).join('')}${teams.map(t => `<option value="team:${esc(t.id)}">Team · ${esc(t.name)}</option>`).join('')}</select><div class="form-row"><div><label for="authority-role">Role</label><select id="authority-role" name="role"><option value="decides">decides</option><option value="approves">must approve</option><option value="knows">knows the area</option></select></div><div><label for="authority-kind">Scope</label><select id="authority-kind" name="scope_kind"><option value="path">path pattern</option><option value="category">decision category</option><option value="repo">whole repository</option></select></div></div><label for="authority-scope">Path pattern or category</label><input id="authority-scope" name="scope" maxlength="300" placeholder="billing/* or pricing"><div class="form-row"><div><label for="authority-repo">Repository (blank for every repository)</label><input id="authority-repo" name="repo" maxlength="300" placeholder="acme/platform"></div><div><label for="authority-until">Until (optional date)</label><input id="authority-until" name="effective_to" maxlength="40" placeholder="2027-01-01"></div></div><label for="authority-note">Note</label><input id="authority-note" name="note" maxlength="500" placeholder="Owns commercial credit exceptions"><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button type="button" class="button" data-action="close">Cancel</button><button class="button primary" type="submit">Record authority</button></div></form>`);
}

function deliveriesCard() {
  const d = state.delivery || {};
  const github = state.github_app || {};
  const repos = github.repositories || [];
  const canConnect = !!state.github_app && state.me && state.me.role === 'admin';
  const githubOn = !!github.configured && repos.length > 0 && (github.mode !== 'device' || !!github.authorized);
  // A server GITHUB_TOKEN syncs without the GitHub App. The card said Off while that sync worked;
  // it now reads the sync itself: which repositories, when each last synced, and what failed.
  const synced = (state.sync && state.sync.repos) || [];
  const tokenSync = !!(state.sync && state.sync.github) && !githubOn;
  const tokenOk = tokenSync && synced.some(r => r.last_success_at);
  const on = githubOn || tokenOk;
  const failing = synced.filter(r => r.last_error);
  const summary = githubOn ? 'Pull requests and ownership evidence sync into Raven.'
    : tokenOk ? 'Syncing with the server’s GITHUB_TOKEN: pull requests, reviews and ownership evidence come into Raven.'
    : tokenSync ? 'The server has a GITHUB_TOKEN; no repository has synced yet.'
    : 'Choose repositories and authorize Raven through GitHub.';
  const appList = repos.length ? `<details class="github-repositories" id="github-repositories"><summary><strong>${repos.length}</strong> ${githubOn ? 'connected' : 'selected'} ${repos.length === 1 ? 'repository' : 'repositories'}<span>View repositories</span></summary><ul>${repos.map(repo => `<li><span class="repo-owner">${esc(repo.split('/')[0])} /</span> <strong>${esc(repo.split('/').slice(1).join('/') || repo)}</strong></li>`).join('')}</ul></details>` : '';
  const syncList = !repos.length && synced.length ? `<details class="github-repositories" id="github-repositories"><summary><strong>${synced.length}</strong> synced ${synced.length === 1 ? 'repository' : 'repositories'}<span>View repositories</span></summary><ul>${synced.map(r => `<li><span class="repo-owner">${esc(r.repo.split('/')[0])} /</span> <strong>${esc(r.repo.split('/').slice(1).join('/') || r.repo)}</strong> <small>${r.last_error ? `failing: ${esc(r.last_error)}` : r.last_success_at ? `synced ${esc(ago(r.last_success_at))}` : 'not synced yet'}</small></li>`).join('')}</ul></details>` : '';
  const githubCard = `<section class="setup-card github-card"><div class="github-heading"><div><span class="label">SOURCE CONNECTION</span><h2>GitHub</h2></div>${pill(on ? 'On' : tokenSync ? 'Token set' : 'Off', on ? 'green' : 'gray')}</div><p>${summary}</p>${appList || syncList || '<p class="context">No repositories connected yet.</p>'}${failing.length ? `<p class="error">${failing.length} ${failing.length === 1 ? 'repository is' : 'repositories are'} failing to sync: ${esc(failing[0].repo)}: ${esc(failing[0].last_error)}</p>` : ''}${!github.configured && !tokenSync ? '<p>GitHub connection is awaiting setup by the Raven operator.</p>' : ''}${canConnect && github.configured ? `<button class="button ${githubOn ? 'soft' : 'primary'}" data-action="github-connect">${repos.length ? 'Manage repositories' : 'Connect GitHub'}</button>` : ''}</section>`;
  if (!d.enabled) return githubCard + `<div class="setup-card"><h2>Optional notifications</h2><p>The Raven inbox works without Slack. Your coding host can send a short task link through your existing authorized employee Slack MCP, after verifying the recipient and permission. No new Raven bot or copied credentials are needed.</p><p>Experts review context, ask questions, answer and sign in Raven. Default links require a Raven account; contact discovery does not grant access.</p><a href="https://github.com/nintendo-ds75/raven-preview/blob/main/docs/host-notifications.md" target="_blank" rel="noopener">Existing Slack MCP setup</a><details><summary>Optional server bot</summary><p>An operator can configure a bot for notification-only delivery. Workspace app policies may require approval.</p><a href="https://github.com/nintendo-ds75/raven-preview/blob/main/docs/slack.md" target="_blank" rel="noopener">Bot setup</a></details></div>`;
  const failed = (d.failed || 0);
  return githubCard + `<div class="setup-card"><h2>Slack delivery</h2><p>Contacts are discovered automatically. Slack carries a short notification; people review, clarify and answer in Raven.</p><p>${esc(d.directory?.error || (d.directory?.synced_at ? `${d.directory.people} contacts synced` : 'Directory sync starting…'))}</p><button class="button soft" data-action="slack-sync">Refresh Slack contacts</button><p>${d.sent || 0} sent · ${d.queued || 0} queued · ${failed} failed${d.signing_secret ? '' : ' · signed source-change callbacks are not configured'}</p><div class="form-row"><div><label for="fallback-channel">Triage channel (when no contact can be reached)</label><input id="fallback-channel" maxlength="100" placeholder="C0123ABC" value="${esc(d.fallback_channel || '')}"></div><div><button class="button" data-action="fallback">Save</button></div></div>${failed ? `<button class="button soft" data-action="deliveries">Show failed deliveries</button><div id="deliveries"></div>` : ''}</div>`;
}

function agentStatus() {
  const tokens = state.agent_connections || [];
  return tokens.length ? tokens.map(t => `<p>${esc(t.label)} · ${pill(t.last_used_at ? 'Activity detected' : 'Waiting for first use', t.last_used_at ? 'green' : 'gray')}${t.last_used_at ? ` · Last seen ${esc(ago(t.last_used_at))}` : ''}</p>`).join('') : '<p>No active agent credentials yet.</p>';
}

function connections() {
  const shared = !!state.auth?.enabled;
  const step = (n, title) => `<h2><span class="step">${n}</span>${title}</h2>`;
  let agentStep;
  if (shared) {
    agentStep = `<section class="setup-card">${step(1, 'Connect your coding agent')}<p>Pick your client below. Give it a task in your own words: it finds the files and brings the decisions here.</p>${agentCards()}<h3>Your agent connections</h3><div id="agent-status" aria-live="polite">${agentStatus()}</div><p class="context">Refreshes every five seconds while this tab is visible. Last seen confirms the credential was used, not that a session is running.</p><button class="button" data-action="agent-status">Refresh status</button><details class="history" id="manual-agent-setup"><summary>Other MCP clients</summary><p>Create a limited credential and add the endpoint and header in that client’s MCP settings.</p><label for="agent-client">Agent client</label><select id="agent-client"><option>Claude Code</option><option>Cursor</option><option>Codex</option><option>Other MCP client</option></select><button class="button primary" data-action="agent-setup">Create connection details</button><p class="context">The client must support HTTP MCP with an authorization header.</p></details></section>`;
  } else {
    const {remote, notice, ...config} = state.mcp_config || {};
    agentStep = `<section class="setup-card">${step(1, 'Local agent connection')}<p>Authentication is off on this server, so no credential is needed. Add this to your client’s MCP settings, then give it a task in your own words.</p><pre id="mcp-config">${esc(JSON.stringify(config, null, 2))}</pre><button class="button" data-action="copy">Copy configuration</button><p class="context">Then add the Raven rule to the repository’s <code>CLAUDE.md</code> or <code>AGENTS.md</code>, so the agent starts every task in Raven:</p><pre id="agent-rule">${esc(AGENT_RULE)}</pre><button class="button" data-action="copy" data-target="#agent-rule">Copy instruction</button></section>`;
  }
  // A repository ingested at startup (./setup --repo) is step 2 already done.
  // Measured on a fresh install: the card still read "ingest a local clone"
  // under "No repositories connected yet", so the person could not tell.
  const graph = state.graph || {};
  const indexed = [...new Set((graph.repos || []).filter(r => typeof r === 'string' && r))];
  const indexedLine = indexed.length ? `<p class="context"><strong>Already indexed:</strong> ${indexed.map(esc).join(', ')} · ${plural(graph.engineers || 0, 'person', 'people')} from its history${graph.intents ? `, ${plural(graph.intents, 'record')}` : ''}. Raven can name them now.</p>` : '';
  const contextStep = `<section class="setup-card">${step(2, 'Give Raven context')}<p>Repository history tells Raven who owns what; connected sources give it evidence to cite.</p>${indexedLine}<p>Connect GitHub below${indexed.length ? ' to keep it current' : ''}, or ingest a local clone with <code>./setup --repo /path/to/checkout</code>.</p></section>${deliveriesCard()}${contextSourcesCard()}`;
  const peopleStep = shared
    ? `<section class="setup-card">${step(3, 'Invite the people who decide')}<p>Raven routes each decision to the person its evidence names. They answer and sign here; Slack is optional.</p></section>${workspaceAccountCard()}`
    : `<section class="setup-card">${step(3, 'Invite the people who decide')}<p>With authentication off this is a single-operator workspace. Enable authentication before exposing it to a team.</p></section>`;
  return `<div class="setup-grid mcp-setup"><section>${agentStep}${contextStep}</section><section>${peopleStep}${workbenchSetupCard()}</section></div>`;
}

function contextSourcesCard() {
  const report = state.context_sources;
  if (!report) return '';
  const rows = (report.sources || []).map(s => {
    const refused = Object.entries(s.refused || {}).map(([k, n]) => `${esc(k.replace(/_/g, ' '))}: ${n}`).join(', ');
    return `<li><strong>${esc(s.kind)}</strong> ${esc(s.label || s.target)} → ${esc(s.repo)} · <span class="status-${esc(s.state)}">${esc(s.state.replace(/_/g, ' '))}</span> · ${s.records} records${s.last_success_at ? ' · synced ' + esc(new Date(s.last_success_at).toLocaleString()) : ''}${refused ? `<br><small>Not imported — ${refused}</small>` : ''}${s.do ? `<br><small>${esc(s.do)}</small>` : ''}</li>`;
  }).join('');
  const missing = (report.not_configured || []).map(s => `<li><strong>${esc(s.kind)}</strong> not configured${s.credentials_present ? '' : ' (credentials not set)'}<br><code>${esc(s.enable)}</code></li>`).join('');
  return `<section class="setup-card" id="context-sources"><h2>Context sources</h2><p>Slack public channels, Jira projects and documentation sites that an administrator attested are shared with every reader. Records are evidence, never approval.</p>${rows ? `<ul>${rows}</ul>` : '<p>No native context source yet.</p>'}${missing ? `<details class="history"><summary>Available to add</summary><ul>${missing}</ul></details>` : ''}</section>`;
}

function workspaceAccountCard() {
  if (!state.auth?.enabled || state.me?.role !== 'admin') return '';
  return `<section class="setup-card"><h2>${esc(state.workspace?.name || 'Set up your workspace')}</h2>${state.workspace?.needs_setup ? '<p>Create your owner login and name this workspace.</p><a class="button primary" href="/auth/setup">Create workspace</a>' : ''}<p>Invite teammates to review context, ask questions and sign off on decisions in Raven. Default notification links require a Raven account. Invite links are private and expire after 48 hours.</p><button class="button" data-action="invite-person">Invite teammate</button></section>`;
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
  if (typeof renderModelStrip === 'function') renderModelStrip();
  if (!state.auth) {
    $('#new-request').hidden = true;
    $('#manual-actions').hidden = true;
    $('#app').innerHTML = workspaceError ? `<div class="empty" role="alert"><h2>Workspace access unavailable</h2><p>${esc(workspaceError)}</p><button class="button" data-action="retry">Try again</button></div>` : '<p class="context" role="status">Loading workspace…</p>';
    return;
  }
  $('#new-request').hidden = view === 'today' || view === 'connect' || state.me?.role === 'viewer' || (view === 'owners' && !canAdminister());
  $('#manual-actions').hidden = $('#new-request').hidden;
  if (typeof rememberTaskView === 'function') rememberTaskView();
  const noteDraft = $('#note-text')?.value;
  const active = document.activeElement;
  const focusWas = $('#app').contains(active) ? focusKey(active) : '';
  const selection = active.tagName === 'INPUT' ? active.selectionStart : null;
  const disclosures = [...document.querySelectorAll('#app details[id]')].map(el => ({id: el.id, open: el.open, scroll: el.querySelector('ul')?.scrollTop || 0}));
  $('.page-head').hidden = !!taskId() || view === 'work' || view === 'today';
  document.body.classList.toggle('work-view', view === 'work');
  if (view === 'today' && typeof renderObjectives === 'function') { $('#app').dataset.taskId = ''; renderObjectives(); return; }
  if (view === 'work' && typeof renderWorkbench === 'function') { $('#app').dataset.taskId = ''; renderWorkbench(); return; }
  $('#app').innerHTML = taskId() ? taskOverview() : ({inbox, runs, memory, owners, connect: connections})[view]();
  $('#app').dataset.taskId = taskId();
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
  const replacement = keepFocus($('#app'), focusWas, () => $('#app .toolbar .tab.active') || $('#main'));
  if (replacement && selection !== null && ['search','text'].includes(replacement.type)) replacement.setSelectionRange(selection, selection);
}

// Where focus is, as a selector that survives a redraw: an id, or the
// action a control performs on the thing it names. Measured in Chromium:
// a Review button a keyboard user had reached was dropped to <body> by
// the next poll, every five seconds, on every list in the app.
function focusKey(el) {
  if (!el || el === document.body) return '';
  const quote = value => `"${String(value).replace(/["\\]/g, '\\$&')}"`;
  if (el.id) return `[id=${quote(el.id)}]`;
  const data = el.dataset || {};
  if (!data.action) return '';
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
  openModal('Raven help', 'Set up once. Then answer the decisions that need you.',
    '<h3>Set up</h3><p>Open Connections &amp; setup and follow the three steps: connect your coding agent over MCP, give Raven context (GitHub or a local clone), and invite the people who decide. The same page lets you check agent activity. MCP connections do not require the local launcher.</p><h3>Follow a task</h3><p>Tasks lists what each agent registered. Open one for the brief, what the agent learned, who was asked, and the full history.</p><h3>Answer a decision</h3><p>The inbox holds the decisions waiting on a person. Answer, sign, or hand on to someone better placed. A found answer still needs its owner’s signature unless an enabled standing rule covers it.</p><div class="modal-actions"><button class="button primary" data-action="help-connect">Open Connections &amp; setup</button><button class="button" data-action="close">Close</button></div>');
}
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
const requestKey = () => 'ui-' + (globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`);

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
    ${!canAsk ? `<p class="context">${state.auth ? 'You have read-only access to this workspace.' : 'Loading workspace access…'}</p>` : !repos.length && !locked ? '<p class="context">No repository scope is available yet. Connect or ingest a repository in Connections & setup.</p>' : ''}
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
  openModal('Start a task.', 'Describe the work. Raven will bring unresolved decisions to the inbox.', `<form id="task-form" data-submission-key="${esc(crypto.randomUUID())}"><label for="task">Task</label><textarea id="task" name="task" maxlength="12000" placeholder="Add usage-based pricing" required></textarea><label for="repository">Repository</label><select id="repository" name="repository">${(state.execution_config?.repositories || []).map(r => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join('')}</select><p class="context">Runs in a disposable hosted workspace. Changes and test results appear in the run.</p><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button type="button" class="button" data-action="close">Cancel</button><button class="button primary" type="submit">Start task</button></div></form>`);
}

function newIngest() {
  openModal('Ingest a repository.', 'Build the ownership map and record graph from a local git checkout.', `<form id="ingest-form"><label for="ingest-path">Absolute path to the checkout</label><input id="ingest-path" name="path" placeholder="/home/you/src/platform" maxlength="1000" required><div class="form-row"><div><label for="ingest-repo">Repository name (optional)</label><input id="ingest-repo" name="repo" placeholder="platform" maxlength="100"></div><div><label for="ingest-commits">History depth (0 = all)</label><input id="ingest-commits" name="max_commits" placeholder="2000" maxlength="8"></div></div><p class="context">Reads git log, blame shares, CODEOWNERS, and Reviewed-by trailers on this machine. Nothing leaves it. Re-running refreshes the map.</p><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button type="button" class="button" data-action="close">Cancel</button><button class="button primary" type="submit">Ingest</button></div></form>`);
}

function newOwner() {
  openModal('Add a decision owner.', 'Give questions a clear route to the person who decides.', `<form id="owner-form"><label for="name">Name</label><input id="name" name="name" maxlength="100" placeholder="Wes Chen" required><label for="team">Team</label><input id="team" name="team" maxlength="100" placeholder="Platform Data" required><label for="patterns">Path patterns</label><input id="patterns" name="patterns" maxlength="2000" placeholder="billing/*, metering/*" required><p class="context">Comma-separated globs, matched against the request’s file path. Use * for a fallback owner. Later matching owners take precedence.</p><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button type="button" class="button" data-action="close">Cancel</button><button class="button primary" type="submit">Add owner</button></div></form>`);
}

function ruleBox(d) {
  if (d.reusable) {
    return `<div class="context-box"><span class="label">Reusable rule${d.rule_by ? ` · made by ${esc(d.rule_by)}` : ''}</span>Questions memory matches to this answer ${state.settings && state.settings.auto_rules ? 'resolve without a fresh signature' : 'are shown as covered by this rule (automatic rules are off, so a person still signs)'}${d.rule_conditions ? ` when they satisfy <strong>${esc(d.rule_conditions)}</strong>` : ''}${d.rule_scope === 'any' ? ', in any scope' : ', in this scope only'}${d.rule_expires ? `, until ${esc(d.rule_expires.slice(0, 10))}` : ''}. Ending it puts every outstanding node it authorized back in front of a person.<div class="modal-actions"><button class="button small" data-action="end-rule" data-id="${esc(d.id)}" data-updated="${esc(d.updated_at)}">End the rule</button></div></div>`;
  }
  return `<details class="history"><summary>Make this answer a reusable rule</summary><form id="rule-form" data-id="${esc(d.id)}" data-updated="${esc(d.updated_at)}"><label for="rule-conditions">Applies when the question mentions (optional; one phrase per line)</label><textarea id="rule-conditions" name="conditions" maxlength="500" placeholder="enterprise plan"></textarea><label for="rule-expires">Expires (optional, YYYY-MM-DD)</label><input id="rule-expires" name="expires" maxlength="25" placeholder="2027-01-01"><label for="rule-scope"><input type="checkbox" id="rule-scope" name="scope" value="any"> Applies anywhere (other customers, other files); otherwise only in this decision's own scope</label><p class="context">By default every decision is request-specific: a later task that asks the same thing gets this answer as evidence and still needs your signature. A rule skips that signature while its conditions hold, once automatic rules are on under People &amp; ownership. Conditions are checked against what the agent states as facts (<code>plan=enterprise</code>) or phrases the question carries and does not deny; a missing fact means a person decides.</p><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button class="button primary small" type="submit">Make it a rule</button></div></form></details>`;
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
  return `<details class="history"><summary>Where this answer applies (optional)</summary>
    <label for="${prefix}-requires">Required facts</label><input id="${prefix}-requires" name="applies_when" value="${esc(pairs(spec.requires))}" placeholder="plan=enterprise, region=us" maxlength="2000">
    <label for="${prefix}-excludes">Exceptions</label><input id="${prefix}-excludes" name="excludes_when" value="${esc(pairs(spec.excludes))}" placeholder="contract=monthly" maxlength="2000">
    <label for="${prefix}-paths">Repository paths</label><input id="${prefix}-paths" name="applies_to_paths" value="${esc((spec.paths || []).join(', '))}" placeholder="billing/, pricing/rules.py" maxlength="2000">
    <label for="${prefix}-until">Valid until</label><input id="${prefix}-until" name="valid_until" type="date" value="${esc((spec.valid_until || '').slice(0, 10))}">
    <p class="context">A later agent must state every required fact and a matching path before Raven reuses this answer. Missing information goes back to a person. This does not make the answer an automatic rule.</p></details>`;
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
  return `<div class="context-box"><span class="label">Follow-up questions for the agent · the right questions for the next level</span><textarea id="followups" aria-label="Follow-up questions" placeholder="One question per line. They appear on the agent's tree under this decision." maxlength="4000"></textarea><label for="followups-required"><input type="checkbox" id="followups-required"> Required: the task cannot finish until the agent takes these up and they are answered</label><button class="button small soft" data-action="followups" data-id="${esc(d.id)}">Add follow-up questions</button></div>`;
}

function reframeControls(d) {
  if (!(d.allowed_actions || []).includes('correct') || ['withdrawn','duplicate','adopted','suggested'].includes(d.status)) return '';
  return `<details class="history"><summary>Is this the wrong question?</summary>
    <p class="context">Replace the question when its premise is mistaken. This clears its old answer and signatures, reroutes it, and marks dependent work for review. Its history is retained.</p>
    <label for="reframe-question">Corrected question</label><textarea id="reframe-question" maxlength="2000">${esc(d.question)}</textarea>
    <label for="reframe-reason">Why does the question need to change?</label><textarea id="reframe-reason" maxlength="4000"></textarea>
    <button class="button small" data-action="reframe" data-id="${esc(d.id)}" data-updated="${esc(d.updated_at)}">Replace question and clear old signatures</button></details>`;
}

// Closed by default: the person opened the decision to answer it, and the
// answer form is what should be on screen; the question box is one line
// away for the times the context is not enough.
function decisionInquiry(d) {
  return `<details class="decision-inquiry" id="decision-inquiry-${esc(d.id)}"><summary>Ask Raven before deciding</summary><form data-decision-inquiry="${esc(d.id)}" data-task="${esc(d.run_id)}"><label for="decision-inquiry-text">Question about the context, alternatives or consequences</label><textarea id="decision-inquiry-text" name="question" maxlength="2000" required placeholder="What would change if we choose the other option?"></textarea><p class="context">Read-only clarification. Record your answer or sign off separately below.</p><button class="button small" type="submit">Ask Raven</button><div class="decision-inquiry-result" role="status"></div></form></details>`;
}

// Show the current record before the request that originally raised it.
function decisionCurrentSummary(d) {
  const stale = d.needs_review || d.source_refresh_required;
  const title = stale ? 'Recorded answer · review required' : d.signoff === 'rule' ? 'Answer covered by standing rule' : signedNow(d) ? 'Current signed answer' : 'Current recorded answer';
  return `<section class="decision-current" aria-label="Current decision"><div class="metadata">${stale ? pill('Approval needs review') : pill(statusLabel(d))}${stale ? '' : signoffLabel(d)}</div><h3>${d.answer ? title : 'No answer recorded yet'}</h3>${d.answer ? `<p class="decision-current-answer">${esc(d.answer)}</p>` : ''}<p class="context">${d.signed_by ? `Recorded signer: ${esc(d.signed_by)}. ` : ''}${d.answered_by ? `Answer recorded by: ${esc(d.answered_by)}. ` : ''}Owner: ${esc(d.owner_name || 'Unassigned')}.</p>${stale ? `<p class="task-warning">${esc(d.review_reason || d.source_notice || 'Review current evidence before using this answer. Earlier sign-off does not authorize it while review is required.')}</p>` : ''}${d.rationale ? `<p class="decision-current-reason"><strong>Why:</strong> ${esc(d.rationale)}</p>` : ''}</section>`;
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
  return `<section class="decision-current-scope" aria-label="Current recorded scope"><h3>Current recorded scope</h3><dl class="scope-fields">${fields.map(([key,value]) => `<dt>${esc(labels[key] || key)}</dt><dd data-scope-field="${esc(key)}">${scopeValue(value,key)}</dd>`).join('')}</dl><details class="history decision-original-scope"><summary>Original request & complete recorded scope</summary><p class="context">The request and its background may predate the current answer above. They do not replace its recorded decision or approval status.</p>${approvalScope(d)}</details></section>`;
}

function decisionBackground(d) {
  const scope = typeof objectiveScope === 'function' ? objectiveScope(d) : null;
  return `<details class="history decision-background" ${!d.answer && !d.prediction ? 'open' : ''}><summary>Recorded background & evidence</summary><p class="context">Context captured when this question was raised may describe an earlier unresolved state. Read it alongside the current answer and scope above.</p>${d.brief ? `<p><strong>Original brief:</strong> ${esc(d.brief)}</p>` : ''}${scope ? objectiveScopeCard(scope, `background-${d.id}`) : `<p class="modal-context">${esc(d.context || '')}</p>`}${d.evidence ? `<p class="context"><strong>Recorded evidence:</strong> ${esc(d.evidence)}</p>` : ''}${workItemContext(d)}${sourceEvidence(d.sources, d.needs_review ? null : d.source_revalidation)}</details>`;
}

// Why the signed-in person may only read this decision, and what would let
// them act: the authority map (a person it names for the decision's scope
// may answer and sign). Measured in a shared workspace: a member whose
// name matched the owner's read "Alex Morgan owns this decision" under
// their own name, with no way on.
// An invitation links to a person on the map by exact email on its own. A
// person seeded without an email (an owner from CODEOWNERS or git history)
// is linked only when the admin names them here, never by a matching name.
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
  const who = esc(d.owner_name);
  const admin = state.me?.role === 'admin';
  const actions = !withActions ? '' : admin
    ? `<div class="modal-actions"><button class="button small" data-action="invite-owner" data-person="${esc(account.person_id || '')}" data-email="${esc(account.email || '')}">${account.state === 'invited' ? 'Invite again' : `Invite ${who}`}</button></div><div class="modal-owner">${avatar(d.owner_name)}<select id="assign-owner" aria-label="Decision owner">${ownerOptions(d.owner_id)}</select><button class="button small soft" data-action="refer" data-override="1" data-id="${esc(d.id)}" data-updated="${esc(d.updated_at)}">Hand on &amp; learn</button></div><p class="context">Handing on is recorded as your admin override: it moves this request to the person you pick and teaches Raven that they decide, once they answer. It signs nothing.</p>`
    : '<p class="context">An administrator can invite them or hand this on.</p>';
  return `<div class="context-box prediction"><span class="label">Nobody can sign this yet</span>${who} ${account.state === 'invited' ? 'has been invited but has not joined yet' : 'has no account yet'}, so this decision waits until they join.${actions}</div>`;
}

function readOnlyReason(d) {
  const owner = d.owner_name ? `${esc(d.owner_name)} owns this decision` : 'This decision has no owner yet';
  if (state.me?.role === 'viewer') return `Read-only access. ${owner}.`;
  const how = state.me?.role === 'admin' ? 'record your authority' : 'ask an administrator to record your authority';
  return `${owner}; you can add follow-up questions here and context on the task. To answer or sign it yourself, ${how} for its scope under People &amp; ownership.`;
}

async function review(id) {
  const requestVersion = ++modalVersion;
  try {
    const d = await api(`/api/decisions/${encodeURIComponent(id)}`);
    if (requestVersion !== modalVersion) return;
    const canDecide = (d.allowed_actions || []).includes('answer');
    const canRoute = (d.allowed_actions || []).includes('assign');
    if (!canDecide && !canRoute) {
      openModal(d.question, `${d.agent} · ${d.run_title}`, `${decisionCurrentSummary(d)}${decisionReviewScope(d)}${d.consequences ? `<p class="context"><strong>Consequences:</strong> ${esc(typeof d.consequences === 'string' ? d.consequences : JSON.stringify(d.consequences))}</p>` : ''}${decisionInquiry(d)}${d.reusable ? '<p class="context">Recording a new or corrected answer retires the existing standing rule. A sign-off with unchanged answer and sources keeps it. Future automatic reuse needs a fresh explicit make-rule action.</p>' : ''}${ownerAccountNotice(d, true)}<p class="context">${readOnlyReason(d)}</p>${decisionBackground(d)}${d.routing_reason ? `<p class="context"><strong>Routing:</strong> ${esc(d.routing_reason)}</p>` : ''}${d.source_id ? `<button class="button small" data-action="source" data-id="${esc(d.source_id)}">Inspect source decision</button>` : ''}${followupControls(d)}<details class="history"><summary>Decision history · ${plural(d.events.length, 'event')}</summary>${d.events.map(e => `<div class="history-item"><strong>${esc(eventLabels[e.kind] || e.kind)}</strong>${esc(e.detail)}<br><small>${esc(new Date(e.created_at).toLocaleString())}</small></div>`).join('')}</details>`);
      return;
    }
    const approved = d.status === 'approved';
    const settled = settledNow(d);
    // Signed or covered by a rule, but not a recorded answer: it began as evidence, a prediction or an
    // assumed default, a person stands behind it now, and what is left is to correct it or make it a rule.
    const signed = signedNow(d) && !approved;
    // Three shapes: a node waiting for sign-off gets one block (sign off or correct); a follow-up,
    // adopted or duplicate node takes no owner and no answer; everything else takes the answer form.
    const signoffWanted = canDecide && (d.signoff === 'required' || d.needs_review) && !approved && !['pending','suggested','adopted','duplicate'].includes(d.status);
    const inert = ['suggested','adopted','duplicate'].includes(d.status);
    // A node nobody was routed to is the one that most needs placing, so
    // a node waiting for sign-off is assignable too, not only an open question.
    const assignable = canRoute && (d.status === 'pending' || (d.signoff === 'required' && !inert));
    // Signed in, the answer and the signature are the signed-in person's;
    // only the local workspace records on behalf of the named owner.
    const me = state.auth?.enabled ? state.me?.name || '' : '';
    // Locally, a decision one approver signed is signed next as the one
    // it still waits on, not again as its owner.
    const signer = me || (halfSigned(d) && stillToSign(d)[0]) || d.owner_name || 'the local operator';
    const suggestion = !approved && !signoffWanted && !signed ? (d.prediction || ((d.status === 'assumed' || d.status === 'proposed' || d.status === 'resolved' || d.status === 'partial') ? d.answer : '')) : '';
    const canonical = d.canonical ? `<div class="context-box"><span class="label">Same decision as node ${esc(d.canonical.id)} · ${esc(statusLabels[d.canonical.status] || d.canonical.status)}${d.canonical.answered_by ? ` · ${esc(d.canonical.answered_by)}` : ''}</span>${esc(d.canonical.answer || 'Not answered yet.')}<br><button class="button text small" data-action="source" data-id="${esc(d.canonical.id)}">Open the canonical decision ↗</button></div>` : '';
    // Two sources that disagree, and the records the question names with their status, stated up front rather
    // than left in the evidence. Measured live on 63eb671: the brief said no policy was given beside a conflict.
    const conflicts = (d.evidence || '').split(/;\s+(?=[a-z]+:\s)/).filter(p => p.startsWith('conflict:')).map(p => p.slice(9).trim());
    const upfront = `${conflicts.length && !settled ? `<div class="context-box prediction"><span class="label">Conflict · a person decides which stands</span>${esc(conflicts.join(' '))}</div>` : ''}${d.records_named ? `<p class="context"><strong>Records it names:</strong> ${esc(d.records_named)}.</p>` : ''}`;
    openModal(d.question, `${d.agent} · ${d.run_title}`, `${decisionCurrentSummary(d)}${decisionReviewScope(d)}${d.consequences ? `<p class="context"><strong>Consequences:</strong> ${esc(typeof d.consequences === 'string' ? d.consequences : JSON.stringify(d.consequences))}</p>` : ''}${decisionInquiry(d)}${d.reusable ? '<p class="context">Recording a new or corrected answer retires the existing standing rule. A sign-off with unchanged answer and sources keeps it. Future automatic reuse needs a fresh explicit make-rule action.</p>' : ''}${upfront}${d.source_notice ? `<p class="context">${esc(d.source_notice)}</p>` : ''}${settled ? decisionBackground(d) : `${workItemContext(d)}${sourceEvidence(d.sources, d.needs_review ? null : d.source_revalidation)}${decisionBackground({...d, sources:[], evidence:''})}`}${canonical}
      ${canDecide && !inert && state.auth?.enabled && state.me?.id && ['session','token'].includes(state.me.kind) ? `<div class="context-box"><strong>Prefer to talk it through?</strong><p class="context">Use an attributed interview, with optional browser dictation. Review and confirm the exact decision before the agent can use it.</p><button class="button small" data-action="interview-start" data-task="${esc(d.run_id)}" data-id="${esc(d.id)}">Start or resume interview</button></div>` : ''}
      ${d.needs_review ? `<div class="context-box prediction"><span class="label">Needs review</span>${esc(d.review_reason || 'An answer this decision leaned on was corrected.')} Confirm the answer below by signing it, or correct it.</div>` : ''}

      ${(d.allowed_actions || []).includes('rule') && (approved || (signed && d.signoff === 'signed')) && !inert ? ruleBox(d) : ''}
      ${d.evidence && !signoffWanted && !settled ? `<p class="context"><strong>Evidence:</strong> ${esc(d.evidence)}</p>` : ''}${d.owner_evidence ? `<details class="history"><summary>Why this owner</summary><p class="context">${esc(d.owner_evidence)}</p></details>` : ''}
      ${suggestion ? `<div class="context-box prediction"><span class="label">${d.status === 'resolved' || d.status === 'partial' ? resolvedLabel(d) : d.status === 'assumed' ? 'Default assumed · not approved' : d.status === 'proposed' ? 'Prediction, unconfirmed · not a decision' : predictionLabel(d)}</span>${esc(suggestion)}${predictionScope(d) ? `<p class="context"><strong>Scope:</strong> ${esc(predictionScope(d))}</p>` : '<br>'}<button class="button text small" data-action="use-suggestion" data-text="${esc(suggestion)}">Use this text as the answer</button>${d.source_id ? `<button class="button text small" data-action="source" data-id="${esc(d.source_id)}">Inspect source decision ↗</button>` : ''}</div>` : ''}
      ${signoffWanted ? `<div class="context-box prediction"><span class="label">${resolvedLabel(d)}${halfSigned(d) ? ' · every required approver signs' : ' · evidence, not sign-off · your signature is wanted'}</span>${esc(d.answer || d.prediction || '')}${d.evidence ? `<p class="context"><strong>Evidence:</strong> ${esc(d.evidence)}</p>` : ''}<p class="context">The agent prepares on this answer and cannot finish its task until you sign it or correct it.</p>${d.source_id ? `<button class="button text small" data-action="source" data-id="${esc(d.source_id)}">Inspect source decision ↗</button>` : ''}${sourceRevalidationFields(d, 'signoff')}<div class="modal-actions"><button class="button primary small" data-action="signoff" data-id="${esc(d.id)}" data-updated="${esc(d.updated_at)}">Sign off as ${esc(signer)} ${icon('check')}</button></div></div>
      <form id="correct-form" data-id="${esc(d.id)}"><input type="hidden" name="expected_updated_at" value="${esc(d.updated_at)}"><label for="correction">Or correct it</label><textarea id="correction" name="answer" placeholder="The answer the agent should act on instead." maxlength="12000" required></textarea><label for="correction-rationale">Why this correction?</label><textarea id="correction-rationale" name="rationale" placeholder="Capture the reasoning for future tasks." maxlength="12000" required></textarea>${applicabilityFields(d, 'correction')}${sourceRevalidationFields(d, 'correction')}${sourceReplacementField(d)}<p class="context">A correction is a signed answer ${me ? `recorded as ${esc(me)}` : `recorded on behalf of ${esc(signer)}`}; the agent reads it on the tree, and every decision that leaned on the old answer is marked for review.</p><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button type="button" class="button" data-action="close">Close</button><button class="button primary" type="submit">Record correction ${icon('check')}</button></div></form>` : ''}
      ${inert || settled ? '' : ownerAccountNotice(d, false)}
      ${inert ? '' : `<div class="modal-owner">${avatar(d.owner_name)}<select id="assign-owner" aria-label="Decision owner" ${assignable ? '' : 'disabled'}>${ownerOptions(d.owner_id)}</select>${assignable ? `<button class="button small" data-action="assign" data-id="${esc(d.id)}">Assign this one</button><button class="button small soft" data-action="refer" data-id="${esc(d.id)}" data-updated="${esc(d.updated_at)}">Hand on &amp; learn</button>` : ''}</div>${assignable && d.handon ? `<label class="refer-scope" for="refer-scope">Hand on teaches Raven that they decide <select id="refer-scope" aria-label="What handing on teaches">${d.handon.options.map(o => `<option value="${esc(o.scope_kind)}:${esc(o.scope)}" ${o.scope_kind === d.handon.default.scope_kind && o.scope === d.handon.default.scope ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}</select></label>${d.handon.why_none ? `<p class="field-help">${esc(d.handon.why_none)}; pick a scope to teach one.</p>` : ''}` : ''}<p class="context">${esc(d.routing_reason)}${d.routing_reason && !/[.!?]$/.test(d.routing_reason) ? '.' : ''}${assignable ? ' Assign moves this request only; Hand on also teaches Raven the scope above, once the person answers.' : ''}</p>`}
      ${signed && canDecide ? `<form id="correct-form" data-id="${esc(d.id)}"><input type="hidden" name="expected_updated_at" value="${esc(d.updated_at)}"><label for="correction">Signed answer · edit to make a correction</label><textarea id="correction" name="answer" maxlength="12000" required>${esc(d.answer || '')}</textarea><label for="correction-rationale">Why this correction?</label><textarea id="correction-rationale" name="rationale" placeholder="Capture the reasoning for future tasks." maxlength="12000" required></textarea>${applicabilityFields(d, 'correction')}${sourceRevalidationFields(d, 'correction')}${sourceReplacementField(d)}<p class="context">A correction is a signed answer ${me ? `recorded as ${esc(me)}` : `recorded on behalf of ${esc(signer)}`}; the agent reads it on the tree, and every decision that leaned on the old answer is marked for review.</p><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button type="button" class="button" data-action="close">Close</button><button class="button primary" type="submit">Record correction ${icon('check')}</button></div></form>` : ''}
      ${!canDecide || inert || signoffWanted || signed ? '' : `<form id="answer-form" data-id="${esc(d.id)}"><input type="hidden" name="expected_updated_at" value="${esc(d.updated_at)}"><label for="answer">${approved ? 'Recorded answer · edit to make a correction' : 'Your answer'}</label><textarea id="answer" name="answer" placeholder="Give the agent a clear decision and any conditions." maxlength="12000" required>${esc(approved ? d.answer : '')}</textarea><label for="rationale">Why this decision?</label><textarea id="rationale" name="rationale" placeholder="Capture the reasoning for future tasks." maxlength="12000" required>${esc(d.rationale || '')}</textarea>${applicabilityFields(d, 'answer')}${sourceRevalidationFields(d, 'answer')}${sourceReplacementField(d)}<label for="supersedes">Supersedes decision (optional id)</label><input id="supersedes" name="supersedes" placeholder="Decision id this answer replaces" maxlength="100" value="${esc(d.supersedes || '')}"><p class="context">${me ? `Recorded as ${esc(me)}${d.owner_name && d.owner_name !== me ? `, on your own standing; ${esc(d.owner_name)} is its assigned owner` : ''}.` : `Recorded by the local operator on behalf of ${esc(d.owner_name || 'the assigned owner')}.`} ${approved ? 'The previous answer stays in the revision history.' : (state.executions || []).some(e => e.run_id === d.run_id) ? 'Saving queues delivery to the waiting agent. Follow delivery in Tasks.' : 'The agent can retrieve your answer after it is saved.'}</p><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button type="button" class="button" data-action="close">Close</button><button class="button primary" type="submit" ${d.owner_id ? '' : 'disabled'}>${approved ? 'Save correction' : 'Record decision'} ${icon('check')}</button></div></form>`}
      ${reframeControls(d)}
      ${followupControls(d)}
      <details class="history"><summary>Decision history · ${plural(d.events.length, 'event')}</summary>${d.events.map(e => `<div class="history-item"><strong>${esc(eventLabels[e.kind] || e.kind)}</strong>${esc(e.detail)}<br><small>${esc(new Date(e.created_at).toLocaleString())}</small></div>`).join('')}</details>`);
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
  const target = event.target.closest('[data-action]');
  if (!target || target.disabled) return;
  const action = target.dataset.action;
  // The dialog as it was when the action began: a response that lands
  // after the person closed it redraws nothing.
  const version = modalVersion;
  const done = busyLabels[action] ? busy(target, busyLabels[action]) : () => {};
  try {
    if (action === 'close') closeModal();
    if (action === 'new' || action === 'request-status') newRequest();
    if (action === 'request-new') { requestDraft = null; newRequest(); }
    if (action === 'request-view-run') closeModal();
    if (action === 'owner') newOwner();
    if (action === 'person') newPerson();
    if (action === 'team') newTeam();
    if (action === 'authority') newAuthority();
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
    if (action === 'review' || action === 'source') await review(target.dataset.id);
    if (action === 'run') location.hash = ((state.browser_tasks || []).some(t => t.task_id === target.dataset.id) ? 'work/' : 'runs/') + encodeURIComponent(target.dataset.id);
    if (action === 'task-tab') { taskTab = target.dataset.tab; taskView().tab = taskTab; render(); }
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
  if (['inbox-search','memory-search'].includes(event.target.id)) { query = event.target.value; render(); }
});
document.addEventListener('change', async event => {
  if (event.target.id === 'owner-filter') { ownerFilter = event.target.value; render(); }
  try {
    if (event.target.id === 'coordinator-select') {
      await api('/api/settings', {coordinator: event.target.value});
      directory = null; render(); notify(event.target.value ? 'Coordinator set. Unrouted questions go to them.' : 'Coordinator cleared.');
    }
    if (event.target.id === 'verified-only') {
      await api('/api/settings', {require_verified_route: event.target.checked});
      directory = null; render(); notify(event.target.checked ? 'Pilot mode on: only verified owners are routed to.' : 'Pilot mode off: git history routes again.');
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
  if (!['invite-form','request-form','owner-form','answer-form','correct-form','task-form','ingest-form','person-form','team-form','authority-form','rule-form','note-form','task-inquiry-form'].includes(form.id)) return;
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
    } else if (form.id === 'owner-form') {
      await api('/api/owners', data); notify('Owner added to your workspace.'); directory = null;
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
$('#new-request').addEventListener('click', () => view === 'owners' ? newOwner() : newRequest());
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

function workbenchSetupCard() {
  const config = state.workbench || {};
  return `<section class="setup-card"><h2>Work in this browser</h2><p>${config.ready ? 'Raven runs tasks here. No separate coding agent is needed.' : 'An operator connects a repository and configures the server’s inference key once. Then anyone with member access can start here.'}</p>${config.ready ? `<a class="button" href="#work">New task</a><p>${config.tests_enabled ? 'Isolated test commands are enabled.' : 'Test execution is not configured. Raven can read and draft changes; testing requires a sandbox.'}</p>` : '<pre>python -m bridge serve --workbench \\\n  --managed-repo your-org/project=/path/to/checkout</pre><p>Set ANTHROPIC_API_KEY and BRIDGE_MODEL_API=anthropic in the server environment. Keep credentials out of task messages.</p>'}<a href="https://github.com/nintendo-ds75/raven-preview/blob/main/docs/browser-tasks.md" target="_blank" rel="noopener">Browser setup and isolated tests ↗</a></section>`;
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
