-- zammad_voicemail.lua
-- Hangup hook for the voicemail extension: posts the finished message
-- to the bridge so it becomes a voicemail ticket (type=voicemail,
-- group=customer-service group). Runs via api_hangup_hook, so the
-- session carries the finished call's variables (billsec, duration,
-- start_epoch). Same mod_curl POST pattern as zammad_bridge.lua.
-- The recording file is ${uuid}.wav in the shared /recordings volume.

local voicemail_url = session and session:getVariable("voicemail_url")
  or "http://127.0.0.1:8890/events/voicemail"
local bridge_secret = session and session:getVariable("bridge_secret") or ""

local function json_escape(s)
  return (tostring(s or ""):gsub('[%z\1-\31\\"]', function(c)
    if c == '"' then return '\\"' end
    if c == '\\' then return '\\\\' end
    return string.format('\\u%04x', c:byte())
  end))
end

if session then
  local uuid = session:getVariable("uuid")
  local payload = string.format(
    '{"uuid":"%s","caller_id_number":"%s","destination_number":"%s",'
      .. '"direction":"inbound","billsec":"%s","duration":"%s",'
      .. '"start_epoch":"%s","recording_path":"%s.wav"}',
    json_escape(uuid),
    json_escape(session:getVariable("caller_id_number")),
    json_escape(session:getVariable("destination_number")),
    json_escape(session:getVariable("billsec")),
    json_escape(session:getVariable("duration")),
    json_escape(session:getVariable("start_epoch")),
    json_escape(uuid)
  )

  local api = freeswitch.API()
  local result = api:executeString("curl", string.format(
    "%s post %s header 'Content-Type: application/json' header 'X-Bridge-Secret: %s'",
    voicemail_url, payload, bridge_secret))
  freeswitch.consoleLog("INFO", "zammad_voicemail uuid=" .. tostring(uuid) ..
    " result=" .. tostring(result) .. "\n")
end
