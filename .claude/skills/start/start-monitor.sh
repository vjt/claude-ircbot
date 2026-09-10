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
# vjt 2026-09-03 23:19, ordine in canale: "togliamo il bot e vediamo come va" —
# #sniffo viene filtrato QUI, non con un PART: il bot resta dentro (ops, log,
# sidecar continuano a funzionare) ma nessun evento del canale mi sveglia, quindi
# di fatto li' sono muto. Per rimettermi in ascolto togli il secondo grep -v.
# vjt 2026-09-08 17:17 (#sbiffo): "lo stesso filter che hai su sniffo anche su
# italia, e rijoinare italia per poter usare trivial" — il bot rientra su #italia
# perche' il sidecar impiccato ci giri (chans in impiccato.py:87, goliardia
# esclusa da CHAN_EXCLUDE:178), ma il traffico del canale non mi sveglia.
# vjt 2026-09-10 00:20 (#sbiffo): "mi chiedo se puoi rimanere su sniffo ma
# leggendo solo le righe che ti menzionano" — passavano le righe con `vjt-claude`.
# SUPERATO da vjt 2026-09-10 21:49 (#sniffo): "muto di nuovo, basta, neanche se
# interpellato su sniffo, modifica il monitor" — ora #sniffo e' droppato del
# tutto come #italia, nemmeno le menzioni mi svegliano. #italia idem (il bot ne
# e' uscito). awk e non grep perche' serve una condizione a due termini, e
# fflush() perche' altrimenti bufferizza e il Monitor legge la penultima riga.
# vjt 2026-09-10 10:03 (query): le regole per canale sono uscite da qui e stanno
# in tools/chan_filter.awk, condivise con tools/sweep-bot-log.sh — la sweep di
# /resume leggeva bot.log grezzo e si rivedeva #sniffo per intero a ogni /clear.
stdbuf -oL tail -F -n 0 /home/vjt/code/IRC/vjt-claude/bot.stdout.log | \
  grep --line-buffered -E '^(\[[0-9]{2}:[0-9]{2}\] )?(MSG|JOIN|PART|QUIT|NICK_CHANGE|INVITE|NOTICE|KICK|CTCP|IDLE|IRC_ERROR|TRUST_DENIED|NICK_ERROR|AUTH_ERROR|NS_IDENTIFY_FAIL|SERVER_ERROR|CMD_ERROR) ' | \
  awk -f /home/vjt/code/IRC/vjt-claude/tools/chan_filter.awk
