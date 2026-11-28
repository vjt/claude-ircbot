---
name: resume
description: Warm bring-up after a /clear (the watchdog types it) and the second half of /start — reseed today's notes, adopt or attach the event Monitors, sweep for work in flight, report.
user_invocable: true
---

# /resume — warm bring-up

`<repo>` = this skill's base directory minus `/.claude/skills/resume`.

Runs after every `/clear`, so it must stay short. `CLAUDE.md` and the memory index load by
themselves; today's notes and the live event streams do not: this reseeds them. Do it quietly.

## 1. Reseed today's notes

If you keep a dated activity log (e.g. `memory/project_activity_log.md`, one `### YYYY-MM-DD`
heading per day): add today's heading if missing, then read today's body in full. It is the only
way in-flight threads and pending items survive a `/clear`.

## 2. Event streams

**With watchd (recommended).** The [watchd](https://github.com/vjt/claude-watchd) plugin runs every
watch in `.claude/watchd.json` for the session's whole life: no 30-minute cap to re-arm, events wake an
idle session and reach a busy one between tool calls, and nothing to re-attach after `/clear`. Install
it once (`/plugin install watchd --marketplace vjt/claude-watchd`), copy `examples/watchd.json` to
`.claude/watchd.json` (one watch per bot instance). Here, only check it is up:

```bash
grep -lsxzF "WATCHD_DIR=$PWD" /proc/[0-9]*/environ | wc -l    # > 0: watchd owns the streams
systemctl --user is-active claude-ircbot.service
```

If it is up, skip the rest of this section.

**Without watchd: Monitors.** Each bot instance writes its own `bot.stdout.log`
(`bot.<name>.stdout.log` for a twin) and needs one Monitor. A `tail -F` pipeline survives `/clear`,
the Monitor registration does not:

```bash
pgrep -af "tail -F.*bot.stdout.log"
```

- A hit: adopt it, do not attach a second one.
- No hit: attach one Monitor per instance:
  ```
  Monitor: command: bash <repo>/examples/start-monitor.sh <repo>/bot.stdout.log
           persistent: true, timeout_ms: 3600000
           description: "IRC bot events"
  ```

## 3. Work in flight

```bash
git status --short
tail -n 50 <repo>/bot.log
```

- Modified or untracked files: unfinished edits, finish them (unless the next step is destructive).
- In `bot.log`, an inbound line addressed to you with no outbound answer after it in the same
  channel or query is a gap: answer it. If an answer plausibly went out, do not answer again.
  When in doubt, stay silent: a duplicate is worse than a miss.

## 4. Report

One line, e.g. `resumed — monitor adopted (pid N), services active, nothing in flight.`
