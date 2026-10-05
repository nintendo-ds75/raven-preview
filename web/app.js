'use strict';

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
const RUN_STATUS = {working: 'Agent working', needs_judgment: 'Waiting for decisions', pending: 'Starting',
  completed: 'Agent reported complete', abandoned: 'Closed by the agent', result_ready: 'Result ready for inspection',
  failed: 'Agent failed', cancelled: 'Cancelled', review_required: 'Review needed', delivery_pending: 'Delivering answers',
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
  inbox: ['Judgment inbox', '', 'Inbox', 'Questions from your coding agents. Confirm an answer or make the decision.'],
  runs: ['Tasks', '', 'Tasks', 'Started in your coding agent. Follow decisions and progress here.'],
  memory: ['Decision memory', '', 'Memory', 'Past answers and source evidence, with their owner, scope, and approval status.'],
  owners: ['People & ownership', '', 'People', 'Questions go to whoever this map says decides; where it is silent, to repository history and the coordinator. Readiness says what is missing.'],
  connect: ['Connections & setup', '', 'Connect your agent', 'Add Raven over MCP, then give your agent a task in your own words.'],
};
let state = {decisions: [], runs: [], owners: [], events: []};
// The ownership graph and the directory (people, teams, the authority map, the coordinator) are not in the
// polled state: the owners view fetches them when it renders.
let ownership = null, ownershipLoading = false;
let directory = null, directoryLoading = false;
let view = 'inbox', tab = 'pending', tabChosen = false, query = '', ownerFilter = '', fetching = false;
let toastTimer;
let modalVersion = 0;
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
  return `<p class="context">Connecting Raven over MCP does not require the local launcher. Only the Start and Resume terminal buttons do.</p><div class="agent-cards">${[['claude', 'Claude Code'], ['codex', 'Codex'], ['cursor', 'Cursor']].map(([id, name]) => `<div class="agent-card"><span class="agent-logo ${id}"><img src="/${id === 'codex' ? 'openai' : id}.svg" alt="${id === 'codex' ? 'OpenAI' : name} logo" width="28" height="28"></span><h3>${name}</h3><button class="button primary" data-action="quick-agent" data-client="${id}">${id === 'cursor' ? 'Add to Cursor' : 'Connect existing'}</button>${id !== 'cursor' ? `<button class="button" data-action="launch-agent" data-client="${id}" data-mode="new">Start new session</button><button class="button" data-action="launch-agent" data-client="${id}" data-mode="resume">Resume a session</button>` : '<p>Opens Cursor’s installation confirmation.</p>'}</div>`).join('')}</div><details class="history" id="local-launcher-help"><summary>Local launcher · enable terminal buttons</summary><p>One-time per work session: run this from your Raven checkout, selecting the project folder agents should start in. It opens a paired Raven tab. Keep it running; closing it disables launches. Requires Python 3, installed agent CLIs, and macOS or Linux.</p><pre>./agents /path/to/your/project</pre><p>A browser cannot inspect local processes or start a CLI by itself. The paired helper is the permission boundary for terminal launches. Resume opens the agent’s own session picker, not an attachment to an arbitrary running terminal. Agent approval prompts remain enabled.</p></details>`;
}

function updateAgentConnectPrompt() {
  const prompt = $('#agent-connect-prompt');
  if (!prompt) return;
  prompt.hidden = (state.agent_connections || []).length > 0 || (state.runs || []).length > 0;
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
    content = command ? `<p>Run once in your project terminal, then restart or reconnect MCP in Claude Code.</p><pre id="mcp-config">${esc(command)}</pre>` : `<p>Add this entry in your Codex MCP configuration. Preserve other existing settings.</p><pre id="mcp-config">${esc('[mcp_servers.bridge]\nurl = ' + JSON.stringify(entry.url) + '\nhttp_headers = { Authorization = ' + JSON.stringify(entry.headers.Authorization) + ' }')}</pre>`;
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

async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-Bridge-CSRF': state.csrf_token}, body: JSON.stringify(data),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'Request failed. Please try again.');
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
      $('#modal').close(); await refresh(); notify('GitHub connected. Repository sync has started.');
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
    state = await api('/api/state');
    updateWorkspaceName();
    updateAgentConnectPrompt();
    if ($('#agent-status')) $('#agent-status').innerHTML = agentStatus();
    $('#connection').classList.remove('offline');
    $('#connection').innerHTML = state.auth && state.auth.enabled ? '<i></i> Shared workspace' : '<i></i> Local workspace';
    if (state.me) {
      $('#profile-name').textContent = state.me.name;
      $('#profile-avatar').textContent = initials(state.me.name);
      $('#profile-note').innerHTML = state.auth && state.auth.enabled ? `${esc(state.me.role)} · <a href="/auth/logout">Sign out</a>` : 'Self-hosted workspace';
    }
    if (personalInbox() && !tabChosen) tab = 'mine';
    $('#inbox-count').textContent = personalInbox() ? state.decisions.filter(assignedToMe).length
      : state.counts ? state.counts.needs_you : state.decisions.filter(needsYou).length;
    if (taskId()) await loadTask({quiet});
    if (!taskId() && (!quiet || (!$('#modal').open && !['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)))) render();
  } catch (error) {
    $('#connection').classList.add('offline');
    $('#connection').innerHTML = '<i></i> Connection lost';
    if (!quiet) $('#app').innerHTML = `<div class="empty"><h3>We couldn’t reach your workspace.</h3><p>${esc(error.message)}</p><button class="button" data-action="retry">Try again</button></div>`;
  } finally { fetching = false; }
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
  const next = location.hash.slice(1).split('/')[0];
  view = titles[next] ? next : 'inbox';
  query = ''; ownerFilter = ''; ownership = null; directory = null;
  const [breadcrumb, eyebrow, title, description] = titles[view];
  $('#breadcrumb').textContent = breadcrumb;
  $('#eyebrow').textContent = eyebrow;
  $('#page-title').textContent = title;
  $('#page-description').textContent = description;
  document.title = `Raven · ${breadcrumb}`;
  $('#new-request').hidden = view === 'connect';
  $('#new-request').innerHTML = `${icon('plus')}${view === 'owners' ? 'Add owner' : 'New request'}`;
  document.querySelectorAll('nav a').forEach(link => {
    link.classList.toggle('active', link.dataset.view === view);
    if (link.dataset.view === view) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  taskTab = 'overview';
  taskDetail = null; taskError = '';
  if (taskId()) loadTask();
  render();
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
    <h3>${esc(d.question)}</h3><p class="context">${esc(d.context)}</p>
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
const assignedToMe = d => needsYou(d) && (d.owner_name === state.me?.name || stillToSign(d).includes(state.me?.name));
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
    <div class="decision-list">${rows.length ? rows.map(decisionCard).join('') : empty(query ? 'No matching decisions.' : tab === 'predicted' ? 'No suggestions awaiting review.' : 'You’re all caught up.', query ? 'Try a different search.' : 'New questions and sign-offs appear when an agent needs your judgment.', state.decisions.length ? '' : 'help-connect', 'Connect an agent')}</div>
    <div class="section-footnote">${icon('shield')}Every answer keeps its owner, context, and history.</div></section>
    </div>`;
}

function runs() {
  return `<div class="wide-toolbar"><h2>Tasks in this workspace</h2>${state.execution_config?.enabled ? '<button class="button primary" data-action="task">Start a task</button>' : ''}<span class="muted">${plural(state.runs.length, 'task')}</span></div>${state.runs.length ? `<div class="table-wrap"><table><thead><tr><th>TASK / REPOSITORY</th><th>AGENT</th><th>STATUS</th><th>DECISIONS</th><th>UPDATED</th><th></th></tr></thead><tbody>${state.runs.map(run => {
    const decisions = state.decisions.filter(d => d.run_id === run.id);
    const pending = decisions.filter(needsYou);
    return `<tr><td><strong>${esc(run.title)}</strong><small>${esc(run.repo)}</small></td><td>${esc(run.agent)}</td><td>${pill(pending.length ? 'Waiting for decisions' : runStatusLabel(run.status), pending.length ? '' : 'gray')}</td><td>${decisions.length} total${pending.length ? ` · ${pending.length} need a person` : ''}</td><td>${ago(run.updated_at)}</td><td><button class="button small" data-action="run" data-id="${esc(run.id)}">View run</button></td></tr>`;
  }).join('')}</tbody></table></div>` : empty('No tasks yet.', 'Connect Raven over MCP, then ask your coding agent to work on a task. It discovers the files and decisions.', 'help-connect', 'Connect an agent')}`;
}

function memory() {
  const decisions = state.decisions.filter(d => settledStatuses.includes(d.status) && !d.superseded_by && (!ownerFilter || d.owner_id === ownerFilter) && `${d.question} ${d.answer} ${d.rationale} ${d.owner_name} ${d.evidence}`.toLowerCase().includes(query.toLowerCase()));
  return `<p class="notice">A past answer is evidence for the next task, not permission to use it. Fresh owner signoff is required unless an enabled standing rule covers the new case.</p><div class="filter-row"><input id="memory-search" type="search" placeholder="Search questions, answers, or rationale…" aria-label="Search decision memory" value="${esc(query)}"><select id="owner-filter" aria-label="Filter by owner"><option value="">All owners</option>${state.owners.map(o => `<option value="${esc(o.id)}" ${ownerFilter === o.id ? 'selected' : ''}>${esc(o.name)}</option>`).join('')}</select><button class="button" data-action="export">Export history ↗</button></div><div class="memory-list">${decisions.length ? decisions.map(d => `<article class="decision-card"><div class="card-top">${pill(statusLabel(d),signedNow(d) ? 'green' : '')}${signoffLabel(d)}${kindBadge(d)}<span>${esc(d.answered_by || d.owner_name || '')}</span><time>${ago(d.updated_at)}</time></div><h3>${esc(d.question)}</h3><p class="result-answer">${esc(d.answer)}</p><p class="context">${esc(d.rationale || d.evidence || '')}</p><button class="button small" data-action="review" data-id="${esc(d.id)}">Context & history ${icon('arrow')}</button></article>`).join('') : empty('A place for decisions worth remembering.', query || ownerFilter ? 'No decisions match these filters.' : 'Answers recorded in the inbox will appear here with their context and revision history.')}</div>`;
}

function ownershipGraph() {
  if (ownership === null) loadOwnership();
  const rows = ownership || [];
  const repos = [...new Set(rows.map(r => r.repo))];
  const table = rows.length ? `<div class="table-wrap"><table><thead><tr><th>REPO</th><th>PATH</th><th>ENGINEER</th><th>SIGNAL</th><th>WEIGHT</th><th>EVIDENCE</th></tr></thead><tbody>${rows.map(r => `<tr><td>${esc(r.repo)}</td><td><code>${esc(r.path_prefix || '(repo-wide)')}</code></td><td>${esc(r.engineer)}</td><td>${esc(r.source)}</td><td>${Number(r.weight).toFixed(2)}</td><td><small>${esc(r.evidence)}</small></td></tr>`).join('')}</tbody></table></div>` : ownership === null ? '<p class="context">Loading the ownership graph…</p>' : '<p class="context">No repository ingested yet. Ingest a local git checkout to build the map from git history, blame shares, CODEOWNERS, and Reviewed-by trailers.</p>';
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
  const control = `<section class="setup-card routing-settings"><div><span class="label">ROUTING SETTINGS</span><h2>Coordinator &amp; pilot mode</h2><p>Optional overrides. Raven normally discovers contacts from connected sources and learns from Slack replies.</p><label for="coordinator-select">Coordinator</label><select id="coordinator-select" aria-label="Coordinator"><option value="">No coordinator — use the Slack triage channel</option>${people.map(p => `<option value="${esc(p.id)}" ${coordinator && coordinator.id === p.id ? 'selected' : ''}>${esc(p.name)}${p.team ? ` · ${esc(p.team)}` : ''}</option>`).join('')}</select><p class="field-help">Optional fallback before the Slack triage channel.</p></div><div class="routing-options"><label class="routing-option" for="verified-only"><input type="checkbox" id="verified-only" ${settings.require_verified_route ? 'checked' : ''}><span><strong>Pilot mode: route only verified owners</strong><small>Route git-history-only matches to the coordinator, with the candidates named.</small></span></label><label class="routing-option" for="auto-rules"><input type="checkbox" id="auto-rules" ${settings.auto_rules ? 'checked' : ''}><span><strong>Automatic rules</strong><small>Matching reusable rules authorize without a fresh signature. Leave off to review every request.</small></span></label><label class="routing-option" for="brief-mode"><span><strong>Task page in messages</strong><small>Each Slack message carries the recipient’s own link to the task page, no account needed.</small><select id="brief-mode" aria-label="Task page in messages">${[['off','Off: messages link to the inbox'],['static','Task page: the task’s context, the answer form and notes']].map(([v,l]) => `<option value="${v}" ${(settings.brief_mode || 'static') === v ? 'selected' : ''}>${l}</option>`).join('')}</select></span></label></div></section>`;
  const cards = people.length ? `<div class="people-grid">${people.map((p, i) => `<article class="person-card ${p.active ? '' : 'inactive'}">${avatar(p.name, i)}<h3>${esc(p.name)}</h3><p>${esc(p.team || '')}${p.teams && p.teams.length ? ` · ${esc(p.teams.join(', '))}` : ''}</p><small>${[p.email, p.github_login ? '@' + p.github_login : '', p.slack_id ? 'Slack ' + p.slack_id : ''].filter(Boolean).map(esc).join(' · ') || 'no identities recorded'}</small><small>${(p.authority || []).length ? (p.authority || []).map(a => `${roleLabels[a.role] || a.role} ${a.scope_kind === 'repo' ? 'everything' : esc(a.scope)}${a.repo ? ` in ${esc(a.repo)}` : ''}`).join('; ') : 'Contact discovered · routing follows available evidence'}</small>${p.active ? '' : '<small>inactive</small>'}</article>`).join('')}</div>` : '<p class="context">Connect Slack to discover people automatically. No manual owner setup or personal Raven accounts are required.</p>';
  const rows = dir.authority || [];
  // A row whose date has passed decides nothing; it read the same as a
  // live one, which is how a map can look set up and route nowhere.
  const expired = a => Boolean(a.effective_to) && a.effective_to.slice(0, 10) < new Date().toISOString().slice(0, 10);
  const inForce = rows.filter(a => !expired(a)).length;
  const table = rows.length ? `<div class="table-wrap"><table><thead><tr><th>WHO</th><th>ROLE</th><th>SCOPE</th><th>REPOSITORY</th><th>SOURCE</th><th>UNTIL</th><th></th></tr></thead><tbody>${rows.map(a => `<tr class="${expired(a) ? 'inactive' : ''}"><td><strong>${esc(a.who)}</strong>${a.is_team ? ' <small>(team)</small>' : ''}</td><td>${esc(roleLabels[a.role] || a.role)}${a.accepted ? '' : ' <small>(not yet accepted)</small>'}</td><td><code>${esc(a.scope_kind === 'repo' ? 'everything' : a.scope)}</code> <small>${esc(scopeLabels[a.scope_kind] || a.scope_kind)}</small></td><td>${esc(a.repo || 'every repository')}</td><td>${esc(a.source)}${a.asserted_by ? `<br><small>asserted by ${esc(a.asserted_by)}</small>` : ''}${a.note ? `<br><small>${esc(a.note)}</small>` : ''}</td><td>${a.effective_to ? `${esc(a.effective_to.slice(0, 10))}${expired(a) ? ' <small>expired: decides nothing now</small>' : ''}` : 'open'}</td><td>${canAdminister() ? `<button class="button small soft" data-action="end-authority" data-id="${esc(a.id)}">End</button>` : ''}</td></tr>`).join('')}</tbody></table></div>` : '<p class="context">No manual overrides. Raven infers contacts from connected sources and learns from replies.</p>';
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
  return `<div class="owners-page">${workspaceAccountCard()}${readiness()}${verifiedLayer()}<details class="history" id="repository-signals"><summary>Repository history and routing signals</summary>${ownershipGraph()}</details><div class="wide-toolbar"><h2>Owners by path pattern</h2><span class="muted">${state.owners.length} people</span></div><div class="people-grid">${state.owners.map((owner, i) => `<article class="person-card">${avatar(owner.name, i)}<h3>${esc(owner.name)}</h3><p>${esc(owner.team)}</p><code>${esc(owner.patterns)}</code><small>${state.decisions.filter(d => d.owner_id === owner.id && d.status === 'pending').length} pending · ${plural(state.decisions.filter(d => d.owner_id === owner.id && d.status === 'approved').length, 'answer')} captured</small></article>`).join('')}</div>${state.owners.length ? '<div class="notice">A configured owner is a person with verified authority for their path patterns. The patterns also route by repository-relative file path when no graph exists: simple globs, separated by commas, the last matching owner wins. You can reassign pending requests from the inbox.</div>' : empty('Start with the people who know.', 'Add an owner and the paths they are responsible for.', 'owner', 'Add your first owner')}</div>`;
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
  if (!d.enabled) return githubCard + `<div class="setup-card"><h2>Slack delivery</h2><p>Set <code>SLACK_BOT_TOKEN</code> and <code>SLACK_SIGNING_SECRET</code> where Raven runs, point the app's Events API at <code>/webhooks/slack</code>, and every question, sign-off and review lands in the owner's DMs, with replies in the thread recorded as answers. Raven imports Slack contacts automatically. No owner setup or recipient accounts are required. See <a href="https://github.com/nintendo-ds75/raven-preview/blob/main/docs/slack.md" target="_blank" rel="noopener">Slack setup</a>.</p></div>`;
  const failed = (d.failed || 0);
  return githubCard + `<div class="setup-card"><h2>Slack delivery</h2><p>Contacts are discovered automatically. People reply here without a Raven account.</p><p>${esc(d.directory?.error || (d.directory?.synced_at ? `${d.directory.people} contacts synced` : 'Directory sync starting…'))}</p><button class="button soft" data-action="slack-sync">Refresh Slack contacts</button><p>${d.sent || 0} sent · ${d.queued || 0} queued · ${failed} failed${d.signing_secret ? '' : ' · replies are not verified: set SLACK_SIGNING_SECRET'}</p><div class="form-row"><div><label for="fallback-channel">Triage channel (when no contact can be reached)</label><input id="fallback-channel" maxlength="100" placeholder="C0123ABC" value="${esc(d.fallback_channel || '')}"></div><div><button class="button" data-action="fallback">Save</button></div></div>${failed ? `<button class="button soft" data-action="deliveries">Show failed deliveries</button><div id="deliveries"></div>` : ''}</div>`;
}

function agentStatus() {
  const tokens = state.agent_connections || [];
  return tokens.length ? tokens.map(t => `<p>${esc(t.label)} · ${pill(t.last_used_at ? 'Activity detected' : 'Waiting for first use', t.last_used_at ? 'green' : 'gray')}${t.last_used_at ? ` · Last seen ${esc(ago(t.last_used_at))}` : ''}</p>`).join('') : '<p>No active agent credentials yet.</p>';
}

function connections() {
  const shared = !!state.auth?.enabled;
  if (!shared) {
    const {remote, notice, ...config} = state.mcp_config || {};
    return `<div class="setup-grid"><section class="setup-card"><h2>Local agent connection</h2><p>This server has authentication disabled. Use the MCP configuration below; <code>./agents</code> is not required to connect. The optional helper only lets this page open local terminal sessions.</p><pre id="mcp-config">${esc(JSON.stringify(config, null, 2))}</pre><button class="button" data-action="copy">Copy configuration</button></section><section>${deliveriesCard()}</section></div>`;
  }
  return `<div class="setup-grid mcp-setup"><section><div class="setup-card"><h2>Connect an agent</h2><p>Give it a task in your own words. It finds the files and brings decisions here.</p>${agentCards()}<h3>Your agent connections</h3><div id="agent-status" aria-live="polite">${agentStatus()}</div><p>Activity refreshes automatically every five seconds while this tab is visible. Last seen confirms credential use, not a continuously running session.</p><button class="button" data-action="agent-status">Refresh status</button><details class="history" id="manual-agent-setup"><summary>Advanced manual configuration</summary><p>Choose your client, create a limited credential, and add the connection in that client’s MCP settings.</p><label for="agent-client">Agent client</label><select id="agent-client"><option>Claude Code</option><option>Cursor</option><option>Codex</option><option>Other MCP client</option></select><button class="button primary" data-action="agent-setup">Set up agent</button><p>Creating a credential does not connect the agent automatically. Your client must support HTTP MCP and authorization headers.</p></details>${shared ? '' : '<p>Authenticated HTTP agent connections require authentication to be enabled on this server.</p>'}</div><a class="button" href="#runs">View tasks</a></section><section>${workspaceAccountCard()}<details class="history" id="source-connections"><summary>Repository sources and Slack</summary><p>Connect GitHub or index a local clone for repository history. Slack discovers contacts automatically; Raven uses the evidence to choose who to ask.</p>${deliveriesCard()}</details></section></div>`;
}

function workspaceAccountCard() {
  if (!state.auth?.enabled || state.me?.role !== 'admin') return '';
  return `<section class="setup-card"><h2>${esc(state.workspace?.name || 'Set up your workspace')}</h2>${state.workspace?.needs_setup ? '<p>Create your owner login and name this workspace.</p><a class="button primary" href="/auth/setup">Create workspace</a>' : ''}<p>Optional browser access. People can already answer in Slack without a Raven account. Invite links are private and valid for 48 hours.</p><button class="button" data-action="invite-person">Invite teammate</button></section>`;
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
  if (!state.auth) {
    $('#new-request').hidden = true;
    $('#manual-actions').hidden = true;
    $('#app').innerHTML = '<p class="context" role="status">Loading workspace…</p>';
    return;
  }
  $('#new-request').hidden = view === 'connect' || state.me?.role === 'viewer' || (view === 'owners' && !canAdminister());
  $('#manual-actions').hidden = $('#new-request').hidden;
  const noteDraft = $('#note-text')?.value;
  const active = document.activeElement;
  const focusId = active.id;
  const selection = active.tagName === 'INPUT' ? active.selectionStart : null;
  const disclosures = [...document.querySelectorAll('#app details[id]')].map(el => ({id: el.id, open: el.open, scroll: el.querySelector('ul')?.scrollTop || 0}));
  $('.page-head').hidden = !!taskId();
  $('#app').innerHTML = taskId() ? taskOverview() : ({inbox, runs, memory, owners, connect: connections})[view]();
  if (noteDraft && $('#note-text')) $('#note-text').value = noteDraft;
  for (const saved of disclosures) {
    const el = document.getElementById(saved.id);
    if (el) {
      el.open = saved.open;
      if (el.querySelector('ul')) el.querySelector('ul').scrollTop = saved.scroll;
    }
  }
  if (focusId && document.getElementById(focusId) && ['INPUT','SELECT','BUTTON','SUMMARY'].includes(active.tagName)) {
    const replacement = document.getElementById(focusId);
    replacement.focus();
    if (selection !== null && ['search','text'].includes(replacement.type)) replacement.setSelectionRange(selection, selection);
  }
}

function showHelp() {
  openModal('Raven help', 'Follow the task. Answer the decisions that need you.',
    '<h3>Follow a task</h3><p>Open Tasks for the brief, what the agent has learned, who it has asked, and the complete history. Owner signoff is required even when Raven finds an answer in memory, unless an enabled standing rule applies.</p><h3>Connect your tools</h3><p>Use Connections &amp; setup to authorize GitHub repositories, configure MCP, and check agent activity. MCP connections do not require the local launcher.</p><h3>Work with your team</h3><p>Admins can invite teammates from Connections or People &amp; ownership. Owners are the people responsible for decisions.</p><h3>Start or resume an agent</h3><p>Terminal buttons require the local launcher running on your computer because a browser cannot discover or start local CLI sessions. Resume opens the agent’s own session picker.</p><div class="modal-actions"><button class="button primary" data-action="help-connect">Open Connections &amp; setup</button><button class="button" data-action="close">Close</button></div>');
}

function openModal(title, subtitle, body) {
  modalVersion += 1;
  delete $('#modal').dataset.runId;
  $('#modal-content').innerHTML = `<div class="modal-head"><div><h2 id="modal-title">${esc(title)}</h2><p>${esc(subtitle)}</p></div><button class="icon-button" data-action="close" aria-label="Close dialog">${icon('close')}</button></div><div class="modal-body">${body}</div>`;
  if (!$('#modal').open) $('#modal').showModal();
}

function ownerOptions(selected, auto = false) {
  return `${auto ? '<option value="">Route automatically by path</option>' : '<option value="">Select an owner</option>'}${state.owners.map(o => `<option value="${esc(o.id)}" ${selected === o.id ? 'selected' : ''}>${esc(o.name)} · ${esc(o.team)}</option>`).join('')}`;
}

function newRequest() {
  openModal('Create a request', 'Add a task and its first decision manually.', `<form id="request-form"><label for="title">Task</label><input id="title" name="title" placeholder="Add usage-based pricing" maxlength="300" required><div class="form-row"><div><label for="agent">Agent</label><input id="agent" name="agent" placeholder="Claude Code, Codex, Cursor…" maxlength="100" required></div><div><label for="repo">Repository</label><input id="repo" name="repo" placeholder="team/platform" maxlength="300" required></div></div><label for="question">What decision is needed?</label><textarea id="question" name="question" placeholder="Ask one specific question." maxlength="2000" required></textarea><label for="context">Context & evidence</label><textarea id="context" name="context" placeholder="What does the owner need to know? Include constraints and consequences." maxlength="12000" required></textarea><label for="path">Relevant file path</label><input id="path" name="path" placeholder="billing/usage.py" maxlength="1000" required><label for="owner_id">Decision owner</label><select id="owner_id" name="owner_id">${ownerOptions('',true)}</select><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button type="button" class="button" data-action="close">Cancel</button><button class="button primary" type="submit">Create request ${icon('arrow')}</button></div></form>`);
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

async function review(id) {
  const requestVersion = ++modalVersion;
  try {
    const d = await api(`/api/decisions/${encodeURIComponent(id)}`);
    if (requestVersion !== modalVersion) return;
    const canDecide = (d.allowed_actions || []).includes('answer');
    const canRoute = (d.allowed_actions || []).includes('assign');
    if (!canDecide && !canRoute) {
      openModal(d.question, `${d.agent} · ${d.run_title}`, `<div class="metadata">${pill(statusLabel(d))}${signoffLabel(d)}</div><p class="context">${state.me?.role === 'viewer' ? 'Read-only access.' : 'This decision belongs to another owner. You can add context on the task overview.'} ${esc(d.owner_name || 'No owner assigned')} owns this decision.</p><p class="modal-context">${esc(d.context || '')}</p>${d.answer ? `<div class="context-box"><span class="label">${signedNow(d) ? 'Signed answer' : 'Answer awaiting owner signoff'}</span>${esc(d.answer)}</div>` : '<p>No answer recorded yet.</p>'}${d.rationale ? `<p><strong>Why:</strong> ${esc(d.rationale)}</p>` : ''}${d.evidence ? `<p class="context"><strong>Evidence:</strong> ${esc(d.evidence)}</p>` : ''}${d.routing_reason ? `<p class="context"><strong>Routing:</strong> ${esc(d.routing_reason)}</p>` : ''}${d.source_id ? `<button class="button small" data-action="source" data-id="${esc(d.source_id)}">Inspect source decision</button>` : ''}${followupControls(d)}<details class="history"><summary>Decision history · ${plural(d.events.length, 'event')}</summary>${d.events.map(e => `<div class="history-item"><strong>${esc(eventLabels[e.kind] || e.kind)}</strong>${esc(e.detail)}<br><small>${esc(new Date(e.created_at).toLocaleString())}</small></div>`).join('')}</details>`);
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
    openModal(d.question, `${d.agent} · ${d.run_title}`, `<div class="metadata">${pill(statusLabel(d), settled ? 'green' : '')}${kindBadge(d)}${signoffLabel(d)}<span>${esc(d.repo)} / ${esc(d.path)}</span></div>${upfront}${d.brief ? `<div class="context-box"><span class="label">The brief</span>${esc(d.brief)}</div>` : ''}<details class="history" ${d.answer || d.prediction ? '' : 'open'}><summary>Context and constraints</summary><p class="modal-context">${esc(d.context)}</p></details>${canonical}
      ${d.needs_review ? `<div class="context-box prediction"><span class="label">Needs review</span>${esc(d.review_reason || 'An answer this decision leaned on was corrected.')} Confirm the answer below by signing it, or correct it.</div>` : ''}
      ${signed ? `<div class="context-box"><span class="label">${esc(signedAs(d))} · began as ${esc(beganAs(d))}</span>${esc(d.answer || '')}${d.source_id ? `<br><button class="button text small" data-action="source" data-id="${esc(d.source_id)}">Inspect source decision ↗</button>` : ''}</div>` : ''}
      ${(d.allowed_actions || []).includes('rule') && (approved || (signed && d.signoff === 'signed')) && !inert ? ruleBox(d) : ''}
      ${d.evidence && !signoffWanted ? `<p class="context"><strong>Evidence:</strong> ${esc(d.evidence)}</p>` : ''}${d.owner_evidence ? `<details class="history"><summary>Why this owner</summary><p class="context">${esc(d.owner_evidence)}</p></details>` : ''}
      ${suggestion ? `<div class="context-box prediction"><span class="label">${d.status === 'resolved' || d.status === 'partial' ? resolvedLabel(d) : d.status === 'assumed' ? 'Default assumed · not approved' : d.status === 'proposed' ? 'Prediction, unconfirmed · not a decision' : predictionLabel(d)}</span>${esc(suggestion)}${predictionScope(d) ? `<p class="context"><strong>Scope:</strong> ${esc(predictionScope(d))}</p>` : '<br>'}<button class="button text small" data-action="use-suggestion" data-text="${esc(suggestion)}">Use this text as the answer</button>${d.source_id ? `<button class="button text small" data-action="source" data-id="${esc(d.source_id)}">Inspect source decision ↗</button>` : ''}</div>` : ''}
      ${signoffWanted ? `<div class="context-box prediction"><span class="label">${resolvedLabel(d)}${halfSigned(d) ? ' · every required approver signs' : ' · evidence, not sign-off · your signature is wanted'}</span>${esc(d.answer || d.prediction || '')}${d.evidence ? `<p class="context"><strong>Evidence:</strong> ${esc(d.evidence)}</p>` : ''}<p class="context">The agent prepares on this answer and cannot finish its task until you sign it or correct it.</p>${d.source_id ? `<button class="button text small" data-action="source" data-id="${esc(d.source_id)}">Inspect source decision ↗</button>` : ''}<div class="modal-actions"><button class="button primary small" data-action="signoff" data-id="${esc(d.id)}" data-updated="${esc(d.updated_at)}">Sign off as ${esc(signer)} ${icon('check')}</button></div></div>
      <form id="correct-form" data-id="${esc(d.id)}"><input type="hidden" name="expected_updated_at" value="${esc(d.updated_at)}"><label for="correction">Or correct it</label><textarea id="correction" name="answer" placeholder="The answer the agent should act on instead." maxlength="12000" required></textarea><label for="correction-rationale">Why this correction?</label><textarea id="correction-rationale" name="rationale" placeholder="Capture the reasoning for future tasks." maxlength="12000" required></textarea>${applicabilityFields(d, 'correction')}<p class="context">A correction is a signed answer recorded on behalf of ${esc(signer)}; the agent reads it on the tree, and every decision that leaned on the old answer is marked for review.</p><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button type="button" class="button" data-action="close">Close</button><button class="button primary" type="submit">Record correction ${icon('check')}</button></div></form>` : ''}
      ${inert ? '' : `<div class="modal-owner">${avatar(d.owner_name)}<select id="assign-owner" aria-label="Decision owner" ${assignable ? '' : 'disabled'}>${ownerOptions(d.owner_id)}</select>${assignable ? `<button class="button small" data-action="assign" data-id="${esc(d.id)}">Assign this one</button><button class="button small soft" data-action="refer" data-id="${esc(d.id)}" data-updated="${esc(d.updated_at)}">Hand on &amp; learn</button>` : ''}</div>${assignable && d.handon ? `<label class="refer-scope" for="refer-scope">Hand on teaches Raven that they decide <select id="refer-scope" aria-label="What handing on teaches">${d.handon.options.map(o => `<option value="${esc(o.scope_kind)}:${esc(o.scope)}" ${o.scope_kind === d.handon.default.scope_kind && o.scope === d.handon.default.scope ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}</select></label>${d.handon.why_none ? `<p class="field-help">${esc(d.handon.why_none)}; pick a scope to teach one.</p>` : ''}` : ''}<p class="context">${esc(d.routing_reason)}${d.routing_reason && !/[.!?]$/.test(d.routing_reason) ? '.' : ''}${assignable ? ' Assign moves this request only; Hand on also teaches Raven the scope above, once the person answers.' : ''}</p>`}
      ${signed && canDecide ? `<form id="correct-form" data-id="${esc(d.id)}"><input type="hidden" name="expected_updated_at" value="${esc(d.updated_at)}"><label for="correction">Signed answer · edit to make a correction</label><textarea id="correction" name="answer" maxlength="12000" required>${esc(d.answer || '')}</textarea><label for="correction-rationale">Why this correction?</label><textarea id="correction-rationale" name="rationale" placeholder="Capture the reasoning for future tasks." maxlength="12000" required></textarea>${applicabilityFields(d, 'correction')}<p class="context">A correction is a signed answer recorded on behalf of ${esc(signer)}; the agent reads it on the tree, and every decision that leaned on the old answer is marked for review.</p><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button type="button" class="button" data-action="close">Close</button><button class="button primary" type="submit">Record correction ${icon('check')}</button></div></form>` : ''}
      ${!canDecide || inert || signoffWanted || signed ? '' : `<form id="answer-form" data-id="${esc(d.id)}"><input type="hidden" name="expected_updated_at" value="${esc(d.updated_at)}"><label for="answer">${approved ? 'Recorded answer · edit to make a correction' : 'Your answer'}</label><textarea id="answer" name="answer" placeholder="Give the agent a clear decision and any conditions." maxlength="12000" required>${esc(approved ? d.answer : '')}</textarea><label for="rationale">Why this decision?</label><textarea id="rationale" name="rationale" placeholder="Capture the reasoning for future tasks." maxlength="12000" required>${esc(d.rationale || '')}</textarea>${applicabilityFields(d, 'answer')}<label for="supersedes">Supersedes decision (optional id)</label><input id="supersedes" name="supersedes" placeholder="Decision id this answer replaces" maxlength="100" value="${esc(d.supersedes || '')}"><p class="context">${me ? `Recorded as ${esc(me)}${d.owner_name && d.owner_name !== me ? `, on behalf of ${esc(d.owner_name)}` : ''}.` : `Recorded by the local operator on behalf of ${esc(d.owner_name || 'the assigned owner')}.`} ${approved ? 'The previous answer stays in the revision history.' : (state.executions || []).some(e => e.run_id === d.run_id) ? 'Saving queues delivery to the waiting agent. Follow delivery in Tasks.' : 'The agent can retrieve your answer after it is saved.'}</p><p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button type="button" class="button" data-action="close">Close</button><button class="button primary" type="submit" ${d.owner_id ? '' : 'disabled'}>${approved ? 'Save correction' : 'Record decision'} ${icon('check')}</button></div></form>`}
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
  const target = event.target.closest('[data-action]');
  if (!target || target.disabled) return;
  const action = target.dataset.action;
  try {
    if (action === 'close') $('#modal').close();
    if (action === 'new') newRequest();
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
      $('#modal').close(); await refresh();
      notify(result.repositories.length ? 'Connected repositories refreshed. Sync has started.' : 'No repositories selected for Raven yet.');
      return;
    }
    if (action === 'use-suggestion') { const ta = $('#answer'); if (ta) { ta.value = target.dataset.text; ta.focus(); } }
    if (action === 'help') showHelp();
    if (action === 'help-connect') {
      $('#modal').close();
      if (location.hash === '#connect') navigate();
      else location.hash = '#connect';
    }
    if (action === 'task') newTask();
    if (action === 'retry') await refresh();
    if (action === 'tab') { tab = target.dataset.tab; tabChosen = true; render(); }
    if (action === 'review' || action === 'source') await review(target.dataset.id);
    if (action === 'run') location.hash = 'runs/' + encodeURIComponent(target.dataset.id);
    if (action === 'task-tab') { taskTab = target.dataset.tab; render(); }
    if (action === 'task-refresh') await loadTask();
    if (action === 'task-brief') { const r = await api(`/api/tasks/${encodeURIComponent(taskId())}/link`, {decision_id: taskBriefDecision()}); location.assign(r.url); }
    if (action === 'task-share') { await navigator.clipboard.writeText(location.href); notify('App link copied. Workspace access is required on shared instances.'); }
    if (action === 'execution-details') showRun(target.dataset.id);
    if (action === 'assign') {
      target.disabled = true;
      await api(`/api/decisions/${target.dataset.id}/assign`, {owner_id: $('#assign-owner').value});
      await refresh(); await review(target.dataset.id); notify('Decision reassigned.');
    }
    if (action === 'refer') {
      const owner = state.owners.find(o => o.id === $('#assign-owner').value);
      if (!owner) { notify('Pick the person to hand it to first.'); return; }
      target.disabled = true;
      const [scopeKind, ...scopeRest] = ($('#refer-scope')?.value || '').split(':');
      const result = await api(`/api/decisions/${target.dataset.id}/refer`, {person: owner.name, by: state.me?.name || 'Local operator', expected_updated_at: target.dataset.updated, ...(scopeKind ? {scope_kind: scopeKind, scope: scopeRest.join(':')} : {})});
      await refresh(); await review(target.dataset.id); notify(result.notice);
    }
    if (action === 'end-rule') {
      target.disabled = true;
      const result = await api(`/api/decisions/${target.dataset.id}/rule`, {end: true, by: state.me?.name || 'Local operator', expected_updated_at: target.dataset.updated});
      await refresh(); await review(target.dataset.id); notify(result.notice);
    }
    if (action === 'signoff') {
      target.disabled = true;
      const d = state.decisions.find(x => x.id === target.dataset.id);
      const signed = await api(`/api/decisions/${target.dataset.id}/signoff`, {by: (d && halfSigned(d) && stillToSign(d)[0]) || d?.owner_name || 'Local operator', expected_updated_at: target.dataset.updated || d?.updated_at || ''});
      await refresh(); await review(target.dataset.id); notify(signed?.notice || 'Signed off. The agent sees it on the tree.');
    }
    if (action === 'followups') {
      const text = ($('#followups')?.value || '').trim();
      if (!text) { notify('Write one question per line first.'); return; }
      target.disabled = true;
      const d = state.decisions.find(x => x.id === target.dataset.id);
      const result = await api(`/api/decisions/${target.dataset.id}/followups`, {questions: text, required: !!$('#followups-required')?.checked, by: d?.owner_name || 'Local operator'});
      await refresh(); await review(target.dataset.id); notify(`${result.nodes.length} follow-up ${result.nodes.length === 1 ? 'question' : 'questions'} added to the agent's tree.`);
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
      $('#deliveries').innerHTML = result.notifications.length ? `<div class="table-wrap"><table><thead><tr><th>TO</th><th>KIND</th><th>ERROR</th><th></th></tr></thead><tbody>${result.notifications.map(n => `<tr><td>${esc(n.person_name)}</td><td>${esc(n.kind)}</td><td><small>${esc(n.last_error)}</small></td><td><button class="button small" data-action="retry-delivery" data-id="${esc(n.id)}">Retry</button></td></tr>`).join('')}</tbody></table></div>` : '<p class="context">Nothing failed.</p>';
    }
    if (action === 'retry-delivery') {
      target.disabled = true;
      await api(`/api/deliveries/${target.dataset.id}/retry`, {});
      notify('Queued again.'); await refresh();
    }
    if (action === 'export') await exportWorkspace();
    if (action === 'invite-person') openModal('Invite teammate', 'Invite-only access. No email is sent automatically.', '<form id="invite-form"><label>Email<input name="email" type="email" required maxlength="254"></label><label>Access<select name="role"><option value="member">Member — contribute and answer assigned decisions</option><option value="viewer">Viewer — read only</option></select></label><p class="error" id="form-error" hidden></p><button class="button primary" type="submit">Create invitation</button></form>');
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
      $('#modal').close(); notify('Terminal launch requested. Approve the connection in your agent.');
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
  } catch (error) { notify(error.message); target.disabled = false; }
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
  if (!['invite-form','request-form','owner-form','answer-form','correct-form','task-form','ingest-form','person-form','team-form','authority-form','rule-form','note-form'].includes(form.id)) return;
  event.preventDefault();
  const button = form.querySelector('[type="submit"]');
  if (button.disabled || !form.reportValidity()) return;
  button.disabled = true;
  $('#form-error').hidden = true;
  const data = Object.fromEntries(new FormData(form));
  try {
    if (form.id === 'invite-form') {
      const invitation = await api('/api/invitations', data);
      openModal('Invitation ready', 'Share privately with ' + data.email + '. Expires in 48 hours.', `<pre id="mcp-config">${esc(invitation.url)}</pre><button class="button" data-action="copy">Copy invitation link</button><p>The recipient sets their own password. This link grants access; creating another invitation for this email invalidates the previous one.</p>`);
      return;
    }
    if (form.id === 'task-form') {
      await api('/api/tasks', {...data, submission_key:form.dataset.submissionKey});
      notify('Task queued. Follow progress in Tasks.');
      location.hash = 'runs';
    } else if (form.id === 'request-form') {
      // Keep the started task on the form if the node fails, so retry does not start it twice.
      const taskId = form.dataset.taskId || (await api('/api/tasks/start', {title:data.title, agent:data.agent, repo:data.repo, paths:data.path})).task_id;
      form.dataset.taskId = taskId;
      await api(`/api/tasks/${encodeURIComponent(taskId)}/nodes`, {question:data.question, context:data.context, paths:data.path, ...(data.owner_id ? {owner_id:data.owner_id} : {})});
      notify('Request created. Its owner can review it in the inbox.');
    } else if (form.id === 'note-form') {
      await api(`/api/tasks/${encodeURIComponent(form.dataset.id)}/notes`, {text: data.text, by: state.me?.name || 'Local operator'});
      notify('Note added. The agent can read it on the tree.');
      form.reset();
    } else if (form.id === 'rule-form') {
      const d = state.decisions.find(x => x.id === form.dataset.id);
      const result = await api(`/api/decisions/${form.dataset.id}/rule`, {by: state.me?.name || d?.signed_by || d?.owner_name || 'Local operator', expected_updated_at: form.dataset.updated, ...data});
      notify(result.notice);
    } else if (form.id === 'correct-form') {
      const d = state.decisions.find(x => x.id === form.dataset.id);
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
      takeApplicability(data);
      await api(`/api/decisions/${form.dataset.id}/answer`, data); notify('Decision saved. The answer is available to the agent.');
    }
    $('#modal').close();
    await refresh();
  } catch (error) {
    $('#form-error').textContent = error.message;
    $('#form-error').hidden = false;
    button.disabled = false;
  }
});
$('#new-request').addEventListener('click', () => view === 'owners' ? newOwner() : newRequest());
$('#modal').addEventListener('close', () => { modalVersion += 1; });
$('#modal').addEventListener('click', event => { if (event.target === $('#modal')) { const r = $('#modal').getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) $('#modal').close(); } });
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
