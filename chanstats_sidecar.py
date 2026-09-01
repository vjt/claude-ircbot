#!/usr/bin/env python3
"""!chanstats sidecar — channel stats summary in channel.

vjt order (#sbiffo, 2026-08-27 14:08): "serve un !chanstats che fa il sunto
delle chanstats che mostriamo su web, ma che include anche le roll stats".

Two sources, both already on disk, both read-only here:

  * ~/code/sniffo_stats/stats.db — the SQLite the web chanstats are rendered
    from (volumes, classes, talkers, bestemmie per 100 messages, the tier
    ladder). This sidecar NEVER touches the renderer or the salt.
  * rolls.json — the roll_counter sidecar state (::Roll / ::Dab, blasphemy
    forms). Same numbers `stats.py` prints, scoped to one channel.

CONFIDENTIALITY: the web pages live under a keyed-hash slug precisely so the
URLs are unguessable. This sidecar prints NUMBERS ONLY — never the slug, never
a URL. Whoever wants the page already knows where it is.

Shape is the house sidecar shape (roll_counter.py, list_sidecar.py): tail
bot.log, match, write verbs into the bot.send FIFO. Non-blocking FIFO open, so
a dead bot means a skipped line and not a wedged daemon.

Mutes: an explicitly typed command outranks the channel mute, exactly like the
`!list` gag — the mute silences the session's own chatter, not a thing someone
asked for by name. Same ruling shape as list_sidecar.RESPECT_MUTES=False.

Usage:
  chanstats_sidecar.py                 # daemon (tail bot.log)
  chanstats_sidecar.py --once '#chan'  # print the lines, send nothing
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path("/home/vjt/code/IRC/vjt-claude")
LOG = os.environ.get("CHANSTATS_LOG", str(ROOT / "bot.log"))
FIFO = os.environ.get("CHANSTATS_FIFO", str(ROOT / "bot.send"))
ROLLS = ROOT / "rolls.json"
STATS_DB = Path(os.environ.get(
    "CHANSTATS_DB", str(Path.home() / "code" / "sniffo_stats" / "stats.db")))
# bot.log is one network per process; the stats DB keys by network name.
NETWORK = os.environ.get("CHANSTATS_NETWORK", "azzurra")

_TZ = ZoneInfo("Europe/Rome")

# Where the command answers. Working channels included on purpose: this is a
# read-only summary someone asked for, not a gag.
CHANNELS = {"#sniffo", "#sbiffo", "#it-opers"}

# Same ladder as sniffo_stats/metrics.py — copied, not imported, so this
# sidecar has zero import-path coupling to that repo. Keep in sync by hand;
# it has moved once in four months.
BESTEMMIA_TIERS = [
    (0.5, "catechismo"),
    (2.0, "chierichetto"),
    (5.0, "peccatore"),
    (12.0, "bestemmiatore"),
    (float("inf"), "super bestemmiato"),
]

COOLDOWN_SEC = 30

PRIVMSG_PAT = re.compile(
    r'< :(?P<nick>[^!@\s]+)!\S+\s+PRIVMSG\s+(?P<chan>#\S+)\s+:(?P<text>.*?)$')
CMD_PAT = re.compile(r'^\s*!chanstats(?:\s+(?P<target>#\S+))?\s*$', re.I)


def tier(per100):
    for hi, name in BESTEMMIA_TIERS:
        if per100 < hi:
            return name
    return BESTEMMIA_TIERS[-1][1]


def fmt_top(pairs):
    return " ".join(f"{n}={c}" for n, c in pairs) or "-"


def db_summary(channel):
    """Numbers for one channel out of the web-stats DB. Read-only, and opened
    read-only in the URI sense too — the renderer owns that file."""
    if not STATS_DB.exists():
        return None
    conn = sqlite3.connect(f"file:{STATS_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT klass, COUNT(*), SUM(bestemmie) FROM events "
            "WHERE network=? AND channel=? GROUP BY klass",
            (NETWORK, channel)).fetchall()
        if not rows:
            return None
        by_klass = {k: c for k, c, _b in rows}
        total = sum(by_klass.values())
        best_total = sum(int(b or 0) for _k, _c, b in rows)

        span = conn.execute(
            "SELECT MIN(ts), MAX(ts) FROM events WHERE network=? AND channel=?",
            (NETWORK, channel)).fetchone()
        humans = conn.execute(
            "SELECT COUNT(DISTINCT nick) FROM events "
            "WHERE network=? AND channel=? AND klass='human'",
            (NETWORK, channel)).fetchone()[0]
        talkers = conn.execute(
            "SELECT nick, COUNT(*) c FROM events WHERE network=? AND channel=? "
            "AND klass='human' GROUP BY nick ORDER BY c DESC LIMIT 5",
            (NETWORK, channel)).fetchall()
        best_by = conn.execute(
            "SELECT nick, SUM(bestemmie) b FROM events WHERE network=? AND channel=? "
            "GROUP BY nick HAVING b>0 ORDER BY b DESC LIMIT 3",
            (NETWORK, channel)).fetchall()
        # Rome-day boundary, computed here and passed as epoch: the DB stores UTC.
        now = datetime.now(_TZ)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today = conn.execute(
            "SELECT COUNT(*) FROM events WHERE network=? AND channel=? AND ts>=?",
            (NETWORK, channel, int(midnight.timestamp()))).fetchone()[0]
        hourly = [0] * 24
        for (ts,) in conn.execute(
                "SELECT ts FROM events WHERE network=? AND channel=?",
                (NETWORK, channel)):
            hourly[datetime.fromtimestamp(ts, _TZ).hour] += 1
    finally:
        conn.close()

    days = max(1, (span[1] - span[0]) // 86400) if span and span[0] else 1
    return {
        "total": total,
        "human": by_klass.get("human", 0),
        "ai": by_klass.get("ai", 0),
        "classic": by_klass.get("classic", 0),
        "service": by_klass.get("service", 0),
        "humans": humans,
        "days": days,
        "today": today,
        "peak_hour": max(range(24), key=lambda h: hourly[h]),
        "talkers": talkers,
        "best_total": best_total,
        "best_by": best_by,
    }


def roll_summary(channel):
    """::Roll / ::Dab and blasphemy for this channel out of rolls.json."""
    try:
        d = json.loads(ROLLS.read_text())
    except (OSError, ValueError):
        return None
    out = {}
    for cmd in ("Roll", "Dab"):
        block = (d.get("by_cmd", {}).get(cmd) or {}).get("per_channel", {})
        nicks = block.get(channel, {})
        out[cmd] = (sum(int(v) for v in nicks.values()),
                    sorted(nicks.items(), key=lambda x: -x[1])[:2])
    blasph = (d.get("blasphemy", {}).get("per_channel", {}) or {}).get(channel, {})
    forms = defaultdict(int)
    for e in d.get("events", []):
        if e.get("chan") == channel and e.get("kind") == "concat":
            forms[str(e.get("form", ""))] += 1
    out["blasph_total"] = sum(int(v) for v in blasph.values())
    out["forms"] = sorted(forms.items(), key=lambda x: -x[1])[:3]
    return out


def build_lines(channel):
    db = db_summary(channel)
    if not db:
        return [f"{channel}: non ho una riga di stats per questo canale, porco dio."]
    rolls = roll_summary(channel) or {}
    bots = db["ai"] + db["classic"]
    per100 = 100.0 * db["best_total"] / (db["total"] or 1)
    lines = [
        f"{channel}: {db['total']} msg in {db['days']}g "
        f"(umani {db['human']}, AI {db['ai']}, bot classici {db['classic']}, "
        f"servizio {db['service']}) · {db['humans']} nick umani · "
        f"picco alle {db['peak_hour']:02d} · oggi {db['today']}",
        f"{channel}: top umani {fmt_top(db['talkers'])} · "
        f"rapporto bot/umani {bots / (db['human'] or 1):.2f}",
    ]
    third = (f"{channel}: bestemmie {db['best_total']} "
             f"({per100:.1f}/100 msg, tier «{tier(per100)}») · "
             f"top {fmt_top(db['best_by'])}")
    if rolls:
        roll_tot, roll_top = rolls.get("Roll", (0, []))
        dab_tot, dab_top = rolls.get("Dab", (0, []))
        third += (f" · ::Roll {roll_tot} [{fmt_top(roll_top)}] "
                  f"::Dab {dab_tot} [{fmt_top(dab_top)}]")
        if rolls.get("forms"):
            third += f" · forme {fmt_top(rolls['forms'])}"
    lines.append(third)
    return lines


def say(chan, text):
    """One line to the bot FIFO. Non-blocking open: with no reader we get
    ENXIO and skip, instead of hanging the daemon forever on a dead bot."""
    try:
        fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return
    try:
        # SAY:chanstats — origin tag, log-only; see bot.py process_cmd.
        os.write(fd, f"SAY:chanstats {chan} {text}\n".encode())
    except OSError:
        pass
    finally:
        os.close(fd)


def daemon():
    last = {}
    p = subprocess.Popen(["tail", "-F", "-n", "0", LOG],
                         stdout=subprocess.PIPE, text=True, errors="replace")
    for line in p.stdout:
        m = PRIVMSG_PAT.search(line)
        if not m:
            continue
        chan = m.group("chan")
        if chan not in CHANNELS:
            continue
        cmd = CMD_PAT.match(m.group("text"))
        if not cmd:
            continue
        target = cmd.group("target") or chan
        now = time.time()
        if now - last.get(chan, 0) < COOLDOWN_SEC:
            continue
        last[chan] = now
        for out in build_lines(target):
            say(chan, out)


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--once":
        for line in build_lines(sys.argv[2]):
            print(line)
        return 0
    daemon()
    return 0


if __name__ == "__main__":
    sys.exit(main())
