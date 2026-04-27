#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PID_FILE="$PROJECT_ROOT/.dosm.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "No PID file at $PID_FILE — is DOSM running?"
    exit 1
fi

PID=$(cat "$PID_FILE")

if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    rm -f "$PID_FILE"
    echo "Stopped DOSM (PID $PID)"
else
    echo "Process $PID is not running — cleaning up stale PID file"
    rm -f "$PID_FILE"
fi
