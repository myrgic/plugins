"""Contract + behaviour tests for cogos-discord against a real Hermes checkout.

Run from a Hermes source tree (so ``gateway`` and ``plugins.platforms.discord`` import):

    cd <hermes src> && <hermes venv>/bin/python -m pytest <this dir>/tests -q

These are the tests to rerun whenever the Hermes runtime pin changes.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
BOT_ID = 999
OTHER_ID = 4242


def _load_plugin():
    spec = importlib.util.spec_from_file_location("cogos_discord_under_test", PLUGIN_DIR / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


plugin = _load_plugin()
discord_platform = pytest.importorskip("plugins.platforms.discord.adapter")
from gateway.config import PlatformConfig  # noqa: E402
from gateway.platforms.helpers import ThreadParticipationTracker  # noqa: E402


# --- contract ---------------------------------------------------------------------------------

def test_contract_holds_against_this_hermes():
    assert plugin.contract_problems(discord_platform.DiscordAdapter, ThreadParticipationTracker) == []


def test_contract_reports_missing_pieces():
    class Bare:
        pass

    problems = plugin.contract_problems(Bare, None)
    assert "DiscordAdapter._handle_message missing" in problems
    assert "ThreadParticipationTracker not importable" in problems


def test_gates_still_read_self_threads():
    """Both mention gates must still consult self._threads, or the wrapper does nothing."""
    import inspect

    live = inspect.getsource(discord_platform.DiscordAdapter._handle_message)
    recovered = inspect.getsource(discord_platform.DiscordAdapter._dispatch_recovered_message)
    assert "in self._threads" in live
    assert "in self._threads" in recovered


# --- behaviour --------------------------------------------------------------------------------

class FakeForum:
    def __init__(self, cid=500):
        self.id = cid
        self.name = "cog-dev"
        self.guild = SimpleNamespace(name="Lab")
        self.type = 15
        self.topic = None


class FakeThread:
    def __init__(self, cid, owner_id, parent):
        self.id = cid
        self.name = "post"
        self.parent = parent
        self.parent_id = parent.id
        self.owner_id = owner_id
        self.guild = parent.guild
        self.topic = None

    def history(self, *, limit, before, after=None, oldest_first=None):
        async def _iter():
            return
            yield
        return _iter()


class FakeDM:
    pass


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(discord_platform.discord, "Thread", FakeThread, raising=False)
    monkeypatch.setattr(discord_platform.discord, "DMChannel", FakeDM, raising=False)
    monkeypatch.setattr(discord_platform.discord, "ForumChannel", FakeForum, raising=False)
    for var in ("DISCORD_FREE_RESPONSE_CHANNELS", "DISCORD_ALLOWED_CHANNELS", "DISCORD_IGNORED_CHANNELS",
                "DISCORD_HISTORY_BACKFILL", "DISCORD_ALLOW_BOTS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setenv("DISCORD_THREAD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")
    cls = plugin.make_adapter_class(discord_platform.DiscordAdapter)
    a = cls(PlatformConfig(enabled=True, token="fake"))
    a._allowed_user_ids = {"42"}  # recovery path runs the allowlist before the mention gate
    forum = FakeForum()
    threads = {
        111: FakeThread(111, BOT_ID, forum),      # created by the bot over REST
        222: FakeThread(222, OTHER_ID, forum),    # created by a human
    }
    a._client = SimpleNamespace(user=SimpleNamespace(id=BOT_ID), get_channel=lambda cid: threads.get(cid))
    a._text_batch_delay_seconds = 0
    a.handle_message = AsyncMock()
    a._fake_threads = threads
    return a


def _msg(channel, text="hello, no mention"):
    return SimpleNamespace(
        id=7, content=text, mentions=[], attachments=[], reference=None,
        created_at=datetime.now(timezone.utc), channel=channel,
        author=SimpleNamespace(id=42, display_name="Chaz", name="chaz", bot=False),
        type=discord_platform.discord.MessageType.default,
    )


def test_tracker_is_wrapped(adapter):
    assert isinstance(adapter._threads, plugin.OwnedThreadTracker)


@pytest.mark.asyncio
async def test_bot_owned_thread_answers_without_mention(adapter, tmp_path):
    await adapter._handle_message(_msg(adapter._fake_threads[111]))
    adapter.handle_message.assert_awaited_once()
    saved = json.loads((tmp_path / "discord_threads.json").read_text())
    assert "111" in saved  # persisted, survives restart


@pytest.mark.asyncio
async def test_human_owned_thread_still_needs_mention(adapter):
    await adapter._handle_message(_msg(adapter._fake_threads[222]))
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_thread_require_mention_still_wins(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_THREAD_REQUIRE_MENTION", "true")
    await adapter._handle_message(_msg(adapter._fake_threads[111]))
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovered_message_in_bot_owned_thread_is_admitted(adapter):
    adapter._handle_message = AsyncMock(return_value=True)
    assert await adapter._dispatch_recovered_message(_msg(adapter._fake_threads[111])) is True
    adapter._handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovered_message_in_human_thread_is_refused(adapter):
    adapter._handle_message = AsyncMock(return_value=True)
    assert await adapter._dispatch_recovered_message(_msg(adapter._fake_threads[222])) is False


def test_unknown_channel_and_missing_client_are_not_owned(adapter):
    assert "333" not in adapter._threads
    adapter._client = None
    assert "444" not in adapter._threads


# --- registration -----------------------------------------------------------------------------

def test_register_copies_bundled_entry_and_swaps_only_the_factory(monkeypatch):
    from hermes_cli.plugins import discover_plugins
    from gateway.platform_registry import PlatformEntry, platform_registry

    discover_plugins()  # registers the bundled deferred loader, as the gateway does at startup
    bundled = platform_registry.get("discord")
    assert bundled is not None
    captured = {}

    class Ctx:
        manifest = SimpleNamespace(name="cogos-discord")

        def register_platform(self, name, label, adapter_factory, check_fn, **kw):
            captured["entry"] = PlatformEntry(
                name=name, label=label, adapter_factory=adapter_factory, check_fn=check_fn,
                source="plugin", plugin_name="cogos-discord", **kw,
            )

    plugin.register(Ctx())
    entry = captured["entry"]
    assert entry.adapter_factory is not bundled.adapter_factory
    for f in dataclasses.fields(bundled):
        if f.name in plugin._SET_BY_REGISTER:
            continue
        assert getattr(entry, f.name) == getattr(bundled, f.name), f.name
    # the pieces the review named explicitly
    assert entry.apply_yaml_config_fn is bundled.apply_yaml_config_fn
    assert entry.standalone_sender_fn is bundled.standalone_sender_fn
    assert entry.check_fn is bundled.check_fn
