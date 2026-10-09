'use strict';

// These reads use normal workspace authentication. Copying a task URL grants no access.
let taskDetail = null, taskError = '', taskTab = 'overview', taskRequest = 0;
const taskViews = new Map();
const taskId = () => view === 'runs' ? location.hash.slice(1).split('/')[1] || '' : '';
const taskList = value => Array.isArray(value) ? value : [];
const taskText = value => typeof value === 'string' ? value : value === undefined ? '' : JSON.stringify(value, null, 2);
const taskExcerpt = value => { const text = Array.from(taskText(value)); return text.slice(0,240).join('') + (text.length > 240 ? '…' : ''); };
const flattenTask = nodes => taskList(nodes).flatMap(n => [n, ...flattenTask(n.children)]);
const taskSame = (a,b) => String(a || '').toLowerCase() === String(b || '').toLowerCase();
const taskTime = at => at && !Number.isNaN(Date.parse(at)) ? new Date(at).toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : taskText(at);
const taskSourceLabels = {record:'Source record',memory:'Earlier decision',human:'Recorded human answer',agent:'Agent proposal'};
const taskSourceLabel = source => Object.hasOwn(taskSourceLabels,source) ? taskSourceLabels[source] : taskText(source) || 'Recorded finding';
const taskAuthorized = n => !!n.authorized && !n.needs_review && !n.source_refresh_required;
const taskWaiting = n => taskList(n.required_signers).filter(name => !taskList(n.signatures).some(s => taskSame(s,name)));
const taskView = (id = taskId()) => {
  if (!taskViews.has(id)) taskViews.set(id, {tab:'overview', limits:{}, disclosures:{}, note:'', notePending:false, noteError:'', focus:null});
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
    render();
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

function taskNodeStatus(n) {
  if (n.source_refresh_required) return 'Evidence refresh needed';
  if (n.needs_review) return 'Review needed';
  if (n.status === 'duplicate') return 'Duplicate question';
  if (taskAuthorized(n)) return n.signoff === 'rule' ? 'Standing rule' : n.signoff === 'signed' ? 'Signed answer' : 'Authorized answer';
  if (n.status === 'suggested') return n.followup_required ? 'Required follow-up' : 'Suggested follow-up';
  if (n.blocking) return !n.owner ? 'Needs a contact' : n.answer || taskList(n.signatures).length ? 'Waiting for sign-off' : 'Waiting for an answer';
  return n.answer ? 'Answer recorded' : 'Context only';
}

function taskApproval(n) {
  if (n.needs_review || n.source_refresh_required) return 'Approval needs review';
  if (!taskAuthorized(n)) return 'Not authorized';
  return n.signoff === 'rule' ? 'Authorized by standing rule' : n.signoff === 'signed' ? 'Human sign-off recorded' : 'Authorized';
}

function taskAuthority(n) {
  if (n.needs_review || n.source_refresh_required) return 'Earlier sign-off does not authorize use while this answer needs review.';
  if (taskAuthorized(n) && n.signoff === 'rule') return 'Authorized by an enabled standing rule. No fresh human signature is implied.';
  if (taskAuthorized(n) && n.signoff === 'signed') return `Signed by ${taskList(n.signatures).join(', ') || n.signed_by || 'the recorded signer'}.`;
  if (taskAuthorized(n)) return 'Authorized in the recorded task state.';
  const waiting = taskWaiting(n);
  return waiting.length ? 'Still needs ' + waiting.join(', ') + '.' : 'Not authorized for use.';
}

function taskSources(n, prefix) {
  const sources = taskList(n.sources).filter(s => s && typeof s === 'object');
  const pins = taskList(n.related).filter(link => link && link.source_decision_id && link.source_version_id);
  if (!sources.length && !pins.length && !n.source && !n.source_id && !n.source_notice && !n.evidence && !n.prediction) return '';
  return `<details class="task-evidence" id="${esc(prefix)}-evidence"><summary id="${esc(prefix)}-evidence-summary">Evidence & answer origin${sources.length ? ` · ${sources.length} sources` : ''}</summary>
    ${n.source || n.source_id ? `<p><strong>Recorded answer source:</strong> ${esc(taskSourceLabel(n.source))}${n.source_id ? ` · ${esc(n.source_id)}` : ''}${n.source_revision != null && n.source_revision !== '' ? ` · revision ${esc(n.source_revision)}` : ''}</p>` : ''}
    ${n.source === 'memory' && typeof n.source_id === 'string' && n.source_id ? `<button id="${esc(prefix)}-earlier-decision" class="button small" data-action="source" data-id="${esc(n.source_id)}">Earlier decision</button><p class="context">Opens that decision’s current record. Exact recorded versions below preserve the pinned relationship.</p>` : ''}
    ${pins.map((link,i) => `<details id="${esc(prefix)}-related-${i}"><summary>Recorded ${esc(link.kind || 'decision')} link · ${esc(link.source_decision_id)}</summary><dl class="task-facts"><dt>Source decision</dt><dd>${esc(link.source_decision_id)}</dd><dt>Source version</dt><dd>${esc(link.source_version_id)}</dd><dt>Dependent decision</dt><dd>${esc(link.decision_id || 'Not recorded')}</dd><dt>Dependent version</dt><dd>${esc(link.decision_version_id || 'Not recorded')}</dd></dl><pre>${esc(taskText(link))}</pre></details>`).join('')}
    ${n.source_notice ? `<p>${esc(n.source_notice)}</p>` : ''}${n.evidence ? `<p class="task-verbatim">${esc(taskText(n.evidence))}</p>` : ''}${n.prediction ? `<p><strong>Unconfirmed suggestion:</strong> ${esc(n.prediction)}</p>` : ''}
    ${sources.map((s,i) => {
      const url = /^https?:\/\//i.test(s.url || '') ? s.url : '', label = s.ref || s.record_id || 'Recorded source';
      return `<div class="task-source"><p>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>` : esc(label)} · ${esc(s.role || 'Evidence')}<br><small>${s.current === true ? 'Current observed version' : s.current === false ? 'Historical or unavailable version' : 'Version freshness not recorded'}${s.stale ? ' · Needs review for reuse' : ''}</small></p><details id="${esc(prefix)}-source-${i}"><summary>Recorded source fields</summary><pre>${esc(taskText(s))}</pre></details></div>`;
    }).join('')}
    <p class="context">${n.source ? `Source code: ${esc(taskText(n.source))}. ` : ''}Answer origin does not imply a new interaction in this run; see History. Recorded versions are observations, not live verification. Source authors and statuses do not grant approval. Open the decision for full source review and approval controls.</p></details>`;
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

function taskDecision(n, prefix = 'question') {
  const id = `${prefix}-${n.node_id}`, trace = taskDetail?.trace || {};
  const events = taskList(trace.events).filter(e => e && e.decision_id === n.node_id && ['owner_changed','ask_drafted','route_unknown','route_learned','ownership_invalidated'].includes(e.kind));
  const notifications = taskList(trace.notifications).filter(e => e && e.decision_id === n.node_id);
  const finding = n.answer || n.prediction || n.evidence;
  const excerpt = finding ? `${taskSourceLabel(n.source)}${!n.answer && n.prediction ? ' · unconfirmed suggestion' : ''}: ${taskExcerpt(finding)}` : 'No answer or finding recorded yet.';
  return `<details class="task-question tree-node" id="${esc(id)}" data-node-id="${esc(n.node_id)}"><summary id="${esc(id)}-summary"><span class="task-question-title">${esc(n.question)}</span><span class="task-question-meta"><span class="task-status">${esc(taskApproval(n))}</span>${n.blocking || ['duplicate','suggested'].includes(n.status) ? `<span>${esc(taskNodeStatus(n))}</span>` : ''}<span>${esc(n.owner || 'No selected contact')}</span>${n.model_pending ? '<span class="task-reading">Reading context…</span>' : ''}</span><span class="task-finding-excerpt">${esc(excerpt)}</span></summary><div class="task-question-body">
    ${n.depth ? `<p class="context">Level ${esc(n.depth)}${n.origin === 'human' ? ' · added by a person' : ''}</p>` : ''}
    ${n.answer ? `<p class="task-answer">${esc(n.answer)}</p>` : '<p class="context">No answer recorded yet.</p>'}<p class="task-authority">${esc(taskAuthority(n))}${n.partial ? ' This answer is partial.' : ''}</p>
    ${n.needs_review || n.source_refresh_required ? `<p class="task-warning">${esc(n.review_reason || 'Review current evidence and applicability before using this answer.')}</p>` : ''}
    ${n.rationale ? `<p><strong>Rationale:</strong> ${esc(n.rationale)}</p>` : ''}${n.next ? `<details id="${esc(id)}-next"><summary>Recorded next action</summary><p class="task-verbatim">${esc(n.next)}</p></details>` : ''}
    ${n.duplicate_of ? `<p>Repeats decision ${esc(n.duplicate_of)}. Its answer and authorization read through to that decision.</p>` : ''}
    <dl class="task-facts"><dt>Selected contact</dt><dd>${esc(n.owner || 'Not assigned')}</dd>${n.answered_by ? `<dt>${n.source === 'memory' ? 'Earlier answer respondent' : 'Recorded answer respondent'}</dt><dd>${esc(n.answered_by)}</dd>` : ''}${taskList(n.required_signers).length ? `<dt>Required sign-off</dt><dd>${esc(n.required_signers.join(', '))}</dd>` : ''}${taskList(n.signatures).length ? `<dt>Recorded signatures</dt><dd>${esc(n.signatures.join(', '))}</dd>` : ''}${taskList(n.historical_signatures).length ? `<dt>Historical sign-off</dt><dd>${esc(n.historical_signatures.join(', '))}. Current applicability needs review.</dd>` : ''}${n.path ? `<dt>Recorded path</dt><dd>${esc(n.path)}</dd>` : ''}</dl>
    ${taskSources(n,id)}${taskDetails(workItemContext(n),id+'-work-item')}
    <details class="task-contact" id="${esc(id)}-contact"><summary id="${esc(id)}-contact-summary">Contact, routing & referrals</summary>${n.routing_reason ? `<p><strong>Recorded routing reason:</strong> ${esc(n.routing_reason)}</p>` : ''}${n.owner_evidence ? `<p><strong>Recorded routing evidence:</strong> ${esc(taskText(n.owner_evidence))}</p><p class="context">This evidence can predate a reassignment. Events below show the recorded assignment and referral trail.</p>` : ''}${taskTimeline({events,notifications},[],id+'-contact',8)}</details>
    ${taskList(n.depends_on).length ? `<p class="context">Depends on: ${esc(n.depends_on.join(', '))}</p>` : ''}<button id="${esc(id)}-review" class="button small" data-action="review" data-id="${esc(n.node_id)}">Open decision ${icon('arrow')}</button>
  </div></details>`;
}

function taskQuestions(nodes, key, emptyText) {
  const shown = nodes.slice(0,taskView().limits[key] || 12);
  return nodes.length ? shown.map(n => taskDecision(n,key)).join('') + (nodes.length > shown.length ? `<button id="task-more-${esc(key)}" class="button small task-more" data-action="task-more" data-key="${esc(key)}" data-step="12">Show ${Math.min(12,nodes.length-shown.length)} more questions (${nodes.length-shown.length} remaining)</button>` : '') : `<p class="task-empty">${esc(emptyText)}</p>`;
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
  if (!review) return '<section class="task-panel"><h2>No code review yet</h2><p>A coding agent can submit its diff for review. Owner approval and code review are separate.</p></section>';
  return `<section class="task-panel"><h2>Code review against decisions</h2>${pill(review.status)}${review.seconds != null ? `<span class="context"> ${esc(review.seconds)} seconds</span>` : ''}<p class="context">This model review covers the recorded decisions. It does not replace tests or verify the whole change.</p>${taskList(review.follows).map(r => `<article class="task-review-item"><div>${pill(r.verdict)} <strong>${esc(nodes.find(n => n.node_id === r.node_id)?.question || r.node_id)}</strong></div><p>${esc(r.why || r.reason || '')}</p><details class="review-requirements" id="requirements-${esc(r.node_id)}"><summary>Inspect requirements (${taskList(r.requirements).length})</summary>${taskList(r.requirements).map((q,i) => `<details id="requirement-${esc(r.node_id)}-${i}"><summary>${esc(q.state || q.status || q.verdict || '')} · ${esc(q.needs || q.requirement || q.text || '')}</summary><pre>${esc(taskText(q))}</pre></details>`).join('')}</details></article>`).join('')}<details id="task-review-record"><summary>Full review record</summary><pre>${esc(taskText(review))}</pre></details></section>`;
}

function taskNotes(t, canWrite) {
  const saved = taskView(), notes = taskList(t.notes), shown = notes.slice(-(saved.limits.notes || 12));
  return `<section class="task-panel"><h2>Context & discussion</h2><p class="context">Notes are visible when the agent reads this task. Use a decision’s follow-up action for a question that must block completion.</p>${shown.map(n => `<article class="task-note"><div><strong>${esc(n.by || 'Unattributed note')}</strong><time>${esc(taskTime(n.at))}</time></div><p>${esc(n.text)}</p></article>`).join('') || '<p class="context">No notes yet.</p>'}${notes.length > shown.length ? '<button id="task-more-notes" class="button small" data-action="task-more" data-key="notes" data-step="12">Show earlier notes</button>' : ''}${canWrite ? `<details id="task-note-compose"><summary id="task-note-compose-summary">Add context for the agent</summary><form id="note-form" data-id="${esc(t.task_id)}"><label for="note-text">A note the agent reads on its tree</label><textarea id="note-text" name="text" maxlength="4000" required></textarea><p class="error" id="form-error" role="alert" ${saved.noteError ? '' : 'hidden'}>${esc(saved.noteError)}</p><button id="task-note-submit" class="button primary" type="submit" ${saved.notePending ? 'disabled' : ''}>${saved.notePending ? 'Adding note…' : 'Add note'}</button></form></details>` : '<p class="context">You have read-only access to this workspace.</p>'}</section>`;
}

function taskOverview() {
  if (!state.auth) return '<div class="empty" role="status">Loading workspace access…</div>';
  if (!taskDetail || taskDetail.tree.task_id !== taskId()) return `<a class="task-back" href="#runs">← All tasks</a>${taskError ? `<div class="empty" role="alert"><h2>Could not open this task</h2><p>${esc(taskError)}</p><button class="button" data-action="task-refresh">Try again</button></div>` : '<div class="empty" role="status">Loading task history…</div>'}`;
  const {tree:t,trace} = taskDetail, all = flattenTask(t.nodes), nodes = all.filter(n => n.status !== 'adopted');
  const blockers = nodes.filter(n => n.blocking), scopes = taskList(t.scope_clarifications), count = blockers.length + scopes.length;
  const authorized = nodes.filter(n => taskAuthorized(n) && n.status !== 'duplicate');
  const needsReview = t.needs_review || t.review?.status === 'stale' || nodes.some(n => n.needs_review || n.source_refresh_required);
  const waiting = [...new Set(blockers.flatMap(n => taskWaiting(n).concat(n.owner && !taskList(n.signatures).some(s => taskSame(s,n.owner)) ? [n.owner] : [])))];
  const canWrite = !state.auth.enabled || ['admin','member'].includes(state.me?.role);
  const reading = !!t.model_pending || nodes.some(n => n.model_pending);
  const lifecycle = runStatusLabel(t.status) || 'In progress';
  const decisionState = needsReview ? 'Review needed' : scopes.length ? 'Waiting for task scope' : count ? 'Waiting for decisions' : 'No recorded blockers';
  const status = ['failed','cancelled','abandoned','completed'].includes(t.status) ? lifecycle : count || needsReview ? decisionState : lifecycle;
  const next = ['cancelled','abandoned'].includes(t.status) ? 'This run is closed. Unresolved questions below are retained as history.' : t.status === 'failed' ? 'Inspect failure details in History and the remaining decisions before retrying the originating workflow.' : scopes.length ? 'Confirm the missing task facts below, then retry the same question with confirmed scope.' : needsReview ? 'Review the affected answers and evidence before using them.' : blockers.length ? waiting.length ? `Waiting on ${waiting.join(', ')}. Open a question for its sign-off and contact history.` : 'Assign a contact or resolve the required follow-up below.' : t.status === 'completed' ? 'Inspect the reported result and checks. Completion alone does not verify the work.' : !nodes.length ? reading ? 'Wait for context reading to finish; refresh to see the next recorded findings.' : 'No question is recorded yet.' : 'No recorded decision blocks this task. Continue the work in the originating workflow.';
  const uncertain = taskList(t.review?.follows).filter(r => r.verdict !== 'follows').length;
  const progress = `<section class="task-summary" aria-label="Current task status"><div><span class="label">Current status</span><h2>${esc(status)}</h2><p class="task-lifecycle"><strong>Recorded lifecycle:</strong> ${esc(lifecycle)} · <strong>Decision state:</strong> ${esc(decisionState)}</p><p class="task-progress-facts">${plural(count,'blocker')} · ${authorized.length} authorized ${authorized.length === 1 ? 'answer' : 'answers'} · ${nodes.filter(n => !['duplicate','suggested'].includes(n.status)).length} recorded questions</p><p class="task-next"><strong>Next:</strong> ${esc(next)}</p>${reading ? '<p class="task-reading" role="status">Reading available context… Findings below reflect the latest recorded state.</p>' : ''}${t.next ? `<details id="task-recorded-next"><summary>Full recorded next action</summary><p class="task-verbatim">${esc(t.next)}</p></details>` : ''}${t.review?.status === 'running' || uncertain ? `<p class="task-warning">${t.review?.status === 'running' ? 'The code review is still running.' : `${uncertain} decisions need inspection after the model review.`}<button class="button small" data-action="task-tab" data-tab="review">Open code review</button></p>` : ''}${t.review?.status === 'stale' ? '<p class="task-warning">The code review needs refreshing for the current questions and answers. Submit the current diff again.</p>' : ''}</div></section>`;
  const context = `<details class="task-panel task-context" id="task-context"><summary id="task-context-summary">Task context</summary>${taskDetails(workItemContext(t),'task-work-item')}<dl class="task-facts"><dt>Requested by</dt><dd>${esc(t.requester || 'Not recorded')}</dd><dt>Repository</dt><dd>${esc(t.repo)}</dd>${Object.entries(t.facts || {}).map(([k,v]) => `<dt>${esc(k)}</dt><dd>${esc(taskText(v))}</dd>`).join('')}</dl><details id="task-kickoff"><summary>Kickoff verdict · ${esc(t.verdict || 'not recorded')}</summary><p>${esc(t.verdict_why)}</p></details><p class="context">Requester is recorded task context, not proof of identity. Signatures and notes keep their acting person.</p></details>`;
  const findings = [...nodes].sort((a,b) => Number(!!(b.blocking || b.needs_review || b.source_refresh_required)) - Number(!!(a.blocking || a.needs_review || a.source_refresh_required)));
  const overview = `<div class="task-overview">${scopes.length ? `<section class="task-panel"><h2>Scope needed · ${scopes.length}</h2>${taskScopes(scopes)}</section>` : ''}<section class="task-panel"><h2>Questions & findings</h2><p class="context">Unresolved questions appear first. Findings can be unapproved. Each row keeps its current approval status separate from its recorded answer or evidence.</p>${taskQuestions(findings,'finding',reading ? 'No findings recorded yet; context reading is still pending.' : 'No questions or findings recorded yet.')}</section>${context}${taskNotes(t,canWrite)}</div>`;
  return `<div class="task-heading"><a class="task-back" href="#runs">← All tasks</a><div class="task-heading-actions"><button id="task-refresh" class="button small" data-action="task-refresh">Refresh</button><button id="task-share" class="button small" data-action="task-share">Copy app link</button>${state.auth.enabled && state.me?.id && state.me.role !== 'viewer' && state.settings?.brief_mode !== 'off' ? '<button class="button small" data-action="task-brief">Open task page</button>' : ''}</div><h1>${esc(taskDisplayLabel(t))}</h1><details class="task-brief" id="task-brief"><summary id="task-brief-summary">Read the task brief</summary><p class="task-goal">${esc(t.goal || t.title)}</p></details><div class="metadata"><span>Requested by ${esc(t.requester || 'unknown')}</span><span>${esc(t.repo)}</span></div></div>
    ${taskError ? `<p class="task-warning" role="alert">Refresh failed: ${esc(taskError)}. Showing the last successful read.</p>` : ''}${progress}
    <nav class="task-tabs" aria-label="Task sections">${[['overview','Overview'],['decisions',`Decisions (${nodes.length})`],['history','History'],['review',`Code review${uncertain ? ' ('+uncertain+' to inspect)' : ''}`]].map(([key,label]) => `<button id="task-tab-${key}" class="tab ${taskTab === key ? 'active' : ''}" data-action="task-tab" data-tab="${key}" aria-pressed="${taskTab === key}">${label}</button>`).join('')}<span>Observed ${esc(taskTime(t.observed_at))}</span></nav>
    ${taskTab === 'overview' ? overview : taskTab === 'decisions' ? `<section class="task-panel task-decisions"><h2>Questions & answers</h2>${taskScopes(scopes)}${taskQuestions(nodes,'question','No decisions recorded yet.')}</section>` : taskTab === 'review' ? taskReview(t.review,nodes) : `<section class="task-panel"><h2>What happened, in order</h2><p class="context">Recorded assignments, messages, answers, corrections, and notes. Work outside Raven is not captured here.</p>${taskTimeline(trace,all)}</section>`}
    ${taskList(state.executions).some(e => e.run_id === t.task_id) ? `<button class="button" data-action="execution-details" data-id="${esc(t.task_id)}">Execution output & files</button>` : ''}<p class="task-access">${icon('shield')}${state.auth.enabled ? 'Visible to signed-in workspace members and viewers. A link does not grant access.' : 'Local workspace. Enable shared authentication before exposing this instance to a network.'} Task status records workflow state; it does not prove an external coding agent is running.</p>`;
}
