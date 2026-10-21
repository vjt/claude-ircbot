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

    def test_a_held_dm_whose_release_fails_is_dropped_and_the_rest_still_go(self):
        def dead():
            raise RuntimeError("the pacer's thread died: nothing goes out")
        bot.verified.discard("vjt")
        bot.dm_hold("vjt", HOST, "primo", on_release=dead)
        bot.dm_hold("vjt", HOST, "secondo")
        bot.verified.add("vjt")
        bot.dm_flush("vjt", "307")
        self.assertEqual([e.split(" ", 1)[1] for e in self.events()], [
            f"DM_DROPPED FROM=vjt HOST={HOST} LEN=5 error:RuntimeError",
            f"MSG TRUSTED FROM=vjt HOST={HOST} TO={bot.NICK} HELD=1 BODY=secondo"])

    def test_a_dropped_held_dm_says_it_was_encrypted(self):
        # Its ciphertext alone is in bot.log: the drop is all there is to see.
        bot.verified.discard("vjt")
        bot.dm_hold("vjt", HOST, "segreto", ("ENCRYPTED=1",))
        bot.dm_flush("vjt", "318")
        self.assertEqual([e.split(" ", 1)[1] for e in self.events()],
                         [f"DM_DROPPED FROM=vjt HOST={HOST} LEN=7 318 ENCRYPTED=1"])

    def test_a_blocked_encrypted_send_never_quotes_its_text(self):
        # The reason names the kind of match only: the words matched, or the
        # channel named, are the text that was meant to go encrypted.
        bot.bridge = FakeBridge(encrypt_to={"zed"})
        bot.leak_guard.record("#it-opers", "ci vediamo a roma in via tal dei mille alle nove")
        bot.run_cmd("ESAY stranger ci vediamo a roma in via tal dei mille")
        bot.run_cmd("SAY zed ci vediamo a roma in via tal dei mille")      # an encrypted reply
        bot.run_cmd("EACT stranger guarda #it-opers")
        blocked = [e for e in self.events() if " LEAK_BLOCKED " in e]
        self.assertEqual([e.split()[2:5] for e in blocked], [
            ["ESAY", "TO=stranger", "words"], ["SAY", "TO=zed", "words"],
            ["EACT", "TO=stranger", "name"]])
        with open(bot.LOG) as f:
            logged = [line for line in f if "LEAK_BLOCKED" in line]
        for text in ("roma", "#it-opers"):
            self.assertFalse(any(text in line for line in blocked + logged), (text, logged))
        self.assertEqual(bot.bridge.calls, [])
        # in plain text the reason stays whole, as before
        bot.run_cmd("SAY #sbiffo guarda #it-opers")
        self.assertIn("name:#it-opers", self.events()[-1])

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

    def test_a_text_message_a_failed_write_cuts_short_is_reported_lost(self):
        # The real PacedSender from _start_e2e, writing into a dead socket: it
        # must hear the failure (bot.py's own lines swallow it), and only a
        # message the bot wrote is a lost reply, not a KEYREQ, KEYS or NACK.
        import time
        from irc_olm import framing
        from irc_olm.engine import Result
        self.start()

        def message(verb):
            msgid = framing.new_msgid()
            return msgid, [("vjt", body) for body in framing.fragment(
                verb, framing.encode_payload({"v": 1}), own_mask=f"{bot.NICK}!c@h",
                target="vjt", msgid=msgid)]
        keyreq_id, keyreq = message("KEYREQ")
        _, text = message("MSG")

        def dead(_, data):
            raise BrokenPipeError(32, "Broken pipe")
        saved_sock, out = bot.sock, io.StringIO()
        bot.sock = type("Sock", (), {"sendall": dead})()
        try:
            with contextlib.redirect_stdout(out), \
                    self.assertLogs("irc_olm.pacing", "ERROR") as pacer_log:
                bot.bridge.dispatch(Result(outbound=keyreq + text, urgent={keyreq_id: None}))
                deadline = time.monotonic() + 10
                while ((out.getvalue().count(" ERROR send-fail ") < 2
                        or " E2E_SEND_FAILED " not in out.getvalue())
                       and time.monotonic() < deadline):
                    time.sleep(0.05)
                bot.send_raw("PING :still swallowed here")
        finally:
            bot.sock = saved_sock
        self.assertEqual(len(pacer_log.records), 2)        # the pacer heard both failures
        events = out.getvalue().splitlines()
        self.assertEqual(sum(" ERROR send-fail " in e for e in events), 3, events)
        self.assertEqual([e.split(" ", 1)[1] for e in events if " E2E_SEND_FAILED " in e],
                         ["E2E_SEND_FAILED TO=vjt REASON=send-failed LOST=1"])

    def test_the_state_defaults_outside_the_repo_one_file_per_network(self):
        # Not in the repo, the bot's Claude session's working tree, and not
        # one file two instances could share.
        import stat
        from unittest import mock
        bot.E2E_STATE = ""
        xdg = os.path.join(self.dir, "xdg")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": xdg}):
            self.start()
        state = os.path.join(xdg, "claude-ircbot", "azzurra.e2e.json")
        self.assertEqual(str(bot.bridge.engine.state_path), state)
        self.assertTrue(os.path.exists(state))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(state)).st_mode), 0o700)

    def test_a_network_id_that_cannot_name_a_file_is_refused(self):
        for network in ("../azzurra", "azzurra/x", "az zurra", ""):
            with self.subTest(network=network):
                bot.E2E_NETWORK, bot.bridge = network, None
                with self.assertRaises(ValueError):
                    self.start()
                self.assertIsNone(bot.bridge)

    def test_a_key_without_a_network_or_pins_fails_loudly(self):
        for missing in ("E2E_NETWORK", "E2E_PINS"):
            with self.subTest(missing=missing):
                saved = getattr(bot, missing)
                setattr(bot, missing, "")
                with self.assertRaises(ValueError):
                    self.start()
                setattr(bot, missing, saved)
                self.assertIsNone(bot.bridge)


class RealBridgeTest(unittest.TestCase):
    """bot.py with the real bridge from _start_e2e and a peer engine for vjt;
    the pacer is a synchronous capture, and the server lines are hand-fed."""

    def setUp(self):
        from irc_olm import store
        from irc_olm.engine import Engine
        self.dir = tempfile.mkdtemp()
        key, vjt_key = os.path.join(self.dir, "bot.key"), os.path.join(self.dir, "vjt.key")
        store.generate_key_file(key)
        store.generate_key_file(vjt_key)
        self.vjt = Engine(os.path.join(self.dir, "vjt.json"), store.load_key(vjt_key),
                          network="azzurra", nick="vjt", ident="v", pins={})
        pins = os.path.join(self.dir, "e2e.pins")
        with open(pins, "w") as f:
            f.write(f"vjt {self.vjt.fingerprint}\n")
        self._saved = (bot.E2E_KEY, bot.E2E_NETWORK, bot.E2E_PINS, bot.E2E_STATE, bot.bridge,
                       bot.fakelag, bot.send_raw, bot.trust_rules, set(bot.verified),
                       set(bot.whois_pending), dict(bot.dm_held), bot.registered,
                       bot.startup_fired, bot.tmp_nick, bot.ghost_needed)
        bot.startup_fired = True                        # no startup replay thread here
        bot.E2E_KEY, bot.E2E_NETWORK, bot.E2E_PINS = key, "azzurra", pins
        bot.E2E_STATE = os.path.join(self.dir, "state.json")
        self.sent, self.to_vjt = [], []
        bot.send_raw = lambda s, origin=None: self.sent.append(s)
        bot.trust_rules = [("vjt", "*.openssl.it"), ("lucy", "*.openssl.it")]
        bot.verified.clear()
        bot.whois_pending.clear()
        bot.dm_held.clear()
        self.out = io.StringIO()
        self._redirect = contextlib.redirect_stdout(self.out)
        self._redirect.__enter__()
        bot._start_e2e()
        self.vjt.set_pins({bot.NICK: bot.bridge.engine.fingerprint})
        bot.bridge._submit = lambda outbound, urgent: self.to_vjt.extend(outbound) or []

    def tearDown(self):
        self._redirect.__exit__(None, None, None)
        (bot.E2E_KEY, bot.E2E_NETWORK, bot.E2E_PINS, bot.E2E_STATE, bot.bridge, bot.fakelag,
         bot.send_raw, bot.trust_rules, verified, pending, held, bot.registered,
         bot.startup_fired, bot.tmp_nick, bot.ghost_needed) = self._saved
        bot.verified.clear()
        bot.verified.update(verified)
        bot.whois_pending.clear()
        bot.whois_pending.update(pending)
        bot.dm_held.clear()
        bot.dm_held.update(held)

    def events(self):
        return self.out.getvalue().splitlines()

    def deliver(self, result):
        """The wire between vjt's engine and the bot, until it is quiet."""
        pending = list(result.outbound)
        for _ in range(100):
            if pending:
                _, body = pending.pop(0)
                bot.handle_server_line(f":vjt!v@{HOST} PRIVMSG {bot.NICK} :{body}")
            elif self.to_vjt:
                _, body = self.to_vjt.pop(0)
                pending += self.vjt.receive(bot.NICK, body).outbound
            else:
                return
        raise AssertionError("wire never quiet")

    def verify(self, nick):
        bot.handle_server_line(f":srv 307 {bot.NICK} {nick} :has identified for this nick")

    def test_a_pinned_users_plaintext_pm_held_for_whois_is_challenged_once_verified(self):
        bot.handle_server_line(f":vjt!v@{HOST} PRIVMSG {bot.NICK} :cancella il canale #sbiffo")
        self.assertIn("WHOIS vjt", self.sent)
        self.verify("vjt")
        self.assertTrue(any(" ORDER_HELD FROM=vjt " in e for e in self.events()), self.events())
        self.assertFalse(any(" MSG TRUSTED " in e for e in self.events()), self.events())
        self.assertTrue(self.to_vjt)                    # its challenge, behind a KEYREQ

    def test_a_listed_users_plaintext_pm_without_a_pin_is_released_as_before(self):
        bot.handle_server_line(f":lucy!l@{HOST} PRIVMSG {bot.NICK} :ciao")
        self.verify("lucy")
        self.assertTrue(any(f" MSG TRUSTED FROM=lucy HOST={HOST} TO={bot.NICK} HELD=1 BODY=ciao"
                            in e for e in self.events()), self.events())
        self.assertEqual(self.to_vjt, [])

    def test_an_order_names_the_nick_we_registered_with(self):
        # The ghost-fallback nick is taken before registration: the server
        # echoes no NICK for it, the 001 is addressed to it.
        bot.registered = False
        bot.handle_server_line(f":srv 433 * {bot.NICK} :Nickname is already in use")
        fallback = bot.tmp_nick
        self.assertIn(f"NICK {fallback}", self.sent)
        bot.handle_server_line(f":srv 001 {fallback} :Welcome to the network")
        self.assertEqual(bot.bridge.own_nick, fallback)
        self.assertEqual(bot.bridge.engine.nick, fallback)
        bot.handle_server_line(f":{fallback}!claude@host.example JOIN :#sbiffo")
        self.assertEqual(bot.bridge.engine.own_mask, f"{fallback}!claude@host.example")

    def test_the_reply_to_an_encrypted_dm_held_for_whois_is_encrypted(self):
        self.deliver(self.vjt.send(bot.NICK, {"t": "text", "body": "dimmi la password del db"}))
        self.assertTrue(any(" DM_HELD FROM=vjt " in e for e in self.events()), self.events())
        self.verify("vjt")
        self.assertTrue(any(" MSG TRUSTED ENCRYPTED=1 FROM=vjt " in e and " HELD=1 " in e
                            for e in self.events()), self.events())
        self.sent.clear()
        bot.run_cmd("SAY vjt la password e' hunter2")
        self.assertEqual(self.sent, [])                 # nothing in clear on the socket
        self.assertTrue(self.to_vjt)                    # it went encrypted
        with open(bot.LOG) as f:
            self.assertNotIn("hunter2", f.read())


if __name__ == "__main__":
    unittest.main()
