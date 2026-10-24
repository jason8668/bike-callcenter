#!/bin/bash
# ============================================================
# One-shot configuration for the bike-callcenter trial instance.
# Run as root on the server AFTER Zammad first-run setup is done:
#   bash /opt/bike-callcenter/deploy/apply-cn-setup.sh
# It will:
#   [1] pull the latest project code from GitHub
#   [2] run deploy/zammad_cn_setup.rb inside Zammad (Simplified
#       Chinese everywhere, foreign channels/logins off, seeded
#       names renamed, bridge API + CTI tokens created)
#   [3] write the tokens into middleware/.env (values never printed)
#   [4] create the bicycle groups and ticket fields in Zammad
#   [5] rebuild the bridge (test page on host port 8890)
# Safe to re-run: every step is idempotent.
# English-only output on purpose (VNC console friendly).
# ============================================================
set -e
APP_DIR="/opt/bike-callcenter"
ZDIR="$APP_DIR/deploy/zammad"

echo "[1/6] Pull latest code"
cd "$APP_DIR"
# Proxy first: direct github.com access from this host currently fails
# (curl 28 timeouts and GnuTLS -110 resets); fall back to direct if needed.
git pull https://gh-proxy.com/https://github.com/jason8668/bike-callcenter.git main || git pull

echo "[2/6] Configure Zammad (language / channels / tokens)"
cd "$ZDIR"
CID=$(docker compose ps -q zammad-railsserver)
if [ -z "$CID" ]; then
  echo "ERROR: zammad-railsserver container is not running."
  exit 1
fi
docker cp "$APP_DIR/deploy/zammad_cn_setup.rb" "$CID:/tmp/zammad_cn_setup.rb"
OUT=$(docker compose exec -T zammad-railsserver bundle exec rails runner /tmp/zammad_cn_setup.rb)
echo "$OUT" | grep -v -E '^(API_TOKEN|CTI_TOKEN)=' || true
API_TOKEN=$(echo "$OUT" | grep '^API_TOKEN=' | head -1 | cut -d= -f2-)
CTI_TOKEN=$(echo "$OUT" | grep '^CTI_TOKEN=' | head -1 | cut -d= -f2-)
if [ -z "$API_TOKEN" ] || [ -z "$CTI_TOKEN" ]; then
  echo "ERROR: token creation failed, see output above."
  exit 1
fi

echo "[3/6] Save tokens into bridge .env (values are not shown)"
cd "$APP_DIR/middleware"
sed -i "s|^ZAMMAD_TOKEN=.*|ZAMMAD_TOKEN=$API_TOKEN|" .env
sed -i "s|^ZAMMAD_CTI_TOKEN=.*|ZAMMAD_CTI_TOKEN=$CTI_TOKEN|" .env
echo "tokens saved."

echo "[4/6] Create bicycle groups and ticket fields"
command -v python3 >/dev/null 2>&1 || apt-get install -y python3
ZAMMAD_URL=http://127.0.0.1:8899 ZAMMAD_TOKEN=$API_TOKEN \
  python3 "$APP_DIR/zammad-setup/setup_bike_fields.py"

echo "[5/6] Rebuild and restart bridge"
cd "$APP_DIR/deploy"
docker compose up -d --build bridge
sleep 3
curl -s http://127.0.0.1:8890/health && echo ""

echo "[6/6] Done"
echo "Zammad:    http://36.139.118.227:8899  (log out and back in, then hard-refresh)"
echo "Test page: http://36.139.118.227:8890/test"
echo "Reminder: the cloud security group must allow inbound TCP 8890 too."
echo "The test page asks for BRIDGE_SECRET once; it is in $APP_DIR/middleware/.env"
