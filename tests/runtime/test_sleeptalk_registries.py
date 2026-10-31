"""Registry + null-transport + build_session acceptance tests."""

from __future__ import annotations

import pytest

from sleeptalk.builder import build_session, tts_model_for_provider, tts_spec
from sleeptalk.config import load_config
from sleeptalk.defaults import register_defaults
from sleeptalk.null_transport import NullTransport
from sleeptalk.registries import AGENTS, TTS_PROVIDERS


def _config(tmp_path, extra=""):
    path = tmp_path / "sleeptalk.toml"
    path.write_text(
        """\
[transport]
type = "null"

[agent]
type = "pi"
"""
        + extra
    )
    return load_config(path, check_env=False, check_binaries=False)


def test_null_transport_builds_and_runs(tmp_path):
    register_defaults()
    session = build_session(_config(tmp_path), env={})
    assert isinstance(session.transport, NullTransport)
    import asyncio

    asyncio.run(session.run())
    started = session.transport.events[0]
    assert started["event"] == "session_started"
    assert started["stt"] == "deepgram"
    assert started["tts"] == "cartesia"
    assert session.transport.events[-1]["event"] == "session_stopped"


def test_hybrid_builder_skips_cascade_speech_providers(tmp_path, monkeypatch):
    config = _config(
        tmp_path,
        extra="""\
[route]
type = "hybrid"
model = "gpt-realtime-2.1"
voice = "marin"
speech_model = "gpt-4o-mini-tts"
transcription_model = "gpt-4o-mini-transcribe"
""",
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("hybrid must not construct cascade STT/TTS")

    monkeypatch.setattr("sleeptalk.builder.build_stt_components", forbidden)
    monkeypatch.setattr("sleeptalk.builder.build_tts", forbidden)
    monkeypatch.setattr("sleeptalk.builder.build_tts_chain", forbidden)

    session = build_session(config, env={"OPENAI_API_KEY": "openai-key"})

    assert session.route_type.value == "hybrid"
    assert session.stt is None
    assert session.streaming_stt is None
    assert session.tts is None
    assert session.tts_fallbacks == []
    assert session.route._realtime.config.model == "gpt-realtime-2.1"
    assert session.route._realtime.config.speech_model == "gpt-4o-mini-tts"


def test_null_transport_logs_to_configured_path(tmp_path):
    log = tmp_path / "null.log"
    session = build_session(
        _config(tmp_path).model_copy(
            update={"transport": _config(tmp_path).transport.model_copy(
                update={"log_path": str(log)}
            )}
        ),
        env={},
    )
    import asyncio

    asyncio.run(session.run())
    assert "session_started" in log.read_text()


def test_agent_swap_requires_no_code_changes(tmp_path):
    register_defaults()
    pi_session = build_session(_config(tmp_path), env={})
    claude_session = build_session(
        _config(tmp_path).model_copy(
            update={"agent": _config(tmp_path).agent.model_copy(update={"type": "claude_code"})}
        ),
        env={},
    )
    assert type(pi_session.agent).__name__ == "PiRPCClient"
    assert type(claude_session.agent).__name__ == "ClaudeCodeClient"


def test_omp_agent_uses_omp_binary_without_sniff(tmp_path):
    register_defaults()
    config = _config(tmp_path).model_copy(
        update={"agent": _config(tmp_path).agent.model_copy(update={"type": "omp"})}
    )
    session = build_session(config, env={})
    assert type(session.agent).__name__ == "OmpRPCClient"
    assert session.agent.pi_bin == "omp"


def test_acp_alias_resolves_to_acp_factory(tmp_path):
    register_defaults()
    config = _config(tmp_path).model_copy(
        update={"agent": _config(tmp_path).agent.model_copy(update={"type": "hermes-acp"})}
    )
    session = build_session(config, env={})
    assert type(session.agent).__name__ == "HermesACPClient"
    assert session.agent.permission_mode == "auto"


def test_permission_mode_flows_to_adapters(tmp_path):
    register_defaults()
    base = _config(tmp_path)
    claude = build_session(
        base.model_copy(
            update={
                "agent": base.agent.model_copy(
                    update={"type": "claude_code", "permission_mode": "plan"}
                )
            }
        ),
        env={},
    )
    assert claude.agent.permission_mode == "plan"
    acp = build_session(
        base.model_copy(
            update={"agent": base.agent.model_copy(update={"type": "acp", "permission_mode": "deny"})}
        ),
        env={},
    )
    assert acp.agent.permission_mode == "deny"


def test_tts_fallback_chain_from_config(tmp_path):
    register_defaults()
    config = _config(
        tmp_path,
        extra="""\
[tts]
provider = "cartesia"
voice_id = "voice-1"

[[tts.fallback]]
provider = "elevenlabs"
voice_id = "voice-2"
""",
    )
    session = build_session(config, env={})
    assert type(session.tts).__name__ == "CartesiaTTS"
    assert session.tts.spec.voice_id == "voice-1"
    assert len(session.tts_fallbacks) == 1
    assert type(session.tts_fallbacks[0]).__name__ == "ElevenLabsTTS"
    assert session.tts_fallbacks[0].spec.voice_id == "voice-2"


def test_tts_provider_swap(tmp_path):
    register_defaults()
    config = _config(
        tmp_path,
        extra="""\
[tts]
provider = "elevenlabs"
base_url = "https://proxy.example/elevenlabs"
voice_id = "voice-9"
""",
    )
    session = build_session(config, env={})
    assert type(session.tts).__name__ == "ElevenLabsTTS"
    assert session.tts.spec.model == "eleven_multilingual_v2"
    assert session.tts.spec.options["base_url"] == (
        "https://proxy.example/elevenlabs"
    )


@pytest.mark.parametrize(
    ("provider", "model", "class_name", "env"),
    [
        ("playht", "Play3.0-mini", "PlayHTTTS", {
            "PLAYHT_API_KEY": "key", "PLAYHT_USER_ID": "user"
        }),
        ("rime", "mistv3", "RimeTTS", {"RIME_API_KEY": "key"}),
        ("hume", "", "HumeTTS", {"HUME_API_KEY": "key"}),
    ],
)
def test_discovered_tts_providers_build_from_normal_config(
    tmp_path, provider, model, class_name, env
):
    config = _config(
        tmp_path,
        extra=f'[tts]\nprovider = "{provider}"\nvoice_id = "voice-9"\n',
    )
    session = build_session(config, env=env)
    assert type(session.tts).__name__ == class_name
    assert session.tts.spec.model == model
    assert session.tts.spec.voice_id == "voice-9"


def test_adding_a_component_needs_only_a_registration(tmp_path, monkeypatch):
    """A fake transport registered at runtime works with zero builder edits."""
    register_defaults()

    class FakeTransport:
        def __init__(self, config, env):
            self.ran = False

        async def run(self, session):
            self.ran = True

    from sleeptalk.registries import Registry

    fake_registry = Registry("transport")
    fake_registry.register("fake", lambda config, env: FakeTransport(config, env))
    assert fake_registry.get("fake").name == "fake"
    with pytest.raises(ValueError, match="Valid options"):
        fake_registry.get("nope")


def test_builder_passes_env_secrets_to_components(tmp_path):
    register_defaults()
    config = _config(
        tmp_path,
        extra='[tts]\nprovider = "cartesia"\nvoice_id = "v"\n',
    )
    session = build_session(config, env={"CARTESIA_API_KEY": "secret-key"})
    assert session.tts.api_key == "secret-key"
    assert session.usage_meter is session.agent.usage_meter
    assert session.usage_meter is session.stt.usage_meter
    assert session.usage_meter is session.streaming_stt.usage_meter
    assert session.usage_meter is session.tts.usage_meter


def test_provider_base_urls_flow_from_config_to_specs(tmp_path):
    config = _config(
        tmp_path,
        extra="""\
[stt]
base_url = "https://proxy.example/deepgram"

[tts]
base_url = "https://proxy.example/cartesia"
voice_id = "voice-1"
""",
    )

    session = build_session(config, env={})

    assert session.stt.spec.options["base_url"] == "https://proxy.example/deepgram"
    assert session.streaming_stt.spec.options["base_url"] == (
        "https://proxy.example/deepgram"
    )
    assert session.tts.spec.options["base_url"] == "https://proxy.example/cartesia"


def test_tts_spec_replaces_cartesia_model_on_elevenlabs():
    from sleeptalk.config import TTSConfig

    spec = tts_spec(TTSConfig(provider="elevenlabs", model="sonic-3", voice_id="v"))
    assert spec.model == "eleven_multilingual_v2"
    assert tts_model_for_provider("elevenlabs", "eleven_turbo_v2_5") == "eleven_turbo_v2_5"
    assert tts_model_for_provider("cartesia", "sonic-3") == "sonic-3"


def test_tts_spec_rejects_cartesia_voice_uuid_as_model():
    from sleeptalk.config import TTSConfig

    voice = "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"
    spec = tts_spec(TTSConfig(provider="cartesia", model=voice, voice_id=voice))
    assert spec.model == "sonic-3.6"
    assert spec.voice_id == voice
    assert tts_model_for_provider("cartesia", voice) == "sonic-3.6"


def test_tts_spec_rejects_elevenlabs_voice_id_as_model():
    from sleeptalk.config import TTSConfig

    voice = "vGQNBgLaiM3EdZtxIiuY"
    spec = tts_spec(TTSConfig(provider="elevenlabs", model=voice, voice_id=voice))
    assert spec.model == "eleven_multilingual_v2"
    assert spec.voice_id == voice
    assert tts_model_for_provider("elevenlabs", voice) == "eleven_multilingual_v2"


def test_policy_reaches_session(tmp_path):
    register_defaults()
    config = _config(
        tmp_path,
        extra='[policy]\nendpointing = "aggressive"\n[policy.interrupt]\nmode = "off"\n',
    )
    session = build_session(config, env={})
    assert session.policy.endpointing == "aggressive"
    assert session.policy.allow_barge_in is False


def test_every_transport_config_field_is_claimed_by_some_transport():
    """A TOML key the schema accepts but no transport claims is unsettable.

    capture_mode shipped that way: TransportConfig documented it as terminal's
    headphones-or-speakers switch, the wizard wrote it into generated
    profiles, and validation then rejected every profile it had just written,
    because the terminal registration never listed it.
    """
    from sleeptalk.config import TransportConfig
    from sleeptalk.registries import TRANSPORTS

    register_defaults()
    claimed: set[str] = set()
    for name in ("terminal", "web", "discord", "null"):
        claimed |= set(TRANSPORTS.get(name).config_fields)
    orphaned = sorted(set(TransportConfig.model_fields) - claimed - {"type", "source_path", "options"})
    assert orphaned == [], orphaned


def test_terminal_accepts_capture_mode(tmp_path):
    """The switch `sleeptalk init` writes must survive `sleeptalk run`."""
    register_defaults()
    path = tmp_path / "sleeptalk.toml"
    path.write_text(
        '[transport]\ntype = "terminal"\ncapture_mode = "speakers"\n\n[agent]\ntype = "pi"\n'
    )
    config = load_config(path, check_env=False, check_binaries=False)
    assert config.transport.capture_mode == "speakers"
