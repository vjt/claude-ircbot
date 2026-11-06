"""bot.py's ghost-on-connect: a reconnect that finds our own ghost holding the
nick takes a throwaway one, GHOSTs the stale nick and takes it back."""
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


class GhostTest(unittest.TestCase):
    def setUp(self):
        self.sent, self.reclaims, self.startups = [], [], []
        names = ("send_raw", "_schedule_reclaim", "fire_startup", "registered", "ghost_needed",
                 "ghost_tries", "tmp_nick", "me", "SASL", "NICKSERV_PASS", "trust_rules", "bridge")
        self._saved = {n: getattr(bot, n) for n in names}
        bot.send_raw = lambda s, origin=None: self.sent.append(s)
        bot._schedule_reclaim = lambda: self.reclaims.append(1)
        bot.fire_startup = self.startups.append
        bot.registered, bot.ghost_needed, bot.ghost_tries = False, False, 0
        bot.tmp_nick, bot.me = None, bot.NICK
        bot.NICKSERV_PASS, bot.trust_rules, bot.bridge = "pw", [], None

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(bot, n, v)

    def connect_into_our_ghost(self):
        bot.handle_server_line(f":srv 433 * {bot.NICK} :Nickname is already in use.")
        bot.handle_server_line(f":srv 001 {bot.tmp_nick} :Welcome")

    def test_a_reclaim_that_beats_the_ghost_is_tried_again(self):
        # Libera 2026-11-06: services answered the GHOST 6s later, the reclaim
        # went at 4s and got 433, and nothing ever retried.
        bot.SASL = True
        self.connect_into_our_ghost()
        self.assertIn(f"PRIVMSG NickServ :GHOST {bot.NICK} pw", self.sent)
        self.assertEqual(len(self.reclaims), 1)
        bot.handle_server_line(f":srv 433 {bot.tmp_nick} {bot.NICK} :Nickname is already in use.")
        self.assertEqual(len(self.reclaims), 2)

    def test_retries_stop(self):
        bot.SASL = True
        self.connect_into_our_ghost()
        for _ in range(10):
            bot.handle_server_line(f":srv 433 {bot.tmp_nick} {bot.NICK} :Nickname is already in use.")
        self.assertEqual(len(self.reclaims), 1 + bot.GHOST_RECLAIM_MAX)

    def test_a_433_on_another_nick_is_not_a_reclaim(self):
        bot.SASL = True
        self.connect_into_our_ghost()
        bot.handle_server_line(f":srv 433 {bot.tmp_nick} somebody :Nickname is already in use.")
        self.assertEqual(len(self.reclaims), 1)

    def test_with_sasl_the_reclaimed_nick_runs_the_startup_without_identify(self):
        # Under SASL we are logged in already; atheme answers IDENTIFY with
        # "You are already logged in", which never fired the startup.
        bot.SASL = True
        self.connect_into_our_ghost()
        self.assertEqual(self.startups, [])
        bot.handle_server_line(f":{bot.tmp_nick}!c@h NICK :{bot.NICK}")
        self.assertEqual(self.startups, ["sasl"])
        self.assertFalse(any("IDENTIFY" in s for s in self.sent))
        self.assertFalse(bot.ghost_needed)

    def test_without_sasl_the_reclaimed_nick_identifies(self):
        bot.SASL = False
        self.connect_into_our_ghost()
        bot.handle_server_line(f":{bot.tmp_nick}!c@h NICK :{bot.NICK}")
        self.assertIn("PRIVMSG NickServ :IDENTIFY pw", self.sent)
        self.assertEqual(self.startups, [])


if __name__ == "__main__":
    unittest.main()
