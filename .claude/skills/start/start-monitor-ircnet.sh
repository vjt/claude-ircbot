#!/usr/bin/env bash
# Tail the vjt-claude IRCNET bot's curated event stream for a Monitor.
#
# Twin of start-monitor.sh / start-monitor-libera.sh, re-pointed at the IRCnet
# instance's stdout log (bot.ircnet.stdout.log — set via StandardOutput in the
# vjt-claude-ircnet-bot unit). Same grep filter / same stdbuf line-buffering
# rationale (see start-monitor.sh).
#
# The IRCnet bot is a THIRD process (same bot.py, own nick/FIFO/logs) — so it
# needs its OWN Monitor; the Azzurra and Libera Monitors never see these events.
#
# Expect every MSG here to arrive UNTRUSTED: IRCnet runs no services, so nobody
# can be verified and bot.trust.ircnet is empty by design.

stdbuf -oL tail -F -n 0 /home/vjt/code/IRC/vjt-claude/bot.ircnet.stdout.log | \
  grep --line-buffered -E '^(\[[0-9]{2}:[0-9]{2}\] )?(MSG|JOIN|PART|QUIT|NICK_CHANGE|INVITE|NOTICE|KICK|CTCP|IDLE|IRC_ERROR|TRUST_DENIED|NICK_ERROR|AUTH_ERROR|NS_IDENTIFY_FAIL|SERVER_ERROR|CMD_ERROR) '
