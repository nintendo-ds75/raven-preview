"""Raven's voice on the server: speech to text and text to speech.

The calls (the voice intro and the decision call) used to lean on the
browser's own dictation and speech. That fails in many browsers, sends
audio to whoever the browser vendor picked, and gives a robotic voice.
Here the server does both, with one engine per direction:

RAVEN_TTS   elevenlabs | openai | kokoro | off
RAVEN_STT   elevenlabs | openai | whisper | off

Left unset, each picks the first that is set up: ElevenLabs with
ELEVENLABS_API_KEY, OpenAI with OPENAI_API_KEY, otherwise the local
models in RAVEN_VOICE_DIR (default .bridge/voice; fetch them with
scripts/fetch_voice_models.sh). Local means nothing leaves the machine:
Kokoro for speech and Whisper (through sherpa-onnx) for transcripts.
With none, the page falls back to the browser's speech.

Raven keeps only the transcript text. Audio is never stored.
"""
from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.request
import wave
from pathlib import Path

from .store import Invalid

TEXT_LIMIT = 1200
AUDIO_LIMIT = 6 << 20

ELEVEN_TTS = "https://api.elevenlabs.io/v1/text-to-speech/"
ELEVEN_STT = "https://api.elevenlabs.io/v1/speech-to-text"
OPENAI_TTS = "https://api.openai.com/v1/audio/speech"
OPENAI_STT = "https://api.openai.com/v1/audio/transcriptions"


class VoiceOff(Invalid):
    pass


def _dir():
    return Path(os.environ.get("RAVEN_VOICE_DIR") or ".bridge/voice")


def _kokoro_files():
    d = _dir()
    model = next((d / name for name in ("kokoro-v1.0.onnx", "kokoro-v1.0.fp16.onnx", "kokoro-v1.0.int8.onnx")
                  if (d / name).is_file()), None)
    voices = d / "voices-v1.0.bin"
    return (model, voices) if model and voices.is_file() else None


def _whisper_files():
    for sub in ("sherpa-onnx-whisper-base.en", "sherpa-onnx-whisper-small.en", "sherpa-onnx-whisper-tiny.en"):
        d = _dir() / sub
        size = sub.rsplit("-", 1)[1]
        enc, dec, tok = d / f"{size}-encoder.int8.onnx", d / f"{size}-decoder.int8.onnx", d / f"{size}-tokens.txt"
        if enc.is_file() and dec.is_file() and tok.is_file():
            return enc, dec, tok
    return None


def _importable(name):
    try:
        __import__(name)
        return True
    except Exception:
        return False


def tts_engine():
    choice = (os.environ.get("RAVEN_TTS") or "").strip().lower()
    if choice in ("off", "none", "browser"):
        return ""
    if choice == "elevenlabs" or (not choice and os.environ.get("ELEVENLABS_API_KEY")):
        return "elevenlabs" if os.environ.get("ELEVENLABS_API_KEY") else ""
    if choice == "openai" or (not choice and os.environ.get("OPENAI_API_KEY")):
        return "openai" if os.environ.get("OPENAI_API_KEY") else ""
    if choice in ("", "kokoro") and _kokoro_files() and _importable("kokoro_onnx"):
        return "kokoro"
    return ""


def stt_engine():
    choice = (os.environ.get("RAVEN_STT") or "").strip().lower()
    if choice in ("off", "none", "browser"):
        return ""
    if choice == "elevenlabs" or (not choice and os.environ.get("ELEVENLABS_API_KEY")):
        return "elevenlabs" if os.environ.get("ELEVENLABS_API_KEY") else ""
    if choice == "openai" or (not choice and os.environ.get("OPENAI_API_KEY")):
        return "openai" if os.environ.get("OPENAI_API_KEY") else ""
    if choice in ("", "whisper") and _whisper_files() and _importable("sherpa_onnx") and shutil.which("ffmpeg"):
        return "whisper"
    return ""


def status():
    """What the page should use. Empty means: use the browser's own."""
    tts, stt = tts_engine(), stt_engine()
    return {"tts": tts, "stt": stt, "local": {"tts": tts == "kokoro", "stt": stt == "whisper"}}


# ---------------------------------------------------------------- hosted engines

class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Invalid("The voice provider tried to redirect; refusing")


def _post(url, headers, body, timeout=60):
    """Keys go only to the exact provider endpoints above, never through a
    redirect."""
    if not (url.startswith(ELEVEN_TTS) or url in (ELEVEN_STT, OPENAI_TTS, OPENAI_STT)):
        raise Invalid("Unknown voice endpoint")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as error:
        detail = error.read()[:300].decode("utf-8", "replace")
        raise Invalid(f"The voice provider refused ({error.code}): {detail}") from None
    except OSError as error:
        raise Invalid(f"The voice provider could not be reached: {error}") from None


def _multipart(fields, audio, mime):
    boundary = "raven" + secrets.token_hex(12)
    out = io.BytesIO()
    for key, value in fields.items():
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    ext = "webm" if "webm" in mime else "ogg" if "ogg" in mime else "mp4" if "mp4" in mime else "wav"
    out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="answer.{ext}"\r\n'
              f'Content-Type: {mime or "application/octet-stream"}\r\n\r\n'.encode())
    out.write(audio)
    out.write(f"\r\n--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------- local engines

_lock = threading.Lock()
_kokoro = None
_whisper = None


def _local_kokoro():
    global _kokoro
    with _lock:
        if _kokoro is None:
            from kokoro_onnx import Kokoro
            model, voices = _kokoro_files()
            _kokoro = Kokoro(str(model), str(voices))
        return _kokoro


def _local_whisper():
    global _whisper
    with _lock:
        if _whisper is None:
            import sherpa_onnx
            enc, dec, tok = _whisper_files()
            _whisper = sherpa_onnx.OfflineRecognizer.from_whisper(
                encoder=str(enc), decoder=str(dec), tokens=str(tok), language="en", task="transcribe",
                num_threads=max(1, min(4, os.cpu_count() or 1)))
        return _whisper


def _wav(samples, rate):
    import numpy as np
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def _pcm16k(audio):
    """Whatever the browser recorded (webm/opus, ogg, mp4, wav) as 16 kHz
    mono floats, through ffmpeg."""
    import numpy as np
    run = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"],
                         input=audio, capture_output=True, timeout=60)
    if run.returncode != 0:
        raise Invalid("Could not read that recording: " + run.stderr.decode("utf-8", "replace")[:200])
    return np.frombuffer(run.stdout, dtype=np.float32)


def warm():
    """Load the local models in the background so the first turn of the
    first call isn't the slow one."""
    def load():
        try:
            if tts_engine() == "kokoro":
                _local_kokoro().create("Hi.", voice=_voice("af_heart"), speed=1.0, lang="en-us")
            if stt_engine() == "whisper":
                import numpy as np
                r = _local_whisper()
                s = r.create_stream()
                s.accept_waveform(16000, np.zeros(16000, dtype=np.float32))
                r.decode_stream(s)
        except Exception:
            pass
    threading.Thread(target=load, daemon=True, name="raven-voice-warm").start()


def _voice(default):
    return (os.environ.get("RAVEN_TTS_VOICE") or default).strip()


# ---------------------------------------------------------------- the two calls

def speak(text):
    """Raven saying `text`: (audio bytes, mime type)."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text or len(text) > TEXT_LIMIT:
        raise Invalid(f"Text to speak must be 1 to {TEXT_LIMIT} characters")
    engine = tts_engine()
    if engine == "elevenlabs":
        voice = _voice("21m00Tcm4TlvDq8ikWAM")
        if not re.fullmatch(r"[A-Za-z0-9]{8,40}", voice):
            raise Invalid("RAVEN_TTS_VOICE must be an ElevenLabs voice id")
        body = json.dumps({"text": text, "model_id": os.environ.get("RAVEN_TTS_MODEL") or "eleven_flash_v2_5"}).encode()
        audio, _ = _post(ELEVEN_TTS + voice + "?output_format=mp3_44100_128",
                         {"xi-api-key": os.environ["ELEVENLABS_API_KEY"], "Content-Type": "application/json",
                          "Accept": "audio/mpeg"}, body)
        return audio, "audio/mpeg"
    if engine == "openai":
        body = json.dumps({"model": os.environ.get("RAVEN_TTS_MODEL") or "gpt-4o-mini-tts", "voice": _voice("alloy"),
                           "input": text, "response_format": "mp3"}).encode()
        audio, _ = _post(OPENAI_TTS, {"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"],
                                      "Content-Type": "application/json"}, body)
        return audio, "audio/mpeg"
    if engine == "kokoro":
        samples, rate = _local_kokoro().create(text, voice=_voice("af_heart"), speed=1.05, lang="en-us")
        return _wav(samples, rate), "audio/wav"
    raise VoiceOff("No server voice is set up; the page uses the browser's")


# Sound tags speech models write for noise: *buzzer*, [Music], (door closes),
# ♪ ... ♪. None of it is something the person said.
_SOUND = re.compile(r"\*[^*]{1,40}\*|\[[^\]]{1,40}\]|\((?:[^()]{0,40}\b(?:noise|music|laugh\w*|cough\w*|sigh\w*|"
                    r"applause|buzz\w*|beep\w*|silence|inaudible|static|door\w*|ring\w*|click\w*|breath\w*)\b[^()]{0,40})\)|"
                    r"[♪♫]+[^♪♫]{0,80}[♪♫]*", re.I)


def clean_transcript(text):
    text = _SOUND.sub(" ", str(text or ""))
    return re.sub(r"\s+", " ", text).strip(" ,")


def transcribe(audio, mime=""):
    """What the person said in this recording, as text."""
    if not audio or len(audio) > AUDIO_LIMIT:
        raise Invalid("Recording must be between 1 byte and 6 MB")
    mime = str(mime or "")[:80]
    engine = stt_engine()
    started = time.monotonic()
    if engine == "elevenlabs":
        body, ctype = _multipart({"model_id": os.environ.get("RAVEN_STT_MODEL") or "scribe_v1",
                                  "language_code": "en", "tag_audio_events": "false"}, audio, mime)
        raw, _ = _post(ELEVEN_STT, {"xi-api-key": os.environ["ELEVENLABS_API_KEY"], "Content-Type": ctype}, body)
        text = json.loads(raw or b"{}").get("text", "")
    elif engine == "openai":
        body, ctype = _multipart({"model": os.environ.get("RAVEN_STT_MODEL") or "gpt-4o-mini-transcribe",
                                  "language": "en", "response_format": "json"}, audio, mime)
        raw, _ = _post(OPENAI_STT, {"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"], "Content-Type": ctype}, body)
        text = json.loads(raw or b"{}").get("text", "")
    elif engine == "whisper":
        samples = _pcm16k(audio)
        if len(samples) < 1600:
            text = ""
        else:
            recognizer = _local_whisper()
            stream = recognizer.create_stream()
            stream.accept_waveform(16000, samples)
            # Streams are independent; a live caption and the final
            # transcript decode side by side.
            recognizer.decode_stream(stream)
            text = stream.result.text
    else:
        raise VoiceOff("No server transcription is set up; the page uses the browser's")
    text = clean_transcript(text)
    # Whisper hears these in silence and room noise.
    if re.fullmatch(r"[\[\(]?(blank_audio|silence|music|inaudible|you|thank you\.?)[\]\)]?\.?", text, re.I):
        text = ""
    return {"text": text, "engine": engine, "seconds": round(time.monotonic() - started, 2)}
