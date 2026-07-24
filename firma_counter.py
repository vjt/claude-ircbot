#!/usr/bin/env python3
"""firma_counter — "Ridateci tsk su Azzurra" petition signature sidecar.

Mirrors roll_counter.py's shape: tail bot.log, match a chan command, keep
state. Here the command is `!firma [comment]` on the petition channels.

Semantics (vjt's brief, 2026-07-19):

  * NO dedup, NO host. Every `!firma` is appended as a NEW row — the same
    nick may sign as many times as it likes, adding as many comments; the
    old ones are never dropped. Host is not part of any key and is not
    stored anywhere (shared vhosts collided distinct people and cloak
    changes let a nick re-sign, so host made dedup both wrong and looser;
    and we don't keep IRC hosts we don't need).
  * The COUNTER counts heads, not signings: `count` = number of distinct
    (canonicalised) nicks. A nick spamming 20 `!firma` still moves it by 1.

Two files, kept split by the write pattern:

  * PRIVATE state (STATE)  — the append target, canonical, in the repo dir
    (gitignored), OUTSIDE the Syncthing'd static dir.
  * PUBLIC json  (OUT_JSON) — projection {nick, ts, comment} + head count,
    re-rendered from STATE on every append. Lives in the synced static dir
    -> sindro.me/t/sk/tsk.json; this is what tsk's page fetches.

CLI:
  firma_counter.py            run the daemon (tail + append)
  firma_counter.py list       print current signatures (from public json)
  firma_counter.py backfill   (re)build state from bot.log history, no daemon

Env overrides (used for staging/demo so a dry run can't publish):
  FIRMA_OUT    path of the public tsk.json   (default: synced static dir)
  FIRMA_STATE  path of the private state json (default: repo dir)
"""
import json, os, re, subprocess, sys, time
from pathlib import Path

REPO = Path("/home/vjt/code/IRC/vjt-claude")
LOG = str(REPO / "bot.log")

# Private, gitignored, OUTSIDE the synced static dir. Canonical append target.
STATE = Path(os.environ.get("FIRMA_STATE", str(REPO / "firma_state.json")))
# Public, lives in the Syncthing'd static dir -> sindro.me/t/sk/tsk.json.
# nick+ts+comment + head count only. NEVER host/ip.
OUT_JSON = Path(os.environ.get("FIRMA_OUT", "/srv/www-static/t/sk/tsk.json"))

# Channels that count as the petition floor. Started on #sniffo, moved to
# #sbiffo ("repla di là"), then vjt enabled #it-opers too.
PETITION_CHANS = {"#sniffo", "#sbiffo", "#it-opers"}

# Command: line must START with `!firma`, optional trailing comment. Starting
# anchor keeps meta-chatter that merely mentions "!firma" from signing anyone.
FIRMA_PAT = re.compile(r'^!firma(?:\s+(?P<comment>.*\S))?\s*$', re.IGNORECASE)
COMMENT_MAX = 200

# Inbound-only (`< :`): captures nick!ident@host. We match the whole line
# shape (host included) but never store the host. Outbound `> ` never matches.
PRIVMSG_PAT = re.compile(
    r'< :(?P<nick>[^!@\s]+)!(?P<ident>[^@\s]+)@(?P<host>\S+)\s+'
    r'PRIVMSG\s+(?P<chan>#\S+)\s+:(?P<text>.*?)$',
    re.MULTILINE,
)

# Telegram bridges relay as "<tgnick> message" from a shared bridge nick.
# Unwrap so the human signs, not the relay.
BRIDGE_NICKS = {"Trillian", "Gazzurbo"}
BRIDGE_PREFIX_PAT = re.compile(r'^<([^>\s]+)>\s?(.*)$', re.DOTALL)

# Gazzurbo (porto.telegram.su.sniffo.org) is authoritative for the telegram
# bridge. On #sniffo BOTH Gazzurbo and Trillian relay the SAME tg<->#sniffo
# group (verified: vjt42/StefySpora/alk_tg/fpietrosanti/... appear via both,
# identically formatted), so one telegram !firma arrives TWICE and would make
# two rows. Suppress the mirror: an identical bridged signing (same head +
# same comment) within MIRROR_WINDOW seconds is the other bridge's copy of the
# same message, not a second signing. NB this is NOT a blanket "drop Trillian":
# on #it-opers Trillian is the ONLY bridge (its own tg group, no Gazzurbo), so
# nothing there mirrors and nothing is dropped. vjt 2026-07-20.
AUTHORITATIVE_BRIDGE = "Gazzurbo"
MIRROR_WINDOW = 90  # seconds

# Fold vjt's AFK/secondary nicks so his signings collapse to one head.
NICK_ALIASES = {
    "vjt`afk": "vjt", "vjt`zzz": "vjt", "vjt42": "vjt", "vjt_": "vjt",
}


def canon_nick(n):
    return NICK_ALIASES.get(n.casefold(), n)


def empty():
    return {"signatures": []}


def load():
    """Load state, migrating legacy records (drop stored host + old 'seen')."""
    if STATE.exists():
        try:
            d = json.loads(STATE.read_text())
            sigs = d.get("signatures", [])
            for s in sigs:
                s.pop("host", None)  # host is no longer stored
            return {"signatures": sigs}  # legacy 'seen' intentionally dropped
        except Exception:
            pass
    return empty()


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def save(data):
    """Persist private state, then re-render the public json (host-free)."""
    _atomic_write(STATE, json.dumps(data, indent=2, ensure_ascii=False))
    sigs = sorted(data["signatures"], key=lambda s: s["ts"])
    public = [
        {"nick": s["nick"], "ts": s["ts"], "comment": s.get("comment", "")}
        for s in sigs
    ]
    # Counter = distinct heads, not signings.
    count = len({s["nick"].casefold() for s in sigs})
    doc = {"title": "Ridateci tsk su Azzurra", "count": count,
           "signatures": public}
    _atomic_write(OUT_JSON, json.dumps(doc, indent=2, ensure_ascii=False))


def process(line, data, ts=None):
    m = PRIVMSG_PAT.search(line)
    if not m:
        return False
    nick, chan, text = m.group("nick"), m.group("chan"), m.group("text")
    if chan not in PETITION_CHANS:
        return False
    bridged = False
    if nick in BRIDGE_NICKS:
        bm = BRIDGE_PREFIX_PAT.match(text)
        if bm:
            nick, text = bm.group(1), bm.group(2)
            bridged = True
    fm = FIRMA_PAT.match(text.strip())
    if not fm:
        return False
    comment = (fm.group("comment") or "").strip()
    comment = re.sub(r'[\x00-\x1f]', '', comment)[:COMMENT_MAX]
    head = canon_nick(nick)
    ts_i = int(ts if ts is not None else time.time())
    # Bridged signings can arrive twice (both bridges mirror the same #sniffo
    # tg group). Drop the mirror: same head + same comment within MIRROR_WINDOW
    # is the other bridge's copy, not a second signing. Direct IRC !firma is
    # never deduped — a nick may sign repeatedly by design.
    if bridged:
        for s in data["signatures"]:
            if (s["nick"].casefold() == head.casefold()
                    and s.get("comment", "") == comment
                    and abs(ts_i - int(s["ts"])) <= MIRROR_WINDOW):
                return False
    data["signatures"].append({
        "nick": head,
        "ts": ts_i,
        "comment": comment,
    })
    return True


def backfill(data):
    """Seed from bot.log history so signatures already given are carried in."""
    try:
        with open(LOG, errors="replace") as f:
            for raw in f:
                process(raw, data, ts=None)
    except FileNotFoundError:
        pass
    save(data)


def list_cmd():
    if not OUT_JSON.exists():
        print("(no signatures yet)")
        return
    doc = json.loads(OUT_JSON.read_text())
    print(f"📜 {doc.get('title')} — {doc.get('count', 0)} firme:")
    for s in doc.get("signatures", []):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["ts"]))
        c = f" — {s['comment']}" if s.get("comment") else ""
        print(f"  {s['nick']} ({when}){c}")


def daemon():
    data = load()
    if not data["signatures"]:
        backfill(data)
    p = subprocess.Popen(["tail", "-F", "-n", "0", LOG],
                         stdout=subprocess.PIPE, text=True, errors="replace")
    for line in p.stdout:
        if process(line, data, ts=time.time()):
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
