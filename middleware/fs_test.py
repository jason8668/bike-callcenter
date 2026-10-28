"""FreeSWITCH-connected test-call helpers (software-only telephony test).

The old /simulate/* endpoints post fake events straight to the bridge,
so they never touch FreeSWITCH: no queue, no dialplan, no recording,
no CDR. These helpers drive the REAL telephony layer from the bridge
over ESL instead, so the test exercises the same chain a real call
takes (queue -> agent bridge -> record-template -> CDR -> record),
without needing the client's lines or handsets:

  1. The bridge writes two freshly generated tone WAVs into the shared
     recordings volume (caller tones 400-700 Hz, agent tones 900-1400
     Hz, different random pattern every call, so a recording that
     contains them proves media really flowed).
  2. For queue tests the bridge adds a temporary test agent to the
     queue whose contact is loopback/8999: an auto-answer extension
     (answer + sleep) defined in the dialplan templates. The queue
     bridges the test caller to it exactly as it would a real seat.
     The agent is removed again on hangup / cleanup, so it never
     pollutes the production queue.
  3. The caller leg is originated unanswered into the queue with
     loopback/8000/default: while it waits it is NOT answered, so the
     queue extension's execute_on_answer fires only when an agent
     bridges it -- the same answer-time pop a real call produces.
     The originate b-leg &playback plays the caller tones once the
     leg answers (agent bridge, or voicemail answer on timeout).
     originate_timeout=180 gives the call room to pass the 60s queue
     timeout into voicemail 8009 when nobody answers.
  4. Mid-call audio injection uses uuid_broadcast, which plays a file
     into a live (even bridged) call without disturbing the bridge.
  5. Hangup uses uuid_kill. Every ESL reply is returned verbatim to
     the test page, so a wrong command or a down ESL link is visible
     instead of hidden behind a generic error.

Everything here is pure logic over an injected ``esl`` callable
(esl(command) -> reply text or None), so unit tests run without a
FreeSWITCH instance. The FastAPI endpoints in app.py pass the real
bridge_core.esl_command.
"""

import math
import os
import random
import struct
import wave
from typing import Callable, Optional

TEST_AGENT_NAME = "testagent"
TEST_AGENT_CONTACT = "loopback/8999"
TEST_QUEUE = "客服队列"

# Paths as FreeSWITCH sees them (shared /recordings volume). The bridge
# writes the same basenames into RECORDING_DIR; in the deployed
# container RECORDING_DIR is /recordings, so both sides agree.
FS_CALLER_AUDIO = "/recordings/test-caller.wav"
FS_AGENT_AUDIO = "/recordings/test-agent.wav"


def generate_tone_wav(path: str, seed: int, freq_lo: int, freq_hi: int,
                      seconds: int = 15) -> dict:
    """Write an 8 kHz mono 16-bit WAV of random tone bursts.

    Deterministic for a given seed (same seed -> same bytes), different
    across seeds, so each test call's recording carries a unique audio
    fingerprint. Returns {"path", "seconds", "tones"}.
    """
    rng = random.Random(seed)
    rate = 8000
    frames = bytearray()
    tones = []
    remaining = seconds
    while remaining > 0:
        tone_len = min(rng.uniform(0.8, 1.5), remaining)
        freq = rng.randint(freq_lo, freq_hi)
        tones.append(round(freq))
        n = int(tone_len * rate)
        for i in range(n):
            sample = int(12000 * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<h", sample)
        gap = min(0.2, remaining - tone_len)
        if gap > 0:
            frames += struct.pack("<h", 0) * int(gap * rate)
            remaining -= gap
        remaining -= tone_len
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return {"path": path, "seconds": seconds, "tones": tones}


def build_originate_command(caller: str, target: str) -> str:
    """ESL originate string for a test call.

    target: "queue" (test agent answers), "queue-noagent" (nobody
    answers: the call waits, then the queue times out into voicemail),
    or "voicemail" (straight to 8009). The caller leg is
    loopback/<exten>/default, unanswered while it waits, so queue
    answer-pop and voicemail-timeout semantics match a real inbound
    call. Raises ValueError on a bad caller number.
    """
    caller = (caller or "").strip()
    if not caller or not all(c.isdigit() or c == "+" for c in caller):
        raise ValueError("caller must be a phone number (digits, optional +)")
    if target not in ("queue", "queue-noagent", "voicemail"):
        raise ValueError("target must be queue, queue-noagent or voicemail")
    exten = "8009" if target == "voicemail" else "8000"
    return (
        "originate {origination_caller_id_number=" + caller +
        ",origination_caller_id_name=" + caller +
        ",originate_timeout=180}loopback/" + exten + "/default " +
        "&playback(" + FS_CALLER_AUDIO + ")"
    )


def build_broadcast_command(uuid: str, which: str) -> str:
    """uuid_broadcast string: play a test tone file into a live call."""
    path = FS_AGENT_AUDIO if which == "agent" else FS_CALLER_AUDIO
    return "uuid_broadcast " + uuid + " " + path + " both"


def parse_originate_reply(reply: Optional[str]) -> Optional[str]:
    """Extract the call UUID from an originate reply (+OK <uuid>)."""
    if not reply:
        return None
    reply = reply.strip()
    if reply.startswith("+OK"):
        parts = reply[3:].strip().split()
        return parts[0] if parts else None
    return None


def parse_exists_reply(reply: Optional[str]) -> bool:
    return bool(reply) and "true" in reply.lower()


def ensure_test_agent(esl: Callable[[str], Optional[str]]) -> list:
    """Add the loopback test agent to the queue if not already there.

    Returns [{"command", "reply"}] for every step, with None replies
    rendered as "ESL 无响应" by callers. Never raises: a failed step is
    reported, and the originate below will surface the consequence.
    """
    steps = []
    reply = esl("callcenter_config agent list")
    steps.append({"command": "callcenter_config agent list",
                  "reply": reply if reply is not None else "ESL 无响应"})
    if reply is None:
        return steps
    if TEST_AGENT_NAME in reply:
        return steps
    for cmd in (
        "callcenter_config agent add " + TEST_QUEUE + " " +
        TEST_AGENT_NAME + " " + TEST_AGENT_CONTACT,
        "callcenter_config tier add " + TEST_QUEUE + " " +
        TEST_AGENT_NAME + " 1 1",
        "callcenter_config agent set status " + TEST_AGENT_NAME +
        " Available",
    ):
        reply = esl(cmd)
        steps.append({"command": cmd,
                      "reply": reply if reply is not None else "ESL 无响应"})
    return steps


def remove_test_agent(esl: Callable[[str], Optional[str]]) -> list:
    """Remove the test agent from the queue (best effort, never raises)."""
    steps = []
    for cmd in (
        "callcenter_config tier del " + TEST_QUEUE + " " + TEST_AGENT_NAME,
        "callcenter_config agent del " + TEST_AGENT_NAME,
    ):
        try:
            reply = esl(cmd)
        except Exception:
            reply = None
        steps.append({"command": cmd,
                      "reply": reply if reply is not None else "ESL 无响应"})
    return steps


def place_fs_call(esl: Callable[[str], Optional[str]], caller: str,
                  target: str, recording_dir: str) -> dict:
    """Generate audio, ensure the test agent, originate the test call.

    Returns {"ok", "uuid" | None, "error" | None, "steps", "warning"}.
    ``steps`` carries every ESL command + verbatim reply so the test
    page can show exactly what FreeSWITCH did.
    """
    import time
    seed = int(time.time()) % 1000000
    generate_tone_wav(os.path.join(recording_dir, "test-caller.wav"),
                      seed, 400, 700)
    generate_tone_wav(os.path.join(recording_dir, "test-agent.wav"),
                      seed + 1, 900, 1400)
    steps: list = []
    if target == "queue":
        steps += ensure_test_agent(esl)
        if any(s["reply"] == "ESL 无响应" for s in steps):
            return {"ok": False, "uuid": None,
                    "error": "话务层未连接（ESL 无响应）：FreeSWITCH 可能未运行，"
                             "或桥接连不上 127.0.0.1:8021。",
                    "steps": steps, "warning": None}
    try:
        cmd = build_originate_command(caller, target)
    except ValueError as e:
        return {"ok": False, "uuid": None, "error": str(e),
                "steps": steps, "warning": None}
    reply = esl(cmd)
    steps.append({"command": cmd,
                  "reply": reply if reply is not None else "ESL 无响应"})
    uuid = parse_originate_reply(reply)
    warning = None
    if recording_dir != "/recordings":
        warning = ("桥接录音目录是 " + recording_dir + "，不是 /recordings："
                   "FS 播放路径 " + FS_CALLER_AUDIO + " 可能找不到文件，"
                   "部署时桥接录音目录必须是 /recordings。")
    if not uuid:
        return {"ok": False, "uuid": None,
                "error": "FreeSWITCH 未接受 originate：" + (reply or "ESL 无响应"),
                "steps": steps, "warning": warning}
    return {"ok": True, "uuid": uuid, "error": None,
            "steps": steps, "warning": warning}


def hangup_fs_call(esl: Callable[[str], Optional[str]], uuid: str) -> dict:
    """Kill the test call and remove the test agent. Never raises."""
    steps = []
    reply = esl("uuid_kill " + uuid)
    steps.append({"command": "uuid_kill " + uuid,
                  "reply": reply if reply is not None else "ESL 无响应"})
    steps += remove_test_agent(esl)
    return {"ok": True, "steps": steps}


def play_fs_audio(esl: Callable[[str], Optional[str]], uuid: str,
                  which: str) -> dict:
    """Broadcast a test tone file into the live call (both legs)."""
    cmd = build_broadcast_command(uuid, which)
    reply = esl(cmd)
    if reply is None:
        return {"ok": False, "error": "话务层未连接（ESL 无响应）",
                "steps": [{"command": cmd, "reply": "ESL 无响应"}]}
    return {"ok": True, "error": None,
            "steps": [{"command": cmd, "reply": reply}]}


def call_status(esl: Callable[[str], Optional[str]], uuid: str) -> dict:
    """True while the test call leg still exists in FreeSWITCH."""
    reply = esl("uuid_exists " + uuid)
    if reply is None:
        return {"up": False, "error": "话务层未连接（ESL 无响应）",
                "reply": "ESL 无响应"}
    return {"up": parse_exists_reply(reply), "error": None, "reply": reply}
