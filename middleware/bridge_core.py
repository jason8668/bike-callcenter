"""Core record logic: FreeSWITCH call data -> call records.

Pure standard-library code (no third-party imports) so the logic can be
unit-tested anywhere. This is a RECORD system, not a ticket system:
there is no workflow, no dispatch queue, no ticket states. Each
answered, missed, or voicemail contact becomes one record row:
caller, time, duration, agent, question notes, service type, and the
recording file. Agents fill in the question during or after the call.

Key invariants:
- One FreeSWITCH uuid (CallID) maps to at most one record row.
- Internal extension-to-extension legs and outbound legs never become
  records (statistics rows only); only hotline queue legs do.
- A missed queue call IS kept as a record (kind 未接）, so callbacks
  are visible; it just never counts as an answered contact.
"""
from __future__ import annotations

import hashlib
import os
import queue
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional

def esl_command(command: str) -> Optional[str]:
    """One FreeSWITCH event-socket API call (stdlib socket, no deps).
    The socket binds 127.0.0.1 on the host and the bridge runs with host
    networking, so it connects to 127.0.0.1:8021 directly. (A container
    on the Docker bridge cannot reach a loopback listener via
    host.docker.internal: the gateway IP is not loopback, so the
    connection is refused even though FreeSWITCH is up.) The password
    comes from ESL_PASSWORD, else the server-side
    freeswitch/esl-password.txt mounted read-only at /freeswitch-config
    (never in Git). Any failure (no socket, bad auth, timeout) yields
    None so callers fall back."""
    import socket
    password = os.environ.get("ESL_PASSWORD", "")
    if not password:
        try:
            with open("/freeswitch-config/esl-password.txt") as f:
                password = f.read().strip()
        except OSError:
            password = ""
    if not password:
        return None
    host = os.environ.get("ESL_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("ESL_PORT", "8021"))
    except ValueError:
        port = 8021
    try:
        sock = socket.create_connection((host, port), timeout=5)
        buf = b""

        def read_block() -> bytes:
            nonlocal buf
            while b"\n\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("esl closed")
                buf += chunk
            head, buf = buf.split(b"\n\n", 1)
            return head

        def read_body() -> bytes:
            nonlocal buf
            head = read_block()
            length = 0
            for line in head.decode(errors="replace").splitlines():
                if line.lower().startswith("content-length:"):
                    try:
                        length = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        length = 0
            while len(buf) < length:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            body, buf = buf[:length], buf[length:]
            return body

        read_block()  # auth/request banner
        sock.sendall(f"auth {password}\n\n".encode())
        if b"+OK" not in read_block():
            raise ConnectionError("esl auth rejected")
        sock.sendall(f"api {command}\n\n".encode())
        body = read_body()
        try:
            sock.sendall(b"exit\n\n")
        except OSError:
            pass
        sock.close()
        return body.decode(errors="replace")
    except Exception:
        return None


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
    # Dialplan markers (FreeSWITCH sets them as channel variables; the
    # JSON CDR carries them under "variables"). bridge_ticket=1 means
    # this leg went through the hotline/queue dialplan and owns a
    # record; internal extension-to-extension legs and B-legs do not
    # set it and must never create records. bridge_voicemail=1 marks
    # the voicemail leg, whose record is owned by the voicemail path.
    ticket_flag: bool = False
    is_voicemail: bool = False

    # IVR digit -> record grouping for the bicycle hotline (kept for a
    # future IVR; plain calls carry no digit and land in 客服组）.
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


def fmt_duration(sec: int) -> str:
    """Legacy-style duration: 91 -> '1分31秒', 42 -> '0分42秒'."""
    sec = max(int(sec or 0), 0)
    return f"{sec // 60}分{sec % 60:02d}秒"


def fmt_cst(epoch: int) -> str:
    """Unix epoch -> 'YYYY-MM-DD HH:MM:SS' in Asia/Shanghai (the
    client's timezone; the bridge itself runs on UTC). Empty if unset."""
    if not epoch:
        return ""
    return datetime.fromtimestamp(
        int(epoch), tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


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
        # Loopback/queue legs often carry the number only as the
        # effective caller ID (or the originate param) while the plain
        # caller_id_number is empty; those are fallbacks only, never
        # overriding a present caller_id_number.
        caller=str(g("caller_id_number", "from", "caller", "sip_from_user",
                     "effective_caller_id_number",
                     "origination_caller_id_number")),
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
        ticket_flag=str(g("bridge_ticket")).lower() in ("1", "true"),
        is_voicemail=str(g("bridge_voicemail")).lower() in ("1", "true"),
        hangup_cause=str(g("hangup_cause")),
    )


DESK_SEATS = ("8001", "8002", "8003")


def record_owner(rec: dict, seats=DESK_SEATS) -> str:
    """Effective owning seat of a record: the answering agent when it is
    a real seat, else the voicemail owner seat. Anything else (missed
    calls, FS test-agent calls) counts as unowned."""
    agent = (rec.get("agent") or "").strip()
    if agent in seats:
        return agent
    return (rec.get("owner_seat") or "").strip()


def can_edit_record(rec: dict, seat: str, seats=DESK_SEATS) -> bool:
    """Only the owning seat may modify a record; unowned records stay
    editable by any seat, and every seat may always view any record."""
    owner = record_owner(rec, seats)
    return (not owner) or (owner == seat)


# ------------------------------------------------------------------ storage

class CallStore:
    """SQLite call log: one row per CallID, plus per-call statistics.
    Rows with is_record=1 are customer records; the rest are statistics
    rows for internal/outbound legs."""

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
            # agent (answering seat, from the queue CDR's cc_agent) feeds
            # the fewest-calls distribution: answered calls per agent.
            try:
                c.execute("ALTER TABLE calls ADD COLUMN agent TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # column already exists
            # owner_id is legacy (Zammad user id of the dispatched seat);
            # the record system uses owner_seat (the seat number) instead.
            # Both stay in the schema so old databases still open.
            try:
                c.execute("ALTER TABLE calls ADD COLUMN owner_id INTEGER")
            except sqlite3.OperationalError:
                pass  # column already exists
            # recording_attached is legacy (ticket attachment bookkeeping).
            try:
                c.execute("ALTER TABLE calls ADD COLUMN recording_attached INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # column already exists
            # Record-system columns. is_record=1 marks a real customer
            # record (queue leg / voicemail); statistics rows from
            # internal or outbound legs stay 0 and never appear in the
            # record list. kind is 电话 / 未接 / 留言.
            for ddl in (
                "ALTER TABLE calls ADD COLUMN is_record INTEGER DEFAULT 0",
                "ALTER TABLE calls ADD COLUMN kind TEXT DEFAULT ''",
                "ALTER TABLE calls ADD COLUMN recording_path TEXT DEFAULT ''",
                "ALTER TABLE calls ADD COLUMN question TEXT DEFAULT ''",
                "ALTER TABLE calls ADD COLUMN service_type TEXT DEFAULT ''",
                "ALTER TABLE calls ADD COLUMN owner_seat TEXT DEFAULT ''",
            ):
                try:
                    c.execute(ddl)
                except sqlite3.OperationalError:
                    pass  # column already exists
            # meta holds small persistent counters, e.g. the voicemail
            # round-robin position (must survive bridge restarts).
            c.execute(
                "CREATE TABLE IF NOT EXISTS meta ("
                " key TEXT PRIMARY KEY, value TEXT)"
            )
            # Seat accounts for the unified login: login is the seat
            # number (8001/8002/8003); passwords are stored hashed
            # (pbkdf2, per-account salt), never in plain text. Set via
            # the /setup page (BRIDGE_SECRET-protected), not by agents.
            c.execute(
                "CREATE TABLE IF NOT EXISTS agents ("
                " login TEXT PRIMARY KEY, salt TEXT, pw_hash TEXT)"
            )
            # Login sessions: token -> seat, with absolute expiry
            # (12h) and a last_seen heartbeat. The desk page touches
            # its session on every poll, so closing the browser lets
            # the seat go stale and the cleanup logs it out.
            c.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                " token TEXT PRIMARY KEY, seat TEXT,"
                " created_at INTEGER, last_seen INTEGER)"
            )
            # Caller cards + tags (user-level, keyed by phone number):
            # caller_cards keeps one phone's current card number, so a
            # new record from a known phone auto-carries it; record_tags
            # attaches tags to individual records, and per-caller tag
            # frequency is counted from them (most-used first).
            c.execute(
                "CREATE TABLE IF NOT EXISTS caller_cards ("
                " phone TEXT PRIMARY KEY, card TEXT, updated_at INTEGER)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS record_tags ("
                " record_id INTEGER, tag TEXT,"
                " PRIMARY KEY(record_id, tag))"
            )
            # Caller -> answering-seat mappings learned from live
            # bridge events (the queue CDR carries no seat).
            c.execute(
                "CREATE TABLE IF NOT EXISTS call_agents ("
                "call_id TEXT PRIMARY KEY, seat TEXT NOT NULL, "
                "created_at REAL NOT NULL)"
            )
            # card_no on the record itself: the card shown for this
            # call (auto-carried from caller_cards at creation, or set
            # by the agent on the detail page).
            try:
                c.execute("ALTER TABLE calls ADD COLUMN card_no TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # column already exists

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def note_agent(self, call_id: str, seat: str) -> None:
        """Persist a caller -> answering-seat mapping learned from a
        live bridge event (first bridge wins)."""
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS call_agents ("
                "call_id TEXT PRIMARY KEY, seat TEXT NOT NULL, "
                "created_at REAL NOT NULL)")
            c.execute("INSERT OR IGNORE INTO call_agents VALUES (?, ?, ?)",
                      (call_id, seat, time.time()))

    def agent_for_call(self, call_id: str) -> str:
        """Answering seat learned from a live bridge event, or ""."""
        with self._conn() as c:
            try:
                row = c.execute(
                    "SELECT seat FROM call_agents WHERE call_id = ?",
                    (call_id,)).fetchone()
            except sqlite3.OperationalError:
                return ""  # table predates this build (old database)
        return row[0] if row else ""

    def get_record_rowid(self, call_id: str) -> Optional[int]:
        """Rowid of the record row for this CallID, if one exists."""
        with self._conn() as c:
            row = c.execute("SELECT rowid FROM calls WHERE call_id=?", (call_id,)).fetchone()
        return row[0] if row else None

    def meta_get(self, key: str, default: str = "") -> str:
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def meta_set(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                      (key, value))

    def meta_incr(self, key: str) -> int:
        """Increment a meta counter, returning the new value."""
        try:
            new = int(self.meta_get(key, "0")) + 1
        except ValueError:
            new = 1
        self.meta_set(key, str(new))
        return new

    # ---------------------------------------------------------- accounts
    SESSION_TTL_SEC = 12 * 3600      # absolute session lifetime
    SESSION_STALE_SEC = 600          # no heartbeat for this long = stale

    @staticmethod
    def _hash_password(password: str, salt_hex: str) -> str:
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex),
            200_000).hex()

    def set_agent_password(self, login: str, password: str) -> None:
        """Create or reset one seat account (password hashed, never
        stored plain). Called from the /setup page, not by agents."""
        salt = secrets.token_hex(16)
        with self._conn() as c:
            c.execute(
                "INSERT INTO agents (login, salt, pw_hash) VALUES (?,?,?)"
                " ON CONFLICT(login) DO UPDATE SET salt=excluded.salt,"
                " pw_hash=excluded.pw_hash",
                (login, salt, self._hash_password(password, salt)))

    def has_agents(self) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT COUNT(*) FROM agents").fetchone()
        return bool(row and row[0])

    def verify_agent(self, login: str, password: str) -> bool:
        """True when this seat account exists and the password matches."""
        if not login or not password:
            return False
        with self._conn() as c:
            row = c.execute("SELECT salt, pw_hash FROM agents WHERE login=?",
                            (login,)).fetchone()
        if not row:
            return False
        try:
            return secrets.compare_digest(
                self._hash_password(password, row[0]), row[1])
        except (ValueError, TypeError):
            return False

    def create_session(self, seat: str) -> str:
        """New login session token for a seat (12h absolute)."""
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self._conn() as c:
            c.execute(
                "INSERT INTO sessions (token, seat, created_at, last_seen)"
                " VALUES (?,?,?,?)", (token, seat, now, now))
        return token

    def get_session_seat(self, token: str) -> Optional[str]:
        """Seat for a live session token, touching its heartbeat.
        Returns None for unknown or absolutely-expired tokens."""
        if not token:
            return None
        now = int(time.time())
        with self._conn() as c:
            row = c.execute(
                "SELECT seat, created_at FROM sessions WHERE token=?",
                (token,)).fetchone()
            if not row or now - row[1] > self.SESSION_TTL_SEC:
                return None
            c.execute("UPDATE sessions SET last_seen=? WHERE token=?",
                      (now, token))
        return row[0]

    def delete_session(self, token: str) -> Optional[str]:
        """Drop one session, returning its seat (for logout handling)."""
        if not token:
            return None
        with self._conn() as c:
            row = c.execute("SELECT seat FROM sessions WHERE token=?",
                            (token,)).fetchone()
            c.execute("DELETE FROM sessions WHERE token=?", (token,))
        return row[0] if row else None

    def seats_with_live_sessions(self) -> set:
        """Seats with at least one session heartbeating recently."""
        now = int(time.time())
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT seat FROM sessions"
                " WHERE last_seen >= ?", (now - self.SESSION_STALE_SEC,)).fetchall()
        return {r[0] for r in rows}

    def seats_with_any_session(self) -> set:
        """Seats that ever logged in (cleanup candidates)."""
        with self._conn() as c:
            rows = c.execute("SELECT DISTINCT seat FROM sessions").fetchall()
        return {r[0] for r in rows}

    def purge_expired_sessions(self) -> None:
        """Drop sessions past their absolute lifetime."""
        now = int(time.time())
        with self._conn() as c:
            c.execute("DELETE FROM sessions WHERE created_at < ?",
                      (now - self.SESSION_TTL_SEC,))

    def set_busy_flag(self, seat: str, busy: bool) -> None:
        """Desk busy toggle, bridge-side (meta key). This is the
        system-side busy state: it moves even when the queue
        (FreeSWITCH) is unreachable, and login clears it."""
        self.meta_set(f"busy:{seat}", "1" if busy else "0")

    def get_busy_flag(self, seat: str) -> bool:
        return self.meta_get(f"busy:{seat}", "0") == "1"

    def answered_calls_by_agent(self, agent: str) -> int:
        """Answered inbound calls handled by one agent (the queue CDRs
        carry the answering agent in cc_agent). Internal calls and
        voicemail legs carry no agent and are never counted."""
        if not agent:
            return 0
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM calls WHERE answered=1 AND agent=?",
                (agent,)).fetchone()
        return row[0] if row else 0

    def recent_records(self, limit: int = 20) -> list[dict]:
        """Newest customer records first, for the agent board feed:
        each row carries its monotonic rowid so the board can tell a
        newly arrived record from ones it already saw, plus the seat
        that owns it (answering agent, else voicemail owner seat) so
        the board can pop the alert only on that seat's desk."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT rowid, call_id, caller, kind, created_at, agent,"
                " owner_seat FROM calls "
                "WHERE is_record=1 ORDER BY rowid DESC LIMIT ?",
                (limit,)).fetchall()
        return [{"rowid": r[0], "call_id": r[1], "caller": r[2],
                 "kind": r[3] or "电话", "created_at": r[4],
                 "agent": r[5] or r[6] or ""} for r in rows]

    def calls_for_caller(self, phone: str, exclude_call_id: str = "",
                         limit: int = 10) -> list[dict]:
        """This caller number's past records, newest first, excluding
        one call (the current one) — the returning-caller hint source."""
        if not phone:
            return []
        with self._conn() as c:
            rows = c.execute(
                "SELECT start_epoch, kind, duration_sec, service_type, question"
                " FROM calls WHERE caller=? AND call_id != ? AND is_record=1"
                " ORDER BY rowid DESC LIMIT ?",
                (phone, exclude_call_id, limit)).fetchall()
        return [{"time": fmt_cst(r[0] or 0), "kind": r[1] or "电话",
                 "duration": fmt_duration(r[2] or 0),
                 "service_type": r[3] or "", "question": r[4] or ""}
                for r in rows]

    def save_call(self, rec: CallRecord, is_record: bool, kind: str = "",
                  owner_seat: str = "") -> int:
        """Upsert one CDR/event leg. A row becomes a customer record
        when is_record is set (queue leg or voicemail); statistics legs
        keep is_record=0 and never appear in the record list. Notes
        (question/service_type) are only ever written by set_notes, so
        a late CDR can never wipe what the agent typed. Caller and
        agent are fill-only when already set: an answer-time record
        created from a bridge event (caller/agent possibly empty) is
        completed - never clobbered - by the hangup CDR. Returns rowid."""
        # New records auto-carry this phone's associated card number
        # (set by an agent on an earlier record); the upsert below
        # never overwrites a record's own card afterwards.
        card_no = self.get_caller_card(rec.caller) if rec.caller else ""
        with self._conn() as c:
            c.execute(
                "INSERT INTO calls (call_id, caller, callee, direction, answered,"
                " duration_sec, wait_sec, start_epoch, agent, is_record, kind,"
                " recording_path, owner_seat, card_no)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(call_id) DO UPDATE SET"
                " callee=excluded.callee,"
                " caller=CASE WHEN calls.caller = '' THEN excluded.caller"
                " ELSE calls.caller END,"
                " answered=excluded.answered, duration_sec=excluded.duration_sec,"
                " wait_sec=excluded.wait_sec,"
                " agent=CASE WHEN excluded.agent != '' THEN excluded.agent"
                " ELSE calls.agent END,"
                " is_record=MAX(calls.is_record, excluded.is_record),"
                " kind=CASE WHEN excluded.kind != '' THEN excluded.kind ELSE calls.kind END,"
                " recording_path=CASE WHEN excluded.recording_path != ''"
                " THEN excluded.recording_path ELSE calls.recording_path END,"
                " owner_seat=CASE WHEN excluded.owner_seat != ''"
                " THEN excluded.owner_seat ELSE calls.owner_seat END",
                (rec.call_id, rec.caller, rec.callee, rec.direction,
                 1 if rec.answered else 0, rec.duration_sec, rec.wait_sec, rec.start_epoch,
                 rec.agent, 1 if is_record else 0, kind, rec.recording_path, owner_seat,
                 card_no),
            )
            row = c.execute("SELECT rowid FROM calls WHERE call_id=?",
                            (rec.call_id,)).fetchone()
        return row[0] if row else 0

    def get_record(self, rowid: int) -> Optional[dict]:
        """One record's full detail for the detail page, or None."""
        with self._conn() as c:
            r = c.execute(
                "SELECT rowid, call_id, caller, callee, direction, answered,"
                " duration_sec, wait_sec, start_epoch, agent, kind,"
                " recording_path, question, service_type, owner_seat, created_at,"
                " card_no"
                " FROM calls WHERE rowid=? AND is_record=1", (rowid,)).fetchone()
        if not r:
            return None
        # Effective card: this record's own card, else the phone's
        # current association (older records predate the feature).
        card = r[16] or self.get_caller_card(r[2])
        return {
            "record_id": r[0], "call_id": r[1], "caller": r[2], "callee": r[3],
            "direction": r[4], "answered": bool(r[5]),
            "duration": fmt_duration(r[6] or 0), "duration_sec": r[6] or 0,
            "wait_sec": r[7] or 0, "time": fmt_cst(r[8] or 0),
            "start_epoch": r[8] or 0,
            "agent": r[9] or "", "kind": r[10] or "电话",
            "has_recording": bool(r[11]), "recording_file": r[11] or "",
            "question": r[12] or "", "service_type": r[13] or "",
            "owner_seat": r[14] or "", "created_at": r[15] or "",
            "card": card or "",
            "tags": self.get_record_tags(rowid),
            "frequent_tags": self.caller_top_tags(r[2]),
        }

    def set_notes(self, rowid: int, question: str, service_type: str) -> bool:
        """Agent fills in what the caller asked (and the service type)
        during or after the call. Returns False for unknown records."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE calls SET question=?, service_type=? "
                "WHERE rowid=? AND is_record=1",
                (question or "", service_type or "", rowid))
            return cur.rowcount > 0

    def get_caller_card(self, phone: str) -> str:
        """This phone's current card number, or ''."""
        if not phone:
            return ""
        with self._conn() as c:
            row = c.execute("SELECT card FROM caller_cards WHERE phone=?",
                            (phone,)).fetchone()
        return row[0] if row else ""

    def set_record_card(self, rowid: int, card: str) -> bool:
        """Set a record's card number; a non-empty card also becomes
        this phone's associated card (future records auto-carry it).
        Clearing a record's card drops the phone's association too.
        Returns False for unknown records."""
        card = (card or "").strip()
        with self._conn() as c:
            row = c.execute("SELECT caller FROM calls WHERE rowid=? AND is_record=1",
                            (rowid,)).fetchone()
            if not row:
                return False
            phone = row[0] or ""
            c.execute("UPDATE calls SET card_no=? WHERE rowid=?", (card, rowid))
            if phone:
                if card:
                    c.execute(
                        "INSERT INTO caller_cards (phone, card, updated_at)"
                        " VALUES (?,?,?)"
                        " ON CONFLICT(phone) DO UPDATE SET card=excluded.card,"
                        " updated_at=excluded.updated_at",
                        (phone, card, int(time.time())))
                else:
                    c.execute("DELETE FROM caller_cards WHERE phone=?", (phone,))
        return True

    def get_record_tags(self, rowid: int) -> list[str]:
        with self._conn() as c:
            rows = c.execute("SELECT tag FROM record_tags WHERE record_id=?"
                             " ORDER BY tag", (rowid,)).fetchall()
        return [r[0] for r in rows]

    def add_record_tag(self, rowid: int, tag: str) -> bool:
        """Attach a tag to a record. Returns False for unknown records
        or empty tags."""
        tag = (tag or "").strip()
        if not tag:
            return False
        with self._conn() as c:
            row = c.execute("SELECT 1 FROM calls WHERE rowid=? AND is_record=1",
                            (rowid,)).fetchone()
            if not row:
                return False
            c.execute("INSERT OR IGNORE INTO record_tags (record_id, tag)"
                      " VALUES (?,?)", (rowid, tag))
        return True

    def remove_record_tag(self, rowid: int, tag: str) -> bool:
        tag = (tag or "").strip()
        with self._conn() as c:
            cur = c.execute("DELETE FROM record_tags WHERE record_id=? AND tag=?",
                            (rowid, tag))
        return cur.rowcount > 0

    def caller_top_tags(self, phone: str, limit: int = 5) -> list[dict]:
        """This phone's most-used tags across its records, most-used
        first (the auto-suggest source for a returning caller)."""
        if not phone:
            return []
        with self._conn() as c:
            rows = c.execute(
                "SELECT tag, COUNT(*) FROM record_tags JOIN calls"
                " ON record_tags.record_id = calls.rowid"
                " WHERE calls.caller=? AND calls.is_record=1"
                " GROUP BY tag ORDER BY COUNT(*) DESC, tag ASC LIMIT ?",
                (phone, limit)).fetchall()
        return [{"tag": r[0], "count": r[1]} for r in rows]

    def calls_for_user(self, phone: str, card: str = "", exclude_call_id: str = "",
                       limit: int = 10) -> list[dict]:
        """Past records for a user, linked by phone number OR card
        number (the same card on another number counts too), newest
        first, excluding one call (the current one)."""
        if not phone and not card:
            return []
        with self._conn() as c:
            if card:
                rows = c.execute(
                    "SELECT caller, start_epoch, kind, duration_sec,"
                    " service_type, question FROM calls"
                    " WHERE is_record=1 AND call_id != ? AND"
                    " (caller=? OR card_no=? OR caller IN"
                    " (SELECT phone FROM caller_cards WHERE card=?))"
                    " ORDER BY rowid DESC LIMIT ?",
                    (exclude_call_id, phone, card, card, limit)).fetchall()
            else:
                rows = c.execute(
                    "SELECT caller, start_epoch, kind, duration_sec,"
                    " service_type, question FROM calls"
                    " WHERE caller=? AND call_id != ? AND is_record=1"
                    " ORDER BY rowid DESC LIMIT ?",
                    (phone, exclude_call_id, limit)).fetchall()
        return [{"caller": r[0], "time": fmt_cst(r[1] or 0), "kind": r[2] or "电话",
                 "duration": fmt_duration(r[3] or 0),
                 "service_type": r[4] or "", "question": r[5] or "",
                 "via_card": bool(phone and r[0] != phone)}
                for r in rows]

    def caller_history_rows(self, phone: str, card: str = "",
                            exclude_call_id: str = "",
                            limit: int = 10) -> tuple:
        """(total, rows) for the desk history rail: past records for a
        number OR its card, newest first, excluding one call. Same
        scope as calls_for_user; rows carry record_id so the rail can
        open them, plus recording_file for playback."""
        if not phone and not card:
            return 0, []
        if card:
            where = ("is_record=1 AND call_id != ? AND"
                     " (caller=? OR card_no=? OR caller IN"
                     " (SELECT phone FROM caller_cards WHERE card=?))")
            args: tuple = (exclude_call_id, phone, card, card)
        else:
            where = "is_record=1 AND call_id != ? AND caller=?"
            args = (exclude_call_id, phone)
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM calls WHERE " + where,
                              args).fetchone()[0]
            rows = c.execute(
                "SELECT rowid, caller, start_epoch, kind, duration_sec,"
                " service_type, question, recording_path FROM calls"
                " WHERE " + where + " ORDER BY rowid DESC LIMIT ?",
                (*args, limit)).fetchall()
        return total, [{
            "record_id": r[0], "caller": r[1], "time": fmt_cst(r[2] or 0),
            "kind": r[3] or "电话", "duration": fmt_duration(r[4] or 0),
            "service_type": r[5] or "", "question": r[6] or "",
            "recording_file": r[7] or "",
            "via_card": bool(phone and r[1] != phone),
        } for r in rows]

    def caller_portrait(self, phone: str, card: str = "",
                        exclude_call_id: str = "") -> dict:
        """Full-history aggregate for the desk portrait card: same
        number-or-card scope as caller_history_rows but over ALL past
        records (no limit), so counts and top issues are exact, not
        limited to the latest 10 rail rows."""
        empty = {"total": 0, "by_kind": {"电话": 0, "未接": 0, "留言": 0},
                 "first_time": "", "last": None, "top_types": [],
                 "top_tags": [], "via_card": 0}
        if not phone and not card:
            return empty
        if card:
            where = ("is_record=1 AND call_id != ? AND"
                     " (caller=? OR card_no=? OR caller IN"
                     " (SELECT phone FROM caller_cards WHERE card=?))")
            args: tuple = (exclude_call_id, phone, card, card)
        else:
            where = "is_record=1 AND call_id != ? AND caller=?"
            args = (exclude_call_id, phone)
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM calls WHERE " + where,
                              args).fetchone()[0]
            if not total:
                return empty
            by_kind = {"电话": 0, "未接": 0, "留言": 0}
            for kind, n in c.execute(
                    "SELECT kind, COUNT(*) FROM calls WHERE " + where +
                    " GROUP BY kind", args).fetchall():
                by_kind[kind or "电话"] = n
            first = c.execute("SELECT MIN(start_epoch) FROM calls WHERE " +
                              where, args).fetchone()[0]
            last = c.execute(
                "SELECT kind, start_epoch, service_type, question"
                " FROM calls WHERE " + where + " ORDER BY rowid DESC"
                " LIMIT 1", args).fetchone()
            top = c.execute(
                "SELECT service_type, COUNT(*) FROM calls WHERE " + where +
                " AND service_type != '' GROUP BY service_type"
                " ORDER BY COUNT(*) DESC, service_type ASC LIMIT 5",
                args).fetchall()
            via = c.execute(
                "SELECT COUNT(*) FROM calls WHERE " + where +
                " AND caller != ?", (*args, phone)).fetchone()[0] if card \
                else 0
            top_tags = c.execute(
                "SELECT tag, COUNT(*) FROM record_tags JOIN calls"
                " ON record_tags.record_id = calls.rowid WHERE " + where +
                " GROUP BY tag ORDER BY COUNT(*) DESC, tag ASC LIMIT 2",
                args).fetchall()
        return {"total": total, "by_kind": by_kind,
                "first_time": fmt_cst(first or 0),
                "last": ({"kind": last[0] or "电话",
                          "time": fmt_cst(last[1] or 0),
                          "service_type": last[2] or "",
                          "question": last[3] or ""} if last else None),
                "top_types": [{"type": t, "count": n} for t, n in top],
                "top_tags": [t for t, _ in top_tags],
                "via_card": via}

    def list_records(self, caller: str = "", agent: str = "", kind: str = "",
                     date_from: str = "", date_to: str = "",
                     limit: int = 50, offset: int = 0) -> list[dict]:
        """Record rows for the list page / API, newest first.
        date_from/date_to are YYYY-MM-DD (Asia/Shanghai) bounds on the
        call start; caller matches a substring of the number."""
        conds = ["is_record=1"]
        args: list = []
        if caller:
            conds.append("caller LIKE ?")
            args.append(f"%{caller}%")
        if agent:
            conds.append("(agent=? OR owner_seat=?)")
            args.extend([agent, agent])
        if kind:
            conds.append("kind=?")
            args.append(kind)
        if date_from:
            try:
                start = int(datetime.strptime(date_from, "%Y-%m-%d").replace(
                    tzinfo=timezone(timedelta(hours=8))).timestamp())
                conds.append("start_epoch>=?")
                args.append(start)
            except ValueError:
                pass
        if date_to:
            try:
                end = int((datetime.strptime(date_to, "%Y-%m-%d").replace(
                    tzinfo=timezone(timedelta(hours=8))) + timedelta(days=1)).timestamp())
                conds.append("start_epoch<?")
                args.append(end)
            except ValueError:
                pass
        with self._conn() as c:
            rows = c.execute(
                "SELECT rowid, call_id, caller, agent, kind, duration_sec,"
                " start_epoch, service_type, question, owner_seat, answered"
                " FROM calls WHERE " + " AND ".join(conds) +
                " ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (*args, max(1, min(limit, 200)), max(0, offset))).fetchall()
        return [{
            "record_id": r[0], "call_id": r[1], "caller": r[2],
            "agent": r[3] or r[9] or "", "kind": r[4] or "电话",
            "duration": fmt_duration(r[5] or 0), "time": fmt_cst(r[6] or 0),
            "service_type": r[7] or "", "question": r[8] or "",
            "owner_seat": r[9] or "", "answered": bool(r[10]),
        } for r in rows]

    def agent_stats(self, agent: str, today_start: int) -> dict:
        """One seat's handled contacts: answered calls today / total,
        plus assigned voicemails (bridge-tracked, since voicemail legs
        carry no agent)."""
        with self._conn() as c:
            today = c.execute(
                "SELECT COUNT(*) FROM calls WHERE answered=1 AND agent=? "
                "AND start_epoch>=?", (agent, today_start)).fetchone()[0]
            total = c.execute(
                "SELECT COUNT(*) FROM calls WHERE answered=1 AND agent=?",
                (agent,)).fetchone()[0]
        try:
            vm = int(self.meta_get(f"vm_assigned:{agent}", "0"))
        except ValueError:
            vm = 0
        return {"calls_today": today, "calls_total": total, "voicemails": vm}

    def agent_history(self, agent: str, limit: int = 50) -> list[dict]:
        """One seat's handled contacts, newest first: answered queue
        calls (by cc_agent) plus assigned voicemails (by owner seat).
        Missed calls and other seats' contacts never appear."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT rowid, caller, duration_sec, start_epoch, kind, owner_seat"
                " FROM calls WHERE is_record=1 AND"
                " ((answered=1 AND agent=?) OR (owner_seat=? AND owner_seat != ''))"
                " ORDER BY rowid DESC LIMIT ?",
                (agent, agent, limit)).fetchall()
        out = []
        for rowid, caller, dur, start, kind, owner in rows:
            out.append({
                "caller": caller,
                "kind": kind or ("留言" if owner else "电话"),
                "duration": fmt_duration(dur or 0),
                "time": fmt_cst(start or 0),
                "record_id": rowid,
            })
        return out

    def agent_stats_range(self, agent: str, start_epoch: int, end_epoch: int) -> dict:
        """One seat's handled contacts inside [start_epoch, end_epoch):
        answered queue calls (by cc_agent) and assigned voicemails
        (by owner seat). Table-based, so any window works - unlike
        the meta voicemail counter, which is cumulative only."""
        with self._conn() as c:
            calls = c.execute(
                "SELECT COUNT(*) FROM calls WHERE answered=1 AND agent=? "
                "AND start_epoch>=? AND start_epoch<?",
                (agent, start_epoch, end_epoch)).fetchone()[0]
            vms = c.execute(
                "SELECT COUNT(*) FROM calls WHERE is_record=1 AND kind='留言' "
                "AND owner_seat=? AND start_epoch>=? AND start_epoch<?",
                (agent, start_epoch, end_epoch)).fetchone()[0]
        return {"calls": calls, "voicemails": vms}

    def agent_history_range(self, agent: str, start_epoch: int, end_epoch: int,
                            limit: int = 50) -> list[dict]:
        """Like agent_history, but only contacts inside the window."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT rowid, caller, duration_sec, start_epoch, kind, owner_seat"
                " FROM calls WHERE is_record=1 AND start_epoch>=? AND start_epoch<? AND"
                " ((answered=1 AND agent=?) OR (owner_seat=? AND owner_seat != ''))"
                " ORDER BY rowid DESC LIMIT ?",
                (start_epoch, end_epoch, agent, agent, limit)).fetchall()
        out = []
        for rowid, caller, dur, start, kind, owner in rows:
            out.append({
                "caller": caller,
                "kind": kind or ("留言" if owner else "电话"),
                "duration": fmt_duration(dur or 0),
                "time": fmt_cst(start or 0),
                "record_id": rowid,
            })
        return out

    def list_calls(self, start_epoch: int = 0) -> list[dict]:
        """Record rows for the Excel export, oldest first (legacy
        Kinghav-style columns: time, caller, agent, duration, channel,
        service type, question, recording file, CallID)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT rowid, call_id, caller, agent, owner_seat, kind,"
                " duration_sec, start_epoch, service_type, question,"
                " recording_path, answered"
                " FROM calls WHERE is_record=1 AND start_epoch >= ?"
                " ORDER BY start_epoch",
                (start_epoch,),
            ).fetchall()
        return [{
            "record_id": r[0], "call_id": r[1], "caller": r[2],
            "agent": r[3] or r[4] or "", "kind": r[5] or "电话",
            "duration_sec": r[6] or 0, "start_epoch": r[7] or 0,
            "service_type": r[8] or "", "question": r[9] or "",
            "recording_file": r[10] or "", "answered": bool(r[11]),
        } for r in rows]

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


def summarize_call_history(rows: list) -> str:
    """Keyword hint built from a returning customer's past records
    (store rows from calls_for_caller, newest first).

    Counts the service types seen before and names the most recent
    contact, e.g. for the agent answering a repeat call:
    "该客户历史来电 3 次；常见问题：还车异常×2、锁桩故障×1；最近一次：电话（2026-10-20）"
    Returns "" when there is no usable history.
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return ""
    types: dict = {}
    for r in rows:
        t = str(r.get("service_type") or "").strip()
        if t:
            types[t] = types.get(t, 0) + 1
    parts = [f"该客户历史来电 {len(rows)} 次"]
    via_card = sum(1 for r in rows if r.get("via_card"))
    if via_card:
        parts.append(f"其中同卡号他号来电 {via_card} 次")
    if types:
        ranked = sorted(types.items(), key=lambda kv: -kv[1])
        parts.append("常见问题：" + "、".join(f"{k}×{v}" for k, v in ranked))
    latest = rows[0]
    parts.append(f"最近一次：{latest.get('kind') or '电话'}（{str(latest.get('time') or '')[:10]}）")
    return "；".join(parts)


# ------------------------------------------------------------------ bridge

# ------------------------------------------------------- ESL live events
# Queue CDRs do not carry the answering seat (no cc_agent on the caller
# leg - proven by the 2026-10-28 16:09:51 record: caller + notes + tag
# present, agent empty), so the bridge learns it from FreeSWITCH live
# events. When a queue caller bridges to an agent, CHANNEL_BRIDGE fires
# for both legs: the caller leg carries bridge_ticket=1, the agent leg's
# channel identity (sofia/internal/8002@... / destination 8002) maps to
# a seat. The mapping is persisted in call_agents (the listener keeps
# its own SQLite connection: the store's connection belongs to the
# request thread) and handle_cdr consults it when the CDR itself has no
# agent. Event payloads are never logged: bridge headers carry caller
# numbers. Fail-soft: ESL down just means reconnect retries; the bridge
# keeps serving and CDRs keep arriving seat-less, as today.

ESL_EVENT_SUBSCRIPTION = "CHANNEL_BRIDGE"


def _esl_password() -> str:
    """Same source as esl_command: ESL_PASSWORD, else the server-side
    freeswitch/esl-password.txt (never in Git)."""
    pw = os.environ.get("ESL_PASSWORD", "")
    if pw:
        return pw
    try:
        with open("/freeswitch-config/esl-password.txt") as f:
            return f.read().strip()
    except OSError:
        return ""


def explicit_agent(headers: dict, seats=DESK_SEATS) -> Optional[str]:
    """The cc_agent variable, when it names a real seat. This is the
    only agent hint trusted on the caller leg itself: the caller leg's
    channel name identifies the CALLER, not the agent."""
    cc = (headers.get("variable_cc_agent", "") or "").strip()
    return cc if cc in seats else None


def leg_seat(headers: dict, seats=DESK_SEATS) -> Optional[str]:
    """Seat number for an agent leg, from its channel identity hints.
    The seat must match as a full segment ("/8002@" or exact "8002"),
    never as a substring, so 80011 never matches 8001."""
    seat = explicit_agent(headers, seats)
    if seat:
        return seat
    candidates = [
        headers.get("Caller-Destination-Number", ""),
        headers.get("variable_sip_to_user", ""),
        headers.get("variable_dialed_user", ""),
        headers.get("Channel-Name", ""),
    ]
    for c in candidates:
        c = (c or "").strip()
        if not c:
            continue
        for seat in seats:
            if c == seat or ("/" + seat + "@" in c) or \
                    c.endswith("/" + seat) or ("/" + seat + "/" in c):
                return seat
    return None


def event_caller(headers: dict) -> str:
    """Caller number from a bridge event's headers, same fallback chain
    as the CDR parser: the loopback/queue leg may leave the plain caller
    ID empty while the effective/originate IDs carry the number."""
    for key in ("variable_caller_id_number", "Caller-Caller-ID-Number",
                "variable_sip_from_user",
                "variable_effective_caller_id_number",
                "variable_origination_caller_id_number"):
        v = (headers.get(key, "") or "").strip()
        if v:
            return v
    return ""


class BridgeEventPairing:
    """Pairs CHANNEL_BRIDGE legs into caller -> seat mappings.

    Pure logic over event headers (no socket, no DB) so it can be unit
    tested with canned events. The caller leg is the one with
    bridge_ticket=1; the peer leg's channel identity maps to a seat.
    Unpaired legs are kept up to an hour (a bridge pair can straddle a
    slow leg) and then dropped."""

    def __init__(self, seats=DESK_SEATS):
        self.seats = tuple(seats)
        # leg uuid -> (peer uuid, is_caller, seat|None, at, caller)
        self.legs = {}

    def note(self, headers: dict):
        """Feed one CHANNEL_BRIDGE event; returns (call_id, seat|None,
        caller) when a bridge resolves, else None. seat is None when
        the caller bridged to a non-seat leg (e.g. the FS test agent):
        the bridge still happened, so the caller still gets an
        answer-time record, just with no owner."""
        leg = headers.get("Unique-ID", "")
        if not leg:
            return None
        a = headers.get("Bridge-A-Unique-ID", "")
        b = headers.get("Bridge-B-Unique-ID", "")
        peer = a if (a and a != leg) else (b if (b and b != leg) else "")
        is_caller = headers.get("variable_bridge_ticket", "") in ("1", "true")
        # The caller leg's own channel name identifies the caller, so
        # only an explicit cc_agent counts there; the peer leg's
        # identity is the agent's channel and may be inferred.
        seat = explicit_agent(headers, self.seats) if is_caller \
            else leg_seat(headers, self.seats)
        caller = event_caller(headers)
        now = time.time()
        self.legs[leg] = (peer, is_caller, seat, now, caller)
        for k in [k for k, v in self.legs.items() if now - v[3] > 3600]:
            del self.legs[k]
        if is_caller and seat:
            # The caller leg itself names its agent; no peer needed.
            del self.legs[leg]
            return (leg, seat, caller)
        if not peer or peer not in self.legs:
            return None
        _, peer_caller, peer_seat, _, peer_caller_no = self.legs[peer]
        resolved = None
        if is_caller and peer_seat:
            resolved = (leg, peer_seat, caller)
        elif seat and peer_caller:
            resolved = (peer, seat, peer_caller_no)
        elif is_caller:
            resolved = (leg, None, caller)
        elif peer_caller:
            resolved = (peer, None, peer_caller_no)
        if resolved:
            del self.legs[leg]
            self.legs.pop(peer, None)
        return resolved


def event_ts(headers: dict) -> int:
    """Bridge time from an event's Event-Date-Timestamp (microseconds
    since epoch); falls back to now when absent or malformed."""
    raw = (headers.get("Event-Date-Timestamp", "") or "").strip()
    if raw.isdigit():
        return int(raw) // 1_000_000
    return int(time.time())


class BridgeEventListener:
    """Background ESL event subscription feeding BridgeEventPairing."""

    def __init__(self, db_path: str, host: Optional[str] = None,
                 port: Optional[int] = None, password: Optional[str] = None,
                 seats=DESK_SEATS, live_queue=None):
        self.db_path = db_path
        self.host = host or os.environ.get("ESL_HOST", "127.0.0.1")
        try:
            self.port = port or int(os.environ.get("ESL_PORT", "8021"))
        except ValueError:
            self.port = 8021
        self.password = password
        # Resolved bridges land here for the Bridge to materialize as
        # answer-time records on the next feed/list poll (thread-safe).
        self.live_queue = live_queue
        self.pairing = BridgeEventPairing(seats)
        self.connected = False
        self.mapped = 0
        # Counts only (no call data): CHANNEL_BRIDGE events seen,
        # bridges resolved, seat mappings persisted, live records
        # queued. /health exposes them so a silent listener is
        # distinguishable from a pairing miss without logging events.
        self.events_seen = 0
        self.pairs_resolved = 0
        self.live_queued = 0
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True,
                         name="esl-events").start()

    def status(self) -> str:
        return "connected" if self.connected else "down"

    def _persist(self, call_id: str, seat: str) -> None:
        try:
            with sqlite3.connect(self.db_path, timeout=30) as c:
                c.execute(
                    "CREATE TABLE IF NOT EXISTS call_agents ("
                    "call_id TEXT PRIMARY KEY, seat TEXT NOT NULL, "
                    "created_at REAL NOT NULL)")
                c.execute(
                    "INSERT OR IGNORE INTO call_agents VALUES (?, ?, ?)",
                    (call_id, seat, time.time()))
        except sqlite3.Error:
            pass  # mapping loss degrades to today's seat-less behavior

    def _run(self) -> None:
        delay = 2.0
        while not self._stop.is_set():
            try:
                self._serve()
                delay = 2.0
            except Exception:
                self.connected = False
                self._stop.wait(delay)
                delay = min(delay * 2, 60.0)

    def _serve(self) -> None:
        import socket
        sock = socket.create_connection((self.host, self.port), timeout=5)
        buf = b""

        def read_block() -> bytes:
            nonlocal buf
            while b"\n\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("esl closed")
                buf += chunk
            head, buf = buf.split(b"\n\n", 1)
            return head

        def read_body(length: int) -> bytes:
            nonlocal buf
            while len(buf) < length:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("esl closed")
                buf += chunk
            body, buf = buf[:length], buf[length:]
            return body

        try:
            read_block()  # auth/request banner
            pw = self.password or _esl_password()
            if not pw:
                raise ConnectionError("no esl password")
            sock.sendall(f"auth {pw}\n\n".encode())
            if b"+OK" not in read_block():
                raise ConnectionError("esl auth rejected")
            sock.sendall(
                f"event plain {ESL_EVENT_SUBSCRIPTION}\n\n".encode())
            if b"+OK" not in read_block():
                raise ConnectionError("esl subscribe rejected")
            self.connected = True
            sock.settimeout(30)
            while not self._stop.is_set():
                try:
                    head = read_block()
                except socket.timeout:
                    continue
                headers = {}
                for line in head.decode(errors="replace").splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip()] = v.strip()
                length = 0
                raw_len = headers.get("Content-Length", "")
                if raw_len.isdigit():
                    length = int(raw_len)
                if length:
                    try:
                        body = read_body(length)
                    except socket.timeout:
                        continue
                    # Real ESL framing: the first block is only the
                    # MIME envelope (Content-Type/Content-Length); the
                    # event headers live in the BODY that follows it.
                    # Parsing the envelope as the event silently drops
                    # every event, so parse the body when it is one.
                    # A message with no Content-Length keeps its
                    # headers from the first block, as before.
                    if headers.get("Content-Type") == "text/event-plain":
                        for line in body.decode(errors="replace").splitlines():
                            if ":" in line:
                                k, v = line.split(":", 1)
                                headers[k.strip()] = v.strip()
                if headers.get("Event-Name") != "CHANNEL_BRIDGE":
                    continue
                self.events_seen += 1
                resolved = self.pairing.note(headers)
                if resolved:
                    self.pairs_resolved += 1
                    call_id, seat, caller = resolved
                    if seat:
                        self._persist(call_id, seat)
                        self.mapped += 1
                    if self.live_queue is not None:
                        self.live_queue.put({
                            "call_id": call_id, "seat": seat or "",
                            "caller": caller,
                            "started_at": event_ts(headers)})
                        self.live_queued += 1
        finally:
            self.connected = False
            try:
                sock.close()
            except OSError:
                pass


@dataclass
class Bridge:
    store: CallStore
    # Seat numbers in roster order (env ROTATION_AGENTS, default the
    # three seats). Voicemail records are dispatched to the roster
    # member with the fewest handled contacts; seat numbers must match
    # the FreeSWITCH extensions so queue CDRs (cc_agent) line up.
    rotation_agents: list = field(default_factory=list)
    # Off by default (unit tests must not spawn threads); the server
    # enables it so queue calls learn their answering seat live.
    enable_event_listener: bool = False

    def __post_init__(self):
        self._event_listener = None
        # Bridges resolved by the ESL listener wait here until the next
        # feed/list poll materializes them (thread-safe queue).
        self.live_calls: queue.Queue = queue.Queue()
        if self.enable_event_listener:
            self._event_listener = BridgeEventListener(
                self.store.db_path, live_queue=self.live_calls)
            self._event_listener.start()

    def drain_live_calls(self) -> None:
        """Materialize calls the ESL listener saw bridged as answer-time
        records, so the owning seat's desk pops during the call instead
        of at hangup. Idempotent (save_call upserts by CallID); the
        hangup CDR later fills duration/recording and any caller the
        event did not carry. Cheap when empty: called on every feed and
        list poll."""
        while True:
            try:
                item = self.live_calls.get_nowait()
            except queue.Empty:
                return
            rec = CallRecord(
                call_id=item["call_id"], caller=item.get("caller", ""),
                direction="in", answered=True, duration_sec=0, wait_sec=0,
                start_epoch=int(item.get("started_at") or time.time()),
                agent=item.get("seat", ""), ticket_flag=True)
            self.store.save_call(rec, True, kind="电话")

    def event_listener_status(self) -> Optional[str]:
        """None when disabled, else the listener's ESL state
        ("connected" / "down") for /health."""
        if self._event_listener is None:
            return None
        return self._event_listener.status()

    def event_listener_stats(self) -> Optional[dict]:
        """None when disabled, else the listener's counters (counts
        only, no call data) for /health."""
        listener = self._event_listener
        if listener is None:
            return None
        return {"events": listener.events_seen,
                "pairs": listener.pairs_resolved,
                "mapped": listener.mapped,
                "live": listener.live_queued}

    def _agent_statuses(self) -> dict:
        """Seat -> FreeSWITCH agent status (Available / On Call /
        On Break / Logged Out), via the event socket. {} when ESL is
        unreachable; callers then fall back to system login state.
        The agent list's runtime `state` column wins over the admin
        `status` column when it marks an agent in a call."""
        out = esl_command("callcenter_config agent list")
        if not out:
            return {}
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if not lines:
            return {}
        header = [h.strip() for h in lines[0].split("|")]
        try:
            name_i = header.index("name")
            status_i = header.index("status")
        except ValueError:
            return {}
        state_i = header.index("state") if "state" in header else -1
        statuses = {}
        for ln in lines[1:]:
            cols = [c.strip() for c in ln.split("|")]
            if len(cols) > max(name_i, status_i):
                status = cols[status_i]
                if state_i >= 0 and len(cols) > state_i and \
                        "call" in cols[state_i].lower():
                    status = "On Call"
                statuses[cols[name_i]] = status
        return statuses

    def seat_web_status(self, seat: str) -> str:
        """System-side seat status from login state alone: Logged Out
        when no live session, else On Break when the desk toggle set
        busy, else Available. Login alone makes a seat Available -
        this never waits for the queue."""
        if seat not in self.store.seats_with_live_sessions():
            return "Logged Out"
        return "On Break" if self.store.get_busy_flag(seat) else "Available"

    def statuses_with_source(self) -> tuple[dict, bool]:
        """(seat -> status, queue_connected). When the queue answers,
        its statuses are the routing truth (they include phone-side
        *51/*52 busy) — but routing truth is not presence: a seat
        the queue lists Available has nobody at the desk unless a
        desk session is live. So the queue status is merged with
        login state: an in-call seat stays On Call (the call itself
        proves presence), On Break wins from either side, and a seat
        with no live desk session shows Logged Out even when the
        queue still lists it Available (its static config value).
        When the queue does not answer, statuses fall back to system
        login state entirely."""
        esl = self._agent_statuses()
        if esl:
            live = self.store.seats_with_live_sessions()
            out = {}
            for a in self.rotation_agents:
                fs = esl.get(a)
                if fs == "On Call":
                    out[a] = "On Call"
                elif fs == "On Break" or self.store.get_busy_flag(a):
                    out[a] = "On Break"
                elif fs == "Logged Out" or a not in live:
                    out[a] = "Logged Out"
                elif fs == "Available":
                    out[a] = "Available"
                else:
                    out[a] = self.seat_web_status(a)
            return out, True
        return {a: self.seat_web_status(a) for a in self.rotation_agents}, False

    def get_agent_statuses(self) -> dict:
        """Roster seats mapped to their status (queue truth when
        reachable, system login state otherwise)."""
        return self.statuses_with_source()[0]

    def set_seat_busy(self, seat: str, busy: bool) -> tuple[str, bool]:
        """Desk busy toggle. The bridge-side flag always moves
        (system status); the queue is updated when reachable.
        Returns (status, queue_updated). Raises ValueError on a real
        queue error, so the two cannot silently disagree while the
        queue is connected."""
        target = "On Break" if busy else "Available"
        out = esl_command(f"callcenter_config agent set status {seat} '{target}'")
        if out and out.strip().startswith("+OK"):
            self.store.set_busy_flag(seat, busy)
            return target, True
        if out is None:
            # Queue unreachable: system status still moves; it syncs
            # to the queue on the next login/toggle once FreeSWITCH
            # is up (login always re-asserts Available).
            self.store.set_busy_flag(seat, busy)
            return target, False
        raise ValueError((out or "queue error").strip()[:200])

    def get_agent_history(self, seat: str) -> dict:
        """One seat's homepage data: how many calls they answered
        (today / total), how many voicemails were dispatched to them,
        and their handled-contact history, newest first."""
        cst = timezone(timedelta(hours=8))
        midnight = datetime.now(cst).replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            "seat": seat,
            "stats": self.store.agent_stats(seat, int(midnight.timestamp())),
            "history": self.store.agent_history(seat),
        }

    def set_agent_status(self, agent: str, status: str) -> tuple[bool, str]:
        """Set one seat's queue status - Available, On Break, or
        Logged Out - via the event socket. Returns (ok, status-or-error).
        Login uses Available, logout uses Logged Out, the busy toggle
        uses On Break/Available."""
        if status not in ("Available", "On Break", "Logged Out"):
            return False, "unknown status"
        out = esl_command(f"callcenter_config agent set status {agent} '{status}'")
        if out and out.strip().startswith("+OK"):
            return True, status
        return False, (out or "event socket unreachable").strip()[:200]

    def ensure_online(self, seat: str) -> bool:
        """Re-assert the queue status for a seat with a live session
        that the queue reports as Logged Out (e.g. queue restarted).
        Restores the bridge-side state (busy stays busy), never
        blindly Available. No-op when the queue is unreachable or
        the seat has no live session. Returns True when the seat ends
        up Available."""
        if seat not in self.store.seats_with_live_sessions():
            return False
        statuses = self._agent_statuses()
        if statuses.get(seat) != "Logged Out":
            return statuses.get(seat) == "Available"
        target = "On Break" if self.store.get_busy_flag(seat) else "Available"
        ok, _ = self.set_agent_status(seat, target)
        return ok and target == "Available"

    def logout_stale_seats(self) -> list:
        """Log out seats whose sessions all went stale (browser closed
        without logout, heartbeat gone for 10+ minutes). Only seats
        that ever logged in are touched; ESL failures are skipped
        silently so a later pass retries. Returns logged-out seats."""
        live = self.store.seats_with_live_sessions()
        candidates = self.store.seats_with_any_session() - live
        if not candidates:
            self.store.purge_expired_sessions()
            return []
        statuses = self._agent_statuses()
        out = []
        for seat in sorted(candidates):
            if statuses.get(seat) in ("Available", "On Break"):
                ok, _ = self.set_agent_status(seat, "Logged Out")
                if ok:
                    out.append(seat)
        self.store.purge_expired_sessions()
        return out

    def _next_voicemail_owner(self) -> Optional[str]:
        """Fewest-calls dispatch for a voicemail record: among seats
        that are Available right now (busy seats are skipped), pick the
        one with the fewest handled contacts (answered queue calls +
        previously assigned voicemails); ties go by roster order. The
        winner's voicemail count advances even when ESL is down, so one
        bad seat cannot block the rest. None when no seat is Available."""
        roster = [a for a in self.rotation_agents if a]
        if not roster:
            return None
        statuses = self._agent_statuses()
        if statuses:
            candidates = [a for a in roster if statuses.get(a) == "Available"]
        else:
            candidates = list(roster)  # ESL down: assume all available
        if not candidates:
            return None

        def handled(a: str) -> int:
            try:
                vm = int(self.store.meta_get(f"vm_assigned:{a}", "0"))
            except ValueError:
                vm = 0
            return self.store.answered_calls_by_agent(a) + vm

        pick = min(candidates, key=lambda a: (handled(a), roster.index(a)))
        self.store.meta_incr(f"vm_assigned:{pick}")
        return pick

    def _history_hint(self, rec: CallRecord) -> str:
        """Returning-caller hint from the bridge's own record store:
        past records for this number OR this card number, excluding
        the current call."""
        try:
            card = self.store.get_caller_card(rec.caller)
            past = self.store.calls_for_user(rec.caller, card, rec.call_id)
            return summarize_call_history(past)
        except Exception:
            return ""

    def record_history_hint(self, rowid: int) -> str:
        """History hint for an existing record: past records for its
        number OR its card number."""
        rec = self.store.get_record(rowid)
        if not rec:
            return ""
        try:
            past = self.store.calls_for_user(rec["caller"], rec["card"],
                                             rec["call_id"])
            return summarize_call_history(past)
        except Exception:
            return ""

    def caller_history(self, rowid: int, limit: int = 10) -> dict:
        """History rail payload for a record: its number-or-card past
        records, newest first, excluding itself, plus the full-history
        portrait aggregate for the portrait card."""
        rec = self.store.get_record(rowid)
        if not rec:
            return {"total": 0, "rows": [],
                    "portrait": self.store.caller_portrait("", "")}
        total, rows = self.store.caller_history_rows(
            rec["caller"], rec["card"], rec["call_id"], limit)
        portrait = self.store.caller_portrait(
            rec["caller"], rec["card"], rec["call_id"])
        return {"total": total, "rows": rows, "portrait": portrait}

    def handle_event(self, event: str, payload: dict) -> dict:
        rec = parse_cdr(payload)
        if not rec.call_id:
            return {"ok": False, "error": "missing call_id/uuid"}
        # The record must exist WHILE the agent is on the phone, so the
        # agent can fill in the question during the call instead of
        # waiting for hangup. On answer (inbound queue legs only), open
        # the record now; the CDR fills in duration and recording
        # afterwards. A missed call never sends answer, so its record is
        # created by the CDR path instead (kind 未接）.
        if event == "answer" and rec.direction == "in" and rec.ticket_flag:
            existing = self.store.get_record_rowid(rec.call_id)
            if existing:
                return {"ok": True, "call_id": rec.call_id, "event": event,
                        "record_id": existing, "note": "already bound"}
            rec.answered = True
            rowid = self.store.save_call(rec, True, kind="电话")
            return {"ok": True, "call_id": rec.call_id, "event": event,
                    "record_id": rowid, "history_hint": self._history_hint(rec)}
        return {"ok": True, "call_id": rec.call_id, "event": event}

    def handle_cdr(self, payload: dict) -> dict:
        rec = parse_cdr(payload)
        if not rec.call_id:
            return {"ok": False, "error": "missing call_id/uuid"}
        # The queue CDR carries no answering seat, so fall back to the
        # seat learned from the live bridge event (never overrides a
        # seat the CDR itself names).
        if not rec.agent:
            rec.agent = self.store.agent_for_call(rec.call_id)
        # 1) missed queue call -> kept as a record (kind 未接）, so the
        # callback list is visible; it never counts as answered.
        if not rec.answered and rec.ticket_flag:
            rowid = self.store.save_call(rec, True, kind="未接")
            return {"ok": True, "call_id": rec.call_id, "record_id": rowid,
                    "note": "missed call kept as record"}
        # 2) legs that must never own a record: outbound legs, voicemail
        # legs (the voicemail path owns their record), and internal
        # extension calls (no bridge_ticket marker from the queue
        # dialplan). Statistics only; an existing record binding is
        # left untouched.
        if rec.direction != "in" or rec.is_voicemail or not rec.ticket_flag:
            bound = self.store.get_record_rowid(rec.call_id)
            self.store.save_call(rec, False)
            return {"ok": True, "call_id": rec.call_id, "record_id": bound,
                    "note": "statistics only, no record for this leg"}
        # 3) idempotency: one CallID = one record. If the record was
        # already opened at answer time, the CDR only fills in duration
        # and recording path (notes the agent typed are never touched);
        # otherwise the record is created now.
        existing = self.store.get_record_rowid(rec.call_id)
        rowid = self.store.save_call(rec, True, kind="电话")
        return {"ok": True, "call_id": rec.call_id, "record_id": rowid,
                "has_recording": bool(rec.recording_path or rec.recording_url),
                "history_hint": self._history_hint(rec) if not existing else "",
                "note": "record updated" if existing else "record created"}

    def handle_voicemail(self, payload: dict) -> dict:
        """A caller left a voicemail (nobody answered / after hours).

        The message becomes a record of kind 留言 with the message
        recording, dispatched to the Available seat with the fewest
        handled contacts (busy seats are skipped). No roster or no
        Available seat leaves the record unowned. Idempotent by CallID,
        same as calls."""
        rec = parse_cdr(payload)
        if not rec.call_id:
            return {"ok": False, "error": "missing call_id/uuid"}
        rec.ivr_choice = "voicemail"
        rec.answered = True  # a completed message is a handled contact
        existing = self.store.get_record_rowid(rec.call_id)
        if existing:
            return {"ok": True, "call_id": rec.call_id, "record_id": existing,
                    "note": "already bound", "voicemail": True}
        owner_seat = self._next_voicemail_owner()
        rowid = self.store.save_call(rec, True, kind="留言", owner_seat=owner_seat or "")
        return {"ok": True, "call_id": rec.call_id, "record_id": rowid,
                "history_hint": self._history_hint(rec),
                "voicemail": True, "owner_seat": owner_seat}
