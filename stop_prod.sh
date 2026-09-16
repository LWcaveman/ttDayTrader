#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$DIR/prod_trader.pid"

if [ -f "$PID_FILE" ]; then
    PID="$(cat "$PID_FILE")"
    if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping ttDayTrader PROD (PID: $PID)..."
        pkill -P "$PID" 2>/dev/null || true
        kill "$PID" 2>/dev/null || true
        rm -f "$PID_FILE"
        echo "Stopped."
    else
        echo "PID file found, but process $PID is not running. Removing stale PID file."
        rm -f "$PID_FILE"
    fi
else
    # Fallback to searching running process
    RUNNING_PIDS=$(pgrep -f "1. PROD/ttDayTrader/.venv/bin/python -u -m app.main" || true)
    if [ -n "$RUNNING_PIDS" ]; then
        echo "Found running processes without PID file: $RUNNING_PIDS. Terminating..."
        kill $RUNNING_PIDS
        echo "Stopped."
    else
        echo "ttDayTrader PROD is not running."
    fi
fi
