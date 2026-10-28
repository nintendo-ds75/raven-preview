'use strict';

const workViews = new Map();
const workVerifications = new Map();
const workVerificationPending = new Set();
let workMode = 'task', workAnswer = null;
let workRequest = 0, workData = null, workError = '', workOverview = !window.matchMedia?.('(max-width:900px)').matches;
const workId = () => view === 'work' ? location.hash.slice(1).split('/')[1] || '' : '';
const workRepositories = () => workMode === 'ask' ? (state.question_repositories || state.workbench?.repositories || []) : (state.workbench?.repositories || []);
const workDraft = (id = workId()) => {
  if (!workViews.has(id)) workViews.set(id, {text:'', repository:'', host:'', key:(typeof ravenUuid === 'function' ? ravenUuid() : crypto.randomUUID()), pending:false, payload:null, error:''});
  return workViews.get(id);
};
const WORK_STATUS = {queued:'Getting ready', running:'Working', waiting:'Waiting for an update', input:'Your turn',
  paused:'Paused', pausing:'Stopping the host', cancelled:'Cancelled', completed:'Ready for review'};

// A small, escaped formatting subset. Model output never becomes arbitrary HTML.
function workInline(text) {
  return String(text).split(/(`[^`\n]+`|\*\*[^*\n]+\*\*)/g).map(part => {
    if (part.startsWith('`') && part.endsWith('`') && part.length >= 2) return `<code>${esc(part.slice(1,-1))}</code>`;
    if (part.startsWith('**') && part.endsWith('**') && part.length >= 4) return `<strong>${esc(part.slice(2,-2))}</strong>`;
    return esc(part);
  }).join('');
}
function workText(text) {
  const cells = line => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
  return String(text || '').split(/(```[\s\S]*?```)/g).map(part => {
    if (part.startsWith('```') && part.endsWith('```') && part.length >= 6) return `<pre><code>${esc(part.slice(3,-3).replace(/^\w*\n/,''))}</code></pre>`;
    const lines = part.split('\n'), out = [];
    for (let i=0; i<lines.length; i++) {
      const line = lines[i];
      if (line.includes('|') && lines[i+1]?.includes('|') && cells(lines[i+1]).every(c => /^:?-{3,}:?$/.test(c))) {
        const headers = cells(line), rows = []; i += 1;
        while (lines[i+1]?.trim().startsWith('|')) rows.push(cells(lines[++i]));
        out.push(`<div class="work-table"><table><thead><tr>${headers.map(c => `<th>${workInline(c)}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(c => `<td>${workInline(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);
      } else if (/^#{1,4}\s/.test(line)) out.push(`<h3 class="work-section-title">${workInline(line.replace(/^#{1,4}\s+/,''))}</h3>`);
      else if (/^---+$/.test(line.trim())) out.push('<hr class="work-divider">');
      else out.push(workInline(line));
    }
    return out.join('\n');
  }).join('');
}

async function loadWorkbench() {
  const id = workId(), request = ++workRequest;
  if (!id || !state.auth) return;
  try {
    const [task, tree, trace] = await Promise.all([api(`/api/workbench/${encodeURIComponent(id)}`),
      api(`/api/tasks/${encodeURIComponent(id)}/tree`), api(`/api/tasks/${encodeURIComponent(id)}/trace`)]);
    if (view !== 'work' || id !== workId() || request !== workRequest) return;
    workData = {task, tree, trace}; workError = '';
    renderWorkbench();
  } catch (error) {
    if (view !== 'work' || id !== workId() || request !== workRequest) return;
    if ([401,403,404].includes(error.status)) workData = null;
    workError = error.message; renderWorkbench();
  }
}

// The independent artifact checks, one level down on the result: a 'Checks' disclosure.
function workVerificationPanel(task) {
  const id = task.task_id, saved = workVerifications.get(id), pending = workVerificationPending.has(id);
  const admin = !state.auth.enabled || state.me?.role === 'admin';
  return `<details class="work-checks" data-work-detail="checks"><summary>Checks</summary><p>Operator-selected checks bind the actual candidate and current judgments. Passing repository-owned tests is not proof of every intended behavior.</p><button class="button small" data-work-action="verification-status" ${pending ? 'disabled' : ''}>${saved ? 'Refresh verification' : 'Inspect verification'}</button>${saved ? `<p>${esc((saved.config?.blockers || []).join('. '))}</p>${(saved.receipts || []).map(r => `<details><summary>${esc(r.profile_id)} · ${esc(r.usable_pass ? 'Current configured checks passed' : r.status + (r.current ? '' : ' · no longer current'))}</summary><p>${esc(r.coverage_basis)} · ${esc(r.coverage)}</p><p>${esc(r.reason)}</p><small>Candidate ${esc(r.binding?.candidate_tree_sha256 || '')}</small></details>`).join('') || '<p>No independent verification receipt.</p>'}${admin && saved.config?.ready ? (saved.config.profiles || []).filter(p=>p.repository === task.repository).map(p=>`<button class="button small" data-work-action="verify-profile" data-profile="${esc(p.id)}" ${pending ? 'disabled' : ''}>Run ${esc(p.label || p.id)}</button>`).join('') : ''}` : ''}</details>`;
}

function workComposer(id, enabled) {
  const draft = workDraft(id), repos = workRepositories();
  const selected = draft.repository || repos[0]?.id || '';
  const hosts = state.workbench?.hosts || [];
  const selectedHost = draft.host || hosts.find(h => h.ready)?.id || hosts[0]?.id || 'claude';
  return `<form id="work-compose" class="work-compose" data-task="${esc(id)}">
    <label class="sr-only" for="work-prompt">${id ? 'Message Raven' : workMode === 'ask' ? 'Your question' : 'Describe your task'}</label>
    <textarea id="work-prompt" name="prompt" rows="${id ? '2' : '3'}" maxlength="12000" required placeholder="${id ? 'Add context or ask Raven to continue…' : workMode === 'ask' ? 'What would you like to know?' : 'What would you like to get done?'}" ${draft.pending || draft.payload ? 'readonly' : ''}>${esc(draft.text)}</textarea>
    <div class="work-compose-bottom">${id ? '<span>Enter to send · Shift + Enter for a new line</span>' : `<label class="work-repository"><span class="sr-only">Repository</span><select id="work-repository" aria-label="Repository" ${draft.pending || draft.payload ? 'disabled' : ''}>${repos.map(r => `<option value="${esc(r.id)}" ${r.id === selected ? 'selected' : ''}>${esc(r.name)}</option>`).join('') || '<option>No repository connected</option>'}</select></label>`}
    ${!id && workMode === 'task' && hosts.length ? `<label class="work-repository"><span class="sr-only">Coding host</span><select id="work-host" aria-label="Coding host" ${draft.pending || draft.payload ? 'disabled' : ''}>${hosts.map(h => `<option value="${esc(h.id)}" ${h.id === selectedHost ? 'selected' : ''}>${esc(h.name)}${h.ready ? '' : ' · setup needed'}</option>`).join('')}</select></label>` : ''}
    <button class="work-send" type="submit" aria-label="${id ? 'Send message' : workMode === 'ask' ? 'Ask question' : 'Start task'}" ${!enabled || draft.pending ? 'disabled' : ''}>${draft.pending ? '<span class="loading-dot"></span>' : icon('arrow')}</button></div>
    ${!id && workMode === 'task' && hosts.length ? `<p class="work-footnote">${esc(hosts.find(h => h.id === selectedHost)?.ready ? 'Code runs through Raven task tools. Selected context and tool results are sent to ' + hosts.find(h => h.id === selectedHost).inference_destination + '.' : (hosts.find(h => h.id === selectedHost)?.blockers || []).join('. '))}</p>` : ''}
    ${draft.error ? `<p class="work-error" role="alert">${esc(draft.error)} Your text is still in this browser. You can retry.</p>` : ''}
  </form>`;
}

function workActivityLabel(a) {
  const input = a.detail?.input || {};
  return ({list_files:'Explored the repository', search_files:'Searched the code', read_file:'Read ' + (input.path || 'a file'),
    write_file:'Updated ' + (input.path || 'a file'), edit_file:'Updated ' + (input.path || 'a file'), run_command:'Ran a command', bridge_review_discovery:'Reviewed the investigation',
    bridge_add_node:'Asked for a decision', bridge_get_tree:'Read the latest decisions', bridge_search_context:'Searched connected context',
    bridge_get_record:'Read a source', finish_task:'Checked completion', present_result:'Presented the result', pause:'Paused by ' + (a.detail?.actor || 'a teammate'), resume:'Resumed by ' + (a.detail?.actor || 'a teammate'), wait_for_people:'Saved progress and waited'})[a.label] || a.label.replaceAll('_',' ');
}

function workDecision(n) {
  const authorized = taskAuthorized(n);
  // The same sentence as the task page row (task.js); the owner is added only when the sentence does not name them.
  const status = taskStatusSentence(n).text;
  const who = n.owner && !status.includes(n.owner) ? ` · ${esc(n.owner)}` : '';
  const origin = ({record:'Source context',memory:'Earlier decision',human:'Human answer',agent:'Agent proposal'})[n.source] || 'Recorded finding';
  return `<details class="work-decision" data-work-detail="decision-${esc(n.node_id)}"><summary><span class="work-decision-state ${authorized ? 'is-signed' : ''}">${esc(status)}${who}</span><span>${esc(n.question)}</span>${n.answer ? `<span class="work-answer-preview">${esc(n.answer)}</span>` : ''}</summary>
    <p class="work-muted">${esc(origin)}${n.signed_by ? ' · Signed by ' + esc(n.signed_by) : ''}</p>
    ${n.rationale ? `<p><strong>Rationale:</strong> ${esc(n.rationale)}</p>` : ''}
    ${n.alternatives ? `<p><strong>Alternatives:</strong> ${esc(typeof n.alternatives === 'string' ? n.alternatives : JSON.stringify(n.alternatives))}</p>` : ''}
    ${n.consequences ? `<p><strong>Consequences:</strong> ${esc(typeof n.consequences === 'string' ? n.consequences : JSON.stringify(n.consequences))}</p>` : ''}
    ${typeof taskSources === 'function' ? taskSources(n,'work-'+n.node_id) : ''}
    <button class="button small" data-action="review" data-id="${esc(n.node_id)}">${authorized ? 'Inspect decision' : 'Review & answer'}</button></details>`;
}

function workBirdseye(tree, trace, task = {}) {
  const nodes = flattenTask(tree.nodes).filter(n => !['duplicate','adopted'].includes(n.status));
  const signed = nodes.filter(taskAuthorized), remaining = nodes.filter(n => !taskAuthorized(n));
  const blocked = remaining.filter(n => n.blocking || n.needs_review || n.source_refresh_required);
  const people = [...new Set([...nodes.map(n => n.owner), ...(trace.notifications || []).map(n => n.person_name)].filter(Boolean))];
  const investigation = (tree.discovery_reviews || []).findLast(r => r.summary && r.status === 'complete');
  const next = task.status === 'completed' ? 'Review the result and patch before using it.' : task.status === 'paused' ? 'Inspect the saved progress, then continue when ready.' : task.status === 'cancelled' ? 'This task is closed.' : blocked.length ? 'Review the open questions below. Each answer needs its recorded approval before work can use it.' : task.status === 'waiting' ? task.summary || 'Waiting for an update.' : tree.next || 'Raven is investigating the available context.';
  return `<aside id="work-overview" class="work-overview ${workOverview ? 'is-open' : ''}" aria-label="Task overview">
    <div class="work-panel-title"><h2>At a glance</h2><a href="#runs/${encodeURIComponent(tree.task_id)}" title="Open the full task history">Details ↗</a></div>
    <p class="work-next"><strong>Next</strong>${esc(next)}</p>
    <div class="work-counts"><div><strong>${signed.length}</strong><span>resolved decisions</span></div><div><strong>${remaining.length}</strong><span>open questions</span></div></div>
    <section><h3>Remaining questions</h3>${remaining.length ? [...blocked,...remaining.filter(n => !blocked.includes(n))].map(workDecision).join('') : `<p class="work-muted">${nodes.length ? 'No open decision recorded.' : 'Questions will appear as Raven investigates.'}</p>`}</section>
    <section><h3>Resolved decisions</h3>${signed.length ? signed.map(workDecision).join('') : '<p class="work-muted">No authorized answers recorded yet.</p>'}</section>
    ${investigation ? `<details class="work-discovery" data-work-detail="investigation"><summary>What Raven found</summary><p>${esc(investigation.summary)}</p><small>Model observations, not authorization.</small></details>` : ''}
    ${people.length ? `<details class="work-discovery" data-work-detail="people"><summary>People involved · ${people.length}</summary><ul class="work-people">${people.map((p,i) => `<li>${avatar(p,i)}<span>${esc(p)}</span></li>`).join('')}</ul></details>` : ''}
  </aside>`;
}

function renderWorkbench() {
  if (view !== 'work' || !state.auth) return;
  const app = $('#app'), id = workId();
  const active = document.activeElement, focus = active?.id, selection = [active?.selectionStart, active?.selectionEnd];
  const existingInput = $('#work-prompt');
  if (existingInput && app.dataset.workId === id) workDraft(id).text = existingInput.value;
  const disclosures = new Set([...app.querySelectorAll('details[data-work-detail]')].filter(el => el.open).map(el => el.dataset.workDetail));
  const inspections = workDraft(id).inspections ||= {};
  app.querySelectorAll('form[data-work-reconcile]').forEach(form => {
    inspections[form.dataset.workReconcile] = {outcome:form.querySelector('[name=outcome]').value,note:form.querySelector('[name=note]').value};
  });
  const scroll = window.scrollY;
  const followLatest = !app.querySelector('.work-thread') ||
    (document.documentElement?.scrollHeight || 0) - scroll - (window.innerHeight || 0) < 140;
  app.dataset.workId = id;
  const allowed = !state.auth.enabled || ['admin','member'].includes(state.me?.role);
  if (!id) {
    // The home is the composer, only where this server runs tasks in the browser;
    // navigate() sends #work to the task list otherwise.
    if (!state.workbench?.ready) { app.innerHTML = ''; return; }
    const ready = (workMode === 'ask' || state.workbench?.enabled) && workRepositories().length;
    app.innerHTML = `<section class="work-home"><img src="/favicon.svg" alt="" width="38" height="38"><h1>Start a task</h1><div class="work-modes" role="group" aria-label="Choose how to work"><button class="button small" data-work-mode="ask" aria-pressed="${workMode === 'ask'}">Ask a question</button><button class="button small" data-work-mode="task" aria-pressed="${workMode === 'task'}">Start a task</button></div>${workComposer('', ready && allowed)}
      ${!allowed ? '<p class="work-home-note">Your account can view tasks. A workspace member can start one.</p>' : ready ? '' : '<p class="work-home-note"><strong>Context or execution is not set up yet.</strong> <a href="#connect">Open Settings</a></p>'}
      ${workMode === 'ask' && workAnswer ? `<article class="work-question-result" aria-live="polite"><p>${workText(workAnswer.answer)}</p><small>${esc(workAnswer.notice)}</small>${(workAnswer.open || []).map(s => `<p>${esc(s)}</p>`).join('')}<h2>Evidence</h2>${workAnswer.evidence.map(s => `<details><summary>${esc(s.title || s.id)} · ${esc(s.kind === 'prior_decision' ? 'Prior answer' : 'Source')}</summary><p>${workText(s.text)}</p><small>${esc(s.id)} · revision ${esc(s.version)}</small></details>`).join('') || '<p>No matching records. Connect context in Settings, or start a task to investigate.</p>'}</article>` : ''}</section>`;
  } else if (!workData || workData.task.task_id !== id) {
    app.innerHTML = workError ? `<div class="empty" role="alert"><h3>Couldn’t load your workspace.</h3><p>${esc(workError)}</p><button class="button" data-action="retry">Try again</button></div>` : '<p class="work-muted" role="status">Opening your task…</p>';
  } else if (workData.task.withheld || workData.tree.withheld) {
    app.innerHTML = `<section class="work-empty"><h1>Access needs attention</h1><p>${esc(workData.task.notice || workData.tree.notice)}</p><a class="button" href="#connect">Open Settings</a><a href="#runs">Back to tasks</a></section>`;
  } else {
    const {task, tree, trace} = workData, running = ['queued','running','pausing'].includes(task.status);
    const incompleteReview = tree.review?.status === 'failed';
    const activities = task.activity.filter(a => ['tool', 'context', 'control', 'host'].includes(a.kind));
    const latest = activities.at(-1);
    const messages = task.conversation.filter(m => !m.text.startsWith('Raven task ID:') && !m.text.startsWith('Raven has new task information.'));
    if (!messages.length) messages.push({role:'user',text:task.prompt});
    app.innerHTML = `<header class="work-task-header"><div><a href="#work" class="work-back">New task</a><h1 title="${esc(task.prompt)}">${esc(task.prompt)}</h1><span class="work-task-repo">${esc(task.repository)}</span><span class="work-live-status" role="status">${running ? '<span class="loading-dot"></span>' : ''}${esc(WORK_STATUS[task.status] || task.status)}</span></div><div class="work-task-actions">${allowed ? `<button class="button small" data-work-action="${running ? 'pause' : 'resume'}" ${task.status === 'pausing' ? 'disabled' : ''}>${running ? 'Pause' : 'Continue'}</button>` : ''}<button class="button small" data-work-action="overview" aria-expanded="${workOverview}" aria-controls="work-overview">${workOverview && window.matchMedia?.('(max-width:900px)').matches ? 'Conversation' : 'Overview'}</button></div></header>
    ${workError ? `<p class="work-error" role="alert">${esc(workError)} Showing the last saved state.</p>` : ''}
    <div class="work-layout ${workOverview ? '' : 'overview-closed'}"><section class="work-thread" aria-label="Task conversation"><div class="work-messages">${messages.map(m => `<article class="work-message ${m.role}"><span class="work-speaker">${m.role === 'user' ? esc(m.actor || task.requester || 'Requester') : 'Raven'}</span><div>${workText(m.text)}</div></article>`).join('')}
    ${task.pending_messages.map(m => `<article class="work-message user"><span class="work-speaker">${esc(m.actor)} · queued</span><div>${esc(m.body)}</div></article>`).join('')}
    ${task.status === 'waiting' ? `<div class="work-waiting"><span class="status-dot"></span><p>${esc(task.summary || 'Waiting for a reply.')}<small>You can close this page. Raven continues when the task has an update.</small></p></div>` : ''}
    ${task.host?.provider && task.host.provider !== 'scripted' ? `<p class="work-footnote">${esc(({claude:'Claude Agent SDK',codex:'Codex app-server',pi:'Pi Agent SDK',opencode:'OpenCode'})[task.host.provider] || task.host.provider)} · ${esc(task.host.model)}${task.host.session_id ? ' · session saved' : ''}</p>` : ''}
    ${task.error ? `<p class="work-error" role="alert">${esc(task.error)}</p>` : ''}
    ${['paused','cancelled'].includes(task.status) ? (task.unresolved_operations || []).map(op => `<details class="work-activity" data-work-detail="intent-${esc(op.call_id)}"><summary>Check an uncertain operation: ${esc(op.operation || 'Saved operation')}</summary><p>Inspect the saved files and execution records before continuing. Raven will not repeat this operation automatically.</p><pre>${esc(JSON.stringify(op.arguments,null,2))}</pre>${state.me?.role === 'admin' ? `<form data-work-reconcile="${esc(op.call_id)}"><label>Observed outcome<select name="outcome" required><option value="">Choose after inspecting</option><option value="completed">The operation completed</option><option value="not_run">The operation did not run</option></select></label><label>What evidence did you check?<textarea name="note" maxlength="2000" required>${esc(inspections[op.call_id]?.note || '')}</textarea></label><button class="button" type="submit">Record inspection</button></form>` : '<p>A workspace admin must inspect and reconcile this operation.</p>'}</details>`).join('') : ''}
    ${task.status === 'completed' ? `<article class="work-result"><h2>${incompleteReview ? 'Patch ready for manual review' : 'Ready for your review'}</h2>${incompleteReview ? '<p class="work-error">The automatic code review is incomplete. Owner approvals and test results do not replace reviewing the patch.</p>' : ''}<div class="work-result-text">${workText(task.summary)}</div><div class="work-result-actions"><a class="button" href="/api/workbench/${encodeURIComponent(id)}/patch">Download patch</a><a class="button" href="#runs/${encodeURIComponent(id)}">Decisions & review</a></div>${workVerificationPanel(task)}<small>The original repository is unchanged. No commit, push or deployment was made.</small></article>` : ''}
    ${activities.length ? `<details class="work-activity" data-work-detail="activity"><summary>${running ? 'Working · ' : ''}${esc(latest ? workActivityLabel(latest) : 'Activity')}<span>${activities.length} steps</span></summary><ol>${activities.map(a => `<li><details data-work-detail="action-${esc(a.id)}"><summary>${esc(workActivityLabel(a))}</summary><pre>${esc(JSON.stringify(a.detail,null,2))}</pre></details></li>`).join('')}</ol></details>` : '<p class="work-muted" role="status">Preparing a private repository copy…</p>'}
    </div><div class="work-reply">${allowed ? workComposer(id, task.status !== 'pausing') : '<p class="work-muted">You have view access to this conversation.</p>'}</div></section>${workBirdseye(tree,trace,task)}</div>`;
  }
  app.querySelectorAll('details[data-work-detail]').forEach(el => { el.open = disclosures.has(el.dataset.workDetail); });
  app.querySelectorAll('form[data-work-reconcile]').forEach(form => {
    form.querySelector('[name=outcome]').value = inspections[form.dataset.workReconcile]?.outcome || '';
  });
  if (focus && app.querySelector('#' + CSS.escape(focus))) {
    const replacement = document.getElementById(focus); replacement.focus({preventScroll:true});
    if (replacement.setSelectionRange && selection[0] !== null) replacement.setSelectionRange(...selection);
  }
  window.scrollTo({top:id && followLatest && !workError ? document.documentElement?.scrollHeight || scroll : scroll, behavior:'instant'});
}

document.addEventListener('input', event => {
  if (event.target.id === 'work-prompt') workDraft().text = event.target.value;
});
document.addEventListener('change', event => {
  if (event.target.id === 'work-repository') workDraft().repository = event.target.value;
  if (event.target.id === 'work-host') { workDraft().host = event.target.value; renderWorkbench(); }
});
document.addEventListener('keydown', event => {
  if (event.target.id === 'work-prompt' && event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault(); event.target.form.requestSubmit();
  }
});
document.addEventListener('submit', async event => {
  if (event.target.dataset.workReconcile) {
    event.preventDefault();
    const form = event.target, fields = new FormData(form), button = form.querySelector('button');
    if (!form.reportValidity() || button.disabled) return;
    button.disabled = true;
    try {
      await api(`/api/workbench/${encodeURIComponent(workId())}/reconcile`, {call_id:form.dataset.workReconcile,outcome:fields.get('outcome'),note:fields.get('note')});
      await loadWorkbench();
    } catch (error) { workError = error.message; button.disabled = false; }
    return;
  }
  if (event.target.id !== 'work-compose') return;
  event.preventDefault();
  const form = event.target, id = form.dataset.task, draft = workDraft(id), startedOn = navigationVersion;
  if (draft.pending || !form.reportValidity() || (state.auth.enabled && !['admin','member'].includes(state.me?.role))) return;
  const text = draft.text.trim();
  if (!text) return;
  if (!draft.payload) draft.payload = id ? {text,request_key:draft.key} : workMode === 'ask' ? {question:text,repository:$('#work-repository').value} : {prompt:text,request_key:draft.key,repository:$('#work-repository').value,host:$('#work-host')?.value || 'claude'};
  draft.pending = true; draft.error = ''; renderWorkbench();
  try {
    const asking = !id && 'question' in draft.payload;
    const result = await api(id ? `/api/workbench/${encodeURIComponent(id)}/messages` : asking ? '/api/ask' : '/api/workbench', draft.payload);
    if (asking && startedOn === navigationVersion) workAnswer = result;
    workViews.delete(id);
    if (id === workId() && $('#work-prompt')) $('#work-prompt').value = '';
    if (!id && !asking && view === 'work' && !workId() && startedOn === navigationVersion) location.hash = 'work/' + encodeURIComponent(result.task_id);
    else if (id === workId() && startedOn === navigationVersion) await loadWorkbench();
    await refresh({quiet:true});
  } catch (error) { draft.error = error.message; }
  finally { draft.pending = false; if (view === 'work') renderWorkbench(); }
});
document.addEventListener('click', async event => {
  const mode = event.target.closest('[data-work-mode]');
  if (mode) { if (!workDraft('').pending && !workDraft('').payload) {workMode = mode.dataset.workMode; renderWorkbench();} return; }
  const button = event.target.closest('[data-work-action]');
  if (!button) return;
  const action = button.dataset.workAction, id = workId();
  if (action === 'verification-status' || action === 'verify-profile') {
    if (workVerificationPending.has(id)) return;
    if (action === 'verify-profile' && state.auth.enabled && state.me?.role !== 'admin') return;
    workVerificationPending.add(id); renderWorkbench();
    try {
      if (action === 'verify-profile') await api(`/api/workbench/${encodeURIComponent(id)}/verification`, {profile_id:button.dataset.profile});
      const result = await api(`/api/workbench/${encodeURIComponent(id)}/verification`);
      if (id === workId()) {workVerifications.set(id, result); workError = '';}
    } catch (error) {
      workVerifications.delete(id);
      if (id === workId()) workError = error.message;
    } finally { workVerificationPending.delete(id); if (id === workId()) renderWorkbench(); }
    return;
  }
  if (action === 'overview') { workOverview = !workOverview; renderWorkbench(); if (window.matchMedia?.('(max-width:900px)').matches) window.scrollTo({top:0,behavior:'instant'}); return; }
  button.disabled = true;
  try {
    if (action !== 'refresh') await api(`/api/workbench/${encodeURIComponent(id)}/${action}`, {});
    await loadWorkbench();
  } catch (error) { workError = error.message; renderWorkbench(); }
  finally { button.disabled = false; }
});
