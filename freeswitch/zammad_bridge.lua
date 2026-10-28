-- zammad_bridge.lua
-- FreeSWITCH Lua hook: pushes call events to the bridge middleware so the
-- agent board can pop the record (newCall/answer) in real time.
-- The record is opened on answer (so agents can take notes during the
-- call); only duration and recording path are filled in on hangup via
-- mod_json_cdr -> POST /cdr, so this script stays fast and never blocks
-- the call.
--
-- Install: the Docker image copies this file to
-- /usr/share/freeswitch/scripts/. The queue dialplan sets
-- bridge_event_url (host port 8890, reachable via host networking),
-- bridge_secret (from middleware/.env, injected by install-freeswitch.sh)
-- and bridge_ticket=1 before calling it; execute_on_answer runs it again
-- with "answer" when the queued call is bridged to an agent.

local bridge_url = session and session:getVariable("bridge_event_url")
  or "http://127.0.0.1:8890/events/call"
local bridge_secret = session and session:getVariable("bridge_secret") or ""

local event = argv and argv[1] or "newCall"

local function json_escape(s)
  return (tostring(s or ""):gsub('[%z\1-\31\\"]', function(c)
    if c == '"' then return '\\"' end
    if c == '\\' then return '\\\\' end
    return string.format('\\u%04x', c:byte())
  end))
end

if session then
  -- bridge_ticket is REQUIRED by the bridge: without it the answer
  -- event is ignored and no record opens during the call (the dialplan
  -- sets bridge_ticket=1; the payload must carry it, it is not read
  -- from channel variables by the bridge).
  local payload = string.format(
    '{"event":"%s","uuid":"%s","caller_id_number":"%s","destination_number":"%s",'
      .. '"bridge_ticket":"%s","direction":"%s","ivr_choice":"%s"}',
    json_escape(event),
    json_escape(session:getVariable("uuid")),
    json_escape(session:getVariable("caller_id_number")),
    json_escape(session:getVariable("destination_number")),
    json_escape(session:getVariable("bridge_ticket")),
    json_escape(session:getVariable("direction")),
    json_escape(session:getVariable("ivr_choice"))
  )

  local api = freeswitch.API()
  -- mod_curl based POST; fire and forget.
  local result = api:executeString("curl", string.format(
    "%s post %s header 'Content-Type: application/json' header 'X-Bridge-Secret: %s'",
    bridge_url, payload, bridge_secret))
  freeswitch.consoleLog("INFO", "zammad_bridge event=" .. event ..
    " uuid=" .. tostring(session:getVariable("uuid")) ..
    " result=" .. tostring(result) .. "\n")
end
