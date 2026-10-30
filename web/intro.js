'use strict';

// Voice intros (#intro, #intro/<person id>): a short spoken call per person.
// Raven prepares from what the workspace already records, says each
// question out loud, listens, follows up, and keeps what it heard the
// moment the call ends. Speech in and out goes through voice.js (the
// server's voice and transcripts when set up, else the browser's); Raven
// stores only the text.
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

// Raven talks; the person talks back. One orb, one caption, the words
// they are saying as they say them, and an End call button. No bubbles.
let introSpokenFor = -1, introSpeaking = false, introHeard = '', introTyping = false, introMuted = false;
const introSpeech = () => RavenVoice.canListen();
const introVoiceOut = () => RavenVoice.canSpeak();

function introOrb(mode) {
  return RavenVoice.orb(mode === 'speaking' && !RavenVoice.usesServer().tts ? 'speaking pulse' : mode);
}

function introKnownStrip(d) {
  const facts = (d.facts || []).slice(0, 4).map(f => `<span>${esc(f.text.replace(/`/g, ''))}</span>`).join('');
  return facts ? `<div class="voice-known"><span class="ask-label">Raven starts from what's already on record</span>${facts}</div>` : '';
}

function introNotes(context) {
  if (!context.length) return '';
  return `<div class="voice-notes"><span class="ask-label">What Raven heard</span>${context.slice(0, 6).map(saidQuote).join('')}</div>`;
}

function introListView() {
  if (!introList) return '<p class="context" role="status">Loading…</p>';
  const rows = introList.people.map(p => {
    const done = p.status === 'saved';
    const action = p.can_take ? `<a class="button small ${done ? '' : 'primary'}" href="#intro/${esc(p.id)}">${done ? 'Talk again' : p.status === 'active' ? 'Rejoin' : 'Start'}</a>` : '';
    return `<li class="intro-row">${avatar(p.name)}<div><strong>${esc(p.name)}</strong><small>${esc(p.team || 'No team yet')}</small></div><span class="intro-state ${esc(p.status)}">${done ? `Talked with Raven${p.notes ? ' · ' + introPlural(p.notes, 'note') : ''}` : p.status === 'active' ? 'On a call' : 'Not yet'}</span>${action}</li>`;
  }).join('');
  return `<div class="intro"><div class="intro-intro"><p>A short voice intro with each person gives Raven a head start: what isn't written down about how they work and why things are the way they are. After that Raven keeps learning from the questions your agents ask and the answers people give.</p></div>${introList.people.length ? `<ul class="intro-people">${rows}</ul>` : '<p class="intro-muted">Add people first.</p>'}</div>`;
}

function introPersonView() {
  if (!introData) return '<p class="context" role="status">Loading…</p>';
  const row = introData.interview, person = (directory?.people || []).find(p => p.id === introPersonId());
  const name = row?.dossier?.person?.name || introData.dossier?.person?.name || person?.name || '';
  // Right after a switch the person may not be loaded yet: wait for the name.
  if (!name) { if (!directory && typeof loadDirectory === 'function') loadDirectory(); return '<p class="context" role="status">Loading…</p>'; }
  const first = name.split(' ')[0];
  const back = `<a class="intro-back" href="#people/${encodeURIComponent(introPersonId())}">← ${esc(first)}'s context</a>`;
  if (introBusy === 'prepare' || introBusy === 'start') {
    return `<div class="voice">${back}<div class="voice-stage">${introOrb('thinking')}<p class="voice-caption">Reading up on ${esc(first)}…</p><p class="voice-sub">Teams, ownership and recent commits, so the call skips the basics.</p>${introData.dossier ? introKnownStrip(introData.dossier) : ''}</div></div>`;
  }
  if (!row || ['saved', 'cancelled', 'ready'].includes(row.status)) {
    const saved = row?.status === 'saved' || introData.context.length;
    return `<div class="voice">${back}<div class="voice-stage">${introOrb('idle')}<h2>${saved ? `Raven has talked with ${esc(first)}` : `Talk with Raven, ${esc(first)}`}</h2><p class="voice-sub">${saved ? 'What they said is on their page under People. Raven keeps learning from every question after this.' : 'A few minutes, out loud. Raven already knows your team and your code, so it only asks what isn\'t written down.'}</p><div class="voice-controls"><button class="button primary voice-call" data-intro="call" ${introBusy ? 'disabled' : ''}>${saved ? 'Talk again' : 'Start the call'}</button></div>${introData.dossier && !saved ? introKnownStrip(introData.dossier) : ''}${introNotes(introData.context)}</div></div>`;
  }
  if (row.status === 'review') return `<div class="voice">${back}<div class="voice-stage">${introOrb('thinking')}<p class="voice-caption">Keeping what ${esc(first)} said…</p></div></div>`;
  const turn = row.turns[row.turns.length - 1] || {};
  const thinking = introBusy === 'answer';
  const ending = introBusy === 'finish';
  const mode = ending || thinking ? 'thinking' : introSpeaking ? 'speaking' : introRecognition ? 'listening' : 'idle';
  const caption = ending ? `Thanks, ${esc(first)}. Writing up what I heard…` : thinking ? '' : `${turn.ack ? esc(turn.ack) + ' ' : ''}${esc(turn.question || row.opening)}`;
  const heard = introHeard || introDraft;
  const speech = introSpeech();
  const typing = introTyping || !speech;
  return `<div class="voice">${back}<div class="voice-stage live">
    <div class="voice-top"><span class="voice-dot ${mode}"></span><span>${mode === 'speaking' ? 'Raven is speaking' : mode === 'listening' ? 'Listening' : mode === 'thinking' ? 'Raven is thinking' : 'Your turn'}</span><span class="voice-clock" id="intro-clock" data-elapsed="${row.elapsed_minutes}" data-drawn="${Date.now()}" data-paused="${introBusy ? 1 : 0}" data-duration="${row.duration_minutes}">${introClock(row.elapsed_minutes)}</span></div>
    ${introOrb(mode)}
    <p class="voice-caption" aria-live="polite">${caption || '<span class="voice-ellipsis"><i></i><i></i><i></i></span>'}</p>
    <p class="voice-you ${heard ? '' : 'quiet'}">${heard ? `<b>${esc(first)}</b> ${esc(heard)}` : speech && !typing ? 'Just talk. Raven listens until you pause.' : ''}</p>
    ${typing ? `<form class="voice-type" id="intro-answer-form"><label class="sr-only" for="intro-answer">Your answer</label><textarea id="intro-answer" rows="2" maxlength="4000" placeholder="Type your answer" ${introBusy ? 'disabled' : ''}>${introBusy ? '' : esc(introDraft)}</textarea><button type="submit" class="button primary small" ${introBusy ? 'disabled' : ''}>Send</button></form>` : ''}
    <div class="voice-controls">
      ${speech ? `<button type="button" class="voice-btn" data-intro="mute" aria-pressed="${introMuted}" ${introBusy ? 'disabled' : ''}>${introMuted ? 'Unmute' : 'Mute'}</button>` : ''}
      ${speech ? `<button type="button" class="voice-btn" data-intro="type" aria-pressed="${introTyping}">${introTyping ? 'Talk instead' : 'Type instead'}</button>` : ''}
      ${!typing && heard ? `<button type="button" class="voice-btn" data-intro="send" ${introBusy ? 'disabled' : ''}>Done</button>` : ''}
      <button type="button" class="voice-btn" data-intro="skip" ${introBusy ? 'disabled' : ''}>Skip</button>
      <button type="button" class="voice-btn end" data-intro="finish" ${introBusy ? 'disabled' : ''}>End call</button>
    </div></div></div>`;
}

// Raven says each new question once, then the microphone opens.
function introVoice() {
  const row = introData?.interview;
  if (view !== 'intro' || !row || row.status !== 'active' || introBusy) return;
  const index = row.turns.length - 1;
  if (index === introSpokenFor) return;
  introSpokenFor = index;
  const turn = row.turns[index] || {};
  const text = `${turn.ack ? turn.ack + ' ' : ''}${turn.question || row.opening}`;
  stopIntroMic();
  introSpeaking = true; introKey = '';
  render();
  RavenVoice.speak(text).finally(() => {
    if (introSpokenFor !== index || !introSpeaking) return;
    introSpeaking = false; introKey = '';
    if (!introMuted && !introTyping && introSpeech() && !introRecognition) startIntroMic();
    render();
  });
}

function renderIntro() {
  const person = introPersonId();
  if (directory === null && typeof loadDirectory === 'function') loadDirectory();
  if (person ? !introData || introPerson !== person : !introList) {
    if (!introBusy) { introBusy = 'load'; Promise.resolve().then(() => { introBusy = ''; loadIntro(); }); }
  }
  // The app redraws every few seconds. Rebuild only when something we
  // show changed, so a half-typed answer and the scroll position survive.
  const key = JSON.stringify([person, introBusy, introError, person ? introData : introList, !!introRecognition, introSpeaking, introHeard, introTyping, introMuted]);
  if (key === introKey && $('#app .intro')) return;
  const typed = $('#intro-answer')?.value;
  if (typed !== undefined && !introBusy) introDraft = typed;
  introKey = key;
  const actions = $('#page-actions');
  if (actions && !person) actions.innerHTML = '';
  $('.page-head').hidden = !!person;
  document.body.classList.toggle('voice-view', !!person);
  $('#app').innerHTML = (introError ? `<p class="intro-error" role="alert">${esc(introError)}</p>` : '') + (person ? introPersonView() : introListView());
  const box = $('#intro-answer');
  if (box && !introBusy) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
  introTick();
  introVoice();
  const row = introData?.interview;
  // A chat an older Raven left waiting for review keeps every note now.
  if (person && row?.status === 'review' && !introBusy) {
    introCall('save', `/api/intro/${encodeURIComponent(row.id)}/save`, {expected_version: row.version, notes: (row.summary.notes || []).map(n => ({id: n.id}))});
  }
}

function introTick() {
  clearInterval(introTimer);
  const clock = $('#intro-clock');
  if (!clock) return;
  // The server's elapsed time leaves out Raven's thinking, so the clock
  // runs from that figure and stands still while Raven is thinking.
  const elapsed = Number(clock.dataset.elapsed), drawn = Number(clock.dataset.drawn);
  const paused = clock.dataset.paused === '1';
  const update = () => {
    const el = $('#intro-clock');
    if (!el) { clearInterval(introTimer); return; }
    el.textContent = introClock(elapsed + (paused ? 0 : (Date.now() - drawn) / 60000));
  };
  update();
  introTimer = setInterval(update, 1000);
}

async function introCall(action, path, body) {
  introBusy = action; introError = ''; render();
  try {
    const result = await api(path, body);
    if (result && result.status) introData = {...(introData || {}), interview: result};
    if (['save', 'cancel'].includes(action) || result?.status === 'saved') introData = await api('/api/intro/people/' + encodeURIComponent(introPersonId()));
    return result;
  } catch (error) {
    introError = error.message;
  } finally {
    introBusy = ''; render();
  }
}

function stopIntroMic() {
  if (!introRecognition) return;
  introRecognition = null;
  RavenVoice.stopListening();
}

// Raven listens until the person stops: a short pause after a finished
// sentence ends the turn (see voice.js), so nobody presses anything.
function startIntroMic() {
  const you = text => {
    introHeard = text;
    const el = $('.voice-you');
    if (el) { el.classList.remove('quiet'); el.innerHTML = `<b>${esc((introData?.interview?.dossier.person.first_name) || 'You')}</b> ${esc(text)}`; }
  };
  introRecognition = true;
  RavenVoice.listen({
    onheard: you,
    onturn: (text, method) => { introRecognition = null; introDraft = text; you(text); introSend(method); },
    onerror: error => { introRecognition = null; introError = error.message; introTyping = true; introKey = ''; render(); },
  });
}

function introSend(method) {
  const row = introData?.interview;
  const box = $('#intro-answer');
  const text = (box && introTyping ? box.value : introDraft).trim();
  if (!text || !row || introBusy) return;
  stopIntroMic();
  introDraft = text; introHeard = '';
  introCall('answer', `/api/intro/${encodeURIComponent(row.id)}/answer`, {expected_version: row.version, answer: text, capture_method: method})
    .then(result => { if (result) introDraft = ''; introKey = ''; render(); });
}

async function introPlaceCall() {
  const person = introPersonId();
  introSpokenFor = -1; introDraft = ''; introHeard = '';
  const ready = await introCall('prepare', `/api/intro/people/${encodeURIComponent(person)}/prepare`, {});
  if (ready && ready.status === 'ready') await introCall('start', `/api/intro/${encodeURIComponent(ready.id)}/start`, {expected_version: ready.version});
}

document.addEventListener('click', async event => {
  const button = event.target.closest('[data-intro]');
  if (!button || view !== 'intro') return;
  event.preventDefault();
  const row = introData?.interview, base = row ? `/api/intro/${encodeURIComponent(row.id)}` : '';
  const action = button.dataset.intro;
  if (action === 'call') { RavenVoice.unlock(); return introPlaceCall(); }
  if (action === 'mute') { introMuted = !introMuted; if (introMuted) stopIntroMic(); else if (!introSpeaking) startIntroMic(); introKey = ''; return render(); }
  if (action === 'type') { introTyping = !introTyping; if (introTyping) stopIntroMic(); else if (!introMuted) startIntroMic(); introKey = ''; return render(); }
  if (action === 'send') return introRecognition ? RavenVoice.finishTurn() : introSend('typed');
  if (action === 'skip') { stopIntroMic(); introDraft = ''; introHeard = ''; return introCall('answer', base + '/skip', {expected_version: row.version}); }
  if (action === 'finish') {
    stopIntroMic();
    RavenVoice.stopSpeaking();
    introSpeaking = false;
    const done = await introCall('finish', base + '/finish', {expected_version: row.version});
    if (done) { introData = await api('/api/intro/people/' + encodeURIComponent(introPersonId())).catch(() => introData); notify(`Saved what ${done.dossier.person.first_name} said.`); introKey = ''; render(); }
  }
});

document.addEventListener('submit', event => {
  if (event.target.id !== 'intro-answer-form') return;
  event.preventDefault();
  introSend('typed');
});

document.addEventListener('keydown', event => {
  if (event.target.id === 'intro-answer' && event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    event.target.form.requestSubmit();
  }
});
document.addEventListener('input', event => { if (event.target.id === 'intro-answer') introDraft = event.target.value; });
window.addEventListener('hashchange', () => {
  introKey = ''; introData = null; introList = null; introSpokenFor = -1; introSpeaking = false; introHeard = '';
  stopIntroMic();
  RavenVoice.stopSpeaking();
  document.body.classList.remove('voice-view');
});
