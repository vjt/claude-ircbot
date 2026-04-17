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
TRUSTED = "vjt"

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "bot.log")
FIFO = os.path.join(HERE, "bot.send")

# IRC line limit is 512 incl CRLF. Body safe ~400.
MAX_BODY = 400

sock = None
send_lock = threading.Lock()


def emit(kind, *parts):
    print(kind + " " + " ".join(str(p) for p in parts), flush=True)


def log(direction, line):
    try:
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {direction} {line}\n")
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
        return
    if cmd in ("433", "432", "437"):
        emit("NICK_ERROR", cmd, rest)
        return
    if cmd in ("464", "465"):
        emit("AUTH_ERROR", cmd, rest)
        return
    if cmd == "NOTICE":
        parts = rest.split(" :", 1)
        tgt = parts[0]
        msg = parts[1] if len(parts) > 1 else ""
        if nick and nick.lower() != NICK.lower():
            emit("NOTICE", nick, tgt, msg)
        return
    if cmd == "INVITE":
        parts = rest.split(" :", 1)
        target_chan = parts[1] if len(parts) == 2 else rest.split()[-1]
        emit("INVITE", nick, target_chan)
        if nick == TRUSTED:
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
        trust = "vjt" if nick == TRUSTED else "other"
        emit("MSG", trust, nick, target, body)
        return
    if cmd == "JOIN":
        emit("JOIN", nick, rest.lstrip(":"))
        return
    if cmd == "PART":
        emit("PART", nick, rest)
        return
    if cmd == "QUIT":
        emit("QUIT", nick, rest)
        return
    if cmd == "NICK":
        emit("NICK_CHANGE", nick, rest.lstrip(":"))
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
