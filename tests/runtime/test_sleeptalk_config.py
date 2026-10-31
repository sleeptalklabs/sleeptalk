"""Tests for the sleeptalk.toml schema + loader."""

from __future__ import annotations

from pathlib import Path

import pytest
from sleeptalk.config import (
    ConfigError,
    SleeptalkConfig,
    _load_env,
    load_config,
    load_profile_config,
    load_resolved_config,
    shared_root_for_project,
)
from sleeptalk.policy import VoicePolicy

MINIMAL = """\
[transport]
type = "null"

[agent]
type = "pi"
"""

def _write(tmp_path, text=MINIMAL):
    path = tmp_path / "sleeptalk.toml"
    path.write_text(text)
    return path


def load(path, **kwargs):
    kwargs.setdefault("check_env", False)
    kwargs.setdefault("check_binaries", False)
    return load_config(path, **kwargs)


def test_minimal_config_loads_with_defaults(tmp_path):
    config = load(_write(tmp_path))
    assert config.transport.type == "null"
    assert config.agent.type == "pi"
    assert config.stt.provider == "deepgram"
    assert config.stt.model == "nova-3"
    assert config.tts.provider == "cartesia"
    assert config.policy.endpointing == "balanced"
    assert config.policy.fillers is True
    assert config.policy.interrupt.enabled is True
    assert config.policy.interrupt.mode == "immediate"
    assert config.policy.interrupt.min_duration_ms == 150
    assert config.policy.interrupt.grace_ms == 400
    assert config.policy.interrupt.gap_hold_ms == 600
    assert config.policy.interrupt.cooldown_ms == 1200
    assert config.policy.interrupt.min_volume == 0.03
    assert config.policy.resume_after_interrupt is True


def test_hybrid_route_uses_typed_openai_defaults_and_ignores_cascade_providers(
    tmp_path,
):
    text = MINIMAL + """

[route]
type = "hybrid"

[stt]
provider = "not-a-provider"

[tts]
provider = "also-not-a-provider"
fallback = [{provider = "not-a-fallback"}]
"""
    config = load_config(
        _write(tmp_path, text),
        env={"OPENAI_API_KEY": "key"},
        check_binaries=False,
    )

    assert config.route.type == "hybrid"
    assert config.route.provider == "openai-realtime"
    assert config.route.model == "gpt-realtime-2.1"
    assert config.route.voice == "marin"
    assert config.route.speech_model == "gpt-4o-mini-tts"
    assert config.route.transcription_model == "gpt-4o-mini-transcribe"


def test_hybrid_route_requires_only_openai_voice_credentials(tmp_path):
    path = _write(
        tmp_path,
        MINIMAL
        + '\n[route]\ntype = "hybrid"\n'
        + '\n[tts]\nfallback = [{provider = "elevenlabs"}]\n',
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={}, check_binaries=False)

    message = str(excinfo.value)
    assert "OPENAI_API_KEY" in message
    assert "DEEPGRAM_API_KEY" not in message
    assert "CARTESIA_API_KEY" not in message
    assert "ELEVENLABS_API_KEY" not in message


def test_missing_file_names_the_fix(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "nope.toml")


def test_bad_toml_reports_parse_error(tmp_path):
    path = _write(tmp_path, "[transport\noops")
    with pytest.raises(ConfigError, match="does not parse as TOML"):
        load(path)


def test_unknown_key_names_key_and_valid_options(tmp_path):
    path = _write(tmp_path, MINIMAL + '\n[policy]\nfillers = true\nfillerz = true\n')
    with pytest.raises(ConfigError) as excinfo:
        load(path)
    message = str(excinfo.value)
    assert "fillerz" in message
    assert "fillers" in message  # valid options listed


def test_unknown_section_rejected(tmp_path):
    path = _write(tmp_path, MINIMAL + '\n[wizard]\nfoo = 1\n')
    with pytest.raises(ConfigError, match="unknown section"):
        load(path)


def test_unknown_discriminator_lists_valid_options(tmp_path):
    path = _write(tmp_path, MINIMAL.replace('type = "pi"', 'type = "gpt"'))
    with pytest.raises(ConfigError) as excinfo:
        load(path)
    message = str(excinfo.value)
    assert "Unknown agent 'gpt'" in message
    assert "claude_code" in message and "hermes_gateway" in message


def test_room_fields_default_conversationalist(tmp_path):
    config = load(_write(tmp_path))
    assert config.agent.participation_mode == "conversationalist"
    assert config.agent.addressable_name == ""
    assert config.agent.proactive_triggers == []


def test_room_fields_apply_to_every_agent_type(tmp_path):
    path = _write(
        tmp_path,
        MINIMAL
        + '\nparticipation_mode = "attendee"\naddressable_name = "Notes"\n',
    )
    config = load(path)
    assert config.agent.participation_mode == "attendee"
    assert config.agent.addressable_name == "Notes"


def test_unknown_participation_mode_is_a_hard_error(tmp_path):
    path = _write(tmp_path, MINIMAL + '\nparticipation_mode = "heckler"\n')
    with pytest.raises(ConfigError, match="heckler"):
        load(path)


def test_proactive_without_triggers_is_a_hard_error(tmp_path):
    path = _write(tmp_path, MINIMAL + '\nparticipation_mode = "proactive"\n')
    with pytest.raises(ConfigError, match="proactive_triggers"):
        load(path)


def test_agent_aliases_resolve(tmp_path):
    path = _write(tmp_path, MINIMAL.replace('type = "pi"', 'type = "hermes-acp"'))
    config = load(path)
    from sleeptalk.defaults import register_defaults
    from sleeptalk.registries import AGENTS

    register_defaults()
    assert AGENTS.canonical_name(config.agent.type) == "acp"


def test_inapplicable_field_for_type_is_a_hard_error(tmp_path):
    path = _write(tmp_path, MINIMAL.replace("[agent]", '[agent]\ngateway_url = "http://127.0.0.1:1"'))
    with pytest.raises(ConfigError) as excinfo:
        load(path)
    assert "gateway_url" in str(excinfo.value)
    assert "type='pi'" in str(excinfo.value)


def test_policy_literal_validation_lists_choices(tmp_path):
    path = _write(tmp_path, MINIMAL + '\n[policy]\nendpointing = "sleepy"\n')
    with pytest.raises(ConfigError):
        load(path)


def test_leftover_policy_barge_in_fails_load(tmp_path):
    path = _write(tmp_path, MINIMAL + '\n[policy]\nbarge_in = "soft"\n')
    with pytest.raises(ConfigError, match="Unknown key 'barge_in'"):
        load(path)


def test_interrupt_mode_adaptive_fails_load(tmp_path):
    path = _write(tmp_path, MINIMAL + '\n[policy.interrupt]\nmode = "adaptive"\n')
    with pytest.raises(ConfigError, match="policy.interrupt.mode"):
        load(path)


def test_interrupt_soft_mode_loads_onto_voice_policy(tmp_path):
    path = _write(tmp_path, MINIMAL + '\n[policy.interrupt]\nmode = "soft"\n')
    config = load(path)
    policy = VoicePolicy.from_config(config.policy)
    assert config.policy.interrupt.mode == "soft"
    assert policy.barge_in == "soft"
    assert policy.barge_in_mode == "transcript"


def test_missing_env_var_named_with_fix(tmp_path):
    text = MINIMAL.replace('type = "null"', 'type = "discord"')
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={}, check_binaries=False)
    assert "DISCORD_BOT_TOKEN" in str(excinfo.value)
    assert "discord.com/developers" in str(excinfo.value)


def test_transport_token_env_is_configurable(tmp_path):
    text = MINIMAL.replace(
        'type = "null"', 'type = "discord"\ntoken_env = "MY_BOT_TOKEN"'
    )
    path = _write(tmp_path, text)
    config = load_config(
        path,
        env={
            "MY_BOT_TOKEN": "x",
            "DEEPGRAM_API_KEY": "k",
            "CARTESIA_API_KEY": "k",
        },
        check_binaries=False,
    )
    assert config.transport.token_env == "MY_BOT_TOKEN"


def test_profile_loads_env_from_self_contained_project_root(tmp_path, monkeypatch):
    key = "SLEEPTALK_TEST_PROJECT_ROOT_KEY"
    monkeypatch.delenv(key, raising=False)
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    profile = profiles / "deepre.toml"
    profile.write_text(MINIMAL)
    (tmp_path / ".env").write_text(f"{key}=from-project-root\n")

    loaded = _load_env(profile)

    assert loaded[key] == "from-project-root"


def test_missing_binary_named_with_install_hint(tmp_path):
    text = MINIMAL.replace('type = "pi"', 'type = "pi"\npi_bin = "definitely-not-a-real-bin"')
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, check_env=False, check_binaries=True)
    message = str(excinfo.value)
    assert "definitely-not-a-real-bin" in message
    assert "install" in message


def test_all_config_errors_collected_in_one_pass(tmp_path):
    text = (
        '[transport]\ntype = "discord"\n\n'
        '[agent]\ntype = "gpt"\n\n'
        '[stt]\nprovider = "not-a-provider"\n'
    )
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={}, check_binaries=False)
    message = str(excinfo.value)
    assert "Unknown agent 'gpt'" in message
    assert "not-a-provider" in message
    assert "DISCORD_BOT_TOKEN" in message  # env stage still aggregated


def test_tts_fallback_provider_validated(tmp_path):
    text = MINIMAL + '\n[[tts.fallback]]\nprovider = "not-a-tts"\n'
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match="not-a-tts"):
        load(path)


def test_config_is_frozen_schema_object(tmp_path):
    config = load(_write(tmp_path))
    assert isinstance(config, SleeptalkConfig)
    assert config.policy.turns.vad_threshold == 500
    assert config.policy.capture.silence_ms == 450


# --- Layered profile resolution (load_profile_config) -----------------------

ROOT = """\
[transport]
type = "null"

[agent]
type = "pi"

[policy]
filler_general = ["root one.", "root two."]
barge_in_min_chars = 4

[policy.turns]
vad_threshold = 500
max_wait_seconds = 120.0

[tts]
provider = "cartesia"
voice_id = "root-voice"
fallback = [{provider = "elevenlabs", voice_id = "root-fallback"}]
"""


def _write_layered(tmp_path, root=ROOT, profile=""):
    root_path = tmp_path / "sleeptalk.toml"
    root_path.write_text(root)
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    profile_path = profiles / "bot.toml"
    profile_path.write_text(profile)
    return root_path, profiles


def load_layered(root_path, profiles, slug="bot", **kwargs):
    kwargs.setdefault("check_env", False)
    kwargs.setdefault("check_binaries", False)
    return load_profile_config(
        slug, root_path=root_path, profiles_dir=profiles, **kwargs
    )


def test_profile_inherits_root_when_overlay_empty(tmp_path):
    root_path, profiles = _write_layered(tmp_path)
    config = load_layered(root_path, profiles)
    assert config.policy.barge_in_min_chars == 4
    assert config.policy.turns.vad_threshold == 500
    assert config.tts.voice_id == "root-voice"


def test_profile_missing_is_hard_error(tmp_path):
    root_path, profiles = _write_layered(tmp_path)
    with pytest.raises(ConfigError, match="cannot boot without its TOML"):
        load_layered(root_path, profiles, slug="ghost")


def test_single_policy_field_override_keeps_siblings(tmp_path):
    root_path, profiles = _write_layered(
        tmp_path,
        profile=(
            "[policy.turns]\n"
            "max_wait_seconds = 300.0\n"
            "long_task_check_in_seconds = 240.0\n"
            "long_task_auto_continue_seconds = 45.0\n"
        ),
    )
    config = load_layered(root_path, profiles)
    assert config.policy.turns.max_wait_seconds == 300.0
    assert config.policy.turns.long_task_check_in_seconds == 240.0
    assert config.policy.turns.long_task_auto_continue_seconds == 45.0
    assert config.policy.turns.vad_threshold == 500  # inherited, not reset
    assert config.policy.barge_in_min_chars == 4


def test_lists_replace_wholesale_never_element_merge(tmp_path):
    root_path, profiles = _write_layered(
        tmp_path,
        profile=(
            '[policy]\nfiller_general = ["mine only."]\n'
            '[tts]\nfallback = [{provider = "elevenlabs", voice_id = "mine"}]\n'
        ),
    )
    config = load_layered(root_path, profiles)
    assert config.policy.filler_general == ["mine only."]
    assert len(config.tts.fallback) == 1
    assert config.tts.fallback[0].voice_id == "mine"
    assert config.tts.voice_id == "root-voice"  # untouched sibling inherits


def test_route_type_change_replaces_the_discriminated_table(tmp_path):
    root_path, profiles = _write_layered(
        tmp_path,
        root=(
            ROOT
            + '\n[route]\ntype = "hybrid"\nmodel = "gpt-realtime-2.1"\n'
        ),
        profile='[route]\ntype = "cascade"\n',
    )

    config = load_layered(root_path, profiles)

    assert config.route.type == "cascade"
    assert not hasattr(config.route, "model")


def test_string_lists_replace_wholesale(tmp_path):
    # allowed_channel_ids / keyterms are scalars in the schema; confirm a
    # profile value fully replaces root rather than concatenating.
    root_path, profiles = _write_layered(
        tmp_path,
        root=ROOT + '\n[stt]\nkeyterms = "rootterm"\n',
        profile='[stt]\nkeyterms = "mineterm"\n',
    )
    config = load_layered(root_path, profiles)
    assert config.stt.keyterms == "mineterm"


def test_shared_root_prefers_local_toml_then_example(tmp_path):
    example = tmp_path / "sleeptalk.example.toml"
    example.write_text(MINIMAL)
    assert shared_root_for_project(tmp_path) == example
    local = tmp_path / "sleeptalk.toml"
    local.write_text(MINIMAL)
    assert shared_root_for_project(tmp_path) == local


def test_profile_layers_onto_example_when_local_toml_missing(tmp_path):
    (tmp_path / "sleeptalk.example.toml").write_text(MINIMAL)
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "bot.toml").write_text("[policy]\nbarge_in_min_chars = 9\n")
    config = load_resolved_config(
        profiles / "bot.toml", check_env=False, check_binaries=False
    )
    assert config.transport.type == "null"
    assert config.policy.barge_in_min_chars == 9


def test_missing_root_is_hard_error_for_profile(tmp_path):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "bot.toml").write_text('[transport]\ntype = "null"\n[agent]\ntype = "pi"\n')
    with pytest.raises(ConfigError, match="requires the shared sleeptalk.toml"):
        load_layered(tmp_path / "nope.toml", profiles)


def test_resolved_loader_auto_layers_supported_profile_layouts(tmp_path):
    root_path, profiles = _write_layered(
        tmp_path,
        profile='[policy]\nfiller_general = ["profile only."]\n',
    )
    config = load_resolved_config(
        profiles / "bot.toml", check_env=False, check_binaries=False
    )
    assert config.policy.turns.vad_threshold == 500
    assert config.policy.barge_in_min_chars == 4
    assert config.policy.filler_general == ["profile only."]
    assert config.source_path == str(profiles / "bot.toml")
    assert root_path.is_file()
    by_slug = load_profile_config(
        "bot",
        root_path=root_path,
        profiles_dir=profiles,
        check_env=False,
        check_binaries=False,
    )
    assert by_slug.model_dump() == config.model_dump()


def test_resolved_loader_requires_root_for_auto_detected_profile(tmp_path):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    profile = profiles / "bot.toml"
    profile.write_text('[transport]\ntype = "null"\n[agent]\ntype = "pi"\n')
    with pytest.raises(ConfigError, match="requires the shared sleeptalk.toml"):
        load_resolved_config(profile, check_env=False, check_binaries=False)


def test_all_tracked_profiles_resolve_against_tracked_root():
    repo_root = Path(__file__).resolve().parents[2]
    profiles = [repo_root / "config" / "profiles" / "example.toml"]
    assert all(path.is_file() for path in profiles)
    for profile in profiles:
        config = load_resolved_config(
            profile, check_env=False, check_binaries=False
        )
        assert config.source_path == str(profile)
        assert config.agent.slug == profile.stem


# --- workspace_dir resolution (self-contained-folder pattern) ---------------


def test_source_path_propagates_to_agent_section(tmp_path):
    config = load(_write(tmp_path))
    assert config.source_path == str(tmp_path / "sleeptalk.toml")
    assert config.agent.source_path == config.source_path


def test_agent_cwd_absolute_workspace_used_as_is(tmp_path):
    from sleeptalk.defaults import _agent_cwd

    target = tmp_path / "elsewhere"
    config = SleeptalkConfig()
    config.agent.workspace_dir = str(target)
    assert _agent_cwd(config.agent) == str(target)
    assert target.is_dir()


def test_agent_cwd_relative_workspace_anchors_to_profile_root(tmp_path):
    """The wizard writes workspace_dir = "." — that must mean the profile's
    project root, not the cwd of whoever invoked sleeptalk."""
    from sleeptalk.defaults import _agent_cwd

    root = tmp_path / "my-bot"
    profile = root / "config" / "profiles" / "claude.toml"
    profile.parent.mkdir(parents=True)
    profile.write_text('[transport]\ntype = "null"\n[agent]\ntype = "pi"\nworkspace_dir = "."\n')

    from sleeptalk.config import load_config

    config = load_config(profile, check_env=False, check_binaries=False)
    assert _agent_cwd(config.agent) == str(root.resolve())


def test_agent_cwd_tier2_profiles_layout_anchors_to_home_root(tmp_path):
    """SLEEPTALK_HOME layout is <root>/profiles/<slug>.toml — '.' must anchor
    to <root> (where .mcp.json/CLAUDE.md live), not the profiles/ subfolder."""
    from sleeptalk.defaults import _agent_cwd

    root = tmp_path / "my-bot"
    profile = root / "profiles" / "claude.toml"
    profile.parent.mkdir(parents=True)
    profile.write_text('[transport]\ntype = "null"\n[agent]\ntype = "pi"\nworkspace_dir = "."\n')

    from sleeptalk.config import load_config

    config = load_config(profile, check_env=False, check_binaries=False)
    assert _agent_cwd(config.agent) == str(root.resolve())


def test_agent_cwd_relative_subdir_anchors_to_profile_root(tmp_path):
    from sleeptalk.defaults import _agent_cwd

    config = SleeptalkConfig()
    config.agent.workspace_dir = "work/space"
    config.agent.source_path = str(tmp_path / "config" / "profiles" / "x.toml")
    resolved = _agent_cwd(config.agent)
    assert resolved == str(tmp_path.resolve() / "work" / "space")


def test_agent_cwd_empty_workspace_falls_back_to_cwd(tmp_path, monkeypatch):
    from sleeptalk.defaults import _agent_cwd

    monkeypatch.chdir(tmp_path)
    config = SleeptalkConfig()
    assert _agent_cwd(config.agent) == str(tmp_path)


def test_agent_cwd_relative_without_source_path_uses_cwd(tmp_path, monkeypatch):
    """Back-compat: no loaded config file => legacy cwd-relative behavior."""
    from sleeptalk.defaults import _agent_cwd

    monkeypatch.chdir(tmp_path)
    config = SleeptalkConfig()
    config.agent.workspace_dir = "."
    assert _agent_cwd(config.agent) == str(tmp_path)
