"""bot.py's optional knobs: BOT_TZ and the usage command's reply."""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp()
for var, name in (("BOT_LOG", "bot.log"), ("BOT_FIFO", "bot.send"), ("BOT_TRUST", "bot.trust"),
                  ("BOT_ENV", "missing.env"), ("BOT_STARTUP", "bot.startup"),
                  ("BOT_RESERVED", "bot.reserved"), ("BOT_RESERVED_STORE", "reserved.tsv")):
    os.environ[var] = os.path.join(TMP, name)
# No defaults for these in bot.py; the same values as the other bot tests,
# whichever of them imports bot first.
os.environ["IRC_HOST"] = "irc.example.net"
os.environ["IRC_NICK"] = "claude-bot"
os.environ.pop("BOT_E2E_KEY", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: E402


class TimeZoneTest(unittest.TestCase):
    def setUp(self):
        self._saved = (bot.TZ_NAME, bot._TZ)

    def tearDown(self):
        bot.TZ_NAME, bot._TZ = self._saved

    def test_unset_is_the_hosts_local_time(self):
        bot.TZ_NAME, bot._TZ = "", None
        bot.check_config()
        self.assertIsNone(bot._TZ)

    def test_a_zone_name_is_used(self):
        bot.TZ_NAME = "Europe/Rome"
        bot.check_config()
        self.assertEqual(str(bot._TZ), "Europe/Rome")

    def test_an_unknown_zone_stops_the_bot(self):
        for name in ("Mars/Olympus_Mons", "../etc"):
            with self.subTest(name=name):
                bot.TZ_NAME = name
                with self.assertRaises(SystemExit) as cm:
                    bot.check_config()
                self.assertIn("BOT_TZ", str(cm.exception.code))


class UsageLineTest(unittest.TestCase):
    def setUp(self):
        self._saved = (bot.USAGE_CREDS, bot._TZ)
        bot._TZ = bot.ZoneInfo("UTC")
        creds = os.path.join(tempfile.mkdtemp(), "credentials.json")
        with open(creds, "w") as f:
            json.dump({"claudeAiOauth": {"accessToken": "tok"}}, f)
        bot.USAGE_CREDS = creds

    def tearDown(self):
        bot.USAGE_CREDS, bot._TZ = self._saved

    def reply(self, data):
        body = io.BytesIO(json.dumps(data).encode())
        with mock.patch("urllib.request.urlopen", return_value=body):
            return bot.usage_line()

    def test_limits_are_labelled_in_english(self):
        line = self.reply({"limits": [
            {"kind": "session", "percent": 12, "resets_at": "2026-11-30T18:00:00+00:00"},
            {"kind": "weekly_all", "percent": 40, "resets_at": "2026-12-03T09:00:00+00:00"},
            {"kind": "weekly_scoped", "percent": 0},
            {"kind": "weekly_scoped", "percent": 7, "resets_at": "2026-12-03T09:00:00+00:00",
             "scope": {"model": {"display_name": "Opus"}}}]})
        self.assertEqual(line, "usage: 5h session 12% (reset Mon 18:00) · week 40% "
                               "(reset Thu 09:00) · week Opus 7% (reset Thu 09:00)")

    def test_unreadable_credentials_say_so(self):
        bot.USAGE_CREDS = os.path.join(TMP, "no-such-file.json")
        self.assertEqual(bot.usage_line(), "usage: credentials unreadable (FileNotFoundError)")

    def test_a_reply_without_limits_says_so(self):
        self.assertEqual(self.reply({}), "usage: no readable limits in the reply")


if __name__ == "__main__":
    unittest.main()
