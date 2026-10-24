#!/bin/bash
# Install FreeSWITCH (internal test: extensions 8001-8003, queue 8000,
# voicemail 8009) on the bike call-center server. English output only.
# Safe to re-run: extension passwords and the event-socket password are
# kept when they already exist, so softphones keep working.
# Secrets are never printed: they live in freeswitch/extensions.txt and
# freeswitch/esl-password.txt (chmod 600) and in conf-live/ (gitignored).
set -e
cd /opt/bike-callcenter

echo "[1/6] Pulling latest code..."
git pull https://gh-proxy.com/https://github.com/jason8668/bike-callcenter.git main

echo "[2/6] Generating live config from templates..."
rm -rf freeswitch/conf-live
cp -r freeswitch/conf-templates freeswitch/conf-live
mkdir -p freeswitch/conf-live/directory/default

rand_hex() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 8;
  else tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 16; echo; fi
}

echo "[3/6] Extension passwords (reused when already set)..."
if [ ! -f freeswitch/extensions.txt ]; then
  : > freeswitch/extensions.txt
  for ext in 8001 8002 8003; do
    echo "$ext $(rand_hex)" >> freeswitch/extensions.txt
  done
fi
chmod 600 freeswitch/extensions.txt
while read -r ext pass; do
  [ -z "$ext" ] && continue
  cat > "freeswitch/conf-live/directory/default/$ext.xml" <<'USERXML'
<include>
  <user id="__EXT__">
    <params>
      <param name="password" value="__PASS__"/>
    </params>
    <variables>
      <variable name="toll_allow" value="domestic,international,local"/>
      <variable name="accountcode" value="__EXT__"/>
      <variable name="user_context" value="default"/>
      <variable name="effective_caller_id_name" value="__EXT__"/>
      <variable name="effective_caller_id_number" value="__EXT__"/>
      <variable name="outbound_caller_id_name" value="__EXT__"/>
      <variable name="outbound_caller_id_number" value="__EXT__"/>
    </variables>
  </user>
</include>
USERXML
  python3 - "$ext" "$pass" "freeswitch/conf-live/directory/default/$ext.xml" <<'PYEOF'
import sys
ext, pw, path = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(path).read().replace("__EXT__", ext).replace("__PASS__", pw)
open(path, "w").write(s)
PYEOF
done < freeswitch/extensions.txt

echo "[4/6] Bridge secret and event-socket password..."
if [ ! -f middleware/.env ]; then
  echo "ERROR: middleware/.env missing. Run deploy/apply-cn-setup.sh first."
  exit 1
fi
BRIDGE_SECRET=$(grep '^BRIDGE_SECRET=' middleware/.env | head -1 | cut -d= -f2- | tr -d '\r')
if [ -z "$BRIDGE_SECRET" ]; then
  echo "ERROR: BRIDGE_SECRET is empty in middleware/.env."
  exit 1
fi
if [ ! -f freeswitch/esl-password.txt ]; then
  rand_hex > freeswitch/esl-password.txt
fi
chmod 600 freeswitch/esl-password.txt
ESL_PASSWORD=$(cat freeswitch/esl-password.txt)
python3 - "$BRIDGE_SECRET" "$ESL_PASSWORD" <<'PYEOF'
import sys, glob
secret, esl = sys.argv[1], sys.argv[2]
for path in ["freeswitch/conf-live/dialplan/default.xml",
             "freeswitch/conf-live/json_cdr.conf.xml"]:
    s = open(path).read().replace("__BRIDGE_SECRET__", secret)
    open(path, "w").write(s)
p = "freeswitch/conf-live/event_socket.conf.xml"
s = open(p).read().replace("__ESL_PASSWORD__", esl)
open(p, "w").write(s)
PYEOF

echo "[5/6] Building and starting FreeSWITCH..."
cd deploy
docker compose up -d --build freeswitch

echo "[6/6] Waiting for FreeSWITCH to answer (up to 120s)..."
OK=""
for i in $(seq 1 24); do
  if docker compose exec -T freeswitch fs_cli -p "$ESL_PASSWORD" -x "status" 2>/dev/null | grep -q "UP"; then
    OK=1; break
  fi
  sleep 5
done
if [ -z "$OK" ]; then
  echo "ERROR: FreeSWITCH did not come up. Last log lines:"
  docker compose logs --tail 60 freeswitch
  exit 1
fi

echo ""
echo "Module check:"
for mod in mod_sofia mod_callcenter mod_json_cdr mod_lua mod_curl; do
  if docker compose exec -T freeswitch fs_cli -p "$ESL_PASSWORD" -x "show modules" 2>/dev/null | grep -q "$mod"; then
    echo "  $mod OK"
  else
    echo "  $mod MISSING (check the log above)"
  fi
done
echo ""
echo "SIP profile:"
docker compose exec -T freeswitch fs_cli -p "$ESL_PASSWORD" -x "sofia status profile internal" 2>/dev/null | head -20
echo ""
echo "Queue:"
docker compose exec -T freeswitch fs_cli -p "$ESL_PASSWORD" -x "callcenter_config queue list" 2>/dev/null
echo ""
echo "DONE. Next steps:"
echo "  1) Security group (cloud console): allow inbound UDP 5060 and UDP 16384-16884."
echo "  2) Softphone (MicroSIP / Zoiper / Linphone): server 36.139.118.227,"
echo "     user 8001..8003, password from /opt/bike-callcenter/freeswitch/extensions.txt,"
echo "     transport UDP, codecs PCMA/PCMU."
echo "  3) Test: 8001 calls 8002 (seat to seat), then 8001 calls 8000 (queue)."
echo "     The queue call must pop a ticket on the agent board during the call."
echo "  4) Voicemail test: dial 8009, speak, hang up, check the voicemail overview."
echo "  5) fs_cli any time: docker compose exec freeswitch fs_cli -p \$(cat /opt/bike-callcenter/freeswitch/esl-password.txt)"
echo "     Useful commands: status | show calls | show channels | sofia status"
