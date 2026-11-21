#!/usr/bin/env bash
# Tail the vjt-claude LIBERA.CHAT bot's curated event stream for a Monitor.
#
# Twin of start-monitor.sh, re-pointed at the Libera instance's stdout log
# (bot.libera.stdout.log — set via StandardOutput in the vjt-claude-libera unit).
# Same grep filter / same stdbuf line-buffering rationale (see start-monitor.sh).
#
# The Libera bot is a SECOND process (same bot.py, own nick/FIFO/logs) — so it
# needs its OWN Monitor; the Azzurra Monitor never sees these events.

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
# Per-channel read policy is deployment config: tools/chan_filter.awk is local
# and untracked. Without it every event passes.
chan_filter() {
  if [ -f "$ROOT/tools/chan_filter.awk" ]; then awk -f "$ROOT/tools/chan_filter.awk"; else cat; fi
}

stdbuf -oL tail -F -n 0 "$ROOT/bot.libera.stdout.log" | \
  grep --line-buffered -E '^(\[[0-9]{2}:[0-9]{2}\] )?(MSG|JOIN|PART|QUIT|NICK_CHANGE|INVITE|NOTICE|KICK|CTCP|IDLE|IRC_ERROR|TRUST_DENIED|NICK_ERROR|AUTH_ERROR|NS_IDENTIFY_FAIL|SERVER_ERROR|CMD_ERROR|LEAK_BLOCKED|E2E_KEY_ALARM|E2E_SEND_FAILED|E2E_PINS_ERROR|ORDER_ACK_REJECTED) ' | \
  chan_filter
