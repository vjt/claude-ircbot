# claude-ircbot

Minimal Python IRC bridge that lets a [Claude Code](https://www.anthropic.com/claude-code) session participate in an IRC channel as a real user.

Built in an evening as a proof of concept. Full story: [Claude walks into #it-opers](https://sindro.me/posts/2026-04-17-claude-walks-into-it-opers/).

## How it works

- **TLS IRC client** (stdlib `socket` + `ssl`). Connects to an IRC network, registers a nick, handles `PING`, logs everything to `bot.log`.
- **Structured stdout** — one line per interesting event (`MSG`, `INVITE`, `CTCP`, `NOTICE`, errors). Claude Code's [Monitor tool](https://code.claude.com/docs/en/agent-sdk/typescript#monitor) attaches to the bot and delivers each line as a notification mid-conversation.
- **Named-pipe inbox** (`bot.send`). The agent writes commands like `SAY #channel hello world` into the pipe; the bot translates them into `PRIVMSG`s and sends them.

That's the whole thing. About 250 lines of Python, no dependencies outside the standard library.

## Usage

```bash
python3 -u bot.py
```

Default config is inline at the top of `bot.py` — adjust `HOST`, `PORT`, `NICK`, `TRUSTED` for your target network and channel.

Send commands via the FIFO:

```bash
printf 'SAY #mychannel hello everyone\n' > bot.send
```

Supported commands: `SAY`, `ACT`, `NOTICE`, `JOIN`, `PART`, `WHOIS`, `QUIT`, `RAW`.

## Trust model

The bot's own trust check is minimal: only honour `INVITE` from the nick set in `TRUSTED`. Everything else is emitted as an event but not auto-acted on. The actual "who can command the agent" logic lives in the agent's system prompt, not the bot — the bot is transport.

## License

MIT. See `LICENSE`.
