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

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional

def esl_command(command: str) -> Optional[str]:
    """One FreeSWITCH event-socket API call (stdlib socket, no deps).
    The socket binds 127.0.0.1 on the host; from the bridge container it
    is reachable at host.docker.internal:8021. The password comes from
    ESL_PASSWORD, else the server-side freeswitch/esl-password.txt
    mounted read-only at /freeswitch-config (never in Git). Any failure
    (no socket, bad auth, timeout) yields None so callers fall back."""
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
    host = os.environ.get("ESL_HOST", "host.docker.internal")
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
        ticket_flag=str(g("bridge_ticket")).lower() in ("1", "true"),
        is_voicemail=str(g("bridge_voicemail")).lower() in ("1", "true"),
        hangup_cause=str(g("hangup_cause")),
    )


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

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

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
        newly arrived record from ones it already saw."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT rowid, call_id, caller, kind, created_at FROM calls "
                "WHERE is_record=1 ORDER BY rowid DESC LIMIT ?",
                (limit,)).fetchall()
        return [{"rowid": r[0], "call_id": r[1], "caller": r[2],
                 "kind": r[3] or "电话", "created_at": r[4]} for r in rows]

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
        a late CDR can never wipe what the agent typed. Returns rowid."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO calls (call_id, caller, callee, direction, answered,"
                " duration_sec, wait_sec, start_epoch, agent, is_record, kind,"
                " recording_path, owner_seat)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(call_id) DO UPDATE SET"
                " callee=excluded.callee,"
                " answered=excluded.answered, duration_sec=excluded.duration_sec,"
                " wait_sec=excluded.wait_sec, agent=excluded.agent,"
                " is_record=MAX(calls.is_record, excluded.is_record),"
                " kind=CASE WHEN excluded.kind != '' THEN excluded.kind ELSE calls.kind END,"
                " recording_path=CASE WHEN excluded.recording_path != ''"
                " THEN excluded.recording_path ELSE calls.recording_path END,"
                " owner_seat=CASE WHEN excluded.owner_seat != ''"
                " THEN excluded.owner_seat ELSE calls.owner_seat END",
                (rec.call_id, rec.caller, rec.callee, rec.direction,
                 1 if rec.answered else 0, rec.duration_sec, rec.wait_sec, rec.start_epoch,
                 rec.agent, 1 if is_record else 0, kind, rec.recording_path, owner_seat),
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
                " recording_path, question, service_type, owner_seat, created_at"
                " FROM calls WHERE rowid=? AND is_record=1", (rowid,)).fetchone()
        if not r:
            return None
        return {
            "record_id": r[0], "call_id": r[1], "caller": r[2], "callee": r[3],
            "direction": r[4], "answered": bool(r[5]),
            "duration": fmt_duration(r[6] or 0), "duration_sec": r[6] or 0,
            "wait_sec": r[7] or 0, "time": fmt_cst(r[8] or 0),
            "agent": r[9] or "", "kind": r[10] or "电话",
            "has_recording": bool(r[11]), "recording_file": r[11] or "",
            "question": r[12] or "", "service_type": r[13] or "",
            "owner_seat": r[14] or "", "created_at": r[15] or "",
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
    if types:
        ranked = sorted(types.items(), key=lambda kv: -kv[1])
        parts.append("常见问题：" + "、".join(f"{k}×{v}" for k, v in ranked))
    latest = rows[0]
    parts.append(f"最近一次：{latest.get('kind') or '电话'}（{str(latest.get('time') or '')[:10]}）")
    return "；".join(parts)


# ------------------------------------------------------------------ bridge

@dataclass
class Bridge:
    store: CallStore
    # Seat numbers in roster order (env ROTATION_AGENTS, default the
    # three seats). Voicemail records are dispatched to the roster
    # member with the fewest handled contacts; seat numbers must match
    # the FreeSWITCH extensions so queue CDRs (cc_agent) line up.
    rotation_agents: list = field(default_factory=list)

    def _agent_statuses(self) -> dict:
        """Seat -> FreeSWITCH agent status (Available / On Break /
        Logged Out), via the event socket. {} when ESL is unreachable;
        callers then treat every seat as available."""
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
        statuses = {}
        for ln in lines[1:]:
            cols = [c.strip() for c in ln.split("|")]
            if len(cols) > max(name_i, status_i):
                statuses[cols[name_i]] = cols[status_i]
        return statuses

    def get_agent_statuses(self) -> dict:
        """Roster seats mapped to their queue status (or 'unknown'
        when ESL cannot be reached)."""
        statuses = self._agent_statuses()
        return {a: statuses.get(a, "unknown") for a in self.rotation_agents}

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

    def set_agent_busy(self, agent: str, busy: bool) -> tuple[bool, str]:
        """Set one seat On Break (busy) or Available, via the queue.
        Returns (ok, status-or-error)."""
        target = "On Break" if busy else "Available"
        out = esl_command(f"callcenter_config agent set status {agent} '{target}'")
        if out and out.strip().startswith("+OK"):
            return True, target
        return False, (out or "event socket unreachable").strip()[:200]

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
        past records for this number, excluding the current call."""
        try:
            past = self.store.calls_for_caller(rec.caller, rec.call_id)
            return summarize_call_history(past)
        except Exception:
            return ""

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
