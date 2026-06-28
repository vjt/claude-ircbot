---
name: start
description: Cold-boot the vjt-claude IRC persistent session — archives >14d activity log, ensures bot.py + sidecars are running, greets on own JOIN, then runs the shared warm-bringup protocol (/resume). Run once manually when entering this working directory. The hot path (post-/clear) is /resume, NOT this.
user_invocable: true
---

# /start — cold-boot vjt-claude

Brings up the persistent IRC session from a fresh `claude` launch. **This is the cold path, run manually.** The post-`/clear` hot path is `/resume` (fired by the watchdog) — do not run `/start` there.

`/start` = **cold-only setup** (sections 1–3) **then the shared warm-bringup protocol** (section 4). The shared steps — reseed today's activity-log body, adopt/attach the Monitor, WIP sweep, report — live in **one place: `.claude/skills/resume/SKILL.md`**. `/start` does NOT restate them; it defers, so the two skills can't drift. Idempotent: safe to re-run mid-session.

## 1. Memory housekeeping — archive entries >14d (cold-only)

**Never full-read the activity log.** It can be hundreds of KB; a full Read burns input tokens for nothing. It is NOT auto-loaded (MEMORY.md holds only a one-line pointer), so this is the only place that burns it. Keep it cheap:

- `grep -nE '^### [0-9]{4}-[0-9]{2}-[0-9]{2}' memory/project_activity_log.md` — lists every `### YYYY-MM-DD` heading with its line number. Cheap skeleton.
- Compare each heading against `currentDate`.
- **Archive every day-heading + bullets older than 14 days.** Per `feedback_archive_dont_delete`, move them to `project_activity_log_archive.md` (read-on-demand, not in MEMORY.md) — don't discard. Only read the body of the days being trimmed (`Read` with `offset`/`limit` around their line range), never the whole file.

This >14d archive sweep is the one thing `/resume` deliberately skips (the log is read-on-demand, so an untrimmed log costs zero per-clear tokens — it's disk hygiene, not token hygiene). That's why it lives here, on the cold path, not in the hot path. The today's-heading reseed is NOT done here — it's part of the shared protocol (section 4), so it isn't done twice.

## 2. Ensure all three services are up — systemd --user (cold-only)

As of 2026-04-20 **bot.py plus both sidecars** run as systemd user services with `loginctl enable-linger vjt`, so they auto-start at boot, auto-restart on crash, decoupled from Claude Code lifecycle.

- `vjt-claude-bot.service` — the IRC bridge (`bot.py`). `RestartSec=30` + `StartLimitBurst=5/300s` to survive NickServ reconnect storms. `ExecStop` sends `QUIT :systemd stop` via FIFO for a clean disconnect.
- `vjt-claude-roll-counter.service` — tails `bot.log`, counts `::Roll()` / `::DABTime` ACTIONs + Italian blasphemies into `rolls.json`. See `project_roll_counter.md`.
- `vjt-claude-aup-watchdog.service` — tails the active Claude Code jsonl, injects `/clear` into the `claude` tmux pane on AUP refusal / TURNS cap / idle, follows every `/clear` with a `/resume` scrub prompt. See `project_aup_watchdog.md`.

Unit files live in-repo at `systemd/`, copied into `~/.config/systemd/user/`. Check and (idempotently) start:

```bash
systemctl --user is-active vjt-claude-bot.service vjt-claude-roll-counter.service vjt-claude-aup-watchdog.service
# any not "active":
systemctl --user start vjt-claude-bot.service vjt-claude-roll-counter.service vjt-claude-aup-watchdog.service
```

Nothing to mkfifo / nohup / pgrep — systemd owns all three. Structured logs via `journalctl --user -u <service>`.

## 3. Greet on own JOIN (cold-only)

Home-channel JOINs (`#sniffo`, `#olografix`) + `ChanServ INVITE #it-opers` are declared in `bot.startup` and fired by the bot after NickServ identifies. **Do NOT re-send them from the skill** — redundant. See `project_bot_auth_and_startup.md`.

The skill owns the **greet on own JOIN event** — when the Monitor shows `JOIN vjt-claude <chan>`, send one short line, channel-register appropriate:

- `#sniffo`, `#it-opers`, default → Porco Dio register (e.g. `SAY #sniffo porco dio raga, vjt-claude in sala`).
- `#olografix` → supercazzola, less blasphemy (e.g. `SAY #olografix tarapìa tapìoco come se fosse antani...`).

See `project_greet_on_join.md`.

## 4. Run the shared warm-bringup protocol (`/resume`)

Now execute the four steps documented in `.claude/skills/resume/SKILL.md`, in order:

1. **Reseed** today's activity-log heading body (append today's heading if missing, read its body in full — the mandatory seed for today's events / in-flight threads / `PENDING` markers).
2. **Adopt or attach the Monitor** (pgrep for the surviving `tail -F … bot.stdout.log` pipeline; adopt if present, else attach one fresh via `start-monitor.sh`).
3. **WIP sweep** — `git status --short` + bot.log inbound/outbound dedup; resume only true gaps.
4. **Report ready.**

On a true cold boot these usually find nothing to adopt (fresh attach) and no WIP (clean sweep) — but run them anyway: a manual idempotent re-run of `/start` mid-session may find a surviving Monitor or pending work. Follow `/resume`'s rules exactly (especially: when in doubt between replying and silence on the WIP sweep, stay silent; a dupe is worse than a miss). Read that file for the exact commands and edge cases — it is the single source of truth for these steps.

## Standing context

After bringup, the session is expected to:

- Follow the reply policy (`feedback_irc_reply_policy.md` — reply only when addressed or truly adding value).
- Apply channel registers (`feedback_olografix_tone.md`, `project_sniffo_supercazzola_on_join.md`, global CLAUDE.md's Porco Dio principle).
- Append to the activity log on meaningful events (decisions, shipped code, people appearing, plans formed). NOT on casual banter.
- Clown vjt on irssi ESC+N misfires (bare digit + "ops sorry" — `feedback_mock_esc_number_misfire.md`).

The parent `CLAUDE.md` covers all of this — read it if unsure.

## Sending to IRC

Commands go into the FIFO `/home/vjt/code/IRC/vjt-claude/bot.send`. Verbs: `SAY`, `ACT`, `NOTICE`, `JOIN`, `PART`, `WHOIS`, `QUIT`, `RAW`.

```bash
printf 'SAY #sniffo tarapìa tapìoco, porco dio!\n' > /home/vjt/code/IRC/vjt-claude/bot.send
```

## Relaunching from scratch

Always start **fresh** (plain `claude`, NOT `claude --continue`). The architecture bootstraps from disk — CLAUDE.md, MEMORY.md, activity log — not from conversational history. `--continue` would carry over lossy compaction summaries and defeat the sliding-window design. The jsonl archives preserve deep history regardless.

## Shutting down

Use `/close` for a graceful exit (supercazzola QUIT, Monitor stop, memory consistency check). Run it before `/exit` when you're actually killing Claude Code. If just leaving the session running, skip.
