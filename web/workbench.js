'use strict';

const workViews = new Map();
let workRequest = 0, workData = null, workError = '', workOverview = !window.matchMedia?.('(max-width:900px)').matches;
const workId = () => view === 'work' ? location.hash.slice(1).split('/')[1] || '' : '';
const workDraft = (id = workId()) => {
  if (!workViews.has(id)) workViews.set(id, {text:'', repository:'', key:crypto.randomUUID(), pending:false, payload:null, error:''});
  return workViews.get(id);
};
const WORK_STATUS = {queued:'Getting ready', running:'Working', waiting:'Waiting for an update', input:'Your turn',
  paused:'Paused', pausing:'Pausing after the current step', completed:'Ready for review'};

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

function renderWorkRecents() {
  const nav = document.getElementById('work-recents');
  if (!nav) return;
  const rows = (state.browser_tasks || []).slice(0,8);
  nav.innerHTML = rows.length ? `<span class="work-nav-label">Recent</span>${rows.map(r => `<a href="#work/${encodeURIComponent(r.task_id)}" ${workId() === r.task_id ? 'aria-current="page"' : ''} title="${esc(r.prompt)}">${esc(r.prompt)}</a>`).join('')}` : '';
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

function workComposer(id, enabled) {
  const draft = workDraft(id), repos = state.workbench?.repositories || [];
  const selected = draft.repository || repos[0]?.id || '';
  return `<form id="work-compose" class="work-compose" data-task="${esc(id)}">
    <label class="sr-only" for="work-prompt">${id ? 'Message Raven' : 'Describe your task'}</label>
    <textarea id="work-prompt" name="prompt" rows="${id ? '2' : '3'}" maxlength="12000" required placeholder="${id ? 'Add context or ask Raven to continue…' : 'What would you like to get done?'}" ${draft.pending || draft.payload ? 'readonly' : ''}>${esc(draft.text)}</textarea>
    <div class="work-compose-bottom">${id ? '<span>Enter to send · Shift + Enter for a new line</span>' : `<label class="work-repository"><span class="sr-only">Repository</span><select id="work-repository" aria-label="Repository" ${draft.pending || draft.payload ? 'disabled' : ''}>${repos.map(r => `<option value="${esc(r.id)}" ${r.id === selected ? 'selected' : ''}>${esc(r.name)}</option>`).join('') || '<option>No repository connected</option>'}</select></label>`}
    <button class="work-send" type="submit" aria-label="${id ? 'Send message' : 'Start task'}" ${!enabled || draft.pending ? 'disabled' : ''}>${draft.pending ? '<span class="loading-dot"></span>' : icon('arrow')}</button></div>
    ${draft.error ? `<p class="work-error" role="alert">${esc(draft.error)} Your message is saved. Retry sends the same request.</p>` : ''}
  </form>`;
}

function workActivityLabel(a) {
  const input = a.detail?.input || {};
  return ({list_files:'Explored the repository', search_files:'Searched the code', read_file:'Read ' + (input.path || 'a file'),
    write_file:'Updated ' + (input.path || 'a file'), edit_file:'Updated ' + (input.path || 'a file'), run_command:'Ran a command', bridge_review_discovery:'Reviewed the investigation',
    bridge_add_node:'Asked for a decision', bridge_get_tree:'Read the latest decisions', bridge_search_context:'Searched connected context',
    bridge_get_record:'Read a source', finish_task:'Checked completion', present_result:'Presented the result', pause:'Paused by ' + (a.detail?.actor || 'a teammate'), resume:'Resumed by ' + (a.detail?.actor || 'a teammate'), wait_for_people:'Saved progress and waited'})[a.label] || a.label.replaceAll('_',' ');
}

function workBirdseye(tree, trace) {
  const nodes = flattenTask(tree.nodes).filter(n => !['duplicate','adopted'].includes(n.status));
  const blocked = nodes.filter(n => n.blocking), signed = nodes.filter(taskAuthorized);
  const people = [...new Set([...nodes.map(n => n.owner), ...(trace.notifications || []).map(n => n.person_name)].filter(Boolean))];
  const contacts = (trace.notifications || []).length;
  const investigation = (tree.discovery_reviews || []).findLast(r => r.summary && r.status === 'complete');
  return `<aside id="work-overview" class="work-overview ${workOverview ? 'is-open' : ''}" aria-label="Task overview">
    <div class="work-panel-title"><h2>At a glance</h2><a href="#runs/${encodeURIComponent(tree.task_id)}" title="Open the full task history">Full history ↗</a></div>
    <div class="work-counts"><div><strong>${nodes.length}</strong><span>decisions recorded</span></div><div><strong>${signed.length}</strong><span>authorized</span></div><div><strong>${blocked.length}</strong><span>waiting</span></div></div>
    <section><h3>${blocked.length ? 'Waiting on' : 'Decisions'}</h3>${nodes.length ? [...blocked, ...nodes.filter(n => !n.blocking)].map(n => `<details class="work-decision" data-work-detail="decision-${esc(n.node_id)}"><summary><span class="work-decision-state ${taskAuthorized(n) ? 'is-signed' : ''}">${esc(taskAuthorized(n) ? 'Authorized' : n.status === 'suggested' ? (n.blocking ? 'Required follow-up' : 'Suggested follow-up') : n.owner || 'Finding the right person')}</span><span>${esc(n.question)}</span></summary>${n.answer ? `<p>${esc(n.answer)}</p>` : '<p>No answer recorded yet.</p>'}<p class="work-muted">${esc(n.owner || 'Unassigned')}${n.signed_by ? ' · Signed by ' + esc(n.signed_by) : ''}</p><button class="button small" data-action="review" data-id="${esc(n.node_id)}">View decision</button></details>`).join('') : '<p class="work-muted">Raven is investigating. Decisions will appear here as they are recorded.</p>'}</section>
    ${investigation ? `<section><h3>What Raven found</h3><details class="work-discovery" data-work-detail="investigation"><summary>Investigation summary</summary><p>${esc(investigation.summary)}</p><small>Model observations, not authorization.</small></details></section>` : ''}
    <section><h3>People involved</h3>${people.length ? `<ul class="work-people">${people.map((p,i) => `<li>${avatar(p,i)}<span>${esc(p)}</span></li>`).join('')}</ul>` : '<p class="work-muted">Nobody has been asked yet.</p>'}${contacts ? `<a class="work-muted" href="#runs/${encodeURIComponent(tree.task_id)}">View contact history ↗</a>` : ''}</section>
    <p class="work-footnote">This view shows what Raven has recorded. Evidence needs sign-off before it authorizes work.</p>
  </aside>`;
}

function renderWorkbench() {
  if (view !== 'work' || !state.auth) return;
  const app = $('#app'), id = workId();
  const active = document.activeElement, focus = active?.id, selection = [active?.selectionStart, active?.selectionEnd];
  const existingInput = $('#work-prompt');
  if (existingInput && app.dataset.workId === id) workDraft(id).text = existingInput.value;
  const disclosures = new Set([...app.querySelectorAll('details[data-work-detail]')].filter(el => el.open).map(el => el.dataset.workDetail));
  const scroll = window.scrollY;
  const followLatest = !app.querySelector('.work-thread') ||
    (document.documentElement?.scrollHeight || 0) - scroll - (window.innerHeight || 0) < 140;
  app.dataset.workId = id;
  const allowed = !state.auth.enabled || ['admin','member'].includes(state.me?.role);
  if (!id) {
    const ready = state.workbench?.enabled && state.workbench?.ready && state.workbench?.repositories?.length;
    app.innerHTML = `<section class="work-home"><img src="/favicon.svg" alt="" width="38" height="38"><h1>What should we work on?</h1><p>Give Raven a task. It brings the context together, does the work, and asks people when judgment is needed.</p>${workComposer('', ready && allowed)}
      <div class="work-home-note">${!allowed ? 'Your account can view tasks. A workspace member can start one.' : ready ? 'Runs in a separate copy of your repository. You review the result.' : '<strong>Browser execution is not set up yet.</strong> An operator needs to connect a repository and configure inference. <a href="#connect">Connections & setup ↗</a>'}</div>
      <a class="work-alternative" href="#runs">View all workspace tasks ${icon('arrow')}</a></section>`;
  } else if (!workData || workData.task.task_id !== id) {
    app.innerHTML = `<div class="work-empty"><h1>${workError ? 'Could not open this task' : 'Opening your task…'}</h1>${workError ? `<p role="alert">${esc(workError)}</p><button class="button" data-work-action="refresh">Try again</button>` : '<span class="loading-dot"></span>'}</div>`;
  } else {
    const {task, tree, trace} = workData, running = ['queued','running','pausing'].includes(task.status);
    const incompleteReview = tree.review?.status === 'failed';
    const activities = task.activity.filter(a => ['tool', 'context', 'control'].includes(a.kind));
    const latest = activities.at(-1);
    const messages = task.conversation.filter(m => !m.text.startsWith('Raven task ID:') && !m.text.startsWith('Raven has new task information.'));
    if (!messages.length) messages.push({role:'user',text:task.prompt});
    app.innerHTML = `<header class="work-task-header"><div><a href="#work" class="work-back">New task</a><h1 title="${esc(task.prompt)}">${esc(task.prompt)}</h1><span class="work-task-repo">${esc(task.repository)}</span><span class="work-live-status" role="status">${running ? '<span class="loading-dot"></span>' : ''}${esc(WORK_STATUS[task.status] || task.status)}</span></div><div class="work-task-actions">${allowed ? `<button class="button small" data-work-action="${running ? 'pause' : 'resume'}" ${task.status === 'pausing' ? 'disabled' : ''}>${running ? 'Pause' : 'Continue'}</button>` : ''}<button class="button small" data-work-action="overview" aria-expanded="${workOverview}" aria-controls="work-overview">${workOverview && window.matchMedia?.('(max-width:900px)').matches ? 'Conversation' : 'Overview'}</button></div></header>
    ${workError ? `<p class="work-error" role="alert">${esc(workError)} Showing the last saved state.</p>` : ''}
    <div class="work-layout ${workOverview ? '' : 'overview-closed'}"><section class="work-thread" aria-label="Task conversation"><div class="work-messages">${messages.map(m => `<article class="work-message ${m.role}"><span class="work-speaker">${m.role === 'user' ? esc(m.actor || task.requester || 'Requester') : 'Raven'}</span><div>${workText(m.text)}</div></article>`).join('')}
    ${task.pending_messages.map(m => `<article class="work-message user"><span class="work-speaker">${esc(m.actor)} · queued</span><div>${esc(m.body)}</div></article>`).join('')}
    ${task.status === 'waiting' ? `<div class="work-waiting"><span class="status-dot"></span><p>${esc(task.summary || 'Waiting for a reply.')}<small>You can close this page. Raven continues when the task has an update.</small></p></div>` : ''}
    ${task.error ? `<p class="work-error" role="alert">${esc(task.error)}</p>` : ''}
    ${task.status === 'completed' ? `<article class="work-result"><h2>${incompleteReview ? 'Patch ready for manual review' : 'Ready for your review'}</h2>${incompleteReview ? '<p class="work-error">The automatic code review is incomplete. Owner approvals and test results do not replace reviewing the patch.</p>' : ''}<div class="work-result-text">${workText(task.summary)}</div><div class="work-result-actions"><a class="button" href="/api/workbench/${encodeURIComponent(id)}/patch">Download patch</a><a class="button" href="#runs/${encodeURIComponent(id)}">Decisions & review</a></div><small>The original repository is unchanged. No commit, push or deployment was made.</small></article>` : ''}
    ${activities.length ? `<details class="work-activity" data-work-detail="activity"><summary>${running ? 'Working · ' : ''}${esc(latest ? workActivityLabel(latest) : 'Activity')}<span>${activities.length} steps</span></summary><ol>${activities.map(a => `<li><details data-work-detail="action-${esc(a.id)}"><summary>${esc(workActivityLabel(a))}</summary><pre>${esc(JSON.stringify(a.detail,null,2))}</pre></details></li>`).join('')}</ol></details>` : '<p class="work-muted" role="status">Preparing a private repository copy…</p>'}
    </div><div class="work-reply">${allowed ? workComposer(id, task.status !== 'pausing') : '<p class="work-muted">You have view access to this conversation.</p>'}</div></section>${workBirdseye(tree,trace)}</div>`;
  }
  app.querySelectorAll('details[data-work-detail]').forEach(el => { el.open = disclosures.has(el.dataset.workDetail); });
  if (focus && app.querySelector('#' + CSS.escape(focus))) {
    const replacement = document.getElementById(focus); replacement.focus({preventScroll:true});
    if (replacement.setSelectionRange && selection[0] !== null) replacement.setSelectionRange(...selection);
  }
  window.scrollTo({top:id && followLatest && !workError ? document.documentElement?.scrollHeight || scroll : scroll, behavior:'instant'});
  renderWorkRecents();
}

document.addEventListener('input', event => {
  if (event.target.id === 'work-prompt') workDraft().text = event.target.value;
});
document.addEventListener('change', event => {
  if (event.target.id === 'work-repository') workDraft().repository = event.target.value;
});
document.addEventListener('keydown', event => {
  if (event.target.id === 'work-prompt' && event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault(); event.target.form.requestSubmit();
  }
});
document.addEventListener('submit', async event => {
  if (event.target.id !== 'work-compose') return;
  event.preventDefault();
  const form = event.target, id = form.dataset.task, draft = workDraft(id), startedOn = navigationVersion;
  if (draft.pending || !form.reportValidity() || (state.auth.enabled && !['admin','member'].includes(state.me?.role))) return;
  const text = draft.text.trim();
  if (!text) return;
  if (!draft.payload) draft.payload = id ? {text,request_key:draft.key} : {prompt:text,request_key:draft.key,repository:$('#work-repository').value};
  draft.pending = true; draft.error = ''; renderWorkbench();
  try {
    const result = await api(id ? `/api/workbench/${encodeURIComponent(id)}/messages` : '/api/workbench', draft.payload);
    workViews.delete(id);
    if (id === workId() && $('#work-prompt')) $('#work-prompt').value = '';
    if (!id && view === 'work' && !workId() && startedOn === navigationVersion) location.hash = 'work/' + encodeURIComponent(result.task_id);
    else if (id === workId() && startedOn === navigationVersion) await loadWorkbench();
    await refresh({quiet:true});
  } catch (error) { draft.error = error.message; }
  finally { draft.pending = false; if (view === 'work') renderWorkbench(); }
});
document.addEventListener('click', async event => {
  const button = event.target.closest('[data-work-action]');
  if (!button) return;
  const action = button.dataset.workAction, id = workId();
  if (action === 'overview') { workOverview = !workOverview; renderWorkbench(); if (window.matchMedia?.('(max-width:900px)').matches) window.scrollTo({top:0,behavior:'instant'}); return; }
  button.disabled = true;
  try {
    if (action !== 'refresh') await api(`/api/workbench/${encodeURIComponent(id)}/${action}`, {});
    await loadWorkbench();
  } catch (error) { workError = error.message; renderWorkbench(); }
  finally { button.disabled = false; }
});
