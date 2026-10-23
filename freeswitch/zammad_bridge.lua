-- zammad_bridge.lua
-- FreeSWITCH Lua hook: pushes call events to the bridge middleware so Zammad
-- can pop the caller card (newCall/answer) in real time.
-- The heavy work (ticket creation + recording upload) happens on hangup via
-- mod_xml_cdr -> POST /cdr, NOT here, so this script stays fast and never
-- blocks the call.
--
-- Install:
--   1) Copy to /usr/share/freeswitch/scripts/zammad_bridge.lua
--   2) In the inbound dialplan (FusionPBX: Dialplan Manager), BEFORE the
--      bridge/queue action add:
--        action lua zammad_bridge.lua newCall
--      and on the agent leg (after bridge) add:
--        action lua zammad_bridge.lua answer
--   3) Set the two channel variables below via FusionPBX "global" settings
--      or in vars.xml. The secret must match BRIDGE_SECRET in middleware/.env

local bridge_url = session and session:getVariable("bridge_event_url")
  or "http://127.0.0.1:8090/events/call"
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
  local payload = string.format(
    '{"event":"%s","uuid":"%s","caller_id_number":"%s","destination_number":"%s",'
      .. '"direction":"%s","ivr_choice":"%s"}',
    json_escape(event),
    json_escape(session:getVariable("uuid")),
    json_escape(session:getVariable("caller_id_number")),
    json_escape(session:getVariable("destination_number")),
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
