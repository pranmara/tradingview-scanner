#!/usr/bin/env bash
# One-shot VPS bootstrap for Ubuntu 22.04/24.04 or Debian 12. Run as root (or with sudo):
#   curl -fsSL https://raw.githubusercontent.com/pranmara/tradingview-scanner/main/deploy/bootstrap.sh | sudo bash
set -euo pipefail

REPO="${REPO:-https://github.com/pranmara/tradingview-scanner.git}"
DIR="${DIR:-/opt/tradingview-scanner}"
APP_UID=10001   # non-root user inside the app container; data/ must be writable by it

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root: sudo bash bootstrap.sh" >&2
  exit 1
fi

log "Base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git ufw openssl >/dev/null

log "Docker Engine + compose plugin"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker
docker compose version

log "Firewall (SSH, HTTP, HTTPS only)"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
ufw status | sed 's/^/  /'

log "Repository -> $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone "$REPO" "$DIR"
fi
cd "$DIR"

mkdir -p data
chown -R "$APP_UID:$APP_UID" data

if [ ! -f .env ]; then
  cp .env.example .env
  SECRET="$(openssl rand -hex 32)"
  sed -i "s|^TV_WEBHOOK_SECRET=.*|TV_WEBHOOK_SECRET=${SECRET}|" .env
  chmod 600 .env
  log ".env created with a random TV_WEBHOOK_SECRET"
fi

cat <<EOF

Done. Next steps:
  1. nano $DIR/.env  and set at least:
       TELEGRAM_BOT_TOKEN        (from @BotFather)
       TELEGRAM_ALLOWED_USER_IDS (your numeric Telegram id; send /scan once to see it)
       WEBHOOK_DOMAIN            (e.g. mybot.duckdns.org pointing at this VPS)
       ACME_EMAIL                (Let's Encrypt notices)
     optional: TV_SESSION_ID, NANSEN_API_KEY, TWELVEDATA_API_KEY, ACCOUNT_EQUITY
  2. cd $DIR && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
  3. docker compose logs -f app        # wait for "service started"
  4. curl -s https://\$WEBHOOK_DOMAIN/healthz
EOF
