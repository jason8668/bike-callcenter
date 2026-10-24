#!/bin/bash
# Update the trial deployment from GitHub (run as root).
# Same pattern as the course-scheduling project: git pull, then rebuild.
set -e
APP_DIR="/opt/bike-callcenter"
cd "$APP_DIR"
echo "[1/3] git pull"
git pull
echo "[2/3] rebuild bridge"
cd "$APP_DIR/deploy"
docker compose up -d --build bridge
echo "[3/3] status"
curl -s http://127.0.0.1:8890/health && echo ""
docker ps --format "table {{.Names}}\t{{.Status}}" | head -20
echo "Update done. (Zammad stack itself is upgraded separately, see docs.)"
