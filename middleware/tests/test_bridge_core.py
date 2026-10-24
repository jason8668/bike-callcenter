import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bridge_core import (Bridge, CallRecord, CallStore, ZammadClient,  # noqa: E402
                         fmt_cst, fmt_duration, parse_cdr, summarize_history)


class FakeTransport:
    """Records requests; replies like a minimal Zammad."""

    def __init__(self):
        self.calls = []
        self.ticket_seq = 100

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if url.endswith("/api/v1/roles"):
            return 200, json.dumps([{"id": 3, "name": "Admin"},
                                    {"id": 4, "name": "Agent"}]).encode()
        if url.endswith("/api/v1/users?limit=100"):
            return 200, json.dumps(
                [{"id": 5, "login": "jason", "role_ids": [3]}]).encode()
        if url.endswith("/api/v1/groups"):
            return 200, json.dumps([{"id": 11, "name": "客服组"}]).encode()
        if url.endswith("/api/v1/users") and method == "POST":
            return 201, json.dumps({"id": 77}).encode()
        if "/api/v1/tickets/" in url and method == "GET":
            # Ticket 101 (the first created) already has an agent-filled
            # fault type, like a real returning caller's old ticket.
            return 200, json.dumps({
                "id": 101, "fault_type": "锁故障", "ticket_type": "故障报修",
                "created_at": "2026-10-20T09:00:00Z"}).encode()
        if url.endswith("/api/v1/ticket_articles") and method == "POST":
            return 201, b"{}"
        if "/api/v1/tickets/" in url and method == "PUT":
            return 200, b"{}"
        if url.endswith("/api/v1/tickets"):
            self.ticket_seq += 1
            return 201, json.dumps({"id": self.ticket_seq}).encode()
        if "/api/v1/cti/" in url:
            return 200, b"{}"
        return 404, b"{}"


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


class TestBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.transport = FakeTransport()
        zammad = ZammadClient("http://zammad.local", "tok", transport=self.transport,
                              cti_token="cti-tok")
        self.bridge = Bridge(zammad=zammad,
                             store=CallStore(os.path.join(self.tmp, "b.db")),
                             recording_reader=lambda rec: b"RIFF-fake-wav" if rec.recording_path else None)

    def test_fmt_duration_and_cst(self):
        self.assertEqual(fmt_duration(91), "1分31秒")
        self.assertEqual(fmt_duration(42), "0分42秒")
        self.assertEqual(fmt_duration(0), "0分00秒")
        self.assertEqual(fmt_cst(0), "")
        self.assertTrue(fmt_cst(1792800000).startswith("2026-"))

    def test_recent_tickets_newest_first_and_only_ticketed(self):
        self.bridge.handle_cdr(CDR)  # ticketed, caller 13800000000
        self.bridge.store.record(CallRecord(call_id="no-ticket", caller="13700000000",
                                            callee="", direction="in"),
                                 None)
        rows = self.bridge.store.recent_tickets(10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["caller"], "13800000000")
        self.assertTrue(rows[0]["ticket_id"])
        self.assertTrue(rows[0]["rowid"] > 0)

    def test_answered_call_creates_ticket_with_recording(self):
        out = self.bridge.handle_cdr(CDR)
        self.assertTrue(out["ok"])
        self.assertTrue(out["recording_attached"])
        ticket_posts = [c for c in self.transport.calls if c[1].endswith("/api/v1/tickets")]
        self.assertEqual(len(ticket_posts), 1)
        payload = json.loads(ticket_posts[0][3])
        self.assertEqual(payload["call_id"], "abc-123")
        self.assertEqual(payload["caller_number"], "13800000000")
        self.assertEqual(payload["group"], "报修组")
        self.assertEqual(payload["service_channel"], "热线电话")
        self.assertEqual(payload["ivr_keys"], "1")
        self.assertEqual(payload["call_duration"], "1分05秒")
        self.assertIn("attachments", payload["article"])

    def test_duplicate_cdr_is_idempotent(self):
        first = self.bridge.handle_cdr(CDR)
        second = self.bridge.handle_cdr(CDR)
        self.assertEqual(first["ticket_id"], second["ticket_id"])
        ticket_posts = [c for c in self.transport.calls if c[1].endswith("/api/v1/tickets")]
        self.assertEqual(len(ticket_posts), 1)

    def test_different_callers_share_one_ticket_customer(self):
        """No caller accounts at all: two numbers file under the first
        admin user, and no user is ever created for a caller."""
        self.bridge.handle_cdr(CDR)
        self.bridge.handle_cdr({**CDR, "uuid": "other-1",
                                "caller_id_number": "13900000000"})
        ticket_posts = [c for c in self.transport.calls if c[1].endswith("/api/v1/tickets")]
        self.assertEqual(len(ticket_posts), 2)
        for c in ticket_posts:
            self.assertEqual(json.loads(c[3])["customer_id"], 5)
        user_posts = [c for c in self.transport.calls
                      if c[1].endswith("/api/v1/users") and c[0] == "POST"]
        self.assertEqual(len(user_posts), 0)

    def test_configured_customer_id_wins(self):
        """An explicit TICKET_CUSTOMER_ID skips the admin lookup."""
        transport = FakeTransport()
        zammad = ZammadClient("http://zammad.local", "tok", transport=transport,
                              cti_token=None, ticket_customer_id=9)
        bridge = Bridge(zammad=zammad,
                        store=CallStore(os.path.join(tempfile.mkdtemp(), "b.db")),
                        recording_reader=lambda rec: b"RIFF-fake-wav" if rec.recording_path else None)
        bridge.handle_cdr(CDR)
        ticket_posts = [c for c in transport.calls if c[1].endswith("/api/v1/tickets")]
        self.assertEqual(json.loads(ticket_posts[0][3])["customer_id"], 9)
        role_calls = [c for c in transport.calls if c[1].endswith("/api/v1/roles")]
        self.assertEqual(len(role_calls), 0)

    def test_answer_event_opens_ticket_during_call(self):
        """The ticket exists at answer time (no recording yet), so the
        agent can take notes while still on the phone."""
        out = self.bridge.handle_event("answer", {
            "uuid": "live-1", "caller_id_number": "13800000000",
            "destination_number": "4008000000", "direction": "inbound",
            "start_epoch": "1792800000", "bridge_ticket": "1"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["ticket_id"], 101)
        ticket_posts = [c for c in self.transport.calls if c[1].endswith("/api/v1/tickets")]
        self.assertEqual(len(ticket_posts), 1)
        payload = json.loads(ticket_posts[0][3])
        self.assertNotIn("attachments", payload["article"])
        self.assertIn("进行中", payload["article"]["body"])

    def test_cdr_attaches_recording_to_live_ticket(self):
        """Hangup CDR attaches the recording to the answer-time ticket
        instead of creating a second ticket; a duplicate CDR attaches
        nothing twice."""
        self.bridge.handle_event("answer", {
            "uuid": "live-2", "caller_id_number": "13800000000",
            "destination_number": "4008000000", "direction": "inbound",
            "start_epoch": "1792800000", "bridge_ticket": "1"})
        out = self.bridge.handle_cdr({**CDR, "uuid": "live-2"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["ticket_id"], 101)
        self.assertTrue(out["recording_attached"])
        ticket_posts = [c for c in self.transport.calls if c[1].endswith("/api/v1/tickets")]
        self.assertEqual(len(ticket_posts), 1)
        article_posts = [c for c in self.transport.calls
                         if c[1].endswith("/api/v1/ticket_articles")]
        self.assertEqual(len(article_posts), 1)
        self.assertIn("attachments", json.loads(article_posts[0][3]))
        self.assertIn("1分05秒", json.loads(article_posts[0][3])["body"])
        puts = [c for c in self.transport.calls
                if "/api/v1/tickets/" in c[1] and c[0] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(json.loads(puts[0][3])["call_duration"], "1分05秒")
        again = self.bridge.handle_cdr({**CDR, "uuid": "live-2"})
        self.assertEqual(again["note"], "already bound")
        article_posts = [c for c in self.transport.calls
                         if c[1].endswith("/api/v1/ticket_articles")]
        self.assertEqual(len(article_posts), 1)

    def test_newcall_alone_creates_no_ticket(self):
        """Ringing (newCall) pops the caller card only; the ticket opens
        on answer, so a never-answered call still creates no ticket."""
        out = self.bridge.handle_event("newCall", {
            "uuid": "ring-1", "caller_id_number": "13800000000",
            "destination_number": "4008000000", "direction": "inbound",
            "start_epoch": "1792800000"})
        self.assertTrue(out["ok"])
        self.assertNotIn("ticket_id", out)
        ticket_posts = [c for c in self.transport.calls if c[1].endswith("/api/v1/tickets")]
        self.assertEqual(len(ticket_posts), 0)

    def test_non_ticket_legs_create_no_ticket(self):
        """Internal extension calls, outbound legs and voicemail legs
        must never own a ticket (statistics only)."""
        internal = self.bridge.handle_cdr(
            {**CDR, "uuid": "int-1", "destination_number": "8002",
             "bridge_ticket": ""})
        self.assertIsNone(internal["ticket_id"])
        outbound = self.bridge.handle_cdr(
            {**CDR, "uuid": "out-1", "direction": "outbound"})
        self.assertIsNone(outbound["ticket_id"])
        vm_leg = self.bridge.handle_cdr(
            {**CDR, "uuid": "vmleg-1", "bridge_ticket": "",
             "bridge_voicemail": "1"})
        self.assertIsNone(vm_leg["ticket_id"])
        no_flag = self.bridge.handle_event("answer", {
            "uuid": "noflag-1", "caller_id_number": "13800000000",
            "destination_number": "4008000000", "direction": "inbound"})
        self.assertNotIn("ticket_id", no_flag)

    def test_missed_call_creates_no_ticket(self):
        out = self.bridge.handle_cdr({**CDR, "uuid": "miss-1", "billsec": "0", "duration": "20"})
        self.assertTrue(out["ok"])
        self.assertIsNone(out["ticket_id"])

    def test_cti_event_forwarded(self):
        out = self.bridge.handle_event("newCall", CDR)
        self.assertTrue(out["ok"])
        cti = [c for c in self.transport.calls if "/api/v1/cti/" in c[1]]
        self.assertEqual(len(cti), 1)
        self.assertEqual(json.loads(cti[0][3])["callId"], "abc-123")


VM = {
    "uuid": "vm-123", "caller_id_number": "13900000000",
    "destination_number": "4008000000", "billsec": "8", "duration": "25",
    "start_epoch": "1792800000",
    "recording_path": "/var/lib/freeswitch/recordings/vm-123.wav",
}


class TestVoicemail(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.transport = FakeTransport()
        zammad = ZammadClient("http://zammad.local", "tok", transport=self.transport,
                              cti_token="cti-tok")
        self.bridge = Bridge(zammad=zammad,
                             store=CallStore(os.path.join(self.tmp, "b.db")),
                             recording_reader=lambda rec: b"RIFF-fake-wav" if rec.recording_path else None)

    def test_voicemail_creates_ticket_in_service_group(self):
        out = self.bridge.handle_voicemail(VM)
        self.assertTrue(out["ok"])
        self.assertTrue(out["voicemail"])
        self.assertTrue(out["recording_attached"])
        ticket_posts = [c for c in self.transport.calls if c[1].endswith("/api/v1/tickets")]
        self.assertEqual(len(ticket_posts), 1)
        payload = json.loads(ticket_posts[0][3])
        self.assertEqual(payload["group"], "客服组")
        self.assertEqual(payload["ticket_type"], "语音留言")
        self.assertEqual(payload["service_channel"], "语音留言")
        self.assertEqual(payload["call_duration"], "0分08秒")
        self.assertIn("attachments", payload["article"])
        self.assertIn("留言状态: 留言完成", payload["article"]["body"])
        self.assertIn("留言语种: zh", payload["article"]["body"])
        self.assertIn("留言时长: 0分08秒", payload["article"]["body"])

    def test_voicemail_is_idempotent(self):
        first = self.bridge.handle_voicemail(VM)
        second = self.bridge.handle_voicemail(VM)
        self.assertEqual(first["ticket_id"], second["ticket_id"])
        ticket_posts = [c for c in self.transport.calls if c[1].endswith("/api/v1/tickets")]
        self.assertEqual(len(ticket_posts), 1)




class TestCreateAgent(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.zammad = ZammadClient("http://zammad.local", "tok",
                                   transport=self.transport, cti_token=None)

    def test_create_agent_from_login_and_password(self):
        """Agent account from just login + password: firstname=login,
        Agent role, full access to 客服组."""
        user_id, err = self.zammad.create_agent("agent1", "pw123456")
        self.assertEqual(err, "")
        self.assertEqual(user_id, 77)
        posts = [c for c in self.transport.calls
                 if c[1].endswith("/api/v1/users") and c[0] == "POST"]
        self.assertEqual(len(posts), 1)
        payload = json.loads(posts[0][3])
        self.assertEqual(payload["login"], "agent1")
        self.assertEqual(payload["firstname"], "agent1")
        self.assertEqual(payload["password"], "pw123456")
        self.assertEqual(payload["roles"], ["Agent"])
        self.assertEqual(payload["group_ids"], {"11": "full"})

    def test_create_agent_existing_login(self):
        user_id, err = self.zammad.create_agent("jason", "pw123456")
        self.assertIsNone(user_id)
        self.assertEqual(err, "exists")

class TestHistoryHint(unittest.TestCase):
    def test_summarize_history(self):
        history = [
            {"id": 11, "fault_type": "锁故障", "ticket_type": "故障报修",
             "created_at": "2026-10-18T09:00:00Z"},
            {"id": 12, "fault_type": "锁故障", "ticket_type": "故障报修",
             "created_at": "2026-10-20T09:00:00Z"},
            {"id": 13, "fault_type": "掉链", "ticket_type": "故障报修",
             "created_at": "2026-10-19T09:00:00Z"},
        ]
        note = summarize_history(history)
        self.assertIn("历史来电 3 次", note)
        self.assertIn("锁故障×2", note)
        self.assertIn("掉链×1", note)
        self.assertIn("2026-10-20", note)
        self.assertEqual(summarize_history([]), "")

    def test_returning_caller_ticket_carries_hint(self):
        """History is found by caller NUMBER in the bridge's own call
        log (not by customer account): a second call from the same
        number carries the first ticket's fault keyword."""
        tmp = tempfile.mkdtemp()
        transport = FakeTransport()
        zammad = ZammadClient("http://zammad.local", "tok", transport=transport,
                              cti_token=None)
        bridge = Bridge(zammad=zammad, store=CallStore(os.path.join(tmp, "b.db")),
                        recording_reader=lambda rec: b"RIFF-fake-wav" if rec.recording_path else None)
        bridge.handle_cdr(CDR)  # first call -> ticket 101
        out = bridge.handle_cdr({**CDR, "uuid": "repeat-1"})
        self.assertTrue(out["ok"])
        self.assertIn("锁故障×1", out["history_hint"])
        ticket_posts = [c for c in transport.calls if c[1].endswith("/api/v1/tickets")]
        payload = json.loads(ticket_posts[1][3])
        self.assertIn("历史提示", payload["article"]["body"])
        self.assertIn("锁故障×1", payload["article"]["body"])


class DistTransport:
    """Zammad stub with three seat accounts (8001->11, 8002->12, 8003->13)."""

    def __init__(self):
        self.calls = []
        self.ticket_seq = 200

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if url.endswith("/api/v1/roles"):
            return 200, json.dumps([{"id": 3, "name": "Admin"}]).encode()
        if url.endswith("/api/v1/users?limit=100"):
            return 200, json.dumps([
                {"id": 5, "login": "admin", "role_ids": [3]},
                {"id": 11, "login": "8001", "role_ids": [4]},
                {"id": 12, "login": "8002", "role_ids": [4]},
                {"id": 13, "login": "8003", "role_ids": [4]},
            ]).encode()
        if url.endswith("/api/v1/tickets"):
            self.ticket_seq += 1
            return 201, json.dumps({"id": self.ticket_seq}).encode()
        if "/api/v1/tickets/" in url and method == "GET":
            return 200, json.dumps({"id": 101, "created_at": "2026-10-20T09:00:00Z"}).encode()
        if "/api/v1/cti/" in url:
            return 200, b"{}"
        return 404, b"{}"


class TestVoicemailDistribution(unittest.TestCase):
    def setUp(self):
        import bridge_core
        self._orig_esl = bridge_core.esl_command
        bridge_core.esl_command = lambda cmd: None  # ESL down: all available
        self.addCleanup(setattr, bridge_core, "esl_command", self._orig_esl)
        self.tmp = tempfile.mkdtemp()
        self.transport = DistTransport()
        zammad = ZammadClient("http://zammad.local", "tok", transport=self.transport,
                              cti_token="cti-tok")
        self.bridge = Bridge(zammad=zammad,
                             store=CallStore(os.path.join(self.tmp, "b.db")),
                             recording_reader=lambda rec: b"RIFF-fake-wav" if rec.recording_path else None,
                             rotation_agents=["8001", "8002", "8003"])

    def _voicemail_owners(self):
        posts = [json.loads(c[3]) for c in self.transport.calls
                 if c[1].endswith("/api/v1/tickets")]
        return [p.get("owner_id") for p in posts if p.get("ticket_type") == "语音留言"]

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
            owners.append(out["owner_id"])
        self.assertEqual(owners, [12, 13, 12])
        self.assertEqual(self._voicemail_owners(), [12, 13, 12])

    def test_busy_seat_is_skipped(self):
        import bridge_core
        bridge_core.esl_command = lambda cmd: (
            "name|type|contact|status|state\n"
            "8001|callback|user/8001|On Break|Waiting\n"
            "8002|callback|user/8002|Available|Waiting\n"
            "8003|callback|user/8003|Available|Waiting\n")
        out = self.bridge.handle_voicemail({**VM, "uuid": "vm-busy"})
        self.assertEqual(out["owner_id"], 12)

    def test_all_busy_leaves_ticket_unowned(self):
        import bridge_core
        bridge_core.esl_command = lambda cmd: (
            "name|type|contact|status|state\n"
            "8001|callback|user/8001|On Break|Waiting\n"
            "8002|callback|user/8002|On Break|Waiting\n"
            "8003|callback|user/8003|On Break|Waiting\n")
        out = self.bridge.handle_voicemail({**VM, "uuid": "vm-allbusy"})
        self.assertTrue(out["ok"])
        self.assertIsNone(out["owner_id"])

    def test_unknown_login_does_not_block_rotation(self):
        self.bridge.rotation_agents = ["8009", "8001"]
        first = self.bridge.handle_voicemail({**VM, "uuid": "vm-u1"})
        second = self.bridge.handle_voicemail({**VM, "uuid": "vm-u2"})
        self.assertIsNone(first["owner_id"])
        self.assertEqual(second["owner_id"], 11)

    def test_no_roster_leaves_ticket_unowned(self):
        self.bridge.rotation_agents = []
        out = self.bridge.handle_voicemail({**VM, "uuid": "vm-noroster"})
        self.assertTrue(out["ok"])
        self.assertIsNone(out["owner_id"])


class TestAgentHistory(unittest.TestCase):
    def setUp(self):
        import bridge_core
        self._orig_esl = bridge_core.esl_command
        bridge_core.esl_command = lambda cmd: None
        self.addCleanup(setattr, bridge_core, "esl_command", self._orig_esl)
        self.transport = DistTransport()
        zammad = ZammadClient("http://zammad.local", "tok", transport=self.transport,
                              cti_token="cti-tok")
        self.bridge = Bridge(zammad=zammad,
                             store=CallStore(os.path.join(tempfile.mkdtemp(), "b.db")),
                             recording_reader=lambda rec: b"RIFF-fake-wav" if rec.recording_path else None,
                             rotation_agents=["8001", "8002", "8003"])

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
        self.assertEqual(vm["owner_id"], 13)
        mine = self.bridge.get_agent_history("8001")
        self.assertEqual(mine["stats"]["calls_today"], 2)
        self.assertEqual(mine["stats"]["calls_total"], 3)
        self.assertEqual(mine["stats"]["voicemails"], 0)
        self.assertEqual(len(mine["history"]), 3)
        self.assertTrue(all(r["kind"] == "电话" for r in mine["history"]))
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
