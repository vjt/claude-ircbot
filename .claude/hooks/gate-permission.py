#!/usr/bin/env python3
"""PreToolUse gate: auto-deny tools not in allow list, notify vjt on IRC.

Scope: WebFetch, WebSearch (the tools that commonly block on new domains).
Other tools fall through to CC's native prompt (still blocking, but rare for
Read/Grep/Glob/Bash/Edit which have their own broad allows).

Allow-rule syntax honored here (subset of CC's permission grammar):
    WebFetch                             — bare tool = allow all
    WebFetch(domain:host)                — exact host
    WebFetch(domain:*.host)              — subdomain wildcard
    WebSearch                            — bare
"""
import json, sys
from pathlib import Path
from urllib.parse import urlparse

SETTINGS = Path("/home/vjt/code/IRC/vjt-claude/.claude/settings.local.json")
BOT_FIFO = Path("/home/vjt/code/IRC/vjt-claude/bot.send")
GATED = ("WebFetch", "WebSearch")


def notify(text):
    try:
        with BOT_FIFO.open("w") as f:
            f.write(f"NOTICE vjt {text}\n")
    except Exception:
        pass


def host_matches(pattern, host):
    if pattern.startswith("*."):
        suf = pattern[1:]
        return host == pattern[2:] or host.endswith(suf)
    return host == pattern


def rule_matches(rule, tool, tool_input):
    if rule == tool:
        return True
    prefix = f"{tool}("
    if not rule.startswith(prefix) or not rule.endswith(")"):
        return False
    inner = rule[len(prefix):-1]
    if tool == "WebFetch" and inner.startswith("domain:"):
        host = urlparse(tool_input.get("url", "")).hostname or ""
        return host_matches(inner[7:], host)
    return False


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    tool = data.get("tool_name", "")
    if tool not in GATED:
        sys.exit(0)
    tool_input = data.get("tool_input", {}) or {}
    try:
        allow = json.loads(SETTINGS.read_text()).get("permissions", {}).get("allow", [])
    except Exception:
        allow = []
    for rule in allow:
        if rule_matches(rule, tool, tool_input):
            sys.exit(0)
    snippet = json.dumps(tool_input, ensure_ascii=False)[:180]
    host_hint = ""
    if tool == "WebFetch":
        h = urlparse(tool_input.get("url", "")).hostname or "?"
        host_hint = f' — per sbloccare dimmi: "vjt-claude: allow WebFetch(domain:{h})"'
    notify(f"[PERM] {tool} {snippet}{host_hint}")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"{tool} non in allow list di settings.local.json — chiesto a vjt su IRC, aspetta risposta prima di ritentare",
        }
    }))


if __name__ == "__main__":
    main()
