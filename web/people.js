'use strict';

// People (#people, #people/<person id>): find anyone, then see what Raven
// has learned about them, newest first. Each line says who said it, where
// and when; the person (or an admin) can reword it, drop it or add one.
// The same lines answer questions in Ask Raven and through MCP.
let peopleList = null, peopleQuery = '', peopleProfile = null, peopleFor = '', peopleKey = '', peopleError = '', peopleEditing = '';
let peopleTimer = null;
const peopleId = () => { try { return view === 'people' ? decodeURIComponent(location.hash.slice(1).split('/')[1] || '') : ''; } catch { return ''; } };
const peopleFirst = name => String(name || '').split(/\s+/)[0];
const peopleArea = scope => String(scope || '').replace(/\/?\*+$/, '') || 'everything';
const peopleSource = {'on a call with Raven': 'On a call with Raven', 'from a meeting': 'From a meeting', 'intro chat': 'Voice intro with Raven', 'added by them': 'Added by them', 'added by an admin': 'Added by an admin',
  'via an agent': 'Told an agent', 'from a call': 'On a call with Raven', 'answered an agent': 'Answered an agent'};

// A learned line is a pointer: its source (the voice intro, the call, the
// meeting transcript, the decision) with a one-line summary. Clicking it
// opens the whole source with the quoted line marked.
function sourceChip(ptr, quote) {
  if (!ptr) return '';
  return `<button type="button" class="source-chip" data-source="${esc(ptr.ref)}" data-quote="${esc(quote || '')}" title="${esc(ptr.summary || '')}"><span class="source-kind">${esc(ptr.kind_label)}</span><span class="source-title">${esc(ptr.title)}</span><span aria-hidden="true">↗</span></button>`;
}

async function openSource(ref, quote = '') {
  const [kind, ...rest] = String(ref || '').split(':'), id = rest.join(':');
  if (!kind || !id) return;
  let r;
  try { r = await api(`/api/sources/${encodeURIComponent(kind)}/${encodeURIComponent(id)}?quote=${encodeURIComponent(quote)}`); }
  catch (error) { notify(error.message); return; }
  const when = r.at ? new Date(r.at).toLocaleDateString(undefined, {month: 'short', day: 'numeric', year: 'numeric'}) : '';
  const people = (r.people || []).map(p => `<a class="ask-person-chip" href="#people/${esc(p.id)}" data-action="close">${avatar(p.name)}${esc(p.name)}</a>`).join('');
  const turns = (r.turns || []).map(t => `<div class="source-turn ${t.who === 'Raven' ? 'raven' : ''} ${t.quoted ? 'quoted' : ''}"><b>${esc(t.who || '')}</b><p>${esc(t.text)}</p></div>`).join('');
  const decision = r.answer || r.question ? `<div class="source-decision">${r.question ? `<p class="muted">${esc(r.question)}</p>` : ''}${r.answer ? `<p><strong>${esc(r.answer)}</strong></p>` : ''}${r.why ? `<p>Why: ${esc(r.why)}</p>` : ''}${r.signed_by ? `<p class="pr-signed">✓ Signed off by ${esc(r.signed_by)}</p>` : ''}</div>` : '';
  const learned = (r.learned || []).length ? `<div class="source-learned"><span class="ask-label">What Raven took from it</span><ul>${r.learned.map(x => `<li>${esc(x)}</li>`).join('')}</ul></div>` : '';
  const task = r.task_id ? `<a class="button small" href="#runs/${esc(r.task_id)}${r.node_id ? '/' + esc(r.node_id) : ''}" data-action="close">Open the task</a>` : '';
  openModal(r.title, [r.kind_label, when].filter(Boolean).join(' · '), `<div class="source-view"><p class="source-summary">${esc(r.summary || '')}</p>${people ? `<div class="source-people">${people}</div>` : ''}${decision}${learned}${turns ? `<div class="source-turns"><span class="ask-label">Full transcript</span>${turns}</div>` : ''}${task}</div>`);
  const quoted = document.querySelector('.source-turn.quoted');
  if (quoted) quoted.scrollIntoView({block: 'center'});
}
document.addEventListener('click', event => {
  const chip = event.target.closest('[data-source]');
  if (!chip) return;
  event.preventDefault();
  openSource(chip.dataset.source, chip.dataset.quote || '');
});

function saidQuote(x) {
  const same = (a, b) => String(a || '').replace(/\W+/g, ' ').trim().toLowerCase() === String(b || '').replace(/\W+/g, ' ').trim().toLowerCase();
  return `<blockquote class="said-quote"><p>${esc(x.quote ? '“' + x.quote + '”' : x.text)}</p>${x.quote && !same(x.quote, x.text) ? `<small>${esc(x.text)}</small>` : ''}</blockquote>`;
}

function loadPeople() {
  const id = peopleId();
  peopleError = '';
  if (id) {
    if (id !== peopleFor) peopleProfile = null;
    peopleFor = id;
    api('/api/people/' + encodeURIComponent(id) + '/profile').then(r => { peopleProfile = r; peopleKey = ''; render(); })
      .catch(e => { peopleError = e.message; peopleKey = ''; render(); });
    return;
  }
  const q = peopleQuery;
  api('/api/people/directory?q=' + encodeURIComponent(q)).then(r => { if (q === peopleQuery) { peopleList = r; peopleKey = ''; render(); } })
    .catch(e => { peopleError = e.message; peopleKey = ''; render(); });
}

function peopleListView() {
  const rows = (peopleList?.people || []).map(p => `<a class="person-row" href="#people/${encodeURIComponent(p.id)}">${avatar(p.name)}
    <span class="person-row-main"><b>${esc(p.name)}</b><small>${esc([p.teams.join(', '), p.decides.length ? 'Decides ' + p.decides.map(peopleArea).join(', ') : ''].filter(Boolean).join(' · ') || 'No team yet')}</small>
    ${p.match ? `<em>“${esc(p.match.length > 140 ? p.match.slice(0, 138) + '…' : p.match)}”</em>` : ''}</span>
    <span class="person-row-learned ${p.notes ? 'on' : ''}">${p.notes ? `${plural(p.notes, 'thing')} learned${p.learned_at ? ' · ' + esc(ago(p.learned_at)) : ''}` : 'Not heard from yet'}</span></a>`).join('');
  return `<div class="people"><div class="people-head"><div><h1>People</h1><p>Find anyone, and see what Raven has learned from them.</p></div><button type="button" class="button small" data-people="meeting">Add a meeting transcript</button></div>
    <label class="sr-only" for="people-search">Search people</label>
    <input id="people-search" class="people-search" type="search" autocomplete="off" placeholder="Search by name, team, area, or something they said" value="${esc(peopleQuery)}">
    ${!peopleList ? '<p class="ask-muted" role="status">Loading…</p>' : rows ? `<div class="people-list">${rows}</div>` : `<p class="ask-muted">${peopleQuery ? 'Nobody matches that.' : 'Add people under Settings, or connect a repository.'}</p>`}</div>`;
}

function peopleItem(t, p) {
  const when = t.at ? esc(ago(t.at)) : '';
  if (t.kind === 'decided') {
    return `<li class="learned decided"><span class="learned-dot" aria-hidden="true"></span><div>
      <p class="learned-q">${esc(t.question)}</p><p class="learned-text">${esc(t.text)}</p>${t.why ? `<p class="learned-why">${esc(t.why)}</p>` : ''}
      <small>${esc(peopleSource[t.source] || t.source)} · ${when}</small>${sourceChip(t.pointer, t.text)}</div></li>`;
  }
  if (peopleEditing === t.id) {
    return `<li class="learned said editing"><span class="learned-dot" aria-hidden="true"></span><form class="learned-edit" data-people-edit="${esc(t.id)}">
      <label class="sr-only" for="learned-${esc(t.id)}">Reword</label><textarea id="learned-${esc(t.id)}" name="text" rows="2" maxlength="600">${esc(t.text)}</textarea>
      <div><button class="button primary small" type="submit">Save</button><button class="button small text" type="button" data-people="cancel-edit">Cancel</button></div></form></li>`;
  }
  return `<li class="learned said"><span class="learned-dot" aria-hidden="true"></span><div>
    ${t.quote ? `<p class="learned-quote">“${esc(t.quote)}”</p>` : ''}<p class="learned-text">${esc(t.text)}</p>
    <small>${esc(peopleSource[t.source] || t.source)}${t.question ? ` · asked “${esc(t.question.length > 80 ? t.question.slice(0, 78) + '…' : t.question)}”` : ''} · ${when}
    ${t.editable && p.can_edit ? `<span class="learned-tools"><button type="button" data-people="edit" data-id="${esc(t.id)}">Edit</button><button type="button" data-people="remove" data-id="${esc(t.id)}">Remove</button></span>` : ''}</small>${sourceChip(t.pointer, t.quote || t.text)}</div></li>`;
}

function peopleProfileView() {
  if (!peopleProfile) return '<div class="people"><p class="ask-muted" role="status">Loading…</p></div>';
  const r = peopleProfile, p = r.person, first = peopleFirst(p.name);
  const canEdit = !state.auth?.enabled || state.me?.role === 'admin' || state.me?.id === p.id;
  const chips = (list, cls) => list.map(a => `<span class="person-chip ${cls}" title="${esc(a.note || '')}">${esc(peopleArea(a.scope || a.path))}</span>`).join('');
  const said = r.timeline.filter(t => t.kind === 'said').length, decided = r.timeline.length - said;
  const talked = r.intro.status === 'saved';
  return `<div class="people person"><a class="intro-back" href="#people">← People</a>
    <div class="person-head">${avatar(p.name)}<div><h1>${esc(p.name)}</h1><small>${esc(p.teams.join(' · ') || 'No team yet')}${p.github_login ? ' · @' + esc(p.github_login) : ''}</small></div>
      <div class="person-actions"><a class="button small ${talked ? '' : 'primary'}" href="#intro/${encodeURIComponent(p.id)}">${talked ? 'Talk again' : 'Start a voice intro'}</a>
      <button type="button" class="button small" data-people-ask="${esc(`What has ${p.name} decided or told Raven, and what do they own?`)}">Ask about ${esc(first)}</button></div></div>
    <div class="person-grid"><aside class="person-side">
      ${r.decides.length ? `<div class="person-sec"><span class="ask-label">Decides</span><div>${chips(r.decides, 'decides')}</div></div>` : ''}
      ${r.knows.length ? `<div class="person-sec"><span class="ask-label">Knows about</span><div>${chips(r.knows, 'knows')}</div></div>` : ''}
      ${r.code.length ? `<div class="person-sec"><span class="ask-label">Has been writing</span><div>${chips(r.code, 'code')}</div></div>` : ''}
      <div class="person-sec"><span class="ask-label">Raven has</span><p class="person-count"><b>${said}</b> ${said === 1 ? 'thing' : 'things'} they said · <b>${decided}</b> ${decided === 1 ? 'decision' : 'decisions'} they made</p></div>
    </aside>
    <section class="person-main"><div class="person-main-head"><h2>What Raven has learned</h2><span class="ask-muted">Newest first</span></div>
      ${canEdit ? `<form class="learned-add" id="people-add-form"><label class="sr-only" for="people-add">Add something Raven should know about ${esc(first)}</label><input id="people-add" name="text" maxlength="600" placeholder="Add something Raven should know about ${esc(first)}"><button class="button small" type="submit">Add</button></form>` : ''}
      ${r.timeline.length ? `<ol class="learned-list">${r.timeline.map(t => peopleItem(t, {can_edit: canEdit})).join('')}</ol>`
        : `<p class="ask-muted">Raven hasn't heard from ${esc(first)} yet. A few minutes on a voice intro gives it a head start, and every question ${esc(first)} answers for an agent lands here.</p>`}
    </section></div></div>`;
}

function renderPeople() {
  const id = peopleId();
  if (id ? peopleFor !== id || !peopleProfile : !peopleList) {
    if (!renderPeople.loading) { renderPeople.loading = true; Promise.resolve().then(() => { renderPeople.loading = false; loadPeople(); }); }
  }
  $('.page-head').hidden = true;
  const key = JSON.stringify([id, peopleError, id ? peopleProfile : peopleList, peopleEditing]);
  if (key === peopleKey && $('#app .people')) return;
  const focused = document.activeElement?.id === 'people-search';
  const caret = focused ? document.activeElement.selectionStart : 0;
  peopleKey = key;
  $('#app').innerHTML = (peopleError ? `<p class="intro-error" role="alert">${esc(peopleError)}</p>` : '') + (id ? peopleProfileView() : peopleListView());
  const box = focused && $('#people-search');
  if (box) { box.focus(); box.setSelectionRange(caret, caret); }
}

async function peopleChange(body) {
  try {
    peopleProfile = await api('/api/people/' + encodeURIComponent(peopleFor) + '/context', body);
    peopleEditing = ''; peopleKey = ''; render();
  } catch (error) { notify(error.message); }
}

document.addEventListener('input', event => {
  if (event.target.id !== 'people-search') return;
  peopleQuery = event.target.value;
  clearTimeout(peopleTimer);
  peopleTimer = setTimeout(loadPeople, 160);
});
document.addEventListener('click', event => {
  if (view !== 'people') return;
  const ask = event.target.closest('[data-people-ask]');
  if (ask) { event.preventDefault(); location.hash = '#ask'; setTimeout(() => typeof askRaven === 'function' && askRaven(ask.dataset.peopleAsk), 0); return; }
  const button = event.target.closest('[data-people]');
  if (!button) return;
  const action = button.dataset.people;
  if (action === 'edit') { peopleEditing = button.dataset.id; peopleKey = ''; render(); $(`#learned-${CSS.escape(peopleEditing)}`)?.focus(); }
  if (action === 'cancel-edit') { peopleEditing = ''; peopleKey = ''; render(); }
  if (action === 'remove') peopleChange({action: 'remove', id: button.dataset.id});
});
document.addEventListener('submit', event => {
  if (view !== 'people') return;
  const form = event.target;
  if (form.id === 'people-add-form') {
    event.preventDefault();
    const text = form.text.value.trim();
    if (text) peopleChange({action: 'add', text});
  } else if (form.dataset.peopleEdit) {
    event.preventDefault();
    peopleChange({action: 'edit', id: form.dataset.peopleEdit, text: form.text.value.trim()});
  }
});
window.addEventListener('hashchange', () => {
  peopleKey = ''; peopleEditing = '';
  if (!location.hash.startsWith('#people')) { peopleList = null; peopleProfile = null; peopleFor = ''; }
  else if (!peopleId()) peopleList = null;
});

// A meeting that just ended: paste its transcript and Raven keeps, on each
// teammate's page, the asks and commitments in it, quoted and linked back.
document.addEventListener('click', event => {
  const open = event.target.closest('[data-people="meeting"]');
  if (!open) return;
  openModal('Add a meeting transcript', 'Raven keeps the asks, numbers and commitments on the right teammate’s page, each quoted and linked to the transcript. Nothing becomes a decision.',
    `<form id="meeting-form"><label for="meeting-title">Meeting</label><input id="meeting-title" name="title" maxlength="300" required placeholder="Bloom check-in, Oct 29">
     <label for="meeting-transcript">Transcript</label><textarea id="meeting-transcript" name="transcript" rows="10" required placeholder="Dana (Bloom): We print about 300 labels every morning…"></textarea>
     <p class="error" id="form-error" role="alert" hidden></p><div class="modal-actions"><button type="button" class="button" data-action="close">Cancel</button><button class="button primary" type="submit">Read it</button></div></form>`);
});
document.addEventListener('submit', async event => {
  if (event.target.id !== 'meeting-form') return;
  event.preventDefault();
  const form = event.target, button = form.querySelector('[type="submit"]'), error = form.querySelector('#form-error');
  button.disabled = true; button.textContent = 'Reading…'; error.hidden = true;
  try {
    const repo = (state.question_repositories || [])[0]?.id || (state.runs || []).find(r => r.repo)?.repo || '';
    const r = await api('/api/meetings/learn', {repo, title: form.elements.title.value, transcript: form.elements.transcript.value});
    closeModal(); notify(`Raven kept ${plural(r.learned.length, 'thing')} from ${r.meeting.title}.`);
    peopleList = null; peopleKey = ''; if (view === 'people') { loadPeople(); render(); }
  } catch (e) { error.textContent = e.message; error.hidden = false; button.disabled = false; button.textContent = 'Read it'; }
});
