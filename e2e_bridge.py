"""E2E encrypted queries and order challenges, bot side (issue #4).

The crypto, framing and pacing live in the shared irc-olm package
(github.com/vjt/irc-olm); the protocol is grappa-irc issue 2430. This module
maps it onto bot.py: what reaches the event stream, under which trust verdict,
and when an order needs a CHAL first.

Keys are PINNED, never learned (vjt's final ruling, 2026-10-18): the operator
pastes each peer's Ed25519 key into the pins file (BOT_E2E_PINS, one
`<nick> <key>` per line) and the bot talks E2E only to those keys. An unknown
key gets silence. There is no SAS and no confirm command on a bot; rotating a
key means re-pasting it, and the file is reloaded when it changes. The bot
only READS the pins file: the operator makes it root-owned (or denies it to
the bot's Claude session), and the bot says so (E2E_PINS_WRITABLE) when it
could write it.

Trust has two layers:

  * Layer 1 is today's rule for every line: the nick is in bot.trust, the host
    matches its glob, and 307/330 says it is identified.
  * Layer 2 is a pinned key for that nick. A user WITHOUT one keeps layer 1
    alone, exactly as before. A user WITH one no longer counts in plain text
    alone (no downgrade): in the encrypted query the session authenticates the
    key. Outside it, a line addressed to the bot (its nick as a word) or a
    plaintext PM is held, a CHAL goes out over the session, and the line is
    released as TRUSTED only on a matching ACK within 15 s. Anything else from
    that user in plain text is UNTRUSTED.
  * A different key on a pinned nick is an alarm (E2E_KEY_ALARM), and what it
    says never reaches the stream. So is a pinned key speaking under a nick
    pinned to another key.

Log hygiene: decrypted text appears ONLY as the BODY of the MSG event on stdout
(ruling 7). bot.log gets the ciphertext lines plus `E2E MSG from X (n bytes)`.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from irc_olm import framing
from irc_olm.engine import (SENT_MAX, AckOk, AckRejected, ChallengeExpired, Dropped, KeyAlarm,
                            Received, Rekeying, Replayed, SendFailed, SendQueued, Undecryptable)
from irc_olm.pins import PinsError, load_pins, pins_writable

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
    return framing.fold(nick)


def _one_line(text: str) -> str:
    """Decrypted text as one event line. Unlike an IRC line it can carry CR,
    LF or NUL (which would forge a second event) and lone surrogates (which
    print() refuses)."""
    text = text.replace("\r", " ").replace("\n", " ").replace("\0", " ")
    return text.encode("utf-8", "replace").decode("utf-8")


class PinsFile:
    """The operator's pins file. Read, never written; reloaded on change.

    `keys` is what it says, folded nick -> Ed25519 key: "the key pinned to
    this nick" for the bridge. engine.pinned() is NOT that: past the
    configured nicks it answers for wherever a pinned key last spoke (irc-olm
    keeps it to route replies to a renamed peer, in the state file), and a
    routing hint must never decide whose words a message is."""

    def __init__(self, path: str, emit):
        self.path = path
        self._emit = emit
        self._sig = None
        self._error = None
        self.keys: dict[str, str] = {}

    def _stat(self):
        st = os.stat(self.path)
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def load(self) -> dict[str, str]:
        """The first load, at startup: a missing or broken file raises."""
        self._sig = self._stat()
        pins = self._read()
        self._loaded(pins)
        return pins

    def _read(self) -> dict[str, str]:
        try:
            return load_pins(self.path)
        except UnicodeDecodeError as e:
            # A PinsError like any broken file, without the repr that quotes
            # the whole file.
            raise PinsError(f"{self.path}: not UTF-8 (byte {e.start})") from None

    def reload_if_changed(self) -> dict[str, str] | None:
        """The new pins when the file changed and parses, else None. A broken
        or missing file keeps the pins already loaded, and says so once."""
        try:
            sig = self._stat()
            if sig == self._sig:
                return None
            self._sig = sig
            pins = self._read()
        except (OSError, PinsError) as e:
            if str(e) != self._error:
                self._error = str(e)
                self._emit("E2E_PINS_ERROR", f"PATH={self.path}", f"WHY={e}", "kept=previous")
            return None
        self._error = None
        self._loaded(pins)
        return pins

    def _loaded(self, pins: dict[str, str]) -> None:
        # parse_pins accepts one spelling per key, the engine's own.
        self.keys = {_fold(nick): key for nick, key in pins.items()}
        self._emit("E2E_PINS_LOADED", f"N={len(pins)}")
        if pins_writable(self.path):
            self._emit("E2E_PINS_WRITABLE", f"PATH={self.path}",
                       "the operator must make it root-owned or deny it to the bot's Claude")


class Bridge:
    def __init__(self, engine, *, own_nick, pins, submit, room, emit, log, trust_check,
                 is_listed, hold_dm, clock=time.monotonic):
        self.engine = engine
        self.own_nick = own_nick
        self._pins = pins                # PinsFile
        self._submit = submit            # (outbound pairs, urgent msgid -> ttl) -> refused (nick, msgid), paced
        self._room = room                # nick -> whether the pacer can take a text message
        self._emit = emit
        self._log = log                  # (direction, line, origin)
        self._trust_check = trust_check
        self._is_listed = is_listed
        self._hold_dm = hold_dm          # (nick, host, body, extra_fields, on_release) -> depth
        self._clock = clock
        self._lock = threading.RLock()
        self.network_token = ""          # 005 NETWORK=, the CHAL `network` field
        self._held: dict[str, _Held] = {}
        self._reply_mode: dict[str, float] = {}
        # The text messages handed to the pacer (msgids): the only ones whose
        # loss after queueing is a lost reply. As many as the engine keeps
        # plaintexts for.
        self._text: OrderedDict[str, None] = OrderedDict()
        self._hosts: dict[str, str] = {}
        self._set_own_nick(own_nick)

    def _set_own_nick(self, nick: str) -> None:
        """The nick a line must name to be addressed to us, and the TO= of an
        encrypted DM."""
        self.own_nick = nick
        self._addressed = re.compile(
            r"(?<![A-Za-z0-9\[\]\\`^{}|_-])" + re.escape(nick) + r"(?![A-Za-z0-9\[\]\\`^{}|_-])",
            re.IGNORECASE)

    @staticmethod
    def is_e2e(body: str) -> bool:
        """Any PRIVMSG body that claims to be E2E, well-formed or not."""
        return framing.is_e2e(body)

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

    def on_own_nick(self, nick: str) -> None:
        """Our own NICK change: the receivers now see a different mask, and
        an order now names the new nick."""
        with self._lock:
            self._set_own_nick(nick)
            self.engine.nick = nick
            mask = self.engine.own_mask
            if mask and "!" in mask:
                self.engine.set_own_mask(nick + mask[mask.index("!"):])

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
        if self._pinned_key(nick) is None:
            # The bot sends E2E to no such nick, so it expects nothing from one.
            # Handed to the engine, a pinned key speaking from here would move
            # irc-olm's routing hint, and a KEYREQ would then get keys.
            self._emit("E2E_DROPPED", f"FROM={nick}", f"HOST={host}", "REASON=unpinned-nick")
            return
        with self._lock:
            self._hosts[_fold(nick)] = host
            self.dispatch(self.engine.receive(nick, body))

    # --- plaintext orders ------------------------------------------------------

    def gate_plaintext(self, nick: str, host: str, prefix: str, target: str, body: str,
                       trusted: bool) -> str | None:
        """Called for every plaintext PRIVMSG that reached the verdict.

        Returns None when today's verdict stands, "held" when the line waits for
        an ACK (the bridge emits it later), "unchallenged" when it must be shown
        UNTRUSTED because its author has a pinned key and plain text alone no
        longer counts."""
        is_pm = not target.startswith(("#", "&"))
        with self._lock:
            if not trusted:
                return None
            if self._pinned_key(nick) is None:
                if is_pm:
                    # Released TRUSTED in plain text: so is the reply.
                    self._reply_mode.pop(_fold(nick), None)
                return None
            if not (is_pm or self._addressed.search(body)):
                return "unchallenged"
            # Pins change only in tick(), under this same lock: the nick is
            # still pinned here, so challenge() cannot raise NotPinnedError.
            ch, res = self.engine.challenge(nick, network=self.network_token, channel=target,
                                            sender_mask=prefix, line=body.encode("utf-8"))
            self._held[ch.nonce] = _Held(target, nick, host, body, is_pm)
            self.dispatch(res)
            self._emit("ORDER_HELD", f"FROM={nick}", f"HOST={host}", f"TO={target}",
                       f"LEN={len(body)}")
            return "held"

    # --- outbound ------------------------------------------------------------------

    def _pinned_key(self, nick: str) -> str | None:
        return self._pins.keys.get(_fold(nick))

    def wants_encryption(self, nick: str) -> bool:
        with self._lock:
            at = self._reply_mode.get(_fold(nick))
            return at is not None and self._clock() - at < REPLY_MODE_TTL

    def send_text(self, nick: str, text: str, *, action: bool, origin) -> None:
        with self._lock:
            if self._pinned_key(nick) is None:
                # E2E goes only to a nick the operator pinned, never to where
                # irc-olm last heard a pinned key (engine.pinned's hint).
                self._emit("E2E_SEND_FAILED", f"TO={nick}", "REASON=unpinned-nick", "LOST=1")
                return
            if not self._room(nick):
                # Asked first: a message the pacer refuses was encrypted all
                # the same, and the receiver would skip its key.
                self._emit("E2E_SEND_FAILED", f"TO={nick}", "REASON=pacer-full", "LOST=1")
                return
            res = self.engine.send(nick, {"t": "action" if action else "text", "body": text})
            # bot.log says what became of it, never the text.
            line = f"E2E {'ACT' if action else 'MSG'} to {nick} ({len(text.encode())} bytes)"
            if self.dispatch(res):
                self._log(">", line, origin)
            elif any(isinstance(e, SendQueued) for e in res.events):
                self._log(">", f"{line} queued for keys", origin)

    # --- time ------------------------------------------------------------------------

    def tick(self) -> None:
        """About once a second: pins reload, challenge expiry, engine upkeep."""
        with self._lock:
            pins = self._pins.reload_if_changed()
            if pins is not None:
                self.engine.set_pins(pins)
            self.dispatch(self.engine.tick())

    # --- events ------------------------------------------------------------------------

    def dispatch(self, result) -> set[str]:
        """Hand an engine Result to the pacer and its events to the stream.
        Returns the msgids of the text messages the pacer took. Under the
        lock throughout: on_lost, on the pacer's thread, may come for a
        message before it is recorded as text below."""
        with self._lock:
            refused = self._submit(result.outbound, result.urgent) if result.outbound else []
            # Never left: no NACK can come for them, and each is a message lost.
            # A refused challenge is not: its order ends as an unanswered one.
            self.engine.discard([msgid for _, msgid in refused])
            for nick, msgid in refused:
                if msgid not in result.urgent:
                    self._emit("E2E_SEND_FAILED", f"TO={nick}", "REASON=pacer-full", "LOST=1")
            # Everything but text is urgent: KEYREQ, KEYS, NACK, CHAL and ACK.
            taken = ({framing.parse(body).msgid for _, body in result.outbound}
                     - result.urgent.keys() - {msgid for _, msgid in refused})
            for msgid in taken:
                self._text[msgid] = None
            while len(self._text) > SENT_MAX:
                self._text.popitem(last=False)
            for e in result.events:
                self._on_event(e)
            return taken

    def on_lost(self, nick: str, msgid: str | None, reason: str) -> None:
        """irc-olm PacedSender's on_lost, on the pacer's thread: a message
        lost after it was queued. "late" is a challenge that could no longer
        be answered in time (its order ends as an unanswered one, like any
        expired challenge); "send-failed" is a message a line of which failed
        to write, the rest dropped. Only a text message is a lost reply: a
        KEYREQ, KEYS or NACK is the protocol's to recover (or the engine
        reports what it cost), a CHAL ends as an unanswered order."""
        with self._lock:
            if msgid is not None:
                self.engine.discard([msgid])
            if msgid in self._text:
                del self._text[msgid]
                self._emit("E2E_SEND_FAILED", f"TO={nick}", f"REASON={reason}", "LOST=1")

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
            self._emit("E2E_KEY_ALARM", f"FROM={e.nick}", f"HOST={self._hosts.get(_fold(e.nick), '')}",
                       f"PINNED={e.pinned}", f"PRESENTED={e.presented}")
        elif isinstance(e, Replayed):
            self._emit("E2E_REPLAYED", f"FROM={e.nick}")
        elif isinstance(e, Undecryptable):
            self._emit("E2E_UNDECRYPTABLE", f"FROM={e.nick}", f"REASON={e.reason}")
        elif isinstance(e, Rekeying):
            self._emit("E2E_REKEY", f"FROM={e.nick}")
        elif isinstance(e, SendFailed):
            self._emit("E2E_SEND_FAILED", f"TO={e.nick}", f"REASON={e.reason}", f"LOST={e.lost}")
        elif isinstance(e, Dropped):
            self._emit("E2E_DROPPED", f"FROM={e.nick}", f"REASON={e.reason}")

    def _on_received(self, e: Received) -> None:
        kind = e.inner.get("t")
        body = e.inner.get("body")
        if kind not in ("text", "action") or not isinstance(body, str):
            return  # chal / unknown: nothing for the stream
        body = _one_line(body)
        nick, host = e.nick, self._hosts.get(_fold(e.nick), "")
        self._log("*", f"E2E MSG from {nick} ({len(body.encode())} bytes)", None)
        pinned = self._pinned_key(nick)
        if pinned != e.peer.ed25519:
            # A pinned key, but not this nick's: not this nick speaking.
            if pinned is not None:
                # Under a nick pinned to another key: an impersonation, which
                # is what the alarm is for. (Under a nick with no pin it is a
                # renamed peer at most, and layer 1 is that nick's.)
                self._emit("E2E_KEY_ALARM", f"FROM={nick}", f"HOST={host}", f"PINNED={pinned}",
                           f"PRESENTED={e.peer.ed25519}")
            self._emit("E2E_DROPPED", f"FROM={nick}", f"HOST={host}", "REASON=not-the-pinned-key",
                       f"LEN={len(body)}")
            return
        extra = ["ENCRYPTED=1"] + (["ACTION=1"] if kind == "action" else [])
        trusted, reason = self._trust_check(nick, host)
        if not trusted:
            if reason == "pending-whois":
                depth = self._hold_dm(nick, host, body, tuple(extra),
                                      lambda: self._encrypted_dm_released(nick))
                self._emit("DM_HELD", f"FROM={nick}", f"HOST={host}", f"LEN={len(body)}",
                           f"QUEUED={depth}", "ENCRYPTED=1")
            else:
                self._emit("DM_BLOCKED", f"FROM={nick}", f"HOST={host}", f"LEN={len(body)}",
                           reason, "ENCRYPTED=1")
            return
        self._reply_mode[_fold(nick)] = self._clock()
        self._emit("MSG", "TRUSTED", *extra, f"FROM={nick}", f"HOST={host}",
                   f"TO={self.own_nick}", f"BODY={body}")

    def _encrypted_dm_released(self, nick: str) -> bool:
        """bot.py's dm_flush is about to release a held encrypted DM as
        TRUSTED: the reply to it is encrypted too, as on the direct path."""
        with self._lock:
            self._reply_mode[_fold(nick)] = self._clock()
        return True

    def _release(self, nonce: str, *, confirmed: bool, why: str = "") -> None:
        h = self._held.pop(nonce, None)
        if h is None:
            return
        if confirmed and h.is_pm:
            # A plaintext PM confirmed from the session: the conversation is
            # in plain text now, and so is the reply. An unconfirmed one
            # changes nothing, or holding the nick would be enough to turn
            # the reply to the real user into plain text.
            self._reply_mode.pop(_fold(h.nick), None)
        if confirmed:
            self._emit("MSG", "TRUSTED", "CONFIRMED=1", f"FROM={h.nick}", f"HOST={h.host}",
                       f"TO={h.target}", f"BODY={h.body}")
        elif h.is_pm:
            self._emit("DM_DROPPED", f"FROM={h.nick}", f"HOST={h.host}", f"LEN={len(h.body)}",
                       why)
        else:
            self._emit("MSG", "UNTRUSTED", f"WHY={why}", f"FROM={h.nick}", f"HOST={h.host}",
                       f"TO={h.target}", f"BODY={h.body}")
