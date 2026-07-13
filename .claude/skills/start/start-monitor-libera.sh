#!/usr/bin/env bash
# Tail the vjt-claude LIBERA.CHAT bot's curated event stream for a Monitor.
#
# Twin of start-monitor.sh, re-pointed at the Libera instance's stdout log
# (bot.libera.stdout.log — set via StandardOutput in the vjt-claude-libera unit).
# Same grep filter / same stdbuf line-buffering rationale (see start-monitor.sh).
#
# The Libera bot is a SECOND process (same bot.py, own nick/FIFO/logs) — so it
# needs its OWN Monitor; the Azzurra Monitor never sees these events.

stdbuf -oL tail -F -n 0 /home/vjt/code/IRC/vjt-claude/bot.libera.stdout.log | \
  grep --line-buffered -E '^(\[[0-9]{2}:[0-9]{2}\] )?(MSG|JOIN|PART|QUIT|NICK_CHANGE|INVITE|NOTICE|KICK|CTCP|IDLE|IRC_ERROR|TRUST_DENIED|NICK_ERROR|AUTH_ERROR|NS_IDENTIFY_FAIL|SERVER_ERROR|CMD_ERROR) '
