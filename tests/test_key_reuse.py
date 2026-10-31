"""A key the agent already has (OpenRouter and friends) becomes voice with
nothing to enter: discovered by source, validated, stored, profile written."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sleeptalk import host, receipts
from sleeptalk.credential_store import CredentialStore
from sleeptalk.doctor import FAIL, OK, DoctorReport, Rung
from sleeptalk.key_discovery import ReusableKey, discover, read_value
from sleeptalk.key_reuse import ReuseError, pick, profile_summary, reuse_key, spec_for_reuse

SENTINEL = "sk-or-reuse-sentinel-never-in-receipts"


def hermes_home(tmp_path: Path, monkeypatch, value: str = SENTINEL) -> Path:
    home = tmp_path / "hermes"
    home.mkdir()
    (home / ".env").write_text(f"OPENROUTER_API_KEY={value}\nTELEGRAM_TOKEN=x\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.mark.real_key_discovery
def test_discovery_reports_sources_never_values(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".hermes" / ".env").write_text(f"OPENROUTER_API_KEY={SENTINEL}\n")
    (home / ".openclaw").mkdir()
    (home / ".openclaw" / ".env").write_text("VENICE_API_KEY=v\nOPENROUTER_API_KEY=other\n")
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "chat.toml").write_text('[agent]\ntype = "model"\n\n[agent.auth]\nmode = "api_key"\nprotocol = "openai-completions"\n'
                                        'base_url = "https://api.groq.com/openai/v1"\ncredential_ref = "credential:' + "b" * 32 + '"\n')
    # A project's .env is never a source, and ${NAME} is never expanded.
    (tmp_path / ".env").write_text("VENICE_API_KEY=planted\nGROQ_API_KEY=planted\n")
    (home / ".openclaw" / ".env").write_text("VENICE_API_KEY=${TOGETHER_API_KEY}\nOPENROUTER_API_KEY=other\n")
    keys = discover(environment={"TOGETHER_API_KEY": "t"}, home=home, profiles_dir=profiles)
    assert [(k.provider, k.kind, k.source) for k in keys] == [
        ("groq", "credential", "the model key of profile 'chat'"),
        ("together", "environment", "this host's environment"),
        ("openrouter", "dotenv", "~/.hermes/.env"),
        ("venice", "dotenv", "~/.openclaw/.env"),
    ]
    assert SENTINEL not in json.dumps([k.public() for k in keys])
    assert read_value(keys[2]) == SENTINEL
    assert read_value(keys[1], environment={"TOGETHER_API_KEY": "t"}) == "t"
    assert read_value(keys[3]) == "${TOGETHER_API_KEY}"  # literal, never the environment's value
    assert pick("openrouter", keys).location.endswith(".env")
    with pytest.raises(ReuseError, match="No elevenlabs key"):
        pick("elevenlabs", keys)
    assert profile_summary("openrouter")["speaking_model"] == "x-ai/grok-voice-tts-1.0"


async def test_reuse_validates_stores_and_writes_a_reference_only_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / ".sleeptalk"))
    seen = []

    async def validate(provider, value, spec, voice):
        seen.append((provider, value, spec.stt_provider, spec.tts_provider))
        return {"stt_model": "openai/whisper-large-v3-turbo", "voice_id": voice or "flux-hannah-en"}

    async def ready(*args):
        return {"status": "ready"}

    monkeypatch.setattr("sleeptalk.key_reuse.validate_provider", validate)
    monkeypatch.setattr("sleeptalk.runtime_probe.probe_runtime", ready)
    store = CredentialStore(tmp_path / "store", backend="file")
    key = ReusableKey(provider="openrouter", env="OPENROUTER_API_KEY", kind="environment", location="OPENROUTER_API_KEY", source="this host's environment")
    profile_path = tmp_path / ".sleeptalk" / "profiles" / "default.toml"
    spec = spec_for_reuse(request_id="reuse-test-0001", profile="default", workspace=str(tmp_path), agent="codex",
                          surface="web", profile_path=str(profile_path))
    result = await reuse_key(spec, key, store=store, environment={"OPENROUTER_API_KEY": SENTINEL}, state_root=tmp_path / "state")
    assert seen == [("openrouter", SENTINEL, "openrouter", "openrouter")]
    assert result["reused_key"] == key.public() and result["speech"]["voice"] == "eve"
    assert SENTINEL not in json.dumps(result)
    import tomllib
    document = profile_path.read_text()
    assert SENTINEL not in document
    parsed = tomllib.loads(document)
    assert parsed["stt"]["provider"] == parsed["tts"]["provider"] == "openrouter"
    assert parsed["stt"]["model"] == "openai/whisper-large-v3-turbo" and parsed["tts"]["voice_id"] == "flux-hannah-en"
    reference = parsed["credentials"]["refs"]["OPENROUTER_API_KEY"]
    assert store.status(reference).provider == "openrouter" and store.get(reference) == SENTINEL

    async def reject(provider, value, spec, voice):
        raise RuntimeError("401 rejected " + value)

    monkeypatch.setattr("sleeptalk.key_reuse.validate_provider", reject)
    other = spec_for_reuse(request_id="reuse-test-0002", profile="second", workspace=str(tmp_path), agent="codex",
                           surface="web", profile_path=str(tmp_path / ".sleeptalk" / "profiles" / "second.toml"))
    with pytest.raises(ReuseError) as failure:
        await reuse_key(other, key, store=store, environment={"OPENROUTER_API_KEY": SENTINEL}, state_root=tmp_path / "state")
    assert failure.value.code == "invalid_credential" and SENTINEL not in str(failure.value)


@pytest.mark.real_key_discovery
def test_ensure_offers_a_found_key_and_touches_nothing_until_the_user_says_yes(tmp_path, monkeypatch):
    """Plain `host ensure` only names the key and asks: no read, no paid
    check, no copy. The user's yes is the agent's --reuse-key rerun."""
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    hermes_home(tmp_path, monkeypatch)

    async def must_not_validate(*args):
        raise AssertionError("a found key was checked without the user's yes")

    async def ready(*args):
        return {"status": "ready"}

    monkeypatch.setattr("sleeptalk.key_reuse.validate_provider", must_not_validate)
    monkeypatch.setattr("sleeptalk.key_reuse.read_value", lambda *a, **k: pytest.fail("a found key was read without the user's yes"))
    monkeypatch.setattr("sleeptalk.runtime_probe.probe_runtime", ready)
    receipt = host.ensure(profile="default", agent="codex", surface="web", workspace=str(tmp_path))
    assert receipt["status"] == receipts.STATUS_NEEDS_INPUT, receipt
    assert receipt["reusable_keys"][0]["provider"] == "openrouter"
    assert "OpenRouter key" in receipt["ask"] and "/hermes/.env" in receipt["ask"] and "paid" in receipt["ask"]
    assert "--reuse-key PROVIDER" in receipt["remediation"] and "--agent codex" in receipt["remediation"]
    assert "host input link" in receipt["remediation"] and SENTINEL not in json.dumps(receipt)
    assert not (tmp_path / "home" / "profiles" / "default.toml").exists()
    quiet = host.ensure(profile="default", agent="codex", surface="web", workspace=str(tmp_path), key_reuse=False)
    assert quiet["status"] == receipts.STATUS_NEEDS_INPUT and "reusable_keys" not in quiet and "ask" not in quiet


@pytest.mark.real_key_discovery
def test_a_chosen_key_that_fails_validation_falls_back_to_the_page(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    hermes_home(tmp_path, monkeypatch)

    async def reject(provider, value, spec, voice):
        raise RuntimeError("401 rejected " + value)

    async def ready(*args):
        return {"status": "ready"}

    monkeypatch.setattr("sleeptalk.key_reuse.validate_provider", reject)
    monkeypatch.setattr("sleeptalk.runtime_probe.probe_runtime", ready)
    receipt = host.ensure(profile="default", agent="codex", surface="web", workspace=str(tmp_path), reuse_key="openrouter")
    assert receipt["status"] == receipts.STATUS_NEEDS_INPUT
    assert "invalid_credential" in json.dumps(receipt) and SENTINEL not in json.dumps(receipt)
    assert not (tmp_path / "home" / "profiles" / "default.toml").exists()


@pytest.mark.real_key_discovery
def test_ensure_with_reuse_key_writes_the_profile_and_reports_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    hermes_home(tmp_path, monkeypatch)
    calls = []

    async def validate(provider, value, spec, voice):
        calls.append(provider)
        assert value == SENTINEL
        return {"stt_model": "openai/whisper-large-v3-turbo", "voice_id": "flux-hannah-en"}

    async def ready(*args):
        return {"status": "ready"}

    async def fake_doctor(path, *, env=None, live=True, surface=None):
        return DoctorReport(rungs=[Rung("env vars present", OK)])

    monkeypatch.setattr("sleeptalk.key_reuse.validate_provider", validate)
    monkeypatch.setattr("sleeptalk.runtime_probe.probe_runtime", ready)
    monkeypatch.setattr("sleeptalk.setup.ensure_dependencies", lambda *args, **kwargs: ())
    monkeypatch.setattr(host, "run_doctor", fake_doctor)
    receipt = host.ensure(profile="default", agent="codex", surface="web", workspace=str(tmp_path), reuse_key="openrouter")
    assert receipt["status"] == receipts.STATUS_READY, receipt
    assert receipt["reused_key"]["source"].endswith("/hermes/.env") and receipt["speech"]["provider"] == "openrouter"
    assert calls == ["openrouter"]
    profile = tmp_path / "home" / "profiles" / "default.toml"
    assert profile.exists() and "OPENROUTER_API_KEY" in profile.read_text() and SENTINEL not in profile.read_text()
    assert SENTINEL not in json.dumps(receipt)
    again = host.ensure(profile="default", agent="codex", surface="web", workspace=str(tmp_path), reuse_key="openrouter")
    assert again["status"] == receipts.STATUS_READY and again["reused_key"]["source"] == "already set up"
    assert "copied into Sleeptalk's private store" in receipt["say"]
    assert again["say"].startswith("Voice is already set up with your OpenRouter key")
    other = host.ensure(profile="default", agent="codex", surface="web", workspace=str(tmp_path), reuse_key="groq")
    assert other["status"] == receipts.STATUS_NEEDS_INPUT and "uses openrouter" in other["remediation"]
    assert "reused_key" not in other


@pytest.mark.real_key_discovery
def test_reuse_refuses_surfaces_it_cannot_set_up(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "no-hermes"))
    for name in ("OPENROUTER_API_KEY", "GROQ_API_KEY", "TOGETHER_API_KEY", "VENICE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    receipt = host.ensure(profile="discord", agent="codex", surface="discord", reuse_key="openrouter")
    assert receipt["status"] == receipts.STATUS_NEEDS_INPUT and "browser calls" in receipt["remediation"]
    receipt = host.ensure(profile="default", agent="codex", surface="web", reuse_key="openrouter")
    assert receipt["status"] == receipts.STATUS_NEEDS_INPUT and "No openrouter key" in receipt["remediation"]


@pytest.mark.real_key_discovery
def test_hermes_custom_providers_in_config_yaml_are_found(tmp_path):
    """The VPS Hermes runs Venice through `provider: custom` with the key inline
    in config.yaml (not .env); on 2026-09-21 discovery missed it."""
    from sleeptalk.key_discovery import read_yaml_mappings

    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".hermes" / "config.yaml").write_text(
        "model:\n  default: kimi-k3\n  provider: custom\n  base_url: https://api.venice.ai/api/v1\n"
        f"  api_key: {SENTINEL}\n  api_mode: chat_completions\nfallback_providers: []\n"
        "providers:\n  router:\n    api: https://openrouter.ai/api/v1\n    key_env: MY_ROUTER_KEY\n"
        "  fast:\n    api: https://api.groq.com/openai/v1\n    api_key: ${GROQ_SECRET}\n"
        "  other:\n    api: https://example.com/v1\n    api_key: nope\n"
        "toolsets:\n  - hermes-cli\n")
    (home / ".hermes" / ".env").write_text("MY_ROUTER_KEY=router-value\nGROQ_SECRET=groq-value\n")
    parsed = read_yaml_mappings((home / ".hermes" / "config.yaml").read_text())
    assert parsed["model"]["base_url"] == "https://api.venice.ai/api/v1" and parsed["providers"]["router"]["key_env"] == "MY_ROUTER_KEY"
    keys = discover(environment={}, home=home, profiles_dir=tmp_path / "none")
    by_provider = {k.provider: k for k in keys}
    assert set(by_provider) == {"venice", "openrouter", "groq"}
    venice = by_provider["venice"]
    assert venice.kind == "hermes_config" and venice.source == "~/.hermes/config.yaml (model)"
    assert read_value(venice) == SENTINEL and SENTINEL not in json.dumps(venice.public())
    assert by_provider["openrouter"].kind == "dotenv" and read_value(by_provider["openrouter"]) == "router-value"
    assert by_provider["groq"].kind == "dotenv" and read_value(by_provider["groq"]) == "groq-value"


@pytest.mark.real_key_discovery
def test_openclaw_providers_in_openclaw_json_are_found(tmp_path):
    """OpenClaw keeps `models.providers.<id>.{baseUrl, apiKey}` in JSON5 with
    comments and trailing commas; keys may be inline, `${VAR}`, or a secret ref."""
    home = tmp_path / "home"
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw" / "openclaw.json").write_text(
        "{\n  // written by openclaw\n  \"models\": {\n    \"providers\": {\n"
        f"      \"openrouter\": {{\"apiKey\": \"{SENTINEL}\", \"baseUrl\": \"https://openrouter.ai/api/v1\",}},\n"
        "      \"mine\": {\"apiKey\": \"${MY_VENICE}\", \"baseUrl\": \"https://api.venice.ai/api/v1\"},\n"
        "      \"fast\": {\"apiKey\": {\"source\": \"env\", \"provider\": \"default\", \"id\": \"GROQ_FROM_ENV\"}, \"baseUrl\": \"https://api.groq.com/openai/v1\"},\n"
        "      \"other\": {\"apiKey\": \"nope\", \"baseUrl\": \"https://example.com/v1\"},\n"
        "    },\n  },\n  \"agents\": {\"defaults\": {\"model\": \"openrouter/anthropic/claude-sonnet-4\"}},\n}\n")
    (home / ".openclaw" / ".env").write_text("MY_VENICE=venice-value\nDISCORD_BOT_TOKEN=x\n")
    keys = discover(environment={"GROQ_FROM_ENV": "groq-value"}, home=home, profiles_dir=tmp_path / "none")
    by_provider = {k.provider: k for k in keys}
    assert set(by_provider) == {"openrouter", "venice", "groq"}
    assert by_provider["openrouter"].kind == "openclaw_config"
    assert by_provider["openrouter"].source == "~/.openclaw/openclaw.json (models.providers.openrouter)"
    assert read_value(by_provider["openrouter"]) == SENTINEL and SENTINEL not in json.dumps([k.public() for k in keys])
    assert by_provider["venice"].kind == "dotenv" and read_value(by_provider["venice"]) == "venice-value"
    assert by_provider["groq"].kind == "environment" and read_value(by_provider["groq"], environment={"GROQ_FROM_ENV": "groq-value"}) == "groq-value"


@pytest.mark.real_key_discovery
def test_pi_and_oh_my_pi_logins_and_custom_providers_are_found(tmp_path):
    """pi keeps /login results in auth.json and custom providers in models.json;
    a `!command` key is never run and a `$VAR` key comes from the environment."""
    home = tmp_path / "home"
    (home / ".pi" / "agent").mkdir(parents=True)
    (home / ".pi" / "agent" / "auth.json").write_text(json.dumps({
        "openrouter": {"type": "api_key", "key": SENTINEL},
        "anthropic": {"type": "oauth", "token": "not-a-speech-provider"},
    }))
    (home / ".pi" / "agent" / "models.json").write_text(json.dumps({"providers": {
        "my-venice": {"baseUrl": "https://api.venice.ai/api/v1", "api": "openai-completions", "apiKey": "venice-literal", "models": []},
        "fast": {"baseUrl": "https://api.groq.com/openai/v1", "apiKey": "$GROQ_VAR"},
        "cmd": {"baseUrl": "https://api.together.xyz/v1", "apiKey": "!security find-generic-password -ws together"},
    }}))
    (home / ".omp" / "agent").mkdir(parents=True)
    (home / ".omp" / "agent" / "auth.json").write_text(json.dumps({"together": {"type": "api_key", "key": "omp-together"}}))
    keys = discover(environment={"GROQ_VAR": "groq-from-env"}, home=home, profiles_dir=tmp_path / "none")
    by_provider = {k.provider: k for k in keys}
    assert set(by_provider) == {"openrouter", "venice", "groq", "together"}
    assert by_provider["openrouter"].source == "~/.pi/agent/auth.json (openrouter)" and read_value(by_provider["openrouter"]) == SENTINEL
    assert by_provider["venice"].source == "~/.pi/agent/models.json (providers.my-venice)" and read_value(by_provider["venice"]) == "venice-literal"
    assert by_provider["groq"].kind == "environment" and read_value(by_provider["groq"], environment={"GROQ_VAR": "groq-from-env"}) == "groq-from-env"
    # The shell-command entry is skipped; oh-my-pi's login supplies Together instead.
    assert by_provider["together"].source == "~/.omp/agent/auth.json (together)" and read_value(by_provider["together"]) == "omp-together"
    assert SENTINEL not in json.dumps([k.public() for k in keys])


@pytest.mark.real_key_discovery
@pytest.mark.parametrize("agent", ["hermes", "pi"])
def test_reuse_keeps_tool_approvals_with_the_caller(tmp_path, monkeypatch, agent):
    """The VPS receipt on 2026-09-22: 'The key could not be set up for voice' with
    no reason. Hermes's and pi's probes refuse permission mode auto; reuse now
    asks for pending, as the link recipe does, and a failure names its cause."""
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    if agent == "pi":
        (tmp_path / ".pi" / "agent").mkdir(parents=True)
        (tmp_path / ".pi" / "agent" / "auth.json").write_text(json.dumps({"openrouter": {"type": "api_key", "key": SENTINEL}}))
    else:
        hermes_home(tmp_path, monkeypatch)
    probed = []

    async def validate(provider, value, spec, voice):
        return {"stt_model": "openai/whisper-large-v3-turbo", "voice_id": "flux-hannah-en"}

    async def probe(agent_config):
        probed.append(getattr(agent_config, "permission_mode", ""))
        return {"status": "ready"} if agent_config.permission_mode == "pending" else {"status": "needs_input"}

    async def fake_doctor(path, *, env=None, live=True, surface=None):
        return DoctorReport(rungs=[Rung("env vars present", OK)])

    monkeypatch.setattr("sleeptalk.key_reuse.validate_provider", validate)
    monkeypatch.setattr("sleeptalk.runtime_probe.probe_runtime", probe)
    monkeypatch.setattr("sleeptalk.setup.ensure_dependencies", lambda *args, **kwargs: ())
    monkeypatch.setattr(host, "run_doctor", fake_doctor)
    receipt = host.ensure(profile="default", agent=agent, surface="web", workspace=str(tmp_path), reuse_key="openrouter")
    assert receipt["status"] == receipts.STATUS_READY, receipt
    assert probed and all(mode == "pending" for mode in probed)
    assert 'permission_mode = "pending"' in (tmp_path / "home" / "profiles" / "default.toml").read_text()

    # A probe that still refuses names the reason instead of a blank failure.
    async def refuse(agent_config):
        return {"status": "needs_input"}

    monkeypatch.setattr("sleeptalk.runtime_probe.probe_runtime", refuse)
    receipt = host.ensure(profile="second", agent=agent, surface="web", workspace=str(tmp_path), reuse_key="openrouter")
    assert receipt["status"] == receipts.STATUS_NEEDS_INPUT
    assert "Agent login or permission settings changed" in receipt["remediation"]
    # The failed attempt left no orphaned credential behind: only the first profile's record remains.
    import sqlite3
    from sleeptalk.credential_store import default_root
    db = sqlite3.connect(default_root() / "store.sqlite3")
    assert db.execute("select count(*) from credentials where provider='openrouter'").fetchone()[0] == 1


@pytest.mark.real_key_discovery
@pytest.mark.parametrize("provider", ["openrouter", "venice", "groq", "together"])
def test_every_aggregator_key_becomes_a_working_profile(tmp_path, monkeypatch, provider):
    """The same reuse path for each service: found in the environment, validated,
    stored, and written as a profile the config layer accepts."""
    import tomllib
    from sleeptalk.speech.openai_audio import PRESETS
    from sleeptalk.config import load_resolved_config

    preset = PRESETS[provider]
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "no-hermes"))
    for name in ("OPENROUTER_API_KEY", "GROQ_API_KEY", "TOGETHER_API_KEY", "VENICE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(preset.env, SENTINEL)

    async def validate(name, value, spec, voice):
        assert name == provider and value == SENTINEL
        return {"stt_model": preset.stt_model, "voice_id": voice or preset.tts_voice}

    async def ready(*args):
        return {"status": "ready"}

    async def fake_doctor(path, *, env=None, live=True, surface=None):
        return DoctorReport(rungs=[Rung("env vars present", OK)])

    monkeypatch.setattr("sleeptalk.key_reuse.validate_provider", validate)
    monkeypatch.setattr("sleeptalk.runtime_probe.probe_runtime", ready)
    monkeypatch.setattr("sleeptalk.setup.ensure_dependencies", lambda *args, **kwargs: ())
    monkeypatch.setattr(host, "run_doctor", fake_doctor)
    receipt = host.ensure(profile="voice", agent="hermes", surface="web", workspace=str(tmp_path), reuse_key=provider)
    assert receipt["status"] == receipts.STATUS_READY, receipt
    assert receipt["reused_key"]["provider"] == provider and receipt["speech"]["speaking_model"] == preset.tts_model
    profile = tmp_path / "home" / "profiles" / "voice.toml"
    parsed = tomllib.loads(profile.read_text())
    assert parsed["stt"]["provider"] == parsed["tts"]["provider"] == provider
    assert parsed["stt"]["model"] == preset.stt_model and parsed["tts"]["voice_id"] == preset.tts_voice
    assert list(parsed["credentials"]["refs"]) == [preset.env]
    cfg = load_resolved_config(profile, check_env=False, check_binaries=False)
    assert cfg.tts.provider == provider and SENTINEL not in profile.read_text()


@pytest.mark.real_key_discovery
def test_a_provider_block_pointed_at_another_host_is_not_that_provider(tmp_path):
    """A block named "openrouter" that points at your own gateway holds the
    gateway's key; offering it as an OpenRouter key would send it to openrouter.ai."""
    home = tmp_path / "home"
    (home / ".pi" / "agent").mkdir(parents=True)
    (home / ".pi" / "agent" / "models.json").write_text(json.dumps({"providers": {
        "openrouter": {"baseUrl": "https://gateway.internal.example/v1", "apiKey": "gateway-key"},
        "groq": {"apiKey": "groq-by-name"},
    }}))
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw" / "openclaw.json").write_text(json.dumps({"models": {"providers": {
        "together": {"baseUrl": "https://proxy.example/v1", "apiKey": "proxy-key"},
    }}}))
    (home / ".hermes").mkdir(parents=True)
    (home / ".hermes" / "config.yaml").write_text(
        "model:\n  provider: venice\n  base_url: https://my-venice-proxy.example/v1\n  api_key: proxy-key\n")
    keys = discover(environment={}, home=home, profiles_dir=tmp_path / "none")
    assert {(k.provider, read_value(k)) for k in keys} == {("groq", "groq-by-name")}
