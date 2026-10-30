#!/bin/sh
# A preloaded Parcelwise workspace on this machine, for a live demo: Raven with
# every teammate able to sign in, the synthetic company repo, and Claude Code in
# that repo wired to Raven over MCP as Maya Chen. See docs/parcelwise-demo.md.
#
#   scripts/parcelwise_demo.sh           start, keeping what you did last time
#   scripts/parcelwise_demo.sh --reset   start again from the preloaded state
#   scripts/parcelwise_demo.sh --voice   use local voice models (about 450 MB, fetched once)
#
# Raven uses ANTHROPIC_API_KEY from your environment when it is set, otherwise the
# signed-in Claude Code CLI. The key is never written to disk.
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DIR=${PARCELWISE_DEMO_DIR:-$HOME/.raven-parcelwise-demo}
PORT=${PORT:-7333}
PASSWORD=parcelwise-demo
RESET= VOICE=
for arg in "$@"; do
  case $arg in
    --reset) RESET=1 ;;
    --voice) VOICE=1 ;;
    *) echo "usage: $0 [--reset] [--voice]" >&2; exit 2 ;;
  esac
done

# macOS ships python3 3.9; Raven needs 3.11 or newer.
PY=
for c in ${PYTHON:-} python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then PY=$c; break; fi
done
[ -n "$PY" ] || { echo "Raven needs Python 3.11 or newer (brew install python@3.13)." >&2; exit 1; }

mkdir -p "$DIR"
REPO="$DIR/parcelwise-app"
DB="$DIR/raven.db"
[ -d "$REPO/.git" ] || git clone -q https://github.com/parcelwise/parcelwise-app.git "$REPO"

if [ -n "$RESET" ] || [ ! -f "$DB" ]; then
  rm -f "$DB" "$DB-wal" "$DB-shm"
  cp "$ROOT/fixtures/parcelwise-demo.db" "$DB"
  # Point Raven at this clone, give every teammate a sign-in, and mint Maya's
  # agent credential for Claude Code (kept only in the clone's .mcp.json).
  "$PY" - "$ROOT" "$DB" "$REPO" "$PORT" "$PASSWORD" <<'EOF'
import json, sys
root, db, repo, port, password = sys.argv[1:6]
sys.path.insert(0, root)
from bridge.accounts import password_hash
from bridge.auth import Auth
from bridge.store import Store
store = Store(db)
graph = store.graph
with graph.transaction():
    graph.db.execute("UPDATE connector_sources SET locator=? WHERE kind='git'", (repo,))
    for person in graph.people():
        graph.db.execute("INSERT OR REPLACE INTO account_passwords(person_id, password_hash) VALUES(?,?)",
                         (person["id"], password_hash(password)))
maya = graph.find_person("maya@parcelwise.example")
token = Auth(store, enabled=True).create_token(maya["id"], label="Claude Code (Parcelwise demo)")["token"]
with open(f"{repo}/.mcp.json", "w") as f:
    json.dump({"mcpServers": {"raven": {"type": "http", "url": f"http://localhost:{port}/mcp",
                                        "headers": {"Authorization": f"Bearer {token}"}}}}, f, indent=2)
EOF
  grep -qx '.mcp.json' "$REPO/.git/info/exclude" 2>/dev/null || echo '.mcp.json' >> "$REPO/.git/info/exclude"
fi

if [ -n "$VOICE" ]; then
  [ -x "$DIR/venv/bin/python" ] || "$PY" -m venv "$DIR/venv"
  "$DIR/venv/bin/python" -m pip install -q -r "$ROOT/requirements-voice.txt"
  RAVEN_VOICE_DIR="$DIR/voice" sh "$ROOT/scripts/fetch_voice_models.sh" >/dev/null
  command -v ffmpeg >/dev/null 2>&1 || echo "Local voice needs ffmpeg on PATH (brew install ffmpeg)." >&2
  PY="$DIR/venv/bin/python"
  export RAVEN_VOICE_DIR="$DIR/voice" RAVEN_TTS=kokoro RAVEN_STT=whisper RAVEN_TTS_VOICE=${RAVEN_TTS_VOICE:-bf_emma}
fi

if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  export BRIDGE_MODEL_API=anthropic
elif command -v claude >/dev/null 2>&1; then
  export BRIDGE_MODEL_API=claude-cli
else
  echo "No ANTHROPIC_API_KEY and no claude CLI: Ask Raven will show quotes without written answers." >&2
fi
export BRIDGE_CALL_TURN_SECONDS=${BRIDGE_CALL_TURN_SECONDS:-20}

cat <<EOF

  Raven        http://localhost:$PORT
  Sign in as   maya, ana, jordan, lena, marcus, priya, sam, theo or noor @parcelwise.example
  Password     $PASSWORD   (a private window lets two people be signed in at once)
  Claude Code  cd "$REPO" && claude     (Raven's MCP is set up there, as Maya Chen)
  Live intro   sign in as noor@parcelwise.example: nobody has talked with Noor yet
  The PR       https://github.com/parcelwise/parcelwise-app/pull/15

EOF
cd "$ROOT"
exec "$PY" -m bridge --db "$DB" serve --port "$PORT" --auth on
