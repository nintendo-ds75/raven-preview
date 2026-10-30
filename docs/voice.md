# Voice calls

Raven talks with people in two places: the voice intro (`#intro/<person>`)
and the decision call ("Take a quick call instead" on a task page). Both run
on `web/voice.js` and `bridge/voice.py`.

## What runs where

| Piece | Where | Engine |
|---|---|---|
| Raven's voice | server, `POST /api/voice/speak` | ElevenLabs, OpenAI or local Kokoro |
| Transcripts | server, `POST /api/voice/transcribe` | ElevenLabs Scribe, OpenAI or local Whisper (sherpa-onnx) |
| End of turn | browser | silence detection on the microphone level |
| Fallback | browser | Web Speech dictation and speech synthesis |

Raven stores only the transcript text. Audio is never written to disk.

## Setting it up

Pick one per direction; unset means the first one that is configured.

```
RAVEN_TTS=elevenlabs|openai|kokoro|off     RAVEN_TTS_VOICE=<voice id or name>   RAVEN_TTS_MODEL=...
RAVEN_STT=elevenlabs|openai|whisper|off    RAVEN_STT_MODEL=...
ELEVENLABS_API_KEY=...   or   OPENAI_API_KEY=...
RAVEN_VOICE_DIR=.bridge/voice              # local models
```

Local (nothing leaves the machine, works offline and in a cloud container):

```
pip install -r requirements-voice.txt     # sherpa-onnx, kokoro-onnx; needs ffmpeg on PATH
scripts/fetch_voice_models.sh             # Whisper base.en + Kokoro v1.0, about 450 MB
```

`POST /api/voice/status` (or `/api/brief/voice/status` from a task link)
says which engines are live. The models load in the background when the
server starts, so the first turn isn't the slow one.

## Why the turn-taking is fast

Measured on a 4-core cloud container with the local engines: a 3 second
line is spoken back in about 1.3 s, and a 9 second answer is transcribed in
about 1 s.

- **End of turn by silence, not a button.** The browser watches the
  microphone level against a running noise floor. 650 ms of quiet after a
  finished sentence ends the turn. If the words so far end on "and", "so",
  "because" or "um", Raven waits another 1.1 s before deciding the person
  is done (a light version of semantic turn detection). "Done" ends a turn
  at once.
- **Speech starts before the whole reply is synthesized.** The reply is
  split into sentences, all requested at once, and played in order as they
  arrive, so the first sentence plays while the rest are made.
- **Live captions.** While the person talks, the recording so far is
  transcribed every 1.4 s so their words appear as they say them.
- **A time budget on the model.** The intro's follow-up comes from the
  fast model with 400 output tokens. If it takes longer than
  `BRIDGE_INTRO_TURN_SECONDS` (default 6), Raven asks the next planned
  question instead of leaving the caller in silence. The decision call has
  the same guard (`BRIDGE_CALL_TURN_SECONDS`, default 10). With an Anthropic
  API key the fast model answers well inside both; through `claude -p`
  (`BRIDGE_MODEL_API=claude-cli`) it usually does not, so expect planned
  questions there unless you raise the budget.

## The orb

One soft circle. Its size follows the real sound: Raven's voice while it
speaks (an analyser on the audio it plays) and the microphone while it
listens. Thinking is a slow breath. It never spins. When the browser's own
speech is used there is no audio stream to measure, so the orb pulses
gently instead.

## Recording a call in a headless browser

Chromium can feed a file to the microphone:

```
--use-fake-ui-for-media-stream --use-fake-device-for-media-stream
--use-file-for-fake-audio-capture=answer.wav%noloop
--autoplay-policy=no-user-gesture-required
```

The page fires `raven:voice` events on `document` (`speak-start`,
`speak-end`, `listen-start`, `speech-start`, `turn-end` with the
transcript), with `performance.now()` timestamps, for syncing captions to
what happened.
