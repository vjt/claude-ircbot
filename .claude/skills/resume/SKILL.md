---
name: resume
description: Warm-resume the vjt-claude IRC session — reseeds context from today's activity log, adopts/attaches BOTH networks' Monitors (Azzurra + Libera), sweeps for WIP, reports. The canonical bringup protocol: fired by aup_watchdog.py after every /clear (hot path) and reused by /start for cold boot. Self-contained by design.
user_invocable: true
---

# /resume — warm rehydrate after a /clear

The watchdog fires this after every `/clear` — this is the **hot path**, run thousands of
times, so it must stay lean. **This file is the canonical bringup protocol and is
self-contained: never pull in `/start` to "understand" a step — doing so reloads the heavy
cold-boot skill on every clear and burns the tokens this split exists to save.** Everything
needed to execute is right here. (Cold boot runs `/start`, which does its cold-only setup
and then defers back to these same four steps.)

After `/clear`, `CLAUDE.md` + `MEMORY.md` auto-load; the activity-log body and the live
bot state do NOT. These steps reseed that. Do it silently — no narration unless WIP found.

## 1. Reseed context from the activity log

```bash
grep -nE '^### [0-9]{4}-[0-9]{2}-[0-9]{2}' memory/project_activity_log.md
```

- If today's `### YYYY-MM-DD` heading (vs `currentDate`) is missing, append it at the bottom.
- **Read today's heading body in full** (`Read` with `offset` at today's heading line → EOF).
  This is the mandatory seed: today's events, in-flight threads, `PENDING`/`DA FARE` markers
  reach you ONLY here. Skipping it = waking blind. If today's body is thin, also read yesterday's.
- Trim is NOT done here — it's disk hygiene, not token hygiene (the log is read-on-demand, never
  auto-loaded, so an untrimmed log costs zero per-clear tokens). `/start` owns the >14d archive sweep.

## 2. Adopt the Monitors — BOTH networks (don't duplicate)

Two bots run: **Azzurra** and **Libera** (same `bot.py`, separate processes, each with its
OWN stdout event stream and its OWN Monitor — the Azzurra Monitor never sees Libera events and
vice-versa). The `tail -F` pipelines survive `/clear`; the Monitor task registrations do not.
**Handle BOTH.** The Libera one is the easy-to-forget one — its tail often does NOT survive, so
you usually have to attach it fresh even when Azzurra's got adopted.

```bash
pgrep -af "tail -F.*vjt-claude/bot.stdout.log"          # Azzurra
pgrep -af "tail -F.*vjt-claude/bot.libera.stdout.log"   # Libera
systemctl --user is-active vjt-claude-bot.service vjt-claude-libera-bot.service \
  vjt-claude-roll-counter.service vjt-claude-aup-watchdog.service
```

For EACH network independently:
- pgrep hit on its `*stdout.log` → **adopt it, skip attach** (note `monitor adopted (pid <n>)`).
  A hit on `bot.log` / `bot.libera.log` (NOT `*stdout.log`) is a stale pre-2026-05-06 orphan — kill it, attach fresh.
- No hit → attach one fresh Monitor:
  - **Azzurra:**
    ```
    Monitor: command: bash /home/vjt/code/IRC/vjt-claude/.claude/skills/start/start-monitor.sh
             persistent: true, timeout_ms: 3600000
             description: "IRC bot events (msgs+trust, invites, errors)"
    ```
  - **Libera:**
    ```
    Monitor: command: bash /home/vjt/code/IRC/vjt-claude/.claude/skills/start/start-monitor-libera.sh
             persistent: true, timeout_ms: 3600000
             description: "Libera IRC bot events (#grappa)"
    ```
- Any service not `active` → `systemctl --user start <svc>` (linger means they usually survive).

## 3. WIP sweep — resume only true gaps

```bash
git status --short
tail -50 /home/vjt/code/IRC/vjt-claude/bot.log | grep -E ' [<>] '
```

- Untracked/modified files = unfinished edits → read, finish (unless next step is destructive).
- bot.log: read inbound AND outbound interleaved. For each inbound addressed to me, scan outbound
  with later timestamp in same chan/nick — if it plausibly answers, **already handled, do not reply**.
  Only true gaps (inbound, no matching outbound after) get a reply. Half-sent line → finish it.
- **When in doubt between replying and silence, stay silent.** A dupe is worse than a miss.
- Don't announce "nothing found" — silence is correct when nothing's pending.

## 4. Report ready

One terse line, e.g. `resumed — monitors: azzurra adopted (pid <n>) + libera attached, 4 svc active, no WIP.`

Then resume standing behavior (reply policy, channel registers, activity-log appends) per `CLAUDE.md`.
