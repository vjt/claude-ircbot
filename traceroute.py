#!/usr/bin/env python3
"""Unprivileged ICMP traceroute (SOCK_DGRAM ping socket + IP_RECVERR).

traceroute(8) is not installed on this host and installing it needs root, so
the hops come from the kernel error queue instead: one echo per TTL, the
offending router falls out of SO_EE_OFFENDER. Needs no privileges as long as
`net.ipv4.ping_group_range` covers our gid — it does on this Pi.

    ./traceroute.py 185.232.44.16 example.org
    HOPS=30 QUERIES=3 ./traceroute.py 8.8.8.8
    MODE=udp ./traceroute.py 185.232.44.34   # for hosts that drop ICMP echo
    MODE=tcp PORT=443 ./traceroute.py 1.1.1.1

IPv4 only: the ping socket, the TTL sockopt and the offender parsing are all
AF_INET here. Publishing output? Scrub the first hops, they are the LAN.
"""
import errno
import os
import select
import socket
import struct
import sys
import time

SO_EE_OFFENDER_OFF = 16  # sizeof(struct sock_extended_err)
IP_RECVERR = getattr(socket, "IP_RECVERR", 11)  # <linux/in.h>, absent in py3.13


def probe(dest, ttl, timeout=1.0, seq=0, mode="icmp"):
    # Three ways to knock, one way to listen: whatever the probe, the routers
    # answer with ICMP time-exceeded and the kernel files it on this socket's
    # error queue. Only the knock and the "are we there yet" test differ.
    #   icmp — echo request; the destination answers with an echo reply.
    #   udp  — datagram to a high port; the destination answers unreachable.
    #   tcp  — SYN to PORT (443 by default); survives filters that drop the
    #          other two, and the destination answers by completing the
    #          handshake (or refusing it).
    if mode == "udp":
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        port = 33434 + ttl
    elif mode == "tcp":
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        port = PORT
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
        port = 0
    s.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
    s.setsockopt(socket.IPPROTO_IP, IP_RECVERR, 1)
    s.setblocking(False)
    t0 = time.time()
    if mode == "tcp":
        # The SYN goes out with connect() itself; there is nothing to send().
        try:
            s.connect((dest, port))
        except BlockingIOError:
            pass
    else:
        # connect() first: the kernel only queues the ICMP error on the
        # socket's error queue for a connected socket, so a bare sendto()
        # yields all-stars.
        s.connect((dest, port))
        pkt = (
            b"vjt-claude"
            if mode == "udp"
            else struct.pack("!BBHHH", 8, 0, 0, 0, seq) + b"vjt-claude"
        )
        s.send(pkt)
    deadline = t0 + timeout
    while time.time() < deadline:
        # A completed TCP handshake shows up as WRITABLE, never readable.
        w = [s] if mode == "tcp" else []
        r, w, e = select.select([s], w, [s], deadline - time.time())
        r = r or w
        if not (r or e):
            break
        rtt = (time.time() - t0) * 1000
        # A TTL-exceeded makes the socket READABLE, not exceptional, so always
        # drain the error queue first — the plain read would only raise.
        try:
            _, anc, _, _ = s.recvmsg(512, 1024, socket.MSG_ERRQUEUE)
        except OSError:
            anc = []
        for _lvl, _typ, data in anc:
            off = data[SO_EE_OFFENDER_OFF:SO_EE_OFFENDER_OFF + 8]
            if len(off) == 8 and struct.unpack("!H", off[0:2])[0] == 0x0200:
                # ee_type sits at byte 5 of struct sock_extended_err. In UDP
                # mode ICMP_DEST_UNREACH (3) IS the destination answering —
                # that's the walk's end, not another hop along the way.
                ee_type = data[5]
                s.close()
                return socket.inet_ntoa(off[4:8]), rtt, mode != "icmp" and ee_type == 3
        if mode == "tcp":
            # Writable with no pending error means the handshake completed:
            # that is the destination itself, and the walk ends here.
            err = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            s.close()
            return (dest, rtt, True) if err in (0, errno.ECONNREFUSED) else (None, None, False)
        try:
            _data, addr = s.recvfrom(512)
        except OSError:
            continue
        s.close()
        return addr[0], (time.time() - t0) * 1000, True
    s.close()
    return None, None, False


HOPS = int(os.environ.get("HOPS", 16))
QUERIES = int(os.environ.get("QUERIES", 2))
MODE = os.environ.get("MODE", "icmp").lower()
PORT = int(os.environ.get("PORT", 443))  # tcp mode only


def trace(dest, maxhops=HOPS, queries=QUERIES, mode=MODE):
    ip = socket.gethostbyname(dest)
    how = {"udp": "UDP", "tcp": f"TCP SYN port {PORT}"}.get(mode, "ICMP echo")
    print(f"traceroute to {dest} ({ip}), {maxhops} hops max, {how}")
    for ttl in range(1, maxhops + 1):
        hops = []
        done = False
        for q in range(queries):
            hop, rtt, final = probe(ip, ttl, seq=ttl * 10 + q, mode=mode)
            hops.append((hop, rtt))
            done = done or final
        who = next((h for h, _ in hops if h), None)
        if who is None:
            print(f"{ttl:2d}  " + " ".join("*" * queries))
            continue
        try:
            name = socket.gethostbyaddr(who)[0]
        except OSError:
            name = who
        times = "  ".join(f"{r:.3f} ms" for h, r in hops if h and r is not None)
        print(f"{ttl:2d}  {name} ({who})  {times}")
        if done:
            break


if __name__ == "__main__":
    for target in sys.argv[1:]:
        trace(target)
        print()
