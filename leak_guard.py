"""Outbound leak guard: what is said in a reserved channel stays there.

Order of Hypnotize in #it-opers, 2026-09-23 16:46 ("schiaffatelo in un
interceptor [...] non voglio piu' vedere una riga di qui dentro altrove"),
green light from vjt 2026-09-24 14:17. The rule already lived in CLAUDE.md and
was broken three times anyway; this is the mechanical half, in the one place
every outbound line passes through.

Two checks, both run on every PRIVMSG/NOTICE the FIFO asks us to send to a
target that is not the reserved channel itself:

  1. NAME  — the text names a reserved channel.
  2. WORDS — the text shares a run of NGRAM consecutive words with something
             said in a reserved channel (by anyone, us included) in the last
             TTL seconds.

A hit drops the line. There is no override verb on purpose: an override is
the thing that gets used at 3am on the one line that should not have gone.
Paraphrase gets through — no word-level filter stops it. That half stays in
CLAUDE.md.

The three bot instances (Azzurra, Libera, IRCnet) are separate processes, and
the worst leak is exactly the cross-network one, so the state is on disk:
`bot.reserved` lists the channels (tracked), and the instance that sits in one
appends what it hears to a shared store the others read on every check.
DMs to trust-listed nicks pass: "gliela si dice la' dov'e' nata o in query".
"""

import os
import re
import threading
import time

NGRAM = 6
TTL = 24 * 3600
# Rewrite the store once it grows past this, keeping only the TTL window.
PRUNE_BYTES = 512 * 1024

_WORD = re.compile(r"\w+", re.UNICODE)
# mIRC colour / bold / underline / reverse / reset: strip before tokenizing,
# or `\x0304ciao` and `ciao` would never match.
_FORMAT = re.compile(r"\x03(\d{1,2}(,\d{1,2})?)?|[\x02\x0f\x16\x1d\x1f]")


def tokens(text):
    return _WORD.findall(_FORMAT.sub("", text).lower())


def shingles(text, n=NGRAM):
    t = tokens(text)
    return {" ".join(t[i:i + n]) for i in range(len(t) - n + 1)}


def load_reserved(path):
    """`bot.reserved`: one `<irc host> <#channel>` per line.

    Only whole-line comments: a channel name starts with '#', so a trailing
    `# ...` comment cannot be told apart from it.
    """
    out = set()
    try:
        with open(path) as f:
            for line in f:
                raw = line.strip()
                if not raw or raw.startswith("#"):
                    continue
                parts = raw.split()
                if len(parts) == 2:
                    out.add((parts[0].lower(), parts[1].lower()))
    except FileNotFoundError:
        pass
    return out


class LeakGuard:
    def __init__(self, host, reserved_file, store_file, now=time.time):
        self.host = host.lower()
        self.reserved_file = reserved_file
        self.store_file = store_file
        self.now = now
        self.lock = threading.Lock()
        self.reload()

    def reload(self):
        self.reserved = load_reserved(self.reserved_file)
        self.mine = {c for h, c in self.reserved if h == self.host}
        names = sorted({c for _, c in self.reserved}, key=len, reverse=True)
        # Word-bounded on both sides so `#it-opers` does not fire on
        # `#it-operstuff`, and case-insensitive like IRC channel names.
        self.name_re = re.compile(
            r"(?<![\w#&-])(" + "|".join(re.escape(n) for n in names) + r")(?![\w-])",
            re.IGNORECASE,
        ) if names else None

    def is_reserved_here(self, target):
        return target.lower() in self.mine

    def record(self, target, text):
        """Remember text said in one of OUR reserved channels."""
        if not self.is_reserved_here(target) or not text:
            return
        line = f"{int(self.now())}\t{self.host}\t{target.lower()}\t" \
               + text.replace("\n", " ").replace("\r", " ") + "\n"
        with self.lock:
            try:
                with open(self.store_file, "a") as f:
                    f.write(line)
                if os.path.getsize(self.store_file) > PRUNE_BYTES:
                    self._prune()
            except OSError:
                pass

    def _prune(self):
        keep = [ln for ln in self._read_lines() if self._fresh(ln)]
        tmp = self.store_file + ".tmp"
        with open(tmp, "w") as f:
            f.writelines(keep)
        os.replace(tmp, self.store_file)

    def _read_lines(self):
        try:
            with open(self.store_file) as f:
                return f.readlines()
        except OSError:
            return []

    def _fresh(self, ln):
        try:
            return int(ln.split("\t", 1)[0]) >= self.now() - TTL
        except ValueError:
            return False

    def _recent_shingles(self):
        out = set()
        for ln in self._read_lines():
            if self._fresh(ln):
                parts = ln.rstrip("\n").split("\t", 3)
                if len(parts) == 4:
                    out |= shingles(parts[3])
        return out

    def check(self, target, text, dm_ok=False):
        """None if `text` may go to `target`, else a short reason string.

        The reason is for our own event stream: it may quote the matched words,
        so it must never be echoed to a channel.
        """
        if self.is_reserved_here(target):
            return None
        if dm_ok and not target.startswith(("#", "&")):
            return None
        if self.name_re:
            m = self.name_re.search(_FORMAT.sub("", text))
            if m:
                return f"name:{m.group(1).lower()}"
        mine = shingles(text)
        if mine:
            hit = mine & self._recent_shingles()
            if hit:
                return f"words:{sorted(hit)[0]!r}"
        return None
