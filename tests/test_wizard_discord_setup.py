"""Discord setup in the terminal, as the user hit it (2026-09-29): the bot
couldn't be named, Discord wasn't offered after saving a new profile, a new
bot silently became a remembered one, and the token was never asked for."""
from __future__ import annotations

import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sleeptalk import launcher, wizard
from tests.test_quick_setup import _stub_discord, setup_home  # noqa: F401 - fixture


def script(monkeypatch, *, selects=(), texts=()):
    """Like test_quick_setup.script, and also records each menu's choices."""
    prompts: list[str] = []
    offered: dict[str, list] = {}
    picks = iter(selects)
    values = iter(texts)

    def select(prompt, choices=(), **kwargs):
        prompts.append(prompt)
        offered[prompt] = [getattr(choice, "value", choice) for choice in choices]
        return SimpleNamespace(ask_async=AsyncMock(return_value=next(picks)))

    async def text(prompt, **kwargs):
        prompts.append(prompt)
        return next(values)

    monkeypatch.setattr("questionary.select", select)
    monkeypatch.setattr("questionary.confirm", lambda *a, **k: SimpleNamespace(ask_async=AsyncMock(return_value=True)))
    monkeypatch.setattr(wizard, "_ask_text", text)
    return prompts, offered


async def test_a_new_discord_bot_is_named_and_its_command_follows_the_name(setup_home, monkeypatch, capsys):
    _stub_discord(monkeypatch)
    prompts, _ = script(monkeypatch, selects=["codex", "deepgram", "discord", "open"],
                        texts=["deepgram-test", "Night Owl", "discord-test"])
    path, surface = await wizard._configure(workspace=str(setup_home))
    assert surface == "discord"
    assert "Bot's display name:" in prompts
    assert path.stem == "night-owl"
    assert tomllib.loads(path.read_text())["agent"]["name"] == "Night Owl"
    assert "/night-owl join" in capsys.readouterr().out


async def test_a_name_the_settings_page_refuses_is_refused_here_too(setup_home, monkeypatch):
    _stub_discord(monkeypatch)
    # "example" is the template's name; "!!!" has no letters to make a command from.
    prompts, _ = script(monkeypatch, selects=["codex", "deepgram", "discord", "open"],
                        texts=["deepgram-test", "example", "!!!", "Lamp Bot", "discord-test"])
    path, _ = await wizard._configure(workspace=str(setup_home))
    assert prompts.count("Bot's display name:") == 3
    assert path.stem == "lamp-bot"


async def test_a_remembered_bot_is_offered_not_assumed(setup_home, monkeypatch):
    """DISCORD_BOT_TOKEN from an earlier setup used to become the new bot's
    token without a word, and the token prompt never appeared."""
    _stub_discord(monkeypatch)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "old-bot-token")
    check = AsyncMock(side_effect=lambda token: {"old-bot-token": "Old Bot", "new-bot-token": "New Bot"}[token])
    monkeypatch.setattr(wizard.discord_check, "check_token", check)
    prompts, offered = script(monkeypatch, selects=["codex", "deepgram", "discord", "new", "open"],
                              texts=["deepgram-test", "Fresh Bot", "new-bot-token"])
    path, _ = await wizard._configure(workspace=str(setup_home))
    question = next(prompt for prompt in prompts if "Old Bot" in prompt)
    assert offered[question][:2] == ["reuse", "new"]
    assert "Bot token:" in prompts
    from sleeptalk.credential_store import CredentialStore

    ref = tomllib.loads(path.read_text())["credentials"]["refs"]["FRESH_BOT_DISCORD_BOT_TOKEN"]
    assert CredentialStore().get(ref) == "new-bot-token"


async def test_the_remembered_bot_can_be_chosen_on_purpose(setup_home, monkeypatch):
    _stub_discord(monkeypatch)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "old-bot-token")
    prompts, _ = script(monkeypatch, selects=["codex", "deepgram", "discord", "reuse", "open"],
                        texts=["deepgram-test", "Same Bot"])
    path, surface = await wizard._configure(workspace=str(setup_home))
    assert surface == "discord" and "Bot token:" not in prompts


async def test_discord_is_offered_after_saving_a_new_profile_in_settings(setup_home, monkeypatch):
    """`sleeptalk settings` -> new profile -> "Start a call now?" offered only
    browser, terminal and not now until Discord was already set up."""
    _stub_discord(monkeypatch)
    prompts, offered = script(monkeypatch, selects=["codex", "deepgram", "discord", "open"],
                              texts=["deepgram-test", "Porch Bot", "discord-test"])
    result = await wizard._configure(settings=True, workspace=str(setup_home))
    assert "discord" in offered["Start a call now?"]
    path, surface = result
    assert surface == "discord" and path.stem == "porch-bot"
    assert not (path.parent / "sleeptalk.toml").exists(), "the first save's file moves with the name"
    transport = tomllib.loads(path.read_text())["transport"]
    assert (transport["type"], transport["guild_id"], transport["client_id"]) == ("discord", "123", "456")
    assert launcher.selected_profile(setup_home) == path


def test_name_rules_are_the_settings_page_rules():
    assert wizard.profile_name_slug("Night Owl") == "night-owl"
    assert wizard.profile_name_slug("example") == ""
    assert wizard.profile_name_slug("!!!") == ""
    assert wizard.profile_name_slug("x" * 65) == ""

