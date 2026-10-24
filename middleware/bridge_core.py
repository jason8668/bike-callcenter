"""Core bridge logic: FreeSWITCH/FusionPBX call data -> Zammad tickets.

Pure standard-library code (no third-party imports) so the logic can be
unit-tested anywhere. HTTP to Zammad goes through an injectable transport
callable: transport(method, url, headers, body_bytes) -> (status, bytes).

Key invariants:
- One FreeSWITCH uuid (CallID) maps to at most one Zammad ticket.
- Missed calls (never answered) do NOT create tickets, only statistics.
- Recording binding is best-effort with a URL fallback.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Callable, Optional

Transport = Callable[[str, str, dict, Optional[bytes]], tuple[int, bytes]]


def urllib_transport(method: str, url: str, headers: dict, body: Optional[bytes]) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ---------------------------------------------------------------- CDR parsing

@dataclass
class CallRecord:
    call_id: str
    caller: str = ""
    callee: str = ""
    direction: str = "in"          # "in" | "out"
    answered: bool = False
    duration_sec: int = 0
    wait_sec: int = 0
    start_epoch: int = 0
    recording_path: str = ""       # path on the FreeSWITCH host, if any
    recording_url: str = ""        # http(s) URL of the recording, if any
    ivr_choice: str = ""           # DTMF picked in the IVR, e.g. "1"
    agent: str = ""                # agent extension / number, if known
    hangup_cause: str = ""

    # IVR digit -> ticket routing for the bicycle hotline
    IVR_GROUP = {"1": "报修组", "2": "投诉组", "3": "咨询组", "4": "客服组"}
    IVR_TYPE = {"1": "故障报修", "2": "投诉建议", "3": "业务咨询", "4": "其他"}

    @property
    def group_name(self) -> str:
        return self.IVR_GROUP.get(self.ivr_choice, "客服组")

    @property
    def ticket_type(self) -> str:
        return self.IVR_TYPE.get(self.ivr_choice, "")


def _to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def parse_cdr(payload: dict) -> CallRecord:
    """Parse a CDR dict coming from mod_xml_cdr (JSON body or flattened form
    fields) or from FusionPBX's xml_cdr table export. Tolerant by design:
    unknown keys are ignored, missing keys fall back to empty/zero."""
    p = payload or {}
    # mod_xml_cdr JSON nests variables under "variables"; form posts are flat.
    variables = p.get("variables") if isinstance(p.get("variables"), dict) else {}
    merged = {**p, **variables}

    def g(*names):
        for n in names:
            if merged.get(n) not in (None, ""):
                return merged[n]
        return ""

    billsec = _to_int(g("billsec", "duration_sec"))
    duration = _to_int(g("duration", "duration_sec"), billsec)
    start = _to_int(g("start_epoch", "start_time"))
    direction_raw = str(g("direction", "call_direction")).lower()
    direction = "out" if direction_raw.startswith("out") else "in"
    return CallRecord(
        call_id=str(g("uuid", "call_id", "xml_cdr_uuid", "uniqueid")),
        caller=str(g("caller_id_number", "from", "caller", "sip_from_user")),
        callee=str(g("destination_number", "to", "callee", "sip_to_user")),
        direction=direction,
        answered=billsec > 0 or str(g("answered", "bridge_hangup")).lower() == "true",
        duration_sec=billsec or duration,
        wait_sec=max(duration - billsec, 0),
        start_epoch=start,
        recording_path=str(g("record_path", "recording_path", "record_name")),
        recording_url=str(g("recording_url", "record_url")),
        ivr_choice=str(g("ivr_choice", "dtmf", "menu_choice")),
        agent=str(g("agent", "cc_agent", "answering_number")),
        hangup_cause=str(g("hangup_cause")),
    )


# ------------------------------------------------------------------ storage

class CallStore:
    """SQLite mapping call_id -> ticket_id, plus per-call statistics rows."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS calls ("
                " call_id TEXT PRIMARY KEY, ticket_id INTEGER, caller TEXT,"
                " direction TEXT, answered INTEGER, duration_sec INTEGER,"
                " wait_sec INTEGER, start_epoch INTEGER, created_at TEXT"
                " DEFAULT (datetime('now')))"
            )

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def get_ticket_id(self, call_id: str) -> Optional[int]:
        with self._conn() as c:
            row = c.execute("SELECT ticket_id FROM calls WHERE call_id=?", (call_id,)).fetchone()
        return row[0] if row and row[0] else None

    def record(self, rec: CallRecord, ticket_id: Optional[int]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO calls (call_id, ticket_id, caller, direction, answered,"
                " duration_sec, wait_sec, start_epoch) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(call_id) DO UPDATE SET"
                " ticket_id=COALESCE(excluded.ticket_id, calls.ticket_id),"
                " answered=excluded.answered, duration_sec=excluded.duration_sec,"
                " wait_sec=excluded.wait_sec",
                (rec.call_id, ticket_id, rec.caller, rec.direction,
                 1 if rec.answered else 0, rec.duration_sec, rec.wait_sec, rec.start_epoch),
            )

    def daily_stats(self, date_prefix: str) -> dict:
        """Aggregate calls whose start falls on date_prefix (YYYY-MM-DD, UTC)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT answered, duration_sec, wait_sec FROM calls"
                " WHERE date(start_epoch, 'unixepoch') = ?",
                (date_prefix,),
            ).fetchall()
        total = len(rows)
        answered = sum(r[0] for r in rows)
        return {
            "date": date_prefix,
            "calls_total": total,
            "calls_answered": answered,
            "calls_abandoned": total - answered,
            "answer_rate": round(answered / total, 4) if total else 0.0,
            "avg_talk_sec": round(sum(r[1] for r in rows) / answered, 1) if answered else 0.0,
            "avg_wait_sec": round(sum(r[2] for r in rows) / total, 1) if total else 0.0,
        }


def summarize_history(tickets: list) -> str:
    """Keyword hint built from a returning customer's past tickets.

    Counts the fault types / problem types seen before and names the most
    recent issue, e.g. for the agent answering a repeat call:
    "该客户历史来电 3 次；故障关键词：锁故障×2、掉链×1；最近一次：故障报修（2026-10-20）"
    Returns "" when there is no usable history.
    """
    faults: dict = {}
    types: dict = {}
    latest = None  # (created_at, label)
    usable = 0
    for t in tickets or []:
        if not isinstance(t, dict):
            continue
        usable += 1
        fault = str(t.get("fault_type") or "").strip()
        ttype = str(t.get("ticket_type") or "").strip()
        if fault:
            faults[fault] = faults.get(fault, 0) + 1
        if ttype:
            types[ttype] = types.get(ttype, 0) + 1
        label = ttype or fault
        created = str(t.get("created_at") or "")
        if label and (latest is None or created > latest[0]):
            latest = (created, label)
    if not usable:
        return ""
    parts = [f"该客户历史来电 {usable} 次"]
    if faults:
        ranked = sorted(faults.items(), key=lambda kv: -kv[1])
        parts.append("故障关键词：" + "、".join(f"{k}×{v}" for k, v in ranked))
    if types:
        ranked = sorted(types.items(), key=lambda kv: -kv[1])
        parts.append("问题类型：" + "、".join(f"{k}×{v}" for k, v in ranked))
    if latest:
        parts.append(f"最近一次：{latest[1]}（{latest[0][:10]}）")
    return "；".join(parts)


# --------------------------------------------------------------- Zammad API

class ZammadClient:
    def __init__(self, base_url: str, token: str, transport: Transport = urllib_transport,
                 cti_token: str = ""):
        self.base = base_url.rstrip("/")
        self.token = token
        self.cti_token = cti_token
        self.transport = transport

    def _headers(self, content_type="application/json"):
        return {"Authorization": f"Token token={self.token}", "Content-Type": content_type}

    def _call(self, method: str, path: str, payload: Optional[dict] = None) -> tuple[int, object]:
        body = json.dumps(payload).encode() if payload is not None else None
        status, raw = self.transport(method, self.base + path, self._headers(), body)
        try:
            return status, json.loads(raw) if raw else {}
        except ValueError:
            return status, {"raw": raw.decode(errors="replace")}

    # -- CTI: push call events so Zammad pops the caller card & logs the call
    def push_cti_event(self, event: str, rec: CallRecord) -> tuple[int, object]:
        if not self.cti_token:
            return 0, {"skipped": "no CTI token configured"}
        payload = {
            "event": event, "from": rec.caller, "to": rec.callee,
            "direction": rec.direction, "callId": rec.call_id,
        }
        if rec.agent:
            payload["answeringNumber"] = rec.agent
        body = json.dumps(payload).encode()
        return self.transport(
            "POST", f"{self.base}/api/v1/cti/{self.cti_token}",
            {"Content-Type": "application/json"}, body,
        )[0], {}

    # -- customers ------------------------------------------------------------
    def find_or_create_customer(self, phone: str) -> Optional[int]:
        if not phone:
            return None
        status, data = self._call("GET", f"/api/v1/users/search?query=phone:{phone}")
        if status == 200 and isinstance(data, list) and data:
            return data[0].get("id")
        status, data = self._call("POST", "/api/v1/users", {
            "firstname": phone, "lastname": "(电话客户)", "phone": phone,
            "roles": ["Customer"],
        })
        if status in (200, 201) and isinstance(data, dict):
            return data.get("id")
        return None

    def customer_history(self, customer_id: Optional[int], limit: int = 10) -> list:
        """Recent tickets of this customer, for the history/keyword hint.

        Best-effort: any API error yields []. Zammad's ticket search may
        return full ticket objects or just ids depending on version, so
        ids are resolved individually (capped)."""
        if not customer_id:
            return []
        status, data = self._call(
            "GET", f"/api/v1/tickets/search?query=customer_id:{customer_id}&limit={limit}")
        if status != 200 or not isinstance(data, list):
            return []
        tickets = []
        for item in data:
            if isinstance(item, dict):
                tickets.append(item)
            elif isinstance(item, int) and len(tickets) < 5:
                s2, t2 = self._call("GET", f"/api/v1/tickets/{item}")
                if s2 == 200 and isinstance(t2, dict):
                    tickets.append(t2)
        return tickets

    # -- tickets ---------------------------------------------------------------
    def create_ticket_with_recording(self, rec: CallRecord, customer_id: Optional[int],
                                     recording_bytes: Optional[bytes] = None,
                                     recording_filename: str = "",
                                     history_note: str = "") -> Optional[int]:
        article_body = (
            f"来电自动建单\n主叫: {rec.caller} 被叫: {rec.callee}\n"
            f"CallID: {rec.call_id}\n通话时长: {rec.duration_sec}s\n"
            f"类型: {rec.ticket_type or '电话来电'}"
        )
        if history_note:
            article_body += f"\n历史提示: {history_note}"
        article: dict = {
            "subject": "通话记录", "body": article_body,
            "type": "phone", "internal": False,
        }
        if recording_bytes:
            article["attachments"] = [{
                "filename": recording_filename or f"{rec.call_id}.wav",
                "data": base64.b64encode(recording_bytes).decode(),
                "mime-type": "audio/wav",
            }]
        elif rec.recording_url:
            article["body"] += f"\n录音地址: {rec.recording_url}"
        payload = {
            "title": f"电话工单 {rec.caller} {rec.ticket_type}".strip(),
            "group": rec.group_name,
            "customer_id": customer_id,
            "call_id": rec.call_id,
            "recording_url": rec.recording_url,
            "article": article,
        }
        status, data = self._call("POST", "/api/v1/tickets", payload)
        if status in (200, 201) and isinstance(data, dict):
            return data.get("id")
        return None

    def add_recording_to_ticket(self, ticket_id: int, rec: CallRecord,
                                recording_bytes: Optional[bytes],
                                recording_filename: str = "") -> bool:
        article: dict = {
            "ticket_id": ticket_id, "subject": "通话录音",
            "body": f"CallID: {rec.call_id} 的通话录音见附件。" if recording_bytes
                    else f"CallID: {rec.call_id}\n录音地址: {rec.recording_url}",
            "type": "note", "internal": True,
        }
        if recording_bytes:
            article["attachments"] = [{
                "filename": recording_filename or f"{rec.call_id}.wav",
                "data": base64.b64encode(recording_bytes).decode(),
                "mime-type": "audio/wav",
            }]
        status, _ = self._call("POST", "/api/v1/ticket_articles", article)
        return status in (200, 201)


# --------------------------------------------------------------- orchestrator

@dataclass
class Bridge:
    zammad: ZammadClient
    store: CallStore
    recording_reader: Callable[[CallRecord], Optional[bytes]] = field(
        default=lambda rec: None)  # app.py injects a filesystem/HTTP reader

    def handle_event(self, event: str, payload: dict) -> dict:
        rec = parse_cdr(payload)
        if not rec.call_id:
            return {"ok": False, "error": "missing call_id/uuid"}
        self.zammad.push_cti_event(event, rec)
        return {"ok": True, "call_id": rec.call_id, "event": event}

    def handle_cdr(self, payload: dict) -> dict:
        rec = parse_cdr(payload)
        if not rec.call_id:
            return {"ok": False, "error": "missing call_id/uuid"}
        # 1) close the CTI log entry
        self.zammad.push_cti_event("hangup", rec)
        # 2) missed call -> statistics only, no ticket
        if not rec.answered:
            self.store.record(rec, None)
            return {"ok": True, "call_id": rec.call_id, "ticket_id": None,
                    "note": "missed call, no ticket created"}
        # 3) idempotency: one CallID = one ticket
        existing = self.store.get_ticket_id(rec.call_id)
        if existing:
            return {"ok": True, "call_id": rec.call_id, "ticket_id": existing,
                    "note": "already bound"}
        recording = self.recording_reader(rec)
        filename = os.path.basename(rec.recording_path) if rec.recording_path else ""
        customer_id = self.zammad.find_or_create_customer(rec.caller)
        # Returning caller: summarize past tickets into a keyword hint so
        # the agent sees this customer's usual problems on the new ticket.
        history_note = ""
        try:
            history_note = summarize_history(self.zammad.customer_history(customer_id))
        except Exception:
            history_note = ""
        ticket_id = self.zammad.create_ticket_with_recording(
            rec, customer_id, recording, filename, history_note=history_note)
        if ticket_id is None:
            return {"ok": False, "call_id": rec.call_id, "error": "zammad ticket create failed"}
        self.store.record(rec, ticket_id)
        return {"ok": True, "call_id": rec.call_id, "ticket_id": ticket_id,
                "recording_attached": recording is not None,
                "history_hint": history_note}
