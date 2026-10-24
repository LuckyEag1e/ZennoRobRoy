#!/usr/bin/env bash
# Redeploy WinningTicket's code to a host and restart its service.
#
# Never touches data/tourney.db or .env on the remote host, since those hold
# live tournament data and the bot token, so it's safe to re-run anytime.
#
# The target comes from environment variables, usually exported by the
# gitignored deploy.local.sh (see deploy.local.sh.example):
#   DEPLOY_HOST          SSH host/alias to deploy to (required)
#   DEPLOY_DIR           absolute path of the remote directory to deploy into (required)
#   DEPLOY_SERVICE_NAME  systemd service name (default: winningticket)
set -euo pipefail

if [ -f "$(dirname "$0")/deploy.local.sh" ]; then
  # shellcheck source=/dev/null
  source "$(dirname "$0")/deploy.local.sh"
fi

: "${DEPLOY_HOST:?Set DEPLOY_HOST (e.g. DEPLOY_HOST=myhost ./deploy.sh), or create deploy.local.sh -- see deploy.local.sh.example}"
# Absolute because a bare ~ would expand to the local home, not the remote one.
: "${DEPLOY_DIR:?Set DEPLOY_DIR to an absolute path on the remote host -- see deploy.local.sh.example}"
DEPLOY_SERVICE_NAME="${DEPLOY_SERVICE_NAME:-winningticket}"

echo "==> Syncing code to $DEPLOY_HOST:$DEPLOY_DIR"
rsync -az --delete \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache' \
  --exclude='.git' \
  --exclude='data/' \
  --exclude='.env' \
  --exclude='.env.example' \
  --exclude='deploy.local.sh' \
  ./ "$DEPLOY_HOST:$DEPLOY_DIR/"

echo "==> Installing/updating dependencies"
ssh "$DEPLOY_HOST" "cd $DEPLOY_DIR && .venv/bin/pip install -q -r requirements.txt"

echo "==> Restarting service"
ssh "$DEPLOY_HOST" "sudo systemctl restart $DEPLOY_SERVICE_NAME"

sleep 3
echo "==> Recent logs"
ssh "$DEPLOY_HOST" "sudo journalctl -u $DEPLOY_SERVICE_NAME --no-pager -n 15"
