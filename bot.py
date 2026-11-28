#!/usr/bin/env python3
"""
Minimal IRC bot bridging a claude-code conversation <-> an IRC network.

- TLS connect to $IRC_HOST:$IRC_PORT (6697 by default) as $IRC_NICK.
- Every line logged to ./bot.log (next to this script).
- Selected events emitted to stdout (one line each) so a Monitor sees them.
- Reads commands from FIFO ./bot.send:
    SAY <target> <text>     -> splits into safe PRIVMSGs
    ACT <target> <text>     -> CTCP ACTION
    NOTICE <target> <text>
    RAW <irc line>          -> send as-is
    JOIN <chan> / PART <chan> / WHOIS <nick> / QUIT [reason]
    DCC <nick> <file> [name] -> offer $BOT_DCC_DIR/<file> over DCC SEND (v4 if
                               BOT_DCC_BIND4 is set, else IPv6), announcing
                               [name] to the peer if given (else <file>)
    ESAY <nick> <text>      -> end-to-end encrypted query message to a nick
    EACT <nick> <text>         whose key is pinned (needs BOT_E2E_KEY, see
                               e2e_bridge.py; never to a channel). SAY/ACT to
                               a nick whose last DM was encrypted encrypt too.
                               In ESAY/EACT text `\\n` is a line break and
                               `\\\\` a backslash.

  Any verb may carry an origin tag as `VERB:<tag>` (e.g. `SAY:mysidecar`).
  The tag changes NOTHING on the wire — it is only stamped into bot.log,
  right after the direction marker, as `HH:MM:SS > [mysidecar] PRIVMSG ...`.
  It lets a log reader tell the agent's lines from those of the sidecars,
  plain deterministic programs that share this FIFO. Untagged lines keep
  the plain format, so readers that ignore tags are unaffected.

  The tag rides ON the line and never on a separate marker line: bot.py is
  threaded, and two concurrent sends could pair a marker with the wrong line.
"""
import base64
import fnmatch
import os
import random
import re
import socket
import ssl
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from leak_guard import LeakGuard

# All configuration comes from the environment, so one checkout runs any number
# of instances (one process per network), each with its own nick / FIFO / logs
# / trust / startup. IRC_HOST and IRC_NICK have no default: they are required,
# and checked in check_config() at startup rather than here, because the tests
# import this module. The file paths default to files next to this script.
HERE = os.path.dirname(os.path.abspath(__file__))


def _cfg(key, default):
    return os.environ.get(key, default)


HOST = _cfg("IRC_HOST", "")
PORT = int(_cfg("IRC_PORT", "6697"))
NICK = _cfg("IRC_NICK", "")
IDENT = _cfg("IRC_IDENT", "claude")
REAL = _cfg("IRC_REAL", "github.com/vjt/claude-ircbot")

# Source address to bind before connecting. Empty = let the kernel pick. It
# exists because two bots can share a host and still need two different vhosts:
# where a network's vhost is the rDNS of the address we come from, the address
# IS the identity, and the host's default source would hand both bots the same
# one. Port stays 0 — we want the address, not a fixed port.
BIND = _cfg("IRC_BIND", "")

# TLS certificate verification. Default ON: most networks present a chain that
# validates against the system CA bundle, and a NickServ password crosses the
# link, so the default must stay strict.
#
# IRC_TLS_VERIFY=0 drops to CERT_NONE, for a network whose servers mostly
# present expired or self-signed certs, or a name that does not match the one
# we dial. Use it only where no password is sent (no services, no NickServ) and
# orders count only end-to-end (ENCRYPTED=1 / CONFIRMED=1), which an attacker on
# the unverified TLS can read around, drop or delay but not forge.
TLS_VERIFY = _cfg("IRC_TLS_VERIFY", "1") not in ("0", "no", "off", "false")

# SASL PLAIN with NickServ's account (NICK as account, NICKSERV_PASS as
# password), done before registration completes. It exists for networks where
# the user cloak only lands once we are identified: identifying to NickServ
# after 001 shows the real host in the meantime. If SASL fails we QUIT instead
# of registering in clear, and systemd's backoff retries. Off by default: not
# every network's services support SASL.
SASL = _cfg("IRC_SASL", "0") not in ("0", "no", "off", "false")

LOG = _cfg("BOT_LOG", os.path.join(HERE, "bot.log"))
FIFO = _cfg("BOT_FIFO", os.path.join(HERE, "bot.send"))
TRUST_FILE = _cfg("BOT_TRUST", os.path.join(HERE, "bot.trust"))
DM_ALLOW_FILE = _cfg("BOT_DM_ALLOW", os.path.join(HERE, "bot.dmallow"))
ENV_FILE = _cfg("BOT_ENV", os.path.join(HERE, ".env"))
STARTUP_FILE = _cfg("BOT_STARTUP", os.path.join(HERE, "bot.startup"))
# Shared by ALL instances on purpose (not per-network): see leak_guard.py.
RESERVED_FILE = _cfg("BOT_RESERVED", os.path.join(HERE, "bot.reserved"))
RESERVED_STORE = _cfg("BOT_RESERVED_STORE", os.path.join(HERE, "reserved_recent.tsv"))

# End-to-end encrypted queries and order challenges (issue #4, e2e_bridge.py).
# OFF unless BOT_E2E_KEY names a key file (`python -m irc_olm keygen PATH`, kept
# outside the repo). Only then does the bot import irc-olm, its one non-stdlib
# dependency, so an instance without E2E stays stdlib-only and unchanged.
# One identity per network: each instance has its own key, state and network id.
# The network id binds the state file, so it must never change for an instance
# (unlike the server or network name, which can change). The state defaults to
# $XDG_STATE_HOME/claude-ircbot/<network id>.e2e.json: out of the repo, and one
# per instance. The key and the state are secrets the bot's Claude session must
# not reach either (README "Out of the session's reach").
# The pins file (BOT_E2E_PINS) holds the only keys the bot talks E2E with, and
# the bot only ever READS it: the operator makes it root-owned, or denies it to
# the bot's Claude session (see README). Required once E2E is on.
E2E_KEY = _cfg("BOT_E2E_KEY", "")
E2E_STATE = _cfg("BOT_E2E_STATE", "")
E2E_NETWORK = _cfg("BOT_E2E_NETWORK", "")
E2E_PINS = _cfg("BOT_E2E_PINS", "")
E2E_VERBS = ("ESAY", "EACT")
bridge = None  # e2e_bridge.Bridge when E2E is on
# bahamut's flood bank for our connection (irc_olm.pacing.FakeLag), when E2E
# is on: send_raw() charges every line, so E2E backs off while the bot talks.
fakelag = None

# Time zone of the wall-clock timestamps in bot.log and on the event stream.
# BOT_TZ names an IANA zone (e.g. Europe/Rome), DST-aware whatever the host's
# own TZ is; unset = the host's local time. check_config() resolves it, so a
# bad name stops the bot at startup with a plain message.
TZ_NAME = _cfg("BOT_TZ", "")
_TZ = None  # None = local time


def _now(fmt):
    return datetime.now(_TZ).strftime(fmt)


def load_env():
    out = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


ENV = load_env()
NICKSERV_PASS = ENV.get("NICKSERV_PASS") or os.environ.get("NICKSERV_PASS") or ""
startup_fired = False

# Ghost-on-connect state. A line drop leaves our old connection holding NICK as
# a ghost for ~7min (until the server ping-times it out). Every reconnect in
# that window hit 433 pre-001 and just gave up -> registration never completed
# -> recv timeout -> systemd restart -> flap loop for the whole 7min. Instead:
# on a pre-001 433 take a throwaway nick to reach 001, GHOST the stale nick from
# services, reclaim it, then IDENTIFY.
registered = False      # True once we've received 001 (registration complete)
ghost_needed = False    # True while we hold a temp nick and still want NICK back
ghost_tries = 0         # reclaims re-sent after a 433: services can be slower than the 4s beat
GHOST_RECLAIM_MAX = 5
tmp_nick = None         # the throwaway nick we fell back to
me = NICK               # the nick the server knows us by: 001's <me>, then our own NICK echoes

# Trust state: nick must be in trust_rules, host must match the nick's glob,
# AND we must have received RPL_WHOISREGNICK (307) confirming the nick is
# registered+identified to services. Cache reset on PART/QUIT/NICK.
trust_rules = []  # list of (nick_lower, host_glob)
dm_allow_rules = []  # list of (nick_lower, host_glob): DMs pass, never trusted
verified = set()  # lowercase nicks confirmed via 307
whois_pending = set()  # lowercase nicks we WHOIS'd, awaiting 307/318

# IRC line limit is 512 incl CRLF. Body safe ~400.
MAX_BODY = 400

sock = None
send_lock = threading.Lock()
leak_guard = LeakGuard(HOST, RESERVED_FILE, RESERVED_STORE)

# Idle-tick config: per-channel random cooldown (seconds) after the last
# HUMAN PRIVMSG. When elapsed, bot emits `IDLE <chan>` exactly once, then
# disarms. Next human PRIVMSG re-arms with a fresh random cooldown.
# Our own outgoing messages do NOT reset the timer.
# The channels and their ranges come from BOT_IDLE, as
# `#chan=MIN-MAX[,#chan=MIN-MAX...]` in seconds; unset = no idle ticks at all.
# Parsed by check_config() at startup, which fills the two dicts below.
IDLE_SPEC = _cfg("BOT_IDLE", "")
IDLE_RANGES: dict[str, tuple[int, int]] = {}
# state[chan_lower] = fire_at (monotonic) or None if disarmed
idle_state: dict[str, float | None] = {}
idle_lock = threading.Lock()


def parse_idle(spec):
    """BOT_IDLE -> {chan_lower: (min_sec, max_sec)}. ValueError on a bad entry."""
    out = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        chan, _, rng = item.rpartition("=")
        chan = chan.strip()
        lo, _, hi = rng.partition("-")
        try:
            lo, hi = int(lo), int(hi)
        except ValueError:
            lo = hi = -1
        if not chan.startswith(("#", "&")) or not 0 <= lo <= hi:
            raise ValueError(f"BOT_IDLE: bad entry {item!r}, want #chan=MIN-MAX in seconds")
        out[chan.lower()] = (lo, hi)
    return out

# Rolling kick history per channel (monotonic timestamps). If > KICK_MAX in KICK_WINDOW, give up rejoin.
kick_history: dict[str, list[float]] = {}
kick_lock = threading.Lock()
KICK_WINDOW = 60.0
KICK_MAX = 3

# Channel convergence: every CONVERGE_SECS, bring the joined channels back in
# line with the configured ones, so a ban, a kick past the rejoin limit or a
# lost JOIN does not leave the bot out of a channel indefinitely. Configured =
# the JOIN lines of bot.startup, re-read every tick so an edit takes effect
# live. Joined = what the server echoed back for our own nick. Every missing
# channel gets its startup lines replayed (ChanServ INVITE + JOIN). One
# direction only: a channel joined by hand from the FIFO is never parted —
# drop it from bot.startup and PART it if it must go.
CONVERGE_SECS = float(_cfg("BOT_CONVERGE_SECS", "120"))
joined: set[str] = set()
joined_lock = threading.Lock()
startup_done = False


def _arm_idle(chan: str) -> None:
    key = chan.lower()
    rng = IDLE_RANGES.get(key)
    if not rng:
        return
    lo, hi = rng
    delay = random.uniform(lo, hi)
    with idle_lock:
        idle_state[key] = time.monotonic() + delay


def idle_ticker_loop() -> None:
    while True:
        time.sleep(10)
        now = time.monotonic()
        fired = []
        with idle_lock:
            for chan, fire_at in list(idle_state.items()):
                if fire_at is not None and now >= fire_at:
                    idle_state[chan] = None
                    fired.append(chan)
        for chan in fired:
            emit("IDLE", chan)
            # also to bot.log so Monitor tails pick it up alongside IRC traffic
            log("*", f"IDLE {chan}")


def _startup_lines():
    try:
        with open(STARTUP_FILE) as f:
            lines = [raw.strip() for raw in f]
    except FileNotFoundError:
        return []
    return [line for line in lines if line and not line.startswith("#")]


def _is_me(nick: str) -> bool:
    return nick.lower() in (NICK.lower(), (tmp_nick or "").lower())


def converge_loop() -> None:
    last_missing: set[str] = set()
    while True:
        time.sleep(CONVERGE_SECS)
        if not startup_done:
            continue
        lines = _startup_lines()
        configured = {
            line.split()[1].lower()
            for line in lines
            if line.split()[0].upper() == "JOIN" and len(line.split()) > 1
        }
        with joined_lock:
            missing = configured - joined
        if missing != last_missing:
            # Only on change: a channel we're banned from must not emit an
            # event every tick for days.
            emit("CONVERGE", "missing=" + ",".join(sorted(missing)) if missing else "ok")
            last_missing = missing
        for chan in sorted(missing):
            for line in lines:
                if chan in (tok.lstrip(":").lower() for tok in line.split()[1:]):
                    process_cmd(line)
                    time.sleep(0.5)


def emit(kind, *parts):
    # [HH:MM] prefix gives the Monitor/Claude a real time anchor per event
    # instead of extrapolating elapsed time from turn order. Minutes only;
    # the monitor script's grep (examples/start-monitor.sh) tolerates it.
    print(f"[{_now('%H:%M')}] " + kind + " " + " ".join(str(p) for p in parts), flush=True)


def load_trust():
    global trust_rules
    out = []
    try:
        with open(TRUST_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 1)
                if len(parts) == 2:
                    out.append((parts[0].lower(), parts[1]))
    except FileNotFoundError:
        pass
    trust_rules = out
    emit("TRUST_LOADED", len(trust_rules))


def load_dm_allow():
    """bot.dmallow: `nick host_glob` lines whose DMs get through as UNTRUSTED.
    Readable, never authoritative — the trust list stays the only authority.
    It is for nicks outside bot.trust: a listed nick's DMs take the trust
    path, whatever bot.dmallow says."""
    global dm_allow_rules
    out = []
    try:
        with open(DM_ALLOW_FILE) as f:
            for line in f:
                parts = line.split("#", 1)[0].split()
                if len(parts) == 2:
                    out.append((parts[0].lower(), parts[1]))
    except FileNotFoundError:
        pass
    dm_allow_rules = out
    emit("DM_ALLOW_LOADED", len(dm_allow_rules))


def dm_allowed(nick, host):
    n = nick.lower()
    return any(an == n and fnmatch.fnmatch(host, hg) for an, hg in dm_allow_rules)


def is_trust_listed(nick):
    n = nick.lower()
    return any(tn == n for tn, _ in trust_rules)


def host_matches(nick, host):
    n = nick.lower()
    for tn, hg in trust_rules:
        if tn == n and fnmatch.fnmatch(host, hg):
            return True
    return False


def trust_check(nick, host):
    """Evaluate (trusted_bool, reason). Fires WHOIS on first sighting
    of a trust-listed nick. Trust requires: listed, host glob match,
    AND registered (307 seen). Anything less => untrusted."""
    n = nick.lower()
    if not is_trust_listed(n):
        return (False, "not-listed")
    if not host_matches(n, host):
        return (False, "host-mismatch")
    if n not in verified:
        if n not in whois_pending:
            whois_pending.add(n)
            send_raw(f"WHOIS {nick}")
            emit("WHOIS_FIRED", nick)
        return (False, "pending-whois")
    return (True, "ok")


# Hard block on private messages from strangers. A written policy alone is not
# enough: by the time Claude can apply it, the payload is
# already in its context. Dropping at the bot is the only real filter.
#
# The gate is `is_trust_listed`, NOT full `trust_check`: full trust needs a 307
# WHOIS round-trip, which is asynchronous, so the FIRST DM from a trusted nick
# would always be dropped. Listed nicks therefore still get through, tagged
# UNTRUSTED until 307 lands — a nick-squatter on a listed nick reaches the
# event stream but arrives visibly untrusted, which is the case the refusal
# policy already covers. Unlisted nicks never reach the event stream at all.
#
# The raw line is still written to bot.log by handle_server_line() before
# dispatch, so nothing is lost forensically — it just never becomes an event.
SERVICE_NICKS = {
    "nickserv", "chanserv", "memoserv", "operserv", "hostserv",
    "botserv", "helpserv", "statserv", "global", "services",
}
DM_REJECT_MSG = _cfg("BOT_DM_REJECT", "I only talk in channels, not in private.")
DM_REJECT_COOLDOWN = 6 * 3600.0
dm_rejected: dict[str, float] = {}  # nick_lower -> monotonic ts of last auto-reply
dm_lock = threading.Lock()


def dm_blocked(nick):
    """True if a private message from `nick` must be dropped before it can
    become an event. Services and our own nick are never blocked."""
    n = nick.lower()
    if not n or n == NICK.lower() or n in SERVICE_NICKS:
        return False
    return not is_trust_listed(n)


def dm_reject_once(nick):
    """Send the 'talk in channel' line at most once per DM_REJECT_COOLDOWN per
    nick. NOTICE, not PRIVMSG: RFC-correct for an automated reply, and it keeps
    two bots from ping-ponging auto-replies at each other.

    An empty BOT_DM_REJECT disables the reply entirely, for a bot that should
    stay silent in private. The DM is still dropped upstream — only the
    outbound rebound is suppressed."""
    if not DM_REJECT_MSG:
        return False
    n = nick.lower()
    now = time.monotonic()
    with dm_lock:
        last = dm_rejected.get(n)
        if last is not None and now - last < DM_REJECT_COOLDOWN:
            return False
        dm_rejected[n] = now
        if len(dm_rejected) > 500:
            for k, ts in sorted(dm_rejected.items(), key=lambda kv: kv[1])[:100]:
                dm_rejected.pop(k, None)
    send_raw(f"NOTICE {nick} :{DM_REJECT_MSG}")
    return True


# Second half of the same gate: a private prompt injection. Unlisted nicks are
# already dropped above, but a LISTED nick whose WHOIS hasn't resolved yet would
# reach the event stream tagged UNTRUSTED — payload in context, only policy
# stopping it from being obeyed. Policy is applied by a model; the gate is not.
# So the DM is HELD until the WHOIS answers: 307/330 releases it as TRUSTED,
# 318-without-307 drops it. Holding rather than dropping outright is what keeps
# the FIRST DM from a genuinely trusted nick (the operator's own, right after a
# reconnect) from vanishing. Nothing is lost forensically: the raw line is in
# bot.log either way, and the operator can have it read from there. Only
# plaintext is ever held: a nick with a pinned key never waits for a WHOIS
# (e2e_bridge, its key replaces layer 1).
DM_HOLD_MAX = 5  # per nick, newest wins — a flood can't grow the queue
dm_held: dict[str, list[tuple]] = {}  # nick_lower -> [(host, body, on_release)]


def dm_hold(nick, host, body, on_release=None):
    """Park a DM awaiting WHOIS. Returns the queue depth for the event line.
    `on_release`, when given, runs once the WHOIS has verified the sender and
    before the DM is released as TRUSTED; returning False takes the DM over
    (e2e_bridge challenges it, its nick pinned in the meantime, and releases
    or drops it itself). It runs outside dm_lock."""
    n = nick.lower()
    with dm_lock:
        q = dm_held.setdefault(n, [])
        q.append((host, body, on_release))
        for old_host, old_body, _ in q[:-DM_HOLD_MAX]:
            emit("DM_DROPPED", f"FROM={nick}", f"HOST={old_host}", f"LEN={len(old_body)}",
                 "evicted")
        del q[:-DM_HOLD_MAX]
        return len(q)


def dm_flush(nick, reason):
    """Resolve the held DMs for `nick`. Each one is re-checked at release time
    (host glob + 307), never trusted on the strength of the earlier sighting."""
    n = nick.lower()
    with dm_lock:
        queued = dm_held.pop(n, [])
    for host, body, on_release in queued:
        if n in verified and host_matches(n, host):
            try:
                if on_release is not None and not on_release():
                    continue
            except Exception as e:
                # Each DM is popped already: one that fails must not take the
                # rest of the queue with it.
                emit("DM_DROPPED", f"FROM={nick}", f"HOST={host}", f"LEN={len(body)}",
                     f"error:{type(e).__name__}")
                continue
            emit("MSG", "TRUSTED", f"FROM={nick}", f"HOST={host}",
                 f"TO={NICK}", "HELD=1", f"BODY={body}")
            on_trusted_line(nick, NICK, body, me)
        else:
            emit("DM_DROPPED", f"FROM={nick}", f"HOST={host}",
                 f"LEN={len(body)}", reason)


def trust_reset(nick, reason):
    n = nick.lower()
    dm_flush(nick, f"reset:{reason}")
    if n in verified or n in whois_pending:
        verified.discard(n)
        whois_pending.discard(n)
        emit("TRUST_RESET", nick, reason)


def _mask_secrets(line):
    # The SASL PLAIN blob is the password, base64'd: mask it whole.
    if line.startswith("AUTHENTICATE ") and line != "AUTHENTICATE PLAIN" \
            and line != "AUTHENTICATE +":
        return "AUTHENTICATE ********"
    if NICKSERV_PASS and NICKSERV_PASS in line:
        return line.replace(NICKSERV_PASS, "********")
    return line


# Origin tags are stamped into bot.log and read back by parsers, so keep them
# boring: no spaces, no brackets, nothing that could forge a second field.
ORIGIN_PAT = re.compile(r"^[a-z0-9_-]{1,24}$")


def log(direction, line, origin=None):
    tag = f"[{origin}] " if origin else ""
    try:
        with open(LOG, "a") as f:
            f.write(f"{_now('%H:%M:%S')} {direction} {tag}{_mask_secrets(line)}\n")
    except Exception:
        pass


def send_raw(s, origin=None):
    if fakelag is not None:
        fakelag.charge(s, time.monotonic())
    _write(s, origin)


def _paced_write(target, body):
    """The E2E pacer's writer: its lines are charged to the bank already. A
    failed write raises, so the pacer drops the rest of that message and
    reports it lost (e2e_bridge.on_lost)."""
    _write(f"PRIVMSG {target} :{body}", None, raising=True)


def _write(s, origin, raising=False):
    log(">", s, origin)
    with send_lock:
        try:
            sock.sendall((s + "\r\n").encode("utf-8", errors="replace"))
        except Exception as e:
            emit("ERROR", "send-fail", repr(e))
            if raising:
                raise


def split_say(target, text, origin=None):
    """Send text as one or more PRIVMSGs, preserving whitespace verbatim.

    Lines that fit in MAX_BODY go out untouched, so ascii art keeps its
    indentation and internal spacing (#86). Longer lines wrap at the last
    space before the limit WITHOUT stripping or collapsing spaces; a single
    oversized word is hard-split on a utf-8 char boundary.
    """
    b = text.encode("utf-8")
    if len(b) <= MAX_BODY:
        send_raw(f"PRIVMSG {target} :{text}", origin)
        return
    while b:
        if len(b) <= MAX_BODY:
            send_raw(f"PRIVMSG {target} :{b.decode('utf-8', errors='ignore')}", origin)
            return
        cut = b.rfind(b" ", 1, MAX_BODY + 1)
        if cut <= 0:
            cut = MAX_BODY
            while cut > 1 and (b[cut] & 0xC0) == 0x80:
                cut -= 1
        else:
            cut += 1  # keep the breaking space on this chunk, don't strip it
        send_raw(f"PRIVMSG {target} :{b[:cut].decode('utf-8', errors='ignore')}", origin)
        b = b[cut:]


# !bolletta (alias !token, !usage): the Claude subscription usage, read from the
# endpoint Claude Code's `/usage` calls. OFF unless BOT_USAGE_CMDS=1: it reads
# Claude Code's OAuth token from disk (BOT_USAGE_CREDS) and sends it to an
# undocumented Anthropic endpoint, so the parse is defensive. Trusted senders
# only. The token is re-read on every call and NEVER refreshed here — a
# refresh from this side would rotate it under the live session.
USAGE_ON = _cfg("BOT_USAGE_CMDS", "0") not in ("", "0", "no", "off", "false")
USAGE_CMDS = ("!bolletta", "!token", "!usage")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_CREDS = _cfg("BOT_USAGE_CREDS", os.path.expanduser("~/.claude/.credentials.json"))
USAGE_LABELS = {"session": "5h session", "weekly_all": "week"}
USAGE_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _usage_reset(iso):
    try:
        t = datetime.fromisoformat(iso).astimezone(_TZ)
        return f"{USAGE_DAYS[t.weekday()]} {t:%H:%M}"
    except (TypeError, ValueError):
        return "?"


def usage_line():
    import json
    import urllib.error
    import urllib.request
    try:
        with open(USAGE_CREDS) as f:
            tok = json.load(f)["claudeAiOauth"]["accessToken"]
    except (OSError, ValueError, KeyError) as e:
        return f"usage: credentials unreadable ({type(e).__name__})"
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {tok}",
        "anthropic-beta": "oauth-2025-04-20",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        return f"usage: HTTP {e.code}"
    except (OSError, ValueError) as e:
        return f"usage: error ({type(e).__name__})"
    parts = []
    for lim in data.get("limits") or []:
        if not isinstance(lim, dict):
            continue
        kind = lim.get("kind")
        pct = lim.get("percent")
        if kind == "weekly_scoped":
            if not pct:
                continue
            model = ((lim.get("scope") or {}).get("model") or {}).get("display_name") or "?"
            label = f"week {model}"
        else:
            label = USAGE_LABELS.get(kind, kind)
        parts.append(f"{label} {pct}% (reset {_usage_reset(lim.get('resets_at'))})")
    if not parts:
        return "usage: no readable limits in the reply"
    return "usage: " + " · ".join(parts)


def usage_reply(target):
    line = usage_line()
    split_say(target, line, "bolletta")
    emit("BOLLETTA", f"TO={target}", line)


def bot_command(body, own_nick):
    """The command a line carries, lowercased: the whole line, or what
    follows `own_nick` at its start, after an optional `:` or `,` and any
    whitespace (`<nick>: !bolletta`, `<nick>, !bolletta`, `<nick> !bolletta`).
    `own_nick` is ours when the line was said. The addressed form is how a
    user with a pinned key gets the line challenged in a channel
    (e2e_bridge)."""
    text = body.strip()
    m = re.match(re.escape(own_nick) + r"[:,]?\s*", text, re.IGNORECASE)
    if m:
        text = text[m.end():]
    return text.lower()


def on_trusted_line(nick, target, body, own_nick):
    """A plaintext line just reached the stream as TRUSTED: straight away,
    released from its WHOIS hold, or confirmed by the ACK of its challenge
    (e2e_bridge). The bot's own commands answer here and nowhere else, so
    they follow the verdict the event line carries. An encrypted DM never
    comes here: the answer would go out in clear. `own_nick` is ours when
    the line was said: a confirmed one may come after we renamed."""
    if USAGE_ON and bot_command(body, own_nick) in USAGE_CMDS:
        reply_to = target if target.startswith(("#", "&")) else nick
        threading.Thread(target=usage_reply, args=(reply_to,), daemon=True).start()


# DCC SEND: offer a file from DCC_DIR to a nick, typically for a sidecar. A
# listener on the address we connect from, the CTCP with its port, ONE
# connection served, then closed.
#
# IPv6 by default, announced as the address literal. Classic DCC wants the
# address as a 32-bit decimal integer, and a client that does not accept a v6
# literal there (HexChat, for one) reads the string as a number, gets 0.0.0.2
# and fails with "No route to host". irssi and mIRC 7 read the v6 literal, so
# the v6 path stays.
#
# BOT_DCC_BIND4 turns on the v4 path and takes precedence: listen there (a
# private address, behind NAT) and announce BOT_DCC_ADDR4 (the public address
# in front of the port forward) as a 32-bit integer, which v4-only clients
# accept too.
#
# Behind a firewall the port cannot be ephemeral: the hole has to be opened in
# advance, and the kernel's pick cannot be. BOT_DCC_PORT_MIN / MAX give a fixed
# range to mirror in the firewall rule; zero (the default) means an ephemeral
# port, fine only where no firewall is in the way.
DCC_DIR = _cfg("BOT_DCC_DIR", os.path.join(HERE, "dcc"))
DCC_TIMEOUT = int(_cfg("BOT_DCC_TIMEOUT", "180"))
# How long we wait for the peer to close after the last byte, before giving up.
DCC_DRAIN_TIMEOUT = int(_cfg("BOT_DCC_DRAIN_TIMEOUT", "60"))
DCC_PORT_MIN = int(_cfg("BOT_DCC_PORT_MIN", "0"))
DCC_PORT_MAX = int(_cfg("BOT_DCC_PORT_MAX", "0"))
# v4 path: the address to listen on, and the one to announce (the same unless
# NAT sits in front). Empty = stay on IRC_BIND's v6 address.
DCC_BIND4 = _cfg("BOT_DCC_BIND4", "")
DCC_ADDR4 = _cfg("BOT_DCC_ADDR4", "") or DCC_BIND4
DCC_NAME_PAT = re.compile(r"[A-Za-z0-9._-]{1,80}\Z")
# The offer name is what the peer's client shows and never touches the disk,
# so it needs no traversal guard — only CTCP safety (no spaces, the field
# separator) and a length that fits a scene title (~78) plus extension.
DCC_OFFER_NAME_PAT = re.compile(r"[A-Za-z0-9._-]{1,120}\Z")


def _dcc_bind(srv, addr):
    """Bind the listener inside the configured range, or ephemeral if unset.

    Every offer holds its port for the whole transfer, so with N offers in
    flight the first N ports of the range are taken: we walk it until one
    binds. Range exhausted -> the caller refuses the offer.
    """
    if DCC_PORT_MIN <= 0 or DCC_PORT_MAX < DCC_PORT_MIN:
        srv.bind((addr, 0))
        return
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    for port in range(DCC_PORT_MIN, DCC_PORT_MAX + 1):
        try:
            srv.bind((addr, port))
        except OSError:
            continue
        return
    raise OSError(f"no free port in {DCC_PORT_MIN}-{DCC_PORT_MAX}")


def _dcc_drain(conn):
    """Half-close, read the peer's ACK stream to EOF, return the last ACK.

    A DCC receiver answers every block with a 4-byte big-endian ACK, the total
    received so far. Those ACKs pile up in our receive buffer, and closing a
    socket with unread data makes the kernel send RST instead of FIN: the peer
    loses whatever was still in flight and ends up with a truncated file while
    we have already logged DCC_DONE (seen with a slow client: 131072 bytes of
    246254). Half-close, drain, and the final close is a clean FIN. The last
    ACK is also the only proof of delivery we have.
    """
    last = None
    try:
        conn.shutdown(socket.SHUT_WR)
        conn.settimeout(DCC_DRAIN_TIMEOUT)
        tail = b""
        while True:
            more = conn.recv(65536)
            if not more:
                break
            tail = (tail + more)[-4:]
        if len(tail) == 4:
            last = int.from_bytes(tail, "big")
    except OSError:
        pass
    return last


def _dcc_event(kind, *parts):
    """Emit a DCC lifecycle event on BOTH stdout and bot.log.

    stdout is the Monitor's stream; bot.log is what the sidecars tail. A
    sidecar that wants to announce "<nick> got the file" only follows
    bot.log (it is the PRIVMSG stream), so without this second write the
    completion would be invisible to it. Same trick as IDLE above.
    """
    emit(kind, *parts)
    log("*", kind + " " + " ".join(str(p) for p in parts))


def _dcc_serve(srv, path, nick, name):
    """Accept one peer, push the file, hang up. Runs off the reader thread."""
    try:
        conn, _peer = srv.accept()
    except Exception as e:
        _dcc_event("DCC_TIMEOUT", f"TO={nick}", f"FILE={name}", repr(e))
        return
    finally:
        srv.close()
    sent = 0
    try:
        # accept() does NOT inherit the listener's timeout reliably; set it.
        conn.settimeout(DCC_TIMEOUT)
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                conn.sendall(chunk)
                sent += len(chunk)
    except Exception as e:
        _dcc_event("DCC_ERROR", f"TO={nick}", f"FILE={name}", f"SENT={sent}", repr(e))
    else:
        ack = _dcc_drain(conn)
        _dcc_event("DCC_DONE", f"TO={nick}", f"FILE={name}", f"BYTES={sent}",
                   f"ACK={ack if ack is not None else 'none'}")
    finally:
        conn.close()


def dcc_send(nick, name, origin=None, offer=None):
    # `name` is the file on disk; `offer` is the name announced to the peer,
    # defaulting to `name`. Both come from a sidecar and ride a CTCP whose
    # fields are space-separated and unquotable: no path, no traversal, no
    # spaces. `name` must stay a bare basename (it opens a file); `offer`
    # never touches disk, so it only needs to be CTCP-safe.
    if not DCC_NAME_PAT.match(name):
        emit("CMD_ERROR", "dcc-bad-name", repr(name))
        return
    if offer is None:
        offer = name
    elif not DCC_OFFER_NAME_PAT.match(offer):
        emit("CMD_ERROR", "dcc-bad-offer", repr(offer))
        return
    path = os.path.join(DCC_DIR, name)
    if not os.path.isfile(path):
        emit("CMD_ERROR", "dcc-no-file", path)
        return
    bind = DCC_BIND4 or BIND
    if not bind:
        emit("CMD_ERROR", "dcc-no-bind", "IRC_BIND empty: no address, no offer")
        return
    if DCC_BIND4:
        # The DCC address field is a 32-bit integer, not a dotted string.
        try:
            advertise = str(int.from_bytes(socket.inet_aton(DCC_ADDR4), "big"))
        except OSError as e:
            emit("CMD_ERROR", "dcc-bad-addr4", repr(DCC_ADDR4), repr(e))
            return
    else:
        advertise = BIND
    size = os.path.getsize(path)
    family = socket.AF_INET if DCC_BIND4 else socket.AF_INET6
    srv = socket.socket(family, socket.SOCK_STREAM)
    try:
        srv.settimeout(DCC_TIMEOUT)
        _dcc_bind(srv, bind)
        srv.listen(1)
    except Exception as e:
        srv.close()
        emit("CMD_ERROR", "dcc-listen-fail", repr(e))
        return
    port = srv.getsockname()[1]
    threading.Thread(
        target=_dcc_serve, args=(srv, path, nick, offer), daemon=True
    ).start()
    send_raw(f"PRIVMSG {nick} :\x01DCC SEND {offer} {advertise} {port} {size}\x01",
             origin)
    emit("DCC_OFFER", f"TO={nick}", f"FILE={name}", f"OFFER={offer}",
         f"ADDR={advertise}", f"PORT={port}", f"SIZE={size}")


def run_startup():
    try:
        with open(STARTUP_FILE) as f:
            lines = f.readlines()
    except FileNotFoundError:
        emit("STARTUP_DONE", "no-file")
        return
    count = 0
    for raw in lines:
        line = raw.rstrip("\r\n").strip()
        if not line or line.startswith("#"):
            continue
        try:
            process_cmd(line)
            count += 1
        except Exception as e:
            emit("ERROR", "startup-fail", repr(e), line[:80])
        time.sleep(0.5)
    global startup_done
    startup_done = True
    emit("STARTUP_DONE", count)


def fire_startup(reason):
    global startup_fired
    if startup_fired:
        return
    startup_fired = True
    emit("STARTUP_FIRED", reason)
    threading.Thread(target=run_startup, daemon=True).start()


def _reclaim_after_ghost():
    # GHOST is near-instant but async; give services a beat to kill the ghost,
    # then reclaim the real nick. The server's echo of our own NICK change (in
    # handle_server_line) fires IDENTIFY. This is wording-agnostic on purpose,
    # so we don't depend on the exact "has been ghosted" NOTICE text, which
    # differs between services packages (suxserv, atheme, ...).
    time.sleep(4)
    if ghost_needed:
        send_raw(f"NICK {NICK}")
        emit("NS_GHOST_RECLAIM", NICK)


def _schedule_reclaim():
    threading.Thread(target=_reclaim_after_ghost, daemon=True).start()


def _sasl_abort(why):
    # Never fall back to registering unauthenticated: that is the leak SASL is
    # here to prevent. Leave, and let systemd's backoff try again later.
    emit("SASL_FAIL", why)
    send_raw("QUIT :sasl failed")
    sys.exit(1)


def handle_server_line(line):
    global registered, ghost_needed, ghost_tries, tmp_nick, me
    log("<", line)
    if line.startswith("PING "):
        send_raw("PONG " + line[5:])
        return

    m = re.match(r"^(?::(\S+)\s+)?(\S+)(?:\s+(.*))?$", line)
    if not m:
        return
    prefix, cmd, rest = m.groups()
    nick = prefix.split("!")[0] if prefix else ""
    # Host from the source prefix (nick!user@host). Computed once so EVERY
    # emitted event can carry HOST= — the Monitor must never be host-blind on a
    # JOIN/NICK: a nick that imitates a trusted one (`alice_` for `alice`) is
    # told apart by its host, and without HOST= only the raw bot.log shows it.
    host = prefix.split("@", 1)[1] if prefix and "@" in prefix else ""
    rest = rest or ""
    if cmd in ("PRIVMSG", "NOTICE", "TOPIC") and " :" in rest:
        tgt, _, body = rest.partition(" :")
        leak_guard.record(tgt.strip(), body)

    if cmd == "001":
        registered = True
        emit("CONNECTED", NICK)
        if rest:
            me = rest.split()[0]
        if bridge and rest:
            # The nick we registered with: a ghost fallback taken before 001
            # gets no NICK echo (e2e_bridge: what an order must name).
            bridge.on_own_nick(me)
        for tn, _ in trust_rules:
            whois_pending.add(tn)
            send_raw(f"WHOIS {tn}")
            emit("WHOIS_FIRED", tn)
        if SASL and not ghost_needed:
            # Already logged in to our account before 001: nothing to IDENTIFY.
            fire_startup("sasl")
        elif NICKSERV_PASS:
            if ghost_needed:
                # Our real nick is held (by our own ghost after a line drop):
                # kill it from services, then reclaim + IDENTIFY (see below).
                send_raw(f"PRIVMSG NickServ :GHOST {NICK} {NICKSERV_PASS}")
                emit("NS_GHOST_SENT", NICK)
                _schedule_reclaim()
            else:
                send_raw(f"PRIVMSG NickServ :IDENTIFY {NICKSERV_PASS}")
                emit("NS_IDENTIFY_SENT")
        else:
            fire_startup("no-nickserv-pass")
        return
    if cmd == "CAP" and SASL and not registered:
        # ":srv CAP * ACK :sasl" / "NAK :sasl"
        sub = rest.split()[1] if len(rest.split()) > 1 else ""
        if sub == "ACK" and "sasl" in rest.split(" :", 1)[-1].split():
            send_raw("AUTHENTICATE PLAIN")
        elif sub == "NAK":
            _sasl_abort("CAP NAK")
        return
    if cmd == "AUTHENTICATE" and SASL and rest.strip() == "+":
        blob = f"{NICK}\0{NICK}\0{NICKSERV_PASS}".encode()
        send_raw("AUTHENTICATE " + base64.b64encode(blob).decode())
        return
    if cmd == "903" and SASL:
        emit("SASL_OK")
        send_raw("CAP END")
        return
    if cmd in ("902", "904", "905", "906", "908") and SASL and not registered:
        _sasl_abort(f"{cmd} {rest}")
        return
    if cmd == "005":
        # RPL_ISUPPORT: ":srv 005 <me> TOK TOK ... :are supported". The CHAL
        # names the network by its NETWORK token; LINELEN, where a server
        # advertises it, widens the E2E chunk budget.
        if bridge:
            bridge.on_isupport(rest.split(" :", 1)[0].split()[1:])
        return
    if cmd in ("433", "432", "437"):
        emit("NICK_ERROR", cmd, rest)
        if not registered:
            # Pre-registration collision, almost always our own lingering
            # ghost. Grab a throwaway nick so registration completes and we
            # reach 001 -> then GHOST the real nick and reclaim it. Random
            # suffix so a second flap doesn't collide with the same temp.
            tmp_nick = f"{NICK}-{random.randint(100, 999)}"
            send_raw(f"NICK {tmp_nick}")
            ghost_needed, ghost_tries = True, 0
            emit("NICK_FALLBACK", tmp_nick)
        elif ghost_needed and rest.split()[1:2] == [NICK]:
            # Our reclaim beat the GHOST: services can take longer than the 4s
            # beat to answer it (6s has been seen on atheme). Try again, a few
            # times.
            if ghost_tries < GHOST_RECLAIM_MAX:
                ghost_tries += 1
                _schedule_reclaim()
            else:
                emit("NS_GHOST_GIVEUP", NICK)
        return
    if cmd in ("464", "465"):
        emit("AUTH_ERROR", cmd, rest)
        return
    if cmd == "401":
        # ERR_NOSUCHNICK: ":server 401 <me> <target> :No such nick/channel"
        # Surface as event so silent PRIVMSG/NOTICE failures become visible
        # (otherwise they only land in bot.log and the sender never retries).
        parts = rest.split(" :", 1)
        head = parts[0].split()
        tgt = head[1] if len(head) > 1 else ""
        reason = parts[1] if len(parts) > 1 else "No such nick/channel"
        emit("IRC_ERROR", "401", tgt, reason)
        return
    if cmd == "307":
        # RPL_WHOISREGNICK: ":server 307 <me> <target> :has identified for this nick"
        parts = rest.split()
        if len(parts) >= 2:
            target = parts[1].lower()
            verified.add(target)
            whois_pending.discard(target)
            emit("VERIFIED", parts[1])
            dm_flush(parts[1], "307")
        return
    if cmd == "330":
        # RPL_WHOISACCOUNT (Libera/solanum): ":srv 330 <me> <nick> <account>
        # :is logged in as". Libera does NOT emit 307 (a Bahamut-ism), so this
        # is its identified-to-services signal — mirror the 307 path or a
        # trust-listed nick never verifies there. Safe: on Libera only the real
        # account holder wears the `user/<account>` cloak the host-glob already
        # pins, so account-vs-nick mismatch can't slip trust through.
        parts = rest.split()
        if len(parts) >= 2:
            target = parts[1].lower()
            verified.add(target)
            whois_pending.discard(target)
            emit("VERIFIED", parts[1])
            dm_flush(parts[1], "330")
        return
    if cmd == "318":
        # RPL_ENDOFWHOIS: if nick was pending and not verified => not registered
        parts = rest.split()
        if len(parts) >= 2:
            target = parts[1].lower()
            if target in whois_pending:
                whois_pending.discard(target)
                if target not in verified:
                    emit("NOT_REGISTERED", parts[1])
            # Either way the WHOIS is over: nothing may stay parked past it.
            dm_flush(parts[1], "318")
        return
    if cmd == "NOTICE":
        parts = rest.split(" :", 1)
        tgt = parts[0]
        msg = parts[1] if len(parts) > 1 else ""
        if nick.lower() == "nickserv" and NICKSERV_PASS:
            # Services answer in their own language: the English and Italian
            # wordings of success and failure.
            low = msg.lower()
            if any(k in low for k in ("identified", "accepted", "recognized", "riconosciuto", "autenticato", "accettata", "identificato")):
                fire_startup("nickserv-ok")
            elif any(k in low for k in ("incorrect", "invalid", "failed", "errata", "errato")):
                emit("NS_IDENTIFY_FAIL", msg)
        if nick and nick.lower() != NICK.lower():
            emit("NOTICE", f"FROM={nick}", f"HOST={host}", f"TO={tgt}", f"BODY={msg}")
        return
    if cmd == "INVITE":
        parts = rest.split(" :", 1)
        target_chan = parts[1] if len(parts) == 2 else rest.split()[-1]
        trusted, reason = trust_check(nick, host)
        emit("INVITE", nick, f"HOST={host}", target_chan, "trusted" if trusted else f"untrusted:{reason}")
        if trusted:
            send_raw(f"JOIN {target_chan}")
        return
    if cmd == "KICK":
        # rest = "#chan target[ :reason]"
        kick_parts = rest.split(" :", 1)
        head = kick_parts[0].split()
        kick_chan = head[0] if head else ""
        kick_target = head[1] if len(head) > 1 else ""
        kick_reason = kick_parts[1] if len(kick_parts) > 1 else ""
        emit("KICK", nick, f"HOST={host}", kick_chan, kick_target, kick_reason)
        if kick_chan and _is_me(kick_target):
            with joined_lock:
                joined.discard(kick_chan.lower())
        if kick_chan and kick_target.lower() == NICK.lower():
            # Auto-rejoin with kick-flood backoff. Repeated kicks in a short
            # window = a hostile op. Walk away rather than trigger Excess Flood.
            now = time.monotonic()
            key = kick_chan.lower()
            with kick_lock:
                history = [t for t in kick_history.get(key, []) if now - t < KICK_WINDOW]
                history.append(now)
                kick_history[key] = history
                count = len(history)
            if count > KICK_MAX:
                emit("KICK_GIVEUP", kick_chan, nick, f"{count} kicks in {KICK_WINDOW}s")
            else:
                def _rejoin(ch: str = kick_chan) -> None:
                    send_raw(f"PRIVMSG ChanServ :INVITE {ch}")
                    threading.Timer(0.5, lambda: send_raw(f"JOIN {ch}")).start()
                threading.Timer(2.0, _rejoin).start()
                emit("KICK_REJOIN", kick_chan, nick, f"{count}/{KICK_MAX}")
        return
    if cmd == "PRIVMSG":
        parts = rest.split(" :", 1)
        target = parts[0].strip()
        body = parts[1] if len(parts) > 1 else ""
        if bridge and bridge.is_e2e(body):
            # Ciphertext: never the CTCP event, never a plaintext path.
            bridge.on_e2e_line(nick, host, target, body)
            return
        if body.startswith("\x01") and body.endswith("\x01"):
            ctcp = body.strip("\x01")
            emit("CTCP", f"FROM={nick}", f"HOST={host}", f"TO={target}", f"BODY={ctcp}")
            if ctcp.upper() == "VERSION":
                send_raw(f"NOTICE {nick} :\x01VERSION claude-code PoC\x01")
            elif ctcp.upper().startswith("PING"):
                send_raw(f"NOTICE {nick} :\x01{ctcp}\x01")
            return
        if bridge:
            # A nick with a pinned key: the key replaces layer 1. Its PM or a
            # line addressed to the bot is challenged
            # whatever its host, bot.trust or services say, and anything else
            # in plain text is UNTRUSTED: a squatter, on any host, gets no
            # more than that. The DM gates below are layer 1's.
            verdict = bridge.gate_plaintext(nick, host, prefix, target, body)
            if verdict is not None:
                if verdict == "unchallenged":
                    emit("MSG", "UNTRUSTED", "WHY=unchallenged", f"FROM={nick}", f"HOST={host}",
                         f"TO={target}", f"BODY={body}")
                # "held": waiting for the ACK of a CHAL, e2e_bridge emits it later.
                if target.startswith("#") and nick.lower() != NICK.lower():
                    _arm_idle(target)
                return
        # Private message from a nick that isn't even trust-listed: drop it
        # here. No BODY is emitted — the whole point is that the payload never
        # reaches the event stream.
        is_dm = not target.startswith(("#", "&"))
        if is_dm and not is_trust_listed(nick) and dm_allowed(nick, host):
            emit("MSG", "UNTRUSTED", f"FROM={nick}", f"HOST={host}", f"TO={target}", f"BODY={body}")
            return
        if is_dm and dm_blocked(nick):
            replied = dm_reject_once(nick)
            emit(
                "DM_BLOCKED",
                f"FROM={nick}",
                f"HOST={host}",
                f"LEN={len(body)}",
                "replied" if replied else "silent",
            )
            return
        trusted, reason = trust_check(nick, host)
        trust = "TRUSTED" if trusted else "UNTRUSTED"
        if not target.startswith(("#", "&")) and not trusted:
            # Listed (dm_blocked already ate the strangers) but not trusted yet.
            # pending-whois => park it, the 307/330/318 handler decides. Anything
            # else (host-mismatch: a squatter on a listed nick) will never become
            # trusted, so drop it now and rebound the notice.
            if reason == "pending-whois":
                # A nick pinned while its PM waited is challenged at release
                # (e2e_bridge): layer 1 never releases a pinned nick's line.
                def release():
                    if bridge.gate_plaintext(nick, host, prefix, target, body) is not None:
                        return False
                    bridge.trusted_in_plain(nick)
                    return True
                depth = dm_hold(nick, host, body, on_release=release if bridge else None)
                emit("DM_HELD", f"FROM={nick}", f"HOST={host}",
                     f"LEN={len(body)}", f"QUEUED={depth}")
            else:
                replied = dm_reject_once(nick)
                emit("DM_BLOCKED", f"FROM={nick}", f"HOST={host}",
                     f"LEN={len(body)}", reason,
                     "replied" if replied else "silent")
            return
        if bridge and trusted and is_dm:
            # Released TRUSTED in plain text: so is the reply.
            bridge.trusted_in_plain(nick)
        emit("MSG", trust, f"FROM={nick}", f"HOST={host}", f"TO={target}", f"BODY={body}")
        if is_trust_listed(nick) and not trusted:
            emit("TRUST_DENIED", nick, host, reason)
        if trust == "TRUSTED":
            on_trusted_line(nick, target, body, me)
        # Re-arm the idle timer only on HUMAN PRIVMSG to a tracked channel.
        # Our own outgoing messages don't reach this path.
        if target.startswith("#") and nick.lower() != NICK.lower():
            _arm_idle(target)
        return
    if cmd == "JOIN":
        if bridge and nick.lower() == me.lower():
            # Our own full mask, as receivers see it: sizes the E2E chunks.
            bridge.on_own_mask(prefix)
        emit("JOIN", nick, f"HOST={host}", rest.lstrip(":"))
        if _is_me(nick) and rest:
            with joined_lock:
                joined.add(rest.split()[0].lstrip(":").lower())
        return
    if cmd == "PART":
        emit("PART", nick, f"HOST={host}", rest)
        if _is_me(nick) and rest:
            with joined_lock:
                joined.discard(rest.split()[0].lstrip(":").lower())
        trust_reset(nick, "part")
        return
    if cmd == "QUIT":
        emit("QUIT", nick, f"HOST={host}", rest)
        trust_reset(nick, "quit")
        return
    if cmd == "NICK":
        new_nick = rest.lstrip(":")
        if nick.lower() == me.lower():
            me = new_nick
            if bridge:
                # Ours: the receivers now see another mask, and the E2E chunk
                # budget is sized for it.
                bridge.on_own_nick(new_nick)
        emit("NICK_CHANGE", nick, f"HOST={host}", new_nick)
        trust_reset(nick, "nick-change")
        if ghost_needed and new_nick.lower() == NICK.lower():
            # We just reclaimed the real nick after ghosting the stale one.
            # Now IDENTIFY on it (NickServ's "identified" NOTICE then fires the
            # channel-join startup, so JOINs happen on the right nick). Under
            # SASL we are logged in already, and atheme's "You are already
            # logged in" would never fire it: start straight away.
            ghost_needed = False
            if SASL:
                fire_startup("sasl")
            elif NICKSERV_PASS:
                send_raw(f"PRIVMSG NickServ :IDENTIFY {NICKSERV_PASS}")
                emit("NS_IDENTIFY_SENT")
        return
    if cmd == "ERROR":
        emit("SERVER_ERROR", rest)
        return


# EAGAIN on a TLS socket means "the record isn't complete yet", not "the link
# is dead" — retrying is correct. But retrying FOREVER would leave a process
# that looks alive and hears nothing, which is worse than dying, so the retry
# is capped and a real recv resets the count.
RECV_RETRY_SLEEP = 0.05
RECV_RETRY_MAX = 200            # ~10s of solid EAGAIN, then die for real


def reader_loop():
    buf = b""
    retries = 0
    while True:
        try:
            data = sock.recv(8192)
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError) as e:
            # A single BlockingIOError(11) used to kill the process and cost a
            # full reconnect, while genuine TimeoutErrors were far more common
            # — so the two must NOT share a handler. Timeout and reset still
            # fall through and kill us.
            retries += 1
            if retries > RECV_RETRY_MAX:
                emit("ERROR", "recv-fail", f"{retries} consecutive {e!r}")
                return
            time.sleep(RECV_RETRY_SLEEP)
            continue
        except Exception as e:
            emit("ERROR", "recv-fail", repr(e))
            return
        retries = 0
        if not data:
            emit("DISCONNECTED")
            return
        buf += data
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.rstrip(b"\r").decode("utf-8", errors="replace")
            if line:
                try:
                    handle_server_line(line)
                except Exception as e:
                    emit("ERROR", "handler-fail", repr(e), line[:80])


def writer_loop():
    if os.path.exists(FIFO):
        try:
            os.remove(FIFO)
        except Exception:
            pass
    os.mkfifo(FIFO, 0o600)
    emit("FIFO_READY", FIFO)
    while True:
        with open(FIFO, "r") as f:
            for line in f:
                line = line.rstrip("\r\n")
                if line:
                    run_cmd(line)


def _e2e_bound(verb, target):
    """Whether a FIFO command's text is meant to go encrypted: ESAY/EACT, or
    a SAY/ACT the conversation's reply mode encrypts (e2e_bridge)."""
    return verb in E2E_VERBS or (
        verb in ("SAY", "ACT") and bridge is not None
        and not target.startswith(("#", "&")) and bridge.wants_encryption(target))


def _e2e_unescape(text):
    """ESAY/EACT text with `\\n` as a line break and `\\\\` as a backslash:
    the FIFO is one command per line, the encrypted message need not be
    (claude-ircbot#8). Any other backslash stays as typed."""
    return re.sub(r"\\([\\n])", lambda m: "\n" if m.group(1) == "n" else "\\", text)


def _redacted(line):
    """`line` as the event stream may show it. A command bound for an E2E
    query keeps its verb and target, never its text: encrypting it would be
    pointless if a failure echoed it in clear to bot.stdout.log."""
    verb, _, rest = line.partition(" ")
    target, _, text = rest.partition(" ")
    encrypted = _e2e_bound(verb.partition(":")[0].upper(), target)
    return f"{verb} {target} [{len(text.encode())} bytes]" if encrypted else line


def run_cmd(line):
    try:
        process_cmd(line)
    except Exception as e:
        shown = _redacted(line)
        # The exception text can quote the line too: only its type then.
        emit("CMD_ERROR", "cmd-fail", repr(e) if shown == line else type(e).__name__, shown[:80])


def outbound_ok(verb, rest, origin):
    """Leak gate for every chat line the FIFO asks for (leak_guard.py).

    RAW is parsed too, or `RAW PRIVMSG #x :...` would be the way around it.
    A line to a reserved channel of ours is recorded here, since our own
    words there are just as reserved as anyone else's.
    """
    if verb == "RAW":
        m = re.match(r"(?i)(PRIVMSG|NOTICE|TOPIC)\s+(\S+)\s+:(.*)", rest)
        if not m:
            return True
        target, text = m.group(2), m.group(3)
    elif verb in ("SAY", "ACT", "NOTICE", *E2E_VERBS) and " " in rest:
        target, text = rest.split(" ", 1)
    else:
        return True
    # Services commands name the reserved channel by design: without this,
    # bot.startup's ChanServ INVITE for a reserved channel would be blocked.
    if target.lower() in ("chanserv", "nickserv"):
        return True
    reason = leak_guard.check(target, text, dm_ok=is_trust_listed(target))
    if reason:
        if _e2e_bound(verb, target):
            # The kind of match only: what follows it quotes the text.
            reason = reason.split(":", 1)[0]
        log("!", f"LEAK_BLOCKED {verb} {target} {reason}", origin)
        emit("LEAK_BLOCKED", verb, f"TO={target}", reason, f"LEN={len(text)}")
        return False
    leak_guard.record(target, text)
    return True


def process_cmd(line):
    if " " in line:
        verb, rest = line.split(" ", 1)
    else:
        verb, rest = line, ""
    origin = None
    if ":" in verb:
        verb, _, origin = verb.partition(":")
        origin = origin.lower()
        if not ORIGIN_PAT.match(origin):
            emit("CMD_ERROR", "bad-origin-tag", repr(origin),
                 "allowed: [a-z0-9_-]{1,24}")
            return
    verb = verb.upper()
    if not outbound_ok(verb, rest, origin):
        return
    if verb in ("SAY", "ACT") and bridge and " " in rest:
        target, text = rest.split(" ", 1)
        if not target.startswith(("#", "&")) and bridge.wants_encryption(target):
            # The conversation is encrypted: so is the reply (e2e_bridge).
            bridge.send_text(target, text, action=verb == "ACT", origin=origin)
            return
    if verb in E2E_VERBS:
        # Never echo the text in a CMD_ERROR: it was meant to be encrypted.
        target = rest.split(" ", 1)[0]
        if " " not in rest:
            emit("CMD_ERROR", f"{verb} needs <nick> <text>")
        elif target.startswith(("#", "&")):
            emit("CMD_ERROR", "e2e-channel", verb, f"TO={target}", "E2E is for queries only")
        elif bridge is None:
            emit("CMD_ERROR", "e2e-off", verb, f"TO={target}", "BOT_E2E_KEY is not set")
        else:
            bridge.send_text(target, _e2e_unescape(rest.split(" ", 1)[1]),
                             action=verb == "EACT", origin=origin)
    elif verb == "SAY":
        if " " not in rest:
            emit("CMD_ERROR", "SAY needs <target> <text>", repr(rest))
            return
        target, text = rest.split(" ", 1)
        split_say(target, text, origin)
    elif verb == "ACT":
        if " " not in rest:
            emit("CMD_ERROR", "ACT needs <target> <text>", repr(rest))
            return
        target, text = rest.split(" ", 1)
        send_raw(f"PRIVMSG {target} :\x01ACTION {text}\x01", origin)
    elif verb == "NOTICE":
        if " " not in rest:
            emit("CMD_ERROR", "NOTICE needs <target> <text>", repr(rest))
            return
        target, text = rest.split(" ", 1)
        send_raw(f"NOTICE {target} :{text}", origin)
    elif verb == "JOIN":
        send_raw(f"JOIN {rest}", origin)
    elif verb == "PART":
        send_raw(f"PART {rest}", origin)
    elif verb == "WHOIS":
        send_raw(f"WHOIS {rest}", origin)
    elif verb == "QUIT":
        send_raw(f"QUIT :{rest or 'bye'}", origin)
    elif verb == "RAW":
        send_raw(rest, origin)
    elif verb == "DCC":
        if " " not in rest:
            emit("CMD_ERROR", "DCC needs <nick> <file> [name]", repr(rest))
            return
        parts = rest.split(" ", 2)
        nick, name = parts[0], parts[1].strip()
        offer = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
        dcc_send(nick, name, origin, offer)
    else:
        emit("CMD_ERROR", "unknown-verb", repr(verb),
             "valid: SAY ACT ESAY EACT NOTICE JOIN PART WHOIS QUIT RAW DCC")


def _start_e2e():
    """Build the E2E bridge when BOT_E2E_KEY is set. Without it the bot stays
    stdlib-only and unchanged: irc-olm is imported here and nowhere else."""
    global bridge, fakelag
    if not E2E_KEY:
        return
    if not E2E_NETWORK or not E2E_PINS:
        raise ValueError("BOT_E2E_KEY is set: BOT_E2E_NETWORK and BOT_E2E_PINS are required too")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", E2E_NETWORK):
        raise ValueError("BOT_E2E_NETWORK names the state file: letters, digits, '.', '_', '-'")
    import e2e_bridge
    from irc_olm import store
    from irc_olm.engine import Engine
    from irc_olm.pacing import FakeLag, PacedSender, Pacer

    state = E2E_STATE
    if not state:
        base = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
                            "claude-ircbot")
        os.makedirs(base, mode=0o700, exist_ok=True)
        state = os.path.join(base, f"{E2E_NETWORK}.e2e.json")
    pins = e2e_bridge.PinsFile(E2E_PINS, emit)
    engine = Engine(state, store.load_key(E2E_KEY), network=E2E_NETWORK,
                    nick=NICK, ident=IDENT, pins=pins.load())
    # Never past bahamut's flood bank for the connection, and 3 lines per 6 s
    # per target for the receiver's CTCP flood limit (irc-olm pacing).
    fakelag = FakeLag()
    # What the pacer loses after queueing (a challenge too late to answer, a
    # failed write) goes to the bridge, built right below: nothing is queued
    # before it exists.
    pacer = PacedSender(_paced_write, Pacer(fakelag=fakelag),
                        on_lost=lambda nick, msgid, reason: bridge.on_lost(nick, msgid, reason))
    bridge = e2e_bridge.Bridge(
        engine, own_nick=NICK, pins=pins, submit=pacer.submit_all, room=pacer.room, emit=emit,
        log=log, trust_check=trust_check, is_listed=is_trust_listed,
        on_confirmed=on_trusted_line)
    emit("E2E_READY", f"KEY={engine.fingerprint}", f"NETWORK={E2E_NETWORK}")


def e2e_ticker_loop():
    """Once a second: pins reload, challenge expiry, engine upkeep."""
    while True:
        time.sleep(1)
        try:
            bridge.tick()
        except Exception as e:
            emit("ERROR", "e2e-tick", repr(e))


def check_config():
    """Fail fast, with a plain message on stderr, on configuration that has no
    default or does not parse. Called by main(), never at import."""
    global _TZ
    missing = [k for k, v in (("IRC_HOST", HOST), ("IRC_NICK", NICK)) if not v]
    if missing:
        sys.exit(f"bot.py: {' and '.join(missing)} must be set in the environment "
                 "(see README, Configuration)")
    if TZ_NAME:
        try:
            _TZ = ZoneInfo(TZ_NAME)
        except (ZoneInfoNotFoundError, ValueError):
            sys.exit(f"bot.py: BOT_TZ={TZ_NAME!r} is not a known time zone (e.g. Europe/Rome)")
    try:
        ranges = parse_idle(IDLE_SPEC)
    except ValueError as e:
        sys.exit(f"bot.py: {e}")
    with idle_lock:
        IDLE_RANGES.update(ranges)
        idle_state.update({c: None for c in ranges})


def main():
    global sock
    check_config()
    load_trust()
    load_dm_allow()
    _start_e2e()   # before connecting: the 005 that sizes E2E lines comes early
    ctx = ssl.create_default_context()
    if not TLS_VERIFY:
        # Order matters: check_hostname must go first, CERT_NONE with hostname
        # checking still enabled raises ValueError.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection(
        (HOST, PORT), timeout=30, source_address=((BIND, 0) if BIND else None)
    )
    # Belt-and-suspenders against silent TCP death (NAT/ISP drops):
    # kernel keepalive probes + app-level recv timeout. Server PINGs us
    # ~3-5min; 420s timeout on recv surfaces a stalled socket as
    # socket.timeout -> reader_loop returns -> systemd Restart=always.
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    if hasattr(socket, "TCP_KEEPIDLE"):
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 15)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)
    sock = ctx.wrap_socket(raw, server_hostname=HOST)
    sock.settimeout(420)
    emit("TLS_OK", HOST, PORT, "verify" if TLS_VERIFY else "NOVERIFY")
    if SASL:
        # Holds registration open until CAP END (sent on 903 only).
        send_raw("CAP REQ :sasl")
    send_raw(f"NICK {NICK}")
    send_raw(f"USER {IDENT} 0 * :{REAL}")

    threading.Thread(target=writer_loop, daemon=True).start()
    threading.Thread(target=idle_ticker_loop, daemon=True).start()
    threading.Thread(target=converge_loop, daemon=True).start()
    if bridge:
        threading.Thread(target=e2e_ticker_loop, daemon=True).start()
    reader_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        emit("FATAL", repr(e))
        sys.exit(1)
