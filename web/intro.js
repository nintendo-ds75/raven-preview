'use strict';

// Intro chats (#intro, #intro/<person id>): a fifteen-minute get-to-know-you
// interview per person. Raven prepares from what the workspace already
// records, asks broad to specific, follows up, and ends with notes the
// person reviews before anything is saved. Speech recognition is a browser
// capability; Raven stores only the text.
let introList = null, introData = null, introPerson = '', introBusy = '', introError = '', introKey = '';
let introRecognition = null, introTimer = null, introDraft = '';
const introStatusLabel = {not_started: 'Not started', ready: 'Prepared', active: 'In progress', review: 'Waiting for their review', saved: 'Done', cancelled: 'Not started'};
const introTopicLabel = {background: 'Background', role: 'Role', team: 'Team and people', current: 'Current work', unwritten: 'Unwritten knowledge', other: 'Other'};
const introPersonId = () => { try { return view === 'intro' ? decodeURIComponent(location.hash.slice(1).split('/')[1] || '') : ''; } catch { return ''; } };
const introClock = minutes => { const s = Math.max(0, Math.round(minutes * 60)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
const introPlural = (n, one, many = one + 's') => `${n} ${n === 1 ? one : many}`;

function loadIntro() {
  const person = introPersonId();
  introError = '';
  if (!person) {
    introData = null;
    api('/api/intro').then(result => { introList = result; render(); }).catch(error => { introError = error.message; render(); });
    return;
  }
  if (person !== introPerson) { introData = null; introDraft = ''; }
  introPerson = person;
  api('/api/intro/people/' + encodeURIComponent(person)).then(result => { introData = result; render(); })
    .catch(error => { introError = error.message; render(); });
}

function introStages(row) {
  const reached = row.turns.length ? row.stages.findIndex(s => s.key === row.turns[row.turns.length - 1].stage) : -1;
  return `<ol class="intro-arc" aria-label="Interview arc">${row.stages.map((s, i) => `<li class="${i < reached ? 'done' : i === reached ? 'now' : ''}">${esc(s.label)}</li>`).join('')}</ol>`;
}

function introKnown(d) {
  const facts = d.facts.length ? `<ul class="intro-facts">${d.facts.map(f => `<li>${esc(f.text).replace(/`([^`]+)`/g, '<code>$1</code>')}</li>`).join('')}</ul>`
    : '<p class="intro-muted">Nothing yet. Add them to a team or connect a repository, and Raven will skip the basics.</p>';
  return `<section class="intro-panel"><h2>What Raven already knows</h2><p class="intro-muted">From the workspace: teams, the authority map and git history. Raven won't ask about these.</p>${facts}<h3>What it can't know from records</h3><ul class="intro-gaps">${d.gaps.map(g => `<li>${esc(g)}</li>`).join('')}</ul></section>`;
}

function introBubble(who, text, extra = '') {
  return `<div class="intro-msg ${who}">${who === 'raven' ? '<span class="intro-who">Raven</span>' : '<span class="intro-who">You</span>'}<div class="intro-bubble">${text}</div>${extra}</div>`;
}

function introChat(row) {
  const parts = [introBubble('raven', esc(row.opening))];
  for (const t of row.turns) {
    const ask = `${t.ack ? `<p>${esc(t.ack)}</p>` : ''}<p>${esc(t.question)}</p>`;
    const why = t.why ? `<small class="intro-why">${t.kind === 'follow_up' ? 'Follow-up' : 'Why I’m asking'}: ${esc(t.why)}</small>` : '';
    parts.push(introBubble('raven', ask, why));
    if (t.answer) parts.push(introBubble('person', esc(t.answer).replace(/\n/g, '<br>')));
    else if (t.skipped) parts.push('<p class="intro-skip">Skipped</p>');
  }
  if (introBusy === 'answer') {
    if (introDraft) parts.push(introBubble('person', esc(introDraft).replace(/\n/g, '<br>')));
    parts.push('<div class="intro-msg raven"><span class="intro-who">Raven</span><div class="intro-bubble intro-thinking" role="status"><i></i><i></i><i></i><span class="sr-only">Raven is thinking</span></div></div>');
  }
  return parts.join('');
}

function introComposer(row) {
  const speech = !!(window.SpeechRecognition || window.webkitSpeechRecognition) && window.isSecureContext;
  const busy = !!introBusy;
  return `<form class="intro-composer" id="intro-answer-form"><label class="sr-only" for="intro-answer">Your answer</label><textarea id="intro-answer" rows="3" maxlength="4000" placeholder="Type your answer, or use the microphone." ${busy ? 'disabled' : ''}>${busy ? '' : esc(introDraft)}</textarea><div class="intro-composer-row"><div><button type="button" class="button small" data-intro="mic" ${speech && !busy ? '' : 'disabled'} aria-pressed="${introRecognition ? 'true' : 'false'}">${introRecognition ? 'Stop microphone' : 'Use microphone'}</button><button type="button" class="button text small" data-intro="skip" ${busy ? 'disabled' : ''}>Skip this question</button></div><div><button type="button" class="button text small" data-intro="finish" ${busy ? 'disabled' : ''}>End the chat</button><button type="submit" class="button primary small" ${busy ? 'disabled' : ''}>Send</button></div></div>${speech ? '' : '<p class="intro-muted">Dictation needs HTTPS and a browser that supports it. Typing works everywhere.</p>'}</form>`;
}

function introReview(row) {
  const s = row.stats, notes = row.summary.notes || [];
  const groups = Object.keys(introTopicLabel).map(topic => [topic, notes.filter(n => n.topic === topic)]).filter(([, list]) => list.length);
  const honest = `${introPlural(s.questions, 'question')} answered${s.follow_ups ? `, ${introPlural(s.follow_ups, 'follow-up')} among them` : ''}, over ${introPlural(Math.max(1, s.minutes), 'minute')}.`;
  const mode = row.summary.mode === 'model' ? 'Raven drafted these notes from your answers. Each one quotes what it rests on.' : 'No model is configured, so each note is the first sentence of an answer. Edit them into something useful.';
  const dropped = row.summary.dropped ? ` Raven dropped ${introPlural(row.summary.dropped, 'draft note')} it couldn't tie to something you said.` : '';
  const list = groups.map(([topic, items]) => `<section class="intro-note-group"><h3>${esc(introTopicLabel[topic])}</h3>${items.map(n => `<div class="intro-note" data-note="${esc(n.id)}"><label class="intro-keep"><input type="checkbox" checked data-keep="${esc(n.id)}"><span class="sr-only">Keep this note</span></label><div><textarea rows="2" maxlength="600" data-note-text="${esc(n.id)}" aria-label="Note">${esc(n.text)}</textarea><small>You said: “${esc(n.quote)}”</small></div></div>`).join('')}</section>`).join('');
  const unknown = (row.summary.still_unknown || []).length ? `<section class="intro-panel"><h2>What Raven still doesn't know</h2><ul class="intro-gaps">${row.summary.still_unknown.map(u => `<li>${esc(u)}</li>`).join('')}</ul></section>` : '';
  const transcript = `<details class="intro-transcript"><summary>Full transcript</summary>${row.turns.map(t => `<p><strong>Raven:</strong> ${esc(t.question)}</p><p><strong>You:</strong> ${t.answer ? esc(t.answer) : '<em>Skipped</em>'}</p>`).join('')}</details>`;
  return `<div class="intro-review"><div class="intro-review-main"><p class="intro-label">Review before saving</p><h2>What Raven took away</h2><p>${esc(honest)} ${esc(mode)}${esc(dropped)}</p><p class="intro-muted">These are notes on what you said, not verified facts. Uncheck anything wrong or private, and fix the wording. Only what you keep is saved, and your teammates' Raven will use it as background when it routes questions and writes briefs.</p>${notes.length ? list : '<p class="intro-muted">No notes were drafted. You can add your own below.</p>'}<label for="intro-add">Add a note in your own words (optional)</label><textarea id="intro-add" rows="2" maxlength="600" placeholder="Anything you want Raven to know that didn't come up."></textarea><div class="intro-actions"><button class="button text" data-intro="cancel">Discard</button><button class="button primary" data-intro="save" ${introBusy ? 'disabled' : ''}>Save these notes</button></div>${transcript}</div><aside>${unknown}</aside></div>`;
}

function introSaved(context, name) {
  if (!context.length) return '';
  const groups = Object.keys(introTopicLabel).map(topic => [topic, context.filter(n => n.topic === topic)]).filter(([, list]) => list.length);
  return `<section class="intro-panel"><h2>Saved from ${esc(name.split(' ')[0])}'s intro chat</h2><p class="intro-muted">In their words, reviewed by them. Not verified.</p>${groups.map(([topic, items]) => `<h3>${esc(introTopicLabel[topic])}</h3><ul class="intro-facts">${items.map(n => `<li>${esc(n.text)}${n.quote ? `<small>“${esc(n.quote)}”</small>` : ''}</li>`).join('')}</ul>`).join('')}</section>`;
}

function introListView() {
  if (!introList) return '<p class="context" role="status">Loading…</p>';
  const rows = introList.people.map(p => {
    const state = introStatusLabel[p.status] || p.status;
    const action = p.can_take ? `<a class="button small ${p.status === 'not_started' || p.status === 'cancelled' ? 'primary' : ''}" href="#intro/${esc(p.id)}">${p.status === 'saved' ? 'View notes' : p.status === 'active' ? 'Continue' : p.status === 'review' ? 'Review notes' : 'Open'}</a>` : '';
    return `<li class="intro-row">${avatar(p.name)}<div><strong>${esc(p.name)}</strong><small>${esc(p.team || 'No team yet')}</small></div><span class="intro-state ${esc(p.status)}">${esc(state)}${p.notes ? ` · ${introPlural(p.notes, 'note')}` : ''}</span>${action}</li>`;
  }).join('');
  return `<div class="intro"><div class="intro-intro"><p>Raven holds a ${introList.duration_minutes}-minute intro chat with each person. It reads what the workspace already knows about them first, so it skips the basics and asks about what isn't written down: how they actually work, what they're in the middle of, and why things are the way they are.</p><p class="intro-muted">Each person reviews Raven's notes before anything is saved.</p></div>${introList.people.length ? `<ul class="intro-people">${rows}</ul>` : '<p class="intro-muted">Add people first, under People.</p>'}</div>`;
}

function introPersonView() {
  if (!introData) return '<p class="context" role="status">Loading…</p>';
  const row = introData.interview, person = (directory?.people || []).find(p => p.id === introPersonId());
  const name = row?.dossier.person.name || person?.name || 'This person';
  const back = '<a class="intro-back" href="#intro">← All intro chats</a>';
  if (!row || ['saved', 'cancelled'].includes(row.status)) {
    const saved = introSaved(introData.context, name);
    const label = introBusy === 'prepare' ? 'Reading the workspace and writing questions…' : saved ? 'Prepare a new intro chat' : 'Prepare the intro chat';
    const known = introData.dossier ? introKnown(introData.dossier) : '';
    return `<div class="intro">${back}<div class="intro-hero"><h2>${esc(name)}</h2><p>${saved ? 'Their intro chat is done.' : `Raven starts from what the workspace already records about ${esc(name.split(' ')[0])}, then prepares about eight questions that only they can answer. The chat takes fifteen minutes.`}</p><button class="button ${saved ? '' : 'primary'}" data-intro="prepare" ${introBusy ? 'disabled aria-busy="true"' : ''}>${esc(label)}</button></div><div class="intro-columns">${known}${saved || ''}</div></div>`;
  }
  const d = row.dossier;
  if (row.status === 'ready') {
    const plan = `<section class="intro-panel"><h2>Questions Raven prepared</h2><p class="intro-muted">${row.mode === 'model' ? 'Written for ' + esc(d.person.first_name) + ' from the record on the left. Raven will follow up and reword as the chat goes.' : 'No model is configured, so these come from templates filled in from the record, and there are no follow-ups.'}</p><ol class="intro-plan">${row.plan.map(q => `<li><span class="intro-stage">${esc((row.stages.find(s => s.key === q.stage) || {}).label || q.stage)}</span>${esc(q.text)}</li>`).join('')}</ol></section>`;
    return `<div class="intro">${back}<div class="intro-hero"><h2>Intro chat with ${esc(d.person.name)}</h2><p>${esc(row.opening)}</p><div class="intro-actions"><button class="button primary" data-intro="start" ${introBusy ? 'disabled' : ''}>Start the ${row.duration_minutes}-minute chat</button><button class="button text" data-intro="prepare" ${introBusy ? 'disabled' : ''}>${introBusy === 'prepare' ? 'Preparing…' : 'Prepare again'}</button></div></div><div class="intro-columns">${introKnown(d)}${plan}</div></div>`;
  }
  if (row.status === 'review') return `<div class="intro">${back}${introBusy === 'finish' ? '' : introReview(row)}</div>`;
  const left = Math.max(0, row.duration_minutes - row.elapsed_minutes);
  return `<div class="intro">${back}<div class="intro-live"><div class="intro-live-head"><div><h2>Intro chat with ${esc(d.person.name)}</h2>${introStages(row)}</div><div class="intro-clock"><span id="intro-clock" data-elapsed="${row.elapsed_minutes}" data-drawn="${Date.now()}" data-paused="${introBusy ? 1 : 0}" data-duration="${row.duration_minutes}">${introClock(left)}</span><small>left of ${row.duration_minutes}:00</small></div></div><div class="intro-live-body"><div class="intro-thread" id="intro-thread" aria-live="polite">${introChat(row)}${introBusy === 'finish' ? '<p class="intro-muted" role="status">Writing up what Raven heard…</p>' : ''}</div><aside>${introKnown(d)}</aside></div>${introComposer(row)}</div></div>`;
}

function renderIntro() {
  const person = introPersonId();
  if (directory === null && typeof loadDirectory === 'function') loadDirectory();
  if (person ? !introData || introPerson !== person : !introList) {
    if (!introBusy) { introBusy = 'load'; Promise.resolve().then(() => { introBusy = ''; loadIntro(); }); }
  }
  // The app redraws every few seconds. Rebuild only when something we
  // show changed, so a half-typed answer and the scroll position survive.
  const key = JSON.stringify([person, introBusy, introError, person ? introData : introList, !!introRecognition]);
  if (key === introKey && $('#app .intro')) return;
  const typed = $('#intro-answer')?.value;
  if (typed !== undefined && !introBusy) introDraft = typed;
  introKey = key;
  const actions = $('#page-actions');
  if (actions && !person) actions.innerHTML = '';
  $('.page-head').hidden = !!person;
  $('#app').innerHTML = (introError ? `<p class="intro-error" role="alert">${esc(introError)}</p>` : '') + (person ? introPersonView() : introListView());
  const thread = $('#intro-thread');
  if (thread) thread.scrollTop = thread.scrollHeight;
  const box = $('#intro-answer');
  if (box && !introBusy) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
  introTick();
}

function introTick() {
  clearInterval(introTimer);
  const clock = $('#intro-clock');
  if (!clock) return;
  // The server's elapsed time leaves out Raven's thinking, so the clock
  // runs from that figure and stands still while Raven is thinking.
  const elapsed = Number(clock.dataset.elapsed), drawn = Number(clock.dataset.drawn), total = Number(clock.dataset.duration);
  const paused = clock.dataset.paused === '1';
  const update = () => {
    const el = $('#intro-clock');
    if (!el) { clearInterval(introTimer); return; }
    const left = total - elapsed - (paused ? 0 : (Date.now() - drawn) / 60000);
    el.textContent = introClock(left);
    el.classList.toggle('low', left <= 2);
  };
  update();
  introTimer = setInterval(update, 1000);
}

async function introCall(action, path, body) {
  introBusy = action; introError = ''; render();
  try {
    const result = await api(path, body);
    if (result && result.status) introData = {...(introData || {}), interview: result};
    if (action === 'save' || action === 'cancel') introData = await api('/api/intro/people/' + encodeURIComponent(introPersonId()));
    return result;
  } catch (error) {
    introError = error.message;
  } finally {
    introBusy = ''; render();
  }
}

function stopIntroMic() {
  if (!introRecognition) return;
  const rec = introRecognition;
  introRecognition = null;
  rec.onresult = rec.onerror = rec.onend = null;
  rec.stop();
}

function startIntroMic() {
  const Speech = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Speech) return;
  const rec = new Speech();
  rec.lang = document.documentElement.lang || 'en-US';
  rec.continuous = true;
  rec.interimResults = false;
  rec.onresult = event => {
    const box = $('#intro-answer');
    if (!box) return;
    for (let i = event.resultIndex; i < event.results.length; i++) {
      if (event.results[i].isFinal) box.value = (box.value.trim() + ' ' + event.results[i][0].transcript.trim()).trim();
    }
    introDraft = box.value;
    box.dataset.method = 'browser-speech';
  };
  rec.onerror = event => { introError = 'The microphone stopped: ' + (event.error || 'unknown error') + '. Your text is still here.'; stopIntroMic(); render(); };
  rec.onend = () => { if (introRecognition === rec) { introRecognition = null; render(); } };
  introRecognition = rec;
  rec.start();
}

document.addEventListener('click', async event => {
  const button = event.target.closest('[data-intro]');
  if (!button || view !== 'intro') return;
  event.preventDefault();
  const row = introData?.interview, base = row ? `/api/intro/${encodeURIComponent(row.id)}` : '';
  const action = button.dataset.intro;
  if (action === 'prepare') return introCall('prepare', `/api/intro/people/${encodeURIComponent(introPersonId())}/prepare`, {});
  if (action === 'start') return introCall('start', base + '/start', {expected_version: row.version});
  if (action === 'mic') { if (introRecognition) stopIntroMic(); else startIntroMic(); introKey = ''; return render(); }
  if (action === 'skip') { stopIntroMic(); introDraft = ''; return introCall('answer', base + '/skip', {expected_version: row.version}); }
  if (action === 'finish') {
    if (!confirm('End the chat now? Raven will write up what it heard so far.')) return;
    stopIntroMic(); return introCall('finish', base + '/finish', {expected_version: row.version});
  }
  if (action === 'cancel') {
    if (!confirm('Discard this chat? Nothing from it will be saved.')) return;
    return introCall('cancel', base + '/cancel', {expected_version: row.version});
  }
  if (action === 'save') {
    const notes = [...document.querySelectorAll('[data-keep]')].filter(box => box.checked)
      .map(box => ({id: box.dataset.keep, text: document.querySelector(`[data-note-text="${CSS.escape(box.dataset.keep)}"]`).value}));
    const added = $('#intro-add')?.value.trim();
    const result = await introCall('save', base + '/save', {expected_version: row.version, notes, added: added ? [{text: added, topic: 'other'}] : []});
    if (result) notify(`Saved ${introPlural(notes.length + (added ? 1 : 0), 'note')}.`);
  }
});

document.addEventListener('submit', event => {
  if (event.target.id !== 'intro-answer-form') return;
  event.preventDefault();
  const box = $('#intro-answer'), row = introData?.interview;
  const text = box.value.trim();
  if (!text || !row) return;
  const method = box.dataset.method === 'browser-speech' ? 'browser-speech' : 'typed';
  stopIntroMic();
  introDraft = text;
  introCall('answer', `/api/intro/${encodeURIComponent(row.id)}/answer`, {expected_version: row.version, answer: text, capture_method: method})
    .then(result => { if (result) introDraft = ''; introKey = ''; render(); });
});

document.addEventListener('keydown', event => {
  if (event.target.id === 'intro-answer' && event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
    event.preventDefault();
    event.target.form.requestSubmit();
  }
});
document.addEventListener('input', event => { if (event.target.id === 'intro-answer') introDraft = event.target.value; });
window.addEventListener('hashchange', () => { introKey = ''; introData = null; introList = null; stopIntroMic(); });
