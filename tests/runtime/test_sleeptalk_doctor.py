"""Tests for sleeptalk doctor + the new validation plumbing."""

from __future__ import annotations

from datetime import date

import pytest

from sleeptalk import discord_check, language_support
from sleeptalk.config import SleeptalkConfig
from sleeptalk.doctor import FAIL, OK, SKIP, WARN, format_report, run_doctor

MINIMAL = """\
[transport]
type = "null"

[agent]
type = "pi"

[stt]
provider = "deepgram"

[tts]
provider = "cartesia"
"""

ENV = {"DEEPGRAM_API_KEY": "x", "CARTESIA_API_KEY": "y"}


def _write(tmp_path, text=MINIMAL):
    path = tmp_path / "sleeptalk.toml"
    path.write_text(text)
    return path


def _no_binaries(monkeypatch):
    monkeypatch.setattr("sleeptalk.doctor.collect_missing_binaries", lambda config, **kwargs: [])


# ---------------------------------------------------------------------------
# language_support
# ---------------------------------------------------------------------------


def test_language_lookup_tiers():
    assert language_support.lookup("en-US", "deepgram").tier == "full"
    assert language_support.lookup("es", "deepgram").tier == "full"
    assert language_support.lookup("pl", "deepgram").tier == "degraded"
    assert "nova-2" in language_support.lookup("pl", "deepgram").note
    assert language_support.lookup("pl", "cartesia").tier == "full"
    assert language_support.lookup("xx", "cartesia").tier == "unsupported"
    assert language_support.lookup("fi", "elevenlabs").tier == "full"


def test_language_warnings_cite_last_verified():
    warnings = language_support.warnings_for("pl", ["deepgram", "cartesia"])
    assert warnings
    assert any(language_support.LAST_VERIFIED in w for w in warnings)


def _months_after_verified(months: int) -> date:
    year, month = (int(part) for part in language_support.LAST_VERIFIED.split("-"))
    index = year * 12 + (month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def test_staleness_nudge():
    assert language_support.staleness_nudge(_months_after_verified(0)) is None
    nudge = language_support.staleness_nudge(_months_after_verified(7))
    assert nudge and language_support.LAST_VERIFIED in nudge
    assert language_support.staleness_months(_months_after_verified(6)) == 6


# ---------------------------------------------------------------------------
# discord_check intents
# ---------------------------------------------------------------------------


def test_requested_intents_mirror_runtime():
    """Plain voice needs no privileged intent; text summons and rooms need Message Content."""
    config = SleeptalkConfig()
    names = discord_check.requested_intent_names(config)
    assert "voice_states" in names or "guild_voice_states" in names
    assert "message_content" not in names
    assert discord_check.privileged_requested(config) == []

    config.transport.voice_only = True
    assert "message_content" in discord_check.requested_intent_names(config)
    assert discord_check.privileged_requested(config) == ["message_content"]

    rooms = SleeptalkConfig()
    rooms.agent.participation_mode = "attendee"
    assert discord_check.privileged_requested(rooms) == ["message_content"]
    assert discord_check.requested_intents_value_for() == discord_check.requested_intents_value(SleeptalkConfig())
    assert discord_check.requested_intents_value_for(voice_only=True) == discord_check.requested_intents_value(config)


def test_requested_intents_match_discord_py():
    """When discord.py is importable, the value must equal the runtime's."""
    discord = pytest.importorskip("discord")
    config = SleeptalkConfig()
    expected = discord.Intents.default()
    expected.voice_states = True
    expected.message_content = False
    assert discord_check.requested_intents_value(config) == expected.value
    assert discord_check.requested_intents_value(config) == (
        discord_check.runtime_discord_intents(config).value
    )


def test_close_code_mapping():
    with pytest.raises(discord_check.InvalidIntentError):
        discord_check._raise_for_close(4013)
    with pytest.raises(discord_check.DisallowedIntentsError):
        discord_check._raise_for_close(4014)
    with pytest.raises(discord_check.DiscordCheckError):
        discord_check._raise_for_close(1001)


def test_disallowed_intents_message_names_toggles():
    exc = discord_check.DisallowedIntentsError(["message_content"])
    assert "4014" in str(exc)
    assert "message_content" in str(exc)
    assert "Privileged Gateway Intents" in str(exc)


def test_invite_url_has_voice_permissions():
    url = discord_check.invite_url("123")
    assert "client_id=123" in url
    assert "applications.commands" in url
    permissions = int(url.split("permissions=")[1].split("&")[0])
    assert permissions == discord_check.INVITE_PERMISSIONS


def test_translate_connect_error_names_python_org_certs():
    err = discord_check._translate_connect_error(
        RuntimeError("[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer")
    )
    assert "Install Certificates.command" in str(err)
    assert "discord.com" in str(err)


def test_translate_connect_error_other_network_failure():
    # The exception text is never echoed: it can carry a URL with a token.
    err = discord_check._translate_connect_error(RuntimeError("Network is unreachable"))
    assert "Could not reach discord.com" in str(err)
    assert "Network is unreachable" not in str(err)


@pytest.mark.asyncio
async def test_fetch_bot_ssl_failure_is_fix_stating(monkeypatch):
    import aiohttp

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            raise aiohttp.ClientError("CERTIFICATE_VERIFY_FAILED")

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *_a, **_k: FakeSession())
    with pytest.raises(discord_check.DiscordCheckError, match="Install Certificates"):
        await discord_check.fetch_bot("dummy-token")


# ---------------------------------------------------------------------------
# doctor ladder (offline)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_doctor_ok_minimal(tmp_path, monkeypatch):
    _no_binaries(monkeypatch)
    report = await run_doctor(_write(tmp_path), env=dict(ENV), live=False)
    assert not report.failed, format_report(report)
    by_name = {rung.name: rung for rung in report.rungs}
    assert by_name["file parses"].status == OK
    assert by_name["env vars present"].status == OK
    assert by_name["language support"].status == OK


@pytest.mark.asyncio
async def test_doctor_bad_toml_stops(tmp_path):
    report = await run_doctor(_write(tmp_path, "not [toml"), env={}, live=False)
    assert report.failed
    assert report.rungs[0].name == "file parses"
    assert report.rungs[0].status == FAIL
    assert len(report.rungs) == 1  # nothing downstream can run


@pytest.mark.asyncio
async def test_doctor_unknown_key(tmp_path):
    broken = MINIMAL.replace('provider = "deepgram"', 'provider = "deepgram"\nbogus = 1')
    report = await run_doctor(_write(tmp_path, broken), env=dict(ENV), live=False)
    by_name = {rung.name: rung for rung in report.rungs}
    assert by_name["schema valid"].status == FAIL
    assert any("bogus" in line for line in by_name["schema valid"].lines)


@pytest.mark.asyncio
async def test_doctor_names_every_problem_in_one_pass(tmp_path, monkeypatch):
    """Unknown provider + missing env vars both reported in one run."""
    _no_binaries(monkeypatch)
    broken = MINIMAL.replace('provider = "deepgram"', 'provider = "nope"')
    report = await run_doctor(_write(tmp_path, broken), env={}, live=False)
    by_name = {rung.name: rung for rung in report.rungs}
    assert by_name["component choices"].status == FAIL
    assert any("nope" in line for line in by_name["component choices"].lines)
    assert by_name["env vars present"].status == FAIL
    assert any("CARTESIA_API_KEY" in line for line in by_name["env vars present"].lines)


@pytest.mark.asyncio
async def test_doctor_language_warning_not_failure(tmp_path, monkeypatch):
    _no_binaries(monkeypatch)
    broken = MINIMAL.replace('provider = "deepgram"', 'provider = "deepgram"\nlanguage = "pl"')
    report = await run_doctor(
        _write(tmp_path, broken),
        env=dict(ENV),
        live=False,
    )
    by_name = {rung.name: rung for rung in report.rungs}
    assert by_name["language support"].status == WARN
    assert not report.failed


@pytest.mark.asyncio
async def test_doctor_live_rungs_skipped_offline(tmp_path, monkeypatch):
    _no_binaries(monkeypatch)
    report = await run_doctor(_write(tmp_path), env=dict(ENV), live=False)
    names = [rung.name for rung in report.rungs]
    assert "provider connectivity" not in names
    assert "discord" not in names
    assert "stt round-trip" not in names


@pytest.mark.asyncio
async def test_hybrid_doctor_ignores_cascade_stt_tts_and_uses_openai_check(
    tmp_path, monkeypatch
):
    _no_binaries(monkeypatch)
    called = []

    async def check(config, *, env):
        called.append((config.route.model, env["OPENAI_API_KEY"]))

    monkeypatch.setattr("sleeptalk.builder.run_connectivity_checks", check)
    profile = MINIMAL + """

[route]
type = "hybrid"
"""
    report = await run_doctor(
        _write(tmp_path, profile),
        env={"OPENAI_API_KEY": "openai-key"},
        live=True,
    )

    by_name = {rung.name: rung for rung in report.rungs}
    assert called == [("gpt-realtime-2.1", "openai-key")]
    assert by_name["provider connectivity"].status == OK
    assert by_name["stt round-trip"].status == OK
    assert by_name["language support"].status == OK


def test_doctor_cli_exit_codes(tmp_path, monkeypatch):
    _no_binaries(monkeypatch)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    from sleeptalk.cli import main

    assert main(["doctor", "--offline", "--config", str(_write(tmp_path))]) == 0
    assert main(["doctor", "--offline", "--config", str(tmp_path / "nope.toml")]) == 1


# ---------------------------------------------------------------------------
# stt round-trip
# ---------------------------------------------------------------------------


def test_bundled_clip_is_real_wav():
    from sleeptalk.stt_roundtrip import bundled_clip_bytes

    clip = bundled_clip_bytes()
    assert clip[:4] == b"RIFF"
    assert len(clip) > 10_000  # ~1s of speech, not an empty stub


@pytest.mark.asyncio
async def test_roundtrip_requires_key():
    from sleeptalk.providers import ProviderSpec
    from sleeptalk.speech.common import SpeechError
    from sleeptalk.stt_roundtrip import deepgram_roundtrip

    with pytest.raises(SpeechError):
        await deepgram_roundtrip(ProviderSpec(provider="deepgram", model="nova-3"), "")
