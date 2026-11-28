#!/usr/bin/env bash
# Tail one bot instance's event stream, for a Claude Code Monitor (or a watchd watch).
#
#   examples/start-monitor.sh [path/to/bot.stdout.log]    # default: bot.stdout.log next to bot.py
#
# The source is bot.stdout.log (bot.py's emit(): one event per line, trust verdict
# already attached), never bot.log, which is the raw socket traffic. One bot
# instance = one stdout log = one Monitor: run this once per instance.
#
# stdbuf -oL keeps tail line-buffered: block-buffered, the Monitor would get the
# penultimate line instead of the last. tail -F follows rotation; -n 0 does not
# replay history on attach. The grep keeps the verbs worth waking the agent for
# and drops the diagnostics (CONNECTED, TLS_OK, TRUST_LOADED, …).
#
# Optional per-channel read policy: an awk program at $CHAN_FILTER (default
# tools/chan_filter.awk) is applied when it exists, e.g. to stay in a channel
# without being woken by it. Without it every event passes.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="${1:-$ROOT/bot.stdout.log}"
FILTER="${CHAN_FILTER:-$ROOT/tools/chan_filter.awk}"
chan_filter() {
  if [ -f "$FILTER" ]; then awk -f "$FILTER"; else cat; fi
}

stdbuf -oL tail -F -n 0 "$LOG" | \
  grep --line-buffered -E '^(\[[0-9]{2}:[0-9]{2}\] )?(MSG|JOIN|PART|QUIT|NICK_CHANGE|INVITE|NOTICE|KICK|CTCP|IDLE|IRC_ERROR|TRUST_DENIED|NICK_ERROR|AUTH_ERROR|NS_IDENTIFY_FAIL|SERVER_ERROR|CMD_ERROR|LEAK_BLOCKED|E2E_KEY_ALARM|E2E_SEND_FAILED|E2E_PINS_ERROR|ORDER_ACK_REJECTED) ' | \
  chan_filter
