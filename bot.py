#!/usr/bin/env python3
"""
Minimal IRC bot bridging a claude-code conversation <-> Azzurra IRC.

- TLS connect to irc.azzurra.chat:6697 as vjt-claude.
- Every line logged to ./bot.log (next to this script).
- Selected events emitted to stdout (one line each) so a Monitor sees them.
- Reads commands from FIFO ./bot.send:
    SAY <target> <text>     -> splits into safe PRIVMSGs
    ACT <target> <text>     -> CTCP ACTION
    NOTICE <target> <text>
    RAW <irc line>          -> send as-is
    JOIN <chan> / PART <chan> / WHOIS <nick> / QUIT [reason]
"""
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
from zoneinfo import ZoneInfo

# Network + file paths are env-overridable so a SECOND instance (e.g. Libera.
# Chat) runs the SAME code with its own nick / FIFO / logs / trust / startup.
# Every default reproduces the original Azzurra single-instance behaviour, so
# the Azzurra unit (which sets none of these vars) is unchanged.
HERE = os.path.dirname(os.path.abspath(__file__))


def _cfg(key, default):
    return os.environ.get(key, default)


HOST = _cfg("IRC_HOST", "irc.azzurra.chat")
PORT = int(_cfg("IRC_PORT", "6697"))
NICK = _cfg("IRC_NICK", "vjt-claude")
IDENT = _cfg("IRC_IDENT", "claude")
REAL = _cfg("IRC_REAL", "github.com/vjt/claude-ircbot")

LOG = _cfg("BOT_LOG", os.path.join(HERE, "bot.log"))
FIFO = _cfg("BOT_FIFO", os.path.join(HERE, "bot.send"))
TRUST_FILE = _cfg("BOT_TRUST", os.path.join(HERE, "bot.trust"))
ENV_FILE = _cfg("BOT_ENV", os.path.join(HERE, ".env"))
STARTUP_FILE = _cfg("BOT_STARTUP", os.path.join(HERE, "bot.startup"))

# Wall-clock timestamps in Europe/Rome (CET/CEST, DST-aware) instead of the
# host's UTC. bot.log + the event stream were UTC until 2026-07-02, when vjt
# asked for Italian local time. Explicit zoneinfo so it stays correct whatever
# the host TZ is (pi5 runs UTC).
_TZ = ZoneInfo("Europe/Rome")


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
# services, reclaim it, then IDENTIFY. (vjt 2026-07-20: "fatti un ghost on connect".)
registered = False      # True once we've received 001 (registration complete)
ghost_needed = False    # True while we hold a temp nick and still want NICK back
tmp_nick = None         # the throwaway nick we fell back to

# Trust state: nick must be in trust_rules, host must match the nick's glob,
# AND we must have received RPL_WHOISREGNICK (307) confirming the nick is
# registered+identified to services. Cache reset on PART/QUIT/NICK.
trust_rules = []  # list of (nick_lower, host_glob)
verified = set()  # lowercase nicks confirmed via 307
whois_pending = set()  # lowercase nicks we WHOIS'd, awaiting 307/318

# IRC line limit is 512 incl CRLF. Body safe ~400.
MAX_BODY = 400

sock = None
send_lock = threading.Lock()

# Idle-tick config: per-channel random cooldown (seconds) after the last
# HUMAN PRIVMSG. When elapsed, bot emits `IDLE <chan>` exactly once, then
# disarms. Next human PRIVMSG re-arms with a fresh random cooldown.
# Our own outgoing messages do NOT reset the timer.
IDLE_RANGES = {
    "#sniffo": (5 * 60, 20 * 60),
    "#olografix": (10 * 60, 40 * 60),
    "#it-opers": (30 * 60, 90 * 60),
}
# state[chan_lower] = fire_at (monotonic) or None if disarmed
idle_state: dict[str, float | None] = {c.lower(): None for c in IDLE_RANGES}
idle_lock = threading.Lock()

# Rolling kick history per channel (monotonic timestamps). If > KICK_MAX in KICK_WINDOW, give up rejoin.
kick_history: dict[str, list[float]] = {}
kick_lock = threading.Lock()
KICK_WINDOW = 60.0
KICK_MAX = 3


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


def emit(kind, *parts):
    # [HH:MM] prefix gives the Monitor/Claude a real time anchor per event
    # instead of extrapolating elapsed time from turn order (vjt 2026-06-07).
    # No seconds by request. start-monitor.sh's grep tolerates this prefix.
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


def trust_reset(nick, reason):
    n = nick.lower()
    if n in verified or n in whois_pending:
        verified.discard(n)
        whois_pending.discard(n)
        emit("TRUST_RESET", nick, reason)


def _mask_secrets(line):
    if NICKSERV_PASS and NICKSERV_PASS in line:
        return line.replace(NICKSERV_PASS, "********")
    return line


def log(direction, line):
    try:
        with open(LOG, "a") as f:
            f.write(f"{_now('%H:%M:%S')} {direction} {_mask_secrets(line)}\n")
    except Exception:
        pass


def send_raw(s):
    log(">", s)
    with send_lock:
        try:
            sock.sendall((s + "\r\n").encode("utf-8", errors="replace"))
        except Exception as e:
            emit("ERROR", "send-fail", repr(e))


def split_say(target, text):
    """Send text as one or more PRIVMSGs, preserving whitespace verbatim.

    Lines that fit in MAX_BODY go out untouched, so ascii art keeps its
    indentation and internal spacing (#86). Longer lines wrap at the last
    space before the limit WITHOUT stripping or collapsing spaces; a single
    oversized word is hard-split on a utf-8 char boundary.
    """
    b = text.encode("utf-8")
    if len(b) <= MAX_BODY:
        send_raw(f"PRIVMSG {target} :{text}")
        return
    while b:
        if len(b) <= MAX_BODY:
            send_raw(f"PRIVMSG {target} :{b.decode('utf-8', errors='ignore')}")
            return
        cut = b.rfind(b" ", 1, MAX_BODY + 1)
        if cut <= 0:
            cut = MAX_BODY
            while cut > 1 and (b[cut] & 0xC0) == 0x80:
                cut -= 1
        else:
            cut += 1  # keep the breaking space on this chunk, don't strip it
        send_raw(f"PRIVMSG {target} :{b[:cut].decode('utf-8', errors='ignore')}")
        b = b[cut:]


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
    # so we don't depend on the exact "has been ghosted" NOTICE text differing
    # between Azzurra (suxserv) and Libera (atheme).
    time.sleep(4)
    if ghost_needed:
        send_raw(f"NICK {NICK}")
        emit("NS_GHOST_RECLAIM", NICK)


def handle_server_line(line):
    global registered, ghost_needed, tmp_nick
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
    # JOIN/NICK the way it was when a *.diostronzo.org flooder spoofed nick
    # `vjt_` and only the raw bot.log (not the event stream) revealed it wasn't
    # vjt's trust host *.openssl.it (vjt 2026-07-02).
    host = prefix.split("@", 1)[1] if prefix and "@" in prefix else ""
    rest = rest or ""

    if cmd == "001":
        registered = True
        emit("CONNECTED", NICK)
        for tn, _ in trust_rules:
            whois_pending.add(tn)
            send_raw(f"WHOIS {tn}")
            emit("WHOIS_FIRED", tn)
        if NICKSERV_PASS:
            if ghost_needed:
                # Our real nick is held (by our own ghost after a line drop):
                # kill it from services, then reclaim + IDENTIFY (see below).
                send_raw(f"PRIVMSG NickServ :GHOST {NICK} {NICKSERV_PASS}")
                emit("NS_GHOST_SENT", NICK)
                threading.Thread(target=_reclaim_after_ghost, daemon=True).start()
            else:
                send_raw(f"PRIVMSG NickServ :IDENTIFY {NICKSERV_PASS}")
                emit("NS_IDENTIFY_SENT")
        else:
            fire_startup("no-nickserv-pass")
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
            ghost_needed = True
            emit("NICK_FALLBACK", tmp_nick)
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
        return
    if cmd == "NOTICE":
        parts = rest.split(" :", 1)
        tgt = parts[0]
        msg = parts[1] if len(parts) > 1 else ""
        if nick.lower() == "nickserv" and NICKSERV_PASS:
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
        if body.startswith("\x01") and body.endswith("\x01"):
            ctcp = body.strip("\x01")
            emit("CTCP", f"FROM={nick}", f"HOST={host}", f"TO={target}", f"BODY={ctcp}")
            if ctcp.upper() == "VERSION":
                send_raw(f"NOTICE {nick} :\x01VERSION claude-code PoC\x01")
            elif ctcp.upper().startswith("PING"):
                send_raw(f"NOTICE {nick} :\x01{ctcp}\x01")
            return
        trusted, reason = trust_check(nick, host)
        trust = "TRUSTED" if trusted else "UNTRUSTED"
        emit("MSG", trust, f"FROM={nick}", f"HOST={host}", f"TO={target}", f"BODY={body}")
        if is_trust_listed(nick) and not trusted:
            emit("TRUST_DENIED", nick, host, reason)
        # Re-arm the idle timer only on HUMAN PRIVMSG to a tracked channel.
        # Our own outgoing messages don't reach this path.
        if target.startswith("#") and nick.lower() != NICK.lower():
            _arm_idle(target)
        return
    if cmd == "JOIN":
        emit("JOIN", nick, f"HOST={host}", rest.lstrip(":"))
        return
    if cmd == "PART":
        emit("PART", nick, f"HOST={host}", rest)
        trust_reset(nick, "part")
        return
    if cmd == "QUIT":
        emit("QUIT", nick, f"HOST={host}", rest)
        trust_reset(nick, "quit")
        return
    if cmd == "NICK":
        new_nick = rest.lstrip(":")
        emit("NICK_CHANGE", nick, f"HOST={host}", new_nick)
        trust_reset(nick, "nick-change")
        if ghost_needed and new_nick.lower() == NICK.lower():
            # We just reclaimed the real nick after ghosting the stale one.
            # Now IDENTIFY on it (NickServ's "identified" NOTICE then fires the
            # channel-join startup, so JOINs happen on the right nick).
            ghost_needed = False
            if NICKSERV_PASS:
                send_raw(f"PRIVMSG NickServ :IDENTIFY {NICKSERV_PASS}")
                emit("NS_IDENTIFY_SENT")
        return
    if cmd == "ERROR":
        emit("SERVER_ERROR", rest)
        return


def reader_loop():
    buf = b""
    while True:
        try:
            data = sock.recv(8192)
        except Exception as e:
            emit("ERROR", "recv-fail", repr(e))
            return
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
                if not line:
                    continue
                try:
                    process_cmd(line)
                except Exception as e:
                    emit("CMD_ERROR", "cmd-fail", repr(e), line[:80])


def process_cmd(line):
    if " " in line:
        verb, rest = line.split(" ", 1)
    else:
        verb, rest = line, ""
    verb = verb.upper()
    if verb == "SAY":
        if " " not in rest:
            emit("CMD_ERROR", "SAY needs <target> <text>", repr(rest))
            return
        target, text = rest.split(" ", 1)
        split_say(target, text)
    elif verb == "ACT":
        if " " not in rest:
            emit("CMD_ERROR", "ACT needs <target> <text>", repr(rest))
            return
        target, text = rest.split(" ", 1)
        send_raw(f"PRIVMSG {target} :\x01ACTION {text}\x01")
    elif verb == "NOTICE":
        if " " not in rest:
            emit("CMD_ERROR", "NOTICE needs <target> <text>", repr(rest))
            return
        target, text = rest.split(" ", 1)
        send_raw(f"NOTICE {target} :{text}")
    elif verb == "JOIN":
        send_raw(f"JOIN {rest}")
    elif verb == "PART":
        send_raw(f"PART {rest}")
    elif verb == "WHOIS":
        send_raw(f"WHOIS {rest}")
    elif verb == "QUIT":
        send_raw(f"QUIT :{rest or 'bye'}")
    elif verb == "RAW":
        send_raw(rest)
    else:
        emit("CMD_ERROR", "unknown-verb", repr(verb),
             "valid: SAY ACT NOTICE JOIN PART WHOIS QUIT RAW")


def main():
    global sock
    load_trust()
    ctx = ssl.create_default_context()
    raw = socket.create_connection((HOST, PORT), timeout=30)
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
    emit("TLS_OK", HOST, PORT)
    send_raw(f"NICK {NICK}")
    send_raw(f"USER {IDENT} 0 * :{REAL}")

    threading.Thread(target=writer_loop, daemon=True).start()
    threading.Thread(target=idle_ticker_loop, daemon=True).start()
    reader_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        emit("FATAL", repr(e))
        sys.exit(1)
