import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import leak_guard  # noqa: E402
from leak_guard import LeakGuard  # noqa: E402

AZZ = "irc.azzurra.chat"
LIB = "irc.libera.chat"


class LeakGuardTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.reserved = os.path.join(d, "bot.reserved")
        self.store = os.path.join(d, "store.tsv")
        with open(self.reserved, "w") as f:
            f.write("# comment\n\nirc.azzurra.chat #it-opers\n")
        self.t = 1_000_000
        self.azz = LeakGuard(AZZ, self.reserved, self.store, now=lambda: self.t)
        self.lib = LeakGuard(LIB, self.reserved, self.store, now=lambda: self.t)

    def test_name_blocked_elsewhere(self):
        self.assertEqual(self.azz.check("#grappa", "come detto su #IT-Opers ieri"),
                         "name:#it-opers")

    def test_name_blocked_cross_network(self):
        self.assertIsNotNone(self.lib.check("#grappa", "vedi #it-opers"))

    def test_name_through_colour_codes(self):
        self.assertIsNotNone(self.azz.check("#sbiffo", "\x0304#it-opers\x03"))

    def test_name_word_bounded(self):
        self.assertIsNone(self.azz.check("#grappa", "#it-operstuff e ##it-opers"))

    def test_reserved_channel_itself_passes(self):
        self.assertIsNone(self.azz.check("#it-opers", "qui su #it-opers si parla"))

    def test_same_name_on_other_network_is_not_ours(self):
        # Libera has no reserved #it-opers: sending there is not "inside".
        self.assertIsNotNone(self.lib.check("#it-opers", "ciao #it-opers"))

    def test_words_recorded_on_one_network_block_on_another(self):
        self.azz.record("#it-opers", "la petizione per Sonic la facciamo domani sera tardi")
        r = self.lib.check("#grappa", "ho sentito che la petizione per Sonic la facciamo domani")
        self.assertTrue(r and r.startswith("words:"))

    def test_short_overlap_passes(self):
        self.azz.record("#it-opers", "la petizione per Sonic la facciamo domani")
        self.assertIsNone(self.azz.check("#grappa", "la petizione per Sonic"))

    def test_record_ignores_non_reserved(self):
        self.azz.record("#grappa", "uno due tre quattro cinque sei sette")
        self.assertIsNone(self.azz.check("#sbiffo", "uno due tre quattro cinque sei sette"))

    def test_ttl_expiry(self):
        self.azz.record("#it-opers", "uno due tre quattro cinque sei sette")
        self.t += leak_guard.TTL + 1
        self.assertIsNone(self.azz.check("#sbiffo", "uno due tre quattro cinque sei sette"))

    def test_dm_to_trusted_passes_other_dm_blocked(self):
        text = "in #it-opers hanno detto uno due tre"
        self.assertIsNone(self.azz.check("vjt", text, dm_ok=True))
        self.assertIsNotNone(self.azz.check("random", text, dm_ok=False))
        self.assertIsNotNone(self.azz.check("#grappa", text, dm_ok=True))

    def test_prune_keeps_window(self):
        self.azz.record("#it-opers", "vecchio vecchio vecchio vecchio vecchio vecchio")
        self.t += leak_guard.TTL + 1
        old = leak_guard.PRUNE_BYTES
        leak_guard.PRUNE_BYTES = 1
        try:
            self.azz.record("#it-opers", "nuovo nuovo nuovo nuovo nuovo nuovo")
        finally:
            leak_guard.PRUNE_BYTES = old
        with open(self.store) as f:
            body = f.read()
        self.assertNotIn("vecchio", body)
        self.assertIn("nuovo", body)


if __name__ == "__main__":
    unittest.main()
