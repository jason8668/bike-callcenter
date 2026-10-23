"""Bridge service entrypoint (FastAPI).

Run: uvicorn app:app --host 0.0.0.0 --port 8090
Env: see .env.example. All secrets live in .env only, never in Git.
"""
import os
import urllib.request

from fastapi import FastAPI, Header, HTTPException, Request

from bridge_core import Bridge, CallStore, ZammadClient, parse_cdr

ZAMMAD_URL = os.environ.get("ZAMMAD_URL", "http://zammad-nginx:8080")
ZAMMAD_TOKEN = os.environ.get("ZAMMAD_TOKEN", "")
ZAMMAD_CTI_TOKEN = os.environ.get("ZAMMAD_CTI_TOKEN", "")
BRIDGE_SECRET = os.environ.get("BRIDGE_SECRET", "")
RECORDING_DIR = os.environ.get("RECORDING_DIR", "/recordings")
DB_PATH = os.environ.get("BRIDGE_DB", "/data/bridge.db")


def read_recording(rec) -> bytes | None:
    """Recording bytes from the shared recordings volume, or via HTTP URL."""
    if rec.recording_path:
        candidate = rec.recording_path
        if not os.path.isabs(candidate):
            candidate = os.path.join(RECORDING_DIR, candidate)
        # recording_path may be the FreeSWITCH-host absolute path; also try by name
        for path in (candidate, os.path.join(RECORDING_DIR, os.path.basename(candidate))):
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    return f.read()
    if rec.recording_url.startswith("http"):
        try:
            with urllib.request.urlopen(rec.recording_url, timeout=20) as r:
                return r.read()
        except Exception:
            return None
    return None


bridge = Bridge(
    zammad=ZammadClient(ZAMMAD_URL, ZAMMAD_TOKEN, cti_token=ZAMMAD_CTI_TOKEN),
    store=CallStore(DB_PATH),
    recording_reader=read_recording,
)

app = FastAPI(title="bike-callcenter bridge", version="1.0.0")


def check_secret(x_bridge_secret: str | None):
    if BRIDGE_SECRET and x_bridge_secret != BRIDGE_SECRET:
        raise HTTPException(status_code=401, detail="bad bridge secret")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/events/call")
async def call_event(request: Request, x_bridge_secret: str | None = Header(default=None)):
    check_secret(x_bridge_secret)
    payload = await request.json()
    event = payload.get("event", "newCall")
    return bridge.handle_event(event, payload)


@app.post("/cdr")
async def cdr(request: Request, x_bridge_secret: str | None = Header(default=None)):
    check_secret(x_bridge_secret)
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        payload = await request.json()
    else:  # mod_xml_cdr can post form fields
        form = await request.form()
        payload = dict(form)
    return bridge.handle_cdr(payload)


@app.get("/stats/daily")
def daily(date: str):
    return bridge.store.daily_stats(date)


@app.post("/simulate/call")
async def simulate(x_bridge_secret: str | None = Header(default=None)):
    """Trial helper: one fake answered call, end to end, no real phone line.

    Creates a 1-second silent WAV in the recordings dir, pushes newCall +
    answer events, then posts a CDR so a Zammad ticket with the recording
    attached should appear. Safe to run repeatedly (unique call id each run).
    """
    check_secret(x_bridge_secret)
    import struct
    import time
    import uuid as uuidlib
    import wave

    call_id = str(uuidlib.uuid4())
    filename = f"{call_id}.wav"
    path = os.path.join(RECORDING_DIR, filename)
    os.makedirs(RECORDING_DIR, exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(struct.pack("<h", 0) for _ in range(8000)))
    base = {"uuid": call_id, "caller_id_number": "13800000000",
            "destination_number": "4008000000", "direction": "inbound",
            "ivr_choice": "1", "start_epoch": int(time.time())}
    bridge.handle_event("newCall", base)
    bridge.handle_event("answer", base)
    return bridge.handle_cdr({**base, "billsec": "65", "duration": "75",
                              "recording_path": filename})
