#!/usr/bin/env python3
import json, re, glob, subprocess, sys
from pathlib import Path

STATE = Path("/home/vjt/code/claude-chatbot/rolls.json")
LOG = "/home/vjt/code/claude-chatbot/bot.log"

PRIVMSG_PAT = re.compile(
    r'< :(?P<nick>[^!@\s]+)!\S+\s+PRIVMSG\s+(?P<chan>#\S+)\s+:(?P<text>.*?)$',
    re.MULTILINE,
)

ACTION_CMD_PAT = re.compile(
    r'^ACTION\s+::(?P<cmd>[A-Za-z_][A-Za-z0-9_]*)'
    r'(?:\((?:"(?P<variant>[^"]*)")?\))?[;!]?\s*$'
)

BLASPHEMY_PAT = re.compile(
    r'(?i)(?<![a-zà-ù])('
    r'(?:(?P<intens>porc[oa]|porcaccio)\s+'
    r'(?P<subj1>di[oa]|cristo|madonn[ae]|gesù|gesu|cristoforo))'
    r'|'
    r'(?:(?P<subj2>dio|cristo|madonn[ae]|gesù|gesu|cristoforo)\s+'
    r'(?P<epi>cane|troia|ladr[oa]|bestia|merda|boia|bastard[oa]|lupo|porc[oa]|'
    r'schifos[oa]|puttana|cacchio|diavolo|maial[ei]|vacca|zozz[oa]|'
    r'stronz[oa]|fottut[oa]|impiccat[oa]|ruffian[oa]))'
    r')(?![a-zà-ù])'
)

CONCAT_PAT = re.compile(
    r'(?i)(?<![a-zà-ù])(?P<concat>'
    r'porc(?:odd?io|ocristo|amadonna|ogiuda|addio|amiseria|hetta|opupazzo|ogesù|ogesu)|'
    r'madonn(?:apputtana|apampisa|aladra|amerda|atroia|abastarda|apuzzona|amannara)|'
    r'dio(?:f[ae]|can[ei]|merd[ae]?|boi[ae]|lup[oi]|porc[oi]|str(?:onzo)?|'
    r'bestia|ladr[oi]|schifoso|catamarano|giuda|maiale|vacca|cacchio|bastardo)|'
    r'cristo(?:can[ei]|merd[ae]?|boi[ae]|porc[oi])|'
    r'ges(?:ù|u)(?:can[ei]|bambin[oi])'
    r')(?![a-zà-ù])'
)

SUBJECT_CANON = {
    "dio": "dio", "dia": "dio",
    "cristo": "cristo", "cristoforo": "cristo",
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


def process_action_cmd(text, nick, chan, data):
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
    return True


def process_blasphemy(text, nick, chan, data):
    hit = False
    blas = data.setdefault("blasphemy", empty()["blasphemy"])
    for m in BLASPHEMY_PAT.finditer(text):
        subj = canon_subj(m.group("subj1") or m.group("subj2"))
        epi = m.group("epi")
        intens = m.group("intens")
        bump(blas["total"], nick)
        bump(blas["per_channel"], chan, nick)
        bump(blas["by_subject"], subj, nick)
        if epi:
            blas.setdefault("epithets", {}).setdefault(nick, {})
            blas["epithets"][nick][epi.lower()] = (
                blas["epithets"][nick].get(epi.lower(), 0) + 1
            )
        hit = True
    for m in CONCAT_PAT.finditer(text):
        form = m.group("concat").lower()
        bump(blas["total"], nick)
        bump(blas["per_channel"], chan, nick)
        blas.setdefault("concat", {}).setdefault(nick, {})
        blas["concat"][nick][form] = blas["concat"][nick].get(form, 0) + 1
        hit = True
    return hit


def process(line, data):
    m = PRIVMSG_PAT.search(line)
    if not m:
        return False
    nick = m.group("nick")
    chan = m.group("chan")
    text = m.group("text")
    if text.startswith("\x01") and text.endswith("\x01"):
        text = text[1:-1]
    hit = False
    if text.startswith("ACTION "):
        action_text = text  # keep "ACTION ..." prefix for ACTION_CMD_PAT
        if process_action_cmd(action_text, nick, chan, data):
            hit = True
        text = text[len("ACTION "):]
    if process_blasphemy(text, nick, chan, data):
        hit = True
    return hit


def backfill(data):
    for fp in sorted(glob.glob(LOG + "*")):
        try:
            with open(fp, errors="replace") as f:
                for line in f:
                    process(line, data)
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
        if process(line, data):
            save(data)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        stats_cmd(sys.argv[2:])
        return
    daemon()


if __name__ == "__main__":
    main()
