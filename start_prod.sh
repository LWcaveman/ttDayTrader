#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

PID_FILE="$DIR/prod_trader.pid"
LOG_FILE="$DIR/prod_trader.log"

if pgrep -f "$DIR/.venv/bin/python -u -m app.main" >/dev/null; then
    echo "ttDayTrader PROD is already running."
    exit 1
fi

echo "Starting ttDayTrader PROD daemon..."
setsid systemd-inhibit --what=sleep:idle --why="ttDayTrader prod active" \
    "$DIR/.venv/bin/python" -u -m app.main < /dev/null >> "$LOG_FILE" 2>&1 &

sleep 2
PYTHON_PID=$(pgrep -f "$DIR/.venv/bin/python -u -m app.main" || true)
if [ -n "$PYTHON_PID" ]; then
    echo "$PYTHON_PID" > "$PID_FILE"
    echo "ttDayTrader PROD started (PID: $PYTHON_PID)."
    echo "Logs streaming to: $LOG_FILE"
else
    echo "Failed to start ttDayTrader PROD. Check $LOG_FILE for details."
    exit 1
fi
