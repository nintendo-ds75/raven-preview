#!/bin/sh
# Local voice models for Raven's calls (see docs/voice.md): Whisper base.en
# through sherpa-onnx for transcripts and Kokoro for Raven's voice. About
# 450 MB, all from GitHub releases. Nothing leaves the machine at run time.
set -e
DIR="${RAVEN_VOICE_DIR:-.bridge/voice}"
mkdir -p "$DIR"
cd "$DIR"
if [ ! -f sherpa-onnx-whisper-base.en/base.en-encoder.int8.onnx ]; then
  curl -fsSL -O https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-whisper-base.en.tar.bz2
  tar xjf sherpa-onnx-whisper-base.en.tar.bz2 && rm sherpa-onnx-whisper-base.en.tar.bz2
fi
for f in kokoro-v1.0.onnx voices-v1.0.bin; do
  [ -f "$f" ] || curl -fsSL -O "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/$f"
done
echo "Voice models are in $DIR. Install the runtimes with: pip install -r requirements-voice.txt (and have ffmpeg on PATH)."
