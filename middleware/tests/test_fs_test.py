import os
import sys
import tempfile
import unittest
import wave
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fs_test  # noqa: E402


class FakeEsl:
    """Records commands; replies come from a dict of substrings."""

    def __init__(self, replies):
        self.replies = replies  # substring -> reply (or None for ESL down)
        self.commands = []

    def __call__(self, cmd):
        self.commands.append(cmd)
        for sub, reply in self.replies.items():
            if sub in cmd:
                return reply
        return "+OK"


class TestToneWav(unittest.TestCase):
    def test_header_and_determinism(self):
        with tempfile.TemporaryDirectory() as d:
            p1 = os.path.join(d, "a.wav")
            p2 = os.path.join(d, "b.wav")
            p3 = os.path.join(d, "c.wav")
            fs_test.generate_tone_wav(p1, 42, 400, 700)
            fs_test.generate_tone_wav(p2, 42, 400, 700)
            fs_test.generate_tone_wav(p3, 43, 400, 700)
            with open(p1, "rb") as f1, open(p2, "rb") as f2, \
                    open(p3, "rb") as f3:
                self.assertEqual(f1.read(), f2.read())
                f1.seek(0)
                self.assertNotEqual(f1.read(), f3.read())
            with wave.open(p1, "rb") as w:
                self.assertEqual(w.getnchannels(), 1)
                self.assertEqual(w.getsampwidth(), 2)
                self.assertEqual(w.getframerate(), 8000)
                self.assertGreater(w.getnframes(), 110000)
                self.assertLessEqual(w.getnframes(), 120000)


class TestOriginateCommand(unittest.TestCase):
    def test_queue_form(self):
        cmd = fs_test.build_originate_command("13800000000", "queue")
        self.assertEqual(
            cmd,
            "originate {origination_caller_id_number=13800000000,"
            "origination_caller_id_name=13800000000,"
            "originate_timeout=180}loopback/8000/default "
            "&playback(/recordings/test-caller.wav)")

    def test_voicemail_form(self):
        cmd = fs_test.build_originate_command("110", "voicemail")
        self.assertIn("loopback/8009/default", cmd)
        self.assertIn("origination_caller_id_number=110", cmd)

    def test_noagent_form_skips_agent(self):
        cmd = fs_test.build_originate_command("110", "queue-noagent")
        self.assertIn("loopback/8000/default", cmd)
        esl = FakeEsl({"originate": "+OK noagent-uuid\n"})
        with tempfile.TemporaryDirectory() as d:
            result = fs_test.place_fs_call(esl, "110", "queue-noagent", d)
        self.assertTrue(result["ok"])
        self.assertEqual(result["uuid"], "noagent-uuid")
        self.assertFalse(any("agent list" in c for c in esl.commands))

    def test_bad_caller_rejected(self):
        for bad in ("", "abc", "138-0000"):
            with self.assertRaises(ValueError):
                fs_test.build_originate_command(bad, "queue")

    def test_broadcast_form(self):
        self.assertEqual(
            fs_test.build_broadcast_command("u1", "agent"),
            "uuid_broadcast u1 /recordings/test-agent.wav both")
        self.assertEqual(
            fs_test.build_broadcast_command("u1", "caller"),
            "uuid_broadcast u1 /recordings/test-caller.wav both")

    def test_reply_parsers(self):
        self.assertEqual(fs_test.parse_originate_reply("+OK abc-123\n"), "abc-123")
        self.assertIsNone(fs_test.parse_originate_reply("-ERR NO_ROUTE"))
        self.assertIsNone(fs_test.parse_originate_reply(None))
        self.assertTrue(fs_test.parse_exists_reply("true"))
        self.assertFalse(fs_test.parse_exists_reply("false"))


class TestAgentManagement(unittest.TestCase):
    def test_ensure_adds_missing_agent(self):
        esl = FakeEsl({"agent list": "+OK\n8001\n8002\n"})
        steps = fs_test.ensure_test_agent(esl)
        cmds = [s["command"] for s in steps]
        # mod_callcenter takes name + type on add; the contact is a
        # separate step (the old queue-first form failed with -ERR).
        self.assertEqual(cmds[1:], [
            "callcenter_config agent add testagent callback",
            "callcenter_config agent set contact testagent loopback/8999",
            "callcenter_config tier add 客服队列 testagent 1 1",
            "callcenter_config agent set status testagent Available",
        ])

    def test_ensure_skips_existing_agent(self):
        esl = FakeEsl({"agent list": "+OK\ntestagent\n"})
        steps = fs_test.ensure_test_agent(esl)
        self.assertEqual(len(steps), 1)  # only the list call

    def test_remove_agent(self):
        esl = FakeEsl({})
        steps = fs_test.remove_test_agent(esl)
        cmds = [s["command"] for s in steps]
        self.assertTrue(any("tier del" in c for c in cmds))
        self.assertTrue(any("agent del" in c for c in cmds))


class TestPlaceCall(unittest.TestCase):
    def test_esl_down_fails_cleanly(self):
        esl = FakeEsl({})
        esl.replies = {}
        with tempfile.TemporaryDirectory() as d:
            result = fs_test.place_fs_call(lambda c: None, "13800000000",
                                           "queue", d)
        self.assertFalse(result["ok"])
        self.assertIn("ESL", result["error"])
        self.assertIsNone(result["uuid"])

    def test_happy_path_queue(self):
        esl = FakeEsl({
            "agent list": "+OK\n",
            "originate": "+OK call-uuid-1\n",
        })
        with tempfile.TemporaryDirectory() as d:
            result = fs_test.place_fs_call(esl, "13800000000", "queue", d)
            self.assertTrue(os.path.isfile(os.path.join(d, "test-caller.wav")))
            self.assertTrue(os.path.isfile(os.path.join(d, "test-agent.wav")))
        self.assertTrue(result["ok"])
        self.assertEqual(result["uuid"], "call-uuid-1")
        self.assertTrue(any("originate" in c and "loopback/8000/default" in c
                            for c in esl.commands))

    def test_originate_rejected(self):
        esl = FakeEsl({
            "agent list": "+OK\ntestagent\n",
            "originate": "-ERR NO_ROUTE\n",
        })
        with tempfile.TemporaryDirectory() as d:
            result = fs_test.place_fs_call(esl, "13800000000", "queue", d)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["uuid"])
        self.assertIn("NO_ROUTE", result["error"])

    def test_seat_answer_steps(self):
        esl = FakeEsl({})
        steps = fs_test.seat_answer_steps(esl, "8002")
        self.assertEqual([s["command"] for s in steps], [
            "callcenter_config agent set contact 8002 loopback/8999",
            "callcenter_config agent set status 8002 Available",
        ])

    def test_restore_seat_contacts(self):
        esl = FakeEsl({})
        steps = fs_test.restore_seat_contacts(esl)
        self.assertEqual([s["command"] for s in steps], [
            "callcenter_config agent set contact 8001 user/8001",
            "callcenter_config agent set contact 8002 user/8002",
            "callcenter_config agent set contact 8003 user/8003",
        ])

    def test_place_call_seat_answer(self):
        esl = FakeEsl({"originate": "+OK call-uuid-1\n"})
        with tempfile.TemporaryDirectory() as d:
            result = fs_test.place_fs_call(esl, "13800000000", "queue",
                                           d, answer_seat="8002")
        self.assertTrue(result["ok"])
        self.assertEqual(result["uuid"], "call-uuid-1")
        self.assertTrue(any("agent set contact 8002 loopback/8999" in c
                            for c in esl.commands))
        self.assertTrue(any("agent set status 8002 Available" in c
                            for c in esl.commands))
        # No testagent: with fewest-calls it could steal the call
        # from the emulated seat.
        self.assertFalse(any("agent add testagent" in c
                             for c in esl.commands))

    def test_hangup_restores_seats(self):
        esl = FakeEsl({"uuid_kill": "+OK"})
        fs_test.hangup_fs_call(esl, "u9")
        self.assertTrue(any("agent set contact 8002 user/8002" in c
                            for c in esl.commands))

    def test_hangup_and_play(self):
        esl = FakeEsl({"uuid_kill": "+OK", "uuid_broadcast": "+OK"})
        res = fs_test.hangup_fs_call(esl, "u9")
        self.assertTrue(any("uuid_kill u9" in c for c in esl.commands))
        self.assertTrue(any("tier del" in c for c in esl.commands))
        res = fs_test.play_fs_audio(esl, "u9", "agent")
        self.assertTrue(res["ok"])
        self.assertTrue(any("uuid_broadcast u9 /recordings/test-agent.wav both"
                            in c for c in esl.commands))
        self.assertTrue(fs_test.call_status(esl, "u9")["up"] is False)


class TestDialplan(unittest.TestCase):
    def test_test_agent_extension_present(self):
        path = os.path.join(os.path.dirname(__file__), "..", "..",
                            "freeswitch", "conf-templates", "dialplan",
                            "default.xml")
        root = ET.parse(path).getroot()
        names = [e.get("name") for e in root.iter("extension")]
        for expected in ("queue", "voicemail", "test-agent"):
            self.assertIn(expected, names)
        for ext in root.iter("extension"):
            if ext.get("name") == "test-agent":
                cond = ext.find("condition")
                self.assertEqual(cond.get("expression"), "^8999$")
                apps = [a.get("application")
                        for a in ext.iter("action")]
                self.assertIn("answer", apps)
                self.assertIn("sleep", apps)


if __name__ == "__main__":
    unittest.main()
