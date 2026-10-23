#!/bin/bash
# ============================================================
# bike-callcenter trial installer (run as root)
# CO-LOCATION MODE: designed to share the 4C/8G course-demo server.
# - Zammad runs as Docker containers on host port 8899 (NOT 8080:
#   filing ports 80/8080/443/8443 are blocked inbound on this instance)
# - Elasticsearch disabled + per-container memory caps (see
#   deploy/zammad-override.yml) so the course demo keeps its share
# - Adds a 2G swap file if the host has none (burst protection)
# - Builds and starts the bridge middleware (port 127.0.0.1:8090)
# - FusionPBX is NOT installed by this script on a shared host: its
#   installer takes over the host (nginx 80/443, system packages).
#   Test phase 2 uses a plain FreeSWITCH container instead;
#   FusionPBX UI goes to a dedicated host only if the project goes live.
# NOTE: the project repo was made PUBLIC on 2026-10-23 so this server can
# clone it without credentials (same as the course project repo).
# All commands/outputs are English-only on purpose (VNC console friendly).
# ============================================================
set -e

REPO_URL="https://github.com/jason8668/bike-callcenter.git"
APP_DIR="/opt/bike-callcenter"

echo "[1/6] System check"
cat /etc/os-release | head -2
free -h
docker ps --format "table {{.Names}}\t{{.Status}}" || true
if ! swapon --show | grep -q .; then
  echo "No swap found, creating a 2G swap file (burst protection)..."
  fallocate -l 2G /swapfile-bike || dd if=/dev/zero of=/swapfile-bike bs=1M count=2048
  chmod 600 /swapfile-bike
  mkswap /swapfile-bike && swapon /swapfile-bike
  free -h
fi

echo "[2/6] Install Docker if missing"
if ! command -v docker >/dev/null 2>&1; then
  echo "Trying official get.docker.com installer..."
  curl -fsSL https://get.docker.com | sh || true
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "Falling back to distro packages (docker.io)..."
  apt-get update && apt-get install -y docker.io
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Installing docker compose v2 plugin from distro packages..."
  apt-get install -y docker-compose-v2 || apt-get install -y docker-compose-plugin || true
fi
docker --version
docker compose version

echo "[2.5/6] Configure registry mirrors (fast image pulls in mainland China)"
mkdir -p /etc/docker
if [ ! -f /etc/docker/daemon.json ]; then
  cat > /etc/docker/daemon.json <<'EOF'
{"registry-mirrors": ["https://docker.m.daocloud.io", "https://docker.1panel.live"]}
EOF
fi
(systemctl restart docker || service docker restart) || true
sleep 2
docker info >/dev/null 2>&1 && echo "docker daemon OK"

clone_repo() {
  # $1 = https repo url, $2 = target dir.
  # Mainland cloud hosts sometimes cannot reach github.com directly;
  # fall back to the gh-proxy mirror (same pattern as the course project).
  if [ -d "$2/.git" ]; then
    (cd "$2" && git pull) && return 0
    echo "git pull failed for $2, keeping local copy."
    return 0
  fi
  git clone "$1" "$2" && return 0
  echo "Direct GitHub clone failed, trying gh-proxy mirror..."
  git clone "https://gh-proxy.com/$1" "$2"
}

echo "[3/6] Clone project repo"
clone_repo "$REPO_URL" "$APP_DIR"
cd "$APP_DIR"

echo "[4/6] Prepare Zammad stack"
cd "$APP_DIR/deploy"
if [ ! -d zammad ]; then
  clone_repo https://github.com/zammad/zammad-docker-compose.git zammad
fi
cd zammad
cp -n .env.dist .env || true
cp "$APP_DIR/deploy/zammad-override.yml" docker-compose.override.yml
# Co-location settings in zammad's .env:
# - publish nginx on host port 8899 (8080 is a blocked filing port here)
# - disable the Elasticsearch service via the official scenario file
# - include our override (memory caps) in the compose file chain
grep -q '^NGINX_EXPOSE_PORT=' .env && sed -i 's/^NGINX_EXPOSE_PORT=.*/NGINX_EXPOSE_PORT=8899/' .env || echo "NGINX_EXPOSE_PORT=8899" >> .env
grep -q '^ELASTICSEARCH_ENABLED=' .env && sed -i 's/^ELASTICSEARCH_ENABLED=.*/ELASTICSEARCH_ENABLED=false/' .env || echo "ELASTICSEARCH_ENABLED=false" >> .env
COMPOSE_LINE="COMPOSE_FILE=docker-compose.yml:scenarios/disable-elasticsearch-service.yml:docker-compose.override.yml"
grep -q '^COMPOSE_FILE=' .env && sed -i "s|^COMPOSE_FILE=.*|$COMPOSE_LINE|" .env || echo "$COMPOSE_LINE" >> .env
echo "Starting Zammad (first start takes several minutes)..."
docker compose up -d

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
(cd zammad && docker compose ps) || true
echo "Bridge health:"
sleep 3
curl -s http://127.0.0.1:8090/health || echo "bridge not up yet, check: docker logs bike-bridge"
echo ""
echo "Next steps:"
echo " 1) Open http://<server-ip>:8899 and finish Zammad first-run setup."
echo "    (Cloud security group must allow inbound TCP 8899 first.)"
echo " 2) Create tokens, fill middleware/.env, run the field setup script:"
echo "    ZAMMAD_URL=http://127.0.0.1:8899 ZAMMAD_TOKEN=xxx python3 $APP_DIR/zammad-setup/setup_bike_fields.py"
echo " 3) Simulate one call to verify the whole chain:"
echo "    curl -X POST http://127.0.0.1:8090/simulate/call -H \"X-Bridge-Secret: <secret from middleware/.env>\""
echo "Done."
