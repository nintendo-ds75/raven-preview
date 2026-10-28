'use strict';

// These reads use normal workspace authentication. Copying a task URL grants no access.
let taskDetail = null, taskError = '', taskTab = 'decisions', taskRequest = 0;
// What the task page last drew, so a poll that finds the same tree and
// trace does not rebuild it; observed_at is the read's own clock, not a change.
let taskDrawn = '', taskDrawnAt = 0;
const taskFingerprint = (tree, trace) => JSON.stringify({...tree, observed_at: ''}) + JSON.stringify(trace);
const taskViews = new Map();
const taskId = () => view === 'runs' ? location.hash.slice(1).split('/')[1] || '' : '';
const taskList = value => Array.isArray(value) ? value : [];
const taskText = value => typeof value === 'string' ? value : value === undefined ? '' : JSON.stringify(value, null, 2);
const taskExcerpt = (value, limit = 240) => { const text = Array.from(taskText(value)); return text.slice(0,limit).join('') + (text.length > limit ? '…' : ''); };
const flattenTask = nodes => taskList(nodes).flatMap(n => [n, ...flattenTask(n.children)]);
const taskSame = (a,b) => String(a || '').toLowerCase() === String(b || '').toLowerCase();
const taskTime = at => at && !Number.isNaN(Date.parse(at)) ? new Date(at).toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : taskText(at);
const taskSourceLabels = {record:'Source record',memory:'Earlier decision',human:'Recorded human answer',agent:'Agent proposal'};
const taskSourceLabel = source => Object.hasOwn(taskSourceLabels,source) ? taskSourceLabels[source] : taskText(source) || 'Recorded finding';
const taskAuthorized = n => !!n.authorized && !n.needs_review && !n.source_refresh_required;
const taskWaiting = n => taskList(n.required_signers).filter(name => !taskList(n.signatures).some(s => taskSame(s,name)));
// The routed owner cannot sign in yet (the people's tree says so; the agent's does not).
const taskNoAccount = n => !!(n.owner && n.owner_account && n.owner_account.state !== 'account');
const taskView = (id = taskId()) => {
  if (!taskViews.has(id)) taskViews.set(id, {tab:'decisions', briefOpen:false, limits:{}, disclosures:{}, note:'', notePending:false, noteError:'', inquiry:'', inquiryPending:false, inquiryError:'', inquiryResult:null, focus:null});
  return taskViews.get(id);
};

// Drafts and expanded rows stay in memory, scoped to a run, never in localStorage.
function rememberTaskView() {
  const app = $('#app'), id = app.dataset.taskId;
  if (!id) return;
  const saved = taskView(id);
  app.querySelectorAll('details[id]').forEach(el => { saved.disclosures[el.id] = el.open; });
  const note = app.querySelector('#note-text');
  if (note) saved.note = note.value;
  const inquiry = app.querySelector('#task-inquiry-text');
  if (inquiry) saved.inquiry = inquiry.value;
  const active = document.activeElement;
  saved.restoreFocus = app.contains(active) && !!active.id;
  if (saved.restoreFocus) saved.focus = {id:active.id, start:active.selectionStart, end:active.selectionEnd};
}

function restoreTaskView() {
  const saved = taskView(), app = $('#app');
  for (const [id,open] of Object.entries(saved.disclosures)) {
    const el = document.getElementById(id);
    if (el && app.contains(el)) el.open = open;
  }
  const note = app.querySelector('#note-text');
  if (note) note.value = saved.note;
  const inquiry = app.querySelector('#task-inquiry-text');
  if (inquiry) inquiry.value = saved.inquiry;
  const focus = saved.restoreFocus && saved.focus && document.getElementById(saved.focus.id);
  if (focus && app.contains(focus) && !$('#modal').open) {
    focus.focus({preventScroll:true});
    if (saved.focus.start != null && typeof focus.setSelectionRange === 'function') {
      try { focus.setSelectionRange(saved.focus.start,saved.focus.end); } catch {} // Non-text controls.
    }
  }
}

function taskMore(key, step = 12) {
  const saved = taskView();
  saved.limits[key] = (saved.limits[key] || step) + step;
  render();
}

function taskDetails(html, prefix) {
  let index = 0;
  return html.replace(/<details\b([^>]*)>/g, (match, attrs) => /\bid=/.test(attrs) ? match : `<details id="${esc(prefix)}-${index++}"${attrs}>`);
}

async function loadTask({quiet = false} = {}) {
  const id = taskId();
  if (!id || (quiet && $('#modal').open)) return;
  const request = ++taskRequest;
  try {
    const [tree,trace] = await Promise.all([
      api(`/api/tasks/${encodeURIComponent(id)}/tree`),
      api(`/api/tasks/${encodeURIComponent(id)}/trace`),
    ]);
    if (taskId() !== id || request !== taskRequest) return;
    taskDetail = {tree,trace}; taskError = '';
    const drawn = taskFingerprint(tree, trace);
    if (!(quiet && drawn === taskDrawn && Date.now() - taskDrawnAt < 60000)) { taskDrawn = drawn; taskDrawnAt = Date.now(); render(); }
    const focused = location.hash.slice(1).split('/')[2];
    if (!quiet && focused && flattenTask(tree.nodes).some(n => n.node_id === focused) && typeof review === 'function') await review(focused);
  } catch (error) {
    if (taskId() !== id || request !== taskRequest) return;
    if ([401,403,404].includes(error.status)) taskDetail = null;
    taskError = error.message;
    render();
  }
}

async function submitModalTaskNote(form) {
  if (!$('#modal').open || !$('#modal').contains(form)) return;
  const button = form.querySelector('[type="submit"]'), input = form.querySelector('[name="text"]');
  if (button.disabled || !form.reportValidity()) return;
  const errorEl = form.querySelector('#form-error'), text = input.value, version = modalVersion;
  const current = () => $('#modal').open && modalVersion === version && $('#modal').contains(form);
  button.disabled = true; errorEl.hidden = true;
  try {
    await api(`/api/tasks/${encodeURIComponent(form.dataset.id)}/notes`, {text, by:state.me?.name || 'Local operator'});
    if (current()) {
      if (input.value === text) input.value = '';
      notify('Note added. The agent can read it on the tree.');
    }
  } catch (error) {
    if (current()) { errorEl.textContent = error.message; errorEl.hidden = false; }
  } finally { if (current()) button.disabled = false; }
}

async function submitTaskNote(form) {
  if (form.closest('#modal')) return submitModalTaskNote(form);
  const id = form.dataset.id, saved = taskView(id);
  if (saved.notePending || !form.reportValidity()) return;
  const text = form.querySelector('[name="text"]').value;
  saved.note = text; saved.notePending = true; saved.noteError = '';
  form.querySelector('[type="submit"]').disabled = true;
  form.querySelector('#form-error').hidden = true;
  try {
    await api(`/api/tasks/${encodeURIComponent(id)}/notes`, {text, by:state.me?.name || 'Local operator'});
    const current = taskId() === id ? $('#app').querySelector('#note-text') : null;
    if (current) saved.note = current.value;
    if (saved.note === text) { saved.note = ''; if (current) current.value = ''; }
  } catch (error) { saved.noteError = error.message; }
  finally {
    saved.notePending = false;
    if (taskId() === id) { render(); if (!saved.noteError) await loadTask({quiet:true}); }
  }
}

// At most two names, then how many more; never an empty name.
const taskNames = names => { const unique = [...new Set(taskList(names).filter(Boolean))]; return unique.length > 2 ? `${unique[0]}, ${unique[1]} and ${unique.length - 2} more` : unique.join(', '); };

// The one sentence a question row carries, as {text, tone}: a person's name
// when someone is waited on, the state otherwise.
function taskStatusSentence(n) {
  if (n.source_refresh_required) return {text:'Needs fresh evidence', tone:'waiting'};
  if (n.needs_review) return {text:'Needs a second look', tone:'waiting'};
  if (n.status === 'duplicate') return {text:'Duplicate', tone:'neutral'};
  if (taskAuthorized(n)) {
    if (n.signoff === 'rule') return {text:'Covered by a rule', tone:'done'};
    const signers = taskNames(taskList(n.signatures).length ? n.signatures : [n.signed_by]);
    return {text: n.signoff === 'signed' && signers ? `Signed by ${signers}` : 'Answered', tone:'done'};
  }
  if (n.status === 'suggested') return {text:'Follow-up for the agent', tone:'neutral'};
  if (n.answer || taskList(n.signatures).length) return {text:'Waiting for sign-off', tone:'waiting'};
  return {text: n.owner ? `Waiting on ${n.owner}` : 'Needs a contact', tone:'waiting'};
}

// What the button on a row does: answer an open question, sign a recorded one, read a settled one.
const taskAction = n => taskAuthorized(n) || ['duplicate','suggested','adopted'].includes(n.status) ? 'View' : n.answer || taskList(n.signatures).length || n.needs_review || n.source_refresh_required ? 'Sign' : 'Answer';

function taskSources(n, prefix) {
  const sources = taskList(n.sources).filter(s => s && typeof s === 'object');
  const pins = taskList(n.related).filter(link => link && link.source_decision_id && link.source_version_id);
  if (!sources.length && !pins.length && !n.source && !n.source_id && !n.source_notice && !n.prediction) return '';
  return `<details class="task-evidence" id="${esc(prefix)}-evidence"><summary id="${esc(prefix)}-evidence-summary">Evidence & answer origin${sources.length ? ` · ${sources.length} sources` : ''}</summary>
    ${n.source || n.source_id ? `<p><strong>Recorded answer source:</strong> ${esc(taskSourceLabel(n.source))}${n.source_id ? ` · ${esc(n.source_id)}` : ''}${n.source_revision != null && n.source_revision !== '' ? ` · revision ${esc(n.source_revision)}` : ''}</p>` : ''}
    ${n.source === 'memory' && typeof n.source_id === 'string' && n.source_id ? `<button id="${esc(prefix)}-earlier-decision" class="button small" data-action="source" data-id="${esc(n.source_id)}">Earlier decision</button><p class="context">Opens that decision’s current record. Exact recorded versions below preserve the pinned relationship.</p>` : ''}
    ${pins.map((link,i) => `<details id="${esc(prefix)}-related-${i}"><summary>Recorded ${esc(link.kind || 'decision')} link · ${esc(link.source_decision_id)}</summary><dl class="task-facts"><dt>Source decision</dt><dd>${esc(link.source_decision_id)}</dd><dt>Source version</dt><dd>${esc(link.source_version_id)}</dd><dt>Dependent decision</dt><dd>${esc(link.decision_id || 'Not recorded')}</dd><dt>Dependent version</dt><dd>${esc(link.decision_version_id || 'Not recorded')}</dd></dl><pre>${esc(taskText(link))}</pre></details>`).join('')}
    ${n.source_notice ? `<p>${esc(n.source_notice)}</p>` : ''}${n.prediction ? `<p><strong>Unconfirmed suggestion:</strong> ${esc(n.prediction)}</p>` : ''}
    ${sources.map((s,i) => {
      const url = /^https?:\/\//i.test(s.url || '') ? s.url : '', label = s.ref || s.record_id || 'Recorded source';
      return `<div class="task-source"><p>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>` : esc(label)} · ${esc(s.role || 'Evidence')}<br><small>${s.current === true ? 'Current observed version' : s.current === false ? 'Historical or unavailable version' : 'Version freshness not recorded'}${s.stale ? ' · Needs review for reuse' : ''}</small></p><details id="${esc(prefix)}-source-${i}"><summary>Recorded source fields</summary><pre>${esc(taskText(s))}</pre></details></div>`;
    }).join('')}
    </details>`;
}

function taskEntries(trace = {}) {
  return taskList(trace.events).filter(e => e && typeof e === 'object').map(e => ({...e,type:'event'}))
    .concat(taskList(trace.notifications).filter(e => e && typeof e === 'object').map(e => ({...e,type:'notification',at:e.sent_at || e.created_at})))
    .sort((a,b) => String(a.at || '').localeCompare(String(b.at || '')) || Number(a.id || 0) - Number(b.id || 0));
}

function taskTimeline(trace, nodes, key = 'history', step = 25) {
  const entries = taskEntries(trace), shown = entries.slice(-(taskView().limits[key] || step));
  if (!entries.length) return '<p class="task-empty">No history recorded yet.</p>';
  return `${entries.length > shown.length ? `<p class="context">Latest ${shown.length} of ${entries.length} entries, in chronological order.</p><button id="task-more-${esc(key)}" class="button small" data-action="task-more" data-key="${esc(key)}" data-step="${step}">Show earlier ${Math.min(step,entries.length-shown.length)} entries</button>` : ''}<ol class="task-timeline">${shown.map((e,i) => {
    const node = nodes.find(n => n.node_id === e.decision_id), id = `${key}-${e.type}-${e.id ?? i}`;
    let title, body;
    if (e.type === 'notification') {
      title = `${e.state === 'sent' ? 'Message sent' : e.state === 'failed' ? 'Message failed' : 'Message ' + taskText(e.state)}${e.person_name ? ' to ' + e.person_name : ''}`;
      body = `<p>${esc(e.channel)} · ${esc(taskText(e.kind).replaceAll('_',' '))} · ${esc(e.attempts ?? 0)} delivery attempts</p>${e.last_error ? `<p class="task-warning">${esc(e.last_error)}</p>` : ''}<small>Delivery does not confirm that the person read it.</small>`;
    } else {
      title = Object.hasOwn(eventLabels,e.kind) ? eventLabels[e.kind] : taskText(e.kind).replaceAll('_',' ');
      const d = e.detail;
      if (typeof d === 'string') body = `<p>${esc(taskExcerpt(d))}</p>${Array.from(d).length > 240 ? `<details id="${esc(id)}"><summary id="${esc(id)}-summary">Full recorded text</summary><p>${esc(d)}</p></details>` : ''}`;
      else {
        const by = d && (d.actor_name || d.actor || d.by || d.answered_by || d.requester || d.owner);
        const text = d && (d.text || d.answer || d.reason || d.why || d.question || d.summary || d.query || d.prompt);
        body = `${by ? `<p><strong>${esc(taskText(by))}</strong></p>` : ''}${text ? `<p>${esc(taskExcerpt(text))}</p>` : ''}<details id="${esc(id)}"><summary id="${esc(id)}-summary">Recorded details</summary><pre>${esc(taskText(d))}</pre></details>`;
      }
    }
    return `<li><div class="timeline-heading"><strong>${esc(title)}</strong><time datetime="${esc(e.at)}">${esc(taskTime(e.at))}</time></div>${node ? `<button id="${esc(id)}-question" class="timeline-question" data-action="review" data-id="${esc(node.node_id)}">${esc(node.question)}</button>` : ''}${body}</li>`;
  }).join('')}</ol>`;
}

function taskDecision(n, prefix = 'question', primary = false) {
  const id = `${prefix}-${n.node_id}`, trace = taskDetail?.trace || {};
  const events = taskList(trace.events).filter(e => e && e.decision_id === n.node_id && ['owner_changed','ask_drafted','route_unknown','route_learned','ownership_invalidated'].includes(e.kind));
  const notifications = taskList(trace.notifications).filter(e => e && e.decision_id === n.node_id);
  const excerpt = n.answer ? taskExcerpt(n.answer, 140) : n.prediction ? 'Suggested: ' + taskExcerpt(n.prediction, 140) : '';
  return `<details class="task-question tree-node" id="${esc(id)}" data-node-id="${esc(n.node_id)}"><summary id="${esc(id)}-summary"><span class="task-question-title">${esc(n.question)}</span><span class="task-question-meta">${pill(taskStatusSentence(n).text, taskStatusSentence(n).tone)}${n.model_pending ? '<span class="task-reading">Reading context…</span>' : ''}</span>${excerpt ? `<span class="task-finding-excerpt">${esc(excerpt)}</span>` : ''}</summary><div class="task-question-body">
    ${n.answer ? `<p class="task-answer">${esc(n.answer)}</p>` : ''}${n.partial ? '<p class="context">This answer is partial.</p>' : ''}
    ${n.rationale ? `<p class="task-why">Why: ${esc(n.rationale)}</p>` : ''}
    ${n.needs_review || n.source_refresh_required ? `<p class="task-warning">${esc(n.review_reason || 'Review current evidence and applicability before using this answer.')}</p>` : ''}
    <button id="${esc(id)}-review" class="button small${primary ? ' primary' : ''}" data-action="review" data-id="${esc(n.node_id)}" aria-label="${taskAction(n)}: ${esc(n.question)}">${taskAction(n)}</button>
    <details class="task-details" id="${esc(id)}-details"><summary id="${esc(id)}-details-summary">Details</summary>
    ${n.depth ? `<p class="context">Level ${esc(n.depth)}${n.origin === 'human' ? ' · added by a person' : ''}</p>` : ''}
    ${n.next ? `<details id="${esc(id)}-next"><summary>Recorded next action</summary><p class="task-verbatim">${esc(n.next)}</p></details>` : ''}
    ${n.duplicate_of ? `<p>Repeats decision ${esc(n.duplicate_of)}. Its answer and authorization read through to that decision.</p>` : ''}
    <dl class="task-facts"><dt>Selected contact</dt><dd>${esc(n.owner || 'Not assigned')}${taskNoAccount(n) ? ' · not in Raven yet' : ''}</dd>${n.answered_by ? `<dt>${n.source === 'memory' ? 'Earlier answer respondent' : 'Recorded answer respondent'}</dt><dd>${esc(n.answered_by)}</dd>` : ''}${taskList(n.required_signers).length ? `<dt>Required sign-off</dt><dd>${esc(n.required_signers.join(', '))}</dd>` : ''}${taskList(n.signatures).length ? `<dt>Recorded signatures</dt><dd>${esc(n.signatures.join(', '))}</dd>` : ''}${taskList(n.historical_signatures).length ? `<dt>Historical sign-off</dt><dd>${esc(n.historical_signatures.join(', '))}. Current applicability needs review.</dd>` : ''}${n.path ? `<dt>Recorded path</dt><dd>${esc(n.path)}</dd>` : ''}</dl>
    ${taskSources(n,id)}${taskDetails(workItemContext(n),id+'-work-item')}
    <details class="task-contact" id="${esc(id)}-contact"><summary id="${esc(id)}-contact-summary">Contact, routing and referrals</summary>${n.routing_reason ? `<p><strong>Routing reason:</strong> ${esc(n.routing_reason)}</p>` : ''}${n.owner_evidence ? `<p><strong>Routing evidence:</strong> ${esc(taskText(n.owner_evidence))}</p><p class="context">This evidence can predate a reassignment. Events below show the recorded assignment and referral trail.</p>` : ''}${taskTimeline({events,notifications},[],id+'-contact',8)}</details>
    ${taskList(n.depends_on).length ? `<p class="context">Depends on: ${esc(n.depends_on.join(', '))}</p>` : ''}${n.evidence ? `<p class="task-checked">What Raven checked: ${esc(taskText(n.evidence))}</p>` : ''}
    </details>
  </div></details>`;
}

function taskQuestions(nodes, key, emptyText) {
  const shown = nodes.slice(0,taskView().limits[key] || 12);
  return nodes.length ? shown.map((n,i) => taskDecision(n,key,key === 'finding' && i === 0)).join('') + (nodes.length > shown.length ? `<button id="task-more-${esc(key)}" class="button small task-more" data-action="task-more" data-key="${esc(key)}" data-step="12">Show ${Math.min(12,nodes.length-shown.length)} more questions (${nodes.length-shown.length} remaining)</button>` : '') : `<p class="task-empty">${esc(emptyText)}</p>`;
}

function taskScopes(scopes) {
  const shown = scopes.slice(0,taskView().limits.scopes || 12);
  return shown.map((s,i) => {
    const id = 'task-scope-' + (s.request_key || i);
    return `<details class="task-question task-scope" id="${esc(id)}"><summary id="${esc(id)}-summary"><span class="task-question-title">${esc(s.question || 'Confirm task scope')}</span><span class="task-question-meta">Blocked · scope needed</span></summary><div class="task-question-body"><p>Confirm missing task facts before routing this question. No question was sent.</p>${taskList(s.scope_clarifications).map(c => `<p>${taskList(c.missing_keys).length ? `<strong>Missing facts:</strong> ${esc(c.missing_keys.join(', '))}. ` : ''}${taskList(c.missing_source_namespaces).length ? `<strong>Missing source context:</strong> ${esc(c.missing_source_namespaces.join(', '))}. ` : ''}${c.prior_contact ? `Earlier contact: ${esc(c.prior_contact)} (historical context only).` : ''}</p>`).join('')}<p class="context">Historical scope values are not confirmed facts for this task.</p><details id="${esc(id)}-record"><summary>Recorded scope request</summary><pre>${esc(taskText(s))}</pre></details></div></details>`;
  }).join('') + (scopes.length > shown.length ? '<button id="task-more-scopes" class="button small" data-action="task-more" data-key="scopes" data-step="12">Show more scope requests</button>' : '');
}

function taskBriefDecision() {
  const me = state.me?.name || '', mine = name => !!me && taskSame(name,me);
  const nodes = taskDetail ? flattenTask(taskDetail.tree.nodes).filter(n => !['duplicate','adopted','suggested'].includes(n.status)) : [];
  const owed = n => n.blocking && [n.owner,...taskList(n.required_signers)].some(x => mine(x) && !taskList(n.signatures).some(mine));
  return (nodes.find(owed) || nodes.find(n => mine(n.answered_by) || taskList(n.signatures).some(mine)) || nodes.find(n => mine(n.owner)))?.node_id || '';
}

function taskReview(review, nodes) {
  return `<section class="task-panel" id="task-review"><h2 tabindex="-1">Code review</h2>${pill(review.status)}${taskList(review.follows).map(r => `<article class="task-review-item"><div>${pill(r.verdict)} <strong>${esc(nodes.find(n => n.node_id === r.node_id)?.question || r.node_id)}</strong></div><p>${esc(r.why || r.reason || '')}</p><details class="review-requirements" id="requirements-${esc(r.node_id)}"><summary>Inspect requirements (${taskList(r.requirements).length})</summary>${taskList(r.requirements).map((q,i) => `<details id="requirement-${esc(r.node_id)}-${i}"><summary>${esc(q.state || q.status || q.verdict || '')} · ${esc(q.needs || q.requirement || q.text || '')}</summary><pre>${esc(taskText(q))}</pre></details>`).join('')}</details></article>`).join('')}<details id="task-review-record"><summary>Full review record</summary><pre>${esc(taskText(review))}</pre></details></section>`;
}

function taskNotes(t, canWrite) {
  const saved = taskView(), notes = taskList(t.notes), shown = notes.slice(-(saved.limits.notes || 12));
  return `<section class="task-panel" id="task-notes"><h2>Notes</h2>${shown.map(n => `<article class="task-note"><div><strong>${esc(n.by || 'Unattributed note')}</strong><time>${esc(taskTime(n.at))}</time></div><p>${esc(n.text)}</p></article>`).join('') || '<p class="task-empty">No notes yet.</p>'}${notes.length > shown.length ? '<button id="task-more-notes" class="button small" data-action="task-more" data-key="notes" data-step="12">Show earlier notes</button>' : ''}${canWrite ? `<div id="task-note-compose"><form id="note-form" data-id="${esc(t.task_id)}"><label for="note-text">Note for the agent</label><textarea id="note-text" name="text" maxlength="4000" required placeholder="A constraint, a person to ask, a link."></textarea><p class="error" id="form-error" role="alert" ${saved.noteError ? '' : 'hidden'}>${esc(saved.noteError)}</p><button id="task-note-submit" class="button" type="submit" ${saved.notePending ? 'disabled' : ''}>${saved.notePending ? 'Adding note…' : 'Add note'}</button></form></div>` : ''}</section>`;
}

function taskInquiry(id) {
  const saved = taskView(id), result = saved.inquiryResult;
  return `<details class="task-panel task-inquiry" id="task-inquiry"><summary id="task-inquiry-summary">Ask Raven about this task</summary><form id="task-inquiry-form" data-id="${esc(id)}"><textarea id="task-inquiry-text" name="question" rows="2" maxlength="2000" required aria-label="Ask Raven about this task" placeholder="What is still waiting?">${esc(saved.inquiry)}</textarea><button id="task-inquiry-submit" class="button" type="submit" aria-disabled="${saved.inquiryPending ? 'true' : 'false'}">${saved.inquiryPending ? 'Reading…' : 'Ask'}</button><p class="error" role="alert" ${saved.inquiryError ? '' : 'hidden'}>${esc(saved.inquiryError)}</p></form><div class="task-inquiry-result" role="status" aria-live="polite">${saved.inquiryPending ? '<p>Reading task context…</p>' : ''}${result ? `<p class="task-verbatim">${esc(result.answer)}</p><small>${esc(result.notice || 'Clarification does not authorize work.')}</small>${inquiryCitations(result, 'task-inquiry-citations')}${taskList(result.evidence).length ? `<details id="task-inquiry-evidence"><summary>Evidence used</summary>${taskList(result.evidence).map(e => `<h4>${esc(e.title || e.id)}</h4><p class="task-verbatim">${esc(e.text)}</p>`).join('')}</details>` : ''}` : ''}</div></details>`;
}

async function submitTaskInquiry(form) {
  const id = form.dataset.id;
  if (!id || taskId() !== id || !form.isConnected || !form.reportValidity()) return;
  const saved = taskView(id);
  if (saved.inquiryPending) return;
  const question = form.querySelector('[name="question"]').value.trim();
  if (!question) return;
  saved.inquiry = question; saved.inquiryPending = true; saved.inquiryError = ''; saved.inquiryResult = null;
  form.querySelector('[type="submit"]').ariaDisabled = 'true';
  render();
  const wait = typeof AbortController === 'function' ? new AbortController() : null;
  const cap = setTimeout(() => wait && wait.abort(), typeof MODEL_WAIT_MS === 'number' ? MODEL_WAIT_MS : 45000);
  try {
    saved.inquiryResult = await api(`/api/tasks/${encodeURIComponent(id)}/inquiry`, {question}, {signal: wait && wait.signal});
  } catch (error) { saved.inquiryError = typeof modelWaitMessage === 'function' ? modelWaitMessage(error) : error.message; saved.inquiryResult = null; }
  finally {
    clearTimeout(cap);
    saved.inquiryPending = false;
    if (taskId() === id) render();
  }
}

// Whether a goal is long enough to clamp to three lines and offer 'Show more'.
const taskGoalLong = text => Array.from(text).length > 240 || (text.match(/\n/g) || []).length >= 3;

// The one sentence the task page carries, as {text, tone, line}: the people a
// task waits on (saying who has not joined yet), else what state it is in.
// People come before the lifecycle, as on the Tasks row (runStatusSentence):
// a run the agent reported complete can still hold a question a person owes.
function taskStatus(t, nodes, scopes, reading) {
  const blockers = nodes.filter(n => n.blocking);
  const doubted = nodes.some(n => n.needs_review || n.source_refresh_required);
  const unjoined = new Set(blockers.filter(taskNoAccount).map(n => n.owner));
  const me = state.me?.name || '', mine = name => !!me && state.auth?.enabled && taskSame(name, me);
  const waiting = [...new Set(blockers.flatMap(n => taskWaiting(n).concat(n.owner && !taskList(n.signatures).some(s => taskSame(s,n.owner)) ? [n.owner] : [])))].filter(Boolean);
  const names = taskNames(waiting.map(name => mine(name) ? 'you' : unjoined.has(name) ? `${name} · not in Raven yet` : name));
  if (scopes.length) return {text:'Needs task facts', tone:'waiting'};
  if (doubted) return {text:'Needs a second look', tone:'waiting'};
  if (blockers.length) return {text: names ? `Waiting on ${names}` : 'Needs a contact', tone:'waiting'};
  const lifecycle = runStatusLabel(t.status);
  if (lifecycle !== 'In progress') return {text: lifecycle, tone: lifecycle === 'Done' ? 'done' : 'neutral', line: lifecycle === 'Failed' ? 'See History for the failure.' : ''};
  // A stale code review is a line under a finished task's pill, not a person owed; it leads only while the task runs.
  if (t.needs_review || t.review?.status === 'stale') return {text:'Needs a second look', tone:'waiting'};
  if (!nodes.length && reading) return {text:'Reading context…', tone:'neutral'};
  return {text:'In progress', tone:'neutral'};
}

function taskOverview() {
  if (!state.auth) return '<div class="empty" role="status">Loading workspace access…</div>';
  if (!taskDetail || taskDetail.tree.task_id !== taskId()) return `<a class="task-back" href="#runs">← Tasks</a>${taskError ? `<div class="empty" role="alert"><h2>Could not open this task</h2><p>${esc(taskError)}</p><button class="button" data-action="task-refresh">Try again</button></div>` : '<div class="empty" role="status">Loading task history…</div>'}`;
  const {tree:t,trace} = taskDetail, saved = taskView(), all = flattenTask(t.nodes), nodes = all.filter(n => n.status !== 'adopted');
  const scopes = taskList(t.scope_clarifications), canWrite = !state.auth.enabled || ['admin','member'].includes(state.me?.role);
  const reading = !!t.model_pending || nodes.some(n => n.model_pending);
  const status = taskStatus(t, nodes, scopes, reading);
  const uncertain = taskList(t.review?.follows).filter(r => r.verdict !== 'follows').length;
  // Only when a waited-on owner cannot sign in yet: who, and what an administrator can do.
  const unjoined = nodes.filter(n => n.blocking && taskNoAccount(n) && n.owner);
  const first = unjoined[0], unjoinedNames = [...new Set(unjoined.map(n => n.owner))];
  const account = first ? `<p class="task-unjoined">${esc(unjoinedNames.length > 1 ? `${unjoinedNames.join(' and ')} have not joined yet, so nobody can sign this.` : `${first.owner} has not joined yet, so nobody can sign this.`)}${state.me?.role === 'admin' ? `<span class="task-status-actions"><button class="button small" data-action="invite-owner" data-person="${esc(first.owner_account.person_id || '')}" data-email="${esc(first.owner_account.email || '')}">Invite ${esc(first.owner.split(/\s+/)[0])}</button><button class="button small" data-action="review" data-id="${esc(first.node_id)}">Hand on</button></span>` : ''}</p>` : '';
  const warnings = `${t.review?.status === 'running' || uncertain ? `<p class="task-warning">${t.review?.status === 'running' ? 'The code review is still running.' : `${uncertain} decisions need inspection after the model review.`}<button class="button small" data-action="task-review">Open code review</button></p>` : ''}${t.review?.status === 'stale' ? '<p class="task-warning">The code review needs refreshing for the current questions and answers. Submit the current diff again.</p>' : ''}`;
  const summary = `<section class="task-summary" aria-label="Task status"><p class="task-status-line" role="status">${pill(status.text, status.tone)}${status.line ? `<span class="muted">${esc(status.line)}</span>` : ''}</p>${account}${warnings}</section>`;
  const facts = Object.entries(t.facts || {}), workItem = taskDetails(workItemContext(t),'task-work-item');
  const context = facts.length || workItem || t.verdict ? `<details class="task-panel task-context" id="task-context"><summary id="task-context-summary">Details</summary><dl class="task-facts"><dt>Requested by</dt><dd>${esc(t.requester || 'Not recorded')}</dd><dt>Repository</dt><dd>${esc(t.repo)}</dd>${facts.map(([k,v]) => `<dt>${esc(k)}</dt><dd>${esc(taskText(v))}</dd>`).join('')}</dl>${workItem}${t.verdict ? `<p id="task-kickoff"><strong>Kickoff verdict · ${esc(t.verdict)}</strong>${t.verdict_why ? ` ${esc(t.verdict_why)}` : ''}</p>` : ''}</details>` : '';
  const findings = [...nodes].sort((a,b) => Number(!!(b.blocking || b.needs_review || b.source_refresh_required)) - Number(!!(a.blocking || a.needs_review || a.source_refresh_required)));
  const decisions = `<div class="task-overview">${scopes.length ? `<section class="task-panel"><h2>Scope needed · ${scopes.length}</h2>${taskScopes(scopes)}</section>` : ''}<section class="task-panel" id="task-waiting"><h2>Waiting</h2>${taskQuestions(findings.filter(n => !taskAuthorized(n)),'finding',reading ? 'Reading context…' : 'No open questions.')}</section><section class="task-panel" id="task-decided"><h2>Decided</h2>${taskQuestions(findings.filter(taskAuthorized),'resolved','Nothing decided yet.')}</section>${t.review ? taskReview(t.review,nodes) : ''}${taskNotes(t,canWrite)}${state.model?.investigation === false ? '' : taskInquiry(t.task_id)}${context}</div>`;
  const history = `<section class="task-panel"><h2>What happened</h2>${taskTimeline(trace,all)}</section>`;
  const run = taskList(state.runs).find(r => r.id === t.task_id);
  const meta = [t.requester ? `Requested by ${t.requester}` : 'Requester not recorded', t.repo, run?.agent].filter(Boolean).map(esc).join(' · ');
  // The goal is the brief; when the agent gave none, the title already says it.
  const goal = t.goal && t.goal.trim() !== String(t.title || '').trim() ? t.goal : '', long = taskGoalLong(goal);
  const more = [
    `<button id="task-share" class="button text" data-action="task-share">Copy link</button>`,
    state.auth.enabled && state.me?.id && state.me.role !== 'viewer' && state.settings?.brief_mode !== 'off' ? '<button class="button text" data-action="task-brief">Open my personal page</button>' : '',
    (state.browser_tasks || []).some(r => r.task_id === t.task_id) ? `<a href="#work/${encodeURIComponent(t.task_id)}">Open conversation</a>` : '',
    taskList(state.executions).some(e => e.run_id === t.task_id) ? `<button class="button text" data-action="execution-details" data-id="${esc(t.task_id)}">Execution output and files</button>` : '',
  ].filter(Boolean);
  return `<div class="task-heading"><div class="task-heading-row"><a class="task-back" href="#runs">← Tasks</a><details class="more-menu" id="task-more"><summary aria-label="More actions">More</summary><div class="more-menu-items">${more.join('')}</div></details></div><h1>${esc(taskDisplayLabel(t))}</h1>${goal ? `<p class="task-goal${long && !saved.briefOpen ? ' is-clamped' : ''}" id="task-brief">${esc(goal)}</p>` : ''}${long ? `<button id="task-brief-summary" class="button text task-brief-toggle" data-action="task-brief-toggle" aria-expanded="${saved.briefOpen ? 'true' : 'false'}" aria-controls="task-brief">${saved.briefOpen ? 'Show less' : 'Show more'}</button>` : ''}<p class="task-meta">${meta}</p></div>
    ${taskError ? `<p class="task-warning" role="alert">Refresh failed: ${esc(taskError)}. Showing the last successful read.</p>` : ''}${summary}
    <nav class="task-tabs" aria-label="Task sections">${[['decisions',`Decisions (${nodes.length})`],['history','History']].map(([key,label]) => `<button id="task-tab-${key}" class="tab ${(taskTab === 'history') === (key === 'history') ? 'active' : ''}" data-action="task-tab" data-tab="${key}" aria-pressed="${(taskTab === 'history') === (key === 'history')}">${label}</button>`).join('')}</nav>
    ${taskTab === 'history' ? history : decisions}`;
}
