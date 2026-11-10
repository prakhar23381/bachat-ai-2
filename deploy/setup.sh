#!/bin/bash
# One-time setup on a fresh Ubuntu 24.04 VM (needs Python 3.12). Run from a clone of the repo:
#   git clone -b realtime https://github.com/prakhar23381/bachat-ai-2.git && cd bachat-ai-2 && ./deploy/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
SRC=$(pwd)
APP=/opt/bachat

echo "[1/7] Packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv git curl gpg rsync debian-keyring debian-archive-keyring apt-transport-https
if ! command -v node >/dev/null; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - >/dev/null
    sudo apt-get install -y -qq nodejs
fi
if ! command -v caddy >/dev/null; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    sudo apt-get update -qq && sudo apt-get install -y -qq caddy
fi

echo "[2/7] Swap (small free-tier VMs run out of memory building the dashboard)"
if [ "$(free -m | awk '/Mem:/{print $2}')" -lt 2000 ] && ! swapon --show | grep -q swapfile; then
    sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "[3/7] Firewall: allow HTTP/HTTPS (Oracle images block them in iptables by default)"
if command -v netfilter-persistent >/dev/null; then
    for port in 80 443; do
        sudo iptables -C INPUT -p tcp --dport $port -j ACCEPT 2>/dev/null || sudo iptables -I INPUT 6 -p tcp --dport $port -j ACCEPT
    done
    sudo netfilter-persistent save >/dev/null
fi

echo "[4/7] App code in $APP"
id bachat >/dev/null 2>&1 || sudo useradd --system --home "$APP" --shell /usr/sbin/nologin bachat
if [ "$SRC" != "$APP" ]; then sudo rsync -a --delete --exclude .venv --exclude node_modules --exclude data "$SRC/" "$APP/"; fi
sudo mkdir -p "$APP/data" && sudo chown -R bachat:bachat "$APP"
sudo -u bachat python3 -m venv "$APP/.venv"
sudo -u bachat "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
(cd "$APP/frontend" && sudo -u bachat HOME="$APP" npm ci --silent && sudo -u bachat HOME="$APP" npm run build >/dev/null)

echo "[5/7] Secrets in /etc/bachat.env"
if [ ! -f /etc/bachat.env ]; then
    gen() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }
    sudo tee /etc/bachat.env >/dev/null <<ENV
TZ=Asia/Kolkata
BACHAT_DB=$APP/data/transactions.db
BACHAT_KEY=$(gen)
BACHAT_API_TOKEN=$(gen)
# Fill these in (see docs/SETUP.md), then: sudo systemctl restart bachat
BACHAT_TELEGRAM_TOKEN=
BACHAT_TELEGRAM_CHAT_ID=
BACHAT_SUMMARY_HOUR=21
ENV
    sudo chmod 600 /etc/bachat.env
fi

echo "[6/7] Service + HTTPS"
HOST="$(curl -fsS https://api.ipify.org | tr . -).sslip.io"
sudo cp "$APP/deploy/bachat.service" /etc/systemd/system/bachat.service
sed "s/{HOST}/$HOST/" "$APP/deploy/Caddyfile" | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now bachat
sudo systemctl reload caddy || sudo systemctl restart caddy

echo "[7/7] Done"
echo
echo "  Dashboard / API : https://$HOST"
echo "  API token       : sudo grep BACHAT_API_TOKEN /etc/bachat.env"
echo "  BACK UP BACHAT_KEY from /etc/bachat.env somewhere safe: without it the data is unreadable."
echo "  Logs            : journalctl -u bachat -f"
