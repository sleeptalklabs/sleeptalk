"""Independent public-boundary acceptance tests for config policy inheritance.

The fixtures contain no credentials. Tests use public loaders/entrypoints and
never inspect or mutate runtime-private state.
"""

from __future__ import annotations

import asyncio
import hashlib
import shlex
from pathlib import Path

import pytest

from sleeptalk import web_call
from sleeptalk import cli, config as config_module, doctor, service, wizard
from sleeptalk.call_provider import CallContext
from sleeptalk.config import ConfigError, load_profile_config, load_resolved_config


ROOT_TOML = """\
[transport]
type = "null"
log_path = "root-capture.log"

[agent]
type = "claude_code"
name = "Root Policy Bot"
slug = "root-policy-bot"

[tts]
provider = "cartesia"
fallback = [
  { provider = "elevenlabs", voice_id = "root-fallback-one" },
  { provider = "cartesia", voice_id = "root-fallback-two" },
]

[policy]
filler_delay_ms = 1311
first_token_budget_ms = 2711
filler_general = ["root filler one", "root filler two"]
barge_in_min_chars = 17
reply_max_chars = 733
preamble_pools = { action = ["root action preamble"] }

[policy.turns]
vad_threshold = 613
interrupt_min_ms = 377

[policy.endpointing_overrides]
complete_silence_ms = 911

[policy.capture]
rms_threshold = 419
smart_turn_threshold = 0.83
"""

PROFILE_TOML = """\
[agent]
slug = "policy-bot"

[tts]
fallback = [{ provider = "cartesia", voice_id = "profile-fallback-only" }]

[policy]
filler_general = ["profile replacement only"]
"""

ROOT_SHA256 = "ba88c0c3996f039e5cb0c91377360e19ae1ea344fc8b8778921ed6354898ad72"
PROFILE_SHA256 = "03730cc05190074765fda38dcd6b0bd0c46fd6a588e4e731d758a0172af8e1d2"


@pytest.fixture
def config_paths(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "sleeptalk.toml"
    profile = tmp_path / "config" / "profiles" / "policy-bot.toml"
    profile.parent.mkdir(parents=True)
    root.write_text(ROOT_TOML, encoding="utf-8")
    profile.write_text(PROFILE_TOML, encoding="utf-8")
    return root, profile


def public_dump(config) -> dict:
    """Remove loader provenance while retaining the public effective model."""
    return config.model_dump(
        exclude={"source_path": True, "agent": {"source_path": True}}
    )


def test_canonical_root_profile_policy_and_receipt(
    config_paths: tuple[Path, Path], tmp_path: Path
) -> None:
    root, profile = config_paths
    config = load_resolved_config(
        profile, env={}, check_env=False, check_binaries=False
    )
    by_slug = load_profile_config(
        "policy-bot",
        root_path=root,
        profiles_dir=profile.parent,
        env={},
        check_env=False,
        check_binaries=False,
    )

    assert public_dump(config) == public_dump(by_slug)

    # Root-only sentinels span every named voice-policy family.
    assert config.policy.filler_delay_ms == 1311  # thinking
    assert config.policy.preamble_pools == {"action": ["root action preamble"]}
    assert config.policy.turns.vad_threshold == 613  # VAD
    assert config.policy.endpointing_overrides.complete_silence_ms == 911
    assert config.policy.capture.smart_turn_threshold == 0.83  # smart turn
    assert config.policy.capture.rms_threshold == 419  # capture
    assert config.policy.turns.interrupt_min_ms == 377  # interruption
    assert config.policy.barge_in_min_chars == 17
    assert config.policy.reply_max_chars == 733  # reply shaping
    assert config.policy.first_token_budget_ms == 2711  # latency

    # Mappings deep-merge, while lists replace rather than append.
    assert config.policy.filler_general == ["profile replacement only"]
    assert [item.voice_id for item in config.tts.fallback] == [
        "profile-fallback-only"
    ]

    receipt = {
        "root_sha256": hashlib.sha256(root.read_bytes()).hexdigest(),
        "profile_sha256": hashlib.sha256(profile.read_bytes()).hexdigest(),
    }
    assert receipt == {
        "root_sha256": ROOT_SHA256,
        "profile_sha256": PROFILE_SHA256,
    }

    fixture_text = root.read_text(encoding="utf-8") + profile.read_text(
        encoding="utf-8"
    )
    assert all(
        marker not in fixture_text
        for marker in ("api_key", "bot_token", "password", "secret")
    )

    orphan = tmp_path / "orphan" / "profiles" / "policy-bot.toml"
    orphan.parent.mkdir(parents=True)
    orphan.write_text(PROFILE_TOML, encoding="utf-8")
    with pytest.raises(ConfigError, match="requires the shared sleeptalk.toml"):
        load_resolved_config(
            orphan, env={}, check_env=False, check_binaries=False
        )


class RecordingSession:
    def __init__(self, config, captures: list) -> None:
        self.config = config
        self.captures = captures

    async def run(self) -> None:
        self.captures.append(self.config)


class BoundaryCaptured(RuntimeError):
    pass


@pytest.fixture
def expected_config(config_paths: tuple[Path, Path]):
    _, profile = config_paths
    return load_resolved_config(
        profile,
        env={},
        check_env=False,
        check_binaries=False,
    )


@pytest.fixture
def disable_external_startup_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_module, "collect_missing_env", lambda config, env: [])
    monkeypatch.setattr(config_module, "collect_missing_binaries", lambda config, **kwargs: [])


def test_cli_and_installed_service_execute_with_the_canonical_model(
    config_paths: tuple[Path, Path],
    expected_config,
    monkeypatch: pytest.MonkeyPatch,
    disable_external_startup_requirements: None,
) -> None:
    _, profile = config_paths
    captures = []

    monkeypatch.setattr(
        cli,
        "build_session",
        lambda config: RecordingSession(config, captures),
    )
    monkeypatch.setattr(
        "sleeptalk.logging_setup.setup_file_logging",
        lambda **kwargs: Path("acceptance.log"),
    )

    assert cli.main(["run", "--config", str(profile), "--skip-checks"]) == 0
    assert captures[-1].model_dump() == expected_config.model_dump()

    unit = service.render_unit("policy-bot", profile, system="Linux")
    exec_line = next(
        line.removeprefix("ExecStart=")
        for line in unit.contents.splitlines()
        if line.startswith("ExecStart=")
    )
    command = shlex.split(exec_line)
    run_index = command.index("run")
    service_cli_args = command[run_index:] + ["--skip-checks"]

    assert cli.main(service_cli_args) == 0
    assert captures[-1].model_dump() == expected_config.model_dump()
    assert len(captures) == 2


def test_doctor_executes_with_the_canonical_model(
    config_paths: tuple[Path, Path],
    expected_config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, profile = config_paths
    captures = []

    def observe_missing_env(config, env) -> list[str]:
        captures.append(config)
        return []

    monkeypatch.setattr(doctor, "collect_missing_env", observe_missing_env)
    monkeypatch.setattr(doctor, "collect_missing_binaries", lambda config, **kwargs: [])

    report = asyncio.run(doctor.run_doctor(profile, env={}, live=False))

    assert not report.failed
    assert len(captures) == 1
    assert captures[0].model_dump() == expected_config.model_dump()


def test_wizard_in_process_launch_executes_with_the_canonical_model(
    config_paths: tuple[Path, Path],
    expected_config,
    monkeypatch: pytest.MonkeyPatch,
    disable_external_startup_requirements: None,
) -> None:
    _, profile = config_paths
    observed = []
    sessions = []

    monkeypatch.setattr(
        wizard,
        "build_session",
        lambda config: RecordingSession(config, sessions),
    )

    async def checks_ok(config) -> None:
        return None

    monkeypatch.setattr(wizard, "run_connectivity_checks", checks_ok)

    asyncio.run(wizard.launch_profile(profile, before_run=observed.append))

    assert len(observed) == 1
    assert observed[0].model_dump() == expected_config.model_dump()
    assert len(sessions) == 1
    assert sessions[0].model_dump() == expected_config.model_dump()


def test_profile_web_call_executes_with_the_canonical_model(
    config_paths: tuple[Path, Path],
    expected_config,
    monkeypatch: pytest.MonkeyPatch,
    disable_external_startup_requirements: None,
) -> None:
    _, profile = config_paths
    captures = []

    def observe_build(config):
        captures.append(config)
        raise BoundaryCaptured("web call received config")

    monkeypatch.setattr(web_call, "build_session", observe_build)

    context = CallContext(
        agent_instance="policy-bot",
        conversation_id="acceptance-conversation",
        platform_user_id="acceptance-user",
    )

    with pytest.raises(BoundaryCaptured, match="web call received config"):
        asyncio.run(
            web_call.run_profile_web_call(
                config_path=str(profile),
                rendezvous_url="https://rendezvous.invalid",
                context=context,
                ttl_seconds=30,
                on_ready=lambda url: None,
            )
        )

    assert len(captures) == 1
    from sleeptalk.transports.web import WebOptions

    expected_web = expected_config.model_copy(
        update={
            "transport": expected_config.transport.model_copy(
                update={"type": "web", "options": WebOptions().model_dump()}
            )
        }
    )
    assert captures[0].model_dump() == expected_web.model_dump()
