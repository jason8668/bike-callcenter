-- zammad_agent_status.lua
-- Seat busy control from the agent's own softphone: dial *51 to go
-- On Break (the queue and voicemail dispatch skip the seat), *52 to
-- come back to Available. The calling seat is the agent: the script
-- uses the caller's own number, so no seat can change another's status.
-- Same toggle exists on the agent board page (/board).

local mode = argv and argv[1] or ""
local target = (mode == "busy") and "On Break" or "Available"

if session then
  local agent = session:getVariable("caller_id_number")
  local api = freeswitch.API()
  local result = api:executeString("callcenter_config",
    "agent set status " .. tostring(agent) .. " '" .. target .. "'")
  freeswitch.consoleLog("INFO", "bridge_agent_status agent=" ..
    tostring(agent) .. " target=" .. target ..
    " result=" .. tostring(result) .. "\n")
end
