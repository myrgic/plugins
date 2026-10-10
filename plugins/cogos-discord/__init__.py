"""cogos-discord: a layer over Hermes's bundled Discord adapter.

v0.1 changes one thing: a thread the bot itself created counts as a thread the bot is in.
Hermes only skips the @mention requirement inside threads listed in its participation tracker
(``adapter._threads``), and it only adds a thread to that list when the gateway itself creates or
replies in it. Threads the bot created some other way (a forum post made over REST by a script,
a second process sharing the token) are never listed, so the first human reply there is dropped
unless it @mentions the bot.

How the override works (Hermes ``gateway/platform_registry.py``):
- Bundled platform plugins register a *deferred* loader; user plugins load after them.
- A concrete ``register()`` for the same name replaces the entry and drops the deferred loader,
  so after we register nothing would ever load the bundled module. We therefore resolve the bundled
  entry first (``platform_registry.get("discord")`` runs its loader), subclass its adapter, and
  register a *copy* of the bundled entry with only ``adapter_factory`` swapped. Everything else
  on the entry (YAML config translation, cron delivery, setup, auth env names) stays the bundled one.
- Cost: discord.py now imports whenever this profile loads plugins, not only when the gateway
  first asks for Discord.

If the contract check fails (Hermes renamed something we rely on) we log an ERROR and do not
register, so the stock adapter keeps running unchanged. The log line
``cogos-discord: override active`` is the positive signal to look for after a restart.
"""

from __future__ import annotations

import dataclasses
import logging
import sys
from typing import Any, Optional

logger = logging.getLogger(__name__)

PLATFORM = "discord"

# Fields of PlatformEntry that register_platform() sets itself.
_SET_BY_REGISTER = {"name", "label", "adapter_factory", "check_fn", "source", "plugin_name"}

# What we rely on in the bundled adapter. Checked at load; a miss disables the override loudly.
_REQUIRED_ADAPTER_ATTRS = ("_handle_message", "_dispatch_recovered_message", "_discord_thread_require_mention")


class OwnedThreadTracker:
    """Wraps Hermes's ThreadParticipationTracker.

    Membership is true if the wrapped tracker has the thread, or if the thread is a Discord thread
    whose ``owner_id`` is the bot's own user id. The second case is marked into the wrapped tracker,
    so it persists (``discord_threads.json``) and later checks are a dict lookup.
    """

    def __init__(self, inner: Any, adapter: Any):
        self._inner = inner
        self._adapter = adapter

    def __contains__(self, thread_id: Any) -> bool:
        tid = str(thread_id)
        if tid in self._inner:
            return True
        if self._bot_owns(tid):
            try:
                self._inner.mark(tid)
            except Exception:  # persistence failure must not change the answer
                logger.warning("cogos-discord: could not persist owned thread %s", tid, exc_info=True)
            logger.info("cogos-discord: thread %s was created by this bot; treating it as joined", tid)
            return True
        return False

    def _bot_owns(self, tid: str) -> bool:
        client = getattr(self._adapter, "_client", None)
        me = getattr(client, "user", None)
        my_id = getattr(me, "id", None)
        if client is None or my_id is None:
            return False
        try:
            channel = client.get_channel(int(tid))
        except (TypeError, ValueError):
            return False
        except Exception:
            logger.debug("cogos-discord: get_channel(%s) failed", tid, exc_info=True)
            return False
        owner_id = getattr(channel, "owner_id", None)
        return owner_id is not None and str(owner_id) == str(my_id)

    def mark(self, thread_id: str) -> None:
        self._inner.mark(thread_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def make_adapter_class(base: type) -> type:
    """Subclass the bundled DiscordAdapter, wrapping its thread tracker after construction."""

    class CogosDiscordAdapter(base):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._threads = OwnedThreadTracker(self._threads, self)

    CogosDiscordAdapter.__name__ = "CogosDiscordAdapter"
    CogosDiscordAdapter.__qualname__ = "CogosDiscordAdapter"
    return CogosDiscordAdapter


def contract_problems(base: Optional[type], tracker_cls: Optional[type]) -> list[str]:
    """Return what the bundled code no longer provides. Empty list = compatible."""
    problems = []
    if base is None:
        return ["bundled DiscordAdapter class not found"]
    for attr in _REQUIRED_ADAPTER_ATTRS:
        if not hasattr(base, attr):
            problems.append(f"DiscordAdapter.{attr} missing")
    if tracker_cls is None:
        problems.append("ThreadParticipationTracker not importable")
    else:
        for attr in ("mark", "__contains__"):
            if not hasattr(tracker_cls, attr):
                problems.append(f"ThreadParticipationTracker.{attr} missing")
    return problems


def _bundled_adapter_class(entry: Any) -> Optional[type]:
    factory = getattr(entry, "adapter_factory", None)
    module = sys.modules.get(getattr(factory, "__module__", "") or "")
    cls = getattr(module, "DiscordAdapter", None)
    return cls if isinstance(cls, type) else None


def register(ctx: Any) -> None:
    from gateway.platform_registry import platform_registry

    bundled = platform_registry.get(PLATFORM)  # runs the bundled deferred loader
    if bundled is None:
        logger.error("cogos-discord: no '%s' platform registered; override not installed", PLATFORM)
        return
    if getattr(bundled, "plugin_name", "") == ctx.manifest.name:
        logger.info("cogos-discord: already registered in this scope")
        return

    base = _bundled_adapter_class(bundled)
    try:
        from gateway.platforms.helpers import ThreadParticipationTracker as tracker_cls
    except Exception:
        tracker_cls = None
    problems = contract_problems(base, tracker_cls)
    if problems:
        logger.error(
            "cogos-discord: Hermes Discord adapter is not compatible (%s); override NOT installed, "
            "stock adapter stays active", "; ".join(problems),
        )
        return

    adapter_cls = make_adapter_class(base)

    def _build(config: Any) -> Any:
        return adapter_cls(config)

    carried = {
        f.name: getattr(bundled, f.name)
        for f in dataclasses.fields(bundled)
        if f.name not in _SET_BY_REGISTER
    }
    ctx.register_platform(
        name=PLATFORM,
        label=bundled.label,
        adapter_factory=_build,
        check_fn=bundled.check_fn,
        **carried,
    )
    logger.info(
        "cogos-discord: override active (base %s.%s, %d entry fields carried over)",
        base.__module__, base.__name__, len(carried),
    )
