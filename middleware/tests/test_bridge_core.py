import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bridge_core import (Bridge, CallRecord, CallStore,  # noqa: E402
                         fmt_cst, fmt_duration, parse_cdr,
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


if __name__ == "__main__":
    unittest.main()
