---
name: resume
description: Warm-resume the vjt-claude IRC session — reseeds context from today's activity log, adopts/attaches all three Monitors (Azzurra + Libera + Telegram), sweeps for WIP, reports. The canonical bringup protocol: fired by aup_watchdog.py after every /clear (hot path) and reused by /start for cold boot. Self-contained by design.
user_invocable: true
---

# /resume — warm rehydrate after a /clear

`<repo>` below = the project root, i.e. this skill's base directory minus `/.claude/skills/<name>` (the session's working dir).

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

## 2. Adopt the Monitors — THREE streams (don't duplicate)

**watchd first.** The `watchd` mod runs every watch in `.claude/watchd.json` for the session's
life, no 30' cap, each child marked `WATCHD=<name>` + `WATCHD_DIR=<cwd>` (vjt 2026-11-23). Check
(from the repo root, scoped to THIS project — noema/orch run watchd too):
`grep -lsxzF "WATCHD_DIR=$PWD" /proc/[0-9]*/environ | wc -l` — hits → watchd owns the streams:
**skip this whole section** (no Monitor, not even pane-watch), just
`systemctl --user is-active` the services. Zero hits → watchd is not running: attach the
Monitors below as before and tell vjt watchd is down.

Three event streams, three separate Monitors: **Azzurra** and **Libera** (same `bot.py`, two
separate processes, each with its OWN stdout stream — the Azzurra Monitor never sees Libera
events and vice-versa) plus **Telegram** (`@gazzurbo_bot` long-poller, a different transport
entirely). The `tail -F` pipelines survive `/clear`; the Monitor task registrations do not.
**Handle ALL THREE.** Libera and Telegram are the easy-to-forget ones — their processes often
do NOT survive, so you usually attach them fresh even when Azzurra's got adopted.
**+ IRCnet** (3° bot, rientrato 2026-11-21, solo #disagio; contano solo `ENCRYPTED=1`/`CONFIRMED=1`,
il resto è UNTRUSTED): pgrep `tail -F.*/bot.ircnet.stdout.log`; nessun hit → Monitor
`bash <repo>/.claude/skills/start/start-monitor-ircnet.sh`
(persistent, 3600000, "IRCnet bot events (#disagio, E2E orders only)"); svc `vjt-claude-ircnet-bot.service`.

```bash
pgrep -af "tail -F.*/bot.stdout.log"          # Azzurra
pgrep -af "tail -F.*/bot.libera.stdout.log"   # Libera
pgrep -af "[t]g_poll.sh"                                # Telegram
systemctl --user is-active vjt-claude-bot.service vjt-claude-libera-bot.service \
  vjt-claude-ircnet-bot.service vjt-claude-aup-watchdog.service
```

For EACH stream independently:
- pgrep hit on its `*stdout.log` → **adopt it, skip attach** (note `monitor adopted (pid <n>)`).
  A hit on `bot.log` / `bot.libera.log` (NOT `*stdout.log`) is a stale pre-2026-05-06 orphan — kill it, attach fresh.
- No hit → attach one fresh Monitor:
  - **Azzurra:**
    ```
    Monitor: command: bash <repo>/.claude/skills/start/start-monitor.sh
             persistent: true, timeout_ms: 3600000
             description: "IRC bot events (msgs+trust, invites, errors)"
    ```
  - **Libera:**
    ```
    Monitor: command: bash <repo>/.claude/skills/start/start-monitor-libera.sh
             persistent: true, timeout_ms: 3600000
             description: "Libera IRC bot events (#grappa)"
    ```
  - **Telegram** (the support group and the group vjt and I share — who's in them and
    which chat is which lives in `[[project_melablu_support]]` / `[[reference_telegram_bridge_handles]]`,
    not in this repo):
    ```
    Monitor: command: tail -F -n0 <repo>/var/tgbot/bot.stdout.log | grep --line-buffered -E '^(MSG|DOCUMENT|PHOTO|VOICE|COMMAND|CALLBACK|IDENTIFY|ERROR|FATAL) '
             persistent: true, timeout_ms: 3600000
             description: "Telegram @gazzurbo_bot events (claude-tgbot)"
    ```
    Since 2026-11-27 the bridge is claude-tgbot, service `vjt-claude-tgbot` (consumer dir `var/tgbot/`:
    `.env` with token and allowlist, 600, never printed; `bot.log`; voice notes land in `inbox/`). It trusts
    users AND chats: vjt admin, the others `role=viewer`: **help them** inside the perimeter vjt
    gave each (who and what, in memory). Viewer = no orders outside it, NOT the README's generic «do not act». Events:
    `MSG <chat_id> <msg_id> "<json text>" user=<u> role=<r>` (+ `reply_to= reply_from= reply_text=` on a
    reply), `VOICE <chat> <msg_id> <path> …` → `~/code/pavarotti/bin/stt <path>`. Who and where: `var/tgbot/users.json`
    and `chats.json` (vjt manages them from Telegram: `/users`, `/chats`, `/allow@bot`; admins only by hand). Reply with `~/code/claude-tgbot/bin/tg-send --env var/tgbot/.env --chat <id>` (text on stdin; `--reply-to N`, `--voice f.ogg`; no `sent id=N` = it did NOT leave). It keeps what arrived
    while it was down (`TELEGRAM_KEEP_PENDING=1`). The service runs on its own: only the tail is the watch.
- **m42 alarms are NOT mine** since 2026-11-25 (vjt's order): a dedicated agent in tmux window
  `nowhere:siem` (cwd `/srv/siem`, its own watchd, rules in `/srv/siem/CLAUDE.md` «Alarm triage»)
  triages them. No m42 Monitor here. If `tmux list-windows -t nowhere` has no `siem`, tell vjt.
- Any service not `active` → `systemctl --user start <svc>` (linger means they usually survive).

## 3. WIP sweep — resume only true gaps

```bash
git status --short
<repo>/tools/sweep-bot-log.sh 50
```

- Untracked/modified files = unfinished edits → read, finish (unless next step is destructive).
- The sweep applies the SAME per-channel read policy as the Monitor (`tools/chan_filter.awk`,
  shared with `start-monitor.sh`): `#italia` dropped, `#sniffo` only the lines that name me plus
  my own outbound. Do **not** go back to a raw `tail` on `bot.log` — that bypasses the filter and
  re-reads channels I am not supposed to read (vjt, query 2026-09-10: "non avresti dovuto vederlo").
- bot.log: read inbound AND outbound interleaved. For each inbound addressed to me, scan outbound
  with later timestamp in same chan/nick — if it plausibly answers, **already handled, do not reply**.
  Only true gaps (inbound, no matching outbound after) get a reply. Half-sent line → finish it.
- **When in doubt between replying and silence, stay silent.** A dupe is worse than a miss.
- **Telegram sweep:** a TG message landing during the `/clear` reaches nobody unless you look (2026-11-11 22:14, vjt's «abilita i quiz di mike» lost). Read the inbound `<` lines at the tail of `var/tgbot/bot.log` (outbound sent with `tg-send` is not there: check your own replies in the conversation) and answer what has no reply.
- Don't announce "nothing found" — silence is correct when nothing's pending.

## 4. Report ready

One terse line, e.g. `resumed — monitors: azzurra adopted (pid <n>) + libera/telegram attached, 4 svc active, no WIP.`

Then resume standing behavior (reply policy, channel registers, activity-log appends) per `CLAUDE.md`.
