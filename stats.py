#!/usr/bin/env python3
"""Detailed IRC-safe stats summary from rolls.json.

Output: up to 5 lines, each PRIVMSG-safe (≤ ~420 bytes).

Usage:
  stats.py                     # print all lines to stdout
  stats.py --compact           # single-line summary
  stats.py --say <nick|#chan>  # send lines to IRC bridge FIFO
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

STATE = Path(__file__).parent / "rolls.json"
FIFO = Path(__file__).parent / "bot.send"


def fmt_top(pairs: list[tuple[str, int]]) -> str:
    return " ".join(f"{n}={c}" for n, c in pairs) or "-"


def flatten_cmd(cmd_block: dict) -> dict[str, int]:
    if not isinstance(cmd_block, dict):
        return {}
    t = cmd_block.get("total")
    if isinstance(t, dict) and t:
        return {k: int(v) for k, v in t.items()}
    totals: dict[str, int] = {}
    for _chan, nicks in cmd_block.get("per_channel", {}).items():
        if not isinstance(nicks, dict):
            continue
        for k, v in nicks.items():
            totals[k] = totals.get(k, 0) + int(v)
    return totals


def human_span(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"


def build_lines(compact: bool) -> list[str]:
    d = json.loads(STATE.read_text())
    b = d.get("blasphemy", {})
    bt = b.get("total", {})
    blasph_total = sum(int(v) for v in bt.values()) if bt else 0
    blasph_top = sorted(bt.items(), key=lambda x: -x[1])[:5]
    per_chan_totals = {
        chan: sum(int(c) for c in nicks.values())
        for chan, nicks in b.get("per_channel", {}).items()
        if isinstance(nicks, dict)
    }
    chan_pairs = sorted(per_chan_totals.items(), key=lambda x: -x[1])

    subj_totals: dict[str, int] = {}
    for subj, nicks in b.get("by_subject", {}).items():
        if isinstance(nicks, dict):
            subj_totals[subj] = sum(int(c) for c in nicks.values())
    subj_top = sorted(subj_totals.items(), key=lambda x: -x[1])[:4]

    epi_totals: dict[str, int] = defaultdict(int)
    for _nick, epis in b.get("epithets", {}).items():
        if isinstance(epis, dict):
            for e, c in epis.items():
                epi_totals[e] += int(c)
    epi_top = sorted(epi_totals.items(), key=lambda x: -x[1])[:6]

    events = d.get("events", [])
    ev_count = len(events)
    concat_forms: dict[str, int] = defaultdict(int)
    roll_variants: dict[str, int] = defaultdict(int)
    dab_variants: dict[str, int] = defaultdict(int)
    roll_nicks: dict[str, int] = defaultdict(int)
    dab_nicks: dict[str, int] = defaultdict(int)
    for e in events:
        kind = e.get("kind")
        if kind == "concat":
            concat_forms[str(e.get("form", ""))] += 1
        elif kind == "cmd":
            variant = str(e.get("variant") or "bare")
            nick = str(e.get("nick", ""))
            if e.get("cmd") == "Roll":
                roll_variants[variant] += 1
                roll_nicks[nick] += 1
            elif e.get("cmd") == "Dab":
                dab_variants[variant] += 1
                dab_nicks[nick] += 1
    concat_top = sorted(concat_forms.items(), key=lambda x: -x[1])[:4]
    roll_var_top = sorted(roll_variants.items(), key=lambda x: -x[1])[:4]
    dab_var_top = sorted(dab_variants.items(), key=lambda x: -x[1])[:4]
    roll_nick_top = sorted(roll_nicks.items(), key=lambda x: -x[1])[:3]
    dab_nick_top = sorted(dab_nicks.items(), key=lambda x: -x[1])[:3]
    roll_tot = sum(roll_variants.values())
    dab_tot = sum(dab_variants.values())

    if events:
        span = int(events[-1]["ts"]) - int(events[0]["ts"])
        span_str = human_span(max(span, 0))
        age_last = int(time.time()) - int(events[-1]["ts"])
        age_str = human_span(max(age_last, 0))
    else:
        span_str = "-"
        age_str = "-"

    if compact:
        return [
            f"[stats] bestemmie={blasph_total} top5 {fmt_top(blasph_top)} "
            f"| ::Roll={roll_tot} ::Dab={dab_tot} "
            f"| eventi={ev_count} finestra={span_str}"
        ]

    return [
        f"[stats] bestemmie={blasph_total} | top5 {fmt_top(blasph_top)}",
        f"[stats] per chan: {fmt_top(chan_pairs)} | finestra={span_str} ultimo={age_str}fa",
        f"[stats] soggetti: {fmt_top(subj_top)} | concat: {fmt_top(concat_top)}",
        f"[stats] epiteti: {fmt_top(epi_top)}",
        f"[stats] ::Roll={roll_tot} var[{fmt_top(roll_var_top)}] nick[{fmt_top(roll_nick_top)}] "
        f"| ::Dab={dab_tot} var[{fmt_top(dab_var_top)}] nick[{fmt_top(dab_nick_top)}]",
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compact", action="store_true",
                    help="single-line summary (old behavior)")
    ap.add_argument("--say", metavar="TARGET",
                    help="write SAY <target> <line> per line to bot.send FIFO")
    args = ap.parse_args()
    lines = build_lines(args.compact)
    if args.say:
        with FIFO.open("w") as f:
            for line in lines:
                f.write(f"SAY {args.say} {line}\n")
    else:
        for line in lines:
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
