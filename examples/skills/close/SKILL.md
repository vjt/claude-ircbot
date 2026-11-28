---
name: close
description: Shut the IRC session down cleanly before /exit — flush today's notes, QUIT the bot with a message, stop the Monitors.
user_invocable: true
---

# /close — clean shutdown

`<repo>` = this skill's base directory minus `/.claude/skills/close`.

Only when you are about to `/exit` and relaunch later; a session left running needs nothing.

1. **Flush notes.** Anything from this session that should survive (decisions, shipped work, plans)
   and is not in today's notes yet: write it now. Do not invent content for a quiet day.
2. **QUIT.** `printf 'QUIT :back soon\n' > <repo>/bot.send` — a line that fits the channel's
   mood is nicer than a timeout. Wait a couple of seconds for it to flush. The service sees a normal
   exit: do not also `systemctl stop` it, the two race the reconnect.
3. **Stop the Monitors.** `TaskList`, then `TaskStop` every event stream.
4. **Check the memory index.** Every pointer in the index resolves to a file; fix what does not.
5. **Report** in one line, then the user runs `/exit`.
