#!/usr/bin/env python3
"""
Watchdog / clear-sidecar for the long-lived Claude Code session that
powers vjt-claude. Tails the active session JSONL under
~/.claude/projects/-home-vjt-code-IRC-vjt-claude/ and injects `/clear`
into the tmux pane running `claude` in window `0:ircbot` on two
independent triggers:

1. AUP refusal — assistant message matches "Usage Policy" / "unable to
   respond" pattern → clear immediately.
2. Idle — jsonl mtime hasn't advanced for IDLE_SEC AND there is no
   pending assistant tool_use awaiting a user tool_result → clear.

Both triggers share the same cooldown window, so back-to-back clears
never stack. Nothing here runs claude itself — it only kicks the pane.

(Original AUP-only script; extended 2026-04-19 with the idle trigger
per vjt's request to also free KV on quiet periods.)
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path.home() / ".claude/projects/-home-vjt-code-IRC-vjt-claude"
TMUX_WINDOW = "0:ircbot"

POLL_SEC = 2
DEBOUNCE_SEC = 30           # any clear — AUP / idle / turns — holds this window
IDLE_SEC = 600              # 10 min of no jsonl writes = idle
MAX_TURNS = 100             # assistant turns since last clear → eager clear
TAIL_SCAN = 200             # lines from end to check for pending tool_use
POST_CLEAR_WAIT = 3         # seconds for /clear to settle before scrub prompt

SCRUB_PROMPT = (
    "Memory scrub per CLAUDE.md: for each `### YYYY-MM-DD` heading in "
    "/home/vjt/code/IRC/vjt-claude/memory/project_activity_log.md older "
    "than 14 days, review the bullets — promote anything that will matter "
    "beyond 14d into a typed memory file (user_*/feedback_*/project_*/"
    "reference_*), then delete the aged entry. Append one line to "
    "/home/vjt/code/claude-chatbot/scrub.log with ISO timestamp, N days "
    "trimmed, and list of files created/updated. NOTICE vjt via bot.send "
    "ONLY if promotions occurred; otherwise stay silent."
)

STUCK_PATTERNS = re.compile(
    r"(unable to respond to this request|appears to violate our Usage Policy|Usage Policy)",
    re.IGNORECASE,
)


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def latest_jsonl() -> Path | None:
    candidates = sorted(
        PROJECT_DIR.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def resolve_claude_pane() -> str | None:
    try:
        out = subprocess.check_output(
            ["tmux", "list-panes", "-t", TMUX_WINDOW,
             "-F", "#{pane_id} #{pane_current_command}"],
            text=True,
        )
    except subprocess.CalledProcessError as e:
        log(f"tmux list-panes failed: {e}")
        return None
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[1] == "claude":
            return parts[0]
    return None


def inject_clear(pane: str) -> bool:
    try:
        subprocess.check_call(
            ["tmux", "send-keys", "-t", pane, "/clear", "Enter"]
        )
        return True
    except subprocess.CalledProcessError as e:
        log(f"tmux send-keys failed on {pane}: {e}")
        return False


def inject_scrub(pane: str) -> bool:
    """After /clear has settled, kick a memory-scrub prompt into the pane.
    Claude reads CLAUDE.md fresh on the first post-clear turn, so the
    scrub prompt arrives with the housekeeping rules already in context."""
    time.sleep(POST_CLEAR_WAIT)
    try:
        subprocess.check_call(
            ["tmux", "send-keys", "-t", pane, SCRUB_PROMPT, "Enter"]
        )
        return True
    except subprocess.CalledProcessError as e:
        log(f"tmux send-keys (scrub) failed on {pane}: {e}")
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


def fire_clear(reason: str) -> bool:
    pane = resolve_claude_pane()
    if pane is None:
        log(f"{reason} but no claude pane found — skipping")
        return False
    log(f"{reason} → injecting /clear into {pane}")
    if not inject_clear(pane):
        return False
    if inject_scrub(pane):
        log(f"scrub prompt injected into {pane}")
    return True


def main() -> int:
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
            if not fired_this_tick:
                age = now - current_file.stat().st_mtime
                boot_age = now - boot_ts
                if (
                    age >= IDLE_SEC
                    and boot_age >= IDLE_SEC
                    and now - last_fire >= DEBOUNCE_SEC
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
