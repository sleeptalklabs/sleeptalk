"""Discovery cannot promote caller claims into permission to start a call."""

from itertools import product
import json
from pathlib import Path

import pytest

from sleeptalk.onboarding import (
    Capability as C, Context, DiscoveryErrorReceipt, DiscoveryReceipt, Fact, Intent, State as S,
    contract_schema, discover, resolve_plan,
)


def verified(**overrides):
    return tuple(
        Fact(capability=key, state=overrides.get(key.value, S.AVAILABLE),
             evidence="integration_probe", scope=("intended_runtime",))
        for key in C
    )


def request(**overrides):
    return Intent(**{"workspace": "/chosen-workspace", "agent": "codex", **overrides})


def test_missing_identity_is_not_inferred_from_cwd_or_one_binary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sleeptalk.registries.Registration.find_binary", lambda *_: "/bin/only-agent")
    result = discover(Intent())
    assert result.selected_agent is None
    assert {"workspace", "voice_runtime"} <= set(result.plan.decisions)
    assert next(f for f in result.facts if f.capability == C.WORKSPACE).state == S.UNKNOWN


def test_claims_cannot_establish_host_operator_login_or_audio(tmp_path):
    result = discover(Intent(workspace=str(tmp_path), agent="codex", surface="web"), Context(
        harness="codex", claims={key: S.AVAILABLE for key in C},
    ))
    facts = {f.capability: f for f in result.facts}
    for key in (C.HARNESS, C.HOST_BINDING, C.LOGIN, C.OPERATOR, C.HOST_KEY, C.HTTPS, C.MEDIA):
        assert facts[key].state == S.UNKNOWN
        assert facts[key].claim == S.AVAILABLE
    assert facts[C.REMOTE_INPUT].state == S.UNKNOWN
    assert facts[C.REMOTE_INPUT].evidence == "not_probed"
    assert facts[C.WORKSPACE].state == S.AVAILABLE
    assert result.continuity == "unverified"
    assert result.status != "ready"


@pytest.mark.parametrize("label", ["Codex", "Telegram", "iMessage", "desktop", "new app"])
def test_app_label_does_not_change_plan(tmp_path, label):
    intent = Intent(workspace=str(tmp_path), agent="codex")
    baseline = discover(intent, Context(app_label="reference"))
    assert discover(intent, Context(app_label=label)) == baseline


def test_unsupported_runtime_does_not_fall_back_or_echo_input(tmp_path):
    marker = "sentinel-custom-runtime"
    result = discover(Intent(workspace=str(tmp_path), agent=marker))
    assert result.status == "failed"
    assert result.selected_agent is None
    assert marker not in result.model_dump_json()
    assert result.plan.operations[0].requirements[1].capability == C.ADAPTER


def test_inaccessible_workspace_is_precise_failure(tmp_path):
    result = discover(Intent(workspace=str(tmp_path / "missing"), agent="codex"))
    requirements = result.plan.operations[0].requirements
    assert any(r.capability == C.WORKSPACE and r.state == S.UNAVAILABLE for r in requirements)
    assert str(tmp_path) not in result.model_dump_json()
    assert "missing" not in result.model_dump_json()


def test_discovery_does_not_read_keys_open_network_or_spawn(tmp_path, monkeypatch):
    import socket
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError("discovery crossed a side-effect boundary")

    # Register first so this test guards discovery, not Python's module loader.
    from sleeptalk.defaults import register_defaults
    register_defaults()
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr("sleeptalk.config._load_env", forbidden)
    monkeypatch.setattr("sleeptalk.setup_core.detect_agents", forbidden)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "sentinel-speech-key")
    monkeypatch.setenv("SLEEPTALK_CALLING_AGENT", "sentinel-caller-hint")
    before = list(tmp_path.iterdir())
    result = discover(Intent(workspace=str(tmp_path), agent="codex"))
    assert list(tmp_path.iterdir()) == before
    assert "sentinel" not in result.model_dump_json()


def test_registry_inventory_never_loads_plugins(monkeypatch):
    from sleeptalk.registries import Registry
    registry = Registry("discovery-test")
    registry.register("known", lambda *_: None, aliases=("alias",))
    monkeypatch.setattr(registry, "_plugins", lambda: pytest.fail("plugin lookup"))
    assert [r.name for r in registry.registrations()] == ["known"]


@pytest.mark.parametrize("surface", ["terminal", "web", "discord"])
def test_explicit_surface_is_preserved_even_when_unavailable(surface):
    facts = verified(audio_devices=S.UNAVAILABLE, secure_audio_page=S.UNAVAILABLE, discord_bot=S.UNAVAILABLE)
    result = resolve_plan(request(surface=surface), facts)
    assert result.surface == surface
    assert result.status == "failed"


def test_saved_preference_requires_verified_viability():
    result = resolve_plan(request(preferred_surface="web"), verified(secure_audio_page=S.UNKNOWN))
    assert result.surface is None
    assert "surface" in result.decisions
    result = resolve_plan(request(preferred_surface="web"), verified())
    assert result.surface == "web"


def test_unknown_route_is_not_eliminated_to_infer_another():
    result = resolve_plan(request(), verified(audio_devices=S.UNKNOWN, discord_bot=S.UNAVAILABLE))
    assert result.surface is None
    result = resolve_plan(request(), verified(audio_devices=S.UNAVAILABLE, discord_bot=S.UNAVAILABLE))
    assert result.surface == "web"
    assert result.status == "ready"


def test_private_terminal_does_not_require_unimplemented_remote_entry():
    result = resolve_plan(request(surface="terminal"), verified(
        speech_readiness=S.UNKNOWN, local_private_input=S.UNAVAILABLE, remote_private_input=S.UNAVAILABLE,
    ))
    assert next(o for o in result.operations if o.name == "collect_speech").status == "ready"
    assert result.status == "needs_input"


def test_invalid_runtime_blocks_credential_collection_and_call_start():
    result = resolve_plan(request(surface="web"), verified(adapter_login=S.UNAVAILABLE, speech_readiness=S.UNKNOWN))
    assert all(o.status == "failed" for o in result.operations)


def test_remote_entry_needs_independent_operator_and_host_binding():
    result = resolve_plan(request(surface="web"), verified(
        speech_readiness=S.UNKNOWN, human_terminal=S.UNAVAILABLE,
        local_private_input=S.UNAVAILABLE, operator_binding=S.UNKNOWN, host_key_binding=S.UNKNOWN,
    ))
    remote = next(o for o in result.input_options if o.name == "remote_private_input")
    assert {r.capability for r in remote.requirements} == {C.OPERATOR, C.HOST_KEY}
    assert remote.status == "needs_input"


def test_resume_and_persistence_require_their_own_evidence():
    facts = verified(session_handoff=S.UNKNOWN, durable_storage=S.UNAVAILABLE, persistence_consent=S.UNKNOWN)
    assert resolve_plan(request(surface="web"), facts).status == "ready"
    resume = resolve_plan(request(surface="web", session="resume"), facts)
    assert resume.status == "needs_input"
    assert any(r.capability == C.HANDOFF for r in resume.operations[0].requirements)
    persistent = resolve_plan(request(surface="web", persistence=True), facts)
    assert persistent.status == "failed"
    assert {r.capability for r in persistent.operations[-1].requirements} == {C.STORAGE, C.PERSISTENCE}


def test_generated_identity_audio_and_lifetime_combinations():
    dimensions = (C.LOGIN, C.HOST_BINDING, C.HTTPS, C.MEDIA, C.LIFETIME)
    for values in product(S, repeat=len(dimensions)):
        facts = verified(**dict(zip(dimensions, values)))
        result = resolve_plan(request(surface="web"), facts)
        assert (result.status == "ready") == all(value == S.AVAILABLE for value in values)
        start = next(o for o in result.operations if o.name == "start_audio")
        assert {r.capability for r in start.requirements} == {
            key for key, value in zip(dimensions, values) if value != S.AVAILABLE
        }
        assert (result.status == "failed") == any(value == S.UNAVAILABLE for value in values)


def test_only_operations_depending_on_changed_capability_change():
    baseline = resolve_plan(request(surface="web"), verified())
    changed = resolve_plan(request(surface="web"), verified(audio_devices=S.UNAVAILABLE))
    assert baseline.operations == changed.operations
    assert baseline.audio_options[1:] == changed.audio_options[1:]
    assert baseline.audio_options[0] != changed.audio_options[0]


def test_cli_schema_is_generated_without_discovery(monkeypatch, capsys):
    from sleeptalk.cli import main
    monkeypatch.setattr("sleeptalk.onboarding.discover", lambda *_: pytest.fail("probe on --schema"))
    assert main(["host", "discover", "--schema", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == contract_schema()


def test_cli_prints_one_valid_receipt(tmp_path, capsys):
    from sleeptalk.cli import main
    assert main(["host", "discover", "--workspace", str(tmp_path), "--agent", "codex", "--surface", "web", "--json"]) == 2
    output = capsys.readouterr()
    result = DiscoveryReceipt.model_validate_json(output.out)
    assert result.selected_agent == "codex"
    assert result.plan.surface == "web"
    assert len(output.out.splitlines()) == 1
    assert str(tmp_path) not in output.out + output.err


@pytest.mark.parametrize("option", ["--workspace", "--agent"])
def test_empty_intent_is_rejected_instead_of_using_cwd(option, capsys):
    from sleeptalk.cli import main
    assert main(["host", "discover", option, ""]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_persistence_feasibility_request_does_not_establish_consent():
    result = discover(Intent(persistence=True), Context(claims={C.PERSISTENCE: S.AVAILABLE}))
    fact = next(f for f in result.facts if f.capability == C.PERSISTENCE)
    assert fact.state == S.UNKNOWN
    assert fact.claim == S.AVAILABLE


@pytest.mark.parametrize("payload", [
    '{"api_key":"sentinel-private-value"}',
    '{"claims":{"operator_binding":"sentinel-private-value"}}',
    '{"schema_version":2}', 'sentinel-invalid-json', " " * 32769,
])
def test_cli_rejects_unsafe_or_invalid_context_without_echo(tmp_path, capsys, payload):
    from sleeptalk.cli import main
    context = tmp_path / "context.json"
    context.write_text(payload)
    assert main(["host", "discover", "--context", str(context)]) == 2
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["status"] == "error"
    DiscoveryErrorReceipt.model_validate(result)
    assert "sentinel" not in output.out + output.err
    assert str(context) not in output.out + output.err
