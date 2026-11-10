#!/bin/bash
# Pull the latest code and restart. Run inside your clone on the VM: ./deploy/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
APP=/opt/bachat
sudo rsync -a --delete --exclude .venv --exclude node_modules --exclude data ./ "$APP/"
sudo chown -R bachat:bachat "$APP"
sudo -u bachat "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
(cd "$APP/frontend" && sudo -u bachat HOME="$APP" npm ci --silent && sudo -u bachat HOME="$APP" npm run build >/dev/null)
sudo systemctl restart bachat
echo "Updated. Logs: journalctl -u bachat -f"
