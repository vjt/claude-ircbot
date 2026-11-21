import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import leak_guard  # noqa: E402
from leak_guard import LeakGuard  # noqa: E402

AZZ = "irc.example.net"
LIB = "irc.example.org"


class LeakGuardTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.reserved = os.path.join(d, "bot.reserved")
        self.store = os.path.join(d, "store.tsv")
        with open(self.reserved, "w") as f:
            f.write(f"# comment\n\n{AZZ} #secret\n")
        self.t = 1_000_000
        self.azz = LeakGuard(AZZ, self.reserved, self.store, now=lambda: self.t)
        self.lib = LeakGuard(LIB, self.reserved, self.store, now=lambda: self.t)

    def test_name_blocked_elsewhere(self):
        self.assertEqual(self.azz.check("#grappa", "come detto su #SeCrEt ieri"),
                         "name:#secret")

    def test_name_blocked_cross_network(self):
        self.assertIsNotNone(self.lib.check("#grappa", "vedi #secret"))

    def test_name_through_colour_codes(self):
        self.assertIsNotNone(self.azz.check("#sbiffo", "\x0304#secret\x03"))

    def test_name_word_bounded(self):
        self.assertIsNone(self.azz.check("#grappa", "#secretstuff e ##secret"))

    def test_reserved_channel_itself_passes(self):
        self.assertIsNone(self.azz.check("#secret", "qui su #secret si parla"))

    def test_same_name_on_other_network_is_not_ours(self):
        # The other network has no reserved #secret: sending there is not "inside".
        self.assertIsNotNone(self.lib.check("#secret", "ciao #secret"))

    def test_words_recorded_on_one_network_block_on_another(self):
        self.azz.record("#secret", "la petizione per Sonic la facciamo domani sera tardi")
        r = self.lib.check("#grappa", "ho sentito che la petizione per Sonic la facciamo domani")
        self.assertTrue(r and r.startswith("words:"))

    def test_short_overlap_passes(self):
        self.azz.record("#secret", "la petizione per Sonic la facciamo domani")
        self.assertIsNone(self.azz.check("#grappa", "la petizione per Sonic"))

    def test_shared_url_prefix_passes(self):
        # 2026-11-09: one issue link said in a reserved channel blocked every other
        # issue link for 24h — "github com vjt grappa irc issues" is a 6-gram.
        self.azz.record("#secret", "guardate https://github.com/vjt/grappa-irc/issues/2641 rotto")
        self.assertIsNone(self.lib.check(
            "#grappa", "aperta https://github.com/vjt/grappa-irc/issues/2642"))

    def test_same_url_blocked_any_spelling(self):
        # A secret gist URL is a credential: the URL alone is the leak.
        self.azz.record("#secret", "eccolo: https://gist.github.com/vjt/0a1b2c3d4e5f.")
        for text in ("gist.github.com/vjt/0a1b2c3d4e5f",
                     "(https://GIST.github.com/vjt/0a1b2c3d4e5f)",
                     "http://www.gist.github.com/vjt/0a1b2c3d4e5f/"):
            r = self.lib.check("#grappa", text)
            self.assertTrue(r and r.startswith("words:"), text)

    def test_words_around_url_still_count(self):
        self.azz.record("#secret", "dump su https://a.example/x e domani lo cancello senza dirlo")
        r = self.lib.check("#grappa", "vedi https://b.example/y e domani lo cancello senza dirlo")
        self.assertTrue(r and r.startswith("words:"))

    def test_record_ignores_non_reserved(self):
        self.azz.record("#grappa", "uno due tre quattro cinque sei sette")
        self.assertIsNone(self.azz.check("#sbiffo", "uno due tre quattro cinque sei sette"))

    def test_ttl_expiry(self):
        self.azz.record("#secret", "uno due tre quattro cinque sei sette")
        self.t += leak_guard.TTL + 1
        self.assertIsNone(self.azz.check("#sbiffo", "uno due tre quattro cinque sei sette"))

    def test_dm_to_trusted_passes_other_dm_blocked(self):
        text = "in #secret hanno detto uno due tre"
        self.assertIsNone(self.azz.check("vjt", text, dm_ok=True))
        self.assertIsNotNone(self.azz.check("random", text, dm_ok=False))
        self.assertIsNotNone(self.azz.check("#grappa", text, dm_ok=True))

    def test_prune_keeps_window(self):
        self.azz.record("#secret", "vecchio vecchio vecchio vecchio vecchio vecchio")
        self.t += leak_guard.TTL + 1
        old = leak_guard.PRUNE_BYTES
        leak_guard.PRUNE_BYTES = 1
        try:
            self.azz.record("#secret", "nuovo nuovo nuovo nuovo nuovo nuovo")
        finally:
            leak_guard.PRUNE_BYTES = old
        with open(self.store) as f:
            body = f.read()
        self.assertNotIn("vecchio", body)
        self.assertIn("nuovo", body)


if __name__ == "__main__":
    unittest.main()
