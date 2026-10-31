"""The settings page: one profile, every section, edits without secrets."""
from __future__ import annotations

import asyncio
import json
import tomllib
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sleeptalk import setup_core
from sleeptalk.settings_page import SettingsPage, apply_setting, describe_profile

PROFILE = (
    '[agent]\ntype = "hermes"\nname = "Hermes"\nworkspace_dir = "/w"\n\n[transport]\ntype = "web"\n\n'
    '[stt]\nprovider = "openrouter"\nlanguage = "en-US"\n\n[tts]\nprovider = "openrouter"\nvoice_id = "eve"\nlanguage = "en-US"\n\n'
    '[policy]\nendpointing = "balanced"\n\n[credentials.refs]\nOPENROUTER_API_KEY = "credential:' + "a" * 32 + '"\n'
)


@pytest.fixture
def profile(tmp_path):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    path = profiles / "default.toml"
    path.write_text(PROFILE)
    (profiles / "second.toml").write_text(PROFILE)
    return path


def test_describe_profile_shows_every_section_and_no_secret(profile):
    state = describe_profile(profile)
    assert [s["id"] for s in state["sections"]] == ["agent", "speech", "voice", "language", "delivery", "discord", "behaviour", "keys"]
    assert state["agent"]["label"] == "Hermes" and state["speech"]["tts_provider"] == "openrouter"
    assert state["speech"]["stt_model"] == "nvidia/parakeet-tdt-0.6b-v3"
    in_use = [s["id"] for s in state["speech"]["services"] if s["in_use"]]
    assert in_use == ["openrouter"] and any(s["id"] == "local" for s in state["speech"]["services"])
    assert state["voice"]["name"] == "Eve" and [v["name"] for v in state["voice"]["voices"]][:2] == ["Eve", "Ara"]
    assert state["delivery"]["value"] == "relay" and state["behaviour"]["endpointing"] == "balanced"
    assert state["keys"] == [{"env": "OPENROUTER_API_KEY", "reference": "credential:aaaaaaaaaaaa…"}]
    assert "a" * 32 not in str(state)


def test_apply_setting_writes_only_allowed_keys(profile):
    apply_setting(profile, "language", "en-GB")
    apply_setting(profile, "tts.voice_id", "flux-kit-en")
    apply_setting(profile, "tts.speed", 1.25)
    apply_setting(profile, "transport.options.delivery", "tailnet")
    apply_setting(profile, "policy.reply_length", "summarized")
    parsed = tomllib.loads(profile.read_text())
    assert parsed["tts"]["voice_id"] == "flux-kit-en" and parsed["tts"]["speed"] == 1.25
    assert parsed["stt"]["language"] == parsed["tts"]["language"] == "en-GB"
    assert parsed["transport"]["options"]["delivery"] == "tailnet" and parsed["policy"]["reply_length"] == "summarized"
    apply_setting(profile, "transport.options.delivery", "relay")
    assert tomllib.loads(profile.read_text())["transport"]["options"]["delivery"] == ""
    apply_setting(profile, "transport.options.delivery", "local")
    assert tomllib.loads(profile.read_text())["transport"]["options"]["delivery"] == "local"
    assert describe_profile(profile)["delivery"]["value"] == "local"
    for setting, value in (("credentials.refs.X", "y"), ("tts.speed", 3.0), ("policy.endpointing", "batch"), ("tts.voice_id", "two words")):
        with pytest.raises(ValueError):
            apply_setting(profile, setting, value)
    assert "credential:" + "a" * 32 in profile.read_text()


async def test_page_requires_the_token_then_serves_state_and_edits(profile, monkeypatch):
    async def available(*, environ=None):
        return {"available": True, "name": "host.tail.ts.net"}

    monkeypatch.setattr("sleeptalk.tailnet.detect", available)
    page = SettingsPage(profile, profiles_dir=profile.parent, environ={})
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url("/"))[:-1]
    try:
        assert (await client.get("/", headers={"Host": "evil.example"})).status == 403
        assert (await client.get("/api/state")).status == 401
        origin = {"Origin": page.origin}
        assert (await client.post("/session", json={"token": "wrong"}, headers=origin)).status == 401
        response = await client.post("/session", json={"token": page.token}, headers=origin)
        csrf = (await response.json())["csrf"]
        cookie = response.cookies[page.cookie_name].value
        headers = {**origin, "Cookie": f"{page.cookie_name}={cookie}", "X-Settings-CSRF": csrf}
        state = await (await client.get("/api/state", headers=headers)).json()
        assert state["profiles"] == ["default", "second"] and state["tailnet"]["available"] is True
        assert (await client.post("/api/set", json={"setting": "tts.voice_id", "value": "ara"}, headers=headers)).status == 200
        assert tomllib.loads(profile.read_text())["tts"]["voice_id"] == "ara"
        refused = await client.post("/api/set", json={"setting": "agent.type", "value": "codex"}, headers=headers)
        assert refused.status == 422
        assert (await client.post("/api/set", json={"setting": "tts.voice_id", "value": "x"}, headers={**origin, "Cookie": headers["Cookie"]})).status == 403
        assert (await client.post("/api/profile", json={"profile": "second"}, headers=headers)).status == 200
        assert (await (await client.get("/api/state", headers=headers)).json())["profile"] == "second"
        html = await (await client.get("/", headers=headers)).text()
        assert "settings.js" in html
    finally:
        await client.close()


async def test_page_switches_the_speech_service_from_a_pasted_or_found_key(profile, monkeypatch, tmp_path):
    from sleeptalk.credential_store import CredentialStore

    seen = []

    async def validate(provider, value, spec, voice, **models):
        seen.append((provider, value, voice, spec.tts_provider))
        if value == "bad":
            raise RuntimeError("401 rejected")
        return {"stt_model": "ink-2", "voice_id": voice or "default-voice"}

    monkeypatch.setattr("sleeptalk.private_input.validate_provider", validate)
    store = CredentialStore(tmp_path / "store", backend="file")
    from sleeptalk import settings_page

    async def available(*, environ=None):
        return {"available": False, "code": "tailscale_missing"}

    monkeypatch.setattr("sleeptalk.tailnet.detect", available)
    result = await settings_page.switch_speech_service(profile, "cartesia", "  the-key  ", "62ae83ad-4f6a-430b-af41-a9bede9286ca", store=store)
    assert result == {"provider": "cartesia", "voice_id": "62ae83ad-4f6a-430b-af41-a9bede9286ca"}
    assert seen == [("cartesia", "the-key", "62ae83ad-4f6a-430b-af41-a9bede9286ca", "cartesia")]
    parsed = tomllib.loads(profile.read_text())
    assert parsed["stt"]["provider"] == parsed["tts"]["provider"] == "cartesia" and parsed["stt"]["model"] == "ink-2"
    assert parsed["tts"]["voice_id"] == "62ae83ad-4f6a-430b-af41-a9bede9286ca"
    reference = parsed["credentials"]["refs"]["CARTESIA_API_KEY"]
    assert store.get(reference) == "the-key" and "the-key" not in profile.read_text()
    with pytest.raises(ValueError, match="invalid_credential"):
        await settings_page.switch_speech_service(profile, "deepgram", "bad", "", store=store)
    assert tomllib.loads(profile.read_text())["tts"]["provider"] == "cartesia"
    with pytest.raises(ValueError, match="unsupported"):
        await settings_page.switch_speech_service(profile, "local", "x", "", store=store)


async def test_page_sets_up_a_discord_bot_beside_the_profile(profile, monkeypatch, tmp_path):
    """Token in, server and channel picked, owner named: a Discord profile is
    written that reuses this profile's speech key, and can be installed."""
    from sleeptalk import settings_page
    from sleeptalk.credential_store import CredentialStore
    from sleeptalk.discord_check import Guild, VoiceChannel

    store = CredentialStore(tmp_path / "store", backend="file")
    speech_ref = store.put_validated("openrouter", "or-key", request_id="earlier:openrouter")
    profile.write_text(profile.read_text().replace('credential:' + "a" * 32, speech_ref).replace(
        '[stt]\nprovider = "openrouter"\n', '[stt]\nprovider = "openrouter"\nmodel = "nvidia/parakeet-tdt-0.6b-v3"\n').replace(
        'workspace_dir = "/w"', f'workspace_dir = "{tmp_path}"').replace(
        # A pi the runtime probe can't verify (no native API-key model): the bot is still made.
        'type = "hermes"', 'type = "pi"\nmodel = "some-model"'))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("sleeptalk.credential_store.default_root", lambda: tmp_path / "store")
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path))
    joined = []

    async def validate(provider, value, spec, voice, **models):
        assert provider == "discord" and value == "bot-token"
        return {"application": {"id": "123", "name": "Nightjar", "invite_url": "https://discord.com/oauth2/authorize?client_id=123"}, "guilds": []}

    async def guilds(token):
        return [Guild(id="456", name="My server")] if joined else []

    async def channels(token, guild_id):
        return [VoiceChannel(id="789", name="lounge", eligible=True, missing_permissions=()),
                VoiceChannel(id="790", name="locked", eligible=False, missing_permissions=("connect",))]

    async def probe(*args):
        raise AssertionError("the agent comes from a profile already set up; it isn't probed again")

    monkeypatch.setattr("sleeptalk.private_input.validate_provider", validate)
    monkeypatch.setattr("sleeptalk.discord_check.list_guilds", guilds)
    monkeypatch.setattr("sleeptalk.discord_check.list_voice_channels", channels)
    monkeypatch.setattr("sleeptalk.runtime_probe.probe_runtime", probe)
    setup = settings_page.DiscordSetup()
    summary = await setup.accept_token("bot-token", store=store, profile_path=profile)
    assert summary["application"]["name"] == "Nightjar" and summary["guilds"] == []
    with pytest.raises(Exception):
        await settings_page.create_discord_profile(profile, setup, slug="discord", guild_id="456", channel_id="789", operator_id="111", store=store, state_dir=tmp_path / "state")
    joined.append(True)
    # "I've added it, refresh" asks before any server is picked: the first one's channels come back.
    options = await setup.options("", store=store)
    assert options["guild_id"] == "456" and [c["name"] for c in options["channels"] if c["eligible"]] == ["lounge"]
    options = await setup.options("456", store=store)
    assert [c["name"] for c in options["channels"] if c["eligible"]] == ["lounge"]
    result = await settings_page.create_discord_profile(profile, setup, slug="discord", guild_id="456", channel_id="789", operator_id="111", store=store, state_dir=tmp_path / "state")
    written = Path(result["path"])
    parsed = tomllib.loads(written.read_text())
    assert parsed["transport"]["type"] == "discord" and parsed["transport"]["allowed_channel_ids"] == "789" and parsed["transport"]["operator_ids"] == "111"
    assert parsed["agent"]["type"] == "pi" and parsed["agent"]["model"] == "some-model" and parsed["agent"]["permission_mode"] == "pending"
    assert parsed["tts"]["provider"] == "openrouter" and parsed["credentials"]["refs"]["OPENROUTER_API_KEY"] == speech_ref
    assert parsed["credentials"]["refs"]["DISCORD_BOT_TOKEN"].startswith("credential:") and "bot-token" not in written.read_text()
    with pytest.raises(ValueError, match="profile_exists"):
        await settings_page.create_discord_profile(profile, setup, slug="discord", guild_id="456", channel_id="789", operator_id="111", store=store, state_dir=tmp_path / "state")
    assert settings_page.describe_profile(profile)["discord"]["suggested_profile"] == "discord-2"


async def test_the_link_opens_the_page_in_every_browser_on_this_computer(profile):
    page = SettingsPage(profile, profiles_dir=profile.parent, environ={})
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url("/"))[:-1]
    origin = {"Origin": page.origin}
    try:
        assert (await client.post("/session", json={"token": page.token}, headers=origin)).status == 200
        # Another browser (no cookie yet) opening the same link.
        client.session.cookie_jar.clear()
        assert (await client.post("/session", json={"token": page.token}, headers=origin)).status == 200
        assert (await client.post("/session", json={"token": "wrong"}, headers=origin)).status == 401
    finally:
        await client.close()


async def test_a_sealed_channel_token_opens_one_session_unless_the_browser_already_has_it(profile):
    page = SettingsPage(profile, profiles_dir=profile.parent, environ={})
    page.remote = True
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url("/"))[:-1]
    origin = {"Origin": page.origin}
    try:
        first = await client.post("/session", json={"token": page.token}, headers=origin)
        assert first.status == 200
        cookie = first.cookies[page.cookie_name].value
        # A copy of the link, in another user's hands, no longer opens a session.
        client.session.cookie_jar.clear()
        assert (await client.post("/session", json={"token": page.token}, headers=origin)).status == 401
        # The same browser (it holds the cookie) can open it again in a new tab.
        again = await client.post("/session", json={"token": page.token},
                                  headers={**origin, "Cookie": f"{page.cookie_name}={cookie}"})
        assert again.status == 200
    finally:
        await client.close()


def test_each_service_card_carries_its_models_voices_and_key_state(profile):
    state = describe_profile(profile)
    cards = {s["id"]: s for s in state["speech"]["services"]}
    openrouter = cards["openrouter"]
    assert openrouter["current"] == {"stt": "nvidia/parakeet-tdt-0.6b-v3", "tts": "x-ai/grok-voice-tts-1.0"}
    assert openrouter["key_saved"] is True and cards["cartesia"]["key_saved"] is False
    tts = {m["id"]: m for m in openrouter["models"]["tts"]["models"]}
    assert tts["x-ai/grok-voice-tts-1.0"]["recommended"] and tts["x-ai/grok-voice-tts-1.0"]["latency_ms"]
    assert "eve" in tts["x-ai/grok-voice-tts-1.0"]["voices"]
    # A service not in use shows what it would start with: the recommended models.
    assert cards["deepgram"]["current"]["stt"] == cards["deepgram"]["models"]["stt"]["recommended"]


def test_choosing_a_model_keeps_the_voice_only_when_the_model_has_it(profile):
    apply_setting(profile, "stt.model", "microsoft/mai-transcribe-2")
    apply_setting(profile, "tts.model", "qwen/qwen-audio-3.0-tts-flash")
    parsed = tomllib.loads(profile.read_text())
    assert parsed["stt"]["model"] == "microsoft/mai-transcribe-2"
    assert parsed["tts"]["model"] == "qwen/qwen-audio-3.0-tts-flash"
    # "eve" is a Grok voice, so it moved to the Qwen model's own default voice.
    assert parsed["tts"]["voice_id"] == "longanhuan_v3.6"
    for setting, value in (("tts.model", "no/such-model"), ("stt.model", ""), ("tts.model", "minimax/speech-2.8-turbo")):
        with pytest.raises(ValueError):
            apply_setting(profile, setting, value)


async def test_preview_speaks_through_the_profiles_own_key_only(profile, monkeypatch):
    spoken = []

    async def fake_preview(path, *, model, voice, text):
        spoken.append((path.name, model, voice))
        return b"RIFF....WAVE"

    monkeypatch.setattr("sleeptalk.settings_page.preview_voice", fake_preview)
    page = SettingsPage(profile, profiles_dir=profile.parent, environ={})
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url("/"))[:-1]
    try:
        origin = {"Origin": page.origin}
        assert (await client.post("/api/preview", json={"voice_id": "eve"}, headers=origin)).status == 401
        response = await client.post("/session", json={"token": page.token}, headers=origin)
        headers = {**origin, "Cookie": f"{page.cookie_name}={response.cookies[page.cookie_name].value}",
                   "X-Settings-CSRF": (await response.json())["csrf"]}
        ok = await client.post("/api/preview", json={"voice_id": "eve"}, headers=headers)
        assert ok.status == 200 and ok.content_type == "audio/wav" and await ok.read() == b"RIFF....WAVE"
        assert spoken == [("default.toml", "", "eve")]
        # The page can never hand the preview a key.
        assert (await client.post("/api/preview", json={"voice_id": "eve", "value": "sk-x"}, headers=headers)).status == 400
        csp = ok.headers["Content-Security-Policy"]
        assert "media-src 'self' blob:" in csp and "script-src 'self'" in csp
    finally:
        await client.close()


async def test_finishing_a_discord_profile_locks_it_to_one_channel_and_operator(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from sleeptalk.settings_page import DiscordSetup, lock_discord_profile

    path = tmp_path / "bot.toml"
    path.write_text('[transport]\ntype = "discord"\nguild_id = "10"\n')
    assert describe_profile(path)["discord"]["open"] is False  # a missing field means closed

    async def channels(token, guild_id):
        return [SimpleNamespace(id="20", eligible=True), SimpleNamespace(id="21", eligible=False)]

    monkeypatch.setattr("sleeptalk.discord_check.list_voice_channels", channels)
    store = SimpleNamespace(get=lambda ref: "bot-token")
    setup = DiscordSetup()
    setup.reference, setup.details = "credential:" + "b" * 32, {"guilds": [{"id": "10", "name": "lab"}]}
    for bad in ({"channel_id": "21"}, {"guild_id": "99"}, {"operator_id": "me"}):
        with pytest.raises(ValueError):
            await lock_discord_profile(path, setup, **{"guild_id": "10", "channel_id": "20", "operator_id": "5", **bad}, store=store)
    await lock_discord_profile(path, setup, guild_id="10", channel_id="20", operator_id="5", store=store)
    transport = tomllib.loads(path.read_text())["transport"]
    assert transport["authorization_required"] is True
    assert (transport["allowed_channel_ids"], transport["operator_ids"]) == ("20", "5")


def test_the_template_profile_is_never_offered(tmp_path):
    from sleeptalk.settings_page import editable_profiles

    (tmp_path / "example.toml").write_text(PROFILE)
    (tmp_path / "sleeptalk.toml").write_text(PROFILE)
    assert editable_profiles(tmp_path) == ["sleeptalk"]


async def test_shipped_voice_samples_play_without_a_key_and_nothing_else_is_served(profile, monkeypatch, tmp_path):
    import json

    samples = tmp_path / "voice_samples"
    (samples / "cartesia").mkdir(parents=True)
    (samples / "cartesia" / "sky.mp3").write_bytes(b"ID3fake")
    (samples / "cartesia" / "stray.mp3").write_bytes(b"ID3other")
    (samples / "cartesia" / "es").mkdir()
    (samples / "cartesia" / "es" / "sky.mp3").write_bytes(b"ID3hola")
    (samples / "manifest.json").write_text(json.dumps({"cartesia": {
        "sky": {"file": "cartesia/sky.mp3", "language": "en", "name": "Sky"},
        "sky@es": {"file": "cartesia/es/sky.mp3", "language": "es", "name": "Sky", "voice": "sky"},
        "gone": {"file": "cartesia/gone.mp3", "language": "en"},
    }}))
    monkeypatch.setattr("sleeptalk.settings_page._SAMPLES", samples)
    state = describe_profile(profile)
    assert state["samples"] == {"cartesia": {"sky": {"url": "/samples/cartesia/sky.mp3", "language": "en", "name": "Sky", "model": ""}}}
    # A Spanish page gets the Spanish recording of the same voice, and no English one.
    apply_setting(profile, "language", "es-ES")
    assert describe_profile(profile)["samples"] == {"cartesia": {"sky": {"url": "/samples/cartesia/es/sky.mp3", "language": "es", "name": "Sky", "model": ""}}}
    page = SettingsPage(profile, profiles_dir=profile.parent, environ={})
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url("/"))[:-1]
    try:
        ok = await client.get("/samples/cartesia/sky.mp3")
        assert ok.status == 200 and ok.content_type == "audio/mpeg" and await ok.read() == b"ID3fake"
        assert await (await client.get("/samples/cartesia/es/sky.mp3")).read() == b"ID3hola"
        for path in ("/samples/cartesia/stray.mp3", "/samples/cartesia/gone.mp3", "/samples/manifest.json", "/samples/../settings_page.py"):
            assert (await client.get(path)).status == 404, path
    finally:
        await client.close()


async def test_brand_logos_are_served_by_id_only(profile):
    from sleeptalk.settings_page import brand_icons

    assert {"cartesia", "deepgram", "openrouter", "claude_code", "codex", "cursor", "hermes", "pi"} <= set(brand_icons())
    assert describe_profile(profile)["icons"] == brand_icons()
    page = SettingsPage(profile, profiles_dir=profile.parent, environ={})
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url("/"))[:-1]
    try:
        ok = await client.get("/icons/cartesia.svg")
        assert ok.status == 200 and ok.content_type == "image/svg+xml" and b"<svg" in await ok.read()
        for path in ("/icons/nosuch.svg", "/icons/NOTICE.txt", "/icons/../settings_page.py", "/icons/%2e%2e%2fsettings_page.py.svg"):
            assert (await client.get(path)).status == 404, path
    finally:
        await client.close()


def test_changing_the_language_moves_the_choices_that_followed_it(profile):
    from sleeptalk.settings_page import set_language

    # English to Chinese on OpenRouter: the recommended listening and speaking
    # models follow, and the Grok voice moves to one the Qwen model speaks.
    assert set_language(profile, "zh-CN") == ["listening model", "speaking model", "voice"]
    parsed = tomllib.loads(profile.read_text())
    assert parsed["stt"]["language"] == parsed["tts"]["language"] == "zh-CN"
    assert parsed["stt"]["model"] == "microsoft/mai-transcribe-2"
    assert parsed["tts"]["model"] == "qwen/qwen-audio-3.0-tts-flash" and parsed["tts"]["voice_id"] == "longanhuan_v3.6"
    # And back: each was the Chinese recommendation, so each returns.
    assert set_language(profile, "en-US") == ["listening model", "speaking model", "voice"]
    parsed = tomllib.loads(profile.read_text())
    assert parsed["stt"]["model"] == "nvidia/parakeet-tdt-0.6b-v3"
    assert parsed["tts"]["model"] == "x-ai/grok-voice-tts-1.0" and parsed["tts"]["voice_id"] == "eve"


def test_a_voice_the_person_chose_stays_within_the_language(profile):
    from sleeptalk.settings_page import set_language

    apply_setting(profile, "tts.voice_id", "ara")
    assert set_language(profile, "en-GB") == []
    assert tomllib.loads(profile.read_text())["tts"]["voice_id"] == "ara"


async def test_the_page_words_are_served_for_shipped_languages_only(profile):
    page = SettingsPage(profile, profiles_dir=profile.parent, environ={})
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url("/"))[:-1]
    try:
        ok = await client.get("/i18n/ja.json")
        assert ok.status == 200 and ok.content_type == "application/json"
        for path in ("/i18n/xx.json", "/i18n/en.json", "/i18n/%2e%2e%2fsettings.js", "/i18n/JA.json"):
            assert (await client.get(path)).status == 404, path
    finally:
        await client.close()


def test_every_page_string_is_translated_with_its_placeholders():
    import json
    import re

    from sleeptalk.settings_page import UI_LANGUAGES

    root = Path(__file__).resolve().parents[1] / "sleeptalk" / "_settings"
    script = (root / "settings.js").read_text(encoding="utf-8")
    # Every literal handed to t(); the few passed indirectly are covered by the files' own keys.
    used = set(re.findall(r'\bt\("((?:[^"\\]|\\.)*)"', script))
    assert len(used) > 120
    keys = None
    for lang in UI_LANGUAGES:
        table = json.loads((root / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        assert not used - set(table), (lang, sorted(used - set(table))[:5])
        assert keys is None or set(table) == keys, lang
        keys = set(table)
        for english, text in table.items():
            assert set(re.findall(r"\{\w+\}", english)) == set(re.findall(r"\{\w+\}", text)), (lang, english)
            assert text.strip(), (lang, english)


def test_a_new_language_starts_on_a_measured_native_voice(tmp_path):
    from sleeptalk.settings_page import set_language

    path = tmp_path / "d.toml"
    path.write_text(PROFILE.replace("openrouter", "deepgram").replace('voice_id = "eve"', 'voice_id = "flux-kit-en"'))
    assert set_language(path, "es-MX") == ["speaking model", "voice"]
    assert tomllib.loads(path.read_text())["tts"]["voice_id"] == "aura-2-luciano-es"
    state = describe_profile(path)
    card = next(s for s in state["speech"]["services"] if s["id"] == "deepgram")
    assert card["current"]["tts"] == "aura-2"
    assert state["speech"]["voices_by_service"]["deepgram"]["voices"][0]["name"] == "Luciano"
    # Cartesia: sonic-3 stays; the English starting voice becomes the native one.
    path.write_text(PROFILE.replace("openrouter", "cartesia").replace('voice_id = "eve"', 'voice_id = "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"'))
    assert "voice" in set_language(path, "ja-JP")
    assert tomllib.loads(path.read_text())["tts"]["voice_id"] == "aa3ccc4a-cfd8-405c-acf5-1873a527b006"


def test_the_english_lexicon_stays_out_of_other_languages():
    from sleeptalk.speech.common import _deepgram_query_params_from_spec
    from sleeptalk.speech.lexicon import apply_pronunciation
    from sleeptalk.providers import ProviderSpec

    assert apply_pronunciation("Call the API", "es-ES") == "Call the API"
    assert apply_pronunciation("Call the API", "en-US") != "Call the API"
    spec = ProviderSpec(provider="deepgram", model="nova-3", language="hi-IN")
    assert ("language", "hi") in _deepgram_query_params_from_spec(spec)


def test_installed_agents_come_from_programs_on_path(monkeypatch):
    from sleeptalk import settings_page, wizard

    monkeypatch.setattr(setup_core, "detect_agents", lambda: {"acp": "hermes", "codex": "codex", "cursor_acp": "agent"})
    assert settings_page.installed_agents() == ["hermes", "codex"]
    monkeypatch.setattr(setup_core, "detect_agents", lambda: (_ for _ in ()).throw(OSError()))
    assert settings_page.installed_agents() == []


def test_every_provider_explains_its_allowance_in_plain_words():
    import json

    facts = json.loads((Path(__file__).resolve().parents[1] / "sleeptalk" / "_setup" / "providers.json").read_text())
    for item in facts["providers"]:
        assert len(item["usage"]) > 60, item["id"]


async def test_a_call_hands_the_caller_a_phone_settings_link(profile, monkeypatch):
    from sleeptalk import remote_settings
    from sleeptalk.web_voice import session as web_session

    served = []

    async def serve_link(page, *, ready=None, **kwargs):
        served.append(page.profile_path)
        ready("https://call.sleep.talk/settings#/settings/" + "r" * 24 + "/" + "t" * 43 + "/" + "k" * 87, None)
        await asyncio.Event().wait()

    monkeypatch.setattr(remote_settings, "serve_link", serve_link)
    notice, url = await web_session.open_host_settings(str(profile))
    assert "ready" in notice and url.startswith("https://call.sleep.talk/settings#/settings/") and served == [profile]
    # A second request replaces the first link rather than stacking them.
    first = web_session._settings_server
    await web_session.open_host_settings(str(profile))
    await asyncio.sleep(0)
    assert first.cancelled() or first.done()
    assert (await web_session.open_host_settings(""))[0].startswith("Settings aren't available")
    web_session._settings_server.cancel()


async def test_without_the_call_service_settings_open_on_the_host(profile, monkeypatch):
    import webbrowser

    from sleeptalk import remote_settings
    from sleeptalk.web_voice import session as web_session

    async def unavailable(page, **kwargs):
        raise RuntimeError("no hosted service")

    monkeypatch.setattr(remote_settings, "serve_link", unavailable)
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    notice, url = await web_session.open_host_settings(str(profile))
    assert "opened" in notice and url == "" and opened[0].startswith("http://127.0.0.1:")
    monkeypatch.setattr(webbrowser, "open", lambda url: False)
    assert "No browser" in (await web_session.open_host_settings(str(profile)))[0]
    web_session._settings_server.cancel()


@pytest.fixture
def web_setup(monkeypatch):
    """New-profile creation without live provider calls or local agent binaries."""
    proven = []

    async def prove(provider, value, voice, **kwargs):
        proven.append((provider, value, voice, kwargs["language"]))
        if value == "bad":
            raise ValueError("invalid_credential")
        return {"voice_id": voice or "eve"}, "settings-test"

    async def valid(answers, *, live=True):
        return []

    monkeypatch.setattr("sleeptalk.settings_page.prove_speech_key", prove)
    monkeypatch.setattr("sleeptalk.setup_core.validate_answers", valid)
    monkeypatch.setattr("sleeptalk.setup.ensure_dependencies", lambda surface, repair=False: [])
    return proven


async def test_a_new_profile_is_made_in_the_browser_like_the_terminal_wizard(tmp_path, web_setup):
    from sleeptalk.credential_store import CredentialStore
    from sleeptalk.settings_page import create_profile

    profiles = tmp_path / "profiles"
    workspace = tmp_path / "work"
    workspace.mkdir()
    slug = await create_profile(profiles, tmp_path / ".env", {
        "name": "Work Voice", "workspace": str(workspace), "agent": "claude_code", "language": "es-MX",
        "provider": "openrouter", "value": "sk-or-" + "k" * 20, "voice": "ara", "surface": "web", "delivery": "tailnet"})
    assert slug == "work-voice"
    parsed = tomllib.loads((profiles / "work-voice.toml").read_text())
    assert parsed["agent"]["type"] == "claude_code" and parsed["agent"]["workspace_dir"] == str(workspace.resolve())
    assert parsed["stt"]["provider"] == parsed["tts"]["provider"] == "openrouter"
    assert parsed["tts"]["language"] == "es-MX" and parsed["tts"]["voice_id"] == "ara"
    assert parsed["transport"]["options"]["delivery"] == "tailnet"
    reference = parsed["credentials"]["refs"]["OPENROUTER_API_KEY"]
    assert reference.startswith("credential:") and "k" * 20 not in (profiles / "work-voice.toml").read_text()
    assert not (tmp_path / ".env").exists() or "k" * 20 not in (tmp_path / ".env").read_text()
    assert CredentialStore().get(reference).endswith("k" * 20)
    assert web_setup == [("openrouter", "sk-or-" + "k" * 20, "ara", "es-MX")]


async def test_a_new_profile_refuses_what_the_browser_cannot_set_up(tmp_path, web_setup):
    from sleeptalk.settings_page import create_profile

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "taken.toml").write_text(PROFILE)
    good = {"name": "Fresh", "workspace": str(tmp_path), "agent": "pi", "language": "en-US",
            "provider": "cartesia", "value": "key-123456", "surface": "web"}
    for change, code in (({"name": "Taken"}, "profile_exists"), ({"name": ""}, "bad_profile_name"),
                         ({"name": "Example"}, "bad_profile_name"), ({"workspace": str(tmp_path / "nope")}, "bad_workspace"),
                         ({"agent": "gremlin"}, "unsupported"), ({"agent": "openclaw", "openclaw": {"token_source": "paste", "token": ""}}, "openclaw_token_missing"),
                         ({"provider": "local", "language": "es-MX"}, "local_english_only"),
                         ({"provider": "unknown"}, "unsupported"), ({"language": "xx-YY"}, "unsupported"),
                         ({"value": ""}, "invalid_credential"), ({"value": "bad"}, "invalid_credential")):
        with pytest.raises(ValueError, match=code):
            await create_profile(profiles, tmp_path / ".env", {**good, **change})
    # A key that fails its check writes nothing.
    assert sorted(p.name for p in profiles.iterdir()) == ["taken.toml"]


@pytest.mark.parametrize("model", ["gpt-realtime-2.1", "gpt-live-1", ""])
async def test_browser_creates_openai_profile_with_selected_protocol(tmp_path, web_setup, monkeypatch, model):
    from sleeptalk.credential_store import CredentialStore

    checked = []

    async def realtime(config):
        checked.append(("realtime", config))

    async def live(config):
        checked.append(("live", config))

    monkeypatch.setattr("sleeptalk.openai_realtime.check_openai_connectivity", realtime)
    monkeypatch.setattr("sleeptalk.openai_live.check_openai_live_connectivity", live)
    profiles = tmp_path / "profiles"
    page = SettingsPage(profiles / "default.toml", profiles_dir=profiles, environ={})
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url("/"))[:-1]
    try:
        csrf = (await (await client.post("/session", json={"token": page.token}, headers={"Origin": page.origin})).json())["csrf"]
        headers = {"X-Settings-CSRF": csrf, "Origin": page.origin}
        options = await (await client.get("/api/new?language=es-MX", headers=headers)).json()
        service = next(item for item in options["services"] if item["id"] == "openai")
        assert service["name"] == "OpenAI Realtime" and service["takes_key"]
        for role in ("stt", "tts"):
            choices = service["models"][role]
            assert choices["recommended"] == service["current"][role] == "gpt-live-1"
            assert [item["id"] for item in choices["models"]] == ["gpt-live-1", "gpt-realtime-2.1"]
            assert [item["label"] for item in choices["models"]] == ["GPT Live", "GPT Realtime"]
            assert not any(item["recommended"] for item in choices["models"])
        assert "preview" not in str(service).lower()
        assert options["voices_by_service"]["openai"]["voices"]
        response = await client.post("/api/new", headers=headers, json={
            "name": "Fresh", "workspace": str(tmp_path), "agent": "codex", "language": "es-MX",
            "provider": "openai", "value": "fake-openai-secret", "voice": "cedar", "surface": "web",
            "delivery": "local", **({"stt_model": model, "tts_model": model} if model else {}),
        })
        assert response.status == 200, await response.text()
        assert (await response.json())["profile"] == "fresh"
    finally:
        await client.close()
    model = model or "gpt-live-1"
    text = (profiles / "fresh.toml").read_text()
    document = tomllib.loads(text)
    assert document["route"]["type"] == "hybrid" and document["route"]["provider"] == "openai-realtime"
    assert document["route"]["model"] == model and document["route"]["voice"] == "cedar"
    assert document["route"]["language"] == "es-MX"
    assert "stt" not in document and "provider" not in document.get("tts", {})
    assert document["agent"]["type"] == "codex"
    assert document["transport"]["options"]["delivery"] == "local"
    assert "fake-openai-secret" not in text and not (tmp_path / ".env").exists()
    assert CredentialStore().get(document["credentials"]["refs"]["OPENAI_API_KEY"]) == "fake-openai-secret"
    assert len(checked) == 1 and checked[0][0] == ("live" if model == "gpt-live-1" else "realtime")
    config = checked[0][1]
    assert (config.model, config.voice, config.language) == (model, "cedar", "es-MX")
    assert web_setup == []


@pytest.mark.parametrize("changes,code", [
    ({"tts_model": "unknown"}, "unsupported"),
    ({"stt_model": "gpt-realtime-2.1", "tts_model": "gpt-live-1"}, "unsupported"),
    ({"tts_model": "gpt-live-1", "surface": "terminal"}, "requires browser"),
    ({"voice": "unlisted"}, "voice_unavailable"),
    ({"value": ""}, "invalid_credential"),
])
async def test_new_openai_profile_rejects_invalid_choices_before_probing(tmp_path, monkeypatch, changes, code):
    from sleeptalk.settings_page import create_profile

    async def unexpected(config):
        pytest.fail("invalid choices must not contact OpenAI")

    monkeypatch.setattr("sleeptalk.openai_realtime.check_openai_connectivity", unexpected)
    monkeypatch.setattr("sleeptalk.openai_live.check_openai_live_connectivity", unexpected)
    with pytest.raises(ValueError, match=code):
        await create_profile(tmp_path / "profiles", tmp_path / ".env", {
            "name": "Fresh", "workspace": str(tmp_path), "agent": "codex", "provider": "openai",
            "value": "fake-secret", **changes,
        })
    assert not (tmp_path / "profiles").exists()


@pytest.mark.parametrize("model", ["gpt-realtime-2.1", "gpt-live-1"])
async def test_failed_new_openai_probe_never_saves_or_exposes_key(tmp_path, monkeypatch, model):
    from sleeptalk.settings_page import create_profile

    async def failed(config):
        raise RuntimeError("provider response containing fake-secret")

    def unexpected(*args, **kwargs):
        pytest.fail("a failed probe must not store credentials")

    monkeypatch.setattr("sleeptalk.openai_realtime.check_openai_connectivity", failed)
    monkeypatch.setattr("sleeptalk.openai_live.check_openai_live_connectivity", failed)
    monkeypatch.setattr("sleeptalk.credential_store.CredentialStore.put_validated", unexpected)
    with pytest.raises(ValueError) as error:
        await create_profile(tmp_path / "profiles", tmp_path / ".env", {
            "name": "Fresh", "workspace": str(tmp_path), "agent": "codex", "provider": "openai",
            "value": "fake-secret", "tts_model": model,
        })
    assert "fake-secret" not in str(error.value)
    assert not (tmp_path / "profiles").exists() and not (tmp_path / ".env").exists()


def test_new_profile_offers_local_speech_with_no_key(tmp_path):
    from sleeptalk.settings_page import new_profile_options

    options = new_profile_options(tmp_path, "en-US")
    local = next(item for item in options["services"] if item["id"] == "local")
    assert local["takes_key"] is False and local["english_only"] is False and local["voice"]
    assert local["models"]["stt"]["recommended"] and local["models"]["tts"]["recommended"]
    assert all(item["takes_key"] for item in options["services"] if item["id"] != "local")
    assert options["voices_by_service"]["local"]["voices"]
    # Other languages still list it, marked English only.
    assert next(item for item in new_profile_options(tmp_path, "es-MX")["services"] if item["id"] == "local")["english_only"]


async def test_a_local_profile_is_created_as_a_job_with_no_key(tmp_path, web_setup, monkeypatch):
    proofs = []

    async def prove(stt, tts, voice, language, job):
        proofs.append((stt.id, tts.id, voice, language))
        job.stage = "checking"

    monkeypatch.setattr("sleeptalk.settings_page.prove_local_speech", prove)
    profiles = tmp_path / "profiles"
    page = SettingsPage(profiles / "default.toml", profiles_dir=profiles, environ={})
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url("/"))[:-1]
    try:
        csrf = (await (await client.post("/session", json={"token": page.token}, headers={"Origin": page.origin})).json())["csrf"]
        headers = {"X-Settings-CSRF": csrf, "Origin": page.origin}
        body = {"name": "Quiet", "workspace": str(tmp_path), "agent": "pi", "language": "en-US", "provider": "local",
                "surface": "web", "delivery": "local"}
        started = await (await client.post("/api/new", json=body, headers=headers)).json()
        assert started["status"] == "started" and started["job"]["kind"] == "create"
        for _ in range(100):
            job = (await (await client.post("/api/speech/local/status", json={}, headers=headers)).json())["job"]
            if job["stage"] in ("done", "failed"):
                break
            await asyncio.sleep(0.02)
        assert job["stage"] == "done" and job["profile"] == "quiet", job
        written = tomllib.loads((profiles / "quiet.toml").read_text())
        assert written["stt"]["provider"] == written["tts"]["provider"] == "local"
        assert written["tts"]["voice_id"] == proofs[0][2]
        assert "credentials" not in written or not written["credentials"].get("refs")
        assert page.profile_path == profiles / "quiet.toml"
        # Proven before anything was written, with the language's own check.
        assert proofs == [("moonshine-small-streaming-en", "kokoro-en", "af_heart", "en-US")]
    finally:
        await client.close()


async def test_a_local_profile_that_fails_its_check_writes_nothing(tmp_path, web_setup, monkeypatch):
    from sleeptalk.settings_page import LocalSpeechJob, create_profile

    async def prove(stt, tts, voice, language, job):
        raise ValueError("local_download_failed")

    monkeypatch.setattr("sleeptalk.settings_page.prove_local_speech", prove)
    profiles = tmp_path / "profiles"
    with pytest.raises(ValueError, match="local_download_failed"):
        await create_profile(profiles, tmp_path / ".env", {"name": "Quiet", "workspace": str(tmp_path), "agent": "pi",
                                                           "language": "en-US", "provider": "local", "surface": "web"},
                             local_job=LocalSpeechJob("create"))
    assert not (profiles / "quiet.toml").exists()


async def test_the_page_opens_on_new_profile_when_there_is_none(tmp_path, web_setup):
    profiles = tmp_path / "profiles"
    page = SettingsPage(profiles / "default.toml", profiles_dir=profiles, environ={})
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url("/"))[:-1]
    try:
        csrf = (await (await client.post("/session", json={"token": page.token}, headers={"Origin": page.origin})).json())["csrf"]
        headers = {"X-Settings-CSRF": csrf, "Origin": page.origin}
        state = await (await client.get("/api/state", headers=headers)).json()
        assert state["profile"] is None and state["profiles"] == []
        options = await (await client.get("/api/new?language=ja-JP", headers=headers)).json()
        assert options["language"] == "ja-JP" and {"pi", "claude_code"} <= {a["id"] for a in options["agents"] if a["web"]}
        cartesia = next(s for s in options["services"] if s["id"] == "cartesia")
        assert cartesia["voice"] == "aa3ccc4a-cfd8-405c-acf5-1873a527b006"
        assert "key" not in json.dumps(options["reusable"]).lower() or options["reusable"] == {}
        body = {"name": "default", "workspace": str(tmp_path), "agent": "pi", "language": "ja-JP",
                "provider": "cartesia", "value": "key-123456", "surface": "web"}
        created = await client.post("/api/new", json=body, headers=headers)
        assert created.status == 200 and (await created.json())["profile"] == "default"
        state = await (await client.get("/api/state", headers=headers)).json()
        assert state["profile"] == "default" and state["language"]["value"] == "ja-JP"
        refused = await client.post("/api/new", json={**body, "extra": "x"}, headers=headers)
        assert refused.status == 400
    finally:
        await client.close()


async def test_changing_the_agent_rewrites_only_the_agent_tables(profile, tmp_path):
    from sleeptalk.settings_page import change_agent

    apply_setting(profile, "policy.endpointing", "relaxed")
    profile.write_text(profile.read_text() + '\n[policy.interrupt]\nmode = "immediate"\n')
    profile.chmod(0o600)
    before = tomllib.loads(profile.read_text())
    await change_agent(profile, profile.parent, tmp_path / ".env", {"agent": "codex", "workspace": str(tmp_path)})
    after = tomllib.loads(profile.read_text())
    # Still readable only by this user.
    assert profile.stat().st_mode & 0o077 == 0
    assert after["agent"]["type"] == "codex" and after["agent"]["workspace_dir"] == str(tmp_path.resolve())
    # Everything the terminal writer doesn't model is kept.
    assert after["policy"]["endpointing"] == "relaxed" and after["policy"]["interrupt"]["mode"] == "immediate"
    assert after["stt"] == before["stt"] and after["tts"] == before["tts"] and after["credentials"] == before["credentials"]
    with pytest.raises(ValueError, match="bad_workspace"):
        await change_agent(profile, profile.parent, tmp_path / ".env", {"agent": "pi", "workspace": str(tmp_path / "nope")})
    with pytest.raises(ValueError, match="unsupported"):
        await change_agent(profile, profile.parent, tmp_path / ".env", {"agent": "gremlin"})
    assert tomllib.loads(profile.read_text())["agent"]["type"] == "codex"


async def test_openclaw_and_a_model_ask_what_the_terminal_asks(profile, tmp_path, monkeypatch):
    from sleeptalk import model_only_setup
    from sleeptalk.credential_store import CredentialStore
    from sleeptalk.settings_page import change_agent

    env = tmp_path / ".env"
    await change_agent(profile, profile.parent, env, {"agent": "openclaw", "openclaw": {"token_source": "paste", "token": "claw-token", "memory": "main"}})
    parsed = tomllib.loads(profile.read_text())
    assert parsed["agent"]["type"] == "openclaw" and "claw-token" not in profile.read_text()
    assert not env.exists() or "claw-token" not in env.read_text()
    from sleeptalk.credential_store import CredentialStore
    assert CredentialStore().get(parsed["credentials"]["refs"]["OPENCLAW_GATEWAY_TOKEN"]) == "claw-token"
    assert "agent:main:main" in profile.read_text()

    async def verified(choice, model, key):
        return "available" if key == "sk-model-123456" else "unavailable"

    monkeypatch.setattr(model_only_setup, "verify_key", verified)
    with pytest.raises(ValueError, match="detail:"):
        await change_agent(profile, profile.parent, env, {"agent": "model", "model": {"vendor": "openrouter", "model": "x/y", "key": "wrong"}})
    outcome = await change_agent(profile, profile.parent, env, {"agent": "model", "model": {"vendor": "openrouter", "model": "x/y", "key": "sk-model-123456"}})
    parsed = tomllib.loads(profile.read_text())
    assert parsed["agent"]["type"] == "model" and "answers here" in outcome["note"]
    reference = parsed["agent"]["auth"]["credential_ref"]
    assert CredentialStore().get(reference) == "sk-model-123456" and "sk-model-123456" not in profile.read_text()


async def test_moving_the_workspace_keeps_the_agents_answers(profile, tmp_path):
    from sleeptalk.settings_page import change_agent, change_workspace

    env = tmp_path / ".env"
    await change_agent(profile, profile.parent, env, {"agent": "openclaw", "openclaw": {"token_source": "paste", "token": "claw-token"}})
    moved = change_workspace(profile, profile.parent, str(tmp_path))
    parsed = tomllib.loads(profile.read_text())
    assert moved == str(tmp_path.resolve()) and parsed["agent"]["workspace_dir"] == moved
    assert parsed["agent"]["type"] == "openclaw" and "agent:main:main" in profile.read_text()
    with pytest.raises(ValueError, match="bad_workspace"):
        change_workspace(profile, profile.parent, "relative/path")


async def test_my_own_agent_is_checked_like_the_terminal_does(profile, tmp_path):
    import json as _json

    from sleeptalk.settings_page import change_agent

    env = tmp_path / ".env"
    outcome = await change_agent(profile, profile.parent, env, {"agent": "own", "workspace": str(tmp_path), "own": {"kind": "starter", "language": "python"}})
    parsed = tomllib.loads(profile.read_text())
    assert parsed["agent"]["type"] == "command" and outcome["checked"]
    assert outcome["starter"].endswith("agent.py") and Path(outcome["starter"]).is_file()
    broken = {"agent": "own", "own": {"kind": "command", "command": "definitely-not-a-program --serve"}}
    with pytest.raises(ValueError) as failure:
        await change_agent(profile, profile.parent, env, broken)
    assert str(failure.value).startswith("check:") and _json.loads(str(failure.value)[6:])["failures"]
    assert tomllib.loads(profile.read_text())["agent"]["options"]["command"][0] == "python3"
    kept = await change_agent(profile, profile.parent, env, {"agent": "own", "own": {**broken["own"], "keep": True}})
    assert not kept["checked"] and tomllib.loads(profile.read_text())["agent"]["options"]["command"] == ["definitely-not-a-program", "--serve"]
    with pytest.raises(ValueError, match="detail:"):
        await change_agent(profile, profile.parent, env, {"agent": "own", "own": {"kind": "http", "url": "ftp://nope"}})


def test_where_it_talks_and_discord_settings_stay_with_their_kind_of_profile(profile, tmp_path):
    apply_setting(profile, "transport.type", "terminal")
    apply_setting(profile, "transport.capture_mode", "headphones")
    apply_setting(profile, "log.latency", False)
    parsed = tomllib.loads(profile.read_text())
    assert parsed["transport"]["type"] == "terminal" and parsed["transport"]["capture_mode"] == "headphones"
    assert parsed["log"]["latency"] is False
    for setting, value in (("agent.name", "Bot"), ("transport.operator_ids", "123"), ("log.latency", "no"), ("transport.type", "discord")):
        with pytest.raises(ValueError):
            apply_setting(profile, setting, value)
    bot = tmp_path / "bot.toml"
    bot.write_text(PROFILE.replace('[transport]\ntype = "web"', '[transport]\ntype = "discord"'))
    apply_setting(bot, "agent.name", "Night Owl")
    apply_setting(bot, "transport.operator_ids", "111, 222")
    apply_setting(bot, "transport.authorization_required", True)
    parsed = tomllib.loads(bot.read_text())
    assert parsed["agent"]["name"] == "Night Owl" and parsed["transport"]["operator_ids"] == "111,222"
    for setting, value in (("transport.type", "web"), ("transport.operator_ids", "12a"), ("agent.name", 'a"b')):
        with pytest.raises(ValueError):
            apply_setting(bot, setting, value)


def test_renaming_keeps_the_keys_and_deleting_asks_for_the_name(profile, monkeypatch):
    from sleeptalk.settings_page import delete_profile, rename_profile, start_call

    moved = rename_profile(profile, profile.parent, "Work Voice")
    assert moved.name == "work-voice.toml" and not profile.exists()
    parsed = tomllib.loads(moved.read_text())
    assert parsed["agent"]["slug"] == "work-voice" and parsed["credentials"]["refs"]["OPENROUTER_API_KEY"].startswith("credential:")
    with pytest.raises(ValueError, match="profile_exists"):
        rename_profile(moved, moved.parent, "second")
    seen = {}
    monkeypatch.setattr("sleeptalk.host.start_voice_web", lambda **kw: seen.update(kw) or {"status": "started", "join_url": "http://127.0.0.1:1/j", "delivery": "local"})
    assert start_call(moved, here=True)["join_url"].startswith("http://127.0.0.1")
    assert seen["local"] is True and seen["tailnet"] is False and seen["profile"] == "work-voice"
    assert "qr_svg" not in start_call(moved, here=True)
    # A link for another device comes with a QR code to scan it from the screen.
    assert start_call(moved, here=False)["qr_svg"].startswith("<svg")
    assert seen["local"] is False
    delete_profile(moved)
    assert not moved.exists()
    with pytest.raises(ValueError):
        delete_profile(moved)


def test_terminal_edits_keep_turn_taking_set_on_the_page(profile):
    from sleeptalk.wizard import load_existing_answers, write_files

    apply_setting(profile, "policy.endpointing", "relaxed")
    profile.write_text(profile.read_text() + '\n[policy.interrupt]\nmode = "immediate"\n')
    answers = load_existing_answers(profile.stem, profile.parent)
    answers.narration_style = "silence"
    write_files(answers, profiles_dir=profile.parent, env_path=profile.parent.parent / ".env")
    parsed = tomllib.loads(profile.read_text())
    assert parsed["policy"]["narration_style"] == "silence"
    assert parsed["policy"]["endpointing"] == "relaxed" and parsed["policy"]["interrupt"]["mode"] == "immediate"


def test_readiness_lists_what_is_missing_with_its_install_command(monkeypatch):
    from sleeptalk.agents.readiness import AgentReadiness, Need
    from sleeptalk.settings_page import agent_readiness

    missing = AgentReadiness(name="pi", needs=(Need(kind="binary", key="pi", hint="Install pi.", found=False, install_cmd=("npm", "i", "-g", "pi")),))
    monkeypatch.setattr("sleeptalk.agents.readiness.probe", lambda name: missing)
    report = agent_readiness("pi")
    assert report == {"ready": False, "missing": [{"hint": "Install pi.", "command": "npm i -g pi", "installable": True}]}


async def test_other_apps_keys_are_looked_for_only_after_the_person_allows_it(profile, tmp_path, monkeypatch):
    """Opening settings reads no other tool's files. "Look on this computer"
    turns it on, remembered; a found key is used only when chosen."""
    from sleeptalk.key_discovery import ReusableKey

    looked = []

    def discover(**kwargs):
        looked.append(1)
        return [ReusableKey(provider="groq", env="GROQ_API_KEY", kind="dotenv", location="/x/.env", source="~/.hermes/.env")]

    monkeypatch.setattr("sleeptalk.key_discovery.discover", discover)
    monkeypatch.setattr("sleeptalk.key_discovery.read_value", lambda *a, **k: pytest.fail("read without a choice"))
    page = SettingsPage(profile, profiles_dir=profile.parent, environ={}, state_dir=tmp_path / "state")
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url("/"))[:-1]
    try:
        csrf = (await (await client.post("/session", json={"token": page.token}, headers={"Origin": page.origin})).json())["csrf"]
        headers = {"X-Settings-CSRF": csrf, "Origin": page.origin}
        state = await (await client.get("/api/state", headers=headers)).json()
        assert state["speech"]["reusable"] == {} and state["speech"]["key_lookup"]["allowed"] is False
        assert "~/.hermes" in state["speech"]["key_lookup"]["places"]
        options = await (await client.get("/api/new", headers=headers)).json()
        assert options["reusable"] == {} and options["key_lookup"]["allowed"] is False
        refused = await client.post("/api/speech", json={"provider": "groq", "reuse": True}, headers=headers)
        assert refused.status == 422 and (await refused.json())["code"] == "key_not_found"
        assert looked == []

        turned = await (await client.post("/api/keys/lookup", json={"allowed": True}, headers=headers)).json()
        assert turned["reusable"]["groq"]["source"] == "~/.hermes/.env" and turned["key_lookup"]["allowed"]
        again = SettingsPage(profile, profiles_dir=profile.parent, environ={}, state_dir=tmp_path / "state")
        assert again.look_for_keys  # remembered for the next session
        state = await (await client.get("/api/state", headers=headers)).json()
        assert "groq" in state["speech"]["reusable"] and "/x/.env" not in json.dumps(state)
        await client.post("/api/keys/lookup", json={"allowed": False}, headers=headers)
        state = await (await client.get("/api/state", headers=headers)).json()
        assert state["speech"]["reusable"] == {}
        bad = await client.post("/api/keys/lookup", json={"allowed": "yes"}, headers=headers)
        assert bad.status == 400
    finally:
        await client.close()


def test_the_keys_page_says_where_keys_really_are(profile, tmp_path, monkeypatch):
    from sleeptalk.credential_store import CredentialStore

    root = tmp_path / "credential-store"
    monkeypatch.setattr("sleeptalk.credential_store.default_root", lambda: root)
    assert describe_profile(profile)["key_store"]["kind"] == "none"
    with CredentialStore(root, backend="file")._database():
        pass
    assert describe_profile(profile)["key_store"]["kind"] == "file"


async def test_the_page_on_this_computer_stays_open_while_idle(profile, tmp_path):
    from sleeptalk import settings_page

    page = SettingsPage(profile, profiles_dir=profile.parent, environ={}, state_dir=tmp_path / "state")
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url("/"))[:-1]
    try:
        opened = await client.post("/session", json={"token": page.token}, headers={"Origin": page.origin})
        assert "max-age" not in opened.headers["Set-Cookie"].lower()
        headers = {"X-Settings-CSRF": (await opened.json())["csrf"], "Origin": page.origin}
        page.last_used -= settings_page.IDLE_SECONDS * 10
        assert (await client.get("/api/state", headers=headers)).status == 200
    finally:
        await client.close()


async def test_finish_setup_shows_why_the_profile_was_refused(profile, tmp_path, monkeypatch):
    from aiohttp import web as aioweb

    from sleeptalk import settings_page

    async def refuse(*args, **kwargs):
        raise aioweb.HTTPConflict(text="Validate each provider before finishing.")

    monkeypatch.setattr(settings_page, "create_discord_profile", refuse)
    page = SettingsPage(profile, profiles_dir=profile.parent, environ={}, state_dir=tmp_path / "state")
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url("/"))[:-1]
    try:
        csrf = (await (await client.post("/session", json={"token": page.token}, headers={"Origin": page.origin})).json())["csrf"]
        body = {"profile": "bot", "guild_id": "1", "channel_id": "2", "operator_id": "3"}
        refused = await client.post("/api/discord/finish", json=body, headers={"X-Settings-CSRF": csrf, "Origin": page.origin})
        assert refused.status == 422
        assert await refused.json() == {"status": "refused", "code": "detail", "message": "Validate each provider before finishing."}
    finally:
        await client.close()


async def test_an_idle_sealed_channel_session_ends(profile, tmp_path):
    from sleeptalk import settings_page

    page = SettingsPage(profile, profiles_dir=profile.parent, environ={}, state_dir=tmp_path / "state")
    page.remote = True
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url("/"))[:-1]
    try:
        csrf = (await (await client.post("/session", json={"token": page.token}, headers={"Origin": page.origin})).json())["csrf"]
        headers = {"X-Settings-CSRF": csrf, "Origin": page.origin}
        assert (await client.get("/api/state", headers=headers)).status == 200
        page.last_used -= settings_page.IDLE_SECONDS + 1
        assert (await client.get("/api/state", headers=headers)).status == 401
        # The link can't reopen it: the session is over until the link is made again.
        again = await client.post("/session", json={"token": page.token}, headers={"Origin": page.origin})
        assert again.status == 401
        assert (await client.get("/api/state", headers=headers)).status == 401
    finally:
        await client.close()


async def test_just_a_model_reuses_the_openrouter_key_speech_already_uses(profile, tmp_path, monkeypatch):
    from sleeptalk import model_only_setup
    from sleeptalk.credential_store import CredentialStore
    from sleeptalk.settings_page import agent_setup_options, change_agent

    reference = CredentialStore().put_validated("openrouter", "sk-or-speech-123456")
    profile.write_text(PROFILE.replace("credential:" + "a" * 32, reference))
    offered = agent_setup_options(profile)["reusable_model_keys"]
    assert offered == {"openrouter": {"source": "speech", "label": "this profile's speech key"}}
    assert "sk-or" not in json.dumps(offered)
    seen = []

    async def verified(choice, model, key):
        seen.append(key)
        return "available"

    monkeypatch.setattr(model_only_setup, "verify_key", verified)
    outcome = await change_agent(profile, profile.parent, tmp_path / ".env",
                                 {"agent": "model", "model": {"vendor": "openrouter", "model": "x/y", "reuse": True}})
    parsed = tomllib.loads(profile.read_text())
    assert seen == ["sk-or-speech-123456"] and "answers here" in outcome["note"]
    assert CredentialStore().get(parsed["agent"]["auth"]["credential_ref"]) == "sk-or-speech-123456"
    assert "sk-or-speech" not in profile.read_text()
    # Nothing to reuse for a vendor that isn't a speech service: the key is still asked for.
    with pytest.raises(ValueError, match="key_not_found|model_key_required"):
        await change_agent(profile, profile.parent, tmp_path / ".env",
                           {"agent": "model", "model": {"vendor": "openai", "model": "gpt-x", "reuse": True}})


async def test_vendor_models_lists_chat_models_only(monkeypatch):
    from aiohttp import web

    from sleeptalk import model_only_setup as setup

    seen = {}

    async def models(request):
        seen[request.path] = dict(request.headers)
        if request.path.startswith("/anthropic/"):
            return web.json_response({"data": [{"id": "claude-x"}, {"id": "claude-y"}]})
        return web.json_response({"data": [
            {"id": "gpt-chat"}, {"id": "text-embedding-3-small"}, {"id": "whisper-1"}, {"id": "gpt-4o-realtime"},
            {"id": "a/b", "architecture": {"output_modalities": ["text"]}},
            {"id": "a/image", "architecture": {"output_modalities": ["image"]}}, {"bad": 1}]})

    app = web.Application()
    app.router.add_get("/{tail:.*}", models)
    server = TestServer(app)
    await server.start_server()
    base = f"http://127.0.0.1:{server.port}"
    try:
        for name in ("openrouter", "openai", "anthropic"):
            monkeypatch.setitem(setup.PRESETS, name, {**setup.PRESETS[name], "base_url": base + ("/anthropic" if name == "anthropic" else "/v1")})
        assert await setup.vendor_models("openrouter") == ["a/b", "gpt-4o-realtime", "gpt-chat", "text-embedding-3-small", "whisper-1"]
        assert "Authorization" not in seen["/v1/models"]
        assert await setup.vendor_models("openai") == []  # no key, not asked
        assert await setup.vendor_models("openai", "sk-o") == ["a/b", "gpt-chat"]
        assert seen["/v1/models"]["Authorization"] == "Bearer sk-o"
        assert await setup.vendor_models("anthropic", "sk-a") == ["claude-x", "claude-y"]
        assert seen["/anthropic/v1/models"]["x-api-key"] == "sk-a"
        assert await setup.vendor_models("other", "sk") == []
    finally:
        await server.close()


def _loads(path):
    import shutil

    from sleeptalk.config import load_resolved_config

    shared = path.parent.parent / "sleeptalk.toml"
    if not shared.exists():
        shutil.copy(Path(__file__).parents[1] / "sleeptalk" / "sleeptalk.example.toml", shared)
    return load_resolved_config(path, check_env=False, check_binaries=False, verify_binding=False)


@pytest.mark.parametrize("model", ["gpt-realtime-2.1", "gpt-live-1", ""])
async def test_realtime_is_a_speech_service_that_rewrites_the_route_both_ways(profile, monkeypatch, tmp_path, model):
    """Switching to OpenAI Realtime writes a hybrid [route] and its key ref;
    switching to another service puts the cascade route back."""
    from sleeptalk import settings_page
    from sleeptalk.credential_store import CredentialStore

    state = describe_profile(profile)
    card = next(s for s in state["speech"]["services"] if s["id"] == "openai")
    assert not card.get("experimental") and not card["in_use"] and card["takes_key"]
    assert card["models"]["tts"]["models"][0]["voices"][:2] == ["marin", "cedar"]

    checked = []

    async def connectivity(config, **_):
        checked.append((config.api_key, config.voice, config.language))
        if config.api_key == "bad":
            raise RuntimeError("401 rejected")

    monkeypatch.setattr("sleeptalk.openai_realtime.check_openai_connectivity", connectivity)
    monkeypatch.setattr("sleeptalk.openai_live.check_openai_live_connectivity", connectivity)
    store = CredentialStore(tmp_path / "store", backend="file")
    with pytest.raises(ValueError, match="voice_unavailable"):
        await settings_page.switch_to_realtime(profile, "sk-key", "eve", model=model, store=store)
    with pytest.raises(ValueError, match="invalid_credential"):
        await settings_page.switch_to_realtime(profile, "bad", "cedar", model=model, store=store)
    assert _loads(profile).route.type == "cascade"

    result = await settings_page.switch_to_realtime(profile, "  sk-key  ", "cedar", model=model, store=store)
    assert result == {"provider": "openai", "voice_id": "cedar"}
    assert checked[-1] == ("sk-key", "cedar", "en-US")
    config = _loads(profile)
    assert config.route.model == (model or "gpt-live-1")
    assert (config.route.type, config.route.voice, config.route.language) == ("hybrid", "cedar", "en-US")
    parsed = tomllib.loads(profile.read_text())
    assert store.get(parsed["credentials"]["refs"]["OPENAI_API_KEY"]) == "sk-key" and "sk-key" not in profile.read_text()

    state = describe_profile(profile)
    assert [s["id"] for s in state["speech"]["services"] if s["in_use"]] == ["openai"]
    assert state["speech"]["route"] == "hybrid" and state["speech"]["tts_provider"] == "openai"
    assert state["voice"]["voice_id"] == "cedar" and state["voice"]["name"] == "Cedar"

    async def validate(provider, value, spec, voice, **models):
        return {"stt_model": "ink-2", "voice_id": voice or "default-voice"}

    monkeypatch.setattr("sleeptalk.private_input.validate_provider", validate)
    await settings_page.switch_speech_service(profile, "cartesia", "c-key", "62ae83ad-4f6a-430b-af41-a9bede9286ca", store=store)
    config = _loads(profile)
    assert config.route.type == "cascade" and config.stt.provider == config.tts.provider == "cartesia"
    assert tomllib.loads(profile.read_text())["route"] == {"type": "cascade"}


def test_realtime_voice_and_language_live_in_the_route(profile):
    from sleeptalk.profile_edit import set_table

    set_table(profile, "route", {"type": "hybrid", "voice": "marin", "language": "en-US"})
    apply_setting(profile, "tts.voice_id", "sage")
    apply_setting(profile, "tts.model", "gpt-realtime-2.1")
    with pytest.raises(ValueError, match="Realtime"):
        apply_setting(profile, "tts.voice_id", "eve")
    apply_setting(profile, "language", "de-DE")
    parsed = tomllib.loads(profile.read_text())
    assert parsed["route"] == {"type": "hybrid", "voice": "sage", "language": "de-DE", "model": "gpt-realtime-2.1"}
    assert parsed["tts"]["voice_id"] == "eve" and describe_profile(profile)["language"]["value"] == "de-DE"


@pytest.mark.parametrize("model", [None, "gpt-realtime-2.1", "gpt-live-1", "custom-realtime-model"])
def test_live_default_preserves_existing_openai_model(profile, model):
    from sleeptalk.profile_edit import set_table

    route = {"type": "hybrid", **({"model": model} if model else {})}
    set_table(profile, "route", route)
    card = next(s for s in describe_profile(profile)["speech"]["services"] if s["id"] == "openai")
    assert card["current"] == dict.fromkeys(("stt", "tts"), model or "gpt-realtime-2.1")
    assert card["models"]["tts"]["models"][0]["id"] == "gpt-live-1"
    assert tomllib.loads(profile.read_text())["route"] == route


def test_set_table_replaces_one_table_and_keeps_the_rest(tmp_path):
    from sleeptalk.profile_edit import set_table

    path = tmp_path / "p.toml"
    path.write_text('[agent]\ntype = "pi"\n\n["route"]\n"type" = "hybrid"\nvoice = "marin"\n\n[tts]\nspeed = 1.25\n')
    assert set_table(path, "route", {"type": "cascade"})
    assert path.read_text() == '[agent]\ntype = "pi"\n\n[route]\ntype = "cascade"\n\n[tts]\nspeed = 1.25\n'
    assert not set_table(path, "route", {"type": "cascade"})
    set_table(path, "log", {"latency": True})
    assert tomllib.loads(path.read_text())["log"] == {"latency": True}


def test_refusal_words_match_the_page():
    """A page built before a code existed shows the server's message, so both copies must agree."""
    import re

    from sleeptalk.settings_page import _SPEECH_REFUSALS

    js = (Path(__file__).parents[1] / "sleeptalk" / "_settings" / "settings.js").read_text()
    block = js[js.index("const KEY_FAILURES = {"):]
    block = block[:block.index("};")]
    assert dict(re.findall(r'\n  ([a-z_]+): "(.*?)",', block)) == _SPEECH_REFUSALS


def test_realtime_failures_name_the_step_or_the_account_problem():
    from sleeptalk.openai_realtime import OpenAIRealtimeProtocolError, OpenAIRealtimeTransportError, OpenAISpeechError
    from sleeptalk.settings_page import _realtime_failure_code

    class Handshake(Exception):
        status = 401

    def caused(outer, inner):
        try:
            raise outer from inner
        except Exception as exc:  # noqa: BLE001
            return exc

    assert _realtime_failure_code(caused(OpenAIRealtimeTransportError("unable to connect to OpenAI Realtime"), Handshake())) == "invalid_credential"
    assert _realtime_failure_code(OpenAIRealtimeTransportError("unable to connect to OpenAI Realtime")) == "realtime_unreachable"
    assert _realtime_failure_code(OpenAIRealtimeProtocolError("OpenAI Realtime error: You exceeded your current quota (insufficient_quota)")) == "quota_or_rate_limit"
    assert _realtime_failure_code(caused(OpenAIRealtimeTransportError("OpenAI Realtime did not transcribe the connectivity probe"), TimeoutError())) == "realtime_no_transcript"
    assert _realtime_failure_code(OpenAIRealtimeProtocolError("Realtime confirmation mismatched voice")) == "realtime_settings_rejected"
    assert _realtime_failure_code(OpenAISpeechError("OpenAI Speech failed (403): nope")) == "permission_denied"
    assert _realtime_failure_code(OpenAISpeechError("OpenAI Speech request failed")) == "speech_failed"
    assert _realtime_failure_code(caused(OpenAIRealtimeTransportError("OpenAI Realtime did not confirm session configuration"), TimeoutError())) == "network_timeout"
    assert _realtime_failure_code(RuntimeError("something new")) == "validation_failed"


async def test_a_refused_switch_carries_words_for_older_pages(profile, monkeypatch):
    async def connectivity(config, **_):
        raise RuntimeError("OpenAI Realtime did not transcribe the connectivity probe")

    monkeypatch.setattr("sleeptalk.openai_realtime.check_openai_connectivity", connectivity)
    page = SettingsPage(profile, profiles_dir=profile.parent, environ={})
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url("/"))[:-1]
    origin = {"Origin": page.origin}
    try:
        session = await client.post("/session", json={"token": page.token}, headers=origin)
        headers = {**origin, "Cookie": f"{page.cookie_name}={session.cookies[page.cookie_name].value}",
                   "X-Settings-CSRF": (await session.json())["csrf"]}
        response = await client.post("/api/speech", json={"provider": "openai", "value": "sk-x", "voice_id": "marin", "tts_model": "gpt-realtime-2.1"},
                                     headers=headers)
        body = await response.json()
        assert response.status == 422 and body["code"] == "realtime_no_transcript"
        assert "didn't transcribe the test clip" in body["message"]
    finally:
        await client.close()


async def test_a_stopped_bot_says_why_and_where_its_log_is(profile, monkeypatch, tmp_path):
    from sleeptalk import service, settings_page

    (profile.parent / "bot.toml").write_text(PROFILE.replace(
        'type = "web"', 'type = "discord"\nguild_id = "10"\nallowed_channel_ids = "20"\noperator_ids = "1"'))
    definition = tmp_path / "ai.sleeptalk.bot.plist"
    definition.write_text("x")
    monkeypatch.setattr(settings_page, "_missing_discord_extra", lambda: "")
    monkeypatch.setattr(service, "unit_path_for", lambda slug: definition)
    monkeypatch.setattr(service, "preflight", lambda slug, path: {"status": "ready", "problems": [], "workspace": str(path.parent)})
    monkeypatch.setattr(service, "install", lambda slug, path, **k: 0)
    monkeypatch.setattr(service, "start", lambda slug, *where: "")
    monkeypatch.setattr(service, "running", lambda slug: False)
    monkeypatch.setattr(service, "failure", lambda slug: "sleeptalk: Discord rejected this bot's token.")
    monkeypatch.setattr(service, "log_hint", lambda slug, path: "/logs/bot.log")
    real_sleep = asyncio.sleep
    monkeypatch.setattr(settings_page.asyncio, "sleep", lambda _s: real_sleep(0))
    page = SettingsPage(profile, profiles_dir=profile.parent, environ={})
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url("/"))[:-1]
    try:
        csrf = (await (await client.post("/session", json={"token": page.token}, headers={"Origin": page.origin})).json())["csrf"]
        headers = {"X-Settings-CSRF": csrf, "Origin": page.origin}
        status = await (await client.post("/api/discord/status", json={"profile": "bot"}, headers=headers)).json()
        assert status == {"running": False, "installed": True, "reason": "sleeptalk: Discord rejected this bot's token."}
        started = await (await client.post("/api/discord/start", json={"profile": "bot"}, headers=headers)).json()
        assert started["status"] == "stopped" and started["reason"].endswith("token.") and started["log"] == "/logs/bot.log"
        # Which manager keeps it running decides the words beside Start.
        state = await (await client.get("/api/state", headers=headers)).json()
        assert state["discord"]["host_os"] in {"mac", "linux", "other"}
    finally:
        await client.close()


async def test_the_page_starts_and_stops_only_a_discord_profile(profile, monkeypatch):
    from sleeptalk import service, settings_page

    (profile.parent / "bot.toml").write_text(PROFILE.replace(
        'type = "web"', 'type = "discord"\nguild_id = "10"\nallowed_channel_ids = "20"\noperator_ids = "1"'))
    (profile.parent / "unlocked.toml").write_text(PROFILE.replace('type = "web"', 'type = "discord"\nguild_id = "10"'))
    calls = []
    monkeypatch.setattr(settings_page, "_missing_discord_extra", lambda: "")
    monkeypatch.setattr(service, "preflight", lambda slug, path: {"status": "ready", "problems": [], "workspace": str(path.parent)})
    monkeypatch.setattr(service, "install", lambda slug, path, **k: calls.append(("install", slug)) or 0)
    monkeypatch.setattr(service, "start", lambda slug, *where: calls.append(("start", slug)) or "")
    monkeypatch.setattr(service, "stop", lambda slug: calls.append(("stop", slug)))
    monkeypatch.setattr(service, "running", lambda slug: ("start", slug) in calls and ("stop", slug) not in calls)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(settings_page.asyncio, "sleep", lambda _s: real_sleep(0))
    page = SettingsPage(profile, profiles_dir=profile.parent, environ={})
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url("/"))[:-1]
    try:
        csrf = (await (await client.post("/session", json={"token": page.token}, headers={"Origin": page.origin})).json())["csrf"]
        headers = {"X-Settings-CSRF": csrf, "Origin": page.origin}
        assert (await (await client.post("/api/discord/status", json={"profile": "bot"}, headers=headers)).json())["running"] is False
        started = await client.post("/api/discord/start", json={"profile": "bot"}, headers=headers)
        assert started.status == 200 and (await started.json())["status"] == "running"
        assert calls == [("install", "bot"), ("start", "bot")]
        assert (await client.post("/api/discord/stop", json={"profile": "bot"}, headers=headers)).status == 200
        assert calls[-1] == ("stop", "bot")
        # A web profile, or one that doesn't exist, is never installed or started.
        for name in ("default", "missing"):
            assert (await client.post("/api/discord/start", json={"profile": name}, headers=headers)).status == 400
        # A bot not yet locked to a channel and an owner would only exit: it isn't started.
        unfinished = await client.post("/api/discord/start", json={"profile": "unlocked"}, headers=headers)
        assert unfinished.status == 422 and (await unfinished.json())["code"] == "discord_unfinished"
        monkeypatch.setattr(settings_page, "_missing_discord_extra", lambda: "Install sleeptalk[discord]")
        refused = await client.post("/api/discord/start", json={"profile": "bot"}, headers=headers)
        assert refused.status == 422 and (await refused.json())["code"] == "discord_extra_missing"
        assert len(calls) == 3
    finally:
        await client.close()


async def test_a_new_profile_keeps_the_models_and_voice_it_was_given(tmp_path, web_setup, monkeypatch):
    from sleeptalk.settings_page import create_profile

    checked = {}

    async def prove(provider, value, voice, **kwargs):
        checked.update(kwargs, voice=voice)
        return {"voice_id": voice}, "settings-test"

    monkeypatch.setattr("sleeptalk.settings_page.prove_speech_key", prove)
    profiles = tmp_path / "profiles"
    await create_profile(profiles, tmp_path / ".env", {
        "name": "Chinese", "workspace": str(tmp_path), "agent": "pi", "language": "zh-CN", "provider": "openrouter",
        "value": "sk-or-" + "k" * 20, "stt_model": "qwen/qwen3-asr-flash-2026-02-10",
        "tts_model": "qwen/qwen-audio-3.0-tts-flash", "voice": "longanhuan_v3.6", "surface": "web"})
    parsed = tomllib.loads((profiles / "chinese.toml").read_text())
    assert parsed["stt"]["model"] == "qwen/qwen3-asr-flash-2026-02-10"
    assert parsed["tts"]["model"] == "qwen/qwen-audio-3.0-tts-flash"
    assert parsed["tts"]["voice_id"] == "longanhuan_v3.6"
    # The key is checked on the models that will be used, not the defaults.
    assert checked["stt_model"] == "qwen/qwen3-asr-flash-2026-02-10"
    assert checked["tts_model"] == "qwen/qwen-audio-3.0-tts-flash"


async def test_a_new_profile_without_picked_models_gets_the_languages_recommendation(tmp_path, web_setup):
    from sleeptalk.settings_page import create_profile

    profiles = tmp_path / "profiles"
    await create_profile(profiles, tmp_path / ".env", {
        "name": "Chinese", "workspace": str(tmp_path), "agent": "pi", "language": "zh-CN", "provider": "openrouter",
        "value": "sk-or-" + "k" * 20, "surface": "web"})
    parsed = tomllib.loads((profiles / "chinese.toml").read_text())
    # Parakeet (the English default) has no Chinese.
    assert parsed["stt"]["model"] == "microsoft/mai-transcribe-2"
    assert parsed["tts"]["model"] == "qwen/qwen-audio-3.0-tts-flash"


async def test_a_new_profile_moves_a_voice_its_speaking_model_cannot_speak(tmp_path, web_setup):
    from sleeptalk.settings_page import create_profile

    profiles = tmp_path / "profiles"
    await create_profile(profiles, tmp_path / ".env", {
        "name": "Kokoro", "workspace": str(tmp_path), "agent": "pi", "language": "en-US", "provider": "openrouter",
        "value": "sk-or-" + "k" * 20, "tts_model": "hexgrad/kokoro-82m", "voice": "eve", "surface": "web"})
    parsed = tomllib.loads((profiles / "kokoro.toml").read_text())
    # "eve" belongs to Grok Voice; Kokoro starts on its own default.
    assert parsed["tts"]["model"] == "hexgrad/kokoro-82m" and parsed["tts"]["voice_id"] == "af_sky"


async def test_a_new_profile_is_private_to_its_owner(tmp_path, web_setup):
    from sleeptalk.settings_page import create_profile

    profiles = tmp_path / "profiles"
    await create_profile(profiles, tmp_path / ".env", {
        "name": "English", "workspace": str(tmp_path), "agent": "pi", "language": "en-US", "provider": "cartesia",
        "value": "key-123456", "surface": "web"})
    assert (profiles / "english.toml").stat().st_mode & 0o777 == 0o600


async def test_a_new_profile_name_needs_latin_letters_or_digits(tmp_path, web_setup):
    from sleeptalk.settings_page import create_profile, new_profile_options

    good = {"workspace": str(tmp_path), "agent": "pi", "language": "en-US", "provider": "cartesia",
            "value": "key-123456", "surface": "web"}
    with pytest.raises(ValueError, match="bad_profile_name"):
        await create_profile(tmp_path / "profiles", tmp_path / ".env", {**good, "name": "日本語"})
    with pytest.raises(ValueError, match="unsupported"):
        await create_profile(tmp_path / "profiles", tmp_path / ".env", {**good, "name": "x", "tts_model": "nope"})
    # The template's name is listed as reserved, so the page warns before the end.
    options = new_profile_options(tmp_path / "profiles", "en-US", environ={})
    # Reserved, not "already exists": the name step says which.
    assert options["reserved"] == ["example"] and "example" not in options["existing"]


async def test_a_new_deepgram_profile_names_its_voice_as_the_model(tmp_path, web_setup):
    from sleeptalk.settings_page import create_profile

    profiles = tmp_path / "profiles"
    await create_profile(profiles, tmp_path / ".env", {
        "name": "Spanish", "workspace": str(tmp_path), "agent": "pi", "language": "es-ES", "provider": "deepgram",
        "value": "dg-" + "k" * 20, "tts_model": "aura-2", "voice": "aura-2-alvaro-es", "surface": "web"})
    tts = tomllib.loads((profiles / "spanish.toml").read_text())["tts"]
    assert tts["model"] == tts["voice_id"] == "aura-2-alvaro-es"


def test_the_page_shows_the_speech_a_call_will_use(profile):
    """A launch-default model the language swaps at call time (Parakeet and
    Grok for Chinese on OpenRouter) shows as what the call uses, and the Voice
    page lists that model's voices, not the service's usual ones."""
    profile.write_text(profile.read_text().replace('language = "en-US"', 'language = "zh-CN"').replace(
        '[stt]\nprovider = "openrouter"\n', '[stt]\nprovider = "openrouter"\nmodel = "nvidia/parakeet-tdt-0.6b-v3"\n').replace(
        'voice_id = "eve"', 'model = "x-ai/grok-voice-tts-1.0"\nvoice_id = "eve"'))
    state = describe_profile(profile)
    assert state["speech"]["stt_model"] == "microsoft/mai-transcribe-2"
    assert state["speech"]["tts_model"] == "qwen/qwen-audio-3.0-tts-flash"
    assert state["voice"]["voice_id"] == "longanhuan_v3.6"
    assert "longanhuan_v3.6" in [v["voice_id"] for v in state["voice"]["voices"]]
    assert "eve" not in [v["voice_id"] for v in state["voice"]["voices"]]


@pytest.fixture
def local_speech(monkeypatch, tmp_path):
    """Local speech with the runtime present, nothing downloaded, and a test turn that passes."""
    from sleeptalk import sysdeps
    from sleeptalk.speech import local_models

    calls = {"downloaded": [], "heard": [], "spoke": [], "runtime": True, "fail_turn": False}
    ready: set[str] = set()
    monkeypatch.setattr(sysdeps, "missing_local_extra", lambda: None if calls["runtime"] else "missing")
    monkeypatch.setattr(sysdeps, "missing_streaming_extra", lambda: None)
    monkeypatch.setattr(sysdeps, "local_extra_install_command", lambda: ["uv", "pip", "install", "sherpa-onnx"])
    monkeypatch.setattr(local_models, "is_ready", lambda model, root=None: model.id in ready)

    def ensure(model, *, progress=None, **_):
        progress(model.size_mb * 500_000, model.size_mb * 1_000_000)
        calls["downloaded"].append(model.id)
        ready.add(model.id)

    monkeypatch.setattr(local_models, "ensure_model", ensure)

    async def heard(provider, spec, key, **_):
        if calls["fail_turn"]:
            raise RuntimeError("onnx exploded")
        calls["heard"].append((provider, spec.model))
        return "hello", 12.0

    monkeypatch.setattr("sleeptalk.stt_roundtrip.stt_roundtrip", heard)

    class Speaker:
        def __init__(self, spec):
            self.spec = spec

        async def synthesize_wav(self, text):
            calls["spoke"].append((self.spec.model, self.spec.voice_id))
            path = tmp_path / "line.wav"
            path.write_bytes(b"RIFF")
            return path

    monkeypatch.setattr("sleeptalk.registries.TTS_PROVIDERS.build", lambda name, spec, env: Speaker(spec))
    return calls


async def test_page_switches_to_local_speech_after_downloading_and_a_test_turn(profile, local_speech):
    from sleeptalk import settings_page

    job = settings_page.LocalSpeechJob("switch")
    result = await settings_page.switch_to_local(profile, "", "kokoro-en", "af_bella", job=job)
    assert result == {"provider": "local", "voice_id": "af_bella"}
    assert local_speech["downloaded"] == ["moonshine-small-streaming-en", "kokoro-en"]
    assert local_speech["heard"] == [("local", "moonshine-small-streaming-en")] and local_speech["spoke"] == [("kokoro-en", "af_bella")]
    parsed = tomllib.loads(profile.read_text())
    assert parsed["stt"]["provider"] == parsed["tts"]["provider"] == "local"
    assert (parsed["stt"]["model"], parsed["tts"]["model"], parsed["tts"]["voice_id"]) == ("moonshine-small-streaming-en", "kokoro-en", "af_bella")
    assert parsed["credentials"]["refs"]["OPENROUTER_API_KEY"]  # switching back finds the key where it was


@pytest.mark.parametrize("problem, code", [("runtime", "local_runtime_missing"), ("language", "local_english_only"),
                                           ("turn", "local_check_failed"), ("voice", "voice_unavailable")])
async def test_local_speech_is_not_saved_until_it_works_here(profile, local_speech, problem, code):
    from sleeptalk import settings_page

    before = profile.read_text()
    if problem == "runtime":
        local_speech["runtime"] = False
    if problem == "language":
        profile.write_text(before.replace('"en-US"', '"es-ES"'))
        before = profile.read_text()
    local_speech["fail_turn"] = problem == "turn"
    with pytest.raises(ValueError, match=code):
        await settings_page.switch_to_local(profile, "", "", "nobody" if problem == "voice" else "lessac")
    assert profile.read_text() == before


async def test_page_runs_the_local_switch_as_a_job_it_polls(profile, local_speech, tmp_path):
    page = SettingsPage(profile, profiles_dir=profile.parent, environ={}, state_dir=tmp_path / "state")
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url("/"))[:-1]
    try:
        csrf = (await (await client.post("/session", json={"token": page.token}, headers={"Origin": page.origin})).json())["csrf"]
        headers = {"X-Settings-CSRF": csrf, "Origin": page.origin}
        local_speech["runtime"] = False
        status = await (await client.post("/api/speech/local/status", json={"voice_id": "lessac"}, headers=headers)).json()
        assert status["runtime"] is False and "sherpa-onnx" in status["install_command"]
        assert [(m["id"], m["ready"]) for m in status["models"]] == [("moonshine-small-streaming-en", False), ("piper-lessac-medium", False)]
        assert status["models"][1]["download"] == {"bytes": 21_000_000, "approximate": True, "ready": False}
        assert not local_speech["downloaded"]  # inspecting sizes never starts a download
        refused = await (await client.post("/api/speech/local", json={"voice_id": "lessac"}, headers=headers)).json()
        assert refused["status"] == "started"
        for _ in range(50):
            job = (await (await client.post("/api/speech/local/status", json={}, headers=headers)).json())["job"]
            if job["stage"] in ("done", "failed"):
                break
            await asyncio.sleep(0.01)
        assert job["code"] == "local_runtime_missing" and job["message"]

        local_speech["runtime"] = True
        started = await client.post("/api/speech/local", json={"voice_id": "lessac"}, headers=headers)
        assert started.status == 200
        for _ in range(50):
            job = (await (await client.post("/api/speech/local/status", json={}, headers=headers)).json())["job"]
            if job["stage"] in ("done", "failed"):
                break
            await asyncio.sleep(0.01)
        assert job["stage"] == "done", job
        assert tomllib.loads(profile.read_text())["tts"]["provider"] == "local"
        bad = await client.post("/api/speech/local", json={"voice_id": "lessac", "extra": 1}, headers=headers)
        assert bad.status == 400
    finally:
        await client.close()


@pytest.mark.parametrize("body, expected", [
    ({}, "streaming"),
    ({"stt_model": "moonshine-small-streaming-en"}, "streaming"),
    ({"stt_model": "moonshine-tiny-en"}, "batch"),
])
async def test_local_install_uses_streaming_by_default_and_honors_explicit_tiny(monkeypatch, body, expected):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from sleeptalk import settings_page, sysdeps

    installed = []
    monkeypatch.setattr(sysdeps, "install_streaming_extra", lambda: installed.append("streaming"))
    monkeypatch.setattr(sysdeps, "install_local_extra", lambda: installed.append("batch"))
    page = object.__new__(settings_page.SettingsPage)
    page._authenticated = lambda request: None
    page.local_job = None
    response = await page.local_install(SimpleNamespace(json=AsyncMock(return_value=body)))
    await page.local_job.task
    assert response.status == 200
    assert page.local_job.stage == "done"
    assert installed == [expected]


async def test_local_pocket_choice_is_proven_and_saved_with_its_voice(profile, local_speech):
    from sleeptalk.settings_page import switch_to_local, describe_profile
    from sleeptalk.builder import tts_spec
    from sleeptalk.config import TTSConfig

    result = await switch_to_local(profile, '', 'pocket-tts-en', 'pocket-loona')
    assert result == {'provider': 'local', 'voice_id': 'pocket-loona'}
    assert local_speech['downloaded'] == ['moonshine-small-streaming-en', 'pocket-tts-en']
    assert local_speech['spoke'] == [('pocket-tts-en', 'pocket-loona')]
    document = tomllib.loads(profile.read_text())
    spec = tts_spec(TTSConfig.model_validate(document['tts']))
    assert spec.model == 'pocket-tts-en' and spec.voice_id == 'pocket-loona'
    assert describe_profile(profile)['voice']['voice_id'] == 'pocket-loona'


def test_a_discord_profile_refuses_a_browser_delivery(tmp_path):
    from sleeptalk.settings_page import apply_setting

    path = tmp_path / "bot.toml"
    path.write_text('[agent]\ntype = "omp"\nslug = "bot"\n\n[transport]\ntype = "discord"\n')
    before = path.read_text()
    with pytest.raises(ValueError, match="Discord"):
        apply_setting(path, "transport.options.delivery", "local")
    assert path.read_text() == before  # nothing written, so the profile still loads
