#!/bin/sh
set -eu
if [ "${1:-serve}" = "serve" ]; then
    python -m bridge.bootstrap
    if [ "${BRIDGE_AGENTS:-0}" = "1" ]; then
        set -- "$@" --agents
    fi
fi
exec python -m bridge "$@"
