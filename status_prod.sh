#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$DIR/prod_trader.pid"
LOG_FILE="$DIR/prod_trader.log"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "ttDayTrader PROD is RUNNING (PID: $(cat "$PID_FILE"))."
else
    RUNNING_PIDS=$(pgrep -f "1. PROD/ttDayTrader/.venv/bin/python -u -m app.main" || true)
    if [ -n "$RUNNING_PIDS" ]; then
        echo "ttDayTrader PROD is RUNNING (PID: $RUNNING_PIDS)."
    else
        echo "ttDayTrader PROD is STOPPED."
    fi
fi

if [ -f "$LOG_FILE" ]; then
    echo -e "\n--- Latest 20 lines of $LOG_FILE ---"
    tail -n 20 "$LOG_FILE"
fi
