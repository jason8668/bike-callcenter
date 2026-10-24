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
import urllib.parse
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

    # IVR digit -> ticket routing for the bicycle hotline.
    # "voicemail" is a pseudo choice set by the voicemail ingest path
    # (caller left a message because nobody answered / after hours).
    IVR_GROUP = {"1": "报修组", "2": "投诉组", "3": "咨询组", "4": "客服组",
                 "voicemail": "客服组"}
    IVR_TYPE = {"1": "故障报修", "2": "投诉建议", "3": "业务咨询", "4": "其他",
                "voicemail": "语音留言"}

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
            # callee (the dialled number) was added for the Excel export;
            # old databases simply gain the column, old rows keep "".
            try:
                c.execute("ALTER TABLE calls ADD COLUMN callee TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # column already exists
            # recording_attached tracks whether the hangup CDR already
            # attached the recording to the answer-time ticket, so a
            # duplicate CDR does not attach it twice.
            try:
                c.execute("ALTER TABLE calls ADD COLUMN recording_attached INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # column already exists

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def get_ticket_id(self, call_id: str) -> Optional[int]:
        with self._conn() as c:
            row = c.execute("SELECT ticket_id FROM calls WHERE call_id=?", (call_id,)).fetchone()
        return row[0] if row and row[0] else None

    def is_recording_attached(self, call_id: str) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT recording_attached FROM calls WHERE call_id=?",
                            (call_id,)).fetchone()
        return bool(row and row[0])

    def mark_recording_attached(self, call_id: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE calls SET recording_attached=1 WHERE call_id=?", (call_id,))

    def tickets_for_caller(self, phone: str, limit: int = 10) -> list[int]:
        """Ticket ids previously bound to this caller number, newest first."""
        if not phone:
            return []
        with self._conn() as c:
            rows = c.execute(
                "SELECT ticket_id FROM calls WHERE caller=? AND ticket_id IS NOT NULL "
                "ORDER BY rowid DESC LIMIT ?", (phone, limit)).fetchall()
        return [r[0] for r in rows]

    def record(self, rec: CallRecord, ticket_id: Optional[int]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO calls (call_id, ticket_id, caller, callee, direction, answered,"
                " duration_sec, wait_sec, start_epoch) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(call_id) DO UPDATE SET"
                " ticket_id=COALESCE(excluded.ticket_id, calls.ticket_id),"
                " callee=excluded.callee,"
                " answered=excluded.answered, duration_sec=excluded.duration_sec,"
                " wait_sec=excluded.wait_sec",
                (rec.call_id, ticket_id, rec.caller, rec.callee, rec.direction,
                 1 if rec.answered else 0, rec.duration_sec, rec.wait_sec, rec.start_epoch),
            )

    def list_calls(self, start_epoch: int = 0) -> list:
        """Call rows for the Excel export, oldest first:
        (call_id, caller, callee, direction, answered, duration_sec,
         wait_sec, start_epoch, ticket_id)."""
        with self._conn() as c:
            return c.execute(
                "SELECT call_id, caller, COALESCE(callee,''), direction, answered,"
                " duration_sec, wait_sec, start_epoch, ticket_id"
                " FROM calls WHERE start_epoch >= ? ORDER BY start_epoch",
                (start_epoch,),
            ).fetchall()

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
        parts.append("常见问题：" + "、".join(f"{k}×{v}" for k, v in ranked))
    if types:
        ranked = sorted(types.items(), key=lambda kv: -kv[1])
        parts.append("问题类型：" + "、".join(f"{k}×{v}" for k, v in ranked))
    if latest:
        parts.append(f"最近一次：{latest[1]}（{latest[0][:10]}）")
    return "；".join(parts)


# --------------------------------------------------------------- Zammad API

class ZammadClient:
    def __init__(self, base_url: str, token: str, transport: Transport = urllib_transport,
                 cti_token: str = "", ticket_customer_id: Optional[int] = None):
        self.base = base_url.rstrip("/")
        self.token = token
        self.cti_token = cti_token
        self.transport = transport
        # Explicit override for the user filed as customer on phone
        # tickets (env TICKET_CUSTOMER_ID); normally auto-resolved to
        # the first admin user by ticket_customer_id().
        self._configured_customer_id = ticket_customer_id
        # Last ticket-create failure, surfaced to the test page so a failed
        # simulate shows Zammad's real reason (status + message) instead of
        # a generic "create failed". Contains no credentials.
        self.last_ticket_error = ""

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

    # -- ticket customer ------------------------------------------------------
    # Phone tickets get NO customer account at all (operator ruling: only
    # 系统管理 and 客服坐席 accounts exist). Zammad still requires a
    # customer_id on every ticket, so phone tickets are filed under the
    # first admin user (the account that owns the bridge token) and the
    # real caller number lives on the ticket (title + article). Nothing
    # is ever created in the user list for a caller.

    def ticket_customer_id(self) -> Optional[int]:
        """User id filed as customer on phone tickets, cached after the
        first resolution: explicit TICKET_CUSTOMER_ID if configured,
        else the first non-system admin user found via the API."""
        if getattr(self, "_ticket_customer_id", None):
            return self._ticket_customer_id
        found = self._configured_customer_id or self._first_admin_id()
        if found:
            self._ticket_customer_id = found
        return found

    def _first_admin_id(self) -> Optional[int]:
        status, roles = self._call("GET", "/api/v1/roles")
        admin_id = None
        if status == 200 and isinstance(roles, list):
            for r in roles:
                if isinstance(r, dict) and r.get("name") == "Admin":
                    admin_id = r.get("id")
                    break
        if not admin_id:
            return None
        status, users = self._call("GET", "/api/v1/users?limit=100")
        if status == 200 and isinstance(users, list):
            for u in users:
                if not isinstance(u, dict) or u.get("id") == 1:
                    continue
                if admin_id in (u.get("role_ids") or []):
                    return u.get("id")
        return None

    # -- agent accounts -------------------------------------------------------
    def create_agent(self, login: str, password: str) -> tuple[Optional[int], str]:
        """Create an agent account from just a login + password (no
        first/last-name split): firstname=login, Agent role, full access
        to 客服组. Returns (user_id, error_message); error "exists" when
        the login is already taken."""
        status, users = self._call("GET", "/api/v1/users?limit=100")
        if status == 200 and isinstance(users, list):
            for u in users:
                if isinstance(u, dict) and u.get("login") == login:
                    return None, "exists"
        status, groups = self._call("GET", "/api/v1/groups")
        group_id = None
        if status == 200 and isinstance(groups, list):
            for g in groups:
                if isinstance(g, dict) and g.get("name") == "客服组":
                    group_id = g.get("id")
                    break
        if not group_id:
            return None, "客服组 not found"
        status, data = self._call("POST", "/api/v1/users", {
            "login": login, "firstname": login, "lastname": "",
            "password": password, "roles": ["Agent"],
            "group_ids": {str(group_id): "full"},
        })
        if status in (200, 201) and isinstance(data, dict) and data.get("id"):
            return data["id"], ""
        return None, f"HTTP {status}: {str(data)[:200]}"

    def get_ticket(self, ticket_id: int) -> Optional[dict]:
        """One ticket as Zammad returns it (custom attributes included)."""
        status, data = self._call("GET", f"/api/v1/tickets/{ticket_id}")
        if status == 200 and isinstance(data, dict):
            return data
        return None

    # -- tickets ---------------------------------------------------------------
    def create_ticket_with_recording(self, rec: CallRecord, customer_id: Optional[int],
                                     recording_bytes: Optional[bytes] = None,
                                     recording_filename: str = "",
                                     history_note: str = "",
                                     in_progress: bool = False) -> Optional[int]:
        """Ticket for a call. in_progress=True means the call is still
        going on (ticket opened at answer time): the article says so and
        the recording is attached later, when the CDR arrives."""
        dur = "进行中" if in_progress else f"{rec.duration_sec}s"
        article_body = (
            f"来电自动建单\n主叫: {rec.caller} 被叫: {rec.callee}\n"
            f"CallID: {rec.call_id}\n通话时长: {dur}\n"
            f"类型: {rec.ticket_type or '电话来电'}"
        )
        if in_progress:
            article_body += "\n通话进行中，坐席正在记录；录音将在通话结束后附上。"
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
            "caller_number": rec.caller,
            "call_id": rec.call_id,
            "recording_url": rec.recording_url,
            "article": article,
        }
        if rec.ticket_type:
            # Also stamp the 工单类型 attribute (agents can still change it);
            # voicemail tickets rely on it for the 语音留言 overview filter.
            payload["ticket_type"] = rec.ticket_type
        status, data = self._call("POST", "/api/v1/tickets", payload)
        if status in (200, 201) and isinstance(data, dict):
            self.last_ticket_error = ""
            return data.get("id")
        self.last_ticket_error = f"HTTP {status}: {str(data)[:300]}"
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

    # -- exports ---------------------------------------------------------------
    def search_tickets(self, query: str, page: int = 1, per_page: int = 100) -> list:
        """One page of tickets matching a Zammad search query, as dicts.

        Best-effort: any API error yields []. Tolerates id-only search
        results by resolving them individually."""
        q = urllib.parse.quote(query, safe="")
        status, data = self._call(
            "GET", f"/api/v1/tickets/search?query={q}&page={page}&per_page={per_page}")
        if status != 200 or not isinstance(data, list):
            return []
        tickets = []
        for item in data:
            if isinstance(item, dict):
                tickets.append(item)
            elif isinstance(item, int):
                s2, t2 = self._call("GET", f"/api/v1/tickets/{item}")
                if s2 == 200 and isinstance(t2, dict):
                    tickets.append(t2)
        return tickets

    def id_name_map(self, path: str) -> dict:
        """id -> name for /api/v1/ticket_states, /ticket_priorities, /groups."""
        status, data = self._call("GET", path)
        if status == 200 and isinstance(data, list):
            return {i.get("id"): i.get("name") for i in data
                    if isinstance(i, dict) and i.get("id") is not None}
        return {}

    def get_user_phone(self, user_id) -> str:
        """Best-effort phone lookup for the export's 客户电话 column."""
        if not user_id:
            return ""
        status, data = self._call("GET", f"/api/v1/users/{user_id}")
        if status == 200 and isinstance(data, dict):
            return str(data.get("phone") or "")
        return ""


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
        # The ticket must exist WHILE the agent is on the phone, so the
        # agent can take notes during the call instead of waiting for
        # hangup. On answer (inbound only), open the ticket now; the
        # recording is attached later, when the CDR arrives. A missed
        # call never sends answer, so it still creates no ticket.
        if event == "answer" and rec.direction == "in":
            existing = self.store.get_ticket_id(rec.call_id)
            if existing:
                return {"ok": True, "call_id": rec.call_id, "event": event,
                        "ticket_id": existing, "note": "already bound"}
            rec.answered = True
            ticket_id, _attached, history_note = self._bind_ticket(
                rec, in_progress=True)
            if ticket_id is None:
                # CTI already went out; surface the ticket error without
                # failing the event (the CDR path will retry creation).
                return {"ok": True, "call_id": rec.call_id, "event": event,
                        "ticket_error": self.zammad.last_ticket_error}
            self.store.record(rec, ticket_id)
            return {"ok": True, "call_id": rec.call_id, "event": event,
                    "ticket_id": ticket_id, "history_hint": history_note}
        return {"ok": True, "call_id": rec.call_id, "event": event}

    def _bind_ticket(self, rec: CallRecord, in_progress: bool = False) -> tuple[Optional[int], bool, str]:
        """Create the Zammad ticket for an answered contact (call or
        voicemail): recording attached, returning-caller keyword hint
        stamped on top. Returns (ticket_id, recording_attached, hint).
        in_progress=True opens the ticket at answer time (no recording
        yet); the CDR attaches the recording afterwards."""
        recording = self.recording_reader(rec)
        filename = os.path.basename(rec.recording_path) if rec.recording_path else ""
        customer_id = self.zammad.ticket_customer_id()
        # Returning caller: past tickets are found by caller NUMBER in
        # the bridge's own call log (not by customer account, since all
        # phone tickets share one customer), so the agent still sees this
        # number's usual problems on the new ticket.
        history_note = ""
        try:
            past = [self.zammad.get_ticket(tid)
                    for tid in self.store.tickets_for_caller(rec.caller)]
            history_note = summarize_history([t for t in past if t])
        except Exception:
            history_note = ""
        ticket_id = self.zammad.create_ticket_with_recording(
            rec, customer_id, recording, filename, history_note=history_note,
            in_progress=in_progress)
        return ticket_id, recording is not None, history_note

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
        # 3) idempotency: one CallID = one ticket. If the ticket was
        # already opened at answer time, the CDR only attaches the
        # recording to it (once); otherwise the ticket is created now,
        # recording included.
        existing = self.store.get_ticket_id(rec.call_id)
        if existing:
            if self.store.is_recording_attached(rec.call_id):
                self.store.record(rec, existing)
                return {"ok": True, "call_id": rec.call_id, "ticket_id": existing,
                        "note": "already bound"}
            recording = self.recording_reader(rec)
            filename = os.path.basename(rec.recording_path) if rec.recording_path else ""
            attached = self.zammad.add_recording_to_ticket(
                existing, rec, recording, filename)
            has_audio = recording is not None or bool(rec.recording_url)
            if attached and has_audio:
                self.store.mark_recording_attached(rec.call_id)
            self.store.record(rec, existing)
            return {"ok": True, "call_id": rec.call_id, "ticket_id": existing,
                    "recording_attached": attached,
                    "note": "recording attached to live ticket"}
        ticket_id, attached, history_note = self._bind_ticket(rec)
        if ticket_id is None:
            return {"ok": False, "call_id": rec.call_id, "error": "zammad ticket create failed",
                    "zammad_error": self.zammad.last_ticket_error}
        self.store.record(rec, ticket_id)
        if attached:
            self.store.mark_recording_attached(rec.call_id)
        return {"ok": True, "call_id": rec.call_id, "ticket_id": ticket_id,
                "recording_attached": attached, "history_hint": history_note}

    def handle_voicemail(self, payload: dict) -> dict:
        """A caller left a voicemail (nobody answered / after hours).

        The message becomes a ticket in 客服组 with type 语音留言 and the
        message recording attached, so agents work it from the 语音留言
        overview like the legacy system's voicemail list. Idempotent by
        CallID, same as calls."""
        rec = parse_cdr(payload)
        if not rec.call_id:
            return {"ok": False, "error": "missing call_id/uuid"}
        rec.ivr_choice = "voicemail"
        rec.answered = True  # a completed message is a handled contact
        existing = self.store.get_ticket_id(rec.call_id)
        if existing:
            return {"ok": True, "call_id": rec.call_id, "ticket_id": existing,
                    "note": "already bound", "voicemail": True}
        ticket_id, attached, history_note = self._bind_ticket(rec)
        if ticket_id is None:
            return {"ok": False, "call_id": rec.call_id, "error": "zammad ticket create failed",
                    "zammad_error": self.zammad.last_ticket_error}
        self.store.record(rec, ticket_id)
        return {"ok": True, "call_id": rec.call_id, "ticket_id": ticket_id,
                "recording_attached": attached, "history_hint": history_note,
                "voicemail": True}
