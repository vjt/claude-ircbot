#!/usr/bin/env python3
"""next_counter — "what ships after 1.3" poll sidecar for grappa.

Same shape as cena_counter.py: tail bot.log, match a chan command, keep
state, re-render a public json that a static page can poll.

Semantics (vjt's brief, #grappa 2026-08-19 12:52-12:58):

  * `!next <slug>` casts a vote. One nick holds ONE vote and the LAST vote
    replaces the previous one ("ognuno ne vota una"), exactly like !cena.
  * The ballot is a CLOSED list decided by vjt "basandoci sulla roadmap":
    the 13 grappa issues carrying the `roadmap` label, minus four he struck
    off — #311 (Meta AI glasses), #5 (multi-protocol BNC), #261 (graceful
    deploys) and #293 (themes: "ci sono gia"). Nine remain. A slug outside
    the list is not a write-in: it gets the ballot back and changes nothing.
  * Four slugs were renamed by vjt on the spot: `multilingua` not `i18n`,
    `encryption` not `e2ee`, `scripting` not `lua`, `prometheus` not
    `promex`. The spellings he replaced stay as ALIASES, so a voter who
    types the term the issue itself uses is not silently dropped.
  * Bare `!next` asks for the standings and gets ONE line back in channel.
    A cast vote is SILENT — same reasoning as !cena: the page is the
    feedback surface, and an ack per vote spams the channel.

Two files, split by write pattern (same reasoning as cena_counter):

  * PRIVATE state (STATE)  — canonical, repo dir, gitignored.
  * PUBLIC json  (OUT_JSON) — projection, in the Syncthing'd static dir ->
    sindro.me/t/next/next.json, for a page that does not exist yet.

Nick is the only identifier stored. NEVER host/ident/ip.

CLI:
  next_counter.py            run the daemon (tail + append)
  next_counter.py list       print current standings (from public json)
  next_counter.py backfill   (re)build state from bot.log history, no daemon

Env overrides (so a dry run cannot publish or speak):
  NEXT_OUT    path of the public next.json
  NEXT_STATE  path of the private state json
  NEXT_FIFO   path of the bot FIFO ('' disables talking)
"""
import json, os, re, subprocess, sys, time
from pathlib import Path

REPO = Path("/home/vjt/code/IRC/vjt-claude")
LOG = str(REPO / "bot.log")

STATE = Path(os.environ.get("NEXT_STATE", str(REPO / "next_state.json")))
OUT_JSON = Path(os.environ.get("NEXT_OUT", "/srv/www-static/t/next/next.json"))
# '' disables the channel reply entirely (dry runs must stay mute).
FIFO = os.environ.get("NEXT_FIFO", str(REPO / "bot.send"))

POLL_CHANS = {"#grappa", "#sniffo"}   # #sniffo added on vjt's order, 13:32

TITLE = "grappa — cosa facciamo dopo la 1.3"
REPO_URL = "https://github.com/vjt/grappa-irc/issues/"

# The closed ballot. slug -> (issue number, short label shown on the page).
# Titles are abridged from the issues themselves, not invented.
OPTIONS = {
    "search":      (383,  "Ricerca in finestra + storico di rete"),
    "multilingua": (362,  "cicchetto multilingua (it/fr/es/de)"),
    "encryption":  (65,   "Cifratura end-to-end dei messaggi privati"),
    "radio":       (682,  "Player radio internet nel mini-player"),
    # Scope: grappa SPEAKING IRCv3 to clients that connect to it, not
    # demanding IRCv3 from upstream networks -- morph raised that ambiguity on
    # #grappa. The slug carried the disambiguation until !wat existed to spell
    # it out; short slug now, the scope lives in the label (vjt, 13:29).
    "ircv3": (102, "Listener IRCv3 downstream (grappa PARLA IRCv3 ai client)"),
    "export":      (1104, "Export dello scrollback (testo + JSON)"),
    "scripting":   (288,  "Scripting Lua via Luerl, sandboxed"),
    "voice":       (106,  "Voce: TTS + STT on-device"),
    "prometheus":  (99,   "Telemetria -> esportatore Prometheus"),
    # Added on vjt's order (18:22, #grappa) after he said he intends to build
    # it: WEBIRC is not an alternative to the derived source address, the two
    # compose -- WEBIRC where the network grants a block and a secret, the
    # derivation where it does not.
    "webirc":      (1164, "WEBIRC: host reale per-utente verso la rete a monte"),
}

# Not write-ins: every alias resolves INTO the closed list above. These exist
# because the issues use the technical spelling vjt renamed away from, and a
# vote lost to vocabulary is a vote lost for nothing.
ALIASES = {
    "i18n": "multilingua", "multilingual": "multilingua", "l10n": "multilingua",
    "e2ee": "encryption", "e2e": "encryption", "omemo": "encryption",
    "cifratura": "encryption", "crittografia": "encryption",
    "ircv3-listener": "ircv3", "listener": "ircv3",
    "ricerca": "search", "lua": "scripting", "luerl": "scripting",
    "promex": "prometheus", "telemetria": "prometheus",
    "telemetry": "prometheus", "metriche": "prometheus",
    "scrollback": "export", "tts": "voice", "stt": "voice", "voce": "voice",
    "wline": "webirc", "w-line": "webirc", "web-irc": "webirc",
}

# Line must START with the command so meta-chatter mentioning it can't vote.
# A space after the bang is tolerated: phone keyboards autocorrect "!next" to
# "! Next", same fix cena_counter needed.
NEXT_PAT = re.compile(r'^!\s*next(?:\s+(?P<args>.*\S))?\s*$', re.IGNORECASE)
# `!nextsearch` — the command run into its argument. Dying silent here reads
# as "the bot ate my vote", so answer with the syntax instead.
TYPO_PAT = re.compile(r'^!\s*next\S', re.IGNORECASE)
# `!wat <opzione>` — read-only lookup, never touches the tally. Someone asking
# what an option IS is not casting a vote for it (vjt, 13:25).
WAT_PAT = re.compile(r'^!\s*wat(?:\s+(?P<args>.*\S))?\s*$', re.IGNORECASE)
WAT_TYPO_PAT = re.compile(r'^!\s*wat\S', re.IGNORECASE)

SLUG_MAX = 24

PRIVMSG_PAT = re.compile(
    r'< :(?P<nick>[^!@\s]+)!(?P<ident>[^@\s]+)@(?P<host>\S+)\s+'
    r'PRIVMSG\s+(?P<chan>#\S+)\s+:(?P<text>.*?)$',
    re.MULTILINE,
)

NICK_ALIASES = {
    "vjt`afk": "vjt", "vjt`zzz": "vjt", "vjt42": "vjt", "vjt_": "vjt",
}


def canon_nick(n):
    return NICK_ALIASES.get(n.casefold(), n)


def resolve(slug):
    """Slug (or alias) -> canonical slug, or None if off the ballot."""
    s = re.sub(r'[\x00-\x1f]', '', slug).strip().casefold()[:SLUG_MAX]
    s = ALIASES.get(s, s)
    return s if s in OPTIONS else None


def empty():
    return {"votes": {}}


def load():
    if STATE.exists():
        try:
            d = json.loads(STATE.read_text())
            votes = d.get("votes", {})
            if isinstance(votes, dict):
                # A slug renamed on the ballot must not silently void the
                # votes cast under the old spelling: resolve() knows the old
                # name as an alias, so migrate them on the way in.
                for v in votes.values():
                    s = resolve(v.get("slug", ""))
                    if s:
                        v["slug"] = s
                return {"votes": votes}
        except Exception:
            pass
    return empty()


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def tally(votes):
    """Standings, most-voted first, ties broken by slug.

    Every ballot option is present even at zero, so the page shows the whole
    slate instead of hiding what nobody picked yet — a zero row is a fact.

    Counting heads: `votes` is keyed by canonical nick, so a nick that voted
    five times contributes once by construction.
    """
    rows = {s: {"slug": s, "issue": n, "label": lbl, "count": 0, "nicks": []}
            for s, (n, lbl) in OPTIONS.items()}
    for v in votes.values():
        e = rows.get(v["slug"])
        if e is None:      # slug retired from the ballot after the vote landed
            continue
        e["count"] += 1
        e["nicks"].append(v["nick"])
    return sorted(rows.values(), key=lambda e: (-e["count"], e["slug"]))


def save(data):
    _atomic_write(STATE, json.dumps(data, indent=2, ensure_ascii=False))
    votes = data["votes"]
    public = sorted(
        ({"nick": v["nick"], "slug": v["slug"], "ts": v["ts"]}
         for v in votes.values()),
        key=lambda v: v["ts"],
    )
    doc = {
        "title": TITLE,
        "issues_url": REPO_URL,
        "count": len(votes),
        "options": tally(votes),
        "votes": public,
    }
    _atomic_write(OUT_JSON, json.dumps(doc, indent=2, ensure_ascii=False))


def ballot_line():
    return "Opzioni: " + " ".join(sorted(OPTIONS))


def standings_line(votes):
    """One line: who is winning, and the whole rest of the slate.

    The unvoted options are always named. A standings line that only lists
    what has votes hides the ballot from whoever arrives late, and an option
    nobody can see is an option nobody can pick.
    """
    if not votes:
        return (f"Nessun voto ancora. `!next <opzione>` per aprire, "
                f"`!wat <opzione>` per la issue. {ballot_line()}")
    rows = tally(votes)
    voted = [e for e in rows if e["count"]]
    zero = [e["slug"] for e in rows if not e["count"]]
    body = ", ".join(f"{e['slug']} {e['count']}" for e in voted)
    n = len(votes)
    line = f"🗳 {n} {'voto' if n == 1 else 'voti'} — {body}"
    if zero:
        line += f" · a zero: {', '.join(zero)}"
    return line + " · `!wat <opzione>` per la issue"


def issue_line(slug):
    """`!wat voice` -> what it is and where to read the whole thing."""
    num, label = OPTIONS[slug]
    return f"{slug} — {label} — {REPO_URL}{num}"


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
    text = text.strip()

    # `!wat` is read-only: it answers and returns False, so no path through it
    # can ever write a vote.
    wm = WAT_PAT.match(text)
    if wm:
        if talk:
            wargs = (wm.group("args") or "").strip()
            wslug = resolve(wargs.split()[0]) if wargs else None
            if wslug:
                say(chan, issue_line(wslug))
            else:
                say(chan, f"`!wat <opzione>` per la issue. {ballot_line()}")
        return False

    nm = NEXT_PAT.match(text)
    if not nm:
        if talk and TYPO_PAT.match(text):
            say(chan, f"Ci vuole lo spazio: `!next <opzione>`. {ballot_line()}")
        elif talk and WAT_TYPO_PAT.match(text):
            say(chan, f"Ci vuole lo spazio: `!wat <opzione>`. {ballot_line()}")
        return False

    args = (nm.group("args") or "").strip()
    if not args:
        if talk:
            say(chan, standings_line(data["votes"]))
        return False

    # One slug per vote: extra words are the voter explaining themselves, not
    # a second choice. "ognuno ne vota una" — take the first token only.
    slug = resolve(args.split()[0])
    if slug is None:
        if talk:
            say(chan, f"Non e' in scheda. {ballot_line()}")
        return False

    head = canon_nick(nick)
    ts_i = int(ts if ts is not None else time.time())
    prev = data["votes"].get(head.casefold())
    # Last vote wins, and an identical re-vote is not a change: skipping it
    # avoids rewriting ts and re-rendering for nothing.
    if prev and prev["slug"] == slug:
        return False
    data["votes"][head.casefold()] = {"nick": head, "slug": slug, "ts": ts_i}
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
    print(f"🗳 {doc.get('title')} — {doc.get('count', 0)} voti")
    for e in doc.get("options", []):
        nicks = f" ({', '.join(e['nicks'])})" if e["nicks"] else ""
        print(f"  {e['slug']:<12} #{e['issue']:<5} {e['count']}{nicks}")


def daemon():
    data = load()
    if not data["votes"]:
        backfill(data)
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
