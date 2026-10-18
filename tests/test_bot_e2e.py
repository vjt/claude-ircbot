"""bot.py's E2E wiring: commands, routing, redaction, startup. The bridge is
a fake here (tests/test_e2e_bridge.py covers the real one); _start_e2e builds
the real thing and needs irc-olm installed (requirements-e2e.txt)."""
import contextlib
import io
import os
import sys
import tempfile
import unittest

TMP = tempfile.mkdtemp()
for var, name in (("BOT_LOG", "bot.log"), ("BOT_FIFO", "bot.send"), ("BOT_TRUST", "bot.trust"),
                  ("BOT_ENV", "missing.env"), ("BOT_STARTUP", "bot.startup"),
                  ("BOT_RESERVED_STORE", "reserved.tsv")):
    os.environ[var] = os.path.join(TMP, name)
os.environ.pop("BOT_E2E_KEY", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: E402

HOST = "vjt.openssl.it"


class FakeBridge:
    def __init__(self, encrypt_to=(), verdict=None, raises=None):
        self.calls = []
        self.encrypt_to = set(encrypt_to)
        self.verdict = verdict
        self.raises = raises

    def is_e2e(self, body):
        return body.startswith("\x01E2E ")

    def __getattr__(self, name):
        def record(*args, **kw):
            self.calls.append((name, args, kw))
        return record

    def wants_encryption(self, nick):
        return nick.lower() in self.encrypt_to

    def send_text(self, nick, text, *, action, origin):
        if self.raises:
            raise self.raises
        self.calls.append(("send_text", (nick, text), {"action": action, "origin": origin}))

    def gate_plaintext(self, *args):
        self.calls.append(("gate_plaintext", args, {}))
        return self.verdict


class BotE2ETest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._saved = (bot.send_raw, bot.bridge, bot.trust_rules, set(bot.verified))
        bot.send_raw = lambda s, origin=None: self.sent.append(s)
        bot.trust_rules = [("vjt", "*.openssl.it")]
        bot.verified.clear()
        bot.verified.add("vjt")
        self.out = io.StringIO()
        self._redirect = contextlib.redirect_stdout(self.out)
        self._redirect.__enter__()

    def tearDown(self):
        self._redirect.__exit__(None, None, None)
        bot.send_raw, bot.bridge, bot.trust_rules, verified = self._saved
        bot.verified.clear()
        bot.verified.update(verified)

    def events(self):
        return self.out.getvalue().splitlines()

    # --- FIFO commands --------------------------------------------------------------

    def test_esay_and_eact_go_to_the_bridge(self):
        bot.bridge = FakeBridge()
        bot.run_cmd("ESAY vjt ciao")
        bot.run_cmd("EACT:tag vjt saluta")
        self.assertEqual(bot.bridge.calls, [
            ("send_text", ("vjt", "ciao"), {"action": False, "origin": None}),
            ("send_text", ("vjt", "saluta"), {"action": True, "origin": "tag"})])
        self.assertEqual(self.sent, [])

    def test_esay_to_a_channel_is_refused(self):
        bot.bridge = FakeBridge()
        bot.run_cmd("ESAY #sbiffo segreto")
        self.assertEqual(bot.bridge.calls, [])
        self.assertTrue(any("CMD_ERROR" in e and "e2e-channel" in e for e in self.events()))
        self.assertNotIn("segreto", self.out.getvalue())

    def test_esay_without_e2e_is_an_error_that_never_echoes_the_text(self):
        bot.bridge = None
        bot.run_cmd("ESAY vjt segreto")
        self.assertTrue(any("CMD_ERROR" in e and "e2e-off" in e for e in self.events()))
        self.assertNotIn("segreto", self.out.getvalue())
        self.assertEqual(self.sent, [])

    def test_a_failing_e2e_command_is_reported_redacted(self):
        bot.bridge = FakeBridge(raises=ValueError("boom: segreto"))
        bot.run_cmd("ESAY vjt segreto")
        (err,) = [e for e in self.events() if "CMD_ERROR" in e]
        self.assertIn("ValueError", err)
        self.assertIn("ESAY vjt [7 bytes]", err)
        self.assertNotIn("segreto", self.out.getvalue())

    def test_say_to_a_nick_in_an_encrypted_conversation_is_encrypted(self):
        bot.bridge = FakeBridge(encrypt_to={"vjt"})
        bot.run_cmd("SAY vjt risposta")
        bot.run_cmd("ACT vjt annuisce")
        self.assertEqual([c[0] for c in bot.bridge.calls], ["send_text", "send_text"])
        self.assertEqual(bot.bridge.calls[1][2]["action"], True)
        self.assertEqual(self.sent, [])

    def test_a_failing_encrypted_say_is_reported_redacted_too(self):
        bot.bridge = FakeBridge(encrypt_to={"vjt"}, raises=ValueError("segreto"))
        bot.run_cmd("SAY vjt segreto")
        self.assertNotIn("segreto", self.out.getvalue())

    def test_say_stays_plain_elsewhere(self):
        bot.bridge = FakeBridge(encrypt_to={"vjt"})
        bot.run_cmd("SAY lucy in chiaro")
        bot.run_cmd("SAY #sbiffo in chiaro")
        self.assertEqual(self.sent, ["PRIVMSG lucy :in chiaro", "PRIVMSG #sbiffo :in chiaro"])
        self.assertEqual(bot.bridge.calls, [])

    def test_esay_goes_through_the_leak_guard(self):
        bot.bridge = FakeBridge()
        bot.run_cmd("ESAY stranger vedi #it-opers")
        self.assertEqual(bot.bridge.calls, [])
        self.assertTrue(any(e.split()[1:3] == ["LEAK_BLOCKED", "ESAY"] for e in self.events()))

    # --- inbound routing ---------------------------------------------------------------

    def test_e2e_lines_go_to_the_bridge_and_never_become_ctcp(self):
        bot.bridge = FakeBridge()
        line = "\x01E2E MSG AAAAAAAAAAA 1/1 eyJ2IjoxfQ==\x01"
        bot.handle_server_line(f":vjt!v@{HOST} PRIVMSG vjt-claude :{line}")
        self.assertEqual(bot.bridge.calls, [("on_e2e_line", ("vjt", HOST, "vjt-claude", line), {})])
        self.assertFalse(any(" CTCP " in e for e in self.events()))

    def test_server_facts_reach_the_bridge(self):
        bot.bridge = FakeBridge()
        bot.handle_server_line(":srv 005 vjt-claude NETWORK=Azzurra CASEMAPPING=ascii :are supported")
        bot.handle_server_line(f":{bot.NICK}!claude@host.example JOIN :#sbiffo")
        bot.handle_server_line(f":{bot.NICK}!claude@host.example NICK :{bot.NICK}_")
        self.assertEqual([c[:2] for c in bot.bridge.calls], [
            ("on_isupport", (["NETWORK=Azzurra", "CASEMAPPING=ascii"],)),
            ("on_own_mask", (f"{bot.NICK}!claude@host.example",)),
            ("on_own_nick", (f"{bot.NICK}_",))])

    def test_someone_elses_nick_change_is_not_ours(self):
        bot.bridge = FakeBridge()
        bot.handle_server_line(f":vjt!v@{HOST} NICK :vjt_")
        self.assertEqual(bot.bridge.calls, [])

    def test_a_held_order_emits_nothing_yet(self):
        bot.bridge = FakeBridge(verdict="held")
        bot.handle_server_line(f":vjt!v@{HOST} PRIVMSG #sbiffo :vjt-claude: riavvia")
        self.assertFalse(any(" MSG " in e for e in self.events()))

    def test_an_unchallenged_line_from_a_pinned_user_is_untrusted(self):
        bot.bridge = FakeBridge(verdict="unchallenged")
        bot.handle_server_line(f":vjt!v@{HOST} PRIVMSG #sbiffo :chiacchiere")
        self.assertTrue(any(f"MSG UNTRUSTED WHY=unchallenged FROM=vjt HOST={HOST} TO=#sbiffo "
                            "BODY=chiacchiere" in e for e in self.events()))


class StartE2ETest(unittest.TestCase):
    def setUp(self):
        from irc_olm import store
        self.dir = tempfile.mkdtemp()
        self.key = os.path.join(self.dir, "azzurra.key")
        store.generate_key_file(self.key)
        self.pins = os.path.join(self.dir, "e2e.pins")
        with open(self.pins, "w") as f:
            f.write("vjt Y3E1/yaaT7cNiOsD0+Xlzctrb2YJM/WU7oUdRYuJ2GE\n")
        self._saved = (bot.E2E_KEY, bot.E2E_NETWORK, bot.E2E_PINS, bot.E2E_STATE, bot.bridge,
                       bot.fakelag)
        bot.E2E_KEY, bot.E2E_NETWORK, bot.E2E_PINS = self.key, "azzurra", self.pins
        bot.E2E_STATE = os.path.join(self.dir, "state.json")

    def tearDown(self):
        (bot.E2E_KEY, bot.E2E_NETWORK, bot.E2E_PINS, bot.E2E_STATE, bot.bridge,
         bot.fakelag) = self._saved

    def start(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bot._start_e2e()
        return out.getvalue().splitlines()

    def test_off_without_a_key(self):
        bot.E2E_KEY = ""
        self.assertEqual(self.start(), [])
        self.assertIsNone(bot.bridge)

    def test_starts_and_announces_its_key(self):
        events = self.start()
        self.assertIsNotNone(bot.bridge)
        (ready,) = [e for e in events if " E2E_READY " in e]
        self.assertIn(f"KEY={bot.bridge.engine.fingerprint}", ready)
        self.assertIn("NETWORK=azzurra", ready)
        self.assertEqual(bot.bridge.engine.pinned("vjt").ed25519,
                         "Y3E1/yaaT7cNiOsD0+Xlzctrb2YJM/WU7oUdRYuJ2GE")

    def test_every_line_the_bot_writes_is_charged_once_to_the_flood_bank(self):
        self.start()
        written, saved_sock = [], bot.sock
        bot.sock = type("Sock", (), {"sendall": lambda _, data: written.append(data)})()
        try:
            import time
            lag = bot.fakelag
            bot.send_raw("PRIVMSG #sbiffo :" + "y" * 380)          # chat: charged here
            after_chat = lag.bank(time.monotonic())
            self.assertGreater(after_chat, 4.0)
            bot._paced_write("vjt", "\x01E2E MSG AAAAAAAAAAA 1/1 eA==\x01")  # charged by the pacer
            self.assertLessEqual(lag.bank(time.monotonic()), after_chat)
            self.assertEqual(len(written), 2)
        finally:
            bot.sock = saved_sock

    def test_what_the_pacer_loses_after_queueing_reaches_the_bridge(self):
        events = self.start()
        pacer = bot.bridge._submit.__self__                 # the PacedSender
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pacer._on_lost("vjt", "AAAAAAAAAAA", "send-failed")
        self.assertTrue(any(" E2E_SEND_FAILED TO=vjt REASON=send-failed LOST=1" in e
                            for e in out.getvalue().splitlines()), events)

    def test_a_key_without_a_network_or_pins_fails_loudly(self):
        for missing in ("E2E_NETWORK", "E2E_PINS"):
            with self.subTest(missing=missing):
                saved = getattr(bot, missing)
                setattr(bot, missing, "")
                with self.assertRaises(ValueError):
                    self.start()
                setattr(bot, missing, saved)
                self.assertIsNone(bot.bridge)


if __name__ == "__main__":
    unittest.main()
