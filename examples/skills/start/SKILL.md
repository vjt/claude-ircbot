---
name: start
description: Cold-boot the persistent IRC session — check the bot services, then run the warm bring-up (/resume). Run once by hand in a fresh `claude`; after a /clear the watchdog runs /resume instead.
user_invocable: true
---

# /start — cold boot

`<repo>` = this skill's base directory minus `/.claude/skills/start` (the session's working dir).

The cold path, run by hand once per `claude` launch. The hot path after every `/clear` is `/resume`
(the watchdog types it): everything both need lives there, so the two cannot drift.

## 1. Notes housekeeping (cold only)

If you keep a dated activity log (see `/resume`), move day-headings older than your retention
window (e.g. 14 days) to an archive file. Never read the whole log: `grep -n '^### '` for the
skeleton, then read only the days you move.

## 2. Services up

The bot (and the watchdog, if you run it) are systemd user services, so they outlive Claude Code:

```bash
systemctl --user is-active claude-ircbot.service aup-watchdog.service
# start whatever is not "active":
systemctl --user start claude-ircbot.service aup-watchdog.service
```

Unit names are yours: see `systemd/*.example`. One unit per bot instance if you run several networks.

## 3. Joins and greeting

Channel JOINs live in `bot.startup` and the bot replays them after identifying: do not send them from
here. When the Monitor shows the bot's own `JOIN <nick> <#chan>`, you may greet with one short line.

## 4. Warm bring-up

Run the steps of `.claude/skills/resume/SKILL.md` in order. On a cold boot they usually find nothing
to adopt and no work in flight; run them anyway, `/start` is safe to re-run.

## Sending to IRC

Commands go into the FIFO `<repo>/bot.send`, one per line: `SAY`, `ACT`, `NOTICE`, `JOIN`, `PART`,
`WHOIS`, `QUIT`, `RAW` (see README). Use `bot.say` for anything with quotes or UTF-8, it reads
the text from stdin:

```bash
<repo>/bot.say '#chan' <<'EOF'
hello, it's me
EOF
```

Start fresh (`claude`, not `claude --continue`): the session rebuilds from disk (CLAUDE.md, memory,
activity log), not from a compacted transcript. Before `/exit`, run `/close`.
