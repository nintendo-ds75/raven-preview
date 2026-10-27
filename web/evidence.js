/* Add evidence by pasting. A member pastes a customer message, an email or a
   thread and it becomes an ordinary record (POST /api/records): workspace
   visible, author stamped by the server, eligible for research the moment it
   is saved. The panel lives in the Today aside and the last ten pasted
   records sit under the form, each with Withdraw for its author or an admin. */
'use strict';
const EVIDENCE_LIMIT = 20000;
const EVIDENCE_SPLIT = 'Split it, or import it as a meeting transcript.';
const EVIDENCE_KINDS = [['note', 'Note'], ['slack', 'Slack message or thread'], ['email', 'Email'], ['doc', 'Document'], ['ticket', 'Ticket']];
let evidenceRecords = null, evidenceError = '', evidenceNotice = '', evidenceLoading = false, evidenceSaving = false, evidenceWithdrawing = '';
const evidenceDraft = {repo: '', kind: 'note', title: '', body: '', created_at: '', url: ''};
const evidenceCanWrite = () => !!state.auth && (!state.auth.enabled || ['admin', 'member'].includes(state.me?.role));
const evidenceCanWithdraw = item => evidenceCanWrite() && item.availability === 'available'
  && (!state.auth.enabled || state.me?.role === 'admin' || (!!state.me?.id && item.attested_by === state.me.id));
const evidenceWhen = value => typeof objectiveTime === 'function' ? objectiveTime(value) : String(value || '');

function evidenceRepositories() {
  const seen = new Map();
  const lists = [typeof objectiveData !== 'undefined' && objectiveData?.repositories, state.question_repositories,
    state.workbench?.repositories, state.execution_config?.repositories];
  for (const list of lists) for (const repo of list || []) {
    const id = repo?.id || repo;
    if (typeof id === 'string' && id && !seen.has(id)) seen.set(id, repo?.name || id);
  }
  return [...seen].map(([id, name]) => ({id, name}));
}

function evidenceForm() {
  const repos = evidenceRepositories();
  if (!evidenceDraft.repo && repos.length === 1) evidenceDraft.repo = repos[0].id;
  const count = evidenceDraft.body.length;
  return `<form id="evidence-form" aria-describedby="evidence-help">
    <label for="evidence-repo">Repository</label>
    <select id="evidence-repo" name="repo" required><option value="">Choose a repository</option>${repos.map(repo => `<option value="${esc(repo.id)}" ${repo.id === evidenceDraft.repo ? 'selected' : ''}>${esc(repo.name)}</option>`).join('')}</select>
    <label for="evidence-kind">What is it</label>
    <select id="evidence-kind" name="kind">${EVIDENCE_KINDS.map(([id, label]) => `<option value="${id}" ${id === evidenceDraft.kind ? 'selected' : ''}>${label}</option>`).join('')}</select>
    <label for="evidence-title">Title</label>
    <input id="evidence-title" name="title" type="text" maxlength="300" required value="${esc(evidenceDraft.title)}" autocomplete="off">
    <label for="evidence-body">Paste the message, email or thread</label>
    <textarea id="evidence-body" name="body" rows="7" maxlength="${EVIDENCE_LIMIT}" required aria-describedby="evidence-count">${esc(evidenceDraft.body)}</textarea>
    <p class="objective-muted evidence-count" id="evidence-count" aria-live="polite">${count.toLocaleString()} / ${EVIDENCE_LIMIT.toLocaleString()} characters</p>
    <label for="evidence-date">When it was written (optional)</label>
    <input id="evidence-date" name="created_at" type="date" value="${esc(evidenceDraft.created_at)}">
    <label for="evidence-url">Link to the original (optional)</label>
    <input id="evidence-url" name="url" type="url" maxlength="1000" value="${esc(evidenceDraft.url)}" placeholder="https://" autocomplete="off">
    <p class="objective-muted" id="evidence-help">Saved as a workspace-visible record in your name. Evidence, never approval: it can be cited, and you can withdraw it.</p>
    <button class="button small primary" type="submit" ${evidenceSaving ? 'disabled aria-busy="true"' : ''}>Save as evidence</button>
  </form>`;
}

function evidenceItem(item) {
  const withdrawn = item.availability !== 'available';
  const confirm = evidenceWithdrawing === item.record_id;
  return `<li class="evidence-item" ${withdrawn ? 'data-withdrawn="true"' : ''}>
    <strong>${esc(item.title || item.ref || 'Untitled')}</strong>
    <span class="objective-muted">${esc(item.kind)} · ${esc(item.author || 'unknown author')} · ${esc(evidenceWhen(item.source_created_at || item.observed_at))}${withdrawn ? ' · withdrawn' : ''}</span>
    ${item.url && /^https?:\/\//.test(item.url) ? `<a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">Original ↗</a>` : ''}
    ${evidenceCanWithdraw(item) && !confirm ? `<button class="button small" type="button" data-evidence-action="withdraw" data-id="${esc(item.record_id)}">Withdraw</button>` : ''}
    ${confirm ? `<form class="evidence-withdraw" id="evidence-withdraw-form" data-id="${esc(item.record_id)}">
      <label for="evidence-reason">Why withdraw it</label>
      <input id="evidence-reason" name="reason" type="text" maxlength="500" required autocomplete="off">
      <p class="objective-muted">Blocks reuse and sends anything that relied on it to review. The audit history stays.</p>
      <button class="button small primary" type="submit">Confirm withdrawal</button>
      <button class="button small" type="button" data-evidence-action="cancel-withdraw" data-id="">Keep it</button>
    </form>` : ''}
  </li>`;
}

function evidencePanel() {
  const items = evidenceRecords?.records || [];
  return `<section class="objective-side-section evidence-panel" id="evidence-panel" aria-labelledby="evidence-heading">
    <h2 id="evidence-heading">Add evidence</h2>
    ${evidenceError ? `<p class="objective-error" role="alert">${esc(evidenceError)}</p>` : ''}
    ${evidenceNotice ? `<p class="objective-notice" role="status">${esc(evidenceNotice)}</p>` : ''}
    ${evidenceForm()}
    <h3>Recently pasted</h3>
    ${evidenceLoading && evidenceRecords === null ? '<p class="objective-muted">Loading…</p>'
      : items.length ? `<ul class="evidence-list">${items.map(evidenceItem).join('')}</ul>`
      : '<p class="objective-muted">Nothing pasted yet.</p>'}
  </section>`;
}

function evidenceStyle() {
  if (document.getElementById('evidence-style') || !document.head?.appendChild) return;
  const style = document.createElement('style');
  style.id = 'evidence-style';
  style.textContent = '.evidence-panel{min-width:0}.evidence-panel h3{font-size:13px;margin:22px 0 4px}.evidence-panel label{display:block;font-size:12px;margin:12px 0 6px}'
    + '.evidence-panel input,.evidence-panel select,.evidence-panel textarea{display:block;width:100%;max-width:100%;box-sizing:border-box;font:inherit;font-size:16px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:inherit}'
    + '.evidence-panel textarea{min-height:140px;resize:vertical}.evidence-count{margin:6px 0 0}.evidence-panel .button{margin-top:12px}'
    + '.evidence-list{list-style:none;margin:8px 0 0;padding:0}.evidence-item{border-top:1px solid var(--line);padding:10px 0;font-size:12px;line-height:1.6;overflow-wrap:anywhere}'
    + '.evidence-item strong,.evidence-item span,.evidence-item a{display:block}.evidence-item[data-withdrawn]{opacity:.7}.evidence-withdraw{margin-top:8px}';
  document.head.appendChild(style);
}

function evidenceMount(force = false) {
  if (view !== 'today' || !evidenceCanWrite()) return;
  const app = document.getElementById('app');
  if (!app) return;
  let mount = document.getElementById('evidence-mount');
  if (!mount) {
    const aside = app.querySelector?.('.objective-layout > aside');
    if (!aside) return;
    mount = document.createElement('div');
    mount.id = 'evidence-mount';
    aside.appendChild(mount);
  }
  const active = document.activeElement;
  if (!force && mount.querySelector?.('#evidence-panel') && active && mount.contains?.(active)) return;
  evidenceStyle();
  mount.innerHTML = evidencePanel();
  if (evidenceRecords === null && !evidenceLoading) evidenceLoad();
}

async function evidenceLoad() {
  evidenceLoading = true;
  try {
    evidenceRecords = await api('/api/records?namespace=raven%3Apaste&limit=10');
  } catch (error) {
    evidenceRecords = {records: []};
    evidenceError = error.message;
  } finally {
    evidenceLoading = false;
    evidenceMount(true);
  }
}

function evidenceValues(form) {
  const out = {};
  for (const field of form.querySelectorAll('input,select,textarea')) if (field.name) out[field.name] = field.value ?? '';
  return out;
}

if (typeof renderObjectives === 'function') {
  const evidenceBase = renderObjectives;
  renderObjectives = function evidenceRenderObjectives() {
    const result = evidenceBase.apply(this, arguments);
    try { evidenceMount(); } catch (error) { console.error(error); }
    return result;
  };
}

document.addEventListener('input', event => {
  const form = event.target?.closest?.('form');
  if (!form || form.id !== 'evidence-form' || !event.target.name) return;
  if (event.target.name in evidenceDraft) evidenceDraft[event.target.name] = event.target.value || '';
  if (event.target.name === 'body') {
    const counter = document.getElementById('evidence-count');
    if (counter) counter.textContent = `${(event.target.value || '').length.toLocaleString()} / ${EVIDENCE_LIMIT.toLocaleString()} characters`;
  }
});

document.addEventListener('paste', event => {
  if (event.target?.id !== 'evidence-body') return;
  const pasted = event.clipboardData?.getData?.('text') || '';
  const current = event.target.value || '';
  const selected = (event.target.selectionEnd ?? current.length) - (event.target.selectionStart ?? current.length);
  if (current.length - selected + pasted.length > EVIDENCE_LIMIT) {
    event.preventDefault();
    evidenceError = `That is ${(current.length - selected + pasted.length).toLocaleString()} characters; a record holds ${EVIDENCE_LIMIT.toLocaleString()}. ${EVIDENCE_SPLIT}`;
    evidenceMount(true);
    document.getElementById('evidence-body')?.focus();
  }
});

document.addEventListener('click', event => {
  const button = event.target?.closest?.('[data-evidence-action]');
  if (!button || view !== 'today' || button.disabled) return;
  const action = button.dataset.evidenceAction;
  if (action === 'withdraw') { evidenceWithdrawing = button.dataset.id || ''; evidenceError = ''; evidenceMount(true); document.getElementById('evidence-reason')?.focus(); }
  if (action === 'cancel-withdraw') { evidenceWithdrawing = ''; evidenceMount(true); }
});

document.addEventListener('submit', async event => {
  const form = event.target;
  if (!form || view !== 'today' || !['evidence-form', 'evidence-withdraw-form'].includes(form.id)) return;
  event.preventDefault();
  if (!evidenceCanWrite() || (form.reportValidity && !form.reportValidity())) return;
  const values = evidenceValues(form);
  if (form.id === 'evidence-withdraw-form') {
    const id = form.dataset.id;
    try {
      await api('/api/records/' + encodeURIComponent(id) + '/withdraw', {reason: values.reason || ''});
      evidenceWithdrawing = ''; evidenceError = ''; evidenceNotice = 'Withdrawn. Reuse is blocked and dependents are under review.';
      await evidenceLoad();
    } catch (error) { evidenceError = error.message; evidenceMount(true); }
    return;
  }
  if (evidenceSaving) return;
  Object.assign(evidenceDraft, {repo: values.repo || '', kind: values.kind || 'note', title: values.title || '', body: values.body || '', created_at: values.created_at || '', url: values.url || ''});
  if (evidenceDraft.body.length > EVIDENCE_LIMIT) {
    evidenceError = `That is ${evidenceDraft.body.length.toLocaleString()} characters; a record holds ${EVIDENCE_LIMIT.toLocaleString()}. ${EVIDENCE_SPLIT}`;
    evidenceMount(true);
    return;
  }
  if (!evidenceDraft.repo || !evidenceDraft.title.trim() || !evidenceDraft.body.trim()) {
    evidenceError = 'Choose a repository and give the evidence a title and a body.';
    evidenceMount(true);
    return;
  }
  const payload = {repo: evidenceDraft.repo, kind: evidenceDraft.kind, ref: 'paste:' + crypto.randomUUID(), provider: 'generic',
    namespace: 'raven:paste', title: evidenceDraft.title.trim(), body: evidenceDraft.body};
  if (evidenceDraft.created_at) payload.created_at = evidenceDraft.created_at;
  if (evidenceDraft.url.trim()) payload.url = evidenceDraft.url.trim();
  evidenceSaving = true; evidenceError = ''; evidenceMount(true);
  try {
    const result = await api('/api/records', payload);
    evidenceNotice = `Saved as evidence for ${result.repo || evidenceDraft.repo}, in your name.`;
    Object.assign(evidenceDraft, {title: '', body: '', created_at: '', url: ''});
    evidenceSaving = false;
    await evidenceLoad();
  } catch (error) {
    evidenceSaving = false; evidenceError = error.message; evidenceMount(true);
  }
});
