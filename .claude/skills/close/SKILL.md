---
name: close
description: Cleanly shut down the vjt-claude IRC session — graceful bot QUIT, final activity-log flush, memory consistency check, Monitor stop. Run before /exit when you plan to relaunch Claude Code later.
user_invocable: true
---

# /close — shut vjt-claude down cleanly

Mirror of `/start`. Run this **before** `/exit` when you're going to relaunch Claude Code from this dir later. Graceful: morph and the crew see a clean leave message instead of a hard timeout.

Skip this skill if you're leaving the session running — it's only needed when you're actually going to kill Claude Code.

## What it does, in order

### 1. Final activity-log flush

- Read `~/.claude/projects/-home-vjt-code-IRC-vjt-claude/memory/project_activity_log.md` and check today's heading.
- **If meaningful events from this session aren't logged yet**, append them now under today's heading. Apply the same filter as during the session (decisions / ships / plans / surprises — not banter).
- If today's heading is empty and nothing notable happened, that's fine — leave it as a placeholder. Don't invent content.

### 2. Graceful IRC quit with a contextual supercazzola

The QUIT message should nod at whatever the crew was just talking about. Mascetti register, one line, <200 chars, Italian, register-matched to the channel the last chatter was in (#olografix = lighter on blasphemy, elsewhere = Porco Dio fine).

Procedure:

1. Read the last ~30 lines of `/home/vjt/code/IRC/vjt-claude/bot.log` (`tail -n 30`). Extract the dominant topic/phrase from the most recent crew exchanges — what's on everyone's lips right this minute?
2. Craft a supercazzola QUIT message that fuses the topic with Mascetti-style verbal nonsense. Aim for: topic-nod + `come se fosse antani / tarapìa tapìoco / scappellamento / prematurata` construction + closing blasphemy if register allows.
3. Fire it:
   ```bash
   printf 'QUIT :<your supercazzola here>\n' > /home/vjt/code/IRC/vjt-claude/bot.send
   ```

Examples of the shape (don't use these verbatim — regenerate from actual current chatter):

- If crew was discussing lost BTC wallets: `QUIT :come la tarapìa tapìoco del wallet perduto, scappellamento a destra del blocchetto genesi, alla prossima reboot. porco dio`
- If crew was in #olografix talking tax: `QUIT :antani dichiarativo con prematurata RW, come se fosse TUIR scappellato a destra. alla prossima`
- If crew was laughing about Magnotta: `QUIT :la lavatrice antani, quattrocentottantamilalire di prematurata, m'iscrivo al reboot!!`

Wait ~2 seconds after firing for the QUIT to flush to the network before step 3 (otherwise Monitor may kill the bot mid-write and the QUIT message is lost).

If `bot.log` tail shows no recent chatter (channel dead), fall back to a pure-Mascetti QUIT without a topic nod: `QUIT :come se fosse antani, la reboot con scappellamento a destra, alla prossima. porco dio.`

### 3. Stop the Monitor

Use `TaskStop` on the IRC event monitor task — this ends the tail-of-`bot.log` stream cleanly.

The bot itself is a systemd user service now (`vjt-claude-bot.service`) and is **not** coupled to the Monitor. The FIFO-`QUIT` in step 2 makes the bot disconnect gracefully and exit; systemd then sees it as a normal stop. Do NOT `systemctl --user stop` as well — the FIFO QUIT has already done it, and stacking the two races the reconnect window. Same applies to the sidecars: leave them alone, they'll keep running until next reboot / next `/start` cycle.

### 4. Memory consistency check

Quick sanity pass to avoid starting the next session with half-written state:

- Read `MEMORY.md` and confirm every `- [Title](file.md)` pointer resolves — `ls` each file listed.
- For each memory file, confirm the frontmatter `name/description/type` is present and unbroken.
- If anything is inconsistent, fix it now. Do not leave corrupted state for the next session to stumble over.

### 5. Report

Print a one-liner like:
- `vjt-claude closed. Bot QUIT sent, Monitor stopped, activity log has N bullets for today, MEMORY.md has M entries. Safe to /exit.`

Then the user runs `/exit`.

## What NOT to do

- Do NOT delete the memory dir or rename it. Its canonical path is `~/.claude/projects/-home-vjt-code-IRC-vjt-claude/` and must persist untouched between sessions.
- Do NOT `pkill` Claude Code itself — let the user /exit normally so the transcript gets saved to the jsonl archive.
- Do NOT promote transient chat into memory just because you're closing. Promotion rule still applies: only if it will matter >14 days.
