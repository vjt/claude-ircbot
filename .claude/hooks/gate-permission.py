#!/usr/bin/env python3
"""PreToolUse gate — wholesale.

Fires on every tool (matcher `*` wired in .claude/settings.json). Allow-list
is the union of `permissions.allow` from both `.claude/settings.json`
(checked-in, generic rules) and `.claude/settings.local.json` (gitignored,
host-specific paths + WebFetch domains). No match → fast deny + IRC NOTICE
to the operator so a blocked call surfaces on IRC instead of CC's silent prompt.

Who gets that notice comes from `.claude/gate.local.json` (gitignored), read
on every call like the rules:
    {"operator": "<nick to notify>", "bot_nick": "<the bot's own nick>"}
No operator → no IRC notice. No bot_nick → the suggested reply carries no
`<bot_nick>:` prefix. Neither changes what is allowed or denied.

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
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent.parent  # .../.claude/
SETTINGS_FILES = [HERE / "settings.json", HERE / "settings.local.json"]
LOCAL_CONF = HERE / "gate.local.json"
BOT_FIFO = HERE.parent / "bot.send"


def load_local():
    """(operator, bot_nick) from gate.local.json; "" for whatever is missing.

    A value that is not a single IRC token counts as missing: it is spliced
    into a FIFO command line, where a space or a newline would change the
    command."""
    try:
        conf = json.loads(LOCAL_CONF.read_text())
    except (OSError, ValueError):
        conf = {}
    if not isinstance(conf, dict):
        conf = {}

    def nick(key):
        v = conf.get(key)
        return v if isinstance(v, str) and re.fullmatch(r"[^\s,:]+", v) else ""

    return nick("operator"), nick("bot_nick")


def _load_rules(key):
    out = []
    for p in SETTINGS_FILES:
        try:
            out.extend(json.loads(p.read_text()).get("permissions", {}).get(key, []))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return out


def load_allow():
    return _load_rules("allow")


def load_deny():
    """`permissions.deny` from the same files. CC reads these natively only at
    boot; this session runs for months, so the gate enforces them itself — and
    deny always wins over allow (see main())."""
    return _load_rules("deny")


def notify(operator, text):
    """Non-blocking FIFO write — silent if bot is down (no reader) or if no
    operator is configured."""
    if not operator:
        return
    try:
        fd = os.open(str(BOT_FIFO), os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, f"SAY {operator} {text}\n".encode())
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
        raw = tool_input.get("file_path", "")
        # Match raw path AND its realpath: the memory dir is reached via a
        # symlink (~/.claude/projects/.../memory -> repo/memory), and the
        # injected memory-prompt hands us the symlink form. Without this, a
        # rule written against the canonical path misses, the gate denies,
        # and the self-allow cascade edits settings.local.json — which trips
        # CC's native settings-edit prompt and blocks the headless session.
        cands = {raw}
        if raw:
            cands.add(os.path.realpath(raw))
        return any(fnmatch.fnmatchcase(c, inner) for c in cands)

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


def hint_for(tool, tool_input, bot_nick=""):
    to = f"{bot_nick}: " if bot_nick else ""
    if tool == "WebFetch":
        h = urlparse(tool_input.get("url", "")).hostname or "?"
        return f' — "{to}allow WebFetch(domain:{h})"'
    if tool in ("Read", "Edit", "Write", "NotebookEdit"):
        p = tool_input.get("file_path", "?")
        return f' — "{to}allow {tool}({p})"'
    if tool == "Bash":
        c = (tool_input.get("command", "") or "").split()[0] or "?"
        return f' — "{to}allow Bash({c} *)"'
    if tool == "Skill":
        n = tool_input.get("skill", "?")
        return f' — "{to}allow Skill({n})"'
    return f' — "{to}allow {tool}"'


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input", {}) or {}
    operator, bot_nick = load_local()

    # Deny first, and deny always wins: an allow-glob wide enough to cover a
    # sensitive file (bot.trust inside Edit(/home/alice/code/**)) must not be
    # able to grant it back.
    for rule in load_deny():
        if rule_matches(rule, tool, tool_input):
            notify(operator, f"[PERM-DENY] {tool} bloccato da regola deny: {rule}")
            who = f"di {operator}" if operator else "dell'operatore"
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"vietato dalla regola deny: {rule} — serve il via esplicito {who}",
                }
            }))
            return

    for rule in load_allow():
        if rule_matches(rule, tool, tool_input):
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": f"matched rule: {rule}",
                }
            }))
            return

    snippet = json.dumps(tool_input, ensure_ascii=False)[:180]
    notify(operator, f"[PERM] {tool} {snippet}{hint_for(tool, tool_input, bot_nick)}")
    if operator:
        why = f"chiesto a {operator} via IRC, aspetta ok prima di ritentare"
    else:
        why = "serve una regola allow in .claude/settings.local.json"
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"{tool} non in allow list — {why}",
        }
    }))


if __name__ == "__main__":
    main()
