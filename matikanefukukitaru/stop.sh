#!/usr/bin/env bash
# Stop the bot that start.sh started.
set -euo pipefail
cd "$(dirname "$0")"
CMD="$PWD/.venv/bin/python $PWD/bot.py"

if ! pgrep -fx "$CMD" >/dev/null; then
  echo "Fuku isn't running."
  exit 0
fi

pkill -fx "$CMD"
for _ in $(seq 10); do
  pgrep -fx "$CMD" >/dev/null || { echo "Fuku stopped."; exit 0; }
  sleep 1
done
echo "Fuku didn't stop within 10s (pid $(pgrep -fx "$CMD" | tr '\n' ' '))." >&2
exit 1
