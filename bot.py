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
import re
import socket
import ssl
import sys
import threading
import time

HOST = "irc.azzurra.chat"
PORT = 6697
NICK = "vjt-claude"
IDENT = "claude"
REAL = "Claude Code PoC (vjt)"

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "bot.log")
FIFO = os.path.join(HERE, "bot.send")
TRUST_FILE = os.path.join(HERE, "bot.trust")
ENV_FILE = os.path.join(HERE, ".env")
STARTUP_FILE = os.path.join(HERE, "bot.startup")


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


def emit(kind, *parts):
    print(kind + " " + " ".join(str(p) for p in parts), flush=True)


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
            f.write(f"{time.strftime('%H:%M:%S')} {direction} {_mask_secrets(line)}\n")
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
    words = text.split(" ")
    cur = ""
    out = []
    for w in words:
        candidate = (cur + " " + w).strip() if cur else w
        if len(candidate.encode("utf-8")) > MAX_BODY:
            if cur:
                out.append(cur)
                cur = w
            else:
                b = w.encode("utf-8")
                while len(b) > MAX_BODY:
                    out.append(b[:MAX_BODY].decode("utf-8", errors="ignore"))
                    b = b[MAX_BODY:]
                cur = b.decode("utf-8", errors="ignore")
        else:
            cur = candidate
    if cur:
        out.append(cur)
    for chunk in out:
        send_raw(f"PRIVMSG {target} :{chunk}")


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


def handle_server_line(line):
    log("<", line)
    if line.startswith("PING "):
        send_raw("PONG " + line[5:])
        return

    m = re.match(r"^(?::(\S+)\s+)?(\S+)(?:\s+(.*))?$", line)
    if not m:
        return
    prefix, cmd, rest = m.groups()
    nick = prefix.split("!")[0] if prefix else ""
    rest = rest or ""

    if cmd == "001":
        emit("CONNECTED", NICK)
        for tn, _ in trust_rules:
            whois_pending.add(tn)
            send_raw(f"WHOIS {tn}")
            emit("WHOIS_FIRED", tn)
        if NICKSERV_PASS:
            send_raw(f"PRIVMSG NickServ :IDENTIFY {NICKSERV_PASS}")
            emit("NS_IDENTIFY_SENT")
        else:
            fire_startup("no-nickserv-pass")
        return
    if cmd in ("433", "432", "437"):
        emit("NICK_ERROR", cmd, rest)
        return
    if cmd in ("464", "465"):
        emit("AUTH_ERROR", cmd, rest)
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
            emit("NOTICE", nick, tgt, msg)
        return
    if cmd == "INVITE":
        parts = rest.split(" :", 1)
        target_chan = parts[1] if len(parts) == 2 else rest.split()[-1]
        host = prefix.split("@", 1)[1] if prefix and "@" in prefix else ""
        trusted, reason = trust_check(nick, host)
        emit("INVITE", nick, target_chan, "trusted" if trusted else f"untrusted:{reason}")
        if trusted:
            send_raw(f"JOIN {target_chan}")
        return
    if cmd == "KICK":
        emit("KICK", nick, rest)
        return
    if cmd == "PRIVMSG":
        parts = rest.split(" :", 1)
        target = parts[0].strip()
        body = parts[1] if len(parts) > 1 else ""
        if body.startswith("\x01") and body.endswith("\x01"):
            ctcp = body.strip("\x01")
            emit("CTCP", nick, target, ctcp)
            if ctcp.upper() == "VERSION":
                send_raw(f"NOTICE {nick} :\x01VERSION claude-code PoC\x01")
            elif ctcp.upper().startswith("PING"):
                send_raw(f"NOTICE {nick} :\x01{ctcp}\x01")
            return
        host = prefix.split("@", 1)[1] if prefix and "@" in prefix else ""
        trusted, reason = trust_check(nick, host)
        trust = nick if trusted else "other"
        emit("MSG", trust, nick, target, body)
        if is_trust_listed(nick) and not trusted:
            emit("TRUST_DENIED", nick, host, reason)
        return
    if cmd == "JOIN":
        emit("JOIN", nick, rest.lstrip(":"))
        return
    if cmd == "PART":
        emit("PART", nick, rest)
        trust_reset(nick, "part")
        return
    if cmd == "QUIT":
        emit("QUIT", nick, rest)
        trust_reset(nick, "quit")
        return
    if cmd == "NICK":
        new_nick = rest.lstrip(":")
        emit("NICK_CHANGE", nick, new_nick)
        trust_reset(nick, "nick-change")
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
                    emit("ERROR", "cmd-fail", repr(e), line[:80])


def process_cmd(line):
    if " " in line:
        verb, rest = line.split(" ", 1)
    else:
        verb, rest = line, ""
    verb = verb.upper()
    if verb == "SAY":
        if " " not in rest:
            emit("ERROR", "SAY needs <target> <text>")
            return
        target, text = rest.split(" ", 1)
        split_say(target, text)
    elif verb == "ACT":
        target, text = rest.split(" ", 1)
        send_raw(f"PRIVMSG {target} :\x01ACTION {text}\x01")
    elif verb == "NOTICE":
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
        emit("ERROR", "unknown-cmd", verb)


def main():
    global sock
    load_trust()
    ctx = ssl.create_default_context()
    raw = socket.create_connection((HOST, PORT), timeout=30)
    sock = ctx.wrap_socket(raw, server_hostname=HOST)
    sock.settimeout(None)
    emit("TLS_OK", HOST, PORT)
    send_raw(f"NICK {NICK}")
    send_raw(f"USER {IDENT} 0 * :{REAL}")

    threading.Thread(target=writer_loop, daemon=True).start()
    reader_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        emit("FATAL", repr(e))
        sys.exit(1)
