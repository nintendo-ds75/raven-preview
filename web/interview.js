'use strict';

// Speech recognition is a browser capability, not an identity credential or
// a Raven model call. Only an explicitly reviewed answer reaches the agent.
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
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  const session = interviewSession;
  if (!session?.recognition) return;
  const recognition = session.recognition;
  session.recognition = null; // Late events from an aborted capture are ignored.
  recognition.onresult = recognition.onerror = recognition.onend = null;
  recognition.abort();
  const start = interviewDialog.querySelector('[data-interview-action="record"]');
  const stop = interviewDialog.querySelector('[data-interview-action="stop"]');
  if (start) start.disabled = false;
  if (stop) stop.disabled = true;
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
function interviewBody(session, reviewing = false) {
  const row = session.row, scope = row.scope;
  const title = reviewing ? 'Review your decision' : 'Talk through this decision';
  const context = `<div class="context-box"><strong>${esc(scope.question)}</strong><p>${esc(scope.context)}</p><p class="context">${esc(scope.repo)} · ${esc(scope.paths.join(', ') || scope.path)} · Task ${esc(scope.task_id)}</p></div>`;
  const canSpeak = !!window.speechSynthesis && !!window.SpeechSynthesisUtterance;
  let body;
  if (reviewing) {
    const spec = row.applicability;
    body = `${context}<button class="button small" data-interview-action="readback" ${canSpeak ? '' : 'disabled'}>Read decision aloud</button><h3>The exact answer you will sign</h3><p class="task-answer">${esc(row.answer)}</p><h3>Why</h3><p>${esc(row.rationale)}</p><details class="history"><summary>Reviewed transcript</summary><p>${esc(row.transcript || 'No transcript supplied; answer entered directly.')}</p></details><h3>Reuse boundaries</h3><pre>${esc(JSON.stringify(spec, null, 2))}</pre><p class="context">${Object.keys(spec).length ? 'Other tasks must satisfy these boundaries; a fresh signature is still required unless an enabled standing rule applies.' : 'No extra reuse boundaries declared. This signs this decision; it does not create a standing rule.'} Other required approvers must still sign.</p><p class="context">Recorded as ${esc(interviewUser().name)} using ${window.ravenInterviewBridge ? 'your personal task link' : 'your signed-in identity'}. Speech recognition does not verify who spoke.</p><div class="modal-actions"><button class="button" data-interview-action="edit">Back to edit</button><button class="button primary" data-interview-action="confirm">Confirm and sign as ${esc(interviewUser().name)}</button></div>`;
  } else {
    const available = !!(window.SpeechRecognition || window.webkitSpeechRecognition) && window.isSecureContext;
    const index = session.turns.length;
    const guided = index < row.prompts.length ? `<section class="context-box"><span class="label">Guided question ${index + 1} of ${row.prompts.length}</span><p>${esc(row.prompts[index])}</p><button type="button" class="button small" data-interview-action="speak" ${canSpeak ? '' : 'disabled'}>Read question aloud</button><label for="interview-response">Your response to this question</label><textarea id="interview-response" name="pending_response" form="interview-form" maxlength="2000">${esc(row.pending_response)}</textarea><button type="button" class="button small" data-interview-action="next">Save response and continue</button></section>` : '<p class="context">Your interview responses are saved. Review the transcript and write your exact decision below.</p>';
    const guidance = row.guidance || {};
    const proposed = guidance.mode === 'model-assisted' ? `<section class="context-box prediction"><span class="label">Model-proposed readback · unapproved</span><p>${esc(guidance.proposed_answer || 'More clarification is needed before proposing an answer.')}</p><p>${esc(guidance.proposed_rationale)}</p>${guidance.caveats.length ? `<h3>Caveats to preserve</h3><ul>${guidance.caveats.map(c => `<li>${esc(c.text)}<br><small>From your response: ${esc(c.quote)}</small></li>`).join('')}</ul>` : ''}${guidance.proposed_answer ? '<button type="button" class="button small" data-interview-action="use-readback">Copy readback into editable answer</button>' : ''}</section>` : guidance.reason ? `<p class="context">${guidance.reason === 'model_unavailable' ? 'No model interviewer is configured. Continuing with guided prompts.' : 'The model did not return a usable follow-up. Continuing with guided prompts.'}</p>` : '';
    const history = session.turns.length ? `<details class="history"><summary>Interview responses (${session.turns.length})</summary>${session.turns.map(t => `<p><strong>${esc(t.prompt)}</strong></p><p>${esc(t.response)}</p>`).join('')}</details>` : '';
    body = `${context}<p class="context">${row.interviewer === 'model-assisted' ? 'Model-assisted follow-ups are grounded in your saved responses. Suggestions remain unapproved.' : 'Guided questions are available without a model. After a response, Raven uses an adaptive interviewer when a model backend is configured.'} You can answer the prompts or write your decision directly. Adaptive follow-ups send the saved task context and responses to the configured model provider.</p>${guided}${history}${proposed}<p class="context">Optional browser dictation may send audio to your browser provider. Raven stores only the text you save. Microphone access starts only when you choose Start microphone. You can type instead.</p><div class="modal-actions"><button class="button" data-interview-action="record" ${available ? '' : 'disabled'}>Start microphone</button><button class="button" data-interview-action="stop" disabled>Stop microphone</button></div>${available ? '' : '<p class="context">Browser dictation is unavailable here. Use HTTPS and a supported browser, or type your interview below.</p>'}${row.failure ? `<p class="task-warning">Last capture stopped: ${esc(row.failure)}. Your saved text is preserved; type or try again.</p>` : ''}<form id="interview-form"><label for="interview-transcript">Interview transcript · review for recognition errors</label><textarea id="interview-transcript" name="transcript" maxlength="12000">${esc(row.transcript)}</textarea><p class="context">The transcript is an unapproved draft. Write or edit the exact decision below.</p><label for="interview-answer">Answer the agent should follow</label><textarea id="interview-answer" name="answer" maxlength="12000" required>${esc(row.answer)}</textarea><label for="interview-rationale">Reasoning, constraints and exceptions</label><textarea id="interview-rationale" name="rationale" maxlength="12000" required>${esc(row.rationale)}</textarea>${interviewScopeFields(row.applicability)}<div class="modal-actions"><button type="button" class="button" data-interview-action="save">Save draft</button><button type="button" class="button" data-interview-action="adapt">Suggest follow-up and readback</button><button type="submit" class="button primary">Save and review</button></div></form><button class="button text small" data-interview-action="cancel">Discard this interview</button><p class="context">Discard closes the draft without recording an answer. Previously saved text stays in your private interview history.</p>`;
  }
  interviewDialog.innerHTML = `<div class="modal-head"><div><h2 id="interview-title">${title}</h2><p>Attributed human interview · no automatic approval</p></div><button class="icon-button" data-interview-action="close" aria-label="Save draft and close interview"><span aria-hidden="true">×</span></button></div><div class="modal-body">${body}<p data-interview-status role="status">Draft saved. No decision recorded.</p><p class="error" data-interview-error role="alert" hidden></p></div>`;
}
async function openInterview(task, decision) {
  const generation = ++interviewGeneration;
  if (!window.ravenInterviewBridge && (!state.auth?.enabled || !state.me?.id || !['session', 'token'].includes(state.me.kind))) throw new Error('Sign in as yourself to record an attributed interview.');
  const base = `/api/tasks/${encodeURIComponent(task)}/interviews`;
  const existing = await interviewRequest(base);
  if (generation !== interviewGeneration) return;
  let row = existing.interviews.find(r => r.decision_id === decision && ['draft', 'failed'].includes(r.status));
  if (!row) row = await interviewRequest(base, {decision_id: decision, client_key: crypto.randomUUID()});
  if (generation !== interviewGeneration) return;
  if ($('#modal')?.open) $('#modal').close();
  interviewSession = {row, base: `${base}/${row.id}`, writes: Promise.resolve(), captureMethod: row.capture_method, turns: row.turns, recognition: null, editVersion: 0};
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
function startInterviewSpeech() {
  const session = interviewSession;
  if (!session || session.recognition) return;
  const Speech = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Speech || !window.isSecureContext) throw new Error('Browser dictation is unavailable. You can type instead.');
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  const recognition = new Speech();
  recognition.lang = document.documentElement.lang || 'en-US';
  recognition.continuous = true;
  recognition.interimResults = false;
  session.recognition = recognition;
  recognition.onresult = event => {
    if (interviewSession !== session || session.recognition !== recognition) return;
    const input = interviewDialog.querySelector('#interview-response') || interviewDialog.querySelector('#interview-transcript');
    if (!input) return;
    const parts = [];
    for (let i = event.resultIndex; i < event.results.length; i++) if (event.results[i].isFinal) parts.push(event.results[i][0].transcript);
    if (!parts.length) return;
    const joined = [input.value, ...parts].filter(Boolean).join('\n');
    if (joined.length > input.maxLength) { stopInterviewSpeech(); interviewError(new Error('Transcript limit reached. Save this draft before continuing.')); return; }
    input.value = joined;
    session.editVersion++;
    session.captureMethod = 'browser-speech';
    interviewStatus('Speech transcribed. Save the draft and review recognition errors.');
    interviewWrite().catch(interviewError);
  };
  recognition.onerror = event => {
    if (interviewSession !== session || session.recognition !== recognition) return;
    stopInterviewSpeech();
    const allowed = ['not-allowed','service-not-allowed','audio-capture','network','no-speech','aborted','language-not-supported'];
    const failure = allowed.includes(event.error) ? event.error : 'recognition-failed';
    interviewError(new Error(`Microphone stopped: ${failure}. Your saved text is preserved. You can type instead.`));
    interviewWrite('failed', {failure}).catch(interviewError);
  };
  recognition.onend = () => {
    if (interviewSession !== session || session.recognition !== recognition) return;
    stopInterviewSpeech();
    interviewStatus('Microphone stopped. Review the transcript before recording a decision.');
  };
  try { recognition.start(); }
  catch (error) { stopInterviewSpeech(); throw error; }
  interviewDialog.querySelector('[data-interview-action="record"]').disabled = true;
  interviewDialog.querySelector('[data-interview-action="stop"]').disabled = false;
  interviewStatus('Listening. Stop the microphone before reviewing.');
}
document.addEventListener('click', async event => {
  const start = event.target.closest('[data-action="interview-start"]');
  if (!start || start.disabled) return;
  start.disabled = true;
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
    if (action === 'record') startInterviewSpeech();
    else if (action === 'stop') { stopInterviewSpeech(); await interviewWrite(); }
    else if (action === 'speak' || action === 'readback') {
      stopInterviewSpeech();
      const row = interviewSession.row;
      const text = action === 'speak' ? row.prompts[interviewSession.turns.length] : `Your decision: ${row.answer}. Your reasoning and constraints: ${row.rationale}. Reuse boundaries: ${JSON.stringify(row.applicability)}. This has not been confirmed yet.`;
      window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
    }
    else if (action === 'next') {
      stopInterviewSpeech();
      const session = interviewSession;
      const response = interviewDialog.querySelector('#interview-response').value.trim();
      if (!response) throw new Error('Answer this prompt first, or write your decision directly below.');
      const index = session.turns.length;
      const turns = [...session.turns, {prompt_id: index, response, capture_method: session.captureMethod}];
      const transcript = [interviewDialog.querySelector('#interview-transcript').value, `Q: ${session.row.prompts[index]}\nA: ${response}`].filter(Boolean).join('\n\n');
      const saved = await interviewWrite('draft', {turns, transcript, pending_response: ''});
      session.turns = saved.turns;
      if (interviewSession !== session) return;
      interviewBody(session);
      if (await advanceInterview(session)) interviewBody(session);
    }
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
    else if (action === 'edit') { stopInterviewSpeech(); interviewBody(interviewSession); }
    else if (action === 'cancel') {
      stopInterviewSpeech(); await interviewWrite('cancel');
      interviewSession = null; ++interviewGeneration; interviewDialog.close();
      interviewNotify('Interview discarded. No answer was recorded.');
    } else if (action === 'confirm') {
      stopInterviewSpeech();
      const session = interviewSession;
      await session.writes;
      const result = await interviewRequest(`${session.base}/confirm`, {confirmed: true, expected_version: session.row.version,
        expected_updated_at: session.row.decision_revision});
      session.row = result;
      interviewSession = null; ++interviewGeneration; interviewDialog.close();
      interviewNotify('Your answer and signature were recorded. Other required approvers may still need to sign.');
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
