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

### 2. Ensure the IRC bot is running

- Run `ps aux | grep -v grep | grep 'python3 -u bot.py'` to check for the bot process.
- **If already running**: skip the launch, note the PID, proceed to step 3. The existing bot keeps its IRC connection — no disconnect needed.
- **If not running**: launch it fresh via Monitor in the next step (Monitor will both start the process and stream its stdout).

### 3. Attach Monitor to bot event stream

The bot (`/home/vjt/code/IRC/vjt-claude/bot.py`) emits one line per structured event to stdout (format: `MSG vjt|other|trust <nick> <chan> <text>`, `JOIN <nick> <chan>`, `QUIT <nick> :<reason>`, `INVITE ...`, `CTCP ...`, `NOTICE ...`, `TRUST_DENIED ...`). The full raw IRC traffic lives in `bot.log`; stdout is the filtered event stream.

Two attachment patterns depending on what we found in step 2:

**A. Bot is NOT running** — launch via Monitor directly:

```
Monitor:
  description: "IRC bot events (msgs, invites, errors, trust)"
  persistent: true
  command: cd /home/vjt/code/IRC/vjt-claude && exec python3 -u bot.py
```

This makes Monitor both the supervisor AND the event stream. When the session ends the bot dies with Monitor, which is fine — `/start` in the next session will bring it back.

**B. Bot IS already running** — tail `bot.log` with a filter that reconstructs the event stream shape:

```
Monitor:
  description: "IRC bot events (msgs, invites, errors, trust)"
  persistent: true
  command: |
    tail -F -n 0 /home/vjt/code/IRC/vjt-claude/bot.log | \
      grep --line-buffered -E ' PRIVMSG | JOIN | PART | QUIT | INVITE | NOTICE '
```

This is lossier than pattern A (loses the structured `MSG vjt|other` trust classification), but doesn't require killing the running bot. Prefer pattern A on cold starts; pattern B for re-attaching after a session crash where the bot survived.

### 3.5. Ensure the sidecar services are up (systemd --user)

As of 2026-04-20 both sidecars are managed by systemd user services with `loginctl enable-linger vjt` set, so they auto-start at boot, auto-restart on crash, and survive Claude Code session boundaries.

- `vjt-claude-roll-counter.service` — tails `bot.log`, counts `::Roll()` / `::DABTime` ACTIONs + Italian blasphemies into `rolls.json`. See `project_roll_counter.md`.
- `vjt-claude-aup-watchdog.service` — tails active Claude Code jsonl, injects `/clear` into the `claude` tmux pane on AUP refusal / TURNS≥100 / idle≥10min, follows every `/clear` with a post-clear memory-scrub prompt. See `project_aup_watchdog.md`.

Unit files live in-repo at `/home/vjt/code/IRC/vjt-claude/systemd/` and are symlinked/copied into `~/.config/systemd/user/`. To check and (idempotently) start:

```bash
systemctl --user is-active vjt-claude-roll-counter.service vjt-claude-aup-watchdog.service
# if either is not "active":
systemctl --user start vjt-claude-roll-counter.service vjt-claude-aup-watchdog.service
```

Do NOT attach Monitor — both are silent by design (write to `rolls.json` / their own logs). Monitor stays reserved for the bot event stream. Structured logs available via `journalctl --user -u <service>`.

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
