"""Secrets never reach a log line, whoever wrote the message."""
from __future__ import annotations

import json
import logging
import stat

import pytest

from sleeptalk.logging_setup import RedactingFormatter, redact, setup_file_logging

SECRETS = [
    "ws://127.0.0.1:9119/api/ws?token=abcDEF123456xyz",
    "Authorization: Bearer sk-or-v1-0123456789abcdef0123456789",
    "{'xi-api-key': '0123456789abcdef0123456789abcdef'}",
    "Token 0123456789abcdef0123456789abcdef",
    # Assemble this synthetic Discord token to avoid a secret-scanner false positive.
    ".".join(("MTIzNDU2Nzg5MDEyMzQ1Njc4OQ", "GaBcDe", "abcdefghijklmnopqrstuvwxyz0123456789")),
    "gsk_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "HERMES_GATEWAY_SESSION_TOKEN=abcDEF123456xyz",
    "OPENCLAW_GATEWAY_TOKEN='abcDEF123456xyz'",
    '{"token": "abcDEF123456xyz"}',
    "https://user:abcDEF123456xyz@host.example/path",
    "https://call.example/#token=abcDEF123456xyz",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnopqrstuvwxyz0123456789",
    "tgp_v1_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
]


@pytest.mark.parametrize("line", SECRETS)
def test_secret_shapes_are_masked(line):
    masked = redact(line)
    assert "***" in masked
    for fragment in ("abcDEF123456xyz", "0123456789abcdef0123", "abcdefghijklmnopqrstuvwxyz0123456789",
                     "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        assert fragment not in masked


@pytest.mark.parametrize("line", [
    "the token was refused and authentication failed",
    "voice_id=db6b0ed5-d5d3-463d-ae85-518a07d3c2b4",
    "Listening with Parakeet; latency 260 ms",
    "credential:0123456789abcdef0123456789abcdef",
])
def test_ordinary_lines_are_left_alone(line):
    assert redact(line) == line


def test_file_logs_mask_tracebacks_and_stay_private(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        path = setup_file_logging(logs_dir=tmp_path / "logs", slug="t")
        try:
            raise RuntimeError("handshake failed url='wss://h/ws?token=SENTINEL123456'")
        except RuntimeError:
            logging.getLogger("sleeptalk.test").exception("gateway down with Bearer sk-SENTINEL0123456789abcd")
        for handler in root.handlers:
            handler.flush()
        text = path.read_text()
        assert "SENTINEL" not in text and "token=***" in text and "Bearer ***" in text
        assert stat.S_IMODE((tmp_path / "logs").stat().st_mode) == 0o700
        assert all(isinstance(h.formatter, RedactingFormatter) for h in root.handlers)
    finally:
        for handler in root.handlers[:]:
            if handler not in before:
                root.removeHandler(handler)
                handler.close()


@pytest.mark.parametrize("url", ["http://api.example.com/v1", "ws://10.0.0.5/ws", "https://user:pw@api.example.com",
                                 "https://api.example.com/v1?key=x", "ftp://api.example.com"])
def test_a_speech_key_is_never_sent_to_an_unsafe_base_url(url):
    from sleeptalk.config import STTConfig, TTSConfig
    from sleeptalk.providers import ProviderSpec, provider_http_url

    with pytest.raises(ValueError):
        provider_http_url(ProviderSpec(provider="cartesia", options={"base_url": url}), "https://api.cartesia.ai", "/tts")
    with pytest.raises(ValueError):
        STTConfig(base_url=url)
    with pytest.raises(ValueError):
        TTSConfig(base_url=url)


@pytest.mark.parametrize("url", ["https://proxy.example.com/v1", "http://127.0.0.1:8080", "http://localhost:9000/v1", "wss://speech.example.com"])
def test_https_and_loopback_base_urls_still_work(url):
    from sleeptalk.config import STTConfig
    from sleeptalk.providers import ProviderSpec, provider_http_url

    assert provider_http_url(ProviderSpec(provider="x", options={"base_url": url}), "https://d", "/a").startswith(url.rstrip("/"))
    assert STTConfig(base_url=url).base_url == url


@pytest.mark.parametrize("agent_type", ["pi", "codex", "cursor", "claude_code", "command"])
def test_a_model_credential_never_reaches_an_agent_that_does_not_use_it(agent_type):
    from types import SimpleNamespace

    from sleeptalk.agent_auth import AgentAuthError, resolve_agent_auth_env

    agent = SimpleNamespace(type=agent_type, auth=SimpleNamespace(credential_ref="credential:" + "a" * 32))
    with pytest.raises(AgentAuthError, match="doesn't use"):
        resolve_agent_auth_env(agent, {"PATH": "/bin"}, store=object())


@pytest.mark.parametrize("value", ["plain-token", "has # hash", "dollar $HOME and ${X}", "it's", "sp ace"])
def test_env_values_round_trip_exactly(tmp_path, value):
    from dotenv import dotenv_values

    from sleeptalk.wizard import _merge_env_text

    path = tmp_path / ".env"
    path.write_text(_merge_env_text("OTHER=1\n", {"TOKEN": value}))
    assert dotenv_values(path, interpolate=False) == {"OTHER": "1", "TOKEN": value}


@pytest.mark.parametrize("value", ["a\nINJECTED=1", "a\rb", "tab\there", "both ' and \""])
def test_env_values_that_could_inject_a_line_are_refused(value):
    from sleeptalk.wizard import _merge_env_text

    with pytest.raises(ValueError):
        _merge_env_text("", {"TOKEN": value})


def test_a_token_in_a_url_flag_is_warned_about(capsys):
    from sleeptalk.cli import _warn_url_token

    _warn_url_token("--url", "http://127.0.0.1:9119/?token=abc123")
    assert "--token -" in capsys.readouterr().err
    _warn_url_token("--url", "http://127.0.0.1:9119/")
    assert capsys.readouterr().err == ""


async def test_the_local_hermes_token_never_goes_to_a_remote_dashboard():
    from types import SimpleNamespace

    from sleeptalk.hermes_connect import SESSION_TOKEN_ENV, attach_hermes

    seen = []

    async def handshake(url, *, timeout):
        seen.append(url)
        return "auth_required"

    answers = SimpleNamespace()
    await attach_hermes(answers, raw_url="wss://attacker.example", env={SESSION_TOKEN_ENV: "local-secret"}, handshake=handshake)
    assert seen and not any("local-secret" in url for url in seen)
    seen.clear()
    await attach_hermes(answers, raw_url="http://127.0.0.1:9119", env={SESSION_TOKEN_ENV: "local-secret"}, handshake=handshake)
    assert any("local-secret" in url for url in seen)


async def test_the_own_agent_check_never_sees_this_process_secrets(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from sleeptalk import settings_page

    monkeypatch.setenv("ANTHROPIC_API_KEY", "process-secret")
    captured = {}

    class Report:
        failed = False
        checks = ()

    async def run_adapter_test(factory, **kwargs):
        from sleeptalk.agents import bridge_lite
        captured["env"] = factory.__closure__ and [c.cell_contents for c in factory.__closure__ if isinstance(c.cell_contents, dict)]
        return Report()

    monkeypatch.setattr("sleeptalk.conformance.runner.run_adapter_test", run_adapter_test)
    answers = SimpleNamespace(slug="own", workspace_dir=str(tmp_path))
    await settings_page._own_agent(answers, {"kind": "http", "url": "https://agent.example/turn", "token_env": "ANTHROPIC_API_KEY"}, home=tmp_path)
    envs = captured["env"]
    assert envs and all("process-secret" not in json.dumps(env) for env in envs)


@pytest.mark.parametrize("field", ["name", "tts_voice_id", "stt_model", "language", "operator_ids"])
def test_a_value_cannot_add_a_setting_to_the_profile(field):
    import tomllib

    from sleeptalk.wizard import WizardAnswers, profile_toml

    answers = WizardAnswers(name="Voice", slug="voice", agent_type="pi", surface="discord", stt_provider="cartesia",
                            tts_provider="cartesia", tts_voice_id="v", guild_id="1", client_id="2", operator_ids="3",
                            allowed_channel_ids="4")
    evil = 'x"\npi_bin = "/tmp/evil'
    setattr(answers, field, evil)
    parsed = tomllib.loads(profile_toml(answers))
    assert "pi_bin" not in parsed["agent"] and "base_url" not in parsed.get("tts", {})
    flat = json.dumps(parsed)
    assert json.dumps(evil)[1:-1] in flat  # kept as the value it was, not as a setting


def test_a_workspace_path_cannot_add_a_binding_field(tmp_path):
    import tomllib

    from sleeptalk.runtime_binding import RuntimeBinding, toml

    fields = {name: "x" for name in RuntimeBinding.model_fields if name != "version"}
    first = next(iter(fields))
    fields[first] = 'w"\nruntime = "evil'
    parsed = tomllib.loads(toml(RuntimeBinding.model_construct(version=1, **fields)))
    assert parsed["binding"][first] == 'w"\nruntime = "evil'


def test_env_files_must_be_private(tmp_path, monkeypatch):
    import stat

    from sleeptalk.config import _load_env

    monkeypatch.delenv("SLEEPTALK_HOME", raising=False)
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    env = profiles / ".env"
    env.write_text("SOME_TOKEN=value\n")
    env.chmod(0o644)
    assert _load_env(profiles / "p.toml")["SOME_TOKEN"] == "value"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600  # tightened on read
    env.chmod(0o666)
    assert "SOME_TOKEN" not in _load_env(profiles / "p.toml") or _load_env(profiles / "p.toml").get("SOME_TOKEN") != "value"


def test_an_open_discord_bot_stays_in_its_own_server():
    from types import SimpleNamespace

    from sleeptalk.discord.access import DiscordAccess

    access = DiscordAccess(required=False, guild_id=111, channels=frozenset(), operators=frozenset(),
                           speakers=frozenset(), listeners=frozenset())
    assert access.channel(111, 5)
    assert not access.channel(222, 5) and not access.channel(None, 5)
    assert not access.audience(SimpleNamespace(id=5, guild=SimpleNamespace(id=222)), bot_id=1)
    unpinned = DiscordAccess(required=False, guild_id=0, channels=frozenset(), operators=frozenset(),
                             speakers=frozenset(), listeners=frozenset())
    assert unpinned.channel(222, 5)
