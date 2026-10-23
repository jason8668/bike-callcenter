#!/bin/bash
# ============================================================
# bike-callcenter trial installer (run as root on a FRESH server)
# - Installs Docker if missing
# - Clones THIS repo from GitHub (edit REPO_URL below first)
# - Starts Zammad (official docker-compose, Elasticsearch disabled, port 8080)
# - Builds and starts the bridge middleware (port 127.0.0.1:8090)
# - FusionPBX/FreeSWITCH is NOT installed by this script:
#   run the official FusionPBX installer on a Debian 12 host (can be a
#   second machine), see docs/05-cloud-deploy-guide.md
# All commands/outputs are English-only on purpose (VNC console friendly).
# ============================================================
set -e

REPO_URL="https://github.com/jason8668/bike-callcenter.git"
APP_DIR="/opt/bike-callcenter"

echo "[1/6] System check"
cat /etc/os-release | head -2
free -h | head -2

echo "[2/6] Install Docker if missing"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
docker --version
docker compose version

echo "[3/6] Clone project repo"
if [ -d "$APP_DIR/.git" ]; then
  cd "$APP_DIR" && git pull
else
  git clone "$REPO_URL" "$APP_DIR"
  cd "$APP_DIR"
fi

echo "[4/6] Prepare Zammad stack"
cd "$APP_DIR/deploy"
if [ ! -d zammad ]; then
  git clone https://github.com/zammad/zammad-docker-compose.git zammad
fi
cd zammad
cp -n .env.dist .env || true
# Disable Elasticsearch (trial, saves ~1.5GB RAM) and set a random secret
sed -i 's/^ELASTICSEARCH_ENABLED=.*/ELASTICSEARCH_ENABLED=false/' .env || echo "ELASTICSEARCH_ENABLED=false" >> .env
cp "$APP_DIR/deploy/zammad-override.yml" docker-compose.override.yml
echo "Starting Zammad (first start takes several minutes)..."
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d

echo "[5/6] Prepare bridge middleware env"
cd "$APP_DIR/middleware"
if [ ! -f .env ]; then
  cp .env.example .env
  SECRET=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
  sed -i "s/^BRIDGE_SECRET=.*/BRIDGE_SECRET=$SECRET/" .env
  echo "Bridge secret generated into middleware/.env"
  echo "IMPORTANT: after Zammad first-run setup, create an API token and a"
  echo "CTI token in Zammad admin, fill ZAMMAD_TOKEN / ZAMMAD_CTI_TOKEN in"
  echo "$APP_DIR/middleware/.env, then run deploy/update-from-github.sh"
fi

echo "[6/6] Build and start bridge"
cd "$APP_DIR/deploy"
docker compose up -d --build bridge

echo "------------------------------------------------------------"
echo "Zammad status:"
docker compose -f zammad/docker-compose.yml -f zammad/docker-compose.override.yml ps || true
echo "Bridge health:"
sleep 3
curl -s http://127.0.0.1:8090/health || echo "bridge not up yet, check: docker logs bike-bridge"
echo ""
echo "Next steps:"
echo " 1) Open http://<server-ip>:8080 and finish Zammad first-run setup."
echo " 2) Create tokens, fill middleware/.env, run the field setup script:"
echo "    ZAMMAD_URL=http://127.0.0.1:8080 ZAMMAD_TOKEN=xxx python3 $APP_DIR/zammad-setup/setup_bike_fields.py"
echo " 3) Simulate one call to verify the whole chain:"
echo "    curl -X POST http://127.0.0.1:8090/simulate/call -H \"X-Bridge-Secret: <secret from middleware/.env>\""
echo "Done."
