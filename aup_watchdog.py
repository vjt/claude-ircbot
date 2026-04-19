#!/usr/bin/env python3
"""
AUP / stuck-state watchdog for the long-lived Claude Code session that
powers vjt-claude. Tails the active session JSONL under
~/.claude/projects/-home-vjt-code-IRC-vjt-claude/ and, on detecting the
"Usage Policy" refusal pattern, injects `/clear` into the tmux pane
running `claude` in window `0:ircbot`.

Nothing here runs claude itself. It only kicks it when stuck.
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
DEBOUNCE_SEC = 30
POLL_SEC = 2

# Patterns that indicate the session is stuck / refused.
# These appear inside assistant `message.content[].text` fields in the jsonl.
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


def line_matches(line: str) -> bool:
    """Return True if this jsonl line represents an assistant message
    that contains one of the stuck-state patterns."""
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
            text = block.get("text", "")
            if STUCK_PATTERNS.search(text):
                return True
    return False


def main() -> int:
    if not PROJECT_DIR.exists():
        log(f"project dir missing: {PROJECT_DIR}")
        return 1

    log(f"watchdog starting — watching {PROJECT_DIR}")
    current_file: Path | None = None
    current_pos = 0
    last_fire = 0.0
    first_attach = True  # on first attach skip history (avoid firing on past refusals)

    while True:
        try:
            latest = latest_jsonl()
            if latest is None:
                time.sleep(POLL_SEC)
                continue

            if latest != current_file:
                if first_attach:
                    # Skip history on initial attach — only react to NEW events.
                    current_pos = latest.stat().st_size
                    log(f"tailing {latest.name} from offset {current_pos} (skipping history)")
                    first_attach = False
                else:
                    # New session file appeared — tail from the top, it's all new.
                    current_pos = 0
                    log(f"tailing {latest.name} (new session)")
                current_file = latest

            with current_file.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(current_pos)
                chunk = f.read()
                current_pos = f.tell()

            if chunk:
                for line in chunk.splitlines():
                    if not line.strip():
                        continue
                    if line_matches(line):
                        now = time.time()
                        if now - last_fire < DEBOUNCE_SEC:
                            log("match (debounced, skipping)")
                            continue
                        last_fire = now
                        pane = resolve_claude_pane()
                        if pane is None:
                            log("match but no claude pane found — skipping")
                            continue
                        log(f"STUCK DETECTED → injecting /clear into {pane}")
                        inject_clear(pane)
                        # Don't keep matching on the same refusal message
                        # within the debounce window.
                        break

            time.sleep(POLL_SEC)
        except KeyboardInterrupt:
            log("interrupted, exiting")
            return 0
        except Exception as e:
            log(f"loop error: {e!r}")
            time.sleep(POLL_SEC)


if __name__ == "__main__":
    sys.exit(main())
