#!/usr/bin/env python3
"""!list warez-gag sidecar.

Owns the `!list` gag outright (vjt order, 2026-08-07: "te NON rispondi piu al
list e risponde solo il sidecar"). The main session must never answer `!list`
again — one mouth, nothing to coordinate.

Shape is the house sidecar shape (roll_counter.py, aup_watchdog.py): tail
bot.log, match, write verbs into the bot.send FIFO.

Output is canonical iroffer/XDCC LIST (vjt order 2026-07-10, "facciamolo
bene") so a victim running an xdcc-catcher parses it and the bestemmia lands
in the filename field of their download manager. That is why bestemmie live
INSIDE the scene name and never in prose: prose does not survive the parse.

Entries are GENERATED, not drawn from a fixed list — combinatorics over
titles x tags x groups x sizes, so the gag is effectively never the same
twice ("una serie di sequenze e voci ben nutrite cosi che sia sempre nuovo e
variegato", same order).
"""

import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/home/vjt/code/IRC/vjt-claude")
LOG = ROOT / "bot.log"
FIFO = ROOT / "bot.send"
MUTES = ROOT / "memory" / "project_active_mutes.md"

# Where the gag is live. #it-opers was dropped by vjt on 2026-08-07 ("deve
# rispondere solo su sniffo e sbiffo") — it is a working channel, not a
# playground.
CHANNELS = {"#sniffo", "#sbiffo"}

# The gag outranks the mute — vjt ruled it explicitly on 2026-08-07 ("no non
# deve tacere"), and only vjt can override a mute. Scope is narrow on purpose:
# THIS sidecar answers `!list` on a muted channel, the session itself stays
# muted for everything else. Flip back to True if that ruling is withdrawn.
RESPECT_MUTES = False

# Nicks we never answer: ourselves, and the other bots. A bot firing `!list`
# at another bot is how you get a two-machine flood and a k-line.
IGNORE_NICKS = {"vjt-claude", "cristobot", "trillian", "gazzurbo", "ottavia"}

# Same nick re-firing inside this window gets one burbero line, not a second
# dump. Repeated full dumps are flood, and flood is cringe.
THROTTLE_S = 60

# Pause between emitted lines. A dump is six lines; without a gap the ircd
# hands us excess flood halfway through the recital.
LINE_GAP_S = 1.2

PRIVMSG_PAT = re.compile(
    r"< :(?P<nick>[^!@\s]+)!\S+\s+PRIVMSG\s+(?P<chan>#\S+)\s+:(?P<text>.*?)$"
)

# `!list`, `!List`, `! list`, trailing punctuation — the bait is typed by
# people who are not being careful. Anchored so `whitelist` never matches.
LIST_PAT = re.compile(r"^\s*!\s*list\b[\s.!?]*$", re.IGNORECASE)


# ---------------------------------------------------------------- generation

TITLES = [
    "Windows.XP.Corporate.FCKGW",
    "Windows.98.SE.OEM.ITA",
    "Photoshop.7.0.ITA",
    "Nero.Burning.ROM.6",
    "WinZip.8.1.ITA",
    "Norton.Antivirus.2003",
    "Office.XP.Professional.ITA",
    "3DStudio.Max.5.0",
    "AutoCAD.2002.ITA",
    "Napster.Beta.Leaked",
    "Kazaa.Lite.NoSpyware",
    "GetRight.5.0.RETAIL",
    "SubSeven.2.1.Bonus",
    "mIRC.6.03.REGGED",
    "Matrix.Reloaded.TS.ITA",
    "Il.Signore.Degli.Anelli.SCREENER",
    "Half.Life.2.ALPHA.LEAK",
    "Diablo.II.LOD.NOCD",
    "Counter.Strike.1.6.FULL",
    "Encarta.2003.ITA.DVD",
    "Divx.Codec.5.02.PRO",
    "Winamp.2.95.CLASSIC",
    "ICQ.2003b.NoAds",
    "Age.Of.Empires.II.ITA",
    "Grand.Theft.Auto.Vice.City",
]

# Rationed: the SignalOS legend is canon, not a running joke. One in ten.
LEGEND_TITLES = [
    "SignalOS.1.0.FINAL.BOOTABLE",
    "SignalOS.0.9b.SOURCE.LEAKED",
]

TAGS = [
    "CRACK", "KEYGEN", "SERIAL", "ISO", "REPACK", "PROPER", "RETAIL",
    "NOCD", "REGGED", "DVDRIP.XVID", "TS.ITA", "WORKING", "FIXED",
]

# Bestemmie as scene tags: uppercase, no spaces, so they survive the
# filename-field parse. Rotate the whole canon, never three of a kind.
BESTEMMIA_TAGS = [
    "PORCODIO", "DIOBOIA", "DIOBESTIA", "DIOCANE", "DIOSTRONZO",
    "MADONNAPUTTANA", "PORCAMADONNA", "MADONNAMAIALA", "CRISTOCANE",
    "DIOSERPENTE", "DIOLADRO", "PORCOGESU", "MADONNAIMPESTATA",
    "DIOCANARO", "CRISTOSTORTO",
]

GROUPS = [
    "cristocane", "RazorCD", "nfo", "PORCODIOteam", "CLASS", "DEVIANCE",
    "RAZOR1911", "FAIRLIGHT", "diobestia", "TNT-Village", "PARADOX",
    "madonnaporca", "DIOBOIAcrew", "ECHELON", "porcodioSFX",
]

SIZES = [
    "1.2G", "699M", "4.4G", "350M", "8.5G", "2.1G", "1.44M", "650M",
    "13.9G", "47M", "3.7G", "128M",
]

# Nonexistent nicks: the /msg goes nowhere, and that IS the joke. Never a
# real nick, never ours — a DM to us burns tokens (vjt order 2026-07-10).
FAKE_NICKS = ["madonna-porca", "porco-dio", "cristo-cane", "dio-bestia",
              "madonna-maiala", "dio-serpente"]

SLOT_NOTES = [
    "Record: 6666.6kB/s",
    "Record: 666.6kB/s",
    "Record: 1.44kB/s",
    "Record: 9999.9kB/s",
]

# The cross-promo vjt asked for: a real CristoBOT command, not an invented
# one (`dio` is in its core command set — CristoBOT CLAUDE.md, "Commands").
CROSS_PROMO = [
    "** Per sapere di piu' prova !dio, !madonna o !bestemmia — CristoBOT serve h24 **",
    "** Fuori catalogo? chiedi a CristoBOT: !dio, !santo, !storiella **",
    "** Assistenza clienti: !dio a CristoBOT. Non risponde il centralino, risponde Lui **",
    "** Il reparto teologico e' di CristoBOT: !bestemmia, !dio, !papa **",
]

THROTTLE_LINES = [
    "{nick}: il catalogo e' quello di dieci secondi fa, porco dio. Aspetta.",
    "{nick}: due !list di fila? Il server FTP e' uno solo e ha gia' fumato.",
    "{nick}: madonna che fretta. Il listino non e' cambiato.",
    "{nick}: rileggi quello sopra, dio bestia, non ne stampo un altro.",
]


def scene_name(rng, bestemmia, title):
    """One filename token: no spaces, bestemmia inside, scene group suffix.

    The caller picks both the bestemmia and the title so a dump can
    guarantee three distinct ones; picking them here and patching afterwards
    is how you end up replacing a tag the name never had.
    """
    # Some titles already carry a tag ("...SCREENER", "...TS.ITA"). Picking a
    # tag the title already ends with produces `Matrix.Reloaded.TS.ITA.TS.ITA`,
    # which reads as a broken generator rather than a scene name.
    candidates = [t for t in TAGS if t not in title]
    tag = rng.choice(candidates or TAGS)
    return ".".join([title, tag, bestemmia]) + "-" + rng.choice(GROUPS)


def pick_titles(rng, count):
    """`count` distinct titles, with the SignalOS legend rationed to ~1 in 10.

    The legend is canon ([[reference_signalos_canon]]), not a running joke;
    seeing it in every dump is how canon turns into a catchphrase.
    """
    titles = rng.sample(TITLES, count)
    if rng.random() < 0.10:
        titles[rng.randrange(count)] = rng.choice(LEGEND_TITLES)
    return titles


def build_dump(rng):
    """Header + three pack lines + footer + cross-promo, per the standing order."""
    packs = 3
    open_slots = rng.randint(0, 3)
    fake = rng.choice(FAKE_NICKS)

    lines = [
        f"** {packs} packs **  {open_slots} of 3 slots open, {rng.choice(SLOT_NOTES)}",
        f"** To request a file, type: /msg {fake} xdcc send #N **",
    ]

    # Three DISTINCT bestemmie — a dump with the same tag three times reads
    # like a broken generator, not a gag.
    tags = rng.sample(BESTEMMIA_TAGS, packs)
    titles = pick_titles(rng, packs)
    total = 0.0
    for i, (tag, title) in enumerate(zip(tags, titles), start=1):
        name = scene_name(rng, tag, title)
        size = rng.choice(SIZES)
        downloads = rng.randint(1, 99)
        lines.append(f"#{i}  {downloads}x [{size}] {name}")
        total += _gigs(size)

    lines.append(
        f"** Total Offered: {total:.1f}G  Total Transferred: "
        f"{total * rng.uniform(8, 99):.1f}G **"
    )
    lines.append(rng.choice(CROSS_PROMO))
    return lines


def _gigs(size):
    unit = size[-1]
    value = float(size[:-1])
    return value if unit == "G" else value / 1024.0


# ------------------------------------------------------------------- plumbing


def muted_channels():
    """Channels muted right now, read from the SAME file the session reads.

    One source of truth: a second copy of the mute state is a second copy to
    forget to update. Everything under `## Active` up to the next heading.
    """
    if not RESPECT_MUTES or not MUTES.exists():
        return set()
    try:
        text = MUTES.read_text(encoding="utf-8")
    except OSError:
        return set()  # unreadable state must not silence the gag forever
    active = re.search(r"^## Active\s*$(.*?)^## ", text, re.M | re.S)
    if not active:
        return set()
    return set(re.findall(r"(#[A-Za-z0-9_\-]+)", active.group(1)))


def say(chan, message):
    with FIFO.open("w") as fifo:
        fifo.write(f"SAY {chan} {message}\n")


def handle(nick, chan, rng, last_fired):
    key = (nick.lower(), chan.lower())
    now = time.time()
    previous = last_fired.get(key, 0.0)
    last_fired[key] = now

    if now - previous < THROTTLE_S:
        say(chan, rng.choice(THROTTLE_LINES).format(nick=nick))
        return

    for line in build_dump(rng):
        say(chan, line)
        time.sleep(LINE_GAP_S)


def tail(path):
    """Follow the log across rotation (bot.log is rotated under us)."""
    while True:
        try:
            handle_ = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            time.sleep(2)
            continue
        handle_.seek(0, os.SEEK_END)
        inode = os.fstat(handle_.fileno()).st_ino
        while True:
            line = handle_.readline()
            if line:
                yield line
                continue
            time.sleep(0.4)
            try:
                if path.stat().st_ino != inode:
                    handle_.close()
                    break
            except OSError:
                handle_.close()
                break


def main():
    rng = random.Random()
    last_fired = {}
    for line in tail(LOG):
        match = PRIVMSG_PAT.search(line)
        if not match:
            continue
        nick, chan, text = match["nick"], match["chan"], match["text"]
        if nick.lower() in IGNORE_NICKS:
            continue
        if chan not in CHANNELS or not LIST_PAT.match(text):
            continue
        if chan in muted_channels():
            continue
        try:
            handle(nick, chan, rng, last_fired)
        except OSError as exc:
            print(f"list_sidecar: FIFO write failed: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
