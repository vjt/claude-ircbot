#!/usr/bin/env bash
# Tail vjt-claude bot's curated event stream for the /start skill's Monitor.
#
# Source = bot.stdout.log (bot.py emit() output, already parses TRUSTED/UNTRUSTED
# verdicts on every chat line). bot.log is raw socket bytes — never tail that
# for the live decision stream (vjt 2026-05-06 audit).
#
# stdbuf -oL forces tail's stdout line-buffered. Without it, tail block-buffers
# its pipe writes and the Monitor delivers the penultimate line instead of the
# latest one (vjt 2026-05-07: "you are not reading the last line, you are
# reading the penultimate one").
#
# grep filter selects only actionable verbs. bot.stdout.log also emits
# diagnostics (CONNECTED, FIFO_READY, TLS_OK, STARTUP_*, TRUST_LOADED,
# VERIFIED, WHOIS_FIRED, NS_IDENTIFY_SENT) that are not worth waking Claude.
#
# tail -F (capital F) survives log rotation; -n 0 skips replaying history on
# attach.

# emit() prefixes each line with [HH:MM] (vjt 2026-06-07, time anchor) — the
# optional `\[..:..\] ` group below tolerates it while keeping the verb anchor.
stdbuf -oL tail -F -n 0 /home/vjt/code/IRC/vjt-claude/bot.stdout.log | \
  grep --line-buffered -E '^(\[[0-9]{2}:[0-9]{2}\] )?(MSG|JOIN|PART|QUIT|NICK_CHANGE|INVITE|NOTICE|KICK|CTCP|IDLE|IRC_ERROR|TRUST_DENIED|NICK_ERROR|AUTH_ERROR|NS_IDENTIFY_FAIL|SERVER_ERROR|CMD_ERROR) '
