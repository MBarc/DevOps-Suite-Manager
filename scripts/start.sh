#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VENV="$PROJECT_ROOT/.venv"
PID_FILE="$PROJECT_ROOT/.dosm.pid"
LOG_FILE="$PROJECT_ROOT/dosm.log"

# Resolve python3 (Linux) or python (some envs)
PYTHON_BIN="$(command -v python3 2>/dev/null || command -v python)"

# Bootstrap venv if the interpreter is missing
if [ ! -f "$VENV/bin/python" ]; then
    echo "No venv found — creating one..."
    "$PYTHON_BIN" -m venv "$VENV"
    echo "Installing dependencies (this may take a minute)..."
    "$VENV/bin/pip" install --quiet -e "$PROJECT_ROOT"
    echo "Done."
fi

VENV_PYTHON="$VENV/bin/python"

# Default DOSM_HOME if not already set
export DOSM_HOME="${DOSM_HOME:-$PROJECT_ROOT/.dosm-home}"

if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "DOSM is already running (PID $OLD_PID). Run stop.sh first."
        exit 1
    else
        rm -f "$PID_FILE"
    fi
fi

echo "Starting DOSM"
echo "  DOSM_HOME : $DOSM_HOME"
echo "  Python    : $VENV_PYTHON"
echo "  Log       : $LOG_FILE"

nohup "$VENV_PYTHON" -m dosm serve >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "Started — PID $(cat "$PID_FILE")"
echo "Run './scripts/stop.sh' to stop."
