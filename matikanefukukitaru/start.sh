#!/usr/bin/env bash
# Run the test suite, then start the bot in the background if every test passes
# (survives closing the terminal, not a reboot). A failing test means it doesn't start.
#
# Creates .venv on first run, and installs requirements-dev.txt whenever it has changed.
set -euo pipefail
cd "$(dirname "$0")"
DIR="$PWD"
PY="$DIR/.venv/bin/python"
# The full command line, so pgrep matches this bot and never WinningTicket's bot.py.
CMD="$PY $DIR/bot.py"

if pgrep -fx "$CMD" >/dev/null; then
  echo "Fuku is already running (pid $(pgrep -fx "$CMD" | tr '\n' ' ')). Use ./stop.sh first to restart it."
  exit 0
fi

if [ ! -x "$PY" ]; then
  echo "==> Creating .venv"
  python3 -m venv .venv
fi
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ] || [ requirements-dev.txt -nt .venv/.installed ]; then
  echo "==> Installing dependencies"
  "$PY" -m pip install -q --upgrade pip
  "$PY" -m pip install -q -r requirements-dev.txt
  touch .venv/.installed
fi

if [ ! -f .env ]; then
  echo "No .env found. Copy .env.example to .env and put the bot token in it." >&2
  exit 1
fi

echo "==> Running tests"
if ! "$PY" -m pytest -q; then
  echo "Tests failed, so Fuku was not started." >&2
  exit 1
fi

echo "==> Starting Fuku"
nohup $CMD >> bot.log 2>&1 &
disown

sleep 8
if pgrep -fx "$CMD" >/dev/null; then
  echo "Fuku started. Last log lines:"
else
  echo "Fuku failed to start. Last log lines:"
fi
tail -n 5 bot.log
