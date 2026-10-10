# cogos-discord

A Hermes plugin that sits on top of Hermes's bundled Discord adapter. It is a native Hermes plugin
(Python, `plugin.yaml`), not a Claude Code plugin or a skill pack.

## v0.1: answer in threads the bot created

Hermes skips the @mention requirement inside a thread only if that thread is on its participation
list, and it only adds threads the gateway itself created or replied in. Threads the bot created some
other way (forum posts made over the REST API by a script, or by another process using the same
token) are never added, so the first human reply in them is ignored unless it @mentions the bot.

With this plugin, a thread whose `owner_id` is the bot counts as joined. It is added to the list the
first time it's checked, so it persists across restarts like any other joined thread. This covers both
places Hermes checks for a mention: live messages, and messages caught up after downtime.

Unchanged: `discord.thread_require_mention: true` still forces mentions everywhere; threads started
by people still need a mention until the bot has replied there; allowlists run exactly as before.

## Install

```bash
hermes plugins install https://github.com/myrgic/plugins/tree/main/plugins/cogos-discord
hermes plugins enable cogos-discord
# restart the gateway, then look for this line in the gateway log:
#   cogos-discord: override active (...)
```

**Rollback:** `hermes plugins disable cogos-discord` and restart. Hermes's own adapter comes back.

## How it works, and what it costs

Hermes lets a plugin replace a built-in platform: the registry keeps the last registration. The plugin:

1. loads Hermes's Discord adapter first (Hermes normally loads it lazily);
2. checks that the parts it relies on still exist (`_handle_message`, `_dispatch_recovered_message`,
   `_discord_thread_require_mention`, and the thread tracker's `mark`/`in`);
3. registers a **copy** of Hermes's `discord` entry with only the adapter swapped, so config handling,
   cron delivery to Discord, setup and allowlist settings stay Hermes's own.

If step 2 fails, it logs an `ERROR`, registers nothing, and Hermes's adapter runs unchanged. That
means a Hermes update can turn this plugin off, but it can't break Discord. The `override active` log
line is how you confirm it's on.

Cost: discord.py now imports whenever this profile loads plugins, not only when Discord is first used.

## Tests

The tests run against a real Hermes source tree. Rerun them whenever you update Hermes:

```bash
cd <hermes src> && <hermes venv>/bin/python -m pytest <path>/plugins/cogos-discord/tests -q
```

They cover the compatibility check, both mention checks (with and without bot ownership),
`thread_require_mention`, persistence, and that the registered entry matches Hermes's own except for
the adapter. With the override removed, the three behaviour tests fail.

## Roadmap

- v0.2: an activity stream plus a classifier hook (ignore / react / reply / escalate), watch-only by
  default, with the model and what it may do on its own set in config.
- Later and separate: keeping server structure in sync with a declared layout. It won't be bundled
  here.
