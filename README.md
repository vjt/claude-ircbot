# claude-ircbot

Minimal Python IRC bridge that lets a [Claude Code](https://www.anthropic.com/claude-code) session participate in an IRC channel as a real user.

Built in an evening as a proof of concept. Full story: [Claude walks into #it-opers](https://sindro.me/posts/2026-04-17-claude-walks-into-it-opers/).

The persistent Claude Code session that runs on top of this bot publishes its operating manual and memory architecture (how it survives compactions, what it remembers, per-channel registers) at **[sindro.me/~vjt/vjt-claude/](https://sindro.me/~vjt/vjt-claude/)**.

## How it works

- **TLS IRC client** (stdlib `socket` + `ssl`). Connects to an IRC network, registers a nick, handles `PING`, logs everything to `bot.log`.
- **Structured stdout** — one line per interesting event (`MSG`, `INVITE`, `CTCP`, `NOTICE`, errors). Claude Code's [Monitor tool](https://code.claude.com/docs/en/agent-sdk/typescript#monitor) attaches to the bot and delivers each line as a notification mid-conversation.
- **Named-pipe inbox** (`bot.send`). The agent writes commands like `SAY #channel hello world` into the pipe; the bot translates them into `PRIVMSG`s and sends them.

That's the whole thing. About 250 lines of Python, no dependencies outside the standard library.

## Usage

```bash
python3 -u bot.py
```

Default config is inline at the top of `bot.py` — adjust `HOST`, `PORT`, `NICK` for your target network. Trust rules live in the adjacent `bot.trust` file.

Send commands via the FIFO:

```bash
printf 'SAY #mychannel hello everyone\n' > bot.send
```

Supported commands: `SAY`, `ACT`, `NOTICE`, `JOIN`, `PART`, `WHOIS`, `QUIT`, `RAW`.

## Trust model

Trust is the combination of three checks, ALL required:

1. **Nick listed** in `bot.trust` (one `<nick> <host_glob>` per line).
2. **Host matches the glob** (`fnmatch`, e.g. `*.openssl.it`) — defends against nick-only impersonation if services lapse.
3. **Registered & identified to services** — confirmed via `RPL_WHOISREGNICK` (numeric `307`). A one-shot `WHOIS` fires on the first sighting of a trust-listed nick (and for every entry at connect), the result is cached, and the cache resets on `PART` / `QUIT` / `NICK` change.

If any check fails, the message is still emitted as `MSG other <nick> ...` and a `TRUST_DENIED` line records the reason. `INVITE` auto-join is gated on the same check.

The actual "who can command the agent" logic still lives in the agent's system prompt — the bot only decides what to tag as trusted. The bot is transport.

Example `bot.trust`:

```
# <nick> <host_glob>
vjt *.openssl.it
```

## License

MIT. See `LICENSE`.
