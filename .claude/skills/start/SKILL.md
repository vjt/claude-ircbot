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

### 3. Attach Monitor to bot event stream (only if none already attached)

**Check first, don't duplicate.** `/start` is idempotent and may be re-run after a watchdog-triggered `/clear` or a manual re-invocation. Two Monitors tailing the same log = every event fires twice (or 4×, etc — they stack). Check **both** sources before attaching — a pipeline surviving a scrub won't appear in TaskList:

1. **TaskList** — catches mid-session re-invoke. Look for a running task whose command contains `/home/vjt/code/IRC/vjt-claude/bot.log`. If found with status `running`/`in_progress`, reuse it and report its id.
2. **`pgrep -af "tail -F.*vjt-claude/bot.log"`** — catches a surviving pipeline from the pre-scrub session. After `/clear` the Monitor task registration is gone (new session's TaskList is empty) but the `tail … | grep …` shell pipeline is still alive and still delivering events here. If pgrep finds a hit, **skip attach** — adopt it, note `existing monitor adopted (pid <n>)` in the status line.

**If TaskList shows multiple live Monitors on the same log** (pre-existing duplication from earlier buggy runs or compaction-survived stragglers): stop all but the newest with `TaskStop`, then continue with the survivor. Report the cleanup in the status line.

**If TaskList shows a dead/stopped Monitor** (status `failed`, `stopped`, `completed`): start a fresh one. Don't try to resurrect the old id.

Only if all sources are clean: attach a fresh Monitor.

The bot writes raw IRC traffic to `/home/vjt/code/IRC/vjt-claude/bot.log` with direction markers (`<` = inbound from server, `>` = outbound to server). One persistent Monitor that tails bot.log and emits only the inbound events we care about:

```
Monitor:
  description: "IRC bot events (msgs, invites, errors, trust)"
  persistent: true
  timeout_ms: 3600000
  command: |
    tail -F -n 0 /home/vjt/code/IRC/vjt-claude/bot.log | \
      grep --line-buffered -E ' < :[^ ]+ (PRIVMSG|JOIN|PART|QUIT|NICK|INVITE|NOTICE|MODE|4[0-9][0-9]) '
```

**Filter semantics — important:**

- `' < :'` anchors on **inbound** lines only. Outbound (`' > '`) lines are my own IRC sends — echoing them back creates a self-confirmation feedback loop. Never tail outbound.
- The `:[^ ]+` group eats the `:nick!user@host` source prefix before the verb, so server-notice lines (`< PING :server`) which lack a source are intentionally excluded — they're noise.
- Verb alternation: PRIVMSG, JOIN, PART, QUIT, INVITE, NOTICE, MODE, plus `4XX` numeric error replies (401 No such nick/channel, 403 No such channel, 404 Cannot send, 432/433/437 nick errors, 442/443 channel-membership errors, 471/473/474/475 join failures, 482 need ops, etc.). MODE is in so I can track who had `+o` in-session (enables re-opping returning ops while vjt is away, per `project_vjt_proxy_on_away.md`). 4XX added 2026-05-01 after a `SAY vjt` (while he was away as `vjt\`zZzZ`) silently 401'd — without this, send-failures were invisible. TOPIC and 2xx/3xx/5xx numerics stay out — rarely actionable, bloats notifications.
- `--line-buffered` is mandatory on grep — without it, pipe buffering delays events by minutes.
- `tail -F` (capital F) survives log rotation. Use `-n 0` so we don't replay history on attach.

Do NOT attach Monitor to sidecar services — they're silent-on-success by design (logs via `journalctl`).

### 4. Greet on JOIN (auto-joins handled by bot.py)

Home-channel JOINs (`#sniffo`, `#olografix`) + `ChanServ INVITE #it-opers` are now declared in `/home/vjt/code/IRC/vjt-claude/bot.startup` and fired by the bot after NickServ identifies. **Do NOT re-send them from the skill** — redundant. See `project_bot_auth_and_startup.md`.

The skill still owns the **greet on own JOIN event** — when the Monitor shows `JOIN vjt-claude <chan>`, send one short line, channel-register appropriate:

- `#sniffo`, `#it-opers`, default → Porco Dio register (e.g. `SAY #sniffo porco dio raga, vjt-claude in sala`).
- `#olografix` → supercazzola, less blasphemy (e.g. `SAY #olografix tarapìa tapìoco come se fosse antani...`).

See `project_greet_on_join.md` in memory.

### 5. Pick up work-in-progress

A watchdog-triggered scrub means the prior session was likely mid-task. /clear wipes conversation context; disk state survives. Before idling, sweep three sources for pending work:

- **`git status --short`** — modified / untracked files = unfinished edits. Read the files, figure out what was in progress, finish it without asking (unless the next step is destructive).

- **bot.log — dedup BOTH directions before acting.** Inbound alone is a trap: /clear drops the conversation, but outbound on disk proves I already answered. **Always read inbound AND outbound interleaved, and read enough of it.**

  Default sweep — last 50 lines, both directions, cap 100:

  ```bash
  tail -50 /home/vjt/code/IRC/vjt-claude/bot.log | grep -E ' [<>] '
  ```

  (50 lines = the fresh tail right around the scrub boundary. Widen to 100 only if the 50-line window ends mid-exchange or if the /clear clearly came from TURNS≥100 after a dense burst. Don't go beyond 100 — old requests beyond the scrub horizon are stale by design; answering them now looks broken.)

  Algorithm for each inbound request addressed to me (`vjt-claude:` prefix, nick mention, or direct query):

  1. Note the timestamp `T` of the inbound line.
  2. Scan outbound (`' > PRIVMSG <chan>'` or `' > NOTICE <nick>'`) with timestamp `> T` in the same channel / to the same nick.
  3. If any outbound line plausibly answers the request (topic match, not just unrelated chatter), **treat as already-handled — do not reply again.**
  4. Only true gaps (inbound request with no matching outbound after) get a reply.

  Half-sent cut-off case: if the newest outbound to that chan is a partial / truncated line with no closing thought, then finish it. But a full prior reply = done, even if you'd word it differently now.

- **Prior session jsonl** — `ls -t ~/.claude/projects/-home-vjt-code-IRC-vjt-claude/*.jsonl | head -3`. If git + bot.log don't clarify, dump the tail of the most recent pre-clear jsonl to see the final assistant turn (what you were about to do) and the final user turn (what was asked).

The sweep runs unconditionally — even when nothing is pending, knowing the last channel activity sets context. Only the follow-up action is conditional: if WIP found, resume it; if not, idle quietly. Don't announce "nothing found" — silence is correct when nothing's pending. **When in doubt between replying and staying silent, stay silent** — a duplicate reply is worse than a missed one (Sonic can re-ping; a dupe makes vjt-claude look broken).

### 6. Report ready

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
