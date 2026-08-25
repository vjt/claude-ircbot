#!/usr/bin/env python3
"""cena_counter — crew dinner poll sidecar ("cenastat").

Same shape as firma_counter.py: tail bot.log, match a chan command, keep
state, re-render a public json that a static page polls.

Semantics (vjt's brief, #sniffo 2026-08-05 22:23):

  * `!cena <citta> [data][, data...]` casts a vote. A nick MAY change its
    mind: the LAST vote replaces the previous one entirely. This is the one
    difference from the petition, where every `!firma` accumulates.
  * Multiple dates in one vote are allowed (alk, same evening: "il coso per
    selezionare la data a scelte multiple"). They are alternatives the voter
    is OK with, so the whole set is replaced by the next vote — "last wins"
    still holds, it just wins over a set rather than a scalar.
  * Dates are split on COMMAS, never on whitespace. The very first real vote
    was `!cena roma 11 settembre`, and splitting on spaces turned one date
    into two bogus ones ("11" and "settembre"). People write dates the way
    they speak them, so a date is free text and only a comma starts a new
    alternative. The city stays the first whitespace-delimited token —
    multi-word cities go hyphenated.
  * `!pranzo` is the same poll with the meal as a third voted dimension
    (vjt, 22:31 "vai anche con !pranzo"; alk's argument was that a lunch lets
    people go home the same evening instead of hunting for a bed). One nick
    still holds ONE vote — city, dates and meal travel together and are all
    replaced at once.
  * Bare `!cena`/`!pranzo` asks for the standings and gets ONE line back in
    channel. A vote itself is silent: the page is the feedback surface, and
    an ack per vote would spam a busy channel.
  * `!sicisono [+N]` is a SECOND, independent dimension (vjt, #sbiffo
    2026-08-25 14:45): the poll asks where people would like to go, this
    asks who is actually coming. The two do not gate each other — you can
    confirm without ever having voted, and voting is not a confirmation.
    `N` counts the people you bring along (vjt: "+ 2 bambini + 1 neonato"),
    so one nick contributes 1 + N heads. `!nonvengo` retracts. Unlike a
    vote, a confirmation DOES get an ack: it is a commitment someone else
    books a table on, and silence there reads as "did it take?".
  * `!confermati` prints the confirmed list and the head count.

Two files, split by write pattern (same reasoning as firma_counter):

  * PRIVATE state (STATE)  — canonical, repo dir, gitignored.
  * PUBLIC json  (OUT_JSON) — projection, in the Syncthing'd static dir ->
    sindro.me/t/cena/cena.json, which the page fetches.

Nick is the only identifier stored. NEVER host/ident/ip.

CLI:
  cena_counter.py            run the daemon (tail + append)
  cena_counter.py list       print current standings (from public json)
  cena_counter.py backfill   (re)build state from bot.log history, no daemon

Env overrides (so a dry run cannot publish or speak):
  CENA_OUT    path of the public cena.json
  CENA_STATE  path of the private state json
  CENA_FIFO   path of the bot FIFO ('' disables talking)
"""
import json, os, re, subprocess, sys, time
from pathlib import Path

REPO = Path("/home/vjt/code/IRC/vjt-claude")
LOG = str(REPO / "bot.log")

STATE = Path(os.environ.get("CENA_STATE", str(REPO / "cena_state.json")))
OUT_JSON = Path(os.environ.get("CENA_OUT", "/srv/www-static/t/cena/cena.json"))
# '' disables the channel reply entirely (dry runs must stay mute).
FIFO = os.environ.get("CENA_FIFO", str(REPO / "bot.send"))

POLL_CHANS = {"#sniffo", "#sbiffo"}

# Line must START with the command so meta-chatter mentioning it can't vote.
# A space after the bang is tolerated: phone keyboards autocorrect "!cena" to
# "! Cena", and a vote lost to autocapitalisation is a vote lost for nothing.
CENA_PAT = re.compile(r'^!\s*(?P<meal>cena|pranzo)(?:\s+(?P<args>.*\S))?\s*$',
                      re.IGNORECASE)
# `!cenabologna13` — the command run into its argument. It used to die silent,
# which reads as "the bot ate my vote". Answer with the syntax instead of
# guessing where the city starts: bologna13 could be a city or a date.
TYPO_PAT = re.compile(r'^!\s*(?:cena|pranzo)\S', re.IGNORECASE)

CONFIRM_PAT = re.compile(r'^!\s*sicisono(?:\s+\+?(?P<extra>\d{1,2}))?\s*$',
                         re.IGNORECASE)
DECLINE_PAT = re.compile(r'^!\s*nonvengo\s*$', re.IGNORECASE)
CONFLIST_PAT = re.compile(r'^!\s*confermati\s*$', re.IGNORECASE)
# Same trap as TYPO_PAT, for the confirmation verbs.
CONFTYPO_PAT = re.compile(r'^!\s*(?:sicisono|nonvengo|confermati)\S',
                          re.IGNORECASE)
# `!sicisono -2`, `!sicisono +100`, `!sicisono due`: spaced correctly, but the
# argument is not a head count, so CONFIRM_PAT drops it and the line dies mute
# — which reads as "the bot ate my confirmation" (peluche found it by fuzzing,
# #sniffo 2026-08-25). It needs its OWN message: the `!cenabologna` advice
# ("add spaces") is wrong here, the spaces are already there. Valid forms are
# consumed earlier, so anything reaching this pattern is malformed by
# construction.
CONFARG_PAT = re.compile(r'^!\s*(?:sicisono|nonvengo|confermati)\s+\S',
                         re.IGNORECASE)

CITY_MAX = 24        # a city name, not an essay
DATE_MAX = 32        # free text: "sabato 13 settembre" must fit whole
MAX_DATES = 6        # Doodle-style alternatives, bounded so one vote can't flood
MAX_EXTRA = 20       # you are bringing family, not a coach party
TITLE = "Cena della crew"

# The decided event. Poll and confirmations stay data; this is the ruling that
# closed the poll (vjt, #sbiffo 2026-08-25 14:46: "la data confermata e' il 12
# a pranzo"). It lives here so page and channel quote the SAME source, and the
# venue lands the moment alk names it.
EVENT = {
    "date": "sabato 12 settembre",
    "meal": "pranzo",
    "city": "Bologna",
    "location": "",          # alk is booking it; empty renders as "in attesa"
}

# The page that renders OUT_JSON. Every reply carries it: the channel line is a
# summary, the page is the whole thing (per-nick list, dates, venue).
PAGE = "https://sindro.me/t/cena/"


def event_confirmed():
    """True once EVENT names a date — that is what closed the poll. From then
    on the standings are history and `!cena` owes people the guest list."""
    return bool(EVENT.get("date"))

PRIVMSG_PAT = re.compile(
    r'< :(?P<nick>[^!@\s]+)!(?P<ident>[^@\s]+)@(?P<host>\S+)\s+'
    r'PRIVMSG\s+(?P<chan>#\S+)\s+:(?P<text>.*?)$',
    re.MULTILINE,
)

# Telegram bridges relay as "<tgnick> message" from a shared bridge nick.
BRIDGE_NICKS = {"Trillian", "Gazzurbo"}
BRIDGE_PREFIX_PAT = re.compile(r'^<([^>\s]+)>\s?(.*)$', re.DOTALL)

NICK_ALIASES = {
    "vjt`afk": "vjt", "vjt`zzz": "vjt", "vjt42": "vjt", "vjt_": "vjt",
}


def canon_nick(n):
    return NICK_ALIASES.get(n.casefold(), n)


def empty():
    return {"votes": {}, "confirmed": {}}


def load():
    if STATE.exists():
        try:
            d = json.loads(STATE.read_text())
            votes = d.get("votes", {})
            conf = d.get("confirmed", {})
            if isinstance(votes, dict):
                return {"votes": votes,
                        "confirmed": conf if isinstance(conf, dict) else {}}
        except Exception:
            pass
    return empty()


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _clean(s, limit):
    return re.sub(r'[\x00-\x1f]', '', s).strip()[:limit]


def tally(votes):
    """City standings, most-voted first, ties broken by city name.

    Counting heads: `votes` is already keyed by canonical nick, so a nick
    that voted five times contributes once by construction — there is no
    separate dedup step that could drift from the render.
    """
    by_city = {}
    for v in votes.values():
        key = v["city"].casefold()
        # Display the first-seen spelling with a leading capital, so "bologna"
        # and "Bologna" are one row and the row doesn't read as a typo.
        label = v["city"][:1].upper() + v["city"][1:]
        e = by_city.setdefault(key, {"city": label, "count": 0, "nicks": []})
        e["count"] += 1
        e["nicks"].append(v["nick"])
    return sorted(by_city.values(), key=lambda e: (-e["count"], e["city"].casefold()))


def meal_tally(votes):
    """Lunch vs dinner. Two fixed rows, always both present even at zero, so
    the page shows the trade-off instead of hiding the losing side."""
    counts = {"pranzo": 0, "cena": 0}
    for v in votes.values():
        counts[v.get("meal", "cena")] += 1
    return [{"meal": m, "count": c} for m, c in
            sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def date_tally(votes):
    """Date standings across ALL cities — a date is an alternative, not a city
    attribute, and the crew picks the day people can actually make."""
    by_date = {}
    for v in votes.values():
        for d in v.get("dates", []):
            e = by_date.setdefault(d.casefold(), {"date": d, "count": 0})
            e["count"] += 1
    return sorted(by_date.values(), key=lambda e: (-e["count"], e["date"]))


def confirmed_list(confirmed):
    """Confirmations oldest-first — this is a guest list, and the order people
    committed in is the only order that means anything here."""
    out = []
    for c in confirmed.values():
        extra = int(c.get("extra", 0))
        out.append({"nick": c["nick"], "extra": extra, "heads": 1 + extra,
                    "ts": c["ts"]})
    return sorted(out, key=lambda c: c["ts"])


def heads(confirmed):
    return sum(1 + int(c.get("extra", 0)) for c in confirmed.values())


def confirmed_line(confirmed):
    ev = f"{EVENT['date']} a {EVENT['meal']}, {EVENT['city']}"
    if not confirmed:
        return (f"Nessun confermato per {ev}. `!sicisono` (o `!sicisono +2` "
                f"se porti gente) per metterti nella lista. {PAGE}")
    who = ", ".join(c["nick"] + (f" +{c['extra']}" if c["extra"] else "")
                    for c in confirmed_list(confirmed))
    n, h = len(confirmed), heads(confirmed)
    loc = EVENT["location"] or "location da confermare"
    return f"✅ {h} a tavola ({n} nick) — {who} | {ev} — {loc} | {PAGE}"


def save(data):
    _atomic_write(STATE, json.dumps(data, indent=2, ensure_ascii=False))
    votes = data["votes"]
    confirmed = data.get("confirmed", {})
    public = sorted(
        ({"nick": v["nick"], "city": v["city"], "dates": v.get("dates", []),
          "meal": v.get("meal", "cena"), "ts": v["ts"]} for v in votes.values()),
        key=lambda v: v["ts"],
    )
    doc = {
        "title": TITLE,
        "event": EVENT,
        "count": len(votes),
        "cities": tally(votes),
        "dates": date_tally(votes),
        "meals": meal_tally(votes),
        "votes": public,
        "heads": heads(confirmed),
        "confirmed": confirmed_list(confirmed),
    }
    _atomic_write(OUT_JSON, json.dumps(doc, indent=2, ensure_ascii=False))


def standings_line(votes, confirmed=None):
    if not votes:
        return ("Nessun voto. `!cena <citta> [data, data]` "
                f"(o `!pranzo`) per aprire le danze. {PAGE}")
    cities = ", ".join(f"{e['city']} {e['count']}" for e in tally(votes)[:5])
    dates = date_tally(votes)[:3]
    meals = ", ".join(f"{m['meal']} {m['count']}" for m in meal_tally(votes)
                      if m["count"])
    out = f"🍕 {len(votes)} voti — {cities}"
    if dates:
        out += " | date: " + ", ".join(f"{d['date']} {d['count']}" for d in dates)
    if meals:
        out += " | " + meals
    # The poll is history now that the date is decided: say so on the same line
    # people already type, or they keep reading the standings as undecided.
    if confirmed:
        out += f" | confermati {heads(confirmed)} a tavola, `!confermati`"
    return out + f" | {PAGE}"


def say(chan, text):
    """One line to the bot FIFO. Non-blocking open: with no reader we get
    ENXIO and skip, instead of hanging the daemon forever on a dead bot."""
    if not FIFO:
        return
    try:
        fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return
    try:
        os.write(fd, f"SAY {chan} {text}\n".encode())
    except OSError:
        pass
    finally:
        os.close(fd)


def process(line, data, ts=None, talk=False):
    """Returns True when state changed (caller saves). Channel replies are
    only emitted with talk=True, so backfill over history stays mute."""
    m = PRIVMSG_PAT.search(line)
    if not m:
        return False
    nick, chan, text = m.group("nick"), m.group("chan"), m.group("text")
    if chan not in POLL_CHANS:
        return False
    if nick in BRIDGE_NICKS:
        bm = BRIDGE_PREFIX_PAT.match(text)
        if bm:
            nick, text = bm.group(1), bm.group(2)
    text = text.strip()
    head = canon_nick(nick)
    conf = data.setdefault("confirmed", {})
    key = head.casefold()
    ts_i = int(ts if ts is not None else time.time())

    # Confirmations first: they share the channel gate and the nick canon with
    # the poll, but nothing else — a confirmation is not a vote.
    if CONFLIST_PAT.match(text):
        if talk:
            say(chan, confirmed_line(conf))
        return False
    if DECLINE_PAT.match(text):
        if conf.pop(key, None) is None:
            return False
        if talk:
            say(chan, f"{head} sfilato dalla lista. Ora si e' in "
                      f"{heads(conf)}.")
        return True
    cf = CONFIRM_PAT.match(text)
    if cf:
        extra = min(int(cf.group("extra") or 0), MAX_EXTRA)
        prev = conf.get(key)
        if prev and int(prev.get("extra", 0)) == extra:
            return False    # same commitment twice (both TG bridges relay it)
        conf[key] = {"nick": head, "extra": extra, "ts": ts_i}
        if talk:
            mine = 1 + extra
            say(chan, f"{head} confermato per {mine}. Siamo in "
                      f"{heads(conf)} il {EVENT['date']} a {EVENT['meal']}.")
        return True

    cm = CENA_PAT.match(text)
    if not cm:
        if talk and (TYPO_PAT.match(text) or CONFTYPO_PAT.match(text)):
            say(chan, "Ci vogliono gli spazi: `!cena <citta> <data>` "
                      "(date separate dalla virgola), `!sicisono +2`.")
        elif talk and CONFARG_PAT.match(text):
            say(chan, f"Dopo `!sicisono` ci va quanta gente porti, da 0 a "
                      f"{MAX_EXTRA}: `!sicisono +2`. Da solo vali 1. "
                      f"`!nonvengo` e `!confermati` non prendono argomenti.")
        return False

    meal = cm.group("meal").lower()
    args = (cm.group("args") or "").strip()
    if not args:
        # Once the date is decided the standings are trivia: bare `!cena` is
        # people asking "who is coming", so give them the same answer
        # `!confermati` gives (vjt, #sniffo 2026-08-25 21:40).
        if talk:
            say(chan, confirmed_line(conf) if event_confirmed()
                else standings_line(data["votes"], conf))
        return False

    city, _, rest = args.partition(" ")
    city = _clean(city, CITY_MAX)
    if not city:
        return False
    # Comma-separated alternatives; a single date keeps its spaces.
    dates = [d for d in (_clean(p, DATE_MAX) for p in rest.split(","))
             if d][:MAX_DATES]

    prev = data["votes"].get(key)
    # Last vote wins, and an identical re-vote is not a change: skipping it
    # keeps the mirrored telegram copy (both bridges relay the same group)
    # from rewriting ts and re-rendering for nothing.
    if prev and prev["city"].casefold() == city.casefold() \
            and prev.get("meal", "cena") == meal \
            and [d.casefold() for d in prev.get("dates", [])] == [d.casefold() for d in dates]:
        return False
    data["votes"][key] = {
        "nick": head, "city": city, "dates": dates, "meal": meal, "ts": ts_i,
    }
    return True


def backfill(data):
    try:
        with open(LOG, errors="replace") as f:
            for raw in f:
                process(raw, data, ts=None)
    except FileNotFoundError:
        pass
    save(data)


def list_cmd():
    if not OUT_JSON.exists():
        print("(no votes yet)")
        return
    doc = json.loads(OUT_JSON.read_text())
    print(f"🍕 {doc.get('title')} — {doc.get('count', 0)} voti")
    for e in doc.get("cities", []):
        print(f"  {e['city']}: {e['count']} ({', '.join(e['nicks'])})")
    if doc.get("dates"):
        print("  date:", ", ".join(f"{d['date']} x{d['count']}" for d in doc["dates"]))
    if doc.get("meals"):
        print("  pasto:", ", ".join(f"{m['meal']} x{m['count']}" for m in doc["meals"]))
    ev = doc.get("event") or {}
    if ev:
        print(f"  evento: {ev.get('date')} a {ev.get('meal')}, {ev.get('city')}"
              f" — {ev.get('location') or 'location da confermare'}")
    conf = doc.get("confirmed") or []
    if conf:
        print(f"  confermati: {doc.get('heads', 0)} a tavola —",
              ", ".join(c["nick"] + (f" +{c['extra']}" if c["extra"] else "")
                        for c in conf))


def daemon():
    data = load()
    if not data["votes"]:
        backfill(data)
    # Publish once on boot: the doc carries EVENT, and that changes here in the
    # source, not in the channel. Without this the page keeps serving the last
    # vote's snapshot until someone happens to type a command.
    save(data)
    p = subprocess.Popen(["tail", "-F", "-n", "0", LOG],
                         stdout=subprocess.PIPE, text=True, errors="replace")
    for line in p.stdout:
        if process(line, data, ts=time.time(), talk=True):
            save(data)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg == "list":
        list_cmd()
    elif arg == "backfill":
        backfill(load())
    else:
        daemon()


if __name__ == "__main__":
    main()
