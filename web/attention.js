'use strict';

// For you (#you): where this person's attention goes. One sentence at the
// top written for the viewer, then only what truly waits on them (and why
// it reached them), what Raven settled on their behalf from calls they
// already made, and their calls that changed. Everything here is scoped to
// the signed-in person and to the tasks being built now; each line opens
// the record it came from.
let attention = null, attentionAt = 0, attentionKey = '';
const attOpen = new Set(), attForm = new Set(), attHand = new Set();
let attShowAll = false;

function loadAttention(force) {
  if (!force && Date.now() - attentionAt < 8000) return;
  attentionAt = Date.now();
  api('/api/attention').then(r => { const before = JSON.stringify(attention); attention = r; if (JSON.stringify(r) !== before) { attentionKey = ''; askKey = ''; if (typeof syncYouBadge === 'function') syncYouBadge(); if (view === 'you' || view === 'ask') render(); } })
    .catch(error => { if (!attention) { attention = {error: error.message}; attentionKey = ''; if (view === 'you') render(); } });
}

const attFirst = name => String(name || '').split(/\s+/)[0];
const attDate = value => { const d = new Date(value); return Number.isNaN(+d) ? '' : d.toLocaleDateString(undefined, {month: 'short', day: 'numeric'}); };
const attWait = value => { const text = ago(value); return text === 'just now' ? 'just now' : text.replace(/ ago$/, ''); };
const attVia = {'browser': 'in Raven', 'slack': 'on Slack', 'teams': 'on Teams', 'voice call': 'on a Raven voice call',
  'call (typed)': 'on a Raven call', 'meeting': 'in a meeting', 'github': 'in a GitHub review', 'agent': 'in Claude Code', 'operator': 'in Raven'};
const attHref = (runId, id) => `#runs/${encodeURIComponent(runId)}${id ? '/' + encodeURIComponent(id) : ''}`;
const attThings = n => `${n} thing${n === 1 ? '' : 's'}`;

// The one line at the top, written for the person reading it.
function attHeadline(a) {
  const n = a.needs_you.length, h = a.handled.length;
  if (!n && !h) return {lead: 'Nothing needs you.', sub: a.counts.live_tasks ? `${plural(a.counts.live_tasks, 'task')} being built, ${a.counts.asked_elsewhere ? `${plural(a.counts.asked_elsewhere, 'open call')} with other people` : 'nothing waiting on anyone'}, none of it yours.` : 'No task is being built right now.'};
  if (!n) return {lead: `Nothing needs you. Raven handled ${attThings(h)} for you.`, sub: `Settled from calls you already made, without asking you. ${a.counts.asked_elsewhere ? `${plural(a.counts.asked_elsewhere, 'other call')} went to other people.` : ''}`};
  return {lead: `Raven needs you for ${attThings(n)}.`, sub: h ? `It handled ${attThings(h)} for you from calls you already made, without asking.${a.counts.asked_elsewhere ? ` ${plural(a.counts.asked_elsewhere, 'other call')} went to other people.` : ''}`
    : `${a.counts.asked_elsewhere ? `${plural(a.counts.asked_elsewhere, 'other call')} went to other people, not you.` : 'Everything else on the team’s tasks is settled.'}`};
}

function attNeeds(item) {
  const open = attForm.has(item.id);
  const guessFrom = item.guess_from;
  return `<article class="att-need" data-id="${esc(item.id)}">
    <div class="att-meta"><span class="att-task"><a href="${attHref(item.run_id)}">${esc(item.task)}</a></span><span>${esc(item.agent || 'Coding agent')}${item.requester ? ' · for ' + esc(attFirst(item.requester)) : ''}</span><span class="att-wait">waiting ${esc(attWait(item.created_at))}</span></div>
    <h3>${esc(item.question)}</h3>
    <p class="att-why"><span class="att-k">Why it reached you</span>${esc(item.why)}</p>
    ${item.guess ? `<div class="att-guess"><span class="att-k">Raven would apply this, but isn’t sure: your call</span>
      <p>“${esc(callRefs(item.guess))}”</p>${attGaps(item)}
      ${guessFrom ? `<small>from ${esc(guessFrom.by === attention.person.name ? 'your' : attFirst(guessFrom.by) + '’s')} ${esc(attDate(guessFrom.when))} call${attVia[guessFrom.answered_via] ? ' ' + esc(attVia[guessFrom.answered_via]) : ''} on <a href="${attHref(guessFrom.run_id, guessFrom.id)}">${esc(guessFrom.task)}</a></small>` : ''}
      <button type="button" class="button small" data-att="apply" data-id="${esc(item.id)}">Use this answer</button></div>` : attGaps(item)}
    ${item.options.length ? `<div class="att-options"><span class="att-k">The agent’s options</span>${item.options.map((o, i) => `<button type="button" class="att-option" data-att="option" data-id="${esc(item.id)}" data-n="${i}">${esc(o)}</button>`).join('')}</div>` : ''}
    <p class="att-meanwhile">${esc(item.meanwhile)}</p>
    ${item.context ? `<details class="att-found"><summary>What the agent found</summary><p>${esc(item.context)}</p></details>` : ''}
    <form class="att-answer" data-id="${esc(item.id)}" data-updated="${esc(item.updated_at)}">
      <textarea name="answer" rows="${open ? 3 : 2}" maxlength="12000" required placeholder="Your answer, in a sentence or two…"></textarea>
      <input name="rationale" maxlength="12000" placeholder="Why, in one line (optional; Raven reuses it next time)">
      <p class="ask-error" hidden></p>
      <div class="att-actions"><button class="button small primary" type="submit">Record my answer</button>
        ${state.auth?.enabled && state.me?.id && ['session', 'token'].includes(state.me.kind) ? `<button class="button small text" type="button" data-action="interview-start" data-task="${esc(item.run_id)}" data-id="${esc(item.id)}">Take a quick call instead</button>` : ''}
        <button class="button small text" type="button" data-att="handon" data-id="${esc(item.id)}" aria-expanded="${attHand.has(item.id)}">Not mine: hand it on</button>
        <a class="att-open" href="${attHref(item.run_id, item.id)}">Full record ↗</a></div>
    </form>
    ${attHand.has(item.id) ? attHandOn(item) : ''}
  </article>`;
}

// Hand the call to the person who should make it, from the card itself: whoever is picked sees it on
// their page with the note as the reason it reached them, and once they answer, calls like it route to
// them first (the same hand-on as the full record).
function attHandOn(item) {
  // Owners carry ownership-map ids, not person ids: leave the signed-in person out by name as well.
  const people = (state.owners || []).filter(o => o.id !== state.me?.id && o.name !== state.me?.name);
  return `<form class="att-handon" data-id="${esc(item.id)}" data-updated="${esc(item.updated_at)}">
      <span class="att-k">Hand this to</span>
      <select name="person" aria-label="Hand on to" required><option value="">Pick a person</option>${people.map(o => `<option value="${esc(o.name)}">${esc(o.name)}${o.team ? ' · ' + esc(o.team) : ''}</option>`).join('')}</select>
      <input name="note" maxlength="200" placeholder="A word for them (optional)">
      <p class="ask-error" hidden></p>
      <div class="att-actions"><button class="button small primary" type="submit">Hand on</button><button class="button small text" type="button" data-att="cancel-handon" data-id="${esc(item.id)}">Cancel</button></div>
    </form>`;
}

// What the source left open: Raven's own note, never inside the quote.
function attGaps(item) {
  const gaps = Array.isArray(item.gaps) ? item.gaps.map(g => String(g || '').trim()).filter(Boolean) : [];
  return gaps.length ? `<p class="att-why att-gaps"><span class="att-k">Raven could not find</span>${esc(callRefs(gaps.join('; ')))}.</p>` : '';
}

function attHandled(item) {
  const open = attOpen.has(item.id);
  const call = item.call || {};
  return `<li class="att-handled ${open ? 'open' : ''}" data-id="${esc(item.id)}">
    <button type="button" class="att-line" data-att="toggle" data-id="${esc(item.id)}" aria-expanded="${open}">
      <span class="att-q">${esc(item.question)}</span>
      <span class="att-from">from ${call.answered_via === 'meeting' ? `what you said in ${esc(call.task || 'a meeting')}` : `your ${esc(attDate(call.when))} call${attVia[call.answered_via] ? ' ' + esc(attVia[call.answered_via]) : ''}`} · <em>${esc(item.task)}</em></span>
    </button>
    ${open ? `<div class="att-detail">
      <p class="att-a">“${esc(callRefs(item.answer))}”</p>
      ${attGaps(item)}
      <p class="att-why"><span class="att-k">Why it fits</span>${esc(item.fit_reason || 'Raven matched it to your earlier call.')}</p>
      <p class="att-call"><span class="att-k">${call.answered_via === 'meeting' ? 'What you said' : 'Your call it used'}</span>${call.answered_via === 'meeting' ? `in ${esc(call.task)}` : `${esc(attDate(call.when))}${attVia[call.answered_via] ? ', ' + esc(attVia[call.answered_via]) : ''}, on <a href="${attHref(call.run_id, call.id)}">${esc(call.task)}</a>`}: “${esc(call.answer)}”</p>
      <div class="att-actions"><button type="button" class="button small" data-att="correct" data-id="${esc(item.id)}">That’s wrong, or doesn’t apply here</button><a class="att-open" href="${attHref(item.run_id, item.id)}">Full record ↗</a></div>
      ${attForm.has(item.id) ? `<form class="att-correct" data-id="${esc(item.id)}" data-updated="${esc(item.updated_at)}">
        <label>The right answer<textarea name="answer" rows="3" maxlength="12000" required>${esc(item.answer)}</textarea></label>
        <label>Why your earlier call doesn’t hold here<textarea name="why" rows="2" maxlength="12000" required placeholder="One or two sentences. This becomes the record under your name."></textarea></label>
        <div class="att-scope"><label><input type="radio" name="scope" value="everywhere" checked> My earlier call was wrong: replace it, so the next similar question uses this</label>
          <label><input type="radio" name="scope" value="here"> It doesn’t apply to this case: keep my earlier call for other cases</label></div>
        <p class="ask-error" hidden></p>
        <div class="att-actions"><button class="button small primary" type="submit">Record under my name</button><button class="button small text" type="button" data-att="cancel" data-id="${esc(item.id)}">Cancel</button></div>
      </form>` : ''}
    </div>` : ''}
  </li>`;
}

function attChanged(item) {
  const me = attention.person.name;
  const who = item.by === me ? 'You' : esc(attFirst(item.by));
  const whose = item.was_mine ? (item.by === me ? 'your' : 'your') : `${esc(attFirst(item.replaced.by))}’s`;
  return `<li class="att-change">
    <p><b>${who} replaced ${whose} ${esc(attDate(item.replaced.when))} call</b> on <a href="${attHref(item.run_id, item.id)}">${esc(item.task)}</a> · ${esc(attDate(item.when))}</p>
    <p class="att-a">Now: “${esc(callRefs(item.answer))}”</p>
    ${item.why ? `<p class="att-why"><span class="att-k">Why</span>${esc(item.why)}</p>` : ''}
    <p class="att-muted">Was: “${esc(item.replaced.answer)}”${item.now_precedent ? ' · The next similar question uses the new call.' : ''}</p>
  </li>`;
}

function renderAttention() {
  $('.page-head').hidden = true;
  loadAttention();
  const key = JSON.stringify(['you', attention, [...attOpen], [...attForm], attShowAll, state.me?.id]);
  if (key === attentionKey && $('#app .att')) return;
  attentionKey = key;
  const a = attention;
  if (!a) { $('#app').innerHTML = '<div class="att"><p class="ask-muted" role="status">Reading what waits on you…</p></div>'; return; }
  if (a.error) { $('#app').innerHTML = `<div class="att"><p class="ask-error" role="alert">${esc(a.error)}</p></div>`; return; }
  const {lead, sub} = attHeadline(a);
  const handled = attShowAll ? a.handled : a.handled.slice(0, 3);
  const name = attFirst(a.person.name);
  $('#app').innerHTML = `<div class="att ${a.needs_you.length ? '' : 'is-calm'}">
    <header class="att-head"><span class="ask-orb ${a.needs_you.length ? '' : 'big'}" aria-hidden="true"></span>
      <div><p class="att-hi">${name ? esc(name) + ', ' : ''}${esc(new Date().getHours() < 12 ? 'good morning' : new Date().getHours() < 18 ? 'good afternoon' : 'good evening')}.</p>
      <h1>${esc(lead)}</h1><p class="att-sub">${esc(sub)}</p></div></header>
    ${a.needs_you.length ? `<section class="att-sec"><h2 class="ask-label">Needs you</h2>${a.needs_you.map(attNeeds).join('')}</section>` : ''}
    ${a.handled.length ? `<section class="att-sec"><h2 class="ask-label">Handled without you <span>${a.handled.length}</span></h2>
      <p class="att-lede">Raven applied your judgment on your behalf. Each one names the call it used and why it fits. If one is wrong, say so and it becomes the new record under your name.</p>
      <ul class="att-list">${handled.map(attHandled).join('')}</ul>
      ${a.handled.length > 3 ? `<button type="button" class="button small text" data-att="all">${attShowAll ? 'Show fewer' : `Show all ${a.handled.length}`}</button>` : ''}</section>` : ''}
    ${a.changed.length ? `<section class="att-sec"><h2 class="ask-label">Your calls that changed</h2><ul class="att-list">${a.changed.map(attChanged).join('')}</ul></section>` : ''}
    ${!a.needs_you.length && !a.handled.length && !a.changed.length ? `<p class="att-calm-note">When a coding agent hits a call only you can make, it shows up here with why it reached you. When Raven settles one from a call you already made, it shows up here too, so you can check it.</p>` : ''}
  </div>`;
}
window.addEventListener('hashchange', () => { if (location.hash.startsWith('#you')) attentionKey = ''; });

document.addEventListener('click', event => {
  const button = event.target.closest('[data-att]');
  if (!button || view !== 'you') return;
  const {att, id, n} = button.dataset;
  if (att === 'toggle') { if (attOpen.has(id)) { attOpen.delete(id); attForm.delete(id); } else attOpen.add(id); }
  else if (att === 'correct') attForm.add(id);
  else if (att === 'cancel') attForm.delete(id);
  else if (att === 'handon') attHand.add(id);
  else if (att === 'cancel-handon') attHand.delete(id);
  else if (att === 'all') attShowAll = !attShowAll;
  else if (att === 'apply' || att === 'option') {
    const item = (attention?.needs_you || []).find(x => x.id === id);
    const box = document.querySelector(`.att-answer[data-id="${CSS.escape(id)}"] textarea[name=answer]`);
    if (item && box) { box.value = att === 'apply' ? callRefs(item.guess) : item.options[+n]; box.focus(); }
    return;
  }
  attentionKey = ''; render();
  if (att === 'correct') document.querySelector(`.att-correct[data-id="${CSS.escape(id)}"] textarea[name=why]`)?.focus();
  if (att === 'handon') document.querySelector(`.att-handon[data-id="${CSS.escape(id)}"] select`)?.focus();
});

document.addEventListener('submit', async event => {
  const form = event.target.closest('.att-answer, .att-correct, .att-handon');
  if (!form || view !== 'you') return;
  event.preventDefault();
  const data = Object.fromEntries(new FormData(form).entries());
  const error = form.querySelector('.ask-error');
  const button = form.querySelector('button[type=submit]');
  const label = button.textContent;
  button.disabled = true; button.textContent = 'Recording…';
  try {
    let result;
    if (form.classList.contains('att-handon')) {
      if (!data.person) throw new Error('Pick the person to hand it to first.');
      result = await api(`/api/decisions/${encodeURIComponent(form.dataset.id)}/refer`, {person: data.person, ...(data.note ? {note: data.note} : {}), expected_updated_at: form.dataset.updated});
      attHand.delete(form.dataset.id);
      notify(result.notice || `Handed on to ${data.person}.`);
    } else if (form.classList.contains('att-correct')) {
      result = await api(`/api/decisions/${encodeURIComponent(form.dataset.id)}/correct`, {answer: data.answer, why: data.why, scope: data.scope || 'here', expected_updated_at: form.dataset.updated});
      attForm.delete(form.dataset.id);
      notify(result.notice || 'Recorded.');
    } else {
      // A call that leaned on an earlier call binds the answer to the source versions the card
      // showed, the way the task page does; without them the answer is refused for review.
      const review = (attention?.needs_you || []).find(i => i.id === form.dataset.id)?.review;
      if (review?.has_reliance && !review.available) throw new Error('The earlier call this leans on is not readable right now. Try again in a moment.');
      result = await api(`/api/decisions/${encodeURIComponent(form.dataset.id)}/answer`, {answer: data.answer, ...(data.rationale ? {rationale: data.rationale} : {}),
        ...(review?.has_reliance ? {source_evidence: review.pins, source_decision_pins: review.decision_pins} : {}), expected_updated_at: form.dataset.updated});
      notify('Recorded under your name. The coding agent can read it now.');
    }
    attentionKey = '';
    loadAttention(true);
    await refresh();
  } catch (err) {
    error.textContent = err.message; error.hidden = false;
    button.disabled = false; button.textContent = label;
  }
});
