#!/usr/bin/env python3
"""
Watchdog / clear-sidecar for the long-lived Claude Code session that
powers the bot. Tails the active session JSONL under the session's
~/.claude/projects/<dir>/ (by default the one Claude Code keeps for a
session started in this repo) and injects `/clear` into the tmux pane
running `claude` in window `ircbot` (resolved session-agnostic via
`tmux list-panes -a`) on four independent triggers:

1. AUP refusal — assistant message matches "Usage Policy" / "unable to
   respond" pattern → clear immediately.
2. Context — the last assistant reply saw MAX_CTX_TOKENS or more of
   context, with no tool_use pending → clear.
3. Idle — jsonl mtime hasn't advanced for IDLE_SEC AND there is no
   pending assistant tool_use awaiting a user tool_result → clear.
4. Manual — SIGUSR1 forces a /clear + scrub on the next tick, bypassing
   debounce. For testing the scrub flow on demand:
       systemctl --user kill -s SIGUSR1 aup-watchdog.service

All triggers share the same cooldown window, so back-to-back clears
never stack. Nothing here runs claude itself — it only kicks the pane.

One instance per session. The defaults fit a session started in this repo
talking through its bot.send; a second session (2026-11-08) runs its own
instance with the WATCHDOG_* environment variables below overriding where it
reads, which pane it kicks and what it says. Nothing goes to IRC unless
WATCHDOG_ESCALATE_NICK / WATCHDOG_ANNOUNCE_TO name a target.

(Original AUP-only script; extended 2026-04-19 with the idle trigger
per vjt's request to also free KV on quiet periods.)
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

_env = os.environ.get

REPO = Path(__file__).resolve().parent
# Claude Code keeps a session's transcripts in ~/.claude/projects/<dir>, <dir>
# being the absolute path the session was started in with every character that
# is not a letter or a digit turned into "-" (so /srv/my.bot -> -srv-my-bot).
# Default: a session started in this repo.
PROJECT_DIR = Path(_env("WATCHDOG_PROJECT_DIR",
                        str(Path.home() / ".claude/projects"
                            / re.sub(r"[^A-Za-z0-9]", "-", str(REPO)))))
TMUX_WINDOW_NAME = _env("WATCHDOG_WINDOW", "ircbot")  # window name only — session-agnostic
# The pane is the one in that window whose foreground command is PANE_CMD. A
# session started through `sudo -u <user> -i` shows `sudo`, not `claude`, and
# then a shell started the same way in the same window looks identical:
# WATCHDOG_PANE_INDEX pins which pane of the window it is.
PANE_CMD = _env("WATCHDOG_PANE_CMD", "claude")
PANE_INDEX = _env("WATCHDOG_PANE_INDEX")  # None = any pane of the window
BOT_FIFO = Path(_env("WATCHDOG_FIFO", str(REPO / "bot.send")))
# SAY target when resolve stays broken; empty = no escalation
ESCALATE_NICK = _env("WATCHDOG_ESCALATE_NICK", "")
# where a refusal/AUP clear is announced (vjt, 2026-10-29); empty = not announced
FORCED_CLEAR_CHAN = _env("WATCHDOG_ANNOUNCE_TO", "")
LABEL = _env("WATCHDOG_LABEL", "watchdog")  # prefix of every IRC line it sends
# Set when the transcripts belong to another user, readable through a default
# ACL on PROJECT_DIR: see grant_read().
GRANT_AS = _env("WATCHDOG_GRANT_AS")

POLL_SEC = 2
DEBOUNCE_SEC = 60           # any clear — AUP / idle / context — holds this window
                            # (must be ≥ PRE_CLEAR_WARN_SEC + POST_CLEAR_WAIT + buffer)
IDLE_ENABLED = True         # idle trigger re-enabled 2026-06-28 per vjt (was disabled
                            # 2026-06-05). History of the disable rationale below:
                            # idle trigger disabled 2026-06-05: clearing on quiet
                            # pays a full /start cycle (re-read CLAUDE.md/MEMORY/
                            # activity-log + scrub + sweep) to drop context that,
                            # while idle, costs zero tokens. The context trigger
                            # guards real bloat; AUP + SIGUSR1 cover recovery.
IDLE_SEC = 3600             # 1 h = prompt-cache TTL: past it the context is cold anyway (2026-11-13)
# Context, in tokens, at which a /clear pays for itself. It replaces a count of
# 150 transcript lines, which cleared at 117k of a 1M window when /resume alone
# brings the session back to ~100k (vjt 2026-11-23: «poteva aspettare»).
MAX_CTX_TOKENS = int(_env("WATCHDOG_MAX_CTX", "300000"))
TAIL_SCAN = 200             # lines from end to check for pending tool_use
PRE_CLEAR_WARN_SEC = 15     # grace window between warning prompt and /clear
                            # so Claude can persist in-flight state to disk
POST_CLEAR_WAIT = 3         # seconds for /clear to settle before scrub prompt (capture-verify + retries cover Ink race)
SCRUB_VERIFY_TRIES = 4      # retries if paste didn't land in input
SCRUB_VERIFY_GAP = 3        # seconds between verify retries
RESOLVE_ALERT_SEC = 300     # consecutive resolve failures before IRC escalation
LOG_DEDUP_SEC = 60          # collapse identical consecutive log lines for this long
IDLE_MIN_TURNS = 30         # skip IDLE fire unless this many assistant turns accumulated
                            # since last clear — post-scrub /start bootstrap alone
                            # produces ~10-20 turns; firing IDLE off a mere
                            # post-bootstrap lull cycles /clear forever on sessions
                            # that are just waiting for their next IRC message.

# warm-resume skill (lean); cold boot still uses /start
SCRUB_PROMPT = _env("WATCHDOG_SCRUB_PROMPT", "/resume")

PRE_CLEAR_PROMPT = _env("WATCHDOG_PRE_CLEAR_PROMPT", (
    "WATCHDOG NOTICE: a /clear will fire in 15 seconds. "
    "Persist any in-flight conversational state to disk NOW: "
    "pending IRC requests, messages-to-relay, partial work, "
    "new standing orders, lessons learned. "
    "Use memory/ for long-term, project_activity_log.md for ephemera. "
    "Do not reply on IRC unless mid-sentence. Write only."
))

STUCK_PATTERNS = re.compile(
    r"(unable to respond to this request|appears to violate our Usage Policy|Usage Policy"
    r"|safeguards flagged this message|Cyber Verification Program)",
    re.IGNORECASE,
)

# Structural refusal markers. Text matching alone is not enough: on 2026-09-12
# 23:57 UTC a session latched on "API Error: Opus 4.8's safeguards flagged this
# message ... Cyber Verification Program", which matched none of the old
# patterns, and every later turn refused identically — 15 minutes of silence
# with vjt typing "beh??" / "wtf?!" until he cleared by hand. The transcript
# carries an unambiguous structural signal for these: an assistant record with
# stop_reason == "refusal", plus a system record whose subtype names it.
# Match on those and the wording of the day stops mattering.
REFUSAL_STOP_REASONS = {"refusal"}
REFUSAL_SUBTYPE = re.compile(r"refusal", re.IGNORECASE)


_log_state: dict[str, float | str | int] = {"last_msg": "", "last_ts": 0.0, "repeat": 0}


def _emit(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def log(msg: str) -> None:
    """Print with dedup: identical consecutive lines collapse for LOG_DEDUP_SEC,
    then emit a '(repeated N times in X s)' summary on transition."""
    now = time.time()
    last_msg = _log_state["last_msg"]
    last_ts = float(_log_state["last_ts"])
    repeat = int(_log_state["repeat"])
    if msg == last_msg and now - last_ts < LOG_DEDUP_SEC:
        _log_state["repeat"] = repeat + 1
        return
    if repeat > 0 and isinstance(last_msg, str):
        _emit(f"(last line repeated {repeat}x over {int(now - last_ts)}s)")
    _emit(msg)
    _log_state["last_msg"] = msg
    _log_state["last_ts"] = now
    _log_state["repeat"] = 0


def send_fifo_say(nick: str, msg: str) -> None:
    """Fire a SAY to the IRC bridge FIFO — best effort, never raises.
    No target configured (empty `nick`) = nothing is sent."""
    if not nick:
        return
    try:
        with BOT_FIFO.open("w") as f:
            f.write(f"SAY {nick} {msg}\n")
    except OSError as e:
        _emit(f"FIFO write failed: {e!r}")


def newest(paths) -> Path | None:
    return max(paths, key=lambda p: p.stat().st_mtime, default=None)


def grant_read(path: Path) -> None:
    """Claude Code creates each transcript 0600, which zeroes the ACL mask and
    with it the read grant inherited from PROJECT_DIR's default ACL. The owner
    can raise the mask again; nothing is granted beyond that default entry."""
    if GRANT_AS:
        subprocess.run(["sudo", "-n", "-u", GRANT_AS, "setfacl", "-m", "m::r", str(path)],
                       check=False)


def resolve_claude_pane() -> str | None:
    """Find the pane where window_name == TMUX_WINDOW_NAME AND
    pane_current_command == PANE_CMD (and pane_index == PANE_INDEX when set).
    NO fallback — there are multiple claude panes in this tmux server, only
    the configured window counts."""
    try:
        out = subprocess.check_output(
            ["tmux", "list-panes", "-a", "-F",
             "#{window_name}\t#{pane_current_command}\t#{pane_index}\t#{pane_id}"],
            text=True,
        )
    except subprocess.CalledProcessError as e:
        log(f"tmux list-panes failed: {e}")
        return None
    matches: list[str] = []
    for line in out.splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 4:
            continue
        win, cmd, idx, pid = parts
        if (win == TMUX_WINDOW_NAME and cmd == PANE_CMD
                and (PANE_INDEX is None or idx == PANE_INDEX)):
            matches.append(pid)
    if not matches:
        log(f"no pane with window={TMUX_WINDOW_NAME!r} cmd={PANE_CMD} index={PANE_INDEX}")
        return None
    if len(matches) > 1:
        log(f"multiple candidate panes {matches} — picking first")
    return matches[0]


def inject_clear(pane: str) -> bool:
    try:
        subprocess.check_call(
            ["tmux", "send-keys", "-t", pane, "/clear", "Enter"]
        )
        return True
    except subprocess.CalledProcessError as e:
        log(f"tmux send-keys failed on {pane}: {e}")
        return False


def inject_pre_clear_warning(pane: str) -> bool:
    """Best-effort: tell Claude a /clear is imminent so it can persist
    in-flight state to disk before the buffer is wiped. No verify — if
    the keys don't land, /clear still fires; we lose the warning, not
    the system. send-keys -l + Enter, same pattern as inject_scrub but
    without the capture-pane retry loop (a missed warning is less bad
    than a missed scrub: the scrub (/resume) reseeds the post-clear
    session from today's activity-log body, the warning is opportunistic)."""
    try:
        subprocess.check_call(
            ["tmux", "send-keys", "-t", pane, "-l", PRE_CLEAR_PROMPT]
        )
        subprocess.check_call(
            ["tmux", "send-keys", "-t", pane, "Enter"]
        )
        return True
    except subprocess.CalledProcessError as e:
        log(f"pre-clear warning send-keys failed on {pane}: {e}")
        return False


def _capture_pane(pane: str) -> str:
    try:
        return subprocess.check_output(
            ["tmux", "capture-pane", "-p", "-t", pane, "-S", "-40"],
            text=True, stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return ""


def inject_scrub(pane: str) -> bool:
    """After /clear has settled, kick a memory-scrub prompt into the pane.

    paste-buffer appears atomic but on a Pi 5 mid-/clear CC's Ink/React
    renderer still swallows the chars past POST_CLEAR_WAIT — only Enter
    lands, producing an empty submit. Fix: longer settle, then send-keys -l
    (literal) and verify via capture-pane before hitting Enter; retry if
    the needle isn't visible.
    """
    time.sleep(POST_CLEAR_WAIT)
    needle = SCRUB_PROMPT.strip().splitlines()[0][:40]
    landed = False
    for attempt in range(1, SCRUB_VERIFY_TRIES + 1):
        try:
            subprocess.check_call(
                ["tmux", "send-keys", "-t", pane, "-l", SCRUB_PROMPT]
            )
        except subprocess.CalledProcessError as e:
            log(f"tmux send-keys -l (scrub) failed on {pane} attempt {attempt}: {e}")
            time.sleep(SCRUB_VERIFY_GAP)
            continue
        time.sleep(SCRUB_VERIFY_GAP)
        if needle and needle in _capture_pane(pane):
            landed = True
            break
        log(f"scrub paste not yet visible on {pane} (attempt {attempt}/{SCRUB_VERIFY_TRIES})")
    if not landed:
        log(f"scrub paste never landed on {pane} after {SCRUB_VERIFY_TRIES} tries — giving up")
        return False
    try:
        subprocess.check_call(
            ["tmux", "send-keys", "-t", pane, "Enter"]
        )
        return True
    except subprocess.CalledProcessError as e:
        log(f"tmux send-keys Enter (scrub submit) failed on {pane}: {e}")
        return False


def line_is_assistant_turn(line: str) -> bool:
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return False
    return rec.get("type") == "assistant"


def line_ctx_tokens(line: str) -> int:
    """Tokens of context the model read for this assistant reply (0 if none)."""
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return 0
    if rec.get("type") != "assistant" or rec.get("isSidechain"):
        return 0
    usage = (rec.get("message") or {}).get("usage") or {}
    return sum(usage.get(k) or 0 for k in (
        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def line_matches_refusal(line: str) -> str | None:
    """Structural refusal check — returns a short reason, or None.

    Two shapes, both emitted when the API refuses instead of answering:
      {"type":"assistant","message":{"stop_reason":"refusal",...}}
      {"type":"system","subtype":"model_refusal_no_fallback",...}
    A latched session repeats these on every turn, so catching either one
    is enough; the debounce keeps the retries from stacking clears.
    """
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return None
    typ = rec.get("type")
    if typ == "assistant":
        msg = rec.get("message", {})
        if isinstance(msg, dict) and msg.get("stop_reason") in REFUSAL_STOP_REASONS:
            return "stop_reason=refusal"
    elif typ == "system":
        subtype = rec.get("subtype", "")
        if isinstance(subtype, str) and REFUSAL_SUBTYPE.search(subtype):
            return f"system/{subtype}"
    return None


def line_matches_aup(line: str) -> bool:
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return False
    if rec.get("type") != "assistant":
        return False
    content = rec.get("message", {}).get("content", [])
    if not isinstance(content, list):
        return False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            if STUCK_PATTERNS.search(block.get("text", "")):
                return True
    return False


def tail_lines(path: Path, n: int = TAIL_SCAN) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return lines[-n:]
    except OSError:
        return []


def has_pending_tool_use(lines: list[str]) -> bool:
    """True if any assistant tool_use in the tail has no matching user
    tool_result afterwards — i.e., a turn is in flight."""
    used: list[str] = []
    results: set[str] = set()
    for raw in lines:
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        typ = rec.get("type")
        msg = rec.get("message", {})
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        if typ == "assistant":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tid = b.get("id")
                    if tid:
                        used.append(tid)
        elif typ == "user":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    if tid:
                        results.add(tid)
    return any(tid not in results for tid in used)


_resolve_state: dict[str, float | bool] = {"fail_since": 0.0, "alerted": False}

# Set by SIGUSR1 handler to force a /clear on the next main-loop tick.
# Use case: manual test of the scrub flow without waiting for idle/turn-cap.
#   systemctl --user kill -s SIGUSR1 aup-watchdog.service
_manual_fire = False


def _handle_sigusr1(_signum, _frame) -> None:
    global _manual_fire
    _manual_fire = True
    _emit("SIGUSR1 received — will fire /clear + scrub on next tick")


def fire_clear(reason: str, warn: bool = True, announce: bool = False) -> bool:
    pane = resolve_claude_pane()
    now = time.time()
    if pane is None:
        fail_since = float(_resolve_state["fail_since"])
        if fail_since == 0.0:
            _resolve_state["fail_since"] = now
        elif (
            not _resolve_state["alerted"]
            and now - fail_since >= RESOLVE_ALERT_SEC
        ):
            msg = (
                f"{LABEL}: can't find claude pane "
                f"(window={TMUX_WINDOW_NAME}, cmd={PANE_CMD}) for "
                f"{int(now - fail_since)}s — /clear injection stalled ({reason})"
            )
            log(f"ESCALATING to {ESCALATE_NICK or 'nobody (WATCHDOG_ESCALATE_NICK unset)'}: {msg}")
            send_fifo_say(ESCALATE_NICK, msg)
            _resolve_state["alerted"] = True
        log(f"{reason} but no claude pane found — skipping")
        return False
    if _resolve_state["alerted"]:
        send_fifo_say(
            ESCALATE_NICK,
            f"{LABEL}: pane resolved again ({pane}) — back to normal",
        )
    _resolve_state["fail_since"] = 0.0
    _resolve_state["alerted"] = False
    # A refusal-latched session can't answer the warning — it's 15s of nothing.
    if warn:
        log(f"{reason} → pre-clear warning into {pane}, sleeping {PRE_CLEAR_WARN_SEC}s")
        inject_pre_clear_warning(pane)
        time.sleep(PRE_CLEAR_WARN_SEC)
    log(f"{reason} → injecting /clear into {pane}")
    if not inject_clear(pane):
        return False
    # A latched session drops its thread mid-conversation; say so in channel,
    # or people see the bot lose the plot with no explanation (vjt, #sbiffo
    # 2026-10-29 21:08). Idle/context clears are routine and stay silent.
    if announce:
        send_fifo_say(
            FORCED_CLEAR_CHAN,
            f"{LABEL}: /clear forzato ({reason}), la sessione era bloccata "
            f"dal filtro. Se ho perso il filo di qualcosa, ripetetemelo.",
        )
    if inject_scrub(pane):
        log(f"scrub prompt injected into {pane}")
    return True


def main() -> int:
    signal.signal(signal.SIGUSR1, _handle_sigusr1)

    if not PROJECT_DIR.exists():
        log(f"project dir missing: {PROJECT_DIR}")
        return 1

    log(f"watchdog starting — watching {PROJECT_DIR} "
        f"(IDLE_SEC={IDLE_SEC}, DEBOUNCE_SEC={DEBOUNCE_SEC}, MAX_CTX_TOKENS={MAX_CTX_TOKENS})")
    boot_ts = time.time()
    # Only a transcript that did not exist before is a new session. Claude Code
    # writes to a finished one again hours later (12:04 and 14:04 UTC on
    # 2026-11-23: a session that had continued in another got 3 lines), and
    # following the newest mtime switched to it and replayed its 180 assistant
    # lines into the count, which cleared this session at 12% of its context.
    seen = set(PROJECT_DIR.glob("*.jsonl"))
    current_file = newest(seen)
    current_pos = current_file.stat().st_size if current_file else 0
    if current_file:
        grant_read(current_file)
        log(f"tailing {current_file.name} from offset {current_pos} (skipping history)")
    last_fire = 0.0
    turns_since_clear = 0
    ctx_tokens = 0

    while True:
        try:
            fresh = set(PROJECT_DIR.glob("*.jsonl")) - seen
            if fresh:
                seen |= fresh
                current_file = newest(fresh)
                grant_read(current_file)
                current_pos = 0
                log(f"tailing {current_file.name} (new session)")
                turns_since_clear = 0
                ctx_tokens = 0
            if current_file is None:
                time.sleep(POLL_SEC)
                continue

            # --- AUP trigger: tail new lines and pattern-match ---
            with current_file.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(current_pos)
                chunk = f.read()
                current_pos = f.tell()

            now = time.time()
            fired_this_tick = False

            # --- Manual trigger: SIGUSR1 forces a /clear + scrub now ---
            # Bypasses debounce and AUP/context/idle logic — for testing the
            # scrub flow on demand. `systemctl --user kill -s SIGUSR1 …`.
            global _manual_fire
            if _manual_fire:
                _manual_fire = False
                if fire_clear("MANUAL SIGUSR1"):
                    last_fire = now
                    turns_since_clear = 0
                    fired_this_tick = True

            if chunk:
                for line in chunk.splitlines():
                    if not line.strip():
                        continue
                    if line_is_assistant_turn(line):
                        turns_since_clear += 1
                        ctx_tokens = line_ctx_tokens(line) or ctx_tokens
                    refusal = line_matches_refusal(line)
                    if refusal:
                        if now - last_fire < DEBOUNCE_SEC:
                            log(f"refusal {refusal} (debounced, skipping)")
                            break
                        if fire_clear(f"REFUSAL {refusal}", warn=False, announce=True):
                            last_fire = now
                            turns_since_clear = 0
                            fired_this_tick = True
                        break
                    if line_matches_aup(line):
                        if now - last_fire < DEBOUNCE_SEC:
                            log("AUP match (debounced, skipping)")
                            break
                        if fire_clear("AUP STUCK DETECTED", announce=True):
                            last_fire = now
                            turns_since_clear = 0
                            fired_this_tick = True
                        break

            # --- Context trigger: the last reply saw MAX_CTX_TOKENS or more ---
            if not fired_this_tick and ctx_tokens >= MAX_CTX_TOKENS:
                if now - last_fire < DEBOUNCE_SEC:
                    pass  # wait out debounce, fire next tick
                elif has_pending_tool_use(tail_lines(current_file)):
                    log(f"context {ctx_tokens} but pending tool_use — skipping")
                elif fire_clear(f"CTX {ctx_tokens}"):
                    last_fire = now
                    turns_since_clear = 0
                    ctx_tokens = 0
                    fired_this_tick = True

            # --- Idle trigger: jsonl quiet + no pending tool_use ---
            # Gated on IDLE_MIN_TURNS so a post-scrub /start bootstrap lull
            # doesn't keep cycling /clear on sessions that are legitimately
            # waiting for their next IRC message. The bootstrap skill alone
            # produces ~10-20 assistant turns; demand real ongoing work
            # before IDLE is allowed to fire.
            if IDLE_ENABLED and not fired_this_tick:
                age = now - current_file.stat().st_mtime
                boot_age = now - boot_ts
                if (
                    age >= IDLE_SEC
                    and boot_age >= IDLE_SEC
                    and now - last_fire >= DEBOUNCE_SEC
                    and turns_since_clear >= IDLE_MIN_TURNS
                ):
                    if has_pending_tool_use(tail_lines(current_file)):
                        log(f"idle {int(age)}s but pending tool_use — skipping")
                    elif fire_clear(f"IDLE {int(age)}s"):
                        last_fire = now
                        turns_since_clear = 0

            time.sleep(POLL_SEC)
        except KeyboardInterrupt:
            log("interrupted, exiting")
            return 0
        except Exception as e:
            log(f"loop error: {e!r}")
            time.sleep(POLL_SEC)


if __name__ == "__main__":
    sys.exit(main())
