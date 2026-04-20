#!/usr/bin/env python3
"""PreToolUse gate — wholesale.

Fires on every tool (matcher `*` wired in .claude/settings.json). Allow-list
is the union of `permissions.allow` from both `.claude/settings.json`
(checked-in, generic rules) and `.claude/settings.local.json` (gitignored,
host-specific paths + WebFetch domains). No match → fast deny + IRC NOTICE
to vjt so a blocked call surfaces on IRC instead of CC's silent prompt.

Allow-rule grammar (superset of CC native):
    <Tool>                          — bare = any use
    Read(<path-glob>)               — fnmatch on tool_input.file_path
    Edit(<path-glob>)               — idem
    Write(<path-glob>)              — idem
    NotebookEdit(<path-glob>)       — idem
    Bash(<cmd-glob>)                — fnmatch on tool_input.command
    WebFetch(domain:<host>)         — exact host
    WebFetch(domain:*.<suffix>)     — subdomain wildcard
    Skill(<name>)                   — exact skill name
    <Tool>(<key>:<value>)           — generic key:value equality on tool_input
"""
import fnmatch
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent.parent  # .../.claude/
SETTINGS_FILES = [HERE / "settings.json", HERE / "settings.local.json"]
BOT_FIFO = HERE.parent / "bot.send"


def load_allow():
    out = []
    for p in SETTINGS_FILES:
        try:
            out.extend(json.loads(p.read_text()).get("permissions", {}).get("allow", []))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return out


def notify(text):
    """Non-blocking FIFO write — silent if bot is down (no reader)."""
    try:
        fd = os.open(str(BOT_FIFO), os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, f"SAY vjt {text}\n".encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def host_matches(pattern, host):
    if pattern.startswith("*."):
        return host == pattern[2:] or host.endswith(pattern[1:])
    return host == pattern


def rule_matches(rule, tool, tool_input):
    if rule == tool:
        return True
    prefix = f"{tool}("
    if not (rule.startswith(prefix) and rule.endswith(")")):
        return False
    inner = rule[len(prefix):-1]

    if tool in ("Read", "Edit", "Write", "NotebookEdit"):
        return fnmatch.fnmatchcase(tool_input.get("file_path", ""), inner)

    if tool == "Bash":
        return fnmatch.fnmatchcase(tool_input.get("command", ""), inner)

    if tool == "WebFetch" and inner.startswith("domain:"):
        host = urlparse(tool_input.get("url", "")).hostname or ""
        return host_matches(inner[7:], host)

    if tool == "Skill":
        return tool_input.get("skill", "") == inner

    if ":" in inner:
        k, v = inner.split(":", 1)
        return str(tool_input.get(k, "")) == v

    return False


def hint_for(tool, tool_input):
    if tool == "WebFetch":
        h = urlparse(tool_input.get("url", "")).hostname or "?"
        return f' — "vjt-claude: allow WebFetch(domain:{h})"'
    if tool in ("Read", "Edit", "Write", "NotebookEdit"):
        p = tool_input.get("file_path", "?")
        return f' — "vjt-claude: allow {tool}({p})"'
    if tool == "Bash":
        c = (tool_input.get("command", "") or "").split()[0] or "?"
        return f' — "vjt-claude: allow Bash({c} *)"'
    if tool == "Skill":
        n = tool_input.get("skill", "?")
        return f' — "vjt-claude: allow Skill({n})"'
    return f' — "vjt-claude: allow {tool}"'


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input", {}) or {}
    for rule in load_allow():
        if rule_matches(rule, tool, tool_input):
            sys.exit(0)

    snippet = json.dumps(tool_input, ensure_ascii=False)[:180]
    notify(f"[PERM] {tool} {snippet}{hint_for(tool, tool_input)}")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"{tool} non in allow list — chiesto a vjt via IRC, aspetta ok prima di ritentare",
        }
    }))


if __name__ == "__main__":
    main()
