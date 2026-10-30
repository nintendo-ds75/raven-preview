'use strict';

// The task page a Slack message links to. The link's token is in the
// fragment, which the browser never sends; the page passes it to the API
// as a header, and nothing is stored in the browser.

const token = location.hash.slice(1);
if (window.RavenVoice) RavenVoice.configure({link: token});
// A link pasted into a tab that already shows a task page only moves the
// fragment, and the page went on as the person the old link named: every
// action carried their link. A new link is a new page.
addEventListener('hashchange', () => { const next = location.hash.slice(1); if (next.startsWith('rvn_') && next !== token) location.reload(); });
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
// Text a person reads and may copy from: a snake_case name (a metric, a
// flag) is code, and on a phone it breaks after an underscore, between
// its words, never mid-word. Measured at 390px:
// "prometheus_tsdb_out_of_order_samples_tota / l".
const prose = value => esc(value).replace(/\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+){2,}\b/g,
  name => `<code class="brief-ident">${name.replace(/_/g, '_<wbr>')}</code>`);
const initials = name => (name || '?').split(/\s+/).map(word => word[0]).slice(0, 2).join('');
const avatar = name => name ? `<span class="avatar">${esc(initials(name))}</span>` : '<span class="avatar empty" aria-hidden="true"></span>';
const pill = (text, color = '') => `<span class="pill ${color}"><span class="status-dot"></span>${esc(text)}</span>`;
const when = at => at ? new Date(at).toLocaleString([], {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'}) : '';
const day = at => at ? new Date(at).toLocaleDateString([], {month:'long', day:'numeric'}) : '';
const plural = (count, one, many = one + 's') => `${count} ${count === 1 ? one : many}`;
const same = (a, b) => (a || '').toLowerCase() === (b || '').toLowerCase();
const unique = names => names.filter(Boolean).filter((v, i, a) => a.findIndex(x => same(x, v)) === i);
const listed = names => names.length < 3 ? names.join(' and ') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
// The app's words for the same states, so the page and the app agree.
const stateLabels = {signed:['Decided','green'], waiting:['Waiting for a person',''], needs_review:['Needs another look',''],
  resolved:['Resolved from evidence, not signed','gray'], proposed:['Prediction, not signed','gray'], assumed:['Default assumed, not signed','gray'],
  suggested:['Follow-up suggested','lilac'], adopted:['Adopted by the agent','gray'], duplicate:['Same as another decision','gray'],
  pending:['Waiting for a person',''], answered:['Answered','green']};
// Events that say the agent is working, not that a person did something.
// A run of them reads as one line, so the answers stay easy to find.
const quietEvents = {node_added: n => `Coding agent raised ${plural(n, 'decision')}`, followup_added: n => `${plural(n, 'follow-up question')} added`,
  node_settled: n => `Coding agent settled ${plural(n, 'decision')}`, run_updated: n => plural(n, 'status change')};
const keyEvents = new Set(['owner_approved', 'signoff', 'answer_corrected', 'task_finished']);
const HISTORY_SHOWN = 8;
const QUOTE_SHOWN = 360;
// One layout per width, so reading order and keyboard order match what
// is on screen: on a phone the decision and the place to add context come
// before the record; on a desktop the context box sits beside it.
const narrow = matchMedia('(max-width: 900px)');
const DRAFTS = ['answer-text', 'answer-why', 'note-text', 'handon-note', 'handon-other', 'inquiry-text'];
// One answer for a link that does not open, whatever the cause: a cut-off
// paste was told the link "expired or was revoked".
const NOT_VALID = 'This link isn’t valid: it may be incomplete, expired or revoked.';
// Every link Raven sends is "rvn_" and 32 more characters. A fragment of
// another length was cut off on the way, and asking Slack for a new link
// for it sends nothing: say so, and do not offer to.
const WELL_FORMED = /^rvn_[A-Za-z0-9_-]{32}$/;
const SEEN = 'The coding agent sees it the next time it checks this task.';

let data = null, error = '', errorStatus = 0, historyAll = false, resent = '';
// The last read as it came: the quiet refresh redraws only when it differs.
let lastRead = '';
// Acknowledgment belongs to the exact evidence and decision last rendered,
// not the fresh response assigned to data before render replaces the DOM.
let renderedSourceReviewKey = '';
// A decision opened from the list; '' is the one the link names.
let focusId = '';
const inquiryViews = new Map();
const inquiryView = () => { const id = data?.focus?.node_id || ''; if (!inquiryViews.has(id)) inquiryViews.set(id, {question:'', answer:null, error:'', pending:false}); return inquiryViews.get(id); };

async function call(path, body) {
  const response = await fetch(path, {method: body ? 'POST' : 'GET', headers: {'X-Raven-Link': token, ...(body ? {'Content-Type': 'application/json'} : {})},
    body: body ? JSON.stringify(body) : undefined, credentials: 'same-origin'});
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw Object.assign(new Error(value.error || `Request failed (${response.status})`), {status: response.status});
  return value;
}

function toast(message) {
  const el = $('#toast');
  el.textContent = message; el.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { el.hidden = true; }, 4000);
}

async function load({quiet = false} = {}) {
  if (!token) { error = 'This page opens from the link in your Raven message, and the link is missing.'; return render(); }
  // Never redraw over something the person is writing.
  if (quiet && document.activeElement && ['TEXTAREA','INPUT','SELECT'].includes(document.activeElement.tagName)) return;
  try {
    const fresh = await call('/api/brief' + (focusId ? '?focus=' + encodeURIComponent(focusId) : ''));
    const read = JSON.stringify(fresh);
    // Nothing changed: no redraw. Every redraw rebuilt the page, and the
    // quiet refresh did it every 30 seconds.
    if (quiet && read === lastRead && !error) return;
    data = fresh; lastRead = read; error = ''; errorStatus = 0;
  }
  catch (e) { errorStatus = e.status || 0; error = e.status === 401 ? NOT_VALID : e.message; }
  render();
}

function stateOf(n) {
  if (n.raw_status === 'suggested' && n.blocking) return pill('Required follow-up', 'lilac');
  const [label, color] = stateLabels[n.state] || [n.state, 'gray']; return pill(label, color);
}

// Who a blocking decision still waits on: its owner and the signers it
// needs, less those who have signed.
const owedBy = n => unique([n.owner, ...(n.required_signers || [])]).filter(x => !(n.signatures || []).some(s => same(s, x)));
const counted = n => !['duplicate', 'adopted'].includes(n.raw_status) && (n.raw_status !== 'suggested' || n.blocking);

// Where the person stands on the decision they were sent: it waits on
// them, it wants their sign-off, they have already decided it (a
// correction is still possible, and tucked away), or they may only read.
function focusStanding(f) {
  const node = data.nodes.find(n => n.node_id === f.node_id) || {};
  // They said it is not theirs: it waits on the person they named, even
  // where their standing would let them answer it.
  if (f.handed_to) return 'handed';
  if (!f.can_act) return 'read';
  if (f.status === 'pending') return 'answer';
  if (node.signed_by_me) return 'mine';
  return ['signed', 'rule'].includes(f.signoff) ? 'cosign' : 'sign';
}

// A long quote shows its start; the rest is one click away, never cut off.
// Its key keeps it open through a redraw.
function quote(text) {
  if (!text) return '';
  const long = text.length > QUOTE_SHOWN;
  return `<blockquote class="brief-quote${long ? ' is-clamped' : ''}"${long ? ` data-keep="more-${esc(keyOf(text))}"` : ''}>${prose(text)}</blockquote>${long ? '<button type="button" class="button text brief-more" data-action="more">Show more</button>' : ''}`;
}

// A short stable key for a piece of text, for sections a redraw rebuilds.
function keyOf(text) {
  let h = 0;
  for (const ch of String(text)) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return h.toString(36);
}

function foundList(items) {
  return `<ul class="brief-found">${items.map(i => `<li class="is-${esc(i.kind)}">
    <p>${esc(i.lead)}${i.question ? ` <span class="brief-found-q">“${prose(i.question)}”</span>` : ''}</p>
    ${quote(i.quote)}${i.note ? `<p class="brief-found-note">${esc(i.note)}</p>` : ''}</li>`).join('')}</ul>`;
}

function reasonBlock(why, id) {
  if (!why || !why.summary) return '';
  return `<div class="brief-reason"><span class="label">${esc(why.heading)}</span><p>${prose(why.summary)}</p>
    ${(why.details || []).length ? `<details class="brief-why" data-keep="why-${esc(id)}"><summary>Details</summary><ul>${why.details.map(x => `<li>${prose(x)}</li>`).join('')}</ul></details>` : ''}</div>`;
}

// The options are a choice with a visible circle: on a phone, chips
// stacked as full-width boxes read as a filled-in field above the real one.
function optionList(f) {
  const options = f.options || [];
  if (!options.length) return '';
  return `<fieldset class="brief-options"><legend>Pick one, or write your own</legend>
    ${options.map((o, i) => `<label class="brief-option" data-value="${esc(o)}"><input type="radio" name="answer-option" id="answer-option-${i}" value="${esc(o)}"><span>${esc(o)}</span></label>`).join('')}</fieldset>`;
}

// Handing the decision on, as `not me @person` does in Slack. Tucked
// under the form: most people answer. From a link it is for this question
// only, and the form says what it leaves alone before it is sent.
function handonForm(f) {
  if (!f.can_refer) return '';
  const near = (f.handon || []).filter(x => x.covers && !x.handed_you), far = (f.handon || []).filter(x => !x.covers && !x.handed_you);
  const back = (f.handon || []).filter(x => x.handed_you);
  const opt = x => `<option value="${esc(x.id)}">${esc(x.name)} · ${esc(x.why)}</option>`;
  const where = (f.paths || []).filter(p => p && p !== 'unknown')[0];
  // People the listings name whom Raven cannot message: shown, so the
  // list does not read as everyone there is, and explained when picked.
  const unknown = f.handon_unknown || [];
  return `<details class="brief-why brief-handon" id="handon" data-keep="handon-${esc(f.node_id)}"><summary>Not mine · hand it on</summary>
    <form id="handon-form" class="brief-handon-form" data-id="${esc(f.node_id)}" data-rev="${esc(f.updated_at)}">
      <label for="handon-person">Hand it to</label>
      <select id="handon-person" required aria-describedby="handon-help"><option value="">Choose a person</option>
        ${near.length ? `<optgroup label="${esc(where ? 'Named for ' + where : 'Named for this question')}">${near.map(opt).join('')}</optgroup>` : ''}
        ${far.length ? `<optgroup label="Other people">${far.map(opt).join('')}</optgroup>` : ''}
        ${back.length ? `<optgroup label="Handed it to you">${back.map(opt).join('')}</optgroup>` : ''}
        ${unknown.length ? `<optgroup label="Listed for this code, not in Raven yet">${unknown.map((x, i) => `<option value="unknown:${i}">${esc(x.name)} · not in Raven yet</option>`).join('')}</optgroup>` : ''}
        <option value="other">Someone else…</option></select>
      <p class="brief-field-help" id="handon-help" hidden></p>
      <label for="handon-other" class="brief-handon-other" hidden>Their name</label>
      <input id="handon-other" class="brief-handon-other" hidden maxlength="200" placeholder="Full name, email or GitHub login" autocomplete="off">
      <label for="handon-note">Why them <span class="muted">(shown to them)</span></label>
      <textarea id="handon-note" class="brief-grow" rows="2" maxlength="300" placeholder="They own this part of the code this quarter."></textarea>
      <p class="error" id="handon-error" role="alert" hidden></p>
      <div class="brief-answer-foot"><button class="button" type="submit">Hand it on</button><span class="brief-answer-note brief-handon-scope" id="handon-scope">For this question only.</span></div>
    </form></details>`;
}

function sourceReview(f) {
  const r = f.source_revalidation;
  if (!r || (!r.has_reliance && !r.sources?.length && !r.dependencies?.length)) return '';
  const sources = (r.sources || []).map(s => {
    const {body, ...metadata} = s.snapshot || {};
    const key = JSON.stringify(['source', f.node_id, s.record_id, s.source_version_id, s.role]);
    return `<details class="brief-why source-snapshot" data-keep="${esc(key)}" open><summary>${esc(s.snapshot?.title || s.record_id)} · ${esc(s.role)}</summary><p>${esc(s.snapshot?.author || 'Author not supplied')} · ${esc(s.snapshot?.status || 'Status not supplied')}</p><pre class="brief-source-body">${esc(body || '')}</pre><p class="muted">Record ID: ${esc(s.record_id)}<br>Exact source version: ${esc(s.source_version_id)}<br>Role: ${esc(s.role)}</p><details class="brief-why" data-keep="${esc(key + ':metadata')}"><summary>Complete source metadata</summary><pre class="brief-source-body">${esc(JSON.stringify(metadata, null, 2))}</pre></details></details>`;
  }).join('');
  const dependencies = (r.dependencies || []).map(s => {
    const key = JSON.stringify(['source-decision', f.node_id, s.decision_id, s.source_version_id,
      s.source_snapshot_sha256 || s.reviewed_snapshot || s]);
    return `<details class="brief-why" data-keep="${esc(key)}"><summary>${s.historical ? 'Historical decision' : 'Source decision'}: ${esc(s.question)}</summary><p>${prose(s.answer)}</p><pre class="brief-source-body">${esc(JSON.stringify(s.reviewed_snapshot || s, null, 2))}</pre></details>`;
  }).join('');
  return `<section class="brief-source-review"><h3>Evidence for this answer</h3><p>${esc(r.notice)}</p>${r.retires_rule ? '<p>Reapproving changed evidence retires the old standing rule. This signature applies here.</p>' : ''}${sources}${dependencies}${r.has_reliance ? (r.available ? '<label><input id="brief-source-confirm" type="checkbox" required> I reviewed the sources and source decisions shown here. Bind my answer to these exact revisions.</label>' : '<p>Current evidence is unavailable. Signing waits until it can be reviewed.</p>') : '<p>These context and work-item snapshots are informational. They do not require source revalidation.</p>'}</section>`;
}

function sourceAcknowledgmentKey(f) {
  if (!f?.source_revalidation?.available || !f.source_revalidation.has_reliance) return '';
  // Exact serialized values, not a short display hash. Any changed displayed
  // scope or source snapshot requires another deliberate acknowledgment.
  return JSON.stringify(f);
}

function workItemContext(f) {
  const a = f.work_item_association;
  const sources = a?.links || [...(f.source_anchors || []).map(s => ({...s, origin:'task'})),
    ...(f.sources || []).filter(s => ['work_item','context'].includes(s.role)).map(s => ({...s, origin:'decision'}))];
  if (!a?.declared && !sources.length) return '';
  const warning = a?.historical && ['unlinked','ambiguous'].includes(a.status)
    ? '<p>Historical declaration on a closed task. Preserve its recorded history; start a new task for a new association.</p>'
    : a?.status === 'unlinked' ? '<p>This declared ID has no explicit work-item link. Historical links are not reconstructed from text.</p>'
    : a?.status === 'ambiguous' ? '<p>Multiple linked records match this ID. Inspect the canonical identities below.</p>' : '';
  return `<details class="brief-why work-item-context" open><summary>Work-item context${a?.declared ? ` · ${esc(a.declared)} · ${a.historical ? 'Historical ' : ''}${esc(a.status)}` : ''}</summary>${warning}${sources.map(s => `<p data-work-item-origin="${esc(s.origin)}">${s.origin === 'decision' ? 'Decision association' : 'Task association'} · ${esc(s.ref)} · ${esc(s.role)} · ${s.current ? 'current observed version' : 'historical or unavailable version'}<br>${esc(s.provider)} / ${esc(s.namespace)} / ${esc(s.kind)} / ${esc(s.external_id)}<br>Record ID: ${esc(s.record_id)}<br>Recorded source version: ${esc(s.source_version_id)}</p>`).join('')}</details>`;
}

function focusCard(f) {
  if (!f) return '';
  const standing = focusStanding(f);
  const open = standing === 'answer', mine = standing === 'mine';
  const cosign = standing === 'cosign';
  const options = optionList(f);
  // A sign-off as it stands keeps no reason, so beside one the reason box
  // is for a correction and opens with it: a reason typed next to a plain
  // sign-off was dropped without a word.
  const why = open || mine ? '' : ' data-correction hidden';
  // The interview is another way to the same answer, beside the button.
  const interview = !['read','handed'].includes(standing) && f.owner && (!data.viewer.interview_decision_id || data.viewer.interview_decision_id === f.node_id)
    ? `<button type="button" class="button text brief-interview" data-action="interview-start" data-task="${esc(data.task.id)}" data-id="${esc(f.node_id)}">Take a quick call instead</button>` : '';
  const fields = `<form id="answer-form" class="brief-answer" data-id="${esc(f.node_id)}" data-rev="${esc(f.updated_at)}">
      ${open ? options : ''}
      <label for="answer-text">${open ? (options ? 'Your answer <span class="muted">(edit the option, or write your own)</span>' : 'Your answer') : mine ? 'Corrected answer' : 'Correct the answer <span class="muted">(leave empty to sign it as it stands)</span>'}</label>
      <textarea id="answer-text" class="brief-grow" maxlength="12000" ${open || mine ? 'required' : ''} placeholder="${open ? 'State the decision the agent should follow.' : mine ? 'The answer the agent should follow instead.' : 'Only if the answer above is wrong.'}"></textarea>
      <label for="answer-why"${why}>Why <span class="muted">(saved for next time)</span></label>
      <textarea id="answer-why" class="short brief-grow" maxlength="4000" placeholder="The reason, constraint or policy behind it."${why}></textarea>
      ${sourceReview(f)}
      <p class="error" id="answer-error" role="alert" hidden></p>
      <div class="brief-answer-foot"><button class="button ${open || standing === 'sign' ? 'primary' : ''}" type="submit"${open || mine ? '' : ` data-plain="${cosign ? 'Add my signature' : 'Confirm'}"`}>${open ? 'Send answer' : mine ? 'Save correction' : cosign ? 'Add my signature' : 'Confirm'}</button><span class="brief-answer-note">Answering as ${esc(data.viewer.name)}. Raven reuses it the next time this comes up.</span>${interview}</div>
    </form>`;
  const form = standing === 'handed' ? `<p class="brief-cannot">You handed this on to ${esc(f.handed_to)}. It now waits on them.</p>`
    : standing === 'read' ? `<p class="brief-cannot">${esc(f.why_not ? 'You can read this, but not answer it: ' + f.why_not + '.' : 'You can read this decision.')}</p>`
    : mine ? `<details class="brief-why brief-correct" data-keep="correct-${esc(f.node_id)}"><summary>Correct my answer</summary>${fields}</details>` : fields + handonForm(f);
  const kicker = {answer: 'Waiting on you', sign: 'Raven would like you to confirm', cosign: 'You were asked about', mine: 'You decided this', read: f.asked ? 'You were asked about' : 'On this task', handed: 'You handed this on'}[standing];
  const paths = (f.paths || []).filter(p => p && p !== 'unknown');
  const found = f.found || [];
  const back = !f.asked && data.asked_focus ? '<button type="button" class="button text brief-back" data-action="focus-asked">Back to the decision you were asked about</button>' : '';
  // Reading order, measured at 390px: the question, the brief (or the
  // agent's context), the answer on the table, then the form, all on the
  // first screen; the consequences and the paths follow the form, and
  // what Raven checked, why it came to them and the recorded scope open
  // on request below. Before, the scope table and the work-item record
  // sat between the question and the brief, and the form began a screen
  // and a half down.
  const sameAsBrief = f.brief && f.context && f.brief.trim().toLowerCase() === f.context.trim().toLowerCase();
  return `<section class="task-panel brief-focus is-${standing}" id="focus" aria-labelledby="focus-question">
    ${back}<p class="brief-kicker">${kicker}</p>
    <h2 id="focus-question" tabindex="-1">${esc(f.question)}</h2>
    ${f.brief ? `<p class="brief-lead">${prose(f.brief)}</p>` : ''}
    ${f.objective && typeof objectiveScopeCard === 'function' ? `<div class="brief-block"><span class="label">Proposed scope</span>${objectiveScopeCard({...f.objective, link_to_today: ''}, `brief-${f.node_id}`)}</div>` : f.context && !sameAsBrief ? `<div class="brief-block"><span class="label">Context from the coding agent</span><p>${prose(f.context)}</p></div>` : ''}
    ${f.answer ? `<div class="brief-block is-answer"><span class="label">${mine ? 'Your answer' : (['signed', 'rule'].includes(f.signoff) ? 'Signed answer' : 'Answer on the table') + (f.answered_by ? ' · ' + esc(f.answered_by) : '')}</span><p>${prose(f.answer)}</p>${f.rationale ? `<p class="brief-why-line">Why: ${prose(f.rationale)}</p>` : ''}</div>` : ''}
    ${f.prediction ? `<div class="brief-block is-guess"><span class="label">Your earlier answer · a guess</span><p>${prose(f.prediction)}</p></div>` : ''}
    ${form}
    ${f.consequences ? `<div class="brief-block"><span class="label">Consequences</span><p>${prose(typeof f.consequences === 'string' ? f.consequences : JSON.stringify(f.consequences))}</p></div>` : ''}
    ${found.length ? `<details class="brief-why brief-checked" data-keep="found-${esc(f.node_id)}"><summary>What Raven checked</summary>${foundList(found)}</details>` : ''}
    <details class="brief-extra brief-scope" data-keep="recorded-scope-${esc(f.node_id)}"><summary>Details</summary>
    ${paths.length ? `<p class="brief-where">Touches ${paths.map(p => `<code>${esc(p)}</code>`).join(', ')}</p>` : ''}
    ${reasonBlock(f.why, f.node_id)}
    ${approvalScope(f)}
    ${workItemContext(f)}
    </details>
  </section>`;
}

// The request as the task recorded it. Raven's kickoff verdict is for the
// agent: on a page that asks for a decision, "pass" read as "you are not
// needed".
function requesterCard(t, r) {
  const about = [r.teams?.join(', ') || r.team, r.github ? '@' + r.github : '', r.known === false && r.name ? 'not in this workspace’s people list' : ''].filter(Boolean);
  const facts = [...(t.agent ? [['Agent', t.agent]] : []), ...Object.entries(t.facts || {})];
  return `<section class="task-panel brief-request" aria-labelledby="request-title"><h2 id="request-title">The request</h2>
    <div class="brief-requester">${avatar(r.name)}<div><strong>${esc(r.name || 'Requester not recorded')}</strong>${about.length ? `<span>${about.map(esc).join(' · ')}</span>` : ''}</div></div>
    <blockquote class="brief-prompt"><p>${esc(t.goal || t.title)}</p></blockquote>
    ${facts.length ? `<dl class="brief-facts">${facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>` : ''}
  </section>`;
}

function decisionsCard(nodes) {
  const me = data.viewer.name;
  const shown = nodes.filter(n => !['duplicate','adopted'].includes(n.raw_status));
  const you = x => same(x, me) ? 'You' : x;
  // The state, then the people it is about: who signed, or who it waits on.
  const who = n => {
    if (n.raw_status === 'suggested' && n.blocking) return 'The coding agent must adopt this';
    if (n.signatures.length) return n.signatures.map(you).join(', ');
    const owed = owedBy(n).map(you).sort((a, b) => (b === 'You') - (a === 'You'));
    if (n.blocking) return owed.length ? owed.join(', ') : 'No owner yet';
    return n.owner ? you(n.owner) : 'No owner yet';
  };
  const focus = data.focus?.node_id;
  // The one decision on this task is the card above; a list that only pointed back to it says nothing.
  if (shown.length === 1 && shown[0].node_id === focus) return '';
  return `<section class="task-panel" aria-labelledby="decisions-title"><h2 id="decisions-title">Decisions on this task</h2>
    ${shown.length ? `<ol class="brief-decisions">${shown.map(n => n.node_id === focus
      // The decision the card above is about, said once.
      // A button, not a #fragment link: the fragment holds the token, and
      // a link to an anchor replaced it, so a reload lost the page.
      ? `<li class="depth-${Math.min(n.depth || 0, 4)} is-focus" id="d-${esc(n.node_id)}"><div class="brief-decision-top">${stateOf(n)}<span>${esc(who(n))}</span></div><button type="button" class="button text brief-decision-here" data-action="to-focus">The decision above</button></li>`
      : `<li class="depth-${Math.min(n.depth || 0, 4)}" id="d-${esc(n.node_id)}">
      <div class="brief-decision-top">${stateOf(n)}<span>${esc(who(n))}</span></div>
      <strong>${esc(n.question)}</strong>
      ${n.answer ? `<p>${prose(n.answer)}</p>` : ''}${n.rationale && n.answer ? `<p class="brief-why-line">Why: ${prose(n.rationale)}</p>` : ''}
      ${n.needs_review ? `<p class="task-warning">${esc(n.review_reason || 'An answer this relied on changed.')}</p>` : ''}
      ${n.can_act && !n.handed_to ? `<button class="button small" data-action="focus" data-id="${esc(n.node_id)}">${n.action === 'answer' ? 'Answer' : n.signatures.some(x => same(x, me)) ? 'Correct my answer' : 'Confirm'}</button>` : ''}
    </li>`).join('')}</ol>` : '<p class="task-empty">No decisions yet.</p>'}
  </section>`;
}

// Each question once per person, with where it stands for them now. A
// person is often asked and then decides, or asked and then hands it on;
// listing the question under each step said it two or three times.
function personQuestions(c) {
  const rows = new Map();
  const row = (question, key = question) => { if (!rows.has(key)) rows.set(key, {question}); return rows.get(key); };
  c.asked.forEach(q => row(q));
  c.handed_on.forEach((h, i) => { row(h.question, h.question || 'handoff-' + i).to = h.to; });
  c.waiting_on.forEach(q => { row(q).owes = true; });
  c.decided.forEach(d => {
    // The server says who decided (briefing.decided_by): the person whose
    // answer it is. A signature on someone else's answer is "Signed". The
    // app's People list counts the same way.
    const r = row(d.question);
    r.answer = d.answer;
    r.decided = r.decided || !d.signed;
  });
  return [...rows.values()].map(r =>
    r.answer !== undefined ? {...r, state: r.decided ? 'Decided' : 'Signed', tone: 'is-done'} :
    r.owes ? {...r, state: 'Waiting on', tone: 'is-owes'} :
    r.to ? {...r, state: 'Handed on', detail: 'to ' + r.to, tone: 'is-moved'} : {...r, state: 'Asked', tone: ''});
}

// A person's line says where they stand ("Decided 1 · Waiting on 1"); the
// questions, whole, open beneath it. The questions are on the page
// already, and cut to two lines here they ended mid-word.
function peopleCard(contacts) {
  const me = data.viewer.name;
  return `<section class="task-panel brief-people" aria-labelledby="people-title"><h2 id="people-title">People involved</h2>
    ${contacts.length ? `<ul class="brief-person-list">${contacts.map((c, i) => {
      const qs = personQuestions(c);
      const counts = [];
      for (const q of qs) { const hit = counts.find(x => x.state === q.state); if (hit) hit.n++; else counts.push({state: q.state, tone: q.tone, n: 1}); }
      return `<li class="brief-person">${avatar(c.name)}<div>
      <div class="brief-person-head"><strong>${esc(c.name)}${same(c.name, me) ? ' <span class="muted">(you)</span>' : ''}</strong><small class="${c.delivery_failed ? 'is-failed' : ''}">${c.messages ? plural(c.messages, 'message') + ' sent' : 'Not messaged'}${c.delivery_failed ? ' · a delivery failed' : ''}</small></div>
      ${qs.length ? `<details class="brief-asks-toggle" data-keep="person-${esc(keyOf(c.name))}"><summary>${counts.map(x => `<span class="brief-state ${x.tone}">${esc(x.state)} ${x.n}</span>`).join('<span class="brief-dot" aria-hidden="true">·</span>')}</summary>
        <ul class="brief-asks">${qs.map(q => `<li class="${q.tone}"><p class="brief-ask-q"><span class="brief-state">${esc(q.state)}${q.detail ? ' ' + esc(q.detail) : ''}${q.question ? ':' : ''}</span>${q.question ? ' ' + prose(q.question) : ''}</p>${q.answer ? `<p class="brief-ask-a">${prose(q.answer)}</p>` : ''}</li>`).join('')}</ul></details>` : ''}
    </div></li>`;
    }).join('')}</ul>` : '<p class="brief-intro">Nobody has been contacted yet.</p>'}
  </section>`;
}

// Oldest first, as the task's history reads in the app. A run of the
// agent's own bookkeeping folds into one line, and notes one person adds
// in a row share one heading.
function historyEntries(history) {
  const entries = [];
  for (const h of [...history].reverse()) {
    const last = entries[entries.length - 1];
    const joins = last && last.kind === h.kind && (quietEvents[h.kind] || (h.kind === 'task_note' && same(last.items[0].by, h.by)));
    if (joins) last.items.push(h); else entries.push({kind: h.kind, items: [h]});
  }
  return entries;
}

function historyItem(entry) {
  const [h] = entry.items;
  const quiet = quietEvents[entry.kind];
  const grouped = Boolean(quiet) && entry.items.length > 1;
  const label = grouped ? quiet(entry.items.length) : h.label;
  // A hand-on reads as one line: "Handed on to Rafael (by Tomas)".
  const inline = h.kind === 'owner_changed' && !grouped && h.text.startsWith('to ');
  const head = `<div class="brief-event-head"><span><strong>${esc(label)}</strong>${inline ? ' ' + esc(h.text) : ''}${h.by && !grouped ? ` <span class="muted">· ${esc(h.by)}</span>` : ''}</span><time datetime="${esc(h.at)}">${esc(when(h.at))}</time></div>`;
  // The agent's bookkeeping is a count: the questions are under Decisions.
  if (grouped || quiet) return `<li class="is-quiet">${head}</li>`;
  const texts = entry.items.map(x => x.text).filter(Boolean);
  return `<li class="${keyEvents.has(h.kind) ? 'is-key' : ''}">${head}${h.question ? `<p class="brief-event-q">${esc(h.question)}</p>` : ''}${inline ? '' : texts.map(x => `<p>${esc(x)}</p>`).join('')}</li>`;
}

function historyCard(history) {
  const entries = historyEntries(history);
  const hidden = historyAll ? 0 : Math.max(0, entries.length - HISTORY_SHOWN);
  return `<section class="task-panel" aria-labelledby="history-title"><h2 id="history-title">History</h2>
    ${entries.length ? `${hidden ? `<button class="button text brief-earlier" data-action="history-all">Show ${plural(hidden, 'earlier event')}</button>` : ''}<ol class="brief-history">${entries.slice(hidden).map(historyItem).join('')}</ol>` : '<p class="brief-intro">Nothing recorded yet.</p>'}
  </section>`;
}

// A note the person added from the page can be withdrawn while their link
// works; the coding agent is told. A withdrawn note stays, struck through.
function notesList(notes) {
  return `<ul class="brief-notes">${notes.map(n => `<li class="${n.withdrawn ? 'is-withdrawn' : ''}"><div class="brief-turn-head"><strong>${esc(n.by || 'Someone')}</strong><time datetime="${esc(n.at)}">${esc(when(n.at))}</time></div><p>${prose(n.text)}</p>${n.withdrawn ? '<p class="brief-note-state">Withdrawn by its author.</p>' : n.mine ? `<button type="button" class="button text brief-note-withdraw" data-action="withdraw-note" data-id="${esc(n.id)}">Withdraw</button>` : ''}</li>`).join('')}</ul>`;
}

function inquiryCard() {
  const inquiry = inquiryView();
  return `<section class="task-panel brief-inquiry" aria-labelledby="inquiry-title"><h2 id="inquiry-title">Ask Raven</h2>
    <form id="inquiry-form" data-focus="${esc(data.focus?.node_id || '')}"><label class="sr-only" for="inquiry-text">Your question about this decision</label><textarea id="inquiry-text" maxlength="2000" required placeholder="What would change if we chose the other option?" ${inquiry.pending ? 'readonly' : ''}>${esc(inquiry.question)}</textarea><div class="brief-chat-foot"><button class="button" type="submit" ${inquiry.pending ? 'disabled' : ''}>${inquiry.pending ? 'Reading…' : 'Ask'}</button></div></form>
    ${inquiry.error ? `<p role="alert" class="error">${esc(inquiry.error)}</p>` : ''}
    ${inquiry.answer ? `<div class="brief-inquiry-result" role="status"><p>${prose(inquiry.answer.answer)}</p>${inquiry.answer.notice ? `<small>${esc(inquiry.answer.notice)}</small>` : ''}${inquiryCitations(inquiry.answer, 'brief-inquiry-citations')}${(inquiry.answer.evidence || []).length ? `<details data-keep="inquiry-evidence"><summary>Evidence used · ${inquiry.answer.evidence.length}</summary>${inquiry.answer.evidence.map(e => `<h3>${esc(e.title || e.id || 'Recorded evidence')}</h3><p>${prose(e.text)}</p>`).join('')}</details>` : ''}</div>` : ''}</section>`;
}

function noteCard() {
  // The record and one place to add to it. The coding agent reads notes
  // on its tree; nothing here calls a model.
  const notes = data.notes || [];
  return `<section class="task-panel brief-chat" aria-label="Notes">
    ${notes.length ? notesList(notes) : '<p class="brief-intro">No notes yet.</p>'}
    ${data.viewer.role === 'viewer' ? '<p class="brief-intro">You have read-only access.</p>' : `<form id="note-form" class="brief-chat-form"><label class="sr-only" for="note-text">Your note</label>
      <textarea id="note-text" maxlength="4000" placeholder="A constraint, a person to ask, a link to an earlier discussion." required aria-describedby="note-audience"></textarea>
      <div class="brief-chat-foot"><span class="brief-audience" id="note-audience">Visible to the workspace.</span><button class="button" type="submit">Add note</button></div></form>`}
  </section>`;
}

// The header names whose page this is, and holds what the link is: the
// footer that said so sat 2,000 to 4,900px down a page with no footers.
function viewerMenu(open) {
  const v = data.viewer;
  return `<details class="brief-viewer-menu"${open ? ' open' : ''}><summary class="brief-viewer" aria-label="Viewing as ${esc(v.name)}: about this link">${avatar(v.name)}<span><span class="brief-viewer-as">Viewing as </span><strong>${esc(v.name)}</strong></span><span class="brief-caret" aria-hidden="true"></span></summary>
    <div class="brief-viewer-pop"><p>This link is yours. It opens this one task as ${esc(v.name)} until ${esc(day(v.link_expires_at))}.</p><p>Please don’t forward it: anyone holding it can answer as you.</p></div></details>`;
}

// One invitation to make an account: the side note on a desktop, a text
// link in the header on a phone. The header button was the largest
// control on a phone's bar, and the side note said it again.
function accountCta() {
  const v = data.viewer;
  // Signed out, the app's sign-in lands on the inbox, not this task: the
  // button says what it does.
  if (v.has_account) return v.signed_in ? `<a class="button small" href="/#runs/${encodeURIComponent(data.task.id)}">Open in Raven</a>`
    : '<a class="brief-top-link" href="/auth/login">Sign in to Raven</a>';
  if (v.can_create_account) return narrow.matches ? '<button class="button text brief-top-link" data-action="account">Create account</button>' : '';
  return v.sign_in ? '<a class="brief-top-link" href="/auth/login">Sign in</a>' : '';
}

function joinNote() {
  if (!data.viewer.can_create_account || narrow.matches) return '';
  return `<p class="brief-join">Raven knows you as ${esc(data.viewer.name)}. An account gives you one inbox for the decisions waiting on you, on every task. <button class="button text" data-action="account">Create account</button></p>`;
}

// A redraw replaces the forms. What someone typed and has not sent stays,
// unless it was for a different decision than the one now on the form.
// So do the sections they opened: every redraw closed "Details", each
// person's line, "Correct your answer" and a quote's "Show more", and the
// quiet refresh redrew every 30 seconds. Each carries a key (`data-keep`)
// made from what it is about, not where it is.
function keepDrafts() {
  return {inquiryFor: $('#inquiry-form')?.dataset.focus || '', answerFor: $('#answer-form')?.dataset.id || '', values: DRAFTS.map(id => [id, document.getElementById(id)?.value || '']),
    picked: document.querySelector('input[name="answer-option"]:checked')?.value || '',
    handonPerson: $('#handon-person')?.value || '', menu: $('.brief-viewer-menu')?.open || false,
    disclosures: [...document.querySelectorAll('details[data-keep]')].map(el => [el.dataset.keep, el.open]),
    sourceReview: {key: renderedSourceReviewKey, checked: $('#brief-source-confirm')?.checked === true},
    shown: [...document.querySelectorAll('blockquote[data-keep]:not(.is-clamped)')].map(el => el.dataset.keep)};
}

function restoreDrafts(kept) {
  const sameForm = $('#answer-form')?.dataset.id === kept.answerFor;
  for (const [id, value] of kept.values) {
    if (id === 'inquiry-text' && kept.inquiryFor !== (data.focus?.node_id || '')) continue;
    if (!value || (id.startsWith('answer') || id.startsWith('handon')) && !sameForm) continue;
    const el = document.getElementById(id);
    if (el && !el.value) el.value = value;
  }
  const disclosures = new Map(kept.disclosures);
  for (const el of document.querySelectorAll('details[data-keep]')) {
    if (disclosures.has(el.dataset.keep)) el.open = disclosures.get(el.dataset.keep);
  }
  const sourceConfirm = $('#brief-source-confirm');
  const sourceKey = sourceAcknowledgmentKey(data.focus);
  if (sourceConfirm) sourceConfirm.checked = Boolean(sameForm && sourceKey &&
    sourceKey === kept.sourceReview.key && kept.sourceReview.checked);
  for (const el of document.querySelectorAll('blockquote[data-keep].is-clamped')) {
    if (kept.shown.includes(el.dataset.keep)) { el.classList.remove('is-clamped'); el.nextElementSibling?.matches('.brief-more') && el.nextElementSibling.remove(); }
  }
  if (sameForm && kept.handonPerson && $('#handon-person')) { $('#handon-person').value = kept.handonPerson; showOther(); }
  syncOptions();
  document.querySelectorAll('textarea.brief-grow').forEach(grow);
}

// A reason box grows with what is in it, up to about eight lines: at
// 390px the start of a two-sentence reason scrolled out of a three-line
// box, and the person could not reread what they were signing.
function grow(el) {
  if (!el || el.tagName !== 'TEXTAREA') return;
  el.style.height = 'auto';
  const line = parseFloat(getComputedStyle(el).lineHeight) || 21;
  el.style.height = `${Math.min(el.scrollHeight + 2, line * 8 + 20)}px`;
}

function clearDrafts(...ids) { ids.forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; }); }

// The option whose words are in the box is the one picked; edit them and
// none is. Beside a sign-off, the reason box opens with a correction.
function syncOptions() {
  const value = ($('#answer-text')?.value || '').trim();
  document.querySelectorAll('input[name="answer-option"]').forEach(r => { r.checked = r.value === value; r.closest('.brief-option')?.classList.toggle('is-picked', r.checked); });
  document.querySelectorAll('#answer-form [data-correction]').forEach(el => { if (el.hidden === !value) return; el.hidden = !value; grow(el); });
  // With a correction typed, the button says what it records.
  const send = $('#answer-form button[data-plain]');
  if (send) send.textContent = value ? 'Save correction' : send.dataset.plain;
}

// "Show more" only where the clamp hides something: at 1440px a quote of
// 400 characters fits its five lines, and the button showed nothing more.
// A quote in a closed section is measured when it opens.
function fitQuotes(scope = document) {
  for (const q of scope.querySelectorAll('blockquote.brief-quote.is-clamped')) {
    if (!q.getClientRects().length || q.scrollHeight > q.clientHeight + 1) continue;
    q.classList.remove('is-clamped');
    if (q.nextElementSibling?.matches('.brief-more')) q.nextElementSibling.remove();
  }
}

function showOther() {
  const value = $('#handon-person')?.value || '';
  const other = value === 'other';
  document.querySelectorAll('.brief-handon-other').forEach(el => { el.hidden = !other; });
  const input = $('#handon-other');
  if (input) input.required = other;
  // Someone the listing names whom Raven cannot message: say so where they
  // were picked, and the form cannot send.
  const help = $('#handon-help');
  const listed = value.startsWith('unknown:') ? (data.focus?.handon_unknown || [])[Number(value.slice(8))] : null;
  if (help) {
    help.hidden = !listed;
    help.textContent = listed ? `${listed.name} is in ${listed.why} but has no Raven person yet. An admin can add them, or pick someone else.` : '';
  }
  const send = $('#handon-form button[type=submit]');
  if (send) send.disabled = Boolean(listed);
}

// What the hero says the person still has to do, and who the task waits
// on once they are done. "Waiting on 1 decision" in amber read as owed
// by them after they had answered.
function heroLine(standing) {
  const me = data.viewer.name;
  const blocking = data.nodes.filter(n => n.blocking && counted(n));
  const mineOwed = blocking.filter(n => n.owed_by_me);
  const others = unique(blocking.flatMap(owedBy).filter(x => !same(x, me)));
  const required = blocking.filter(n => n.raw_status === 'suggested').length;
  const unassigned = blocking.filter(n => n.raw_status !== 'suggested' && !owedBy(n).length).length;
  const waitsOn = [
    others.length ? `The task now waits on ${listed(others)}.` : '',
    required ? `The coding agent must adopt ${plural(required, 'required follow-up')} before finishing.` : '',
    unassigned ? `The task still has ${plural(unassigned, 'unassigned decision')}.` : '',
  ].filter(Boolean).join(' ') || 'No recorded decision is waiting for a person.';
  return standing === 'answer' ? `${esc(data.requester.name || 'A teammate')}’s coding agent needs ${mineOwed.length > 1 ? plural(mineOwed.length, 'decision') : 'one decision'} from you.`
    : standing === 'sign' ? 'An answer here is waiting for your sign-off.'
    : standing === 'cosign' ? 'An answer is recorded. You can add your signature or correct it.'
    : mineOwed.length ? (standing === 'mine' ? 'Your answer is recorded. Another decision here still waits on you; it is under Decisions.' : 'A decision here waits on you; it is under Decisions.')
    : standing === 'mine' || standing === 'handed' ? `You’re done. ${esc(waitsOn)}`
    : 'The context of this task, as Raven has recorded it.';
}

function errorPage() {
  $('#brief-account').innerHTML = '';
  // A link that no longer works can still ask for a new one: it goes to
  // the person it named, in Slack, whoever asks.
  const resendable = errorStatus === 401 && WELL_FORMED.test(token);
  // A cut-off paste: no resend, which would send nothing and leave them
  // waiting for a message that never comes.
  const cut = errorStatus === 401 && !resendable;
  const message = cut ? 'This link looks incomplete. Copy the whole link from your Slack message and open it again.' : error;
  $('#brief').innerHTML = `<div class="brief-empty"><h1>Could not open this task</h1><p>${esc(message)}</p>
    ${resendable ? `<p class="brief-intro">A task link opens only for the person it was sent to, and expires after a while. Raven can send that person a new one in Slack. Paste a new link here and the page opens it.</p>
      <div class="brief-empty-actions"><button class="button primary" data-action="resend" ${resent ? 'disabled' : ''}>${resent ? 'Link requested' : 'Send me a new link in Slack'}</button></div>
      ${resent ? `<p class="brief-resent" role="status">${esc(resent)}</p>` : ''}` : ''}
    <p class="brief-intro">Have an account? <a href="/auth/login">Sign in to Raven</a> to see the task in your workspace.</p></div>`;
}

function render() {
  if (error && !data) return errorPage();
  if (!data) return;
  const root = $('#brief');
  const kept = keepDrafts();
  const active = document.activeElement;
  const focusedId = active?.id, selection = [active?.selectionStart, active?.selectionEnd];
  const t = data.task, f = data.focus, p = data.progress;
  document.title = `Raven · ${f ? f.question : t.title}`;
  $('#brief-workspace').textContent = data.workspace || '';
  $('#brief-account').innerHTML = `${viewerMenu(kept.menu)}${accountCta()}`;
  const standing = f ? focusStanding(f) : 'read';
  const box = inquiryCard() + `<details class="brief-extra" data-keep="notes"><summary>Notes</summary>${noteCard()}</details>`;
  const people = `<details class="brief-extra" data-keep="people"><summary>People involved</summary>${peopleCard(data.contacts)}${joinNote()}</details>`;
  const background = `<details class="brief-extra" data-keep="task-background"><summary>Background and history</summary>${requesterCard(t, data.requester)}${historyCard(data.history)}</details>`;
  // On a phone the decision and its form come first: with the question
  // box above them, the form began below the first screen. People
  // involved is its own disclosure at both widths.
  const body = narrow.matches
    ? `<div class="brief-stack">${focusCard(f)}${box}${decisionsCard(data.nodes)}${people}${background}</div>`
    : `<div class="brief-columns"><div class="brief-main">${focusCard(f)}${decisionsCard(data.nodes)}${background}</div>
       <aside class="brief-side" aria-label="Context and people">${box}${people}</aside></div>`;
  // The heading is the task's title; one too long for a heading gives way
  // to its work item (taskDisplayLabel). One task-wide count beside the
  // personal line: a pill and a meta count beside "needs one decision from
  // you" read as two more owed by them.
  const title = taskDisplayLabel(t, f, '').replace(/^ · /, '') || t.title;
  root.innerHTML = `
    ${error ? `<p class="task-warning" role="alert">Refresh failed: ${esc(error)}. Showing the last successful read.</p>` : ''}
    <section class="brief-hero">
      <h1>${esc(title)}</h1>
      <p class="brief-ask" role="status">${heroLine(standing)}</p>
      <div class="metadata">${data.requester.name ? `<span>${esc(data.requester.name)}</span>` : ''}<span>${esc(t.repo)}</span><span>${p.signed} of ${p.total} settled</span>${t.status === 'completed' ? '<span>Agent reported complete</span>' : ''}</div>
    </section>
    ${body}`;
  restoreDrafts(kept);
  const focus = focusedId && document.getElementById(focusedId);
  if (focus && (!focusedId.startsWith('answer') || kept.answerFor === (data.focus?.node_id || ''))) {
    focus.focus({preventScroll:true});
    if (typeof focus.setSelectionRange === 'function' && selection[0] != null) focus.setSelectionRange(...selection);
  }
  renderedSourceReviewKey = sourceAcknowledgmentKey(f);
  fitQuotes();
  const side = $('.brief-side');
  sideSize.disconnect();
  if (side) sideSize.observe(side);
}

// Below the sticky header, with room to spare.
function scrollToEl(el) {
  if (!el) return;
  const header = $('.brief-top').offsetHeight;
  window.scrollTo({top: Math.max(0, el.getBoundingClientRect().top + scrollY - header - 12), behavior: 'smooth'});
}

// The side column stays in view. One taller than the window scrolls with
// the page until its end shows, then stays: nothing in it is out of reach,
// and it needs no scroll of its own.
const sideSize = new ResizeObserver(() => fitSide());
function fitSide() {
  const side = $('.brief-side');
  if (!side) return;
  const top = $('.brief-top').offsetHeight + 16;
  side.style.top = side.offsetHeight > innerHeight - top - 16 ? `${innerHeight - side.offsetHeight - 16}px` : '';
}

function accountModal() {
  const v = data.viewer;
  const needsEmail = !v.has_email;
  $('#brief-modal-content').innerHTML = `<form id="account-form" class="brief-account"><h2 id="brief-modal-title">Create your Raven account</h2>
    <p>Raven already knows you as ${esc(v.name)}. An account gives you one inbox for the decisions waiting on you, on every task.</p>
    <p>${v.email_hint ? `You’ll sign in with the address Raven has for you, <strong>${esc(v.email_hint)}</strong> (shown in part, since a link can be forwarded), and a password.` : 'You’ll sign in with your email and a password.'}</p>
    ${needsEmail ? '<label for="account-email">Email</label><input id="account-email" type="email" autocomplete="email" required>' : ''}
    <label for="account-password">Password</label><input id="account-password" type="password" autocomplete="new-password" minlength="12" maxlength="256" required aria-describedby="account-password-help">
    <p class="brief-field-help" id="account-password-help">At least 12 characters.</p>
    <p class="error" id="account-error" role="alert" hidden></p>
    <div class="brief-answer-foot"><button class="button primary" type="submit">Create account</button><button class="button" type="button" data-action="close-modal">Not now</button></div>
    ${v.github_sign_in ? '<p class="brief-account-alt">Or <a href="/auth/github">use GitHub instead</a>.</p>' : ''}</form>`;
  $('#brief-modal').showModal();
}

// Open another decision on the task with everything the first one had.
async function openFocus(id) {
  focusId = id;
  await load();
  scrollToEl($('#focus'));
  $('#focus-question')?.focus({preventScroll: true});
}

document.addEventListener('click', async event => {
  // The link popover closes on a click anywhere else.
  const menu = $('.brief-viewer-menu');
  if (menu?.open && !menu.contains(event.target)) menu.open = false;
  // "Skip to content" moves focus, not the fragment: the fragment is the
  // link, and following the anchor replaced it, so a reload lost the page.
  if (event.target.closest('a.skip')) { event.preventDefault(); $('#brief-main').focus(); return; }
  const el = event.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;
  if (action === 'account') accountModal();
  if (action === 'close-modal') $('#brief-modal').close();
  if (action === 'history-all') { historyAll = true; render(); }
  if (action === 'more') { el.previousElementSibling?.classList.remove('is-clamped'); el.remove(); }
  if (action === 'to-focus') scrollToEl($('#focus'));
  if (action === 'resend') {
    el.disabled = true;
    try { resent = (await call('/api/brief/resend', {})).notice; }
    catch (e) { resent = e.message; }
    errorPage();
  }
  if (action === 'focus') await openFocus(el.dataset.id);
  if (action === 'focus-asked') await openFocus('');
  if (action === 'withdraw-note') {
    try {
      await call('/api/brief/withdraw', {note_id: el.dataset.id});
      toast('Withdrawn. The coding agent is told the next time it checks this task.'); await load();
    } catch (e) { toast(e.message); }
  }
});

document.addEventListener('keydown', event => { if (event.key === 'Escape' && $('.brief-viewer-menu')?.open) $('.brief-viewer-menu').open = false; });
// A section's toggle does not bubble; a quote in it is measured once open.
document.addEventListener('toggle', event => { if (event.target.open) fitQuotes(event.target); }, true);

document.addEventListener('input', event => {
  if (event.target.id === 'answer-text') syncOptions();
  if (event.target.classList?.contains('brief-grow')) grow(event.target);
});

document.addEventListener('change', event => {
  if (event.target.name === 'answer-option') {
    const box = $('#answer-text');
    if (box) { box.value = event.target.value; syncOptions(); grow(box); if (!narrow.matches) box.focus(); }
  }
  if (event.target.id === 'handon-person') { showOther(); if ($('#handon-person').value === 'other') $('#handon-other').focus(); }
});

// A refusal shown where the form is. One because the decision moved on
// (an answer from another tab, or from Slack) said "Reopen it", which the
// page had no way to do: every later click was refused the same way. The
// page reads the decision again, keeping what was typed, and says so.
const STALE = /changed while you were reviewing it/;
async function refused(e, box) {
  if (!STALE.test(e.message)) { const el = $(box); el.textContent = e.message; el.hidden = false; return; }
  await load();
  toast('This decision changed while you were reading it. The page shows it as it is now: check it, then act again.');
}

document.addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target;
  if (form.id === 'answer-form') {
    try {
      const review = data.focus?.source_revalidation;
      if (review?.has_reliance && (!review.available || !$('#brief-source-confirm')?.checked)) {
        throw new Error('Review the complete current evidence before signing. If unavailable, wait for the source to refresh.');
      }
      const result = await call('/api/brief/answer', {decision_id: form.dataset.id, expected_updated_at: form.dataset.rev,
        ...(review?.has_reliance ? {source_evidence: review.pins, source_decision_pins: review.decision_pins} : {}),
        answer: $('#answer-text').value.trim(), rationale: $('#answer-why').hidden ? '' : $('#answer-why').value.trim()});
      clearDrafts('answer-text', 'answer-why'); toast(result.notice); await load();
    } catch (e) { await refused(e, '#answer-error'); }
  }
  if (form.id === 'handon-form') {
    const picked = $('#handon-person').value;
    if (picked.startsWith('unknown:')) return;
    try {
      const result = await call('/api/brief/refer', {decision_id: form.dataset.id, expected_updated_at: form.dataset.rev,
        person: picked === 'other' ? $('#handon-other').value.trim() : picked, note: $('#handon-note').value.trim()});
      clearDrafts('handon-note', 'handon-other', 'inquiry-text', 'answer-text', 'answer-why'); toast(result.notice); await load();
    } catch (e) { await refused(e, '#handon-error'); }
  }
  if (form.id === 'note-form') {
    try { await call('/api/brief/note', {text: $('#note-text').value.trim()}); toast(`Added. ${SEEN}`); clearDrafts('note-text'); await load(); }
    catch (e) { toast(e.message); }
  }
  if (form.id === 'account-form') {
    const errorBox = $('#account-error');
    try {
      const result = await call('/api/brief/account', {password: $('#account-password').value, email: $('#account-email')?.value || ''});
      location.href = result.redirect;
    } catch (e) { errorBox.textContent = e.message; errorBox.hidden = false; }
  }
});

// Only a real change of layout redraws: Chromium fires a change event for
// a capture taller than the window with nothing changed, and the redraw
// closed what was open.
let wasNarrow = narrow.matches;
narrow.addEventListener('change', event => { if (event.matches === wasNarrow) return; wasNarrow = event.matches; render(); });
addEventListener('resize', fitSide);
load();
setInterval(() => load({quiet: true}), 30000);


// Interview requests keep the same non-ambient task-link credential. The
// adapter never signs in, creates an account or requests workspace-wide APIs.
window.ravenInterviewBridge = {
  user: () => data?.viewer || {},
  notify: toast,
  refresh: () => load(),
  request: async (path, body) => {
    const parts = path.split('/');
    if (parts[1] !== 'api' || parts[2] !== 'tasks' || decodeURIComponent(parts[3]) !== data?.task?.id || parts[4] !== 'interviews') {
      throw new Error('This interview is outside the task link scope.');
    }
    const interviewId = parts[5] || '';
    const action = parts[6] || (interviewId ? 'get' : body === undefined ? 'list' : 'create');
    return call('/api/brief/interview', {...(body || {}), action, ...(interviewId ? {interview_id: interviewId} : {})});
  },
};

// Clarification has its own read-only endpoint; it never submits an answer.
document.addEventListener('submit', async event => {
  if (event.target.id !== 'inquiry-form') return;
  event.preventDefault();
  const inquiry = inquiryView(), form = event.target, input = form.querySelector('textarea');
  if (inquiry.pending || !form.reportValidity()) return;
  inquiry.question = input.value.trim();
  if (!inquiry.question) return;
  const focus = data.focus?.node_id || '';
  inquiry.pending = true; inquiry.error = ''; render();
  try { inquiry.answer = await call('/api/brief/inquiry', {question:inquiry.question, focus_id:focus}); }
  catch (error) { inquiry.error = error.message; }
  finally { inquiry.pending = false; render(); }
});
