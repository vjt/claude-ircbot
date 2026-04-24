#!/usr/bin/env python3
"""
Watchdog / clear-sidecar for the long-lived Claude Code session that
powers vjt-claude. Tails the active session JSONL under
~/.claude/projects/-home-vjt-code-IRC-vjt-claude/ and injects `/clear`
into the tmux pane running `claude` in window `ircbot` (resolved
session-agnostic via `tmux list-panes -a`) on three independent triggers:

1. AUP refusal — assistant message matches "Usage Policy" / "unable to
   respond" pattern → clear immediately.
2. Idle — jsonl mtime hasn't advanced for IDLE_SEC AND there is no
   pending assistant tool_use awaiting a user tool_result → clear.
3. Manual — SIGUSR1 forces a /clear + scrub on the next tick, bypassing
   debounce. For testing the scrub flow on demand:
       systemctl --user kill -s SIGUSR1 vjt-claude-aup-watchdog.service

All triggers share the same cooldown window, so back-to-back clears
never stack. Nothing here runs claude itself — it only kicks the pane.

(Original AUP-only script; extended 2026-04-19 with the idle trigger
per vjt's request to also free KV on quiet periods.)
"""

import json
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path.home() / ".claude/projects/-home-vjt-code-IRC-vjt-claude"
TMUX_WINDOW_NAME = "ircbot"  # window name only — session-agnostic
BOT_FIFO = Path("/home/vjt/code/IRC/vjt-claude/bot.send")
ESCALATE_NICK = "vjt"        # SAY target when resolve stays broken

POLL_SEC = 2
DEBOUNCE_SEC = 30           # any clear — AUP / idle / turns — holds this window
IDLE_SEC = 600              # 10 min of no jsonl writes = idle
MAX_TURNS = 100             # assistant turns since last clear → eager clear
TAIL_SCAN = 200             # lines from end to check for pending tool_use
POST_CLEAR_WAIT = 10        # seconds for /clear to settle before scrub prompt (Pi is slow)
SCRUB_VERIFY_TRIES = 4      # retries if paste didn't land in input
SCRUB_VERIFY_GAP = 3        # seconds between verify retries
RESOLVE_ALERT_SEC = 300     # consecutive resolve failures before IRC escalation
LOG_DEDUP_SEC = 60          # collapse identical consecutive log lines for this long

SCRUB_PROMPT = "/start"

STUCK_PATTERNS = re.compile(
    r"(unable to respond to this request|appears to violate our Usage Policy|Usage Policy)",
    re.IGNORECASE,
)


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
    """Fire a SAY to the IRC bridge FIFO — best effort, never raises."""
    try:
        with BOT_FIFO.open("w") as f:
            f.write(f"SAY {nick} {msg}\n")
    except OSError as e:
        _emit(f"FIFO write failed: {e!r}")


def latest_jsonl() -> Path | None:
    candidates = sorted(
        PROJECT_DIR.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def resolve_claude_pane() -> str | None:
    """Find the pane where window_name == TMUX_WINDOW_NAME AND
    pane_current_command == 'claude'. NO fallback — there are multiple
    claude panes in this tmux server, only the ircbot window counts."""
    try:
        out = subprocess.check_output(
            ["tmux", "list-panes", "-a", "-F",
             "#{window_name}\t#{pane_current_command}\t#{pane_id}"],
            text=True,
        )
    except subprocess.CalledProcessError as e:
        log(f"tmux list-panes failed: {e}")
        return None
    matches: list[str] = []
    for line in out.splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 3:
            continue
        win, cmd, pid = parts
        if win == TMUX_WINDOW_NAME and cmd == "claude":
            matches.append(pid)
    if not matches:
        log(f"no pane with window={TMUX_WINDOW_NAME!r} cmd=claude")
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
#   systemctl --user kill -s SIGUSR1 vjt-claude-aup-watchdog.service
_manual_fire = False


def _handle_sigusr1(_signum, _frame) -> None:
    global _manual_fire
    _manual_fire = True
    _emit("SIGUSR1 received — will fire /clear + scrub on next tick")


def fire_clear(reason: str) -> bool:
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
                f"watchdog: can't find claude pane "
                f"(window={TMUX_WINDOW_NAME}, cmd=claude) for "
                f"{int(now - fail_since)}s — /clear injection stalled ({reason})"
            )
            log(f"ESCALATING to {ESCALATE_NICK}: {msg}")
            send_fifo_say(ESCALATE_NICK, msg)
            _resolve_state["alerted"] = True
        log(f"{reason} but no claude pane found — skipping")
        return False
    if _resolve_state["alerted"]:
        send_fifo_say(
            ESCALATE_NICK,
            f"watchdog: pane resolved again ({pane}) — back to normal",
        )
    _resolve_state["fail_since"] = 0.0
    _resolve_state["alerted"] = False
    log(f"{reason} → injecting /clear into {pane}")
    if not inject_clear(pane):
        return False
    if inject_scrub(pane):
        log(f"scrub prompt injected into {pane}")
    return True


def main() -> int:
    signal.signal(signal.SIGUSR1, _handle_sigusr1)

    if not PROJECT_DIR.exists():
        log(f"project dir missing: {PROJECT_DIR}")
        return 1

    log(f"watchdog starting — watching {PROJECT_DIR} "
        f"(IDLE_SEC={IDLE_SEC}, DEBOUNCE_SEC={DEBOUNCE_SEC}, MAX_TURNS={MAX_TURNS})")
    boot_ts = time.time()
    current_file: Path | None = None
    current_pos = 0
    last_fire = 0.0
    first_attach = True
    turns_since_clear = 0

    while True:
        try:
            latest = latest_jsonl()
            if latest is None:
                time.sleep(POLL_SEC)
                continue

            if latest != current_file:
                if first_attach:
                    current_pos = latest.stat().st_size
                    log(f"tailing {latest.name} from offset {current_pos} (skipping history)")
                    first_attach = False
                else:
                    current_pos = 0
                    log(f"tailing {latest.name} (new session)")
                current_file = latest
                turns_since_clear = 0

            # --- AUP trigger: tail new lines and pattern-match ---
            with current_file.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(current_pos)
                chunk = f.read()
                current_pos = f.tell()

            now = time.time()
            fired_this_tick = False

            # --- Manual trigger: SIGUSR1 forces a /clear + scrub now ---
            # Bypasses debounce and AUP/turns/idle logic — for testing the
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
                    if line_matches_aup(line):
                        if now - last_fire < DEBOUNCE_SEC:
                            log("AUP match (debounced, skipping)")
                            break
                        if fire_clear("AUP STUCK DETECTED"):
                            last_fire = now
                            turns_since_clear = 0
                            fired_this_tick = True
                        break

            # --- Turns trigger: MAX_TURNS assistant turns since last clear ---
            if not fired_this_tick and turns_since_clear >= MAX_TURNS:
                if now - last_fire < DEBOUNCE_SEC:
                    pass  # wait out debounce, fire next tick
                elif has_pending_tool_use(tail_lines(current_file)):
                    log(f"turns {turns_since_clear} but pending tool_use — skipping")
                elif fire_clear(f"TURNS {turns_since_clear}"):
                    last_fire = now
                    turns_since_clear = 0
                    fired_this_tick = True

            # --- Idle trigger: jsonl quiet + no pending tool_use ---
            # `mtime > last_fire` gates on evidence that CC actually processed
            # the previous /clear (wrote at least one line after it). Without
            # this, a stuck pane never advances mtime → age stays huge → every
            # DEBOUNCE_SEC fires another /clear → clears pile up in CC's input.
            if not fired_this_tick:
                age = now - current_file.stat().st_mtime
                boot_age = now - boot_ts
                if (
                    age >= IDLE_SEC
                    and boot_age >= IDLE_SEC
                    and now - last_fire >= DEBOUNCE_SEC
                    and current_file.stat().st_mtime > last_fire
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
