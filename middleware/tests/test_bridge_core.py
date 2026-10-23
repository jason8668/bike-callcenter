import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bridge_core import Bridge, CallStore, ZammadClient, parse_cdr  # noqa: E402


class FakeTransport:
    """Records requests; replies like a minimal Zammad."""

    def __init__(self):
        self.calls = []
        self.ticket_seq = 100

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if url.endswith("/api/v1/users/search?query=phone:13800000000"):
            return 200, json.dumps([{"id": 7}]).encode()
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
                             recording_reader=lambda rec: b"RIFF-fake-wav")

    def test_answered_call_creates_ticket_with_recording(self):
        out = self.bridge.handle_cdr(CDR)
        self.assertTrue(out["ok"])
        self.assertTrue(out["recording_attached"])
        ticket_posts = [c for c in self.transport.calls if c[1].endswith("/api/v1/tickets")]
        self.assertEqual(len(ticket_posts), 1)
        payload = json.loads(ticket_posts[0][3])
        self.assertEqual(payload["call_id"], "abc-123")
        self.assertEqual(payload["group"], "报修组")
        self.assertIn("attachments", payload["article"])

    def test_duplicate_cdr_is_idempotent(self):
        first = self.bridge.handle_cdr(CDR)
        second = self.bridge.handle_cdr(CDR)
        self.assertEqual(first["ticket_id"], second["ticket_id"])
        ticket_posts = [c for c in self.transport.calls if c[1].endswith("/api/v1/tickets")]
        self.assertEqual(len(ticket_posts), 1)

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


if __name__ == "__main__":
    unittest.main()
