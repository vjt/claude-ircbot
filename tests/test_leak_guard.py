import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import leak_guard  # noqa: E402
from leak_guard import LeakGuard  # noqa: E402

NET1 = "irc.example.net"
NET2 = "irc.example.org"


class LeakGuardTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.reserved = os.path.join(d, "bot.reserved")
        self.store = os.path.join(d, "store.tsv")
        with open(self.reserved, "w") as f:
            f.write(f"# comment\n\n{NET1} #secret\n")
        self.t = 1_000_000
        self.net1 = LeakGuard(NET1, self.reserved, self.store, now=lambda: self.t)
        self.net2 = LeakGuard(NET2, self.reserved, self.store, now=lambda: self.t)

    def test_name_blocked_elsewhere(self):
        self.assertEqual(self.net1.check("#a", "come detto su #SeCrEt ieri"),
                         "name:#secret")

    def test_name_blocked_cross_network(self):
        self.assertIsNotNone(self.net2.check("#a", "vedi #secret"))

    def test_name_through_colour_codes(self):
        self.assertIsNotNone(self.net1.check("#b", "\x0304#secret\x03"))

    def test_name_word_bounded(self):
        self.assertIsNone(self.net1.check("#a", "#secretstuff e ##secret"))

    def test_reserved_channel_itself_passes(self):
        self.assertIsNone(self.net1.check("#secret", "qui su #secret si parla"))

    def test_same_name_on_other_network_is_not_ours(self):
        # The other network has no reserved #secret: sending there is not "inside".
        self.assertIsNotNone(self.net2.check("#secret", "ciao #secret"))

    def test_words_recorded_on_one_network_block_on_another(self):
        self.net1.record("#secret", "la petizione per Sonic la facciamo domani sera tardi")
        r = self.net2.check("#a", "ho sentito che la petizione per Sonic la facciamo domani")
        self.assertTrue(r and r.startswith("words:"))

    def test_short_overlap_passes(self):
        self.net1.record("#secret", "la petizione per Sonic la facciamo domani")
        self.assertIsNone(self.net1.check("#a", "la petizione per Sonic"))

    def test_shared_url_prefix_passes(self):
        # One issue link said in a reserved channel must not block every other
        # link to the same repo: "github com alice some repo issues" is a 6-gram.
        self.net1.record("#secret", "guardate https://github.com/alice/some-repo/issues/2641 rotto")
        self.assertIsNone(self.net2.check(
            "#a", "aperta https://github.com/alice/some-repo/issues/2642"))

    def test_same_url_blocked_any_spelling(self):
        # A secret gist URL is a credential: the URL alone is the leak.
        self.net1.record("#secret", "eccolo: https://gist.github.com/alice/0a1b2c3d4e5f.")
        for text in ("gist.github.com/alice/0a1b2c3d4e5f",
                     "(https://GIST.github.com/alice/0a1b2c3d4e5f)",
                     "http://www.gist.github.com/alice/0a1b2c3d4e5f/"):
            r = self.net2.check("#a", text)
            self.assertTrue(r and r.startswith("words:"), text)

    def test_words_around_url_still_count(self):
        self.net1.record("#secret", "dump su https://a.example/x e domani lo cancello senza dirlo")
        r = self.net2.check("#a", "vedi https://b.example/y e domani lo cancello senza dirlo")
        self.assertTrue(r and r.startswith("words:"))

    def test_record_ignores_non_reserved(self):
        self.net1.record("#a", "uno due tre quattro cinque sei sette")
        self.assertIsNone(self.net1.check("#b", "uno due tre quattro cinque sei sette"))

    def test_ttl_expiry(self):
        self.net1.record("#secret", "uno due tre quattro cinque sei sette")
        self.t += leak_guard.TTL + 1
        self.assertIsNone(self.net1.check("#b", "uno due tre quattro cinque sei sette"))

    def test_dm_to_trusted_passes_other_dm_blocked(self):
        text = "in #secret hanno detto uno due tre"
        self.assertIsNone(self.net1.check("alice", text, dm_ok=True))
        self.assertIsNotNone(self.net1.check("random", text, dm_ok=False))
        self.assertIsNotNone(self.net1.check("#a", text, dm_ok=True))

    def test_prune_keeps_window(self):
        self.net1.record("#secret", "vecchio vecchio vecchio vecchio vecchio vecchio")
        self.t += leak_guard.TTL + 1
        old = leak_guard.PRUNE_BYTES
        leak_guard.PRUNE_BYTES = 1
        try:
            self.net1.record("#secret", "nuovo nuovo nuovo nuovo nuovo nuovo")
        finally:
            leak_guard.PRUNE_BYTES = old
        with open(self.store) as f:
            body = f.read()
        self.assertNotIn("vecchio", body)
        self.assertIn("nuovo", body)


if __name__ == "__main__":
    unittest.main()
