from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

discord = pytest.importorskip("discord")

from discord import app_commands

from sleeptalk.discord.bot import PiDiscordBot, build_slash_group
from sleeptalk.policy import VoicePolicy
from sleeptalk.discord.access import DiscordAccess

OPEN_ACCESS = DiscordAccess(required=False)


class FakeRealtimeSession:
    def __init__(self) -> None:
        self.policy = VoicePolicy(narration_style="narrated", reply_length="full")
        self.speed = 1.0

    def current_tts_speed(self) -> float:
        return self.speed

    def set_narration_style(self, value: object) -> str:
        self.policy.narration_style = str(value)
        return self.policy.narration_style

    def set_reply_length(self, value: object) -> str:
        self.policy.reply_length = str(value)
        return self.policy.reply_length

    def set_tts_speed(self, value: float | None) -> float:
        self.speed = 1.0 if value is None else value
        return self.speed


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    def is_done(self) -> bool:
        return False

    async def send_message(self, content: str, *, ephemeral: bool = False) -> None:
        self.messages.append((content, ephemeral))


def fake_bot(*, voice_only: bool = False):
    session = FakeRealtimeSession()
    return SimpleNamespace(
        access=OPEN_ACCESS,
        profile=SimpleNamespace(
            slug="campbell",
            name="Campbell",
            discord=SimpleNamespace(voice_only=voice_only),
        ),
        realtime_session=session,
        policy=session.policy,
        allowed_channel_ids=set(),
        native_sessions={},
    )


def settings_command(bot):
    group = build_slash_group(bot)
    return next(command for command in group.commands if command.name == "settings")


@pytest.mark.asyncio
async def test_discord_settings_without_options_shows_current_values() -> None:
    bot = fake_bot()
    command = settings_command(bot)
    interaction = SimpleNamespace(channel_id=123, response=FakeResponse())

    await command.callback(interaction, None, None, None)

    assert interaction.response.messages == [
        (
            "**Campbell voice settings**\n"
            "Tool narration: **Narrated**\n"
            "Response length: **Full**\n"
            "Speech speed: **1x**\n\n"
            "Applies to the current or next call. Resets when Campbell restarts.",
            True,
        )
    ]


@pytest.mark.asyncio
async def test_discord_settings_applies_any_supplied_choices() -> None:
    bot = fake_bot()
    command = settings_command(bot)
    interaction = SimpleNamespace(channel_id=123, response=FakeResponse())

    await command.callback(
        interaction,
        app_commands.Choice(name="Silent", value="silence"),
        app_commands.Choice(name="Brief", value="summarized"),
        app_commands.Choice(name="1.25x", value=1.25),
    )

    assert bot.realtime_session.policy.narration_style == "silence"
    assert bot.realtime_session.policy.reply_length == "summarized"
    assert bot.realtime_session.speed == 1.25
    assert interaction.response.messages[0][0].startswith(
        "**Campbell voice settings**\n"
        "Tool narration: **Silent**\n"
        "Response length: **Brief**\n"
        "Speech speed: **1.25x**"
    )
    assert interaction.response.messages[0][1] is True


def test_voice_only_discord_registers_only_settings() -> None:
    group = build_slash_group(fake_bot(voice_only=True))

    assert [command.name for command in group.commands] == ["settings"]


@pytest.mark.asyncio
async def test_voice_only_setup_registers_and_syncs_settings(monkeypatch) -> None:
    events: list[str] = []
    tree = SimpleNamespace(sync=AsyncMock())
    bot = SimpleNamespace(
        access=OPEN_ACCESS,
        profile=SimpleNamespace(
            discord=SimpleNamespace(voice_only=True, guild_id="")
        ),
        tree=tree,
        register_voice_commands=lambda: events.append("registered"),
        _keep_conversation=lambda: events.append("kept"),
    )
    monkeypatch.setattr(
        "sleeptalk.discord.bot._kick_warmup",
        lambda _bot: events.append("warmed"),
    )

    await PiDiscordBot.setup_hook(bot)

    # The agent is told where its conversation is saved before it first starts.
    assert events == ["registered", "kept", "warmed"]
    tree.sync.assert_awaited_once_with()
