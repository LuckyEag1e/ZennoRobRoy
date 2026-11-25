#!/usr/bin/env bash
# Start the bot in the background (survives closing the terminal, not a reboot).
cd "$(dirname "$0")" || exit 1

if pgrep -f '^node src/index.js' >/dev/null; then
  echo "Bot is already running (pid $(pgrep -f '^node src/index.js' | tr '\n' ' '))."
  exit 0
fi

nohup npm start >> bot.log 2>&1 &
disown

sleep 8
if pgrep -f '^node src/index.js' >/dev/null; then
  echo "Bot started. Last log lines:"
else
  echo "Bot failed to start. Last log lines:"
fi
tail -n 5 bot.log
