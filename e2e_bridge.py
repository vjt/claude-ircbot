"""E2E encrypted queries and order challenges, bot side (issue #4).

The crypto, framing and pacing live in the shared irc-olm package
(github.com/vjt/irc-olm); the protocol is grappa-irc issue 2430. This module
maps it onto bot.py: what reaches the event stream, under which trust verdict,
and when an order needs a CHAL first.

Trust, per vjt's rulings on #4:

  * A user with NO verified key keeps today's rules: layer 1 alone (trust list +
    host glob + 307/330). Encrypting does not change the verdict, it adds
    confidentiality. That is what makes this safe to ship before anyone has a
    verified key.
  * Once a user HAS a verified key, plaintext alone no longer counts (no
    downgrade). In the encrypted query the session itself authenticates.
    Outside it, a line addressed to the bot (or a plaintext PM) is held, a CHAL
    goes out over the verified session, and the line is released as TRUSTED
    only on a matching ACK within 15 s. Anything else from that user in plain
    text is UNTRUSTED.
  * A key that is not the verified one pinned to the nick is an alarm, and what
    it says never reaches the stream.

Log hygiene: decrypted text appears ONLY as the BODY of the MSG event on stdout
(ruling 3). bot.log gets the ciphertext lines plus `E2E MSG from X (n bytes)`.

TODO(#4, vjt reconsidering): how a key becomes verified on the bot. SAS on the
bot is on hold; the alternative is a pre-pinned key in the bot's
configuration. Until he rules, nothing calls Engine.verify() here, so no key
is verified and every user stays on layer 1.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

from irc_olm.engine import (AckOk, AckRejected, ChallengeExpired, Dropped, KeyAlarm,
                            NotVerifiedError, PeerPinned, Received, RekeyThrottled,
                            Rekeying, SendFailed, Undecryptable)

# How long a conversation stays "encrypted" for replies after the last
# encrypted DM, so a SAY to that nick goes out encrypted too.
REPLY_MODE_TTL = 3600.0


@dataclass
class _Held:
    target: str
    nick: str
    host: str
    body: str
    is_pm: bool


def _fold(nick: str) -> str:
    return nick.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                                        "abcdefghijklmnopqrstuvwxyz"))


class Bridge:
    def __init__(self, engine, *, own_nick, submit, emit, log, trust_check, is_listed,
                 hold_dm, clock=time.monotonic):
        self.engine = engine
        self.own_nick = own_nick
        self._submit = submit            # (outbound pairs) -> None, paced
        self._emit = emit
        self._log = log
        self._trust_check = trust_check
        self._is_listed = is_listed
        self._hold_dm = hold_dm          # (nick, host, body, extra_fields) -> depth
        self._clock = clock
        self._lock = threading.RLock()
        self.network_token = ""          # 005 NETWORK=, the CHAL `network` field
        self._held: dict[str, _Held] = {}
        self._reply_mode: dict[str, float] = {}
        self._hosts: dict[str, str] = {}
        self._addressed = re.compile(
            r"(?<![A-Za-z0-9\[\]\\`^{}|_-])" + re.escape(own_nick) + r"(?![A-Za-z0-9\[\]\\`^{}|_-])",
            re.IGNORECASE)

    # --- server facts ------------------------------------------------------------

    def on_isupport(self, tokens: list[str]) -> None:
        for tok in tokens:
            key, _, value = tok.partition("=")
            if key == "NETWORK" and value:
                self.network_token = value
            elif key == "LINELEN" and value.isdigit():
                self.engine.linelen = int(value)

    def on_own_mask(self, mask: str) -> None:
        self.engine.set_own_mask(mask)

    # --- inbound E2E ---------------------------------------------------------------

    def on_e2e_line(self, nick: str, host: str, target: str, body: str) -> None:
        """A PRIVMSG body framing.is_e2e() recognised. Never a plaintext path."""
        if target.startswith(("#", "&")):
            self._emit("E2E_DROPPED", f"FROM={nick}", f"HOST={host}", f"TO={target}",
                       "REASON=channel")
            return
        if not self._is_listed(nick):
            # Strangers get nothing: no keys, no rebound (same gate as DMs).
            self._emit("E2E_BLOCKED", f"FROM={nick}", f"HOST={host}", f"LEN={len(body)}")
            return
        trusted, reason = self._trust_check(nick, host)
        if not trusted and reason == "host-mismatch":
            self._emit("E2E_BLOCKED", f"FROM={nick}", f"HOST={host}", f"LEN={len(body)}",
                       reason)
            return
        with self._lock:
            self._hosts[_fold(nick)] = host
            self.dispatch(self.engine.receive(nick, body))

    # --- plaintext orders ------------------------------------------------------

    def gate_plaintext(self, nick: str, host: str, prefix: str, target: str, body: str,
                       trusted: bool) -> str | None:
        """Called for every plaintext PRIVMSG that passed layer 1 (or not).

        Returns None when today's verdict stands, "held" when the line waits for
        an ACK (the bridge emits it later), "unchallenged" when it must be shown
        UNTRUSTED because its author has a verified key and plain text alone no
        longer counts."""
        is_pm = not target.startswith(("#", "&"))
        with self._lock:
            if is_pm:
                self._reply_mode.pop(_fold(nick), None)
            if not trusted:
                return None
            pinned = self.engine.pinned(nick)
            if pinned is None or not pinned.verified:
                return None
            if not (is_pm or self._addressed.search(body)):
                return "unchallenged"
            try:
                ch, res = self.engine.challenge(nick, network=self.network_token, channel=target,
                                                sender_mask=prefix, line=body.encode("utf-8"))
            except NotVerifiedError:
                return None
            self._held[ch.nonce] = _Held(target, nick, host, body, is_pm)
            self.dispatch(res)
            self._emit("ORDER_HELD", f"FROM={nick}", f"HOST={host}", f"TO={target}",
                       f"LEN={len(body)}")
            return "held"

    # --- outbound ------------------------------------------------------------------

    def wants_encryption(self, nick: str) -> bool:
        with self._lock:
            at = self._reply_mode.get(_fold(nick))
            return at is not None and self._clock() - at < REPLY_MODE_TTL

    def send_text(self, nick: str, text: str, action: bool = False) -> None:
        with self._lock:
            self.dispatch(self.engine.send(nick, {"t": "action" if action else "text",
                                                  "body": text}))
            self._log(">", f"E2E {'ACT' if action else 'MSG'} to {nick} ({len(text.encode())} bytes)")

    # --- time ------------------------------------------------------------------------

    def tick(self) -> None:
        with self._lock:
            self.dispatch(self.engine.tick())

    # --- events ------------------------------------------------------------------------

    def dispatch(self, result) -> None:
        if result.outbound:
            self._submit(result.outbound)
        for e in result.events:
            self._on_event(e)

    def _on_event(self, e) -> None:
        if isinstance(e, Received):
            self._on_received(e)
        elif isinstance(e, AckOk):
            self._release(e.challenge.nonce, confirmed=True)
        elif isinstance(e, ChallengeExpired):
            self._release(e.challenge.nonce, confirmed=False, why="no-ack")
        elif isinstance(e, AckRejected):
            self._emit("ORDER_ACK_REJECTED", f"FROM={e.nick}", f"REASON={e.reason}")
        elif isinstance(e, KeyAlarm):
            self._emit("E2E_KEY_ALARM", f"FROM={e.nick}", f"PINNED={e.pinned.ed25519}",
                       f"VERIFIED={int(e.pinned.verified)}", f"PRESENTED={e.presented.ed25519}")
        elif isinstance(e, PeerPinned):
            self._emit("E2E_PINNED", f"FROM={e.nick}", f"KEY={e.peer.ed25519}")
        elif isinstance(e, Undecryptable):
            self._emit("E2E_UNDECRYPTABLE", f"FROM={e.nick}", f"REASON={e.reason}")
        elif isinstance(e, (Rekeying, RekeyThrottled)):
            self._emit("E2E_REKEY" if isinstance(e, Rekeying) else "E2E_REKEY_THROTTLED",
                       f"FROM={e.nick}")
        elif isinstance(e, SendFailed):
            self._emit("E2E_SEND_FAILED", f"TO={e.nick}", f"REASON={e.reason}", f"LOST={e.lost}")
        elif isinstance(e, Dropped):
            self._emit("E2E_DROPPED", f"FROM={e.nick}", f"REASON={e.reason}")

    def _on_received(self, e: Received) -> None:
        kind = e.inner.get("t")
        body = e.inner.get("body")
        if kind not in ("text", "action") or not isinstance(body, str):
            return  # chal / sas.* / unknown: nothing for the stream
        nick, host = e.nick, self._hosts.get(_fold(e.nick), "")
        self._log("*", f"E2E MSG from {nick} ({len(body.encode())} bytes)")
        pinned = self.engine.pinned(nick)
        if pinned and pinned.verified and pinned.ed25519 != e.peer.ed25519:
            self._emit("E2E_DROPPED", f"FROM={nick}", f"HOST={host}", "REASON=not-the-verified-key",
                       f"LEN={len(body)}")
            return
        verified = bool(pinned and pinned.verified)
        extra = ["ENCRYPTED=1", f"VERIFIED={int(verified)}"] + (["ACTION=1"] if kind == "action" else [])
        trusted, reason = self._trust_check(nick, host)
        if not trusted:
            if reason == "pending-whois":
                depth = self._hold_dm(nick, host, body, tuple(extra))
                self._emit("DM_HELD", f"FROM={nick}", f"HOST={host}", f"LEN={len(body)}",
                           f"QUEUED={depth}", "ENCRYPTED=1")
            else:
                self._emit("DM_BLOCKED", f"FROM={nick}", f"HOST={host}", f"LEN={len(body)}",
                           reason, "ENCRYPTED=1")
            return
        self._reply_mode[_fold(nick)] = self._clock()
        self._emit("MSG", "TRUSTED", *extra, f"FROM={nick}", f"HOST={host}",
                   f"TO={self.own_nick}", f"BODY={body}")

    def _release(self, nonce: str, *, confirmed: bool, why: str = "") -> None:
        h = self._held.pop(nonce, None)
        if h is None:
            return
        if confirmed:
            self._emit("MSG", "TRUSTED", "CONFIRMED=1", f"FROM={h.nick}", f"HOST={h.host}",
                       f"TO={h.target}", f"BODY={h.body}")
        elif h.is_pm:
            self._emit("DM_DROPPED", f"FROM={h.nick}", f"HOST={h.host}", f"LEN={len(h.body)}",
                       why)
        else:
            self._emit("MSG", "UNTRUSTED", f"WHY={why}", f"FROM={h.nick}", f"HOST={h.host}",
                       f"TO={h.target}", f"BODY={h.body}")
