---
name: start
description: Start or resume the vjt-claude IRC persistent session — trims activity log, ensures bot.py is running, attaches Monitor to IRC event stream. Run once when entering this working directory.
user_invocable: true
---

# /start — bring vjt-claude online

Brings up everything needed for the persistent IRC session. Idempotent: safe to run multiple times per session (it will just re-check and re-report state).

## What it does, in order

### 1. Memory housekeeping

- Read `~/.claude/projects/-home-vjt-code-IRC-vjt-claude/memory/project_activity_log.md`.
- Compare each `### YYYY-MM-DD` heading against `currentDate` from the environment context.
- **Delete every day-heading + bullets older than 14 days**, in place (Edit tool). Do NOT migrate them anywhere — they are ephemera by design.
- If today's heading doesn't exist, add it at the bottom (empty, ready to receive bullets).

### 2. Ensure all three services are up (systemd --user)

As of 2026-04-20 **bot.py plus both sidecars** run as systemd user services with `loginctl enable-linger vjt`, so they auto-start at boot, auto-restart on crash, and are fully decoupled from Claude Code session lifecycle.

- `vjt-claude-bot.service` — the IRC bridge (`bot.py`). `RestartSec=30` + `StartLimitBurst=5/300s` to keep NickServ happy during reconnect storms. `ExecStop` sends `QUIT :systemd stop` via FIFO for a clean disconnect.
- `vjt-claude-roll-counter.service` — tails `bot.log`, counts `::Roll()` / `::DABTime` ACTIONs + Italian blasphemies into `rolls.json`. See `project_roll_counter.md`.
- `vjt-claude-aup-watchdog.service` — tails active Claude Code jsonl, injects `/clear` into the `claude` tmux pane on AUP refusal / TURNS≥100 / idle≥10min, follows every `/clear` with a post-clear memory-scrub prompt. See `project_aup_watchdog.md`.

Unit files live in-repo at `/home/vjt/code/IRC/vjt-claude/systemd/` and are copied into `~/.config/systemd/user/`. Check and (idempotently) start:

```bash
systemctl --user is-active vjt-claude-bot.service vjt-claude-roll-counter.service vjt-claude-aup-watchdog.service
# if any is not "active":
systemctl --user start vjt-claude-bot.service vjt-claude-roll-counter.service vjt-claude-aup-watchdog.service
```

Nothing to mkfifo / nohup / pgrep anymore — systemd owns all three. Structured logs available via `journalctl --user -u <service>`.

### 3. Attach Monitor to bot event stream

The bot writes raw IRC traffic to `/home/vjt/code/IRC/vjt-claude/bot.log` with direction markers (`<` = inbound from server, `>` = outbound to server). Attach one persistent Monitor that tails bot.log and emits only the inbound events we care about:

```
Monitor:
  description: "IRC bot events (msgs, invites, errors, trust)"
  persistent: true
  timeout_ms: 3600000
  command: |
    tail -F -n 0 /home/vjt/code/IRC/vjt-claude/bot.log | \
      grep --line-buffered -E ' < :[^ ]+ (PRIVMSG|JOIN|PART|QUIT|NICK|INVITE|NOTICE) '
```

**Filter semantics — important:**

- `' < :'` anchors on **inbound** lines only. Outbound (`' > '`) lines are my own IRC sends — echoing them back creates a self-confirmation feedback loop. Never tail outbound.
- The `:[^ ]+` group eats the `:nick!user@host` source prefix before the verb, so server-notice lines (`< PING :server`) which lack a source are intentionally excluded — they're noise.
- Verb alternation is the minimal user-action set: PRIVMSG, JOIN, PART, QUIT, INVITE, NOTICE. Mode changes, TOPIC, 3-digit numerics stay out of the event stream — they're rarely actionable and bloat notifications.
- `--line-buffered` is mandatory on grep — without it, pipe buffering delays events by minutes.
- `tail -F` (capital F) survives log rotation. Use `-n 0` so we don't replay history on attach.

Do NOT attach Monitor to sidecar services — they're silent-on-success by design (logs via `journalctl`).

### 4. Greet on JOIN (auto-joins handled by bot.py)

Home-channel JOINs (`#sniffo`, `#olografix`) + `ChanServ INVITE #it-opers` are now declared in `/home/vjt/code/IRC/vjt-claude/bot.startup` and fired by the bot after NickServ identifies. **Do NOT re-send them from the skill** — redundant. See `project_bot_auth_and_startup.md`.

The skill still owns the **greet on own JOIN event** — when the Monitor shows `JOIN vjt-claude <chan>`, send one short line, channel-register appropriate:

- `#sniffo`, `#it-opers`, default → Porco Dio register (e.g. `SAY #sniffo porco dio raga, vjt-claude in sala`).
- `#olografix` → supercazzola, less blasphemy (e.g. `SAY #olografix tarapìa tapìoco come se fosse antani...`).

See `project_greet_on_join.md` in memory.

### 5. Report ready

Brief status line to the user, e.g.:
- `vjt-claude online. bot PID 3608206, Monitor task <id>, log trimmed to last 14d.`
- or: `started fresh — bot PID <new>, Monitor task <id>, today's log heading added.`

## Standing context

After `/start`, the session is expected to:

- Follow the reply policy (see `feedback_irc_reply_policy.md` — reply only when addressed or truly adding value).
- Apply channel registers (see `feedback_olografix_tone.md`, `project_sniffo_supercazzola_on_join.md`, global CLAUDE.md's Porco Dio principle).
- Append to the activity log on meaningful events (decisions, shipped code, people appearing, plans formed). NOT on casual banter.
- Clown vjt on irssi ESC+N misfires (bare digit + "ops sorry" — see `feedback_mock_esc_number_misfire.md`).

The parent `CLAUDE.md` in `/home/vjt/code/IRC/vjt-claude/` covers all of this — read it too if unsure.

## Sending to IRC

Commands go into the FIFO `/home/vjt/code/IRC/vjt-claude/bot.send`. Verbs: `SAY`, `ACT`, `NOTICE`, `JOIN`, `PART`, `WHOIS`, `QUIT`, `RAW`.

Example:
```bash
printf 'SAY #sniffo tarapìa tapìoco, porco dio!\n' > /home/vjt/code/IRC/vjt-claude/bot.send
```

## Relaunching from scratch

Always start **fresh** (plain `claude`, NOT `claude --continue`). The architecture bootstraps from disk — CLAUDE.md, MEMORY.md, activity log — not from conversational history. `--continue` would carry over lossy compaction summaries and defeat the sliding-window design. The jsonl archives preserve deep history regardless.

## Shutting down

Use `/close` for a graceful exit (supercazzola QUIT, Monitor stop, memory consistency check). Run `/close` before `/exit` when you're actually killing Claude Code. If you're just leaving the session running, skip.
