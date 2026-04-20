#!/usr/bin/env python3
import json, re, glob, subprocess, sys, time, os
from pathlib import Path
from datetime import datetime, date, timedelta

STATE = Path("/home/vjt/code/IRC/vjt-claude/rolls.json")
LOG = "/home/vjt/code/IRC/vjt-claude/bot.log"

# Parse "HH:MM:SS " prefix that bot.py stamps at start of each log line.
LOG_TS_PAT = re.compile(r'^(\d{2}):(\d{2}):(\d{2})\s')

PRIVMSG_PAT = re.compile(
    r'< :(?P<nick>[^!@\s]+)!\S+\s+PRIVMSG\s+(?P<chan>#\S+)\s+:(?P<text>.*?)$',
    re.MULTILINE,
)

# Trillian is an IRC↔Telegram bridge. Messages arrive from nick "Trillian"
# with the real speaker wrapped as "<tgnick> rest of message". Unwrap so
# stats attribute to the human, not the relay.
BRIDGE_NICKS = {"Trillian"}
BRIDGE_PREFIX_PAT = re.compile(r'^<([^>\s]+)>\s?(.*)$', re.DOTALL)

# Nick aliasing — fold AFK/secondary nicks under a canonical identity so
# per-person totals don't get split across `vjt` / `vjt42` / `vjt\`zZz`.
# Grow this table as new aliases show up in chan. Applied at ingest, so
# rebackfill (flip backfilled=False + restart service) migrates history.
NICK_ALIASES = {
    "vjt`afk": "vjt",
    "vjt`zZz": "vjt",
    "vjt42": "vjt",
    "vjt_": "vjt",
}


def canon_nick(n):
    return NICK_ALIASES.get(n, n)

ACTION_CMD_PAT = re.compile(
    r'^ACTION\s+::(?P<cmd>[A-Za-z_][A-Za-z0-9_]*)'
    r'(?:\((?:"(?P<variant>[^"]*)")?\))?[;!]?\s*$'
)

BLASPHEMY_PAT = re.compile(
    r'(?i)(?<![a-zà-ù])('
    r'(?:(?P<intens>porc[oa]|porcaccio)\s+'
    r'(?P<subj1>di[oa]|cristo|madonn[ae]|gesù|gesu|signore))'
    r'|'
    r'(?:(?P<subj2>dio|cristo|madonn[ae]|gesù|gesu|signore)\s+'
    r"(?P<epi>[a-zà-ù']{3,}))"
    r')(?![a-zà-ù])'
)

# Tokens that can legitimately follow dio/cristo/madonna/gesù without
# counting as blasphemy: possessives, definite/indefinite articles + prep,
# the rest of a compound holy name, reverent adjectives, common adverbs.
# Growing this list is the maintenance path — matcher is open-set so any
# creative insult ("lazzarone", "canchero", "imbufalita", "belva", …) is
# caught automatically; only reverent/structural neighbours need naming.
BLASPHEMY_STOPWORDS = frozenset({
    # possessives / pronouns
    "mio", "mia", "miei", "mie", "tuo", "tua", "tuoi", "tue",
    "nostro", "nostra", "nostri", "nostre",
    "ti", "ci", "mi", "si", "li", "le", "lo", "la", "gli",
    # reverent epithets
    "santo", "santa", "santi", "sante", "santissimo", "santissima",
    "benedetto", "benedetta", "benedetti", "benedette", "benedica",
    "onnipotente", "immacolata", "addolorata", "misericordioso",
    "misericordiosa", "vergine", "protettore", "protettrice",
    # compound holy names / liturgy
    "cristo", "maria", "giuseppe", "bambino", "bambina", "bambini",
    "padre", "figlio", "spirito",
    # common prepositions / articles / fillers
    "di", "del", "della", "delle", "dei", "degli", "al", "alla",
    "in", "con", "per", "tra", "fra", "se", "che", "chi",
    "e", "o", "ma", "non", "sì", "si",
    # very common verbs in reverent phrases
    "è", "sia", "sa", "ha", "era", "sono", "sei", "fu",
    "aiutaci", "salvaci", "perdonami", "perdonaci", "proteggici",
    "grazie", "prega", "benedici", "amen",
    # frequent false positives seen in Italian chat
    "caro", "cara", "buono", "buona", "amore", "amori", "mio",
    "pietà", "celeste", "assunta",
})

CONCAT_PAT = re.compile(
    r'(?i)(?<![a-zà-ù])(?P<concat>'
    r'porc(?:odd?io|ocristo|amadonna|ogiuda|addio|amiseria|hetta|opupazzo|ogesù|ogesu)|'
    r'madonn(?:apputtana|apampisa|aladra|amerda|atroia|abastarda|apuzzona|amannara|'
    r'aimbufalita|araggrinzita|aimpestata)|'
    r'dio(?:f[ae]|can[ei]|merd[ae]?|boi[ae]|lup[oi]|porc[oi]|str(?:onzo)?|'
    r'bestia|ladr[oi]|schifoso|catamarano|giuda|maiale|vacca|cacchio|bastardo|'
    r'belva|lazzarone|canchero|imbecille|imbufalit[ao]|stramaledetto)|'
    r'cristo(?:can[ei]|merd[ae]?|boi[ae]|porc[oi])|'
    r'ges(?:ù|u)(?:can[ei]|bambin[oi]|lazzarone)'
    r')(?![a-zà-ù])'
)

SUBJECT_CANON = {
    "dio": "dio", "dia": "dio",
    "cristo": "cristo",
    "madonna": "madonna", "madonne": "madonna",
    "gesù": "gesù", "gesu": "gesù",
}


def canon_subj(s):
    if not s:
        return None
    return SUBJECT_CANON.get(s.lower(), s.lower())


def empty():
    return {
        "by_cmd": {},
        "blasphemy": {
            "total": {},
            "per_channel": {},
            "by_subject": {},
            "epithets": {},
            "concat": {},
        },
        "events": [],
        "backfilled": False,
    }


def load():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return empty()


def save(data):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))
    tmp.replace(STATE)


def bump(d, *keys):
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = d.get(keys[-1], 0) + 1


def add_event(data, ts, nick, chan, kind, **extra):
    if ts is None:
        return
    ev = {"ts": int(ts), "nick": nick, "chan": chan, "kind": kind}
    ev.update(extra)
    data.setdefault("events", []).append(ev)


def process_action_cmd(text, nick, chan, data, ts=None):
    m = ACTION_CMD_PAT.match(text)
    if not m:
        return False
    cmd = m.group("cmd")
    variant = m.group("variant") or ""
    by_cmd = data.setdefault("by_cmd", {})
    entry = by_cmd.setdefault(cmd, {"total": {}, "per_channel": {}, "variants": {}})
    bump(entry["total"], nick)
    bump(entry["per_channel"], chan, nick)
    bump(entry["variants"], variant, nick)
    add_event(data, ts, nick, chan, "cmd", cmd=cmd, variant=variant)
    return True


def process_blasphemy(text, nick, chan, data, ts=None):
    hit = False
    blas = data.setdefault("blasphemy", empty()["blasphemy"])
    for m in BLASPHEMY_PAT.finditer(text):
        subj = canon_subj(m.group("subj1") or m.group("subj2"))
        epi = m.group("epi")
        intens = m.group("intens")
        # Open-slot form (subj + any token): reject when the token is a
        # reverent/structural neighbour. Intensifier form (porco + subj)
        # is always a hit — no stopword gate.
        if epi and epi.lower() in BLASPHEMY_STOPWORDS:
            continue
        bump(blas["total"], nick)
        bump(blas["per_channel"], chan, nick)
        bump(blas["by_subject"], subj, nick)
        if epi:
            blas.setdefault("epithets", {}).setdefault(nick, {})
            blas["epithets"][nick][epi.lower()] = (
                blas["epithets"][nick].get(epi.lower(), 0) + 1
            )
        add_event(data, ts, nick, chan, "blasphemy", subj=subj, epi=(epi or "").lower(), intens=(intens or "").lower())
        hit = True
    for m in CONCAT_PAT.finditer(text):
        form = m.group("concat").lower()
        bump(blas["total"], nick)
        bump(blas["per_channel"], chan, nick)
        blas.setdefault("concat", {}).setdefault(nick, {})
        blas["concat"][nick][form] = blas["concat"][nick].get(form, 0) + 1
        add_event(data, ts, nick, chan, "concat", form=form)
        hit = True
    return hit


def process(line, data, ts=None):
    m = PRIVMSG_PAT.search(line)
    if not m:
        return False
    nick = m.group("nick")
    chan = m.group("chan")
    text = m.group("text")
    if text.startswith("\x01") and text.endswith("\x01"):
        text = text[1:-1]
    if nick in BRIDGE_NICKS:
        bm = BRIDGE_PREFIX_PAT.match(text)
        if bm:
            nick = bm.group(1)
            text = bm.group(2)
    nick = canon_nick(nick)
    hit = False
    if text.startswith("ACTION "):
        action_text = text  # keep "ACTION ..." prefix for ACTION_CMD_PAT
        if process_action_cmd(action_text, nick, chan, data, ts=ts):
            hit = True
        text = text[len("ACTION "):]
    if process_blasphemy(text, nick, chan, data, ts=ts):
        hit = True
    return hit


def backfill(data):
    """Walk every archived + live bot.log, attribute each line a unix ts.

    bot.log lines are stamped `HH:MM:SS ...` with no date. Anchor the last
    line's date to the file mtime (today if live); walk forward counting
    monotonic-decrease jumps in the HH:MM:SS as midnight rollovers. This
    is approximate — a cold-start restart that straddles midnight is
    indistinguishable from a pure rollover — but good enough for day/week
    buckets since restarts are rare.
    """
    for fp in sorted(glob.glob(LOG + "*")):
        try:
            mtime = os.path.getmtime(fp)
            mtime_date = datetime.fromtimestamp(mtime).date()
            # First pass: count total day rollovers so we can anchor the
            # final line to mtime_date and back-date earlier chunks.
            prev_hms = None
            total_rolls = 0
            with open(fp, errors="replace") as f:
                for raw in f:
                    tm = LOG_TS_PAT.match(raw)
                    if not tm:
                        continue
                    hms = (int(tm.group(1)), int(tm.group(2)), int(tm.group(3)))
                    if prev_hms is not None and hms < prev_hms:
                        total_rolls += 1
                    prev_hms = hms
            start_date = mtime_date - timedelta(days=total_rolls)
            # Second pass: assign per-line ts and process.
            prev_hms = None
            day_offset = 0
            with open(fp, errors="replace") as f:
                for raw in f:
                    tm = LOG_TS_PAT.match(raw)
                    ts = None
                    if tm:
                        hms = (int(tm.group(1)), int(tm.group(2)), int(tm.group(3)))
                        if prev_hms is not None and hms < prev_hms:
                            day_offset += 1
                        prev_hms = hms
                        d = start_date + timedelta(days=day_offset)
                        ts = datetime(d.year, d.month, d.day, *hms).timestamp()
                    process(raw, data, ts=ts)
        except Exception:
            pass
    data["backfilled"] = True
    save(data)


def stats_cmd(args):
    """Print a leaderboard from rolls.json. Usage: roll_counter.py stats [N].
    N caps each top-list (default 10). Same parsing schema as the daemon
    writes, so this is the single source of truth for reading the state."""
    top = 10
    if args and args[0].isdigit():
        top = int(args[0])
    data = load()
    blas = data.get("blasphemy", {})
    total = blas.get("total", {})
    concat = blas.get("concat", {})
    by_subj = blas.get("by_subject", {})
    rolls = data.get("by_cmd", {}).get("Roll", {})

    def _sort_desc(d):
        return sorted(d.items(), key=lambda x: -x[1])

    print(f"🏆 BESTEMMIOMETRO (top {top}, totale eventi):")
    for n, c in _sort_desc(total)[:top]:
        print(f"  {n}: {c}")

    all_forms: dict[str, int] = {}
    for n, vv in concat.items():
        for f, c in vv.items():
            all_forms[f] = all_forms.get(f, 0) + c
    print(f"\n🔥 CONCAT forms (top {top}):")
    for f, c in _sort_desc(all_forms)[:top]:
        print(f"  {f}: {c}")

    print(f"\n🧬 concat creatività (varianti uniche per nick, top {top}):")
    creativity = {n: len(v) for n, v in concat.items()}
    for n, c in _sort_desc(creativity)[:top]:
        print(f"  {n}: {c}")

    print(f"\n🎯 subject breakdown (eventi per soggetto canonico):")
    subj_totals: dict[str, int] = {}
    for subj, per_nick in by_subj.items():
        subj_totals[subj] = sum(per_nick.values())
    for s, c in _sort_desc(subj_totals):
        print(f"  {s}: {c}")

    print(f"\n🎲 ::Roll:")
    roll_total = rolls.get("total", {})
    roll_variants = rolls.get("variants", {})
    if not roll_total:
        print("  (nessun roll registrato)")
    else:
        for n, c in _sort_desc(roll_total):
            vars_for_n = [
                (v or "vanilla", d.get(n, 0))
                for v, d in roll_variants.items()
                if d.get(n)
            ]
            vs = " ".join(f"{v}×{c2}" for v, c2 in vars_for_n)
            print(f"  {n}: {c} ({vs})")

    print(
        f"\n📊 grand: {sum(total.values())} bestemmie, "
        f"{len(total)} nick, {len(all_forms)} concat forms"
    )


RANGE_PERIODS = {
    "day": 86400,
    "week": 7 * 86400,
    "month": 30 * 86400,
    "year": 365 * 86400,
    "all": None,
}


def range_cmd(args):
    """Leaderboard filtered by rolling time window. Usage:
    roll_counter.py range <day|week|month|year|all> [top] [--irc]
    --irc switches to compact multi-line format suited for PRIVMSG."""
    irc = False
    if "--irc" in args:
        irc = True
        args = [a for a in args if a != "--irc"]
    if not args or args[0] not in RANGE_PERIODS:
        print(f"usage: roll_counter.py range <{'|'.join(RANGE_PERIODS)}> [top] [--irc]",
              file=sys.stderr)
        sys.exit(2)
    period = args[0]
    top = int(args[1]) if len(args) > 1 and args[1].isdigit() else (5 if irc else 10)
    data = load()
    events = data.get("events", [])
    window = RANGE_PERIODS[period]
    cutoff = time.time() - window if window else 0
    filtered = [e for e in events if e.get("ts", 0) >= cutoff]

    total, concat_forms, subjects = {}, {}, {}
    concat_per_nick = {}
    rolls, roll_variants = {}, {}
    for e in filtered:
        n, k = e.get("nick"), e.get("kind")
        if k == "blasphemy":
            total[n] = total.get(n, 0) + 1
            s = e.get("subj")
            if s:
                subjects[s] = subjects.get(s, 0) + 1
        elif k == "concat":
            total[n] = total.get(n, 0) + 1
            f = e.get("form", "")
            concat_forms[f] = concat_forms.get(f, 0) + 1
            concat_per_nick.setdefault(n, set()).add(f)
        elif k == "cmd" and e.get("cmd") == "Roll":
            rolls[n] = rolls.get(n, 0) + 1
            v = e.get("variant") or "vanilla"
            roll_variants.setdefault(v, {})
            roll_variants[v][n] = roll_variants[v].get(n, 0) + 1

    def _sort_desc(d):
        return sorted(d.items(), key=lambda x: -x[1])

    label = "all-time" if period == "all" else f"ultimi {period}"

    if irc:
        # Compact multi-line format for PRIVMSG: one line per axis,
        # each ≤ ~360 chars to stay safely under the 512-byte IRC limit
        # after the bot prepends `:nick!user@host PRIVMSG #chan :`.
        header = f"📅 {label} · {len(filtered)} eventi · top {top}"
        top_bast = " ".join(f"{n}:{c}" for n, c in _sort_desc(total)[:top]) or "(vuoto)"
        top_concat = " ".join(f"{f}×{c}" for f, c in _sort_desc(concat_forms)[:top]) or "(vuoto)"
        top_subj = " ".join(f"{s}:{c}" for s, c in _sort_desc(subjects)) or "(vuoto)"
        if rolls:
            parts = []
            for n, c in _sort_desc(rolls):
                vs = " ".join(
                    f"{v}×{roll_variants[v].get(n, 0)}"
                    for v in roll_variants if roll_variants[v].get(n)
                )
                parts.append(f"{n}:{c}({vs})")
            rolls_line = " ".join(parts)
        else:
            rolls_line = "(nessun roll)"
        print(header)
        print(f"🏆 {top_bast}")
        print(f"🔥 {top_concat}")
        print(f"🎯 {top_subj}")
        print(f"🎲 {rolls_line}")
        return

    print(f"🏆 BESTEMMIOMETRO — {label} (top {top}, {len(filtered)} eventi):")
    if not total:
        print("  (nessun evento nel periodo)")
    for n, c in _sort_desc(total)[:top]:
        print(f"  {n}: {c}")

    print(f"\n🔥 CONCAT forms (top {top}):")
    for f, c in _sort_desc(concat_forms)[:top]:
        print(f"  {f}: {c}")

    print(f"\n🧬 concat creatività (varianti uniche per nick, top {top}):")
    creativity = {n: len(v) for n, v in concat_per_nick.items()}
    for n, c in _sort_desc(creativity)[:top]:
        print(f"  {n}: {c}")

    print(f"\n🎯 subject breakdown:")
    if not subjects:
        print("  (nessuno)")
    for s, c in _sort_desc(subjects):
        print(f"  {s}: {c}")

    print(f"\n🎲 ::Roll:")
    if not rolls:
        print("  (nessun roll)")
    else:
        for n, c in _sort_desc(rolls):
            vs = " ".join(
                f"{v}×{roll_variants[v].get(n, 0)}"
                for v in roll_variants if roll_variants[v].get(n)
            )
            print(f"  {n}: {c} ({vs})")

    print(
        f"\n📊 window: {label}, "
        f"{sum(total.values())} bestemmie, "
        f"{len(total)} nick, {len(concat_forms)} concat forms"
    )


def daemon():
    data = load()
    if not data.get("backfilled") or "blasphemy" not in data:
        data = empty()
        backfill(data)
    p = subprocess.Popen(
        ["tail", "-F", "-n", "0", LOG],
        stdout=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    for line in p.stdout:
        if process(line, data, ts=time.time()):
            save(data)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        stats_cmd(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "range":
        range_cmd(sys.argv[2:])
        return
    daemon()


if __name__ == "__main__":
    main()
