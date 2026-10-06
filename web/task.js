'use strict';

// Task reads use the same authenticated API as the inbox. A copied URL
// carries no credential and gives its recipient no additional access.
let taskDetail = null, taskError = '', taskTab = 'overview', taskRequest = 0;
const taskId = () => view === 'runs' ? location.hash.slice(1).split('/')[1] || '' : '';
const flattenTask = nodes => nodes.flatMap(n => [n, ...flattenTask(n.children || [])]);
const taskTime = at => at ? new Date(at).toLocaleString([], {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'}) : '';

async function loadTask({quiet = false} = {}) {
  const id = taskId();
  if (!id) return;
  // Do not destroy a note or move keyboard focus while someone is writing.
  if (quiet && ($('#note-text')?.value || $('#modal').open || ($('#app').contains(document.activeElement) && ['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)))) return;
  const request = ++taskRequest;
  try {
    const [tree, trace] = await Promise.all([
      api(`/api/tasks/${encodeURIComponent(id)}/tree`),
      api(`/api/tasks/${encodeURIComponent(id)}/trace`),
    ]);
    if (taskId() !== id || request !== taskRequest) return;
    taskDetail = {tree, trace}; taskError = '';
    render();
  } catch (error) {
    if (taskId() !== id || request !== taskRequest) return;
    taskError = error.message;
    render();
  }
}

function taskDecision(n) {
  const waiting = n.required_signers.filter(name => !n.signatures.some(s => s.toLowerCase() === name.toLowerCase()));
  const status = n.needs_review ? 'Needs another look' : n.authorized ? n.signoff === 'rule' ? 'Standing rule' : 'Signed answer' : n.blocking ? 'Waiting for a person' : n.status === 'suggested' ? 'Follow-up suggested' : 'Context only';
  return `<article class="decision-card task-decision ${n.authorized ? 'is-signed' : ''}">
    <div class="card-top">${pill(status, n.authorized ? 'green' : '')}${n.partial ? pill('Partial answer') : ''}<span>${esc(n.owner || 'Owner not assigned')}</span></div>
    <h3>${esc(n.question)}</h3>
    ${n.answer ? `<p class="task-answer">${esc(n.answer)}</p>` : '<p class="context">No answer recorded yet.</p>'}
    ${n.rationale ? `<p class="context"><strong>Why:</strong> ${esc(n.rationale)}</p>` : ''}
    ${n.needs_review ? `<p class="task-warning">${esc(n.review_reason || 'A source answer changed. Review before using this answer.')}</p>` : ''}
    <div class="task-decision-foot"><span>${n.authorized ? esc(n.signoff === 'rule' ? 'Authorized by an enabled standing rule' : 'Signed by ' + (n.signed_by || n.answered_by)) : esc(waiting.length ? 'Still needs ' + waiting.join(', ') : 'Not authorized for use')}${n.path ? `<br><code>${esc(n.path)}</code>` : ''}</span><button class="button small" data-action="review" data-id="${esc(n.node_id)}">Open decision ${icon('arrow')}</button></div>
  </article>`;
}

function taskTimeline(trace, nodes) {
  const entries = trace.events.map(e => ({...e, type:'event'})).concat(trace.notifications.map(n => ({...n, type:'notification', at:n.sent_at || n.created_at})))
    .sort((a,b) => a.at.localeCompare(b.at) || Number(a.id) - Number(b.id));
  return `<ol class="task-timeline">${entries.map(e => {
    const node = nodes.find(n => n.node_id === e.decision_id);
    let title, body;
    if (e.type === 'notification') {
      title = `${e.state === 'sent' ? 'Message sent' : e.state === 'failed' ? 'Message failed' : 'Message ' + e.state}${e.person_name ? ' to ' + e.person_name : ''}`;
      body = `<p>${esc(e.channel)} · ${esc(e.kind.replaceAll('_', ' '))} · ${e.attempts} delivery attempts</p>${e.last_error ? `<p class="task-warning">${esc(e.last_error)}</p>` : ''}<small>Delivery does not confirm that the person read it.</small>`;
    } else {
      title = eventLabels[e.kind] || e.kind.replaceAll('_', ' ');
      const d = e.detail;
      if (typeof d === 'string') body = `<p>${esc(d)}</p>`;
      else {
        const by = d.actor_name || d.actor || d.by || d.answered_by || d.requester || d.owner || '';
        const text = d.text || d.answer || d.reason || d.why || d.question || d.summary || '';
        body = `${by ? `<p><strong>${esc(by)}</strong></p>` : ''}${text ? `<p>${esc(text)}</p>` : ''}<details><summary>Recorded details</summary><dl class="task-event-details">${Object.entries(d).map(([key,value]) => `<dt>${esc(key.replaceAll('_', ' '))}</dt><dd>${esc(typeof value === 'object' ? JSON.stringify(value, null, 2) : value)}</dd>`).join('')}</dl></details>`;
      }
    }
    return `<li><span class="timeline-mark">${icon(e.type === 'notification' ? 'arrow' : 'activity')}</span><div><div class="timeline-heading"><strong>${esc(title)}</strong><time datetime="${esc(e.at)}">${esc(taskTime(e.at))}</time></div>${node ? `<button class="timeline-question" data-action="review" data-id="${esc(node.node_id)}">${esc(node.question)}</button>` : ''}${body}</div></li>`;
  }).join('')}</ol>`;
}

// Where each person stands, in the task page's words (Decided, Signed,
// Handed on, Waiting on), and in the order the task reached them. The page
// and the app named the same facts two ways, and listed people in two
// orders.
function taskPeople(all, trace) {
  const same = (a, b) => (a || '').toLowerCase() === (b || '').toLowerCase();
  // The decisions the page counts: a duplicate reads through to the one it
  // repeats, and counted that answer twice.
  const nodes = all.filter(n => !['duplicate', 'suggested', 'adopted'].includes(n.status));
  const first = name => (trace.notifications.find(n => same(n.person_name, name)) || {}).created_at || '~';
  const names = [...new Set(nodes.flatMap(n => [n.owner, n.answered_by, ...n.required_signers, ...n.signatures]).concat(trace.notifications.map(n => n.person_name)).filter(Boolean))]
    .sort((a, b) => first(a).localeCompare(first(b)));
  const handoffs = trace.events.filter(e => e.kind === 'owner_changed' && e.detail && e.detail.referral);
  return names.length ? names.map((name,i) => {
    // Who decided a node, as the task page counts it (briefing.decided_by):
    // the person whose answer it is, who gave it and signed it. An answered
    // node keeps the kind it was asked with, so `kind === 'answer'` alone
    // called Mei's and Priya's own answers "Signed".
    const decidedBy = n => n.answered_by && (n.kind === 'answer' || n.signatures.some(s => same(s, n.answered_by))) ? n.answered_by : '';
    const decided = nodes.filter(n => same(decidedBy(n), name)).length;
    const signed = nodes.filter(n => n.signatures.some(s => same(s, name)) && !same(decidedBy(n), name)).length;
    const waiting = nodes.filter(n => n.blocking && [n.owner, ...n.required_signers].some(x => same(x, name)) && !n.signatures.some(s => same(s, name))).length;
    const handed = handoffs.filter(e => same(e.detail.by, name)).length;
    const states = [['Decided', decided], ['Signed', signed], ['Handed on', handed], ['Waiting on', waiting]].filter(([, n]) => n).map(([label, n]) => `${label} ${n}`);
    const messages = trace.notifications.filter(n => n.person_name === name);
    const sent = messages.filter(m => m.state === 'sent').length;
    return `<div class="task-person">${avatar(name,i)}<div><strong>${esc(name)}</strong><p>${esc(states.join(' · ') || 'Asked')}</p><small>${sent ? `${plural(sent, 'message')} sent` : 'No external message sent; available in the inbox'}${messages.some(m => m.state === 'failed') ? ' · delivery failed' : ''}</small></div></div>`;
  }).join('') : '<p class="context">Nobody has been assigned a decision yet.</p>';
}

// The decision the task page should open on for the signed-in person:
// one waiting on them, else one they answered or signed, else one they
// own. Without it the page lost the decision they had just answered.
function taskBriefDecision() {
  const me = state.me?.name || '';
  const nodes = taskDetail ? flattenTask(taskDetail.tree.nodes).filter(n => !['duplicate','adopted','suggested'].includes(n.status)) : [];
  const mine = name => (name || '').toLowerCase() === me.toLowerCase();
  const owed = n => n.blocking && [n.owner, ...n.required_signers].some(x => mine(x) && !n.signatures.some(mine));
  const pick = nodes.find(owed) || nodes.find(n => mine(n.answered_by) || n.signatures.some(mine)) || nodes.find(n => mine(n.owner));
  return pick ? pick.node_id : '';
}

function taskOverview() {
  // Task reads can beat the first account read. Unknown access is not local
  // operator access; wait before rendering actions or the sharing notice.
  if (!state.auth) return '<div class="empty" role="status">Loading workspace access…</div>';
  if (!taskDetail || taskDetail.tree.task_id !== taskId()) return `<a class="task-back" href="#runs">← All tasks</a>${taskError ? `<div class="empty"><h2>Could not open this task</h2><p>${esc(taskError)}</p><button class="button" data-action="task-refresh">Try again</button></div>` : '<div class="empty" role="status">Loading task history…</div>'}`;
  const {tree:t, trace} = taskDetail;
  // An adopted follow-up has a real decision in the same tree. Its placeholder
  // belongs in history, not beside the active decision as a second question.
  const nodes = flattenTask(t.nodes).filter(n => n.status !== 'adopted');
  const blockers = nodes.filter(n => n.blocking);
  const signed = nodes.filter(n => n.authorized && n.status !== 'duplicate');
  const waiting = [...new Set(blockers.flatMap(n => n.required_signers.filter(s => !n.signatures.includes(s)).concat(n.owner && !n.signatures.includes(n.owner) ? [n.owner] : [])))];
  const canWrite = !state.auth?.enabled || ['admin','member'].includes(state.me?.role);
  const status = t.needs_review || t.review?.status === 'stale' ? 'Review needed' : blockers.length ? 'Waiting for decisions' : t.status === 'completed' ? 'Agent reported complete' : ({result_ready:'Result ready for inspection',failed:'Agent failed',cancelled:'Cancelled',review_required:'Review needed'})[t.status] || 'In progress';
  const facts = Object.entries(t.facts || {});
  const uncertain = (t.review?.follows || []).filter(r => r.verdict !== 'follows').length;
  const reviewNote = t.review?.status === 'running' ? 'The code review is still running.' : uncertain ? `${uncertain} decision${uncertain === 1 ? ' needs' : 's need'} inspection after the model review. Open Code review for details.` : '';
  const progress = `<div class="task-summary"><div><span class="label">Current status</span><h2>${esc(status)}</h2><p>${blockers.length ? `${blockers.length} decision${blockers.length === 1 ? '' : 's'} still ${blockers.length === 1 ? 'blocks' : 'block'} completion${waiting.length ? '. Waiting on ' + esc(waiting.join(', ')) : '. An owner needs to be assigned or a follow-up adopted'}.` : nodes.length ? 'No recorded decision is blocking this task.' : 'The agent has not recorded a decision yet.'}</p>${reviewNote ? `<p class="task-warning">${esc(reviewNote)} <button class="button small" data-action="task-tab" data-tab="review">Open code review</button></p>` : ''}${t.review?.status === 'stale' ? '<p class="task-warning">The code review needs refreshing for the current signed questions and answers. The agent must submit its current diff again.</p>' : ''}</div><div class="task-count"><strong>${signed.length}<span> / ${nodes.filter(n => !['duplicate','adopted','suggested'].includes(n.status)).length}</span></strong><span>signed</span></div></div>`;
  const review = t.review ? `<section class="task-panel"><h2>Code review against decisions</h2>${pill(t.review.status)}${t.review.seconds != null ? `<span class="context"> ${esc(t.review.seconds)} seconds</span>` : ''}<p class="context">This model review covers the recorded decisions. It does not replace tests or verify the whole change.</p>${(t.review.follows || []).map(r => `<article class="task-review-item"><div>${pill(r.verdict)} <strong>${esc(nodes.find(n => n.node_id === r.node_id)?.question || r.node_id)}</strong></div><p>${esc(r.why || r.reason || '')}</p><details class="review-requirements" id="requirements-${esc(r.node_id)}"><summary>Inspect requirements (${(r.requirements || []).length})</summary>${(r.requirements || []).map(q => `<details><summary>${esc(q.state || q.status || q.verdict || '')} · ${esc(q.needs || q.requirement || q.text || '')}</summary><pre>${esc(JSON.stringify(q, null, 2))}</pre></details>`).join('')}</details></article>`).join('')}<details><summary>Full review record</summary><pre>${esc(JSON.stringify(t.review, null, 2))}</pre></details></section>` : '';
  const notes = `<section class="task-panel"><h2>Context & discussion</h2><p class="context">Notes are visible to the agent when it reads the task. Use a decision’s follow-up action for a question that must block completion.</p>${(t.notes || []).map(n => `<article class="task-note"><div><strong>${esc(n.by || 'Unattributed note')}</strong><time>${esc(taskTime(n.at))}</time></div><p>${esc(n.text)}</p></article>`).join('') || '<p class="context">No notes yet.</p>'}${canWrite ? `<details class="history" id="task-note-compose"><summary>Add context for the agent</summary><form id="note-form" data-id="${esc(t.task_id)}"><label for="note-text">A note the agent reads on its tree</label><textarea id="note-text" name="text" maxlength="4000" placeholder="Add context, a constraint, or something the agent should revisit." required></textarea><p class="error" id="form-error" role="alert" hidden></p><button class="button primary" type="submit">Add note</button></form></details>` : '<p class="context">You have read-only access to this workspace.</p>'}</section>`;
  const overview = `<div class="task-columns"><div><section class="task-panel"><h2>What the agent has learned</h2><p class="context">Signed answers and enabled standing rules. Evidence and guesses stay in Decisions until authorized.</p>${signed.length ? signed.map(n => `<details class="learned-decision" id="learned-${esc(n.node_id)}"><summary><span>${esc(n.question)}</span><small>${esc(n.signoff === 'rule' ? 'Standing rule' : 'Signed by ' + (n.signed_by || n.answered_by || n.owner))}</small></summary>${taskDecision(n)}</details>`).join('') : '<p class="task-empty">No authorized answers yet. Open Decisions to see the questions and proposals.</p>'}</section>${notes}</div><aside><section class="task-panel"><h2>People involved</h2>${taskPeople(nodes,trace)}</section><section class="task-panel"><h2>Task context</h2><dl class="task-facts"><dt>Requested by</dt><dd>${esc(t.requester || 'Not recorded')}</dd><dt>Repository</dt><dd>${esc(t.repo)}</dd>${facts.map(([k,v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl><details class="history"><summary>Kickoff verdict · ${esc(t.verdict || 'not recorded')}${t.requester ? ' · asked by ' + esc(t.requester) : ''}</summary><p>${esc(t.verdict_why)}</p></details><p class="context">Requester is recorded task context, not proof of identity. Signatures and notes keep their acting person.</p></section></aside></div>`;
  const decisions = `<div class="task-decisions">${nodes.length ? nodes.map(n => `<div class="tree-node depth-${Math.min(n.depth,6)}">${n.depth ? `<p class="context">Level ${n.depth}${n.origin === 'human' ? ' · added by a person' : ''}</p>` : ''}${taskDecision(n)}</div>`).join('') : '<div class="empty">No decisions recorded yet.</div>'}</div>`;
  return `<div class="task-heading"><a class="task-back" href="#runs">← All tasks</a><div class="task-heading-actions"><button class="button small" data-action="task-refresh">Refresh</button><button class="button small" data-action="task-share">Copy app link</button>${state.auth?.enabled && state.me?.id && state.me.role !== 'viewer' && state.settings?.brief_mode !== 'off' ? `<button class="button small" data-action="task-brief">Open task page</button>` : ''}</div><p class="eyebrow">TASK OVERVIEW</p><h1>${esc(t.title)}</h1><details class="task-brief" id="task-brief"><summary>Read the task brief</summary><p class="task-goal">${esc(t.goal || 'No task brief recorded.')}</p></details><div class="metadata">${pill(status, blockers.length ? '' : 'gray')}<span>Requested by ${esc(t.requester || 'unknown')}</span><span>${esc(t.repo)}</span></div></div>
    ${taskError ? `<p class="task-warning" role="alert">Refresh failed: ${esc(taskError)}. Showing the last successful read.</p>` : ''}${progress}
    <nav class="task-tabs" aria-label="Task sections">${[['overview','Overview'],['decisions',`Decisions (${nodes.length})`],['history','History'],['review',`Code review${uncertain ? ' (' + uncertain + ' to inspect)' : ''}`]].map(([key,label]) => `<button id="task-tab-${key}" class="tab ${taskTab === key ? 'active' : ''}" data-action="task-tab" data-tab="${key}" aria-pressed="${taskTab === key}">${label}</button>`).join('')}<span>Updated ${esc(taskTime(t.observed_at))}</span></nav>
    ${taskTab === 'overview' ? overview : taskTab === 'decisions' ? decisions : taskTab === 'review' ? review || '<section class="task-panel"><h2>No code review yet</h2><p>The agent submits its diff when it finishes. Owner approval and code review are separate.</p></section>' : `<section class="task-panel"><h2>What happened, in order</h2><p class="context">The complete recorded task history, including assignments, messages, answers, corrections, and notes. Work an agent does outside Raven is not captured here.</p>${taskTimeline(trace,flattenTask(t.nodes))}</section>`}
    ${(state.executions || []).some(e => e.run_id === t.task_id) ? `<button class="button" data-action="execution-details" data-id="${esc(t.task_id)}">Execution output & files</button>` : ''}<p class="task-access">${icon('shield')}${state.auth?.enabled ? 'Visible to signed-in workspace members and viewers. A link does not grant access.' : 'Local workspace. Enable shared authentication before exposing this instance to a network.'} Agent completion is a report, not verification of the patch.</p>`;
}
