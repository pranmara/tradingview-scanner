#!/usr/bin/env bash
# Pull the latest code and redeploy with zero manual steps. Run from anywhere:  sudo bash /opt/tradingview-scanner/deploy/update.sh
set -euo pipefail
DIR="${DIR:-/opt/tradingview-scanner}"
cd "$DIR"
git pull --ff-only
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build --remove-orphans
docker image prune -f >/dev/null
docker compose ps
