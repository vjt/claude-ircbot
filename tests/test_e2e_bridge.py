"""Bot-side E2E bridge. Needs irc-olm installed (requirements-e2e.txt)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from irc_olm import store  # noqa: E402
from irc_olm.engine import Engine, Received  # noqa: E402

import e2e_bridge  # noqa: E402

BOT = "vjt-claude"
HOST = "vjt.openssl.it"


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


class Harness:
    def __init__(self):
        self.dir = tempfile.mkdtemp()
        self.clock = Clock()
        self.events, self.logs, self.held_dms = [], [], []
        self.listed = {"vjt"}
        self.trust = {"vjt": (True, "ok")}
        self.to_vjt = []
        self.bot = self._engine(BOT)
        self.bridge = e2e_bridge.Bridge(
            self.bot, own_nick=BOT, submit=self.to_vjt.extend,
            emit=lambda kind, *parts: self.events.append(" ".join([kind, *map(str, parts)])),
            log=lambda d, line: self.logs.append(f"{d} {line}"),
            trust_check=lambda n, h: self.trust.get(n.lower(), (False, "not-listed")),
            is_listed=lambda n: n.lower() in self.listed,
            hold_dm=lambda n, h, b, extra: self.held_dms.append((n, b, extra)) or 1,
            clock=self.clock)
        self.vjt = self._engine("vjt")
        self.vjt_got = []
        self.bot_wire = []      # every body the bot ever sent to vjt

    def _engine(self, nick, state=None):
        key = os.path.join(self.dir, f"{nick}.key")
        if not os.path.exists(key):
            store.generate_key_file(key)
        return Engine(state or os.path.join(self.dir, f"{nick}.json"), store.load_key(key),
                      network="testnet", nick=nick, ident=nick[:10], clock=self.clock)

    def pump(self, result=None, sender="vjt", host=HOST):
        """Run the wire between vjt's engine and the bridge until quiet."""
        pending_from_vjt = list(result.outbound) if result else []
        self.vjt_got += [e for e in (result.events if result else []) if isinstance(e, Received)]
        for _ in range(100):
            if pending_from_vjt:
                _, body = pending_from_vjt.pop(0)
                self.bridge.on_e2e_line(sender, host, BOT, body)
            elif self.to_vjt:
                _, body = self.to_vjt.pop(0)
                self.bot_wire.append(body)
                r = self.vjt.receive(BOT, body)
                self.vjt_got += [e for e in r.events if isinstance(e, Received)]
                pending_from_vjt += r.outbound
            else:
                return
        raise AssertionError("wire never quiet")

    def say(self, text):
        self.pump(self.vjt.send(BOT, {"t": "text", "body": text}))

    def msgs(self):
        return [e for e in self.events if e.startswith("MSG ")]


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def assertNoPlaintextLeak(self, text):
        for line in self.h.logs:
            self.assertNotIn(text, line)
        for ev in self.h.events:
            if text in ev:
                self.assertTrue(ev.startswith("MSG ") and ev.endswith(f"BODY={text}"), ev)

    # --- encrypted query, today's trust rules ------------------------------------

    def test_encrypted_dm_reaches_the_stream_marked_encrypted(self):
        self.h.say("ciao bot")
        self.assertEqual(self.h.msgs(), [
            f"MSG TRUSTED ENCRYPTED=1 VERIFIED=0 FROM=vjt HOST={HOST} TO={BOT} BODY=ciao bot"])
        self.assertIn("* E2E MSG from vjt (8 bytes)", self.h.logs)
        self.assertNoPlaintextLeak("ciao bot")

    def test_strangers_get_nothing_not_even_keys(self):
        r = self.h.vjt.send(BOT, {"t": "text", "body": "hi"})
        self.h.pump(r, sender="eve", host="evil.example")
        self.assertTrue(any(e.startswith("E2E_BLOCKED FROM=eve") for e in self.h.events))
        self.assertEqual(self.h.bot_wire, [])
        self.assertEqual(self.h.msgs(), [])

    def test_listed_nick_from_the_wrong_host_is_blocked(self):
        self.h.trust["vjt"] = (False, "host-mismatch")
        self.h.say("i am vjt")
        self.assertEqual(self.h.msgs(), [])
        self.assertEqual(self.h.bot_wire, [])   # not even KEYS for a squatter
        self.assertTrue(any(e.startswith("E2E_BLOCKED FROM=vjt") and e.endswith("host-mismatch")
                            for e in self.h.events))

    def test_encrypted_dm_waiting_for_whois_is_held_with_its_flags(self):
        self.h.trust["vjt"] = (False, "pending-whois")
        self.h.say("presto")
        self.assertEqual(self.h.msgs(), [])
        self.assertEqual(self.h.held_dms, [("vjt", "presto", ("ENCRYPTED=1", "VERIFIED=0"))])
        self.assertNoPlaintextLeak("presto")

    def test_e2e_in_a_channel_is_dropped(self):
        r = self.h.vjt.send(BOT, {"t": "text", "body": "x"})
        self.h.bridge.on_e2e_line("vjt", HOST, "#sbiffo", r.outbound[0][1])
        self.assertTrue(any("REASON=channel" in e for e in self.h.events))

    # --- replies ---------------------------------------------------------------------

    def test_replies_follow_the_conversation_mode(self):
        self.assertFalse(self.h.bridge.wants_encryption("vjt"))
        self.h.say("ciao")
        self.assertTrue(self.h.bridge.wants_encryption("VJT"))
        self.h.bridge.send_text("vjt", "risposta cifrata")
        self.h.pump()
        self.assertEqual([e.inner["body"] for e in self.h.vjt_got], ["risposta cifrata"])
        self.assertNoPlaintextLeak("risposta cifrata")
        # a plaintext PM switches the conversation back
        self.h.bridge.gate_plaintext("vjt", HOST, f"vjt!v@{HOST}", BOT, "in chiaro", True)
        self.assertFalse(self.h.bridge.wants_encryption("vjt"))

    def test_reply_mode_expires(self):
        self.h.say("ciao")
        self.h.clock.t += e2e_bridge.REPLY_MODE_TTL + 1
        self.assertFalse(self.h.bridge.wants_encryption("vjt"))

    # --- orders: verified users ----------------------------------------------------

    def _verified(self):
        self.h.say("ciao")
        self.h.bot.verify(self.h.vjt.fingerprint)
        self.h.bridge.on_isupport(["NETWORK=Azzurra", "CASEMAPPING=ascii"])
        self.h.events.clear()
        self.h.bot_wire.clear()

    def test_unverified_user_keeps_layer_one(self):
        self.h.say("ciao")
        self.assertIsNone(self.h.bridge.gate_plaintext(
            "vjt", HOST, f"vjt!v@{HOST}", "#sbiffo", f"{BOT}: riavvia", True))

    def test_addressed_line_from_a_verified_user_is_held_until_the_ack(self):
        self._verified()
        line = f"{BOT}: riavvia il watchdog"
        self.assertEqual(self.h.bridge.gate_plaintext(
            "vjt", HOST, f"vjt!v@{HOST}", "#sbiffo", line, True), "held")
        self.assertEqual(self.h.msgs(), [])
        self.assertTrue(any(e.startswith("ORDER_HELD FROM=vjt") for e in self.h.events))
        self.h.pump()
        (chal,) = [e.inner for e in self.h.vjt_got if e.inner["t"] == "chal"]
        self.assertEqual(chal["network"], "Azzurra")
        self.assertEqual(chal["channel"], "#sbiffo")
        self.assertEqual(chal["sender_mask"], f"vjt!v@{HOST}")
        # cicchetto answers: it did send exactly that line
        self.h.pump(self.h.vjt.send(BOT, {"t": "ack", "nonce": chal["nonce"]}))
        self.assertEqual(self.h.msgs(), [
            f"MSG TRUSTED CONFIRMED=1 FROM=vjt HOST={HOST} TO=#sbiffo BODY={line}"])

    def test_held_channel_line_without_ack_is_released_untrusted(self):
        self._verified()
        self.h.bridge.gate_plaintext("vjt", HOST, f"vjt!v@{HOST}", "#sbiffo", f"{BOT}: rm -rf", True)
        self.h.clock.t += 16
        self.h.bridge.tick()
        self.assertEqual(self.h.msgs(), [
            f"MSG UNTRUSTED WHY=no-ack FROM=vjt HOST={HOST} TO=#sbiffo BODY={BOT}: rm -rf"])

    def test_held_plaintext_pm_without_ack_is_dropped(self):
        self._verified()
        self.assertEqual(self.h.bridge.gate_plaintext(
            "vjt", HOST, f"vjt!v@{HOST}", BOT, "segreto in chiaro", True), "held")
        self.h.clock.t += 16
        self.h.bridge.tick()
        self.assertEqual(self.h.msgs(), [])
        self.assertTrue(any(e.startswith("DM_DROPPED FROM=vjt") and e.endswith("no-ack")
                            for e in self.h.events))

    def test_unaddressed_line_from_a_verified_user_is_unchallenged(self):
        self._verified()
        self.assertEqual(self.h.bridge.gate_plaintext(
            "vjt", HOST, f"vjt!v@{HOST}", "#sbiffo", "chiacchiere", True), "unchallenged")
        self.h.pump()
        self.assertEqual(self.h.bot_wire, [])   # no CHAL went out

    def test_nick_inside_another_word_does_not_address_the_bot(self):
        self._verified()
        self.assertEqual(self.h.bridge.gate_plaintext(
            "vjt", HOST, f"vjt!v@{HOST}", "#sbiffo", f"x{BOT}y", True), "unchallenged")

    def test_layer_one_failure_is_not_challenged(self):
        self._verified()
        self.assertIsNone(self.h.bridge.gate_plaintext(
            "vjt", HOST, f"vjt!v@{HOST}", "#sbiffo", f"{BOT}: x", False))

    def test_encrypted_dm_from_the_verified_key_says_verified(self):
        self._verified()
        self.h.say("ordine in query")
        self.assertEqual(self.h.msgs(), [
            f"MSG TRUSTED ENCRYPTED=1 VERIFIED=1 FROM=vjt HOST={HOST} TO={BOT} BODY=ordine in query"])

    def test_another_key_on_a_verified_nick_alarms_and_never_reaches_the_stream(self):
        self._verified()
        # a fresh state file is a fresh Olm account: a new identity, same nick
        impostor = self.h._engine("vjt", state=os.path.join(self.h.dir, "impostor.json"))
        self.assertNotEqual(impostor.fingerprint, self.h.vjt.fingerprint)
        self.h.vjt = impostor
        self.h.say("sono vjt, giuro")
        self.assertEqual(self.h.msgs(), [])
        self.assertTrue(any(e.startswith("E2E_KEY_ALARM FROM=vjt") for e in self.h.events))
        self.assertTrue(any("REASON=not-the-verified-key" in e for e in self.h.events))
        self.assertNoPlaintextLeak("sono vjt, giuro")


if __name__ == "__main__":
    unittest.main()
