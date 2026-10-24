#!/usr/bin/env bash
# One-time remote setup: creates the venv, installs dependencies, and
# installs+enables the systemd service on a fresh target host. Run this once
# per host; use deploy.sh for routine code updates afterward.
#
# Reads the same DEPLOY_HOST/DEPLOY_DIR/DEPLOY_SERVICE_NAME as deploy.sh (see
# deploy.sh's header comment), plus DEPLOY_USER (the user the service should
# run as on the remote host -- defaults to whoever DEPLOY_HOST's SSH user is).
set -euo pipefail

# Everything below (rsync's ./, the service template) is relative to this folder,
# so work from here no matter where the script was started.
cd "$(dirname "$0")"

if [ -f deploy.local.sh ]; then
  # shellcheck source=/dev/null
  source deploy.local.sh
fi

: "${DEPLOY_HOST:?Set DEPLOY_HOST, or create deploy.local.sh -- see deploy.local.sh.example}"
# Absolute because a bare ~ would expand to the local home, and systemd never expands it.
: "${DEPLOY_DIR:?Set DEPLOY_DIR to an absolute path on the remote host -- see deploy.local.sh.example}"
DEPLOY_SERVICE_NAME="${DEPLOY_SERVICE_NAME:-winningticket}"
DEPLOY_USER="${DEPLOY_USER:-$(ssh "$DEPLOY_HOST" whoami)}"

echo "==> Creating remote directory and syncing code (including data/ and .env this one time)"
ssh "$DEPLOY_HOST" "mkdir -p $DEPLOY_DIR/data"
rsync -az \
  --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.pytest_cache' --exclude='.git' --exclude='data/cli.db' \
  --exclude='deploy.local.sh' \
  ./ "$DEPLOY_HOST:$DEPLOY_DIR/"
echo "    NOTE: this includes data/tourney.db and .env if present locally -- fine for a"
echo "    one-time initial deploy, but deploy.sh (routine updates) never touches either again."

echo "==> Setting up venv + dependencies"
ssh "$DEPLOY_HOST" "cd $DEPLOY_DIR && python3 -m venv .venv && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.txt"

echo "==> Installing systemd service"
sed -e "s|__DEPLOY_USER__|$DEPLOY_USER|g" -e "s|__DEPLOY_DIR__|$DEPLOY_DIR|g" \
  deploy/winningticket.service.template > /tmp/"$DEPLOY_SERVICE_NAME".service
scp /tmp/"$DEPLOY_SERVICE_NAME".service "$DEPLOY_HOST:/tmp/$DEPLOY_SERVICE_NAME.service"
rm /tmp/"$DEPLOY_SERVICE_NAME".service
ssh "$DEPLOY_HOST" "sudo mv /tmp/$DEPLOY_SERVICE_NAME.service /etc/systemd/system/$DEPLOY_SERVICE_NAME.service && \
  sudo systemctl daemon-reload && sudo systemctl enable --now $DEPLOY_SERVICE_NAME"

sleep 3
echo "==> Status"
ssh "$DEPLOY_HOST" "sudo systemctl status $DEPLOY_SERVICE_NAME --no-pager"
