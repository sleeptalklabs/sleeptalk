from __future__ import annotations

import pytest
from pydantic import ValidationError
from sleeptalk.builder import build_session

from sleeptalk.config import SleeptalkConfig


@pytest.mark.parametrize(
    ("name", "slug", "expected"),
    [
        ("Codex", "codex", "Codex"),
        ("   ", "hybrid-lab", "hybrid-lab"),
        ("", "", "Sleeptalk"),
    ],
)
def test_hybrid_builder_shares_one_role_contract(
    name: str,
    slug: str,
    expected: str,
) -> None:
    config = SleeptalkConfig.model_validate(
        {
            "agent": {"type": "pi", "name": name, "slug": slug},
            "transport": {"type": "null"},
            "route": {"type": "hybrid", "provider": "openai-realtime"},
        }
    )

    session = build_session(config, env={"OPENAI_API_KEY": "test-key"})

    assert session.route.role_contract.display_name == expected
    assert session.route._realtime.role_contract is session.route.role_contract
    assert session.route._speculative_audio is True
    assert session.route._realtime.config.specialist_tool is True


def test_hybrid_builder_wires_live_policy_controls() -> None:
    config = SleeptalkConfig.model_validate(
        {
            "agent": {"type": "pi"},
            "transport": {"type": "null"},
            "route": {"type": "hybrid", "provider": "openai-realtime"},
            "policy": {
                "reply_length": "summarized",
                "narration_style": "keyboard",
            },
        }
    )
    session = build_session(config, env={"OPENAI_API_KEY": "test-key"})

    assert session.route._realtime.reply_length() == "summarized"
    assert session.route._mouth._narration_style() == "keyboard"
    session.policy.reply_length = "full"
    session.policy.narration_style = "silence"
    assert session.route._realtime.reply_length() == "full"
    assert session.route._mouth._narration_style() == "silence"


def test_hybrid_builder_starts_at_the_configured_speech_speed() -> None:
    config = SleeptalkConfig.model_validate(
        {
            "agent": {"type": "pi"},
            "transport": {"type": "null"},
            "route": {"type": "hybrid", "provider": "openai-realtime"},
            "tts": {"speed": 1.25},
        }
    )

    session = build_session(config, env={"OPENAI_API_KEY": "test-key"})

    assert session.current_tts_speed() == 1.25


@pytest.mark.parametrize("enabled", [False, True])
def test_speculative_audio_reaches_route_only_with_semantic_routing(enabled):
    config = SleeptalkConfig.model_validate({
        "transport": {"type": "null"},
        "route": {"type": "hybrid", "specialist_tool": enabled,
                  "speculative_audio": enabled},
    })
    session = build_session(config, env={"OPENAI_API_KEY": "test-key"})
    assert session.route._speculative_audio is enabled


def test_speculative_audio_rejects_regex_only_routing():
    with pytest.raises(ValidationError, match="speculative_audio requires specialist_tool"):
        SleeptalkConfig.model_validate({
            "route": {"type": "hybrid", "specialist_tool": False,
                      "speculative_audio": True},
        })


@pytest.mark.parametrize("transport", ["discord", "terminal", "web"])
def test_hybrid_boot_defaults_apply_to_every_transport(transport):
    config = SleeptalkConfig.model_validate({
        "transport": {"type": transport},
        "route": {"type": "hybrid"},
    })
    assert config.route.specialist_tool is True
    assert config.route.speculative_audio is True


@pytest.mark.parametrize("route, expected", [
    ({}, True),
    ({"audio_first": False}, False),
    ({"specialist_tool": False, "speculative_audio": False}, False),
])
def test_audio_first_is_the_default_and_reaches_the_route(route, expected):
    config = SleeptalkConfig.model_validate({
        "transport": {"type": "null"},
        "route": {"type": "hybrid", **route},
    })
    session = build_session(config, env={"OPENAI_API_KEY": "test-key"})
    assert session.route._audio_first is expected

