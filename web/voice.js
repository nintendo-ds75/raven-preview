'use strict';

// Raven's voice in the browser, shared by the voice intro and the
// decision call. Three pieces:
//  - the orb: one soft circle whose size follows the real sound, Raven's
//    voice while it speaks and the microphone while it listens, and that
//    breathes slowly while Raven thinks;
//  - speak(): the server's voice (Kokoro, ElevenLabs or OpenAI, see
//    bridge/voice.py), sentence by sentence so the first words play while
//    the rest are made; the browser's speech when the server has none;
//  - listen(): the microphone with end-of-turn detection. A short pause
//    after a finished sentence ends the turn; a pause after "and" or "so"
//    waits a little longer. The server transcribes; the browser's own
//    dictation is the fallback.
// Raven keeps only the text. Audio is never stored.
const RavenVoice = (() => {
  const END_MS = 650;           // silence after a finished thought
  const HOLD_MS = 1100;         // extra wait after a trailing "and", "so", "um"
  const MAX_TURN_MS = 90000;
  const PARTIAL_MS = 1400;
  let link = '';                // the task page sends its link instead of a session
  let status = null, statusLoad = null;
  let ctx = null, outAnalyser = null, micAnalyser = null, levelSource = null;
  let speaking = null, listening = null, level = 0, loop = 0;

  const post = (action, body) => fetch(`/api/${link ? 'brief/' : ''}voice/${action}`, {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', ...(link ? {'X-Raven-Link': link} : {'X-Bridge-CSRF': (typeof state !== 'undefined' && state.csrf_token) || ''})},
    body: JSON.stringify(body || {}),
  });
  const json = async response => {
    const value = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(value.error || `Voice request failed (${response.status})`);
    return value;
  };
  const emit = detail => document.dispatchEvent(new CustomEvent('raven:voice', {detail: {...detail, at: performance.now()}}));

  function configure(options = {}) { if ('link' in options) { link = options.link || ''; status = null; statusLoad = null; } }

  async function capabilities() {
    if (status) return status;
    if (!statusLoad) statusLoad = post('status').then(json).then(s => (status = s)).catch(() => (status = {tts: '', stt: ''}));
    return statusLoad;
  }
  const browserCanListen = () => !!(window.SpeechRecognition || window.webkitSpeechRecognition) && window.isSecureContext;
  const browserCanSpeak = () => typeof window.speechSynthesis !== 'undefined' && typeof window.SpeechSynthesisUtterance !== 'undefined';
  const canListen = () => !!status?.stt ? !!(navigator.mediaDevices?.getUserMedia && window.MediaRecorder) : browserCanListen();
  const canSpeak = () => !!status?.tts || browserCanSpeak();

  // Call from a click (starting the call) so the browser lets audio play.
  function unlock() {
    try {
      if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
      if (ctx.state === 'suspended') ctx.resume();
    } catch { ctx = null; }
    capabilities();
    tick();
  }

  // ------------------------------------------------------------ the orb
  function orb(mode, size = '') {
    return `<div class="raven-orb ${mode} ${size}" aria-hidden="true"><span class="halo"></span><span class="core"><i></i><i></i></span></div>`;
  }
  function rms(analyser) {
    const data = new Float32Array(analyser.fftSize);
    analyser.getFloatTimeDomainData(data);
    let sum = 0;
    for (const v of data) sum += v * v;
    return Math.sqrt(sum / data.length);
  }
  // One loop drives every orb on the page from whichever sound is live.
  function tick() {
    if (loop) return;
    const step = () => {
      const analyser = levelSource === 'out' ? outAnalyser : levelSource === 'mic' ? micAnalyser : null;
      const target = analyser ? Math.min(1, rms(analyser) * (levelSource === 'out' ? 5 : 9)) : 0;
      // Fast attack, slow release: the way a voice orb feels alive.
      level += (target - level) * (target > level ? 0.45 : 0.12);
      const value = level.toFixed(3);
      document.querySelectorAll('.raven-orb').forEach(el => el.style.setProperty('--level', value));
      loop = requestAnimationFrame(step);
    };
    loop = requestAnimationFrame(step);
  }

  // ------------------------------------------------------------ speaking
  const sentences = text => (String(text || '').match(/[^.!?]+[.!?]+["')\]]*\s*|[^.!?]+$/g) || [])
    .map(s => s.trim()).filter(Boolean)
    // Very short pieces ride with the next one so the voice keeps its flow.
    .reduce((out, s) => { if (out.length && out[out.length - 1].length < 24) out[out.length - 1] += ' ' + s; else out.push(s); return out; }, []);

  function stopSpeaking() {
    const current = speaking;
    speaking = null;
    if (levelSource === 'out') levelSource = null;
    if (current) { current.cancelled = true; try { current.source?.stop(); } catch {} current.done?.(); }
    if (browserCanSpeak()) { try { window.speechSynthesis.cancel(); } catch {} }
  }

  // Resolves when Raven has finished saying it (or was stopped).
  async function speak(text) {
    stopSpeaking();
    text = String(text || '').trim();
    if (!text) return;
    await capabilities();
    const job = {cancelled: false};
    speaking = job;
    emit({type: 'speak-start', text});
    try {
      if (status.tts && ctx) await speakServer(job, text);
      else if (browserCanSpeak()) await speakBrowser(job, text);
    } catch (error) {
      if (!job.cancelled && browserCanSpeak()) await speakBrowser(job, text).catch(() => {});
    } finally {
      if (speaking === job) { speaking = null; if (levelSource === 'out') levelSource = null; }
      emit({type: 'speak-end', text, cancelled: job.cancelled});
    }
  }

  async function speakServer(job, text) {
    if (ctx.state === 'suspended') await ctx.resume().catch(() => {});
    if (!outAnalyser) { outAnalyser = ctx.createAnalyser(); outAnalyser.fftSize = 1024; outAnalyser.connect(ctx.destination); }
    // Ask for every sentence at once; play them in order as they arrive.
    const parts = sentences(text).map(s => post('speak', {text: s}).then(async r => {
      if (!r.ok) await json(r);
      return ctx.decodeAudioData(await r.arrayBuffer());
    }));
    parts.forEach(p => p.catch(() => {}));
    for (const part of parts) {
      const buffer = await part;
      if (job.cancelled) return;
      await new Promise(resolve => {
        const source = ctx.createBufferSource();
        source.buffer = buffer;
        source.connect(outAnalyser);
        job.source = source; job.done = resolve;
        source.onended = resolve;
        levelSource = 'out';
        source.start();
      });
      if (job.cancelled) return;
    }
  }

  function speakBrowser(job, text) {
    return new Promise(resolve => {
      const say = new SpeechSynthesisUtterance(text);
      const voices = window.speechSynthesis.getVoices?.() || [];
      say.voice = voices.find(v => /Samantha|Google US English|Jenny|Aria|Natural/i.test(v.name)) || voices.find(v => /^en/i.test(v.lang)) || null;
      say.rate = 1.04;
      say.onend = say.onerror = () => resolve();
      job.done = resolve;
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(say);
    });
  }

  // ------------------------------------------------------------ listening
  const trailing = /\b(and|but|so|because|or|um+|uh+|like|then|the|a|to|of|with|which|that|if|when|actually|i mean)[\s,.…-]*$/i;
  const blobBase64 = blob => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
  async function transcribe(chunks, mime) {
    const blob = new Blob(chunks, {type: mime});
    return json(await post('transcribe', {audio: await blobBase64(blob), mime}));
  }

  function stopListening() {
    const current = listening;
    listening = null;
    if (levelSource === 'mic') levelSource = null;
    if (!current) return;
    current.stopped = true;
    clearInterval(current.timer); clearTimeout(current.hold);
    try { current.recorder?.state !== 'inactive' && current.recorder?.stop(); } catch {}
    current.stream?.getTracks().forEach(t => t.stop());
    try { current.mic?.disconnect(); } catch {}
    if (current.recognition) { current.recognition.onresult = current.recognition.onerror = current.recognition.onend = null; try { current.recognition.abort(); } catch {} }
  }

  // handlers: onheard(text so far), onturn(final text, method), onerror(error)
  async function listen(handlers = {}) {
    stopListening();
    await capabilities();
    const turn = {stopped: false, handlers};
    listening = turn;
    emit({type: 'listen-start'});
    try {
      if (status.stt && navigator.mediaDevices?.getUserMedia && window.MediaRecorder) await listenServer(turn);
      else if (browserCanListen()) listenBrowser(turn);
      else throw new Error('No microphone or speech recognition is available here. Type instead.');
    } catch (error) {
      if (listening === turn) stopListening();
      handlers.onerror?.(error);
    }
    return turn;
  }

  async function listenServer(turn) {
    const stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true}});
    if (turn.stopped) { stream.getTracks().forEach(t => t.stop()); return; }
    unlock();
    turn.stream = stream;
    if (ctx) {
      turn.mic = ctx.createMediaStreamSource(stream);
      if (!micAnalyser) { micAnalyser = ctx.createAnalyser(); micAnalyser.fftSize = 1024; }
      turn.mic.connect(micAnalyser);
      levelSource = 'mic';
    }
    const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'].find(t => MediaRecorder.isTypeSupported?.(t)) || '';
    const recorder = new MediaRecorder(stream, mime ? {mimeType: mime} : {});
    const type = recorder.mimeType || mime || 'audio/webm';
    const chunks = [];
    recorder.ondataavailable = e => { if (e.data?.size) chunks.push(e.data); };
    recorder.start(200);
    turn.recorder = recorder;
    const started = performance.now();
    let floor = 0.004, voiced = 0, heardAt = 0, lastVoice = 0, partialAt = 0, partialBusy = false, ending = false;
    const finish = async () => {
      if (ending || turn.stopped) return;
      ending = true;
      try { recorder.requestData(); } catch {}
      await new Promise(r => setTimeout(r, 60));
      const spokeAt = lastVoice;
      let result;
      try { result = await transcribe(chunks.slice(), type); } catch (error) { ending = false; if (!turn.stopped) turn.handlers.onerror?.(error); return; }
      if (turn.stopped) return;
      const text = (result.text || '').trim();
      // They started talking again while we transcribed: keep listening.
      if (lastVoice > spokeAt) { ending = false; if (text) turn.handlers.onheard?.(text); return; }
      if (!text) { ending = false; heardAt = 0; return; }
      if (trailing.test(text) && !turn.held) {
        turn.held = true; ending = false;
        turn.handlers.onheard?.(text);
        turn.hold = setTimeout(() => { if (!turn.stopped && lastVoice <= spokeAt) finish(); }, HOLD_MS);
        return;
      }
      stopListening();
      emit({type: 'turn-end', text, engine: result.engine, transcribe_seconds: result.seconds});
      turn.handlers.onturn?.(text, 'server-speech');
    };
    turn.finish = () => { turn.held = true; clearTimeout(turn.hold); if (heardAt) finish(); };
    turn.timer = setInterval(() => {
      if (turn.stopped || !micAnalyser) return;
      const now = performance.now();
      const value = rms(micAnalyser);
      const threshold = Math.max(0.012, floor * 3.2);
      if (value > threshold) {
        voiced += 50;
        if (voiced >= 100) { lastVoice = now; if (!heardAt) { heardAt = now; emit({type: 'speech-start'}); } turn.held = false; clearTimeout(turn.hold); }
      } else {
        voiced = 0;
        floor = floor * 0.97 + value * 0.03;
      }
      if (heardAt && !ending && !partialBusy && now - partialAt > PARTIAL_MS && now - lastVoice < 400) {
        partialAt = now; partialBusy = true;
        transcribe(chunks.slice(), type).then(r => { if (!turn.stopped && !ending && r.text) turn.handlers.onheard?.(r.text); })
          .catch(() => {}).finally(() => { partialBusy = false; });
      }
      if (heardAt && !ending && now - lastVoice >= END_MS) finish();
      if (now - started > MAX_TURN_MS && heardAt) finish();
    }, 50);
  }

  function listenBrowser(turn) {
    const Speech = window.SpeechRecognition || window.webkitSpeechRecognition;
    const rec = new Speech();
    rec.lang = document.documentElement.lang || 'en-US';
    rec.continuous = true;
    rec.interimResults = true;
    let said = '';
    const done = () => { if (turn.stopped || !said) return; const text = said; stopListening(); emit({type: 'turn-end', text, engine: 'browser'}); turn.handlers.onturn?.(text, 'browser-speech'); };
    rec.onresult = event => {
      let interim = '';
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const text = event.results[i][0].transcript.trim();
        if (event.results[i].isFinal) said = (said + ' ' + text).trim(); else interim += ' ' + text;
      }
      turn.handlers.onheard?.((said + interim).trim());
      clearTimeout(turn.hold);
      if (said) turn.hold = setTimeout(done, trailing.test(said) ? END_MS + HOLD_MS : END_MS + 350);
    };
    rec.onerror = event => { if (event.error === 'no-speech') return; stopListening(); turn.handlers.onerror?.(new Error('The microphone stopped: ' + (event.error || 'unknown error') + '. You can type instead.')); };
    rec.onend = () => { if (listening === turn) { try { rec.start(); } catch { stopListening(); } } };
    turn.recognition = rec;
    turn.finish = () => { clearTimeout(turn.hold); done(); };
    rec.start();
  }

  // Send what has been heard now, without waiting for the pause.
  function finishTurn() { listening?.finish?.(); }

  window.addEventListener('pagehide', () => { stopSpeaking(); stopListening(); });
  return {configure, capabilities, unlock, orb, speak, stopSpeaking, listen, stopListening, finishTurn,
    canListen, canSpeak, isSpeaking: () => !!speaking, isListening: () => !!listening,
    usesServer: () => ({tts: !!status?.tts, stt: !!status?.stt})};
})();
