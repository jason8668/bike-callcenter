import os
import queue
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bridge_core import (Bridge, BridgeEventListener,  # noqa: E402
                         BridgeEventPairing, CallRecord, CallStore,
                         can_edit_record, explicit_agent, fmt_cst,
                         fmt_duration, leg_seat, parse_cdr, record_owner,
                         summarize_call_history)

CDR = {
    "uuid": "abc-123", "caller_id_number": "13800000000",
    "destination_number": "4008000000", "billsec": "65", "duration": "75",
    "start_epoch": "1792800000", "ivr_choice": "1",
    "recording_path": "/var/lib/freeswitch/recordings/abc-123.wav",
    "bridge_ticket": "1",
}


class TestParse(unittest.TestCase):
    def test_parse_flat_form_cdr(self):
        rec = parse_cdr(CDR)
        self.assertEqual(rec.call_id, "abc-123")
        self.assertTrue(rec.answered)
        self.assertEqual(rec.wait_sec, 10)
        self.assertEqual(rec.group_name, "报修组")
        self.assertEqual(rec.ticket_type, "故障报修")

    def test_parse_nested_variables(self):
        rec = parse_cdr({"variables": {"uuid": "x1", "billsec": "0"}})
        self.assertEqual(rec.call_id, "x1")
        self.assertFalse(rec.answered)

    def test_parse_caller_effective_id_fallback(self):
        # Loopback/queue legs may leave caller_id_number empty while the
        # effective (or originate) caller ID carries the number.
        rec = parse_cdr({"variables": {
            "uuid": "x2", "billsec": "30",
            "effective_caller_id_number": "13800000000"}})
        self.assertEqual(rec.caller, "13800000000")
        rec = parse_cdr({"variables": {
            "uuid": "x3", "billsec": "30",
            "origination_caller_id_number": "13900000000"}})
        self.assertEqual(rec.caller, "13900000000")
        # A present caller_id_number still wins over the fallbacks.
        rec = parse_cdr({"variables": {
            "uuid": "x4", "billsec": "30",
            "caller_id_number": "13700000000",
            "effective_caller_id_number": "13800000000"}})
        self.assertEqual(rec.caller, "13700000000")


CALLER_EV = {"Unique-ID": "uuid-caller",
             "Bridge-A-Unique-ID": "uuid-caller",
             "Bridge-B-Unique-ID": "uuid-agent",
             "variable_bridge_ticket": "1"}
AGENT_EV = {"Unique-ID": "uuid-agent",
            "Bridge-A-Unique-ID": "uuid-caller",
            "Bridge-B-Unique-ID": "uuid-agent",
            "Channel-Name": "sofia/internal/8002@192.168.0.87",
            "Caller-Destination-Number": "8002"}


class TestBridgeEventPairing(unittest.TestCase):
    def test_caller_then_agent_resolves(self):
        p = BridgeEventPairing()
        self.assertIsNone(p.note(dict(CALLER_EV)))
        self.assertEqual(p.note(dict(AGENT_EV)),
                         ("uuid-caller", "8002", ""))

    def test_agent_then_caller_resolves(self):
        p = BridgeEventPairing()
        self.assertIsNone(p.note(dict(AGENT_EV)))
        self.assertEqual(p.note(dict(CALLER_EV)),
                         ("uuid-caller", "8002", ""))

    def test_pairing_carries_caller_from_event(self):
        p = BridgeEventPairing()
        caller = dict(CALLER_EV,
                      variable_origination_caller_id_number="13811223355")
        self.assertIsNone(p.note(caller))
        self.assertEqual(p.note(dict(AGENT_EV)),
                         ("uuid-caller", "8002", "13811223355"))

    def test_internal_call_never_maps(self):
        p = BridgeEventPairing()
        a = {"Unique-ID": "u1", "Bridge-A-Unique-ID": "u1",
             "Bridge-B-Unique-ID": "u2",
             "Channel-Name": "sofia/internal/8001@192.168.0.87"}
        b = {"Unique-ID": "u2", "Bridge-A-Unique-ID": "u1",
             "Bridge-B-Unique-ID": "u2",
             "Channel-Name": "sofia/internal/8002@192.168.0.87"}
        self.assertIsNone(p.note(a))
        self.assertIsNone(p.note(b))

    def test_testagent_leg_maps_no_seat(self):
        # The bridge still happened, so the caller gets an answer-time
        # record - just with no owner (test records stay editable).
        p = BridgeEventPairing()
        agent = dict(AGENT_EV, **{"Channel-Name": "loopback/8999",
                                  "Caller-Destination-Number": "8999"})
        self.assertIsNone(p.note(dict(CALLER_EV)))
        self.assertEqual(p.note(agent), ("uuid-caller", None, ""))

    def test_caller_cc_agent_resolves_alone(self):
        p = BridgeEventPairing()
        caller = dict(CALLER_EV, variable_cc_agent="8001")
        self.assertEqual(p.note(caller), ("uuid-caller", "8001", ""))

    def test_leg_seat_matching(self):
        self.assertEqual(
            leg_seat({"Channel-Name": "sofia/internal/8002@192.168.0.87"}),
            "8002")
        self.assertEqual(
            leg_seat({"Caller-Destination-Number": "8003"}), "8003")
        self.assertIsNone(leg_seat({"Channel-Name": "loopback/8999"}))
        self.assertIsNone(leg_seat({"Channel-Name": "loopback/8000"}))
        # Substring trap: 80011 must not match 8001.
        self.assertIsNone(
            leg_seat({"Channel-Name": "sofia/internal/80011@x"}))
        self.assertEqual(
            explicit_agent({"variable_cc_agent": "8001"}), "8001")
        self.assertIsNone(
            explicit_agent({"variable_cc_agent": "testagent"}))

    def test_listener_against_fake_esl(self):
        # End-to-end wire check: banner, auth, subscribe, two plain
        # bridge events -> the mapping lands in the store.
        tmp = tempfile.mkdtemp()
        bridge = make_bridge(tmp)
        port_holder = []
        subscribe_lines = []

        def fake_esl():
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", 0))
            port_holder.append(srv.getsockname()[1])
            srv.listen(1)
            conn, _ = srv.accept()
            conn.sendall(b"Content-Type: auth/request\n\n")
            buf = b""
            while b"\n\n" not in buf:
                buf += conn.recv(4096)
            conn.sendall(b"Content-Type: command/reply\n"
                         b"Reply-Text: +OK accepted\n\n")
            buf = b""
            while b"\n\n" not in buf:
                buf += conn.recv(4096)
            # The wire command is "event" (singular): FreeSWITCH has
            # no "events" command, and blindly +OKing any subscribe
            # once hid exactly that one-word bug from this test.
            subscribe_lines.append(buf.split(b"\n")[0].decode())
            conn.sendall(b"Content-Type: command/reply\n"
                         b"Reply-Text: +OK accepted\n\n")
            # Real ESL wire format: MIME envelope (Content-Length +
            # Content-Type), blank line, then the event headers as
            # the body. Sending everything in one block once hid a
            # parser that read the envelope and discarded the body.
            def event_frame(headers_block: bytes) -> bytes:
                body = headers_block + b"\n"
                return (b"Content-Length: " + str(len(body)).encode() +
                        b"\nContent-Type: text/event-plain\n\n" + body)
            caller_block = event_frame(
                b"Event-Name: CHANNEL_BRIDGE\n"
                b"Unique-ID: uuid-caller\n"
                b"Bridge-A-Unique-ID: uuid-caller\n"
                b"Bridge-B-Unique-ID: uuid-agent\n"
                b"variable_bridge_ticket: 1\n")
            agent_block = event_frame(
                b"Event-Name: CHANNEL_BRIDGE\n"
                b"Unique-ID: uuid-agent\n"
                b"Bridge-A-Unique-ID: uuid-caller\n"
                b"Bridge-B-Unique-ID: uuid-agent\n"
                b"Channel-Name: sofia/internal/8002@192.168.0.87\n"
                b"Caller-Destination-Number: 8002\n")
            conn.sendall(caller_block + agent_block)
            time.sleep(0.5)
            conn.close()
            srv.close()

        threading.Thread(target=fake_esl, daemon=True).start()
        deadline = time.time() + 5
        while not port_holder and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(port_holder, "fake ESL never bound")
        live = queue.Queue()
        listener = BridgeEventListener(
            bridge.store.db_path, host="127.0.0.1",
            port=port_holder[0], password="test", live_queue=live)
        listener.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            if bridge.store.agent_for_call("uuid-caller") == "8002":
                break
            time.sleep(0.05)
        self.assertEqual(bridge.store.agent_for_call("uuid-caller"),
                         "8002")
        self.assertEqual(subscribe_lines, ["event plain CHANNEL_BRIDGE"])
        item = live.get(timeout=2)
        self.assertEqual(item["call_id"], "uuid-caller")
        self.assertEqual(item["seat"], "8002")


def make_bridge(tmp, roster=None):
    return Bridge(store=CallStore(os.path.join(tmp, "b.db")),
                  rotation_agents=roster if roster is not None
                  else ["8001", "8002", "8003"])


class TestBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bridge = make_bridge(self.tmp)

    def test_fmt_duration_and_cst(self):
        self.assertEqual(fmt_duration(91), "1分31秒")
        self.assertEqual(fmt_duration(42), "0分42秒")
        self.assertEqual(fmt_duration(0), "0分00秒")
        self.assertEqual(fmt_cst(0), "")
        self.assertTrue(fmt_cst(1792800000).startswith("2026-"))

    def test_recent_records_newest_first_and_only_records(self):
        self.bridge.handle_cdr(CDR)  # record, caller 13800000000
        # A statistics-only leg (internal call) must not appear.
        self.bridge.store.save_call(
            CallRecord(call_id="stat-1", caller="13700000000",
                       callee="", direction="in"), False)
        rows = self.bridge.store.recent_records(10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["caller"], "13800000000")
        self.assertEqual(rows[0]["kind"], "电话")
        self.assertTrue(rows[0]["rowid"] > 0)
        self.assertEqual(rows[0]["agent"], "")

    def test_recent_records_carry_owning_seat_for_directed_pop(self):
        # Answered queue call: the CDR's cc_agent is the owning seat.
        self.bridge.handle_cdr(dict(CDR, uuid="seat-1",
                                    cc_agent="8002"))
        rows = self.bridge.store.recent_records(10)
        self.assertEqual(rows[0]["agent"], "8002")

    def test_cdr_agent_from_live_bridge_event(self):
        # The queue CDR carries no seat; the live bridge event mapping
        # fills it, and a CDR-named seat still wins over the mapping.
        self.bridge.store.note_agent("abc-123", "8002")
        out = self.bridge.handle_cdr(dict(CDR))
        rec = self.bridge.store.get_record(out["record_id"])
        self.assertEqual(rec["agent"], "8002")
        self.bridge.store.note_agent("abc-124", "8002")
        out = self.bridge.handle_cdr(dict(CDR, uuid="abc-124",
                                          cc_agent="8001"))
        rec = self.bridge.store.get_record(out["record_id"])
        self.assertEqual(rec["agent"], "8001")

    def test_save_call_fills_empty_caller_and_keeps_agent(self):
        # Answer-time record from a bridge event (no caller yet, seat
        # known); the hangup CDR completes it without clobbering.
        rec = CallRecord(call_id="live-1", caller="", direction="in",
                         answered=True, agent="8002", ticket_flag=True)
        rowid = self.bridge.store.save_call(rec, True, kind="电话")
        out = self.bridge.handle_cdr(dict(
            CDR, uuid="live-1", caller_id_number="13811223355",
            billsec="20", duration="25"))
        self.assertEqual(out["record_id"], rowid)
        got = self.bridge.store.get_record(rowid)
        self.assertEqual(got["caller"], "13811223355")
        self.assertEqual(got["agent"], "8002")
        # A later empty-caller leg must not wipe either field.
        self.bridge.handle_cdr(dict(
            CDR, uuid="live-1", caller_id_number="",
            billsec="20", duration="25"))
        got = self.bridge.store.get_record(rowid)
        self.assertEqual(got["caller"], "13811223355")
        self.assertEqual(got["agent"], "8002")

    def test_drain_live_calls_creates_answer_time_record(self):
        bridge = self.bridge
        bridge.live_calls.put({"call_id": "live-2", "seat": "8001",
                               "caller": "13811223355",
                               "started_at": 1792800000})
        bridge.drain_live_calls()
        rows = bridge.store.recent_records(10)
        self.assertEqual(rows[0]["call_id"], "live-2")
        self.assertEqual(rows[0]["agent"], "8001")
        self.assertEqual(rows[0]["caller"], "13811223355")
        # Draining again is a no-op (queue empty, upsert idempotent).
        bridge.drain_live_calls()
        self.assertEqual(len(bridge.store.recent_records(10)), 1)

    def test_cdr_agent_empty_without_mapping(self):
        out = self.bridge.handle_cdr(dict(CDR))
        rec = self.bridge.store.get_record(out["record_id"])
        self.assertEqual(rec["agent"], "")

    def test_record_owner_and_edit_permission(self):
        # Only the owning seat may modify; others view only.
        self.assertEqual(
            record_owner({"agent": "8001", "owner_seat": ""}), "8001")
        self.assertTrue(
            can_edit_record({"agent": "8001", "owner_seat": ""}, "8001"))
        self.assertFalse(
            can_edit_record({"agent": "8001", "owner_seat": ""}, "8002"))
        # Voicemail: the dispatched owner seat owns it.
        self.assertEqual(
            record_owner({"agent": "", "owner_seat": "8002"}), "8002")
        self.assertFalse(
            can_edit_record({"agent": "", "owner_seat": "8002"}, "8001"))
        # Missed calls (no seat) and FS test-agent calls stay editable
        # by any seat.
        self.assertEqual(record_owner({"agent": "", "owner_seat": ""}), "")
        self.assertTrue(
            can_edit_record({"agent": "", "owner_seat": ""}, "8001"))
        self.assertEqual(
            record_owner({"agent": "testagent", "owner_seat": ""}), "")
        self.assertTrue(
            can_edit_record({"agent": "testagent", "owner_seat": ""},
                            "8002"))

    def test_answered_call_creates_record_with_recording(self):
        out = self.bridge.handle_cdr(CDR)
        self.assertTrue(out["ok"])
        self.assertTrue(out["has_recording"])
        rec = self.bridge.store.get_record(out["record_id"])
        self.assertEqual(rec["caller"], "13800000000")
        self.assertEqual(rec["duration"], "1分05秒")
        self.assertEqual(rec["recording_file"],
                         "/var/lib/freeswitch/recordings/abc-123.wav")
        self.assertTrue(rec["has_recording"])
        self.assertEqual(len(self.bridge.store.list_records()), 1)

    def test_caller_history_excludes_self_and_stats_legs(self):
        self.bridge.handle_cdr(dict(CDR))  # 13800000000, answered
        self.bridge.handle_cdr(dict(CDR, uuid="b2", billsec="0",
                                    duration="5"))  # same caller, missed
        out3 = self.bridge.handle_cdr(dict(
            CDR, uuid="c3", caller_id_number="13900000000"))
        self.bridge.store.save_call(
            CallRecord(call_id="stat-2", caller="13800000000",
                       callee="", direction="in"), False)
        out_cur = self.bridge.handle_cdr(dict(CDR, uuid="cur1"))
        self.bridge.store.set_record_card(out3["record_id"], "K100")
        self.bridge.store.set_record_card(out_cur["record_id"], "K100")
        hist = self.bridge.caller_history(out_cur["record_id"])
        self.assertEqual(hist["total"], 3)
        self.assertEqual([r["kind"] for r in hist["rows"]],
                         ["电话", "未接", "电话"])  # newest first
        self.assertTrue(all(r["record_id"] != out_cur["record_id"]
                            for r in hist["rows"]))
        self.assertTrue(hist["rows"][0]["via_card"])  # 139 number, same card
        self.assertFalse(hist["rows"][1]["via_card"])
        self.assertTrue(all(r["recording_file"] for r in hist["rows"]))
        self.assertEqual(self.bridge.caller_history(999999),
                         {"total": 0, "rows": [],
                          "portrait": self.bridge.store.caller_portrait(
                              "", "")})

    def test_caller_portrait_aggregates_full_history(self):
        out1 = self.bridge.handle_cdr(dict(CDR))  # 138, answered
        out2 = self.bridge.handle_cdr(dict(CDR, uuid="b2", billsec="0",
                                           duration="5"))  # same caller, missed
        out3 = self.bridge.handle_cdr(dict(
            CDR, uuid="c3", caller_id_number="13900000000"))
        out_cur = self.bridge.handle_cdr(dict(CDR, uuid="cur1"))
        self.bridge.store.set_notes(out1["record_id"], "锁桩打不开", "锁桩故障")
        self.bridge.store.set_notes(out3["record_id"], "还车失败", "还车异常")
        self.bridge.store.set_notes(out_cur["record_id"], "当前通话", "锁桩故障")
        self.bridge.store.set_record_card(out3["record_id"], "K100")
        self.bridge.store.set_record_card(out_cur["record_id"], "K100")
        self.bridge.store.add_record_tag(out1["record_id"], "重点关注")
        self.bridge.store.add_record_tag(out1["record_id"], "老年用户")
        self.bridge.store.add_record_tag(out3["record_id"], "重点关注")
        self.bridge.store.add_record_tag(out3["record_id"], "高频用户")
        self.bridge.store.add_record_tag(out2["record_id"], "高频用户")
        p = self.bridge.store.caller_portrait("13800000000", "K100", "cur1")
        self.assertEqual(p["total"], 3)
        self.assertEqual(p["by_kind"], {"电话": 2, "未接": 1, "留言": 0})
        self.assertEqual(p["top_types"],
                         [{"type": "还车异常", "count": 1},
                          {"type": "锁桩故障", "count": 1}])
        self.assertEqual(p["last"]["kind"], "电话")
        self.assertEqual(p["last"]["service_type"], "还车异常")
        self.assertEqual(p["via_card"], 1)
        self.assertTrue(p["first_time"])
        # Top 2 tags by frequency across the customer's past records;
        # the count-1 tag drops out, ties break by tag name.
        self.assertEqual(p["top_tags"], ["重点关注", "高频用户"])
        hist = self.bridge.caller_history(out_cur["record_id"])
        self.assertEqual(hist["portrait"]["total"], 3)
        self.assertEqual(self.bridge.store.caller_portrait("", ""),
                         {"total": 0,
                          "by_kind": {"电话": 0, "未接": 0, "留言": 0},
                          "first_time": "", "last": None, "top_types": [],
                          "top_tags": [], "via_card": 0})

    def test_duplicate_cdr_is_idempotent(self):
        first = self.bridge.handle_cdr(CDR)
        second = self.bridge.handle_cdr(CDR)
        self.assertEqual(first["record_id"], second["record_id"])
        self.assertEqual(len(self.bridge.store.list_records()), 1)

    def test_answer_event_opens_record_during_call(self):
        """The record exists at answer time (no recording yet), so the
        agent can take notes while still on the phone; the hangup CDR
        fills in duration/recording without wiping those notes."""
        out = self.bridge.handle_event("answer", {
            "uuid": "live-1", "caller_id_number": "13800000000",
            "destination_number": "4008000000", "direction": "inbound",
            "start_epoch": "1792800000", "bridge_ticket": "1"})
        self.assertTrue(out["ok"])
        self.assertTrue(out["record_id"])
        self.bridge.store.set_notes(out["record_id"], "还不了车", "还车异常")
        done = self.bridge.handle_cdr({**CDR, "uuid": "live-1"})
        self.assertEqual(done["record_id"], out["record_id"])
        rec = self.bridge.store.get_record(out["record_id"])
        self.assertEqual(rec["duration"], "1分05秒")
        self.assertEqual(rec["question"], "还不了车")
        self.assertEqual(rec["service_type"], "还车异常")
        again = self.bridge.handle_cdr({**CDR, "uuid": "live-1"})
        self.assertEqual(again["record_id"], out["record_id"])
        self.assertEqual(len(self.bridge.store.list_records()), 1)

    def test_newcall_alone_creates_no_record(self):
        """Ringing (newCall) alone opens nothing; the record opens on
        answer, and a missed call's record comes from the CDR."""
        out = self.bridge.handle_event("newCall", {
            "uuid": "ring-1", "caller_id_number": "13800000000",
            "destination_number": "4008000000", "direction": "inbound",
            "start_epoch": "1792800000"})
        self.assertTrue(out["ok"])
        self.assertNotIn("record_id", out)
        self.assertEqual(self.bridge.store.recent_records(10), [])

    def test_non_record_legs_create_no_record(self):
        """Internal extension calls, outbound legs and voicemail legs
        must never own a record (statistics only)."""
        internal = self.bridge.handle_cdr(
            {**CDR, "uuid": "int-1", "destination_number": "8002",
             "bridge_ticket": ""})
        self.assertIsNone(internal["record_id"])
        outbound = self.bridge.handle_cdr(
            {**CDR, "uuid": "out-1", "direction": "outbound"})
        self.assertIsNone(outbound["record_id"])
        vm_leg = self.bridge.handle_cdr(
            {**CDR, "uuid": "vmleg-1", "bridge_ticket": "",
             "bridge_voicemail": "1"})
        self.assertIsNone(vm_leg["record_id"])
        no_flag = self.bridge.handle_event("answer", {
            "uuid": "noflag-1", "caller_id_number": "13800000000",
            "destination_number": "4008000000", "direction": "inbound"})
        self.assertNotIn("record_id", no_flag)
        self.assertEqual(self.bridge.store.recent_records(10), [])

    def test_missed_call_is_kept_as_record(self):
        """Unlike the old ticket system, a missed queue call IS kept -
        kind 未接， so the callback list is visible. It never counts
        as an answered contact."""
        out = self.bridge.handle_cdr({**CDR, "uuid": "miss-1", "billsec": "0",
                                      "duration": "20"})
        self.assertTrue(out["ok"])
        self.assertTrue(out["record_id"])
        rec = self.bridge.store.get_record(out["record_id"])
        self.assertEqual(rec["kind"], "未接")
        self.assertFalse(rec["answered"])
        self.assertEqual(self.bridge.store.agent_stats("8001", 0)["calls_total"], 0)

    def test_notes_and_search(self):
        call = self.bridge.handle_cdr(CDR)
        self.bridge.store.set_notes(call["record_id"], "还不了车", "还车异常")
        vm = self.bridge.handle_voicemail({
            "uuid": "vm-9", "caller_id_number": "13900000000",
            "destination_number": "4008000000", "billsec": "8", "duration": "25",
            "start_epoch": "1792800000",
            "recording_path": "/var/lib/freeswitch/recordings/vm-9.wav"})
        self.assertTrue(vm["record_id"])
        self.assertEqual(
            [r["record_id"] for r in self.bridge.store.list_records(caller="138")],
            [call["record_id"]])
        self.assertEqual(
            [r["record_id"] for r in self.bridge.store.list_records(kind="留言")],
            [vm["record_id"]])
        self.assertEqual(
            self.bridge.store.list_records(agent="8002"), [])
        got = self.bridge.store.get_record(call["record_id"])
        self.assertEqual(got["question"], "还不了车")
        self.assertEqual(got["service_type"], "还车异常")
        self.assertFalse(self.bridge.store.set_notes(999999, "x", "y"))
        # Date bounds (Asia/Shanghai): the 2026 records fall outside
        # a 2020 window and inside a 2026 window.
        self.assertEqual(
            self.bridge.store.list_records(date_from="2020-01-01",
                                           date_to="2020-12-31"), [])
        self.assertEqual(
            len(self.bridge.store.list_records(date_from="2026-01-01",
                                               date_to="2026-12-31")), 2)


VM = {
    "uuid": "vm-123", "caller_id_number": "13900000000",
    "destination_number": "4008000000", "billsec": "8", "duration": "25",
    "start_epoch": "1792800000",
    "recording_path": "/var/lib/freeswitch/recordings/vm-123.wav",
}


class TestVoicemail(unittest.TestCase):
    def setUp(self):
        import bridge_core
        self._orig_esl = bridge_core.esl_command
        bridge_core.esl_command = lambda cmd: None  # ESL down: all available
        self.addCleanup(setattr, bridge_core, "esl_command", self._orig_esl)
        self.bridge = make_bridge(tempfile.mkdtemp())

    def test_voicemail_creates_record(self):
        out = self.bridge.handle_voicemail(VM)
        self.assertTrue(out["ok"])
        self.assertTrue(out["voicemail"])
        self.assertTrue(out["record_id"])
        self.assertEqual(out["owner_seat"], "8001")
        rec = self.bridge.store.get_record(out["record_id"])
        self.assertEqual(rec["kind"], "留言")
        self.assertEqual(rec["duration"], "0分08秒")
        self.assertTrue(rec["has_recording"])

    def test_voicemail_is_idempotent(self):
        first = self.bridge.handle_voicemail(VM)
        second = self.bridge.handle_voicemail(VM)
        self.assertEqual(first["record_id"], second["record_id"])
        self.assertEqual(len(self.bridge.store.list_records()), 1)


class TestHistoryHint(unittest.TestCase):
    def test_summarize_call_history(self):
        rows = [
            {"time": "2026-10-20 09:00:00", "kind": "电话",
             "service_type": "还车异常", "question": "还不了车"},
            {"time": "2026-10-19 09:00:00", "kind": "电话",
             "service_type": "掉链", "question": ""},
            {"time": "2026-10-18 09:00:00", "kind": "电话",
             "service_type": "还车异常", "question": ""},
        ]
        note = summarize_call_history(rows)
        self.assertIn("历史来电 3 次", note)
        self.assertIn("还车异常×2", note)
        self.assertIn("掉链×1", note)
        self.assertIn("2026-10-20", note)
        self.assertEqual(summarize_call_history([]), "")

    def test_returning_caller_record_carries_hint(self):
        """History is found by caller NUMBER in the bridge's own record
        store: a second call from the same number carries the first
        record's service type."""
        bridge = make_bridge(tempfile.mkdtemp())
        first = bridge.handle_cdr(CDR)
        bridge.store.set_notes(first["record_id"], "还不了车", "还车异常")
        out = bridge.handle_cdr({**CDR, "uuid": "repeat-1"})
        self.assertTrue(out["ok"])
        self.assertIn("还车异常×1", out["history_hint"])
        self.assertIn("历史来电 1 次", out["history_hint"])


class TestVoicemailDistribution(unittest.TestCase):
    def setUp(self):
        import bridge_core
        self._orig_esl = bridge_core.esl_command
        bridge_core.esl_command = lambda cmd: None  # ESL down: all available
        self.addCleanup(setattr, bridge_core, "esl_command", self._orig_esl)
        self.bridge = make_bridge(tempfile.mkdtemp())

    def test_fewest_calls_gets_next_voicemail(self):
        """8001 answered 2 calls; the next voicemails go 8002, then
        8003, then 8002 again once 8002/8003 tie at one each."""
        for i in (1, 2):
            out = self.bridge.handle_cdr({**CDR, "uuid": f"call-{i}",
                                          "caller_id_number": f"1380000000{i}",
                                          "cc_agent": "8001"})
            self.assertTrue(out["ok"])
        owners = []
        for i, uuid in enumerate(("vm-a", "vm-b", "vm-c")):
            out = self.bridge.handle_voicemail({**VM, "uuid": uuid,
                                                "caller_id_number": f"1390000000{i}"})
            self.assertTrue(out["ok"])
            owners.append(out["owner_seat"])
        self.assertEqual(owners, ["8002", "8003", "8002"])

    def test_busy_seat_is_skipped(self):
        import bridge_core
        bridge_core.esl_command = lambda cmd: (
            "name|type|contact|status|state\n"
            "8001|callback|user/8001|On Break|Waiting\n"
            "8002|callback|user/8002|Available|Waiting\n"
            "8003|callback|user/8003|Available|Waiting\n")
        out = self.bridge.handle_voicemail({**VM, "uuid": "vm-busy"})
        self.assertEqual(out["owner_seat"], "8002")

    def test_queue_available_without_login_shows_logged_out(self):
        import bridge_core
        self._orig_esl = bridge_core.esl_command
        self.addCleanup(setattr, bridge_core, "esl_command",
                        self._orig_esl)
        all_available = (
            "name|type|contact|status|state\n"
            "8001|callback|user/8001|Available|Waiting\n"
            "8002|callback|user/8002|Available|Waiting\n"
            "8003|callback|user/8003|Available|Waiting\n")
        bridge_core.esl_command = lambda cmd: all_available
        # Only 8001 has a live desk session; 8002/8003 never logged in.
        self.bridge.store.create_session("8001")
        statuses, connected = self.bridge.statuses_with_source()
        self.assertTrue(connected)
        self.assertEqual(statuses["8001"], "Available")
        self.assertEqual(statuses["8002"], "Logged Out")
        self.assertEqual(statuses["8003"], "Logged Out")
        # An in-call seat proves presence by itself.
        bridge_core.esl_command = lambda cmd: (
            "name|type|contact|status|state\n"
            "8001|callback|user/8001|Available|In Call\n"
            "8002|callback|user/8002|Available|Waiting\n"
            "8003|callback|user/8003|Available|Waiting\n")
        statuses, _ = self.bridge.statuses_with_source()
        self.assertEqual(statuses["8001"], "On Call")
        # Desk busy wins even when the queue says Available.
        self.bridge.store.set_busy_flag("8001", True)
        bridge_core.esl_command = lambda cmd: all_available
        statuses, _ = self.bridge.statuses_with_source()
        self.assertEqual(statuses["8001"], "On Break")

    def test_all_busy_leaves_record_unowned(self):
        import bridge_core
        bridge_core.esl_command = lambda cmd: (
            "name|type|contact|status|state\n"
            "8001|callback|user/8001|On Break|Waiting\n"
            "8002|callback|user/8002|On Break|Waiting\n"
            "8003|callback|user/8003|On Break|Waiting\n")
        out = self.bridge.handle_voicemail({**VM, "uuid": "vm-allbusy"})
        self.assertTrue(out["ok"])
        self.assertIsNone(out["owner_seat"])

    def test_roster_order_breaks_ties(self):
        """No account lookup anymore: any roster seat can own a
        record, ties go by roster order, and the count advances."""
        self.bridge.rotation_agents = ["8009", "8001"]
        first = self.bridge.handle_voicemail({**VM, "uuid": "vm-u1"})
        second = self.bridge.handle_voicemail({**VM, "uuid": "vm-u2"})
        self.assertEqual(first["owner_seat"], "8009")
        self.assertEqual(second["owner_seat"], "8001")

    def test_no_roster_leaves_record_unowned(self):
        self.bridge.rotation_agents = []
        out = self.bridge.handle_voicemail({**VM, "uuid": "vm-noroster"})
        self.assertTrue(out["ok"])
        self.assertIsNone(out["owner_seat"])


class TestAgentHistory(unittest.TestCase):
    def setUp(self):
        import bridge_core
        self._orig_esl = bridge_core.esl_command
        bridge_core.esl_command = lambda cmd: None
        self.addCleanup(setattr, bridge_core, "esl_command", self._orig_esl)
        self.bridge = make_bridge(tempfile.mkdtemp())

    def test_stats_and_history_are_per_seat(self):
        now = str(int(time.time()))
        for i in (1, 2):
            self.bridge.handle_cdr({**CDR, "uuid": f"today-{i}",
                                    "caller_id_number": f"1380000000{i}",
                                    "cc_agent": "8001", "start_epoch": now})
        self.bridge.handle_cdr({**CDR, "uuid": "old-1",
                                "caller_id_number": "13800000009",
                                "cc_agent": "8001", "start_epoch": "1577836800"})
        self.bridge.handle_cdr({**CDR, "uuid": "other-seat",
                                "caller_id_number": "13800000008",
                                "cc_agent": "8002", "start_epoch": now})
        # Fewest handled wins: 8001 has 3 calls, 8002 has 1, 8003 has 0.
        vm = self.bridge.handle_voicemail({**VM, "uuid": "vm-h1"})
        self.assertEqual(vm["owner_seat"], "8003")
        mine = self.bridge.get_agent_history("8001")
        self.assertEqual(mine["stats"]["calls_today"], 2)
        self.assertEqual(mine["stats"]["calls_total"], 3)
        self.assertEqual(mine["stats"]["voicemails"], 0)
        self.assertEqual(len(mine["history"]), 3)
        self.assertTrue(all(r["kind"] == "电话" for r in mine["history"]))
        self.assertTrue(all(r["record_id"] for r in mine["history"]))
        other = self.bridge.get_agent_history("8002")
        self.assertEqual(other["stats"]["calls_today"], 1)
        self.assertEqual(other["stats"]["voicemails"], 0)
        self.assertTrue(all(r["kind"] == "电话" for r in other["history"]))
        self.assertNotIn("13800000009", [r["caller"] for r in other["history"]])
        third = self.bridge.get_agent_history("8003")
        self.assertEqual(third["stats"]["voicemails"], 1)
        self.assertEqual(len(third["history"]), 1)
        self.assertEqual(third["history"][0]["kind"], "留言")


class TestSeatWebStatus(unittest.TestCase):
    """System-side status: login state, not the queue, when ESL is down."""

    def setUp(self):
        import bridge_core
        self.tmp = tempfile.mkdtemp()
        self.bridge = make_bridge(self.tmp)
        self.commands = []
        self.table = ("name|status\n"
                      "8001|Available\n8002|On Break\n8003|Logged Out\n")

        def stub(cmd):
            self.commands.append(cmd)
            if cmd.startswith("callcenter_config agent list"):
                return self.table
            if cmd.startswith("callcenter_config agent set status"):
                return "+OK"
            return None

        self._orig_esl = bridge_core.esl_command
        bridge_core.esl_command = stub
        self.addCleanup(setattr, bridge_core, "esl_command", self._orig_esl)

    def test_web_status_from_login_state(self):
        store = self.bridge.store
        self.assertEqual(self.bridge.seat_web_status("8001"), "Logged Out")
        store.create_session("8001")
        self.assertEqual(self.bridge.seat_web_status("8001"), "Available")
        store.set_busy_flag("8001", True)
        self.assertEqual(self.bridge.seat_web_status("8001"), "On Break")
        store.set_busy_flag("8001", False)
        self.assertEqual(self.bridge.seat_web_status("8001"), "Available")

    def test_statuses_fall_back_to_web_when_esl_down(self):
        import bridge_core
        bridge_core.esl_command = lambda cmd: None
        self.bridge.store.create_session("8001")
        statuses, connected = self.bridge.statuses_with_source()
        self.assertFalse(connected)
        self.assertEqual(statuses, {"8001": "Available", "8002": "Logged Out",
                                    "8003": "Logged Out"})

    def test_statuses_prefer_queue_when_reachable(self):
        self.bridge.store.create_session("8001")
        statuses, connected = self.bridge.statuses_with_source()
        self.assertTrue(connected)
        self.assertEqual(statuses["8002"], "On Break")  # queue wins over web
        self.assertEqual(statuses["8003"], "Logged Out")

    def test_set_seat_busy_records_flag_when_esl_down(self):
        import bridge_core
        bridge_core.esl_command = lambda cmd: None
        status, queue_updated = self.bridge.set_seat_busy("8001", True)
        self.assertEqual(status, "On Break")
        self.assertFalse(queue_updated)
        self.assertTrue(self.bridge.store.get_busy_flag("8001"))

    def test_set_seat_busy_raises_on_real_queue_error(self):
        import bridge_core
        bridge_core.esl_command = lambda cmd: "-ERR no such agent"
        with self.assertRaises(ValueError):
            self.bridge.set_seat_busy("8001", True)
        self.assertFalse(self.bridge.store.get_busy_flag("8001"))


class TestRangeStats(unittest.TestCase):
    """Stats/history windows: today/week/month/custom, table-based."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bridge = make_bridge(self.tmp)
        self.base = 1792800000  # 2026-10-27-ish epoch base for windows
        with sqlite3.connect(os.path.join(self.tmp, "b.db")) as c:
            rows = [
                # (call_id, caller, answered, agent, owner, kind, start)
                ("c1", "13800000001", 1, "8001", "", "电话", self.base + 100),
                ("c2", "13800000002", 1, "8001", "", "电话", self.base + 200),
                ("c3", "13800000003", 1, "8002", "", "电话", self.base + 300),
                ("old", "13800000009", 1, "8001", "", "电话", self.base - 90000),
                ("v1", "13900000001", 1, "", "8001", "留言", self.base + 400),
                ("v2", "13900000002", 1, "", "8001", "留言", self.base - 90000),
            ]
            for cid, caller, ans, agent, owner, kind, start in rows:
                c.execute(
                    "INSERT INTO calls (call_id, caller, answered, agent,"
                    " owner_seat, is_record, kind, start_epoch, duration_sec)"
                    " VALUES (?,?,?,?,?,1,?,?,65)",  # is_record=1, dur=65
                    (cid, caller, ans, agent, owner, kind, start))

    def test_stats_range_counts_only_window(self):
        s = self.bridge.store.agent_stats_range("8001", self.base, self.base + 1000)
        self.assertEqual(s, {"calls": 2, "voicemails": 1})
        s = self.bridge.store.agent_stats_range("8001", self.base - 100000,
                                                self.base - 80000)
        self.assertEqual(s, {"calls": 1, "voicemails": 1})
        s = self.bridge.store.agent_stats_range("8002", self.base, self.base + 1000)
        self.assertEqual(s, {"calls": 1, "voicemails": 0})

    def test_history_range_filters_and_orders(self):
        h = self.bridge.store.agent_history_range("8001", self.base, self.base + 1000)
        self.assertEqual([r["caller"] for r in h],
                         ["13900000001", "13800000002", "13800000001"])
        self.assertTrue(all(r["record_id"] for r in h))
        h = self.bridge.store.agent_history_range("8001", self.base - 100000,
                                                  self.base - 80000)
        self.assertEqual(len(h), 2)  # one old call + one old voicemail


class TestCallerCardsTags(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bridge = make_bridge(self.tmp)

    def _call(self, uuid, caller, start="1792800000"):
        self.bridge.handle_cdr({**CDR, "uuid": uuid,
                                "caller_id_number": caller,
                                "start_epoch": start})
        return self.bridge.store.get_record_rowid(uuid)

    def test_card_auto_carry_and_update(self):
        r1 = self._call("card-1", "13800000001")
        self.assertEqual(self.bridge.store.get_record(r1)["card"], "")
        self.assertTrue(self.bridge.store.set_record_card(r1, "K1001"))
        self.assertEqual(self.bridge.store.get_caller_card("13800000001"), "K1001")
        r2 = self._call("card-2", "13800000001", start="1792800100")
        self.assertEqual(self.bridge.store.get_record(r2)["card"], "K1001")
        # Clearing drops the association.
        self.assertTrue(self.bridge.store.set_record_card(r2, ""))
        self.assertEqual(self.bridge.store.get_caller_card("13800000001"), "")
        self.assertFalse(self.bridge.store.set_record_card(999999, "K1"))

    def test_tags_and_frequent(self):
        r1 = self._call("tag-1", "13800000002")
        r2 = self._call("tag-2", "13800000002", start="1792800100")
        store = self.bridge.store
        self.assertTrue(store.add_record_tag(r1, "高频用户"))
        self.assertTrue(store.add_record_tag(r1, "需回访"))
        self.assertTrue(store.add_record_tag(r2, "高频用户"))
        self.assertTrue(store.add_record_tag(r2, "自定义标签"))
        self.assertFalse(store.add_record_tag(999999, "高频用户"))
        self.assertFalse(store.add_record_tag(r1, "  "))
        self.assertEqual(store.get_record_tags(r1), ["需回访", "高频用户"])
        top = store.caller_top_tags("13800000002")
        self.assertEqual(top[0], {"tag": "高频用户", "count": 2})
        self.assertTrue(store.remove_record_tag(r1, "需回访"))
        self.assertFalse(store.remove_record_tag(r1, "需回访"))
        self.assertEqual(store.get_record_tags(r1), ["高频用户"])

    def test_history_by_phone_or_card(self):
        r1 = self._call("hist-1", "13800000003")
        r2 = self._call("hist-2", "13800000003", start="1792800100")
        r3 = self._call("hist-3", "13900000003", start="1792800200")
        store = self.bridge.store
        store.set_record_card(r1, "K2002")
        store.set_record_card(r3, "K2002")  # same card, another number
        rows = store.calls_for_user("13800000003", "K2002",
                                    exclude_call_id="hist-2")
        self.assertEqual(sorted(r["caller"] for r in rows),
                         ["13800000003", "13900000003"])
        self.assertEqual(len([r for r in rows if r["via_card"]]), 1)
        hint = self.bridge.record_history_hint(r2)
        self.assertIn("同卡号他号来电 1 次", hint)
        self.assertEqual(store.calls_for_user("", ""), [])


if __name__ == "__main__":
    unittest.main()


import sqlite3


def _backdate_session(tmp, token, created_ago=0, seen_ago=0):
    now = int(time.time())
    with sqlite3.connect(os.path.join(tmp, "b.db")) as c:
        c.execute("UPDATE sessions SET created_at=?, last_seen=? WHERE token=?",
                  (now - created_ago, now - seen_ago, token))


class TestSeatAccounts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bridge = make_bridge(self.tmp)

    def test_password_verify_reset_and_hashing(self):
        store = self.bridge.store
        self.assertFalse(store.has_agents())
        store.set_agent_password("8001", "pw-one")
        self.assertTrue(store.has_agents())
        self.assertTrue(store.verify_agent("8001", "pw-one"))
        self.assertFalse(store.verify_agent("8001", "wrong"))
        self.assertFalse(store.verify_agent("8002", "pw-one"))
        self.assertFalse(store.verify_agent("8001", ""))
        with sqlite3.connect(os.path.join(self.tmp, "b.db")) as c:
            salt, pw_hash = c.execute(
                "SELECT salt, pw_hash FROM agents WHERE login='8001'").fetchone()
        self.assertTrue(salt and pw_hash and pw_hash != "pw-one")
        store.set_agent_password("8001", "pw-two")
        self.assertFalse(store.verify_agent("8001", "pw-one"))
        self.assertTrue(store.verify_agent("8001", "pw-two"))

    def test_session_lifecycle_and_expiry(self):
        store = self.bridge.store
        token = store.create_session("8001")
        self.assertEqual(store.get_session_seat(token), "8001")
        self.assertEqual(store.get_session_seat("nope"), None)
        self.assertEqual(store.delete_session(token), "8001")
        self.assertEqual(store.get_session_seat(token), None)
        self.assertEqual(store.delete_session(token), None)
        old = store.create_session("8002")
        _backdate_session(self.tmp, old, created_ago=13 * 3600)
        self.assertEqual(store.get_session_seat(old), None)

    def test_live_and_any_session_sets_and_purge(self):
        store = self.bridge.store
        live = store.create_session("8001")
        stale = store.create_session("8002")
        _backdate_session(self.tmp, stale, seen_ago=700)
        self.assertEqual(store.seats_with_live_sessions(), {"8001"})
        self.assertEqual(store.seats_with_any_session(), {"8001", "8002"})
        _backdate_session(self.tmp, stale, created_ago=13 * 3600, seen_ago=700)
        store.purge_expired_sessions()
        self.assertEqual(store.seats_with_any_session(), {"8001"})
        self.assertEqual(store.get_session_seat(live), "8001")


class TestAgentStatus(unittest.TestCase):
    def setUp(self):
        import bridge_core
        self.tmp = tempfile.mkdtemp()
        self.bridge = make_bridge(self.tmp)
        self.commands = []
        self.table = ("name|status\n"
                      "8001|Available\n8002|On Break\n8003|Logged Out\n")

        def stub(cmd):
            self.commands.append(cmd)
            if cmd.startswith("callcenter_config agent list"):
                return self.table
            if cmd.startswith("callcenter_config agent set status"):
                return "+OK"
            return None

        self._orig_esl = bridge_core.esl_command
        bridge_core.esl_command = stub
        self.addCleanup(setattr, bridge_core, "esl_command", self._orig_esl)

    def test_set_agent_status_validates_and_reports(self):
        ok, status = self.bridge.set_agent_status("8001", "On Break")
        self.assertTrue(ok and status == "On Break")
        self.assertTrue(any("On Break" in c for c in self.commands))
        ok, _ = self.bridge.set_agent_status("8001", "Busy")
        self.assertFalse(ok)

    def test_set_agent_status_esl_down(self):
        import bridge_core
        bridge_core.esl_command = lambda cmd: None
        ok, _ = self.bridge.set_agent_status("8001", "Available")
        self.assertFalse(ok)

    def test_ensure_online_only_revives_logged_out_with_live_session(self):
        store = self.bridge.store
        store.create_session("8003")  # live: Logged Out revives to Available
        self.assertTrue(self.bridge.ensure_online("8003"))
        self.assertTrue(any("8003" in c and "Available" in c for c in self.commands))
        store.create_session("8002")  # live: On Break untouched
        store.create_session("8001")  # live: Available untouched
        before = len(self.commands)
        self.assertFalse(self.bridge.ensure_online("8002"))
        self.assertTrue(self.bridge.ensure_online("8001"))
        self.assertEqual(len(self.commands), before + 2)  # two list calls only

    def test_ensure_online_restores_busy_flag(self):
        store = self.bridge.store
        store.create_session("8003")
        store.set_busy_flag("8003", True)
        self.assertFalse(self.bridge.ensure_online("8003"))  # ends On Break, not Available
        self.assertTrue(any("8003" in c and "On Break" in c for c in self.commands))

    def test_agent_statuses_state_column_marks_on_call(self):
        self.table = ("name|status|state\n"
                      "8001|Available|In a queue call\n"
                      "8002|Available|Idle\n"
                      "8003|On Break|Idle\n")
        statuses = self.bridge._agent_statuses()
        self.assertEqual(statuses, {"8001": "On Call", "8002": "Available",
                                    "8003": "On Break"})

    def test_agent_statuses_without_state_column_unchanged(self):
        statuses = self.bridge._agent_statuses()
        self.assertEqual(statuses, {"8001": "Available", "8002": "On Break",
                                    "8003": "Logged Out"})

    def test_ensure_online_no_session_is_noop(self):
        # 8003 is Logged Out in the queue table, but without a live
        # session the bridge must not revive it.
        before = len(self.commands)
        self.assertFalse(self.bridge.ensure_online("8003"))
        self.assertEqual(len(self.commands), before)  # not even a list call

    def test_logout_stale_seats(self):
        store = self.bridge.store
        store.create_session("8002")  # live heartbeat: never touched
        stale = store.create_session("8001")
        _backdate_session(self.tmp, stale, seen_ago=700)
        out = self.bridge.logout_stale_seats()
        self.assertEqual(out, ["8001"])
        self.assertTrue(any("8001" in c and "Logged Out" in c for c in self.commands))
        self.assertFalse(any("8002" in c and "Logged Out" in c for c in self.commands))

    def test_logout_stale_seats_esl_down_is_noop(self):
        import bridge_core
        bridge_core.esl_command = lambda cmd: None
        stale = self.bridge.store.create_session("8001")
        _backdate_session(self.tmp, stale, seen_ago=700)
        self.assertEqual(self.bridge.logout_stale_seats(), [])
