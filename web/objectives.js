'use strict';

// Today is a durable work projection, not a second authorization inbox.
let objectiveData = null, objectiveError = '', objectiveRequest = 0;
const objectivePending = new Set();
const objectiveDrafts = new Map();
let objectivePanel = '', objectiveMeetings = null, objectiveNotice = '', objectiveMeetingRepo = '';
const objectiveMeetingsURL = () => '/api/meetings?repo=' + encodeURIComponent(objectiveMeetingRepo);
const objectiveKeys = new Map();
const objectiveKey = key => { if (!objectiveKeys.has(key)) objectiveKeys.set(key, crypto.randomUUID()); return objectiveKeys.get(key); };
const objectiveRoute = () => location.hash.slice(1);
const objectiveId = () => { try { return view === 'today' ? decodeURIComponent(location.hash.slice(1).split('/')[1] || '') : ''; } catch { return ''; } };
const objectiveProposals = () => [...(objectiveData?.proposals || []), ...(objectiveData?.detail?.proposals || []), ...(objectiveData?.objectives || []).flatMap(item => item.proposals || [])];
const objectiveCanWrite = () => !!state.auth && (!state.auth.enabled || ['admin', 'member'].includes(state.me?.role));
const objectiveCanAdmin = () => !!state.auth && (!state.auth.enabled || state.me?.role === 'admin');
const OBJECTIVE_STATUS = {proposed:'Suggested', accepted:'Accepted', active:'In progress', running:'Working', queued:'Queued',
  waiting:'Waiting', blocked:'Needs attention', paused:'Paused', cancelled:'Cancelled', completed:'Completed', achieved:'Achieved · human attested', stopped:'Stopped', needs_you:'Needs you', moving:'In progress', review:'Awaiting signed review', needs_judgment:'Needs judgment', ready:'Ready to start',
  verification_pending:'Verification needed', review_required:'Review needed', failed:'Needs attention', dismissed:'Dismissed'};
const objectiveStatus = value => OBJECTIVE_STATUS[value] || String(value || 'Not reported').replaceAll('_', ' ');
const objectiveTime = value => {
  if (!value) return 'Not scheduled';
  const time = new Date(value);
  return Number.isNaN(time.getTime()) ? 'Invalid scheduled time' : time.toLocaleString(undefined, {dateStyle:'medium',timeStyle:'short'});
};
function objectiveButton(action, id, label, disabled = false, extra = '') {
  const pending = objectivePending.has(id);
  return `<button class="button small" type="button" data-objective-action="${esc(action)}" data-id="${esc(id)}" ${disabled || pending ? 'disabled' : ''} ${pending ? 'aria-busy="true"' : ''} ${extra}>${esc(label)}</button>`;
}
function objectiveSourceLink(item) {
  try {
    const url = new URL(String(item.url || ''));
    if (['http:','https:'].includes(url.protocol) && !url.username && !url.password) return {href:url.href,label:'Open original source ↗'};
  } catch (_) { /* Unsupported source schemes are never navigation targets. */ }
  return item.record_id && item.repo ? {href:'/api/records?record_id=' + encodeURIComponent(item.record_id) + '&repo=' + encodeURIComponent(item.repo),label:'Open recorded source (JSON) ↗'} : null;
}
function objectiveJudgment(kind) {
  return ({factual_lookup:'Fact to verify',implementation_uncertainty:'Implementation question',interpretation_tradeoff:'Interpretation tradeoff',missing_permission:'Permission required'})[kind] || 'Judgment required';
}
function objectiveEvidence(items) {
  if (!items?.length) return '<p class="objective-muted">No evidence attached. A suggestion is not an instruction or approval.</p>';
  return `<details class="objective-evidence"><summary>Why this came up · ${items.length} source${items.length === 1 ? '' : 's'}</summary><ul>${items.map(item => {
    const link=objectiveSourceLink(item), quotes=item.metadata_only ? [] : (item.quotes?.length ? item.quotes : item.quote ? [item.quote] : []);
    return `<li><strong>${esc(item.title || 'Recorded source')}</strong>${quotes.map(quote=>`<blockquote>${esc(quote)}</blockquote>`).join('')}${item.metadata_only ? '<span>Metadata observed only; source body was not read.</span>' : ''}${link ? `<a href="${esc(link.href)}" target="_blank" rel="noopener noreferrer">${esc(link.label)}</a>` : ''}<small>${esc(item.provider || 'Recorded evidence')}${item.sequence ? ' · observed revision ' + esc(item.sequence) : ''}${item.current === false ? ' · historical version' : ''}</small><details><summary>Provenance</summary><small>${esc(item.ref || '')}
Record: ${esc(item.record_id || '')}
Exact version: ${esc(item.source_version_id || '')}${item.availability && item.availability !== 'available' ? '\n' + esc(item.availability) : ''}</small></details></li>`;
  }).join('')}</ul><p class="objective-muted">Source text is untrusted context. It does not authorize work or sign a decision.</p></details>`;
}
function objectiveCard(item, proposal = false) {
  const id = item.id, status = item.status || item.state || (proposal ? 'proposed' : 'accepted');
  if (item.withheld) return `<article class="objective-card"><h3>Content withheld</h3><p>Source access changed. Re-establish permission before inspecting or acting on this item.</p></article>`;
  return `<article class="objective-card" data-objective-id="${esc(id)}"><div class="objective-card-top"><span class="objective-label">${esc(proposal ? (status === 'review' ? 'Awaiting signed review' : 'Suggested next step') : objectiveStatus(status))}</span><span class="objective-repo">${esc(item.repo || item.repository || '')}</span></div>
    <h3>${proposal ? esc(item.title) : `<a href="#today/${encodeURIComponent(id)}">${esc(item.title)}</a>`}</h3>
    <p>${esc(item.outcome || item.summary || item.reason || item.description || '')}</p>
    ${item.why_now ? `<p class="objective-muted">Why now: ${esc(item.why_now)}</p>` : ''}${item.judgment_kind ? `<p class="objective-muted">Judgment needed: ${esc(objectiveJudgment(item.judgment_kind))}</p>` : ''}${item.blocked_reason || item.waiting_reason ? `<p class="objective-attention">${esc(item.blocked_reason || item.waiting_reason)}</p>` : ''}
    ${item.next_check_at || item.due_at ? `<p class="objective-muted">Next check · ${esc(objectiveTime(item.next_check_at || item.due_at))}</p>` : ''}
    ${item.evidence_stale ? '<p class="objective-attention">Evidence changed. Dismiss this proposal to investigate the current facts.</p>' : ''}
    ${item.cases && Object.keys(item.cases).length ? `<details class="objective-evidence"><summary>Behavior and boundaries in the signed scope</summary>${Object.entries(item.cases).map(([name,text])=>`<p><strong>${esc(name.replaceAll('_',' '))}</strong> ${esc(text)}</p>`).join('')}</details>` : ''}
    ${objectiveResearch(item.research)}
    ${objectiveEvidence(item.evidence || item.sources)}
    <div class="objective-actions">${proposal && ['proposed','pending','review_error','reconcile'].includes(status) && objectiveCanWrite() ? (status === 'reconcile' || item.evidence_stale ? '' : objectiveButton('review-accept', id, status === 'review_error' ? 'Retry judgment review' : 'Review & take point')) + objectiveButton('dismiss', id, 'Not now') : proposal && status === 'review' ? `<a class="objective-text-link" href="#runs/${encodeURIComponent(item.task_id || item.governance_run_id || '')}">Open judgment review ↗</a>` : !proposal ? `<a class="objective-text-link" href="#today/${encodeURIComponent(id)}">Open objective <span aria-hidden="true">↗</span></a>` : ''}</div></article>`;
}
function objectiveResearchLimits() {
  const limits = objectiveData.research_limits || [];
  const statuses = objectiveData.research_status || [];
  const unavailable = objectiveData.background_inference?.available === false ? objectiveData.background_inference.notice : '';
  return limits.length || statuses.length || unavailable ? `<section class="objective-side-section"><h2>Background investigation</h2>${unavailable ? `<p role="status">${esc(unavailable)}</p>` : ''}${statuses.map(item=>`<p role="status"><strong>${esc(item.repository)}</strong>: ${esc(item.state)} · ${esc(item.attempts)} attempts. ${item.state === 'blocked' ? 'Automatic retries stopped. Repair setup, then use Find opportunities → explicit retry; or re-enable the watch.' : item.state === 'failed' ? 'Investigation was inconclusive; bounded retry may follow.' : 'Investigation in progress.'}</p>`).join('')}${limits.map(limit=>`<p><strong>${esc(limit.repository)}</strong> · ${esc(limit.used)}/${esc(limit.limit)} attempts in the rolling hour.</p>${limit.status === 'throttled' ? `<p role="status">Waiting for the research budget. Next eligible check: ${esc(objectiveTime(limit.next_retry_at))}. Evidence remains queued; this is not a completed investigation.</p>` : ''}`).join('')}<p class="objective-muted">Automatic reads are debounced and bounded. Manual investigation is a separate explicit request.</p></section>` : '';
}
function objectiveResearch(packet) {
  if (!packet) return '';
  const coverage = packet.coverage || {};
  return `<details class="objective-evidence"><summary>What Raven investigated · ${esc(packet.disposition || 'bounded reading')}</summary>${packet.reason ? `<p>${esc(packet.reason)}</p>` : ''}<p>${esc(coverage.read_sources ?? 0)} of ${esc(coverage.eligible_sources ?? '?')} eligible source versions read; ${esc(coverage.unread_sources ?? '?')} unread. ${coverage.limit_reached ? 'Research budget reached; incomplete.' : ''}</p>${(packet.observations || []).map(finding=>`<p>${esc(finding.statement)}</p>${(finding.citations || []).map(c=>`<blockquote>${esc(c.quote)}<small>Source ${esc(c.record_id)} · version ${esc(c.source_version_id)}</small></blockquote>`).join('')}`).join('')}${(coverage.meetings || []).map(meeting=>`<p>Meeting coverage: ${esc(meeting.read_chunks)}/${esc(meeting.active_chunks)} chunks. ${meeting.full_meeting_read ? 'Whole imported transcript read.' : 'Partial reading, not a whole-meeting conclusion.'}</p>`).join('')}<p class="objective-muted">${esc(coverage.notice || 'Findings are evidence, never permission.')}</p></details>`;
}

function objectiveCapabilities() {
  const hosts = objectiveData?.capabilities || state.workbench?.hosts || [];
  return `<section class="objective-side-section"><h2>Execution hosts</h2><p class="objective-muted">Harnesses run tasks. Model access alone is not an execution connection.</p>${hosts.length ? hosts.map(host => `<details class="objective-capability"><summary><strong>${esc(host.name || host.id)}</strong><span>${host.ready ? 'Configured' : 'Setup needed'}</span></summary><p>${esc(host.execution || '')}</p><p>${esc(host.containment || '')}</p>${(host.blockers || []).map(message => `<p class="objective-muted">${esc(message)}</p>`).join('')}<small>${host.live_verified ? 'Live verification reported by the server' : 'Live acceptance not verified'}${host.version ? ' · ' + esc(host.version) : ''}</small></details>`).join('') : '<p class="objective-muted">No execution hosts reported.</p>'}<a class="objective-text-link" href="#connect">Connections & setup ↗</a></section>`;
}
function objectiveAcceptance(item) {
  return `<section class="objective-editor" aria-labelledby="objective-accept-title"><div class="objective-section-head"><h2 id="objective-accept-title">Review & take point</h2>${objectiveButton('close-panel', '', 'Cancel')}</div><h3>${esc(item.title)}</h3><p>${esc(item.outcome || item.summary || item.reason || '')}</p>${objectiveEvidence(item.evidence || item.sources)}<p class="objective-notice">This creates a judgment request in the existing inbox. It does not activate the objective or approve unresolved decisions. Review and sign the exact scope there before work can start.</p><form id="objective-accept-form" data-id="${esc(item.id)}"><button type="submit" class="button" ${objectivePending.has(item.id) ? 'disabled aria-busy="true"' : ''}>Send to judgment inbox</button></form></section>`;
}
function objectiveDetail(item) {
  const id = item.id, terminal = ['completed','cancelled','achieved','stopped'].includes(item.lifecycle || item.status);
  if (item.withheld) return '<p class="objective-notice">Source access changed. This objective is withheld.</p>';
  const checkpoints = item.checkpoints || item.events || [];
  const tasks = item.runs || [];
  return `<a class="objective-text-link" href="#today">← Today</a><header class="objective-detail-heading"><p class="objective-label">${esc(objectiveStatus(item.status))} · ${esc(item.repo || item.repository || '')}</p><h1>${esc(item.title)}</h1><p>${esc(item.outcome || item.summary || item.description || '')}</p></header>
    <div class="objective-layout"><section><article class="objective-card"><h2>Next step</h2><p>${esc(item.next_action || item.blocked_reason || item.waiting_on || item.waiting_reason || (terminal ? 'This objective is closed.' : 'No next action has been recorded.'))}</p>${item.next_check_at ? `<p class="objective-muted">Scheduled check · ${esc(objectiveTime(item.next_check_at))}</p>` : ''}<div class="objective-actions">${objectiveCanWrite() && !terminal ? objectiveButton((item.lifecycle || item.status) === 'paused' ? 'resume' : 'pause',id,(item.lifecycle || item.status) === 'paused' ? 'Resume' : 'Pause') + objectiveButton('checkpoint',id,'Save checkpoint') + objectiveButton('dispatch',id,'Start execution') + objectiveButton('review-achieve',id,'Attest outcome') + objectiveButton('review-cancel',id,'Cancel objective') : ''}</div></article>
    <article class="objective-card"><h2>Execution harness</h2><p>${esc(item.provider || 'Not selected')}</p><h2>Success criteria</h2><p class="objective-preserve">${esc(item.success_criteria || 'No success criteria recorded.')}</p><h2>Stop criteria</h2><p class="objective-preserve">${esc(item.stop_criteria || 'Not recorded')}</p><h2>Guardrails</h2><p class="objective-preserve">${esc(item.guardrails || 'Not recorded')}</p><p class="objective-notice">A host saying “done” is not independent verification. Review the delivered artifact and recorded checks.</p>${item.verification ? `<pre>${esc(typeof item.verification === 'string' ? item.verification : JSON.stringify(item.verification,null,2))}</pre>` : '<p class="objective-muted">No independent verification recorded.</p>'}</article>
    <section class="objective-section"><h2>Next-step proposals</h2>${(item.proposals || []).filter(proposal => ['proposed','review','review_error','reconcile','preparing'].includes(proposal.status || proposal.state)).map(proposal => objectiveCard(proposal,true)).join('') || '<p class="objective-muted">No next-step proposal awaiting review.</p>'}</section><section class="objective-section"><h2>Linked work</h2>${tasks.length ? tasks.map(task => `<a class="objective-task-link" href="#${task.browser_task ? 'work' : 'runs'}/${encodeURIComponent(task.task_id || task.id)}"><strong>${esc(task.title || task.prompt || task.task_id)}</strong><span>${esc(objectiveStatus(task.status))}</span></a>`).join('') : '<p class="objective-muted">No execution task linked yet.</p>'}</section>
    <section class="objective-section"><h2>Saved progress</h2>${checkpoints.length ? `<ol class="objective-timeline">${checkpoints.map(point => `<li><strong>${esc(point.summary || point.label || point.kind || 'Checkpoint')}</strong><p>${esc(point.detail && typeof point.detail === 'string' ? point.detail : point.note || '')}</p><time>${esc(objectiveTime(point.created_at || point.at))}</time></li>`).join('')}</ol>` : '<p class="objective-muted">No checkpoints yet. Progress stays attached to this objective across sessions.</p>'}</section></section><aside>${objectiveEvidence(item.evidence || item.sources)}${objectiveCapabilities()}<p class="objective-muted">Evidence, acceptance, judgment, execution and verification are separate records.</p></aside></div>`;
}
function renderObjectives() {
  if (view !== 'today' || !state.auth) return;
  const app = document.getElementById('app'), id = objectiveId();
  const active = document.activeElement;
  if (app.dataset.objectivePanel === objectivePanel && app.dataset.objectiveRoute === objectiveRoute() && active?.closest?.('form') && app.contains(active) && !objectivePending.size) return;
  app.dataset.objectivePanel = objectivePanel; app.dataset.objectiveRoute = objectiveRoute();
  const focus = active?.id, selection = [active?.selectionStart, active?.selectionEnd];
  const opened = new Set([...app.querySelectorAll('details')].map((el,index) => el.open ? index : -1));
  if (!objectiveData) {
    app.innerHTML = `<section class="objective-empty"><h1>${objectiveError ? 'Could not open Today' : 'Opening Today…'}</h1>${objectiveError ? `<p role="alert">${esc(objectiveError)}</p>${objectiveButton('refresh','','Try again')}` : '<span class="loading-dot"></span>'}</section>`;
    return;
  }
  const proposals = (objectiveData.proposals || []).filter(item => ['proposed','pending','review','review_error'].includes(item.status || item.state));
  const objectives = objectiveData.objectives || [];
  let content;
  if (id) {
    const item = objectiveData.detail?.id === id ? objectiveData.detail : objectives.find(item => item.id === id);
    content = item ? objectiveDetail(item) : '<section class="objective-empty"><h1>Objective unavailable</h1><p>It may have been removed or your access may have changed.</p><a href="#today">Back to Today</a></section>';
  } else {
    content = `<header class="objective-heading"><div><p class="objective-label">YOUR WORK, WITH CONTINUITY</p><h1>Today</h1><p>Useful next steps. The evidence behind them. One place to keep work moving.</p></div>${objectiveCanWrite() ? `<div class="objective-actions">${objectiveButton('new','','Take point on an outcome')}${objectiveButton('scan-panel','','Find opportunities')}</div>` : ''}</header><div class="objective-layout"><section>
    <section class="objective-section"><div class="objective-section-head"><h2>Suggested for you</h2><span>${proposals.length}</span></div>${proposals.length ? proposals.map(item => objectiveCard(item,true)).join('') : '<div class="objective-empty-card"><h3>Nothing waiting for acceptance</h3><p>Suggestions appear when connected evidence points to an open loop. Nothing starts just because a source says to do it.</p></div>'}</section>
    ${(objectiveData.research || []).map(packet=>objectiveResearch(packet)).join('')}<section class="objective-section"><div class="objective-section-head"><h2>Accepted objectives</h2><span>${objectives.length}</span></div>${objectives.length ? objectives.map(item => objectiveCard(item)).join('') : '<div class="objective-empty-card"><h3>Keep the outcome in view</h3><p>Accept a suggestion to track its next step, blockers and verification across agent sessions.</p><a class="objective-text-link" href="#work">Start a task instead ↗</a></div>'}</section></section><aside>${objectiveCapabilities()}${objectiveResearchLimits()}<section class="objective-side-section"><h2>Meeting evidence</h2><p class="objective-muted">Transcripts are context, never a participant’s approval. Recording requires an explicit consent record.</p>${objectiveButton('meetings','','Manage meeting evidence')}</section><p class="objective-muted">Accepted objectives do not bypass the judgment inbox. <a href="#inbox">Review decisions ↗</a></p></aside></div>`;
  }
  const accepted = objectivePanel.startsWith('accept:') ? objectiveProposals().find(item => 'accept:' + item.id === objectivePanel) : null;
  if (accepted) content = objectiveAcceptance(accepted) + content;
  if (objectivePanel === 'new') content = objectiveNewForm() + content;
  if (objectivePanel === 'scan') content = objectiveScanForm() + content;
  if (objectivePanel === 'meetings') content = objectiveMeetingPanel() + content;
  if (objectivePanel.startsWith('checkpoint:') || objectivePanel.startsWith('achieve:')) content = objectiveProgressForm(objectivePanel) + content;
  if (objectivePanel.startsWith('cancel:')) {
    const cancelId = objectivePanel.slice(7);
    content = `<section class="objective-editor"><h2>Cancel this objective?</h2><p>Its history will remain. This does not claim that an already-running external operation has stopped.</p>${objectiveButton('cancel',cancelId,'Confirm cancellation')}${objectiveButton('close-panel','','Keep objective')}</section>` + content;
  }
  app.innerHTML = `<div class="objectives">${objectiveNotice ? `<p class="objective-notice" role="status">${esc(objectiveNotice)}</p>` : ''}${objectiveError ? `<p class="objective-error" role="alert">${esc(objectiveError)}</p>` : ''}${content}</div>`;
  app.querySelectorAll('details').forEach((el,index) => { el.open = opened.has(index); });
  if (focus) {
    const replacement = document.getElementById(focus);
    if (replacement && app.contains(replacement)) {
      replacement.focus({preventScroll:true});
      if (replacement.setSelectionRange && selection[0] != null) replacement.setSelectionRange(...selection);
    }
  }
}
async function loadObjectives() {
  if (view !== 'today' || !state.auth) return;
  const route = objectiveRoute(), id = objectiveId(), request = ++objectiveRequest;
  try {
    const result = await api('/api/objectives');
    if (id) result.detail = await api('/api/objectives/' + encodeURIComponent(id));
    if (view !== 'today' || route !== objectiveRoute() || request !== objectiveRequest) return;
    objectiveData = result; objectiveError = ''; renderObjectives();
  } catch (error) {
    if (view !== 'today' || route !== objectiveRoute() || request !== objectiveRequest) return;
    if ([401,403].includes(error.status)) objectiveData = null;
    objectiveError = error.message; renderObjectives();
  }
}
async function objectiveMutate(key, url, payload) {
  if (objectivePending.has(key) || !objectiveCanWrite()) return;
  const route = objectiveRoute(), panel = objectivePanel;
  objectivePending.add(key); objectiveError = ''; renderObjectives();
  try {
    const result = await api(url,payload);
    for (const [name, value] of objectiveKeys) if (value === payload.request_key) objectiveKeys.delete(name);
    if (route !== objectiveRoute() || panel !== objectivePanel) return result;
    objectivePanel = ''; objectiveNotice = result.reason && !result.decision_id ? result.reason : result.decision_id ? 'Judgment request created. Review and sign it in the Inbox before this objective can activate.' : 'Saved.'; await loadObjectives(); return result;
  } catch (error) {
    if (route === objectiveRoute() && panel === objectivePanel) objectiveError = error.message;
  } finally {
    objectivePending.delete(key); renderObjectives();
  }
}
document.addEventListener('input', event => {
  if (event.target.id === 'objective-success' && objectivePanel.startsWith('accept:')) {
    objectiveDrafts.set(objectivePanel, {success_criteria:event.target.value});
  }
});
document.addEventListener('click', async event => {
  const button = event.target.closest?.('[data-objective-action]');
  if (!button || view !== 'today' || button.disabled) return;
  const action = button.dataset.objectiveAction, id = button.dataset.id || '';
  if (action === 'refresh') return loadObjectives();
  if (action === 'close-panel') { objectivePanel = ''; objectiveError = ''; renderObjectives(); return; }
  if (action === 'new' || action === 'scan-panel') { objectivePanel = action === 'new' ? 'new' : 'scan'; renderObjectives(); return; }
  if (action === 'checkpoint' || action === 'review-achieve') { objectivePanel = (action === 'checkpoint' ? 'checkpoint:' : 'achieve:') + id; renderObjectives(); return; }
  if (action === 'review-accept') { objectivePanel = 'accept:' + id; renderObjectives(); document.getElementById('objective-success')?.focus(); return; }
  if (action === 'review-cancel') { objectivePanel = 'cancel:' + id; renderObjectives(); return; }
  if (action === 'meetings') { objectivePanel = 'meetings'; const repos = objectiveData?.repositories || []; objectiveMeetingRepo ||= objectiveData?.detail?.repository || repos[0]?.id || repos[0] || ''; renderObjectives(); try { objectiveMeetings = objectiveMeetingRepo ? await api(objectiveMeetingsURL()) : {meetings:[]}; } catch(error) { objectiveError = error.message; } renderObjectives(); return; }
  const item = [...objectiveProposals(), ...(objectiveData?.objectives || []), objectiveData?.detail].filter(Boolean).find(item => item.id === id);

  if (!item) return;
  if (action === 'dismiss') return objectiveMutate(id, '/api/proposals/' + encodeURIComponent(id) + '/dismiss', {expected_revision:item.revision,request_key:objectiveKey(action + ':' + id)});
  if (['pause','resume','cancel','dispatch'].includes(action)) return objectiveMutate(id, '/api/objectives/' + encodeURIComponent(id) + '/' + action, {expected_revision:item.revision,request_key:objectiveKey(action + ':' + id)});
});
document.addEventListener('submit', async event => {
  const form = event.target;
  if (form.id !== 'objective-accept-form' || view !== 'today') return;
  event.preventDefault();
  if (!form.reportValidity() || !objectiveCanWrite()) return;
  const id = form.dataset.id, item = objectiveProposals().find(item => item.id === id);
  if (!item) return;

  await objectiveMutate(id, '/api/proposals/' + encodeURIComponent(id) + '/accept', {expected_revision:item.revision,request_key:objectiveKey('accept:' + id)});
});

function objectiveField(name, label, {required = true, type = 'text', value = '', rows = 0, max = 4000} = {}) {
  const saved = objectiveDrafts.get(objectivePanel)?.[name] ?? value;
  return `<label>${esc(label)}${rows ? `<textarea name="${esc(name)}" rows="${rows}" maxlength="${max}" ${required ? 'required' : ''}>${esc(saved)}</textarea>` : `<input name="${esc(name)}" type="${esc(type)}" maxlength="${max}" value="${esc(saved)}" ${required ? 'required' : ''}>`}</label>`;
}
function objectiveRepositories() {
  const repositories = objectiveData?.repositories || state.workbench?.repositories || [];
  const selected = objectiveDrafts.get(objectivePanel)?.repository || '';
  return `<label>Repository<select name="repository" required><option value="">Choose a repository</option>${repositories.map(repo => `<option value="${esc(repo.id || repo)}" ${(repo.id || repo) === selected ? 'selected' : ''}>${esc(repo.name || repo.id || repo)}</option>`).join('')}</select></label>`;
}
function objectiveNewForm() {
  const hosts = objectiveData?.capabilities || state.workbench?.hosts || [];
  const selected = objectiveDrafts.get(objectivePanel)?.provider || '';
  return `<section class="objective-editor"><div class="objective-section-head"><h2>Take point on an outcome</h2>${objectiveButton('close-panel','','Cancel')}</div><p class="objective-notice">Choose the outcome and bounds. You can start one bounded private preparation run now. This never authorizes external actions.</p><form id="objective-create-form">${objectiveField('title','Short title',{max:300})}${objectiveField('outcome','Outcome to own',{rows:3,max:2000})}${objectiveRepositories()}<label>Execution harness<select name="provider" required><option value="">Choose explicitly</option>${hosts.map(host => `<option value="${esc(host.id)}" ${host.id === selected ? 'selected' : ''}>${esc(host.name || host.id)}${host.ready ? '' : ' · setup needed'}</option>`).join('')}</select></label>${objectiveField('success_criteria','Evidence that will establish success',{rows:2,max:2000})}${objectiveField('stop_criteria','When should Raven stop or ask you?',{rows:2,max:2000})}${objectiveField('guardrails','Scope and limits',{rows:2,max:2000})}<label class="objective-check"><input type="checkbox" name="start_preparation" ${(objectiveDrafts.get(objectivePanel)?.start_preparation ?? true) ? 'checked' : ''}>Start bounded private preparation with the selected configured harness</label><button class="button" type="submit" ${objectivePending.has('new') ? 'disabled aria-busy="true"' : ''}>Create objective</button></form></section>`;
}
function objectiveScanForm() {
  return `<section class="objective-editor"><div class="objective-section-head"><h2>Find useful next steps</h2>${objectiveButton('close-panel','','Cancel')}</div><p>Inspect connected, permission-scoped evidence for this repository. Suggestions remain proposals until reviewed.</p><form id="objective-scan-form">${objectiveRepositories()}<label class="objective-check"><input type="checkbox" name="retry_failed">Explicitly retry identical inputs after fixing a blocked investigation (authorizes another bounded attempt set)</label><button class="button" type="submit" ${objectivePending.has('scan') ? 'disabled aria-busy="true"' : ''}>Inspect evidence</button></form>${objectiveCanAdmin() ? `<form id="objective-watch-form"><h3>Investigate new evidence automatically</h3>${objectiveRepositories()}<label class="objective-check"><input type="checkbox" name="enabled" ${objectiveDrafts.get(objectivePanel)?.enabled === true ? 'checked' : ''}>Enable a repository watch</label><p>New accessible evidence is read with your configured model; it can propose work but cannot approve or start it. Uncheck to disable.</p><button class="button" type="submit">Save evidence watch</button></form>` : ''}</section>`;
}
function objectiveProgressForm(panel) {
  const [action,id] = panel.split(':');
  return `<section class="objective-editor"><div class="objective-section-head"><h2>${action === 'achieve' ? 'Attest the outcome' : 'Save a checkpoint'}</h2>${objectiveButton('close-panel','','Cancel')}</div><form id="objective-progress-form" data-id="${esc(id)}" data-action="${esc(action)}">${objectiveField('summary',action === 'achieve' ? 'What evidence did you inspect that establishes success?' : 'Progress and the next step',{rows:3})}${action === 'checkpoint' ? objectiveField('waiting_on','Waiting on',{required:false}) + objectiveField('next_check_at','Next check (your local time)',{required:false,type:'datetime-local'}) : '<p class="objective-notice">This records your attestation. It is not labeled independent automated verification, and it does not sign unresolved decisions.</p>'}<button class="button" type="submit" ${objectivePending.has(id) ? 'disabled aria-busy="true"' : ''}>${action === 'achieve' ? 'Record human attestation' : 'Save checkpoint'}</button></form></section>`;
}
function objectiveMeetingPanel() {
  const meetings = Array.isArray(objectiveMeetings) ? objectiveMeetings : objectiveMeetings?.meetings || [];
  const repos = objectiveData?.repositories || [];
  return `<section class="objective-editor"><div class="objective-section-head"><h2>Meeting evidence</h2>${objectiveButton('close-panel','','Close')}</div><label>Repository scope<select id="objective-meeting-repository"><option value="">Choose a repository</option>${repos.map(repo => `<option value="${esc(repo.id || repo)}" ${(repo.id || repo) === objectiveMeetingRepo ? 'selected' : ''}>${esc(repo.name || repo.id || repo)}</option>`).join('')}</select></label><p class="objective-notice">Only share transcripts that every workspace reader is permitted to access. Participant statements are evidence, not authorization. Vexa is a self-hosted attendance adapter; transcript import does not join meetings.</p>${objectiveCanAdmin() ? `<details><summary>Add a meeting or transcript</summary><form id="objective-meeting-create">${objectiveField('title','Meeting title',{max:300})}${objectiveRepositories()}<label>Source<select name="provider" required><option value="import">Transcript import (no attendance)</option><option value="vexa">Vexa (requires server configuration)</option></select></label>${objectiveField('namespace','Source namespace (your configured installation)',{max:200})}<label>Meeting platform<select name="platform"><option value="google_meet">Google Meet</option><option value="teams">Microsoft Teams</option><option value="zoom">Zoom</option></select></label>${objectiveField('native_meeting_id','Native meeting ID',{max:300})}<button class="button" type="submit" ${objectivePending.has('meeting-new') ? 'disabled' : ''}>Create record only</button></form></details>` : '<p class="objective-muted">A workspace administrator manages meeting evidence.</p>'}${objectiveMeetings === null ? '<p role="status">Loading meeting records…</p>' : !meetings.length ? '<p class="objective-muted">No meeting records.</p>' : meetings.map(meeting => meeting.withheld ? '<article class="objective-card"><h3>Meeting evidence withheld</h3><p>Local disclosure was revoked. This does not establish that a remote bot stopped; an administrator must inspect the capture status.</p></article>' : `<article class="objective-card"><h3>${esc(meeting.title)}</h3><p class="objective-label">${esc(meeting.state || 'Unknown state')} · ${esc(meeting.provider)}</p><p class="objective-muted">Remote status: ${esc(meeting.remote_state || 'Not established')}. Local revocation does not prove a remote bot stopped.</p>${meeting.last_error ? `<p class="objective-error">${esc(meeting.last_error)}</p>` : ''}${objectiveCanAdmin() ? `<details><summary>Consent and controls</summary><form class="objective-meeting-consent" data-id="${esc(meeting.id)}"><label class="objective-check"><input type="checkbox" name="notice_confirmed" required> I have confirmed the required participant notice and consent for this meeting.</label><label class="objective-check"><input type="checkbox" name="join" required> Allow a visible bot to join this meeting.</label><label class="objective-check"><input type="checkbox" name="transcribe" required> Allow transcription for this meeting.</label><p class="objective-muted">Audio/video recording is not requested. Consent is separate from joining.</p><button class="button" type="submit" ${objectivePending.has(meeting.id) ? 'disabled' : ''}>Record consent</button></form><form class="objective-meeting-import" data-id="${esc(meeting.id)}">${objectiveField('source_version','Export version or revision',{max:200})}${objectiveField('body','Transcript text (up to 1,000,000 characters; stored in versioned chunks)',{rows:4,max:1000000})}<label class="objective-check"><input type="checkbox" name="workspace_shared" required> Every workspace reader is permitted to access this transcript.</label><button class="button" type="submit" ${objectivePending.has(meeting.id) ? 'disabled' : ''}>Import transcript</button></form>${meeting.provider === 'vexa' ? `<p class="objective-muted">Background transcript sync: ${esc(meeting.background_sync?.status || 'disabled')}. Capture ${meeting.transcript_complete ? 'complete' : 'not established complete'}.</p><form class="objective-meeting-background" data-id="${esc(meeting.id)}"><label class="objective-check"><input type="checkbox" name="enabled" ${meeting.background_sync?.enabled ? 'checked' : ''}> Automatically observe this consented meeting and import its transcript after joining.</label><label class="objective-check"><input type="checkbox" name="workspace_shared"> Every workspace reader may access this transcript. I authorize periodic background status and transcript reads.</label><p class="objective-muted">No automatic join or recording. Disable sync to stop further reads; use Stop remote bot separately to leave the meeting.</p><button class="button" type="submit" ${objectivePending.has(meeting.id) ? 'disabled' : ''}>Save background sync</button></form><form class="objective-meeting-sync" data-id="${esc(meeting.id)}"><label class="objective-check"><input type="checkbox" name="workspace_shared" required> Every workspace reader may access the provider transcript.</label><button class="button" type="submit" ${objectivePending.has(meeting.id) ? 'disabled' : ''}>Fetch shared transcript</button></form><form class="objective-meeting-join" data-id="${esc(meeting.id)}"><label>Meeting URL (sent only for this request)<input name="meeting_url" type="url" required autocomplete="off"></label><label>Meeting passcode, if required<input name="passcode" type="password" autocomplete="off"></label><p class="objective-notice">Join starts a real, visible meeting bot. Only proceed with valid participant consent.</p><button class="button" type="submit" ${objectivePending.has(meeting.id) ? 'disabled' : ''}>Join with recorded consent</button></form>` : ''}<div class="objective-actions">${meeting.provider === 'vexa' ? objectiveButton('meeting-stop',meeting.id,'Stop remote bot') : ''}${objectiveButton('meeting-revoke',meeting.id,'Revoke local access')}${meeting.provider === 'vexa' ? objectiveButton('meeting-reconcile',meeting.id,'Refresh remote status') : ''}${meeting.provider === 'vexa' ? objectiveButton('meeting-confirm-delete',meeting.id,'Delete remote data', !['completed','failed'].includes(meeting.state)) : ''}</div></details>` : ''}</article>`).join('')}</section>`;
}
function objectiveFormValues(form) {
  const data = {};
  for (const element of form.querySelectorAll('input[name],textarea[name],select[name]')) {
    data[element.name] = element.type === 'checkbox' ? element.checked : element.value;
  }
  return data;
}
document.addEventListener('input', event => {
  if (view !== 'today' || !objectivePanel) return;
  const form = event.target.closest?.('form');
  // Never preserve meeting credentials or consent checkboxes across navigation.
  if (form && !form.className?.startsWith('objective-meeting') && form.id?.startsWith('objective-')) objectiveDrafts.set(objectivePanel,objectiveFormValues(form));
});
document.addEventListener('change', event => {
  if (view !== 'today' || !objectivePanel) return;
  const form = event.target.closest?.('form');
  if (form && !form.className?.startsWith('objective-meeting') && form.id?.startsWith('objective-')) objectiveDrafts.set(objectivePanel,objectiveFormValues(form));
});
async function objectiveMeetingMutate(id, action, data) {
  if (!objectiveCanAdmin() || objectivePending.has(id)) return;
  objectivePending.add(id); objectiveError = ''; renderObjectives();
  try {
    const meeting = (Array.isArray(objectiveMeetings) ? objectiveMeetings : objectiveMeetings?.meetings || []).find(item => item.id === id);
    if (meeting) data = {...data,repo:meeting.repo};
    await api('/api/meetings' + (action ? '/' + encodeURIComponent(id) + '/' + action : ''),data);
    if (!action && data.repo) objectiveMeetingRepo = data.repo;
    objectiveMeetings = await api(objectiveMeetingsURL()); objectiveNotice = 'Meeting record updated. Check the reported remote status below.';
  } catch(error) { objectiveError = error.message; }
  finally { objectivePending.delete(id); renderObjectives(); }
}
document.addEventListener('submit', async event => {
  const form = event.target;
  if (view !== 'today' || !(form.id?.startsWith('objective-') || form.className?.startsWith('objective-meeting')) || form.id === 'objective-accept-form') return;
  event.preventDefault();
  if (!form.reportValidity() || !objectiveCanWrite()) return;
  const data = objectiveFormValues(form), id = form.dataset.id;
  if (form.id === 'objective-create-form') return objectiveMutate('new','/api/objectives',{...data,start_preparation:data.start_preparation === true || data.start_preparation === 'on',request_key:objectiveKey('new'),source_ids:[]});
  if (form.id === 'objective-watch-form') return objectiveMutate('watch','/api/objectives/watch',{repository:data.repository,enabled:data.enabled === true || data.enabled === 'on',request_key:objectiveKey('watch')});
  if (form.id === 'objective-scan-form') return objectiveMutate('scan','/api/objectives/scan',{...data,retry_failed:data.retry_failed === true,request_key:objectiveKey('scan')});
  if (form.id === 'objective-progress-form') {
    const item = objectiveData.detail?.id === id ? objectiveData.detail : objectiveData.objectives.find(item => item.id === id);
    if (!item) return;
    if (data.next_check_at) data.next_check_at = new Date(data.next_check_at).toISOString();
    return objectiveMutate(id,'/api/objectives/' + encodeURIComponent(id) + '/' + form.dataset.action,{...data,expected_revision:item.revision,request_key:objectiveKey(form.dataset.action + ':' + id)});
  }
  if (form.id === 'objective-meeting-create') { data.repo = data.repository; delete data.repository; return objectiveMeetingMutate('meeting-new','',data); }
  if (form.className === 'objective-meeting-consent') return objectiveMeetingMutate(id,'consent',{...data,record:false});
  if (form.className === 'objective-meeting-import') return objectiveMeetingMutate(id,'import',data);
  if (form.className === 'objective-meeting-background') return objectiveMeetingMutate(id,'background-sync',{enabled:data.enabled === 'on' || data.enabled === true,workspace_shared:data.workspace_shared === true || data.workspace_shared === 'on'});
  if (form.className === 'objective-meeting-sync') return objectiveMeetingMutate(id,'sync',data);
  if (form.className === 'objective-meeting-join') return objectiveMeetingMutate(id,'join',{options:data});
});
document.addEventListener('click', async event => {
  const button = event.target.closest?.('[data-objective-action]');
  if (!button || view !== 'today' || button.disabled) return;
  const action = button.dataset.objectiveAction;
  if (action === 'meeting-confirm-delete') {
    const meeting = (Array.isArray(objectiveMeetings) ? objectiveMeetings : objectiveMeetings?.meetings || []).find(item => item.id === button.dataset.id);
    if (!meeting || !['completed','failed'].includes(meeting.state)) return;
    if (!window.confirm('Delete the remote transcript and recording for this meeting? This cannot be undone.')) return;
    return objectiveMeetingMutate(button.dataset.id,'delete-remote',{});
  }
  if (['meeting-stop','meeting-revoke','meeting-reconcile'].includes(action)) {
    return objectiveMeetingMutate(button.dataset.id, action.slice(8),{});
  }
});

document.addEventListener('change', async event => {
  if (view !== 'today' || event.target.id !== 'objective-meeting-repository') return;
  const repo = event.target.value; objectiveMeetingRepo = repo; objectiveMeetings = null; renderObjectives();
  try { const result = repo ? await api(objectiveMeetingsURL()) : {meetings:[]}; if (objectiveMeetingRepo === repo) objectiveMeetings = result; } catch(error) { if (objectiveMeetingRepo === repo) objectiveError = error.message; }
  renderObjectives();
});
