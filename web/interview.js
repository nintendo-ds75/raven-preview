'use strict';

// A short voice call about one decision. Speech recognition is a browser
// capability, not an identity credential or a Raven model call. Only the
// readback the person confirms reaches the agent.
let interviewSession = null, interviewGeneration = 0;
const interviewDialog = document.createElement('dialog');
interviewDialog.id = 'interview-modal';
interviewDialog.setAttribute('aria-labelledby', 'interview-title');
document.body.append(interviewDialog);

const interviewUser = () => window.ravenInterviewBridge ? window.ravenInterviewBridge.user() : state.me;
const interviewRequest = (path, body) => window.ravenInterviewBridge ? window.ravenInterviewBridge.request(path, body) : api(path, body);
const interviewNotify = text => window.ravenInterviewBridge ? window.ravenInterviewBridge.notify(text) : notify(text);
const interviewRefresh = () => window.ravenInterviewBridge ? window.ravenInterviewBridge.refresh() : refresh();

function interviewScopeFields(spec) {
  const pairs = obj => Object.entries(obj || {}).map(([k,v]) => `${k}=${v}`).join(', ');
  return `<details class="history"><summary>Where this answer applies (optional)</summary><label for="interview-requires">Required facts</label><input id="interview-requires" name="applies_when" maxlength="2000" placeholder="client=new, region=us" value="${esc(pairs(spec.requires))}"><label for="interview-excludes">Exceptions</label><input id="interview-excludes" name="excludes_when" maxlength="2000" value="${esc(pairs(spec.excludes))}"><label for="interview-paths">Repository paths</label><input id="interview-paths" name="applies_to_paths" maxlength="2000" value="${esc((spec.paths || []).join(', '))}"><label for="interview-until">Valid until</label><input id="interview-until" name="valid_until" type="date" value="${esc((spec.valid_until || '').slice(0,10))}"><p class="context">Other tasks must satisfy these facts and paths. This does not create an automatic rule.</p></details>`;
}
function takeInterviewScope(data) {
  const facts = text => {
    const out = {};
    for (const part of String(text || '').split(',').map(x => x.trim()).filter(Boolean)) {
      const at = part.indexOf('=');
      if (at <= 0 || !part.slice(at + 1).trim()) throw new Error('Applicability facts must be comma-separated key=value pairs.');
      out[part.slice(0, at).trim()] = part.slice(at + 1).trim();
    }
    return out;
  };
  data.applicability = {requires: facts(data.applies_when), excludes: facts(data.excludes_when),
    paths: String(data.applies_to_paths || '').split(',').map(x => x.trim()).filter(Boolean), valid_until: data.valid_until || ''};
  delete data.applies_when; delete data.excludes_when; delete data.applies_to_paths; delete data.valid_until;
}

function interviewError(error) {
  const box = interviewDialog.querySelector('[data-interview-error]');
  if (box) { box.textContent = error.message || String(error); box.hidden = false; }
}
function interviewStatus(text) {
  const box = interviewDialog.querySelector('[data-interview-status]');
  if (box) box.textContent = text;
}
function stopInterviewSpeech() {
  RavenVoice.stopSpeaking();
  const session = interviewSession;
  if (session) session.speaking = false;
  if (!session?.recognition) return;
  session.recognition = null; // Late events from an aborted capture are ignored.
  RavenVoice.stopListening();
}
function interviewPayload(session) {
  const form = interviewDialog.querySelector('#interview-form');
  if (!form) return {};
  const data = Object.fromEntries(new FormData(form));
  takeInterviewScope(data);
  return {...data, turns: session.turns, capture_method: session.captureMethod};
}
async function interviewWrite(action = 'draft', extra = {}) {
  const session = interviewSession;
  if (!session) return;
  const payload = {...interviewPayload(session), ...extra};
  // A close, speech error and manual save may race. Serialize writes against
  // the last acknowledged version instead of overwriting a newer draft.
  const work = session.writes.catch(() => {}).then(async () => {
    session.row = await interviewRequest(`${session.base}/${action}`, {...payload, expected_version: session.row.version});
    if (interviewSession === session) interviewStatus(action === 'draft' ? 'Draft saved. No decision recorded.' : 'Interview ' + session.row.status + '.');
    return session.row;
  });
  session.writes = work;
  return work;
}
// The call, drawn the way the voice intro is: one orb, Raven's question as
// a caption, the person's words as they say them, and a readback at the
// end that they confirm in one tap. The form underneath is the same draft.
const interviewCanListen = () => RavenVoice.canListen();
const interviewCanSpeak = () => RavenVoice.canSpeak();
function interviewOrb(mode) {
  return RavenVoice.orb((mode === 'speaking' && !RavenVoice.usesServer().tts ? 'speaking pulse' : mode) + ' small');
}
function interviewMode(session) {
  return session.thinking ? 'thinking' : session.speaking ? 'speaking' : session.recognition ? 'listening' : 'idle';
}
function interviewBody(session, reviewing = false) {
  const row = session.row, scope = row.scope;
  // Bind the button to exactly what this render showed, never a later save.
  session.review = reviewing ? {id: row.id, version: row.version, revision: row.decision_revision} : null;
  session.reviewing = reviewing;
  const first = String(interviewUser()?.name || '').split(' ')[0];
  const where = `${esc(scope.repo)} · ${esc(scope.paths.join(', ') || scope.path)}`;
  const scopeBox = `<details class="voice-scope"><summary>What this is about</summary><p>${esc(scope.context)}</p><p class="context">${where} · Task ${esc(scope.task_id)}</p><pre>${esc(scope.decision_scope_text || "This older draft has no bound structured scope. Start a new call before confirming.")}</pre></details>`;
  const mode = interviewMode(session);
  let body;
  if (reviewing) {
    const spec = row.applicability;
    body = `<div class="voice-stage call">
      <div class="voice-top"><span class="voice-dot ${mode}"></span><span>${mode === 'speaking' ? 'Raven is reading it back' : 'Is this right?'}</span></div>
      <p class="voice-topic">${esc(scope.question)}</p>
      ${interviewOrb(mode)}
      <p class="voice-sub">Here's what I'll tell the agent${first ? `, ${esc(first)}` : ''}:</p>
      <p class="voice-caption">${esc(row.answer)}</p>
      ${row.rationale ? `<p class="voice-you"><b>Why</b> ${esc(row.rationale)}</p>` : ''}
      ${scope.decision_scope?.reusable ? '<p class="context">Confirming this interview answer retires the existing standing rule. Future automatic reuse needs a fresh explicit make-rule action.</p>' : ''}
      <div class="voice-controls"><button class="voice-btn" data-interview-action="edit">Change it</button><button class="voice-btn" data-interview-action="readback" ${interviewCanSpeak() ? '' : 'disabled'}>Read it again</button><button class="button primary voice-call-btn" data-interview-action="confirm" data-interview-id="${esc(row.id)}" data-interview-version="${row.version}">That's right</button></div>
      <p class="context voice-fine">Recorded as ${esc(interviewUser().name)} using ${window.ravenInterviewBridge ? 'your personal task link' : 'your signed-in identity'}. Speech recognition does not verify who spoke. ${Object.keys(spec).length ? 'Other tasks must satisfy the boundaries below.' : 'It applies to this decision.'} Anyone else who must approve is still asked.</p>
      <details class="voice-scope"><summary>Transcript and boundaries</summary><p>${esc(row.transcript || 'No transcript supplied; answer entered directly.')}</p><pre>${esc(JSON.stringify(spec, null, 2))}</pre></details>
      ${scopeBox}</div>`;
  } else {
    const index = session.turns.length;
    const asking = index < row.prompts.length ? row.prompts[index] : 'That covers it. Anything else before I read it back?';
    const typing = session.typing || !interviewCanListen();
    const heard = session.heard || row.pending_response || '';
    const guidance = row.guidance || {};
    const note = guidance.reason ? (guidance.reason === 'model_unavailable' ? 'No model interviewer is configured, so Raven asks its standard questions.' : guidance.reason === 'model_slow' ? 'The model took too long, so Raven asked its standard question instead of keeping you waiting.' : 'The model did not return a usable follow-up, so Raven asks its standard questions.') : '';
    body = `<div class="voice-stage call">
      <div class="voice-top"><span class="voice-dot ${mode}"></span><span>${mode === 'speaking' ? 'Raven is speaking' : mode === 'listening' ? 'Listening' : mode === 'thinking' ? 'Raven is thinking' : 'Your turn'}</span><span class="voice-clock">${index < row.prompts.length ? `${index + 1} of ${row.prompts.length}` : 'Wrapping up'}</span></div>
      <p class="voice-topic">${esc(scope.question)}</p>
      ${interviewOrb(mode)}
      <p class="voice-caption" aria-live="polite">${session.thinking ? '<span class="voice-ellipsis"><i></i><i></i><i></i></span>' : esc(asking)}</p>
      <p class="voice-you ${heard ? '' : 'quiet'}" data-interview-heard>${heard ? `<b>${esc(first || 'You')}</b> ${esc(heard)}` : typing ? '' : 'Just talk. Raven listens until you pause.'}</p>
      <div class="voice-type" ${typing && index < row.prompts.length ? '' : 'hidden'}><label class="sr-only" for="interview-response">Your answer</label><textarea id="interview-response" name="pending_response" form="interview-form" maxlength="2000" rows="2" placeholder="Type your answer">${esc(row.pending_response)}</textarea><button type="button" class="button primary small" data-interview-action="next">Send</button></div>
      <div class="voice-controls">
        ${interviewCanListen() ? `<button type="button" class="voice-btn" data-interview-action="${session.recognition ? 'stop' : 'record'}">${session.recognition ? 'Mute' : 'Start microphone'}</button><button type="button" class="voice-btn" data-interview-action="type" aria-pressed="${!!session.typing}">${session.typing ? 'Talk instead' : 'Type instead'}</button>` : ''}
        ${!typing && heard && index < row.prompts.length ? '<button type="button" class="voice-btn" data-interview-action="next">Done</button>' : ''}
        <button type="button" class="voice-btn" data-interview-action="wrap" ${index ? '' : 'disabled'}>That's everything</button>
        <button type="button" class="voice-btn end" data-interview-action="close">End call</button>
      </div>
      ${note ? `<p class="context voice-fine">${note}</p>` : ''}
      <p class="context voice-fine">${interviewCanListen() ? (RavenVoice.usesServer().stt ? 'Your voice is transcribed by this Raven server and the audio is not kept. ' : 'Browser dictation may send audio to your browser provider. ') : 'Speech recognition is unavailable here, so type your answers. '}Raven stores only the text. Adaptive follow-ups send the task context and your answers to the configured model provider.</p>
      ${row.failure && session.failedNow ? `<p class="task-warning">Last capture stopped: ${esc(row.failure)}. Your saved text is preserved; type or try again.</p>` : ''}
      ${scopeBox}
      <details class="voice-scope"><summary>Write the answer yourself</summary><form id="interview-form"><label for="interview-transcript">Transcript</label><textarea id="interview-transcript" name="transcript" maxlength="12000">${esc(row.transcript)}</textarea><label for="interview-answer">Answer the agent should follow</label><textarea id="interview-answer" name="answer" maxlength="12000" required>${esc(row.answer)}</textarea><label for="interview-rationale">Reasoning, constraints and exceptions</label><textarea id="interview-rationale" name="rationale" maxlength="12000" required>${esc(row.rationale)}</textarea>${interviewScopeFields(row.applicability)}<div class="modal-actions"><button type="button" class="button" data-interview-action="save">Save draft</button><button type="submit" class="button primary">Read it back to me</button></div></form><button class="button text small" data-interview-action="cancel">Discard this call</button></details>
    </div>`;
  }
  interviewDialog.classList?.add?.('voice-dialog');
  interviewDialog.innerHTML = `<div class="modal-head"><div><h2 id="interview-title">${reviewing ? 'Review your decision' : 'Call with Raven'}</h2></div><button class="icon-button" data-interview-action="close" aria-label="Save draft and close the call"><span aria-hidden="true">×</span></button></div><div class="modal-body">${body}<p class="context" data-interview-status role="status">Draft saved. No decision recorded.</p><p class="error" data-interview-error role="alert" hidden></p></div>`;
  interviewVoice(session);
}
// Raven says each new question (or the readback) once, then listens.
function interviewVoice(session) {
  if (interviewSession !== session || session.thinking) return;
  const key = session.reviewing ? 'review:' + session.row.version : 'ask:' + session.turns.length;
  if (session.spokenFor === key) return;
  session.spokenFor = key;
  const row = session.row;
  const text = session.reviewing ? `Here's what I'll tell the agent. ${row.answer}` : session.turns.length < row.prompts.length ? row.prompts[session.turns.length] : '';
  const listen = () => {
    if (interviewSession !== session || session.spokenFor !== key) return;
    session.speaking = false;
    if (!session.reviewing && !session.typing && !session.muted && interviewCanListen() && !session.recognition) {
      try { startInterviewSpeech(); } catch {}
    }
    interviewBody(session, session.reviewing);
  };
  if (!text) return;
  session.speaking = true;
  RavenVoice.speak(text).finally(listen);
}
// End of the call: the readback is the model's proposal when there is one,
// otherwise the person's own first answer with the rest as the reasoning.
// "No, that's everything" said out loud ends the call the way the button does.
const interviewDone = text => String(text || '').trim().length < 80 && /^(?:no[,.!]?\s*|nope[,.!]?\s*|i think\s+)?(?:that'?s (?:it|all|everything)|nothing (?:else|more)|that covers it|we'?re (?:done|good)|i'?m (?:done|good)|all good)(?: for now| from me)?[.!]?$/i.test(String(text || '').trim());
async function interviewWrap(session) {
  const box = interviewDialog.querySelector('#interview-response');
  if (box && box.value?.trim() && session.turns.length < session.row.prompts.length) await interviewNext(session, false);
  if (interviewSession !== session) return;
  const g = session.row.guidance || {};
  const said = session.turns.map(t => t.response).filter(t => t && !interviewDone(t));
  const answer = g.proposed_answer || said[0] || session.row.answer;
  // Confirming needs a reason. When nobody asked "why", say so plainly
  // rather than leave it empty or pad it with "that's everything".
  const rationale = (g.proposed_answer && g.proposed_rationale) || said.slice(1).join(' ') || session.row.rationale
    || 'Stated on a call with Raven; no separate reason was given.';
  const saved = await interviewWrite('draft', {answer, rationale});
  if (interviewSession !== session || !saved) return;
  interviewBody(session, true);
}
async function interviewNext(session, advance = true) {
  stopInterviewSpeech();
  const response = (interviewDialog.querySelector('#interview-response')?.value || '').trim();
  if (!response) throw new Error('Say or type an answer first, or tap That\'s everything.');
  const index = session.turns.length;
  const turns = [...session.turns, {prompt_id: index, response, capture_method: session.captureMethod}];
  const transcript = [interviewDialog.querySelector('#interview-transcript')?.value, `Q: ${session.row.prompts[index]}\nA: ${response}`].filter(Boolean).join('\n\n');
  const saved = await interviewWrite('draft', {turns, transcript, pending_response: ''});
  session.turns = saved.turns;
  session.heard = '';
  if (interviewSession !== session || !advance) return;
  if (interviewDone(response)) return interviewWrap(session);
  session.thinking = true;
  interviewBody(session);
  let advanced = false;
  try { advanced = await advanceInterview(session); } finally { session.thinking = false; }
  if (interviewSession !== session) return;
  const g = session.row.guidance || {};
  // The model has a readback and nothing left to ask: read it back now
  // instead of running through the remaining standard questions.
  const settled = g.mode === 'model-assisted' && !String(g.question || '').trim() && String(g.proposed_answer || '').trim();
  if (settled || session.turns.length >= session.row.prompts.length) return interviewWrap(session);
  if (advanced !== false) interviewBody(session);
}
async function openInterview(task, decision) {
  const generation = ++interviewGeneration;
  if (!window.ravenInterviewBridge && (!state.auth?.enabled || !state.me?.id || !['session', 'token'].includes(state.me.kind))) throw new Error('Sign in as yourself to record an attributed interview.');
  const base = `/api/tasks/${encodeURIComponent(task)}/interviews`;
  const existing = await interviewRequest(base);
  if (generation !== interviewGeneration) return;
  let row = existing.interviews.find(r => r.decision_id === decision && ['draft', 'failed'].includes(r.status));
  if (!row) row = await interviewRequest(base, {decision_id: decision, client_key: (typeof ravenUuid === 'function' ? ravenUuid() : crypto.randomUUID())});
  if (generation !== interviewGeneration) return;
  if ($('#modal')?.open) closeModal();
  interviewSession = {row, base: `${base}/${row.id}`, writes: Promise.resolve(), captureMethod: row.capture_method, turns: row.turns, recognition: null, editVersion: 0,
    typing: false, muted: false, speaking: false, thinking: false, heard: '', spokenFor: ''};
  interviewBody(interviewSession);
  interviewDialog.showModal();
}
async function advanceInterview(session) {
  interviewStatus('Preparing a follow-up and draft readback. No answer has been signed.');
  const version = session.row.version, edits = session.editVersion;
  const row = await interviewRequest(`${session.base}/advance`, {expected_version: version});
  if (interviewSession !== session || session.row.version !== version) return false;
  session.row = row;
  if (session.editVersion !== edits) {
    interviewStatus('A follow-up is ready for the saved draft. Your newer edits are preserved; save them before requesting a new readback.');
    return false;
  }
  return true;
}
async function closeInterview() {
  const session = interviewSession;
  if (!session) return;
  stopInterviewSpeech();
  await interviewWrite();
  if (interviewSession !== session) return;
  interviewSession = null;
  ++interviewGeneration;
  interviewDialog.close();
}
// The microphone stays open until the person stops talking; a short
// pause after a finished sentence sends the answer (see voice.js).
function startInterviewSpeech() {
  const session = interviewSession;
  if (!session || session.recognition) return;
  if (!interviewCanListen()) throw new Error('Speech recognition is unavailable. You can type instead.');
  RavenVoice.stopSpeaking();
  session.speaking = false;
  const marker = {};
  session.recognition = marker;
  const show = text => {
    if (interviewSession !== session || session.recognition !== marker) return;
    const input = interviewDialog.querySelector('#interview-response');
    if (input) input.value = text.slice(0, input.maxLength > 0 ? input.maxLength : 2000);
    session.heard = text;
    session.editVersion++;
    const heard = interviewDialog.querySelector('[data-interview-heard]');
    if (heard) { heard.classList?.remove?.('quiet'); heard.innerHTML = `<b>${esc(String(interviewUser()?.name || 'You').split(' ')[0])}</b> ${esc(text)}`; }
  };
  RavenVoice.listen({
    onheard: show,
    onturn: (text, method) => {
      if (interviewSession !== session || session.recognition !== marker) return;
      show(text);
      session.recognition = null;
      session.captureMethod = method;
      interviewNext(session).catch(interviewError);
    },
    onerror: error => {
      if (interviewSession !== session || session.recognition !== marker) return;
      session.recognition = null;
      session.typing = true;
      session.failedNow = true;
      interviewWrite('failed', {failure: 'recognition-failed'}).then(() => interviewBody(session)).catch(interviewError);
      interviewError(error);
    },
  });
  interviewStatus('Listening.');
}
document.addEventListener('click', async event => {
  const start = event.target.closest('[data-action="interview-start"]');
  if (!start || start.disabled) return;
  start.disabled = true;
  RavenVoice.unlock();
  try { await openInterview(start.dataset.task, start.dataset.id); }
  catch (error) { interviewNotify(error.message); }
  finally { start.disabled = false; }
});
interviewDialog.addEventListener('click', async event => {
  const button = event.target.closest('[data-interview-action]');
  if (!button || button.disabled) return;
  const action = button.dataset.interviewAction;
  button.disabled = true;
  try {
    if (action === 'record') { interviewSession.muted = false; startInterviewSpeech(); interviewBody(interviewSession); }
    else if (action === 'stop') { interviewSession.muted = true; stopInterviewSpeech(); await interviewWrite(); interviewBody(interviewSession); }
    else if (action === 'type') {
      const session = interviewSession;
      session.typing = !session.typing;
      if (session.typing) stopInterviewSpeech(); else if (!session.muted) startInterviewSpeech();
      interviewBody(session);
    }
    else if (action === 'wrap') { stopInterviewSpeech(); await interviewWrap(interviewSession); }
    else if (action === 'speak' || action === 'readback') {
      stopInterviewSpeech();
      const row = interviewSession.row;
      const text = action === 'speak' ? row.prompts[interviewSession.turns.length] : `Here's what I'll tell the agent. ${row.answer}. ${row.scope.decision_scope_text || "No bound structured scope is available."} Your decision: ${row.answer}. Your reasoning and constraints: ${row.rationale}. Reuse boundaries: ${JSON.stringify(row.applicability)}. ${row.scope.decision_scope?.reusable ? "Confirming this answer retires the existing standing rule. Future automatic reuse needs an explicit make-rule action." : ""} This has not been confirmed yet.`;
      RavenVoice.speak(text);
    }
    else if (action === 'next') await interviewNext(interviewSession);
    else if (action === 'adapt') {
      stopInterviewSpeech();
      const session = interviewSession;
      await interviewWrite();
      if (await advanceInterview(session)) interviewBody(session);
    }
    else if (action === 'use-readback') {
      const guidance = interviewSession.row.guidance;
      interviewDialog.querySelector('#interview-answer').value = guidance.proposed_answer;
      interviewDialog.querySelector('#interview-rationale').value = guidance.proposed_rationale;
      interviewSession.editVersion++;
      interviewStatus('Proposed readback copied. Edit it and preserve every caveat before review.');
    }
    else if (action === 'save') await interviewWrite();
    else if (action === 'close') await closeInterview();
    else if (action === 'edit') { stopInterviewSpeech(); interviewSession.typing = true; interviewBody(interviewSession); const box = interviewDialog.querySelector('.voice-scope:last-of-type'); if (box) box.open = true; }
    else if (action === 'cancel') {
      stopInterviewSpeech(); await interviewWrite('cancel');
      interviewSession = null; ++interviewGeneration; interviewDialog.close();
      interviewNotify('Call discarded. No answer was recorded.');
    } else if (action === 'confirm') {
      stopInterviewSpeech();
      const session = interviewSession, review = session?.review;
      if (!review || button.dataset.interviewId !== review.id || button.dataset.interviewVersion !== String(review.version)) return;
      await session.writes;
      if (interviewSession !== session || session.review !== review || session.row.id !== review.id ||
          session.row.version !== review.version || session.row.decision_revision !== review.revision) {
        if (interviewSession === session) interviewError(new Error('This interview changed. Review the saved answer again before confirming.'));
        return;
      }
      const result = await interviewRequest(`${session.base}/confirm`, {confirmed: true, expected_version: review.version,
        expected_updated_at: review.revision});
      if (interviewSession !== session) return;
      session.row = result;
      interviewSession = null; ++interviewGeneration; interviewDialog.close();
      interviewNotify('Got it. Raven passed your answer to the agent.');
      await interviewRefresh();
    }
  } catch (error) { interviewError(error); }
  finally { if (button.isConnected && !(action === 'record' && interviewSession?.recognition)) button.disabled = false; }
});
interviewDialog.addEventListener('submit', async event => {
  if (event.target.id !== 'interview-form') return;
  event.preventDefault();
  const button = event.target.querySelector('[type="submit"]');
  if (button.disabled || !event.target.reportValidity()) return;
  button.disabled = true;
  const session = interviewSession;
  try {
    stopInterviewSpeech(); await interviewWrite();
    if (interviewSession === session) interviewBody(session, true);
  } catch (error) { interviewError(error); button.disabled = false; }
});
interviewDialog.addEventListener('input', () => {
  if (interviewSession) interviewSession.editVersion++;
  interviewStatus('Unsaved changes. Save the draft or close to save.');
});
interviewDialog.addEventListener('cancel', event => { event.preventDefault(); closeInterview().catch(interviewError); });
interviewDialog.addEventListener('close', () => { stopInterviewSpeech(); });
window.addEventListener('hashchange', () => {
  // Invalidate an in-flight launch; never reopen on top of newer navigation.
  ++interviewGeneration;
  if (interviewSession) closeInterview().catch(interviewError);
});
window.addEventListener('pagehide', stopInterviewSpeech);
