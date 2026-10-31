"""Unit tests for sleeptalk host ensure / start-voice receipts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sleeptalk import setup_core
from sleeptalk import call_registry, host, receipts
from sleeptalk.caller_context import (
    CallerKind,
    VoiceDelivery,
    delivery_for,
    launch_plan_for,
    resolve_caller_context,
    surface_for,
)
from sleeptalk.call_provider import CallProviderError
from sleeptalk.doctor import DoctorReport, Rung, FAIL, OK


def _write_web_profile(
    path: Path, *, agent: str = "pi", route: str = "cascade"
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "[agent]",
                f'type = "{agent}"',
                'name = "Host Test"',
                f'slug = "{path.stem}"',
                'workspace_dir = "."',
                "",
                "[transport]",
                'type = "web"',
                "",
                "[route]",
                f'type = "{route}"',
                *(['provider = "openai-realtime"'] if route == "hybrid" else []),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _write_standalone_config(tmp_path: Path, name: str = "default.toml") -> Path:
    """Full-document config outside a profiles/ overlay layout."""
    return _write_web_profile(tmp_path / name)


def test_profile_argument_accepts_slug_or_toml_filename(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path))

    slug_path = host.resolve_profile_path(profile="maxwell")
    filename_path = host.resolve_profile_path(profile="maxwell.toml")

    assert slug_path == filename_path == tmp_path / "profiles" / "maxwell.toml"


def test_ensure_needs_input_when_profile_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    receipt = host.ensure(
        profile="missing", surface="web", route="cascade", agent="pi", env={}
    )
    assert receipt["schema_version"] == 1
    assert receipt["status"] == receipts.STATUS_NEEDS_INPUT
    assert receipt["missing"] == ["stt_key", "tts_key"]
    assert "join_url" not in receipt
    assert "url" not in receipt


def test_ensure_defaults_to_supported_cascade_route(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("sleeptalk.setup_core.detect_agents", lambda: {})

    receipt = host.ensure(profile="missing")

    assert receipt["status"] == receipts.STATUS_NEEDS_INPUT
    assert receipt["route"] == "cascade"


def test_ensure_rejects_profile_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))

    receipt = host.ensure(profile="../../outside", agent="pi")

    assert receipt["status"] == receipts.STATUS_FAILED
    assert receipt["code"] == "config"
    assert not (tmp_path / "outside.toml").exists()


def test_ensure_provisions_missing_profile_from_answers(tmp_path, monkeypatch):
    home = tmp_path / "home"
    answers = tmp_path / "answers.toml"
    answers.write_text(
        "\n".join(
            [
                'name = "Local Host"',
                'stt_key = "deepgram-secret"',
                'tts_key = "cartesia-secret"',
                'tts_voice_id = "voice-1"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SLEEPTALK_HOME", str(home))

    async def fake_validate(_answers, *, live=True):
        return []

    async def fake_doctor(path, *, env=None, live=True, surface=None):
        return DoctorReport(rungs=[Rung("ready", OK)])

    monkeypatch.setattr("sleeptalk.setup_core.validate_answers", fake_validate)
    monkeypatch.setattr(host, "run_doctor", fake_doctor)

    receipt = host.ensure(
        profile="default",
        surface="web",
        route="cascade",
        agent="pi",
        answers=str(answers),
    )

    profile = home / "profiles" / "default.toml"
    assert receipt["status"] == receipts.STATUS_READY
    assert profile.is_file()
    assert 'type = "web"' in profile.read_text(encoding="utf-8")
    assert 'type = "cascade"' in profile.read_text(encoding="utf-8")
    assert 'type = "pi"' in profile.read_text(encoding="utf-8")
    assert f'workspace_dir = "{Path.cwd().resolve()}"' in profile.read_text(
        encoding="utf-8"
    )
    # Keys go to the private store; the profile references them and no
    # plain-text copy is written.
    from sleeptalk.wizard import _stored_secrets

    stored = _stored_secrets(profile)
    assert stored["DEEPGRAM_API_KEY"] == "deepgram-secret"
    assert stored["CARTESIA_API_KEY"] == "cartesia-secret"
    assert not (home / ".env").exists()
    assert "deepgram-secret" not in profile.read_text(encoding="utf-8")
    assert "deepgram-secret" not in json.dumps(receipt)
    assert "cartesia-secret" not in json.dumps(receipt)


def test_ensure_auto_selects_the_only_detected_agent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    answers = tmp_path / "answers.toml"
    answers.write_text(
        'stt_key = "dg"\ntts_key = "tts"\ntts_voice_id = "voice-1"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SLEEPTALK_HOME", str(home))
    monkeypatch.setattr("sleeptalk.setup_core.detect_agents", lambda: {"codex": "codex"})

    async def fake_validate(_answers, *, live=True):
        return []

    async def fake_doctor(path, *, env=None, live=True, surface=None):
        return DoctorReport(rungs=[Rung("ready", OK)])

    monkeypatch.setattr("sleeptalk.setup_core.validate_answers", fake_validate)
    monkeypatch.setattr(host, "run_doctor", fake_doctor)

    receipt = host.ensure(profile="default", answers=str(answers))

    assert receipt["status"] == receipts.STATUS_READY
    assert receipt["agent"] == "codex"
    profile_text = (home / "profiles" / "default.toml").read_text(encoding="utf-8")
    assert 'type = "codex"' in profile_text


def test_ensure_auto_requires_choice_when_detection_is_ambiguous(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SLEEPTALK_HOME", str(home))
    monkeypatch.setattr(
        "sleeptalk.setup_core.detect_agents", lambda: {"codex": "codex", "pi": "pi"}
    )

    receipt = host.ensure(profile="default")

    assert receipt["status"] == receipts.STATUS_NEEDS_INPUT
    assert receipt["missing"] == ["agent"]
    assert not (home / "profiles" / "default.toml").exists()


def test_ensure_repeated_answers_do_not_rewrite_local_state(tmp_path, monkeypatch):
    home = tmp_path / "home"
    answers = tmp_path / "answers.toml"
    answers.write_text(
        'stt_key = "dg"\ntts_key = "tts"\ntts_voice_id = "voice-1"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SLEEPTALK_HOME", str(home))

    async def fake_validate(_answers, *, live=True):
        return []

    async def fake_doctor(path, *, env=None, live=True, surface=None):
        return DoctorReport(rungs=[Rung("ready", OK)])

    monkeypatch.setattr("sleeptalk.setup_core.validate_answers", fake_validate)
    monkeypatch.setattr(host, "run_doctor", fake_doctor)

    first = host.ensure(profile="default", agent="pi", answers=str(answers))
    profile = home / "profiles" / "default.toml"
    before = profile.read_bytes()
    before_mtime = profile.stat().st_mtime_ns
    second = host.ensure(profile="default", agent="pi", answers=str(answers))

    assert first["status"] == second["status"] == receipts.STATUS_READY
    assert profile.read_bytes() == before
    assert profile.stat().st_mtime_ns == before_mtime
    env_path = home / ".env"
    assert not env_path.exists() or "dg" not in env_path.read_text(encoding="utf-8").split("=")


def test_ensure_existing_profile_conflict_is_explicit(tmp_path, monkeypatch):
    profile = _write_web_profile(tmp_path / "default.toml", route="hybrid")
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))

    receipt = host.ensure(config=str(profile), route="cascade")

    assert receipt["status"] == receipts.STATUS_CONFLICT
    assert receipt["conflicts"] == ["route"]


def test_ensure_treats_surface_as_an_invocation_not_profile_identity(
    tmp_path, monkeypatch
):
    profile = _write_web_profile(tmp_path / "default.toml")
    observed: list[str | None] = []

    async def fake_doctor(path, *, env=None, live=True, surface=None):
        observed.append(surface)
        return DoctorReport(rungs=[Rung("ready", OK)])

    monkeypatch.setattr(host, "run_doctor", fake_doctor)

    receipt = host.ensure(config=str(profile), surface="terminal")

    assert receipt["status"] == receipts.STATUS_READY
    assert receipt["surface"] == "terminal"
    assert observed == ["terminal"]
    assert 'type = "web"' in profile.read_text(encoding="utf-8")


def test_ensure_repairs_effective_surface_dependencies_for_existing_profile(
    tmp_path, monkeypatch
):

    profile = _write_web_profile(tmp_path / "default.toml")
    repaired: list[str] = []

    async def fake_doctor(path, *, env=None, live=True, surface=None):
        return DoctorReport(rungs=[Rung("ready", OK)])

    monkeypatch.setattr(host, "run_doctor", fake_doctor)
    monkeypatch.setattr(
        setup_core,
        "missing_runtime_extra",
        lambda surface: "terminal extra missing" if not repaired else None,
    )
    monkeypatch.setattr(
        setup_core,
        "ensure_runtime_extra",
        lambda surface: repaired.append(surface) or None,
    )
    monkeypatch.setattr(
        "sleeptalk.sysdeps.ensure_required_for_surface",
        lambda surface, *, repair: [],
    )

    receipt = host.ensure(config=str(profile), surface="terminal", live=True)

    assert receipt["status"] == receipts.STATUS_READY
    assert repaired == ["terminal"]


def test_ensure_repairs_secrets_without_rewriting_profile(tmp_path, monkeypatch):
    profile = _write_web_profile(tmp_path / "default.toml")
    env_path = tmp_path / ".env"
    env_path.write_text("UNRELATED=keep\n", encoding="utf-8")
    answers = tmp_path / "answers.toml"
    answers.write_text('stt_key = "dg"\ntts_key = "tts"\n', encoding="utf-8")
    before = profile.read_bytes()

    async def fake_doctor(path, *, env=None, live=True, surface=None):
        return DoctorReport(rungs=[Rung("ready", OK)])

    monkeypatch.setattr(host, "run_doctor", fake_doctor)

    receipt = host.ensure(config=str(profile), answers=str(answers))

    assert receipt["status"] == receipts.STATUS_READY
    # The profile gains only references; the keys go to the private store.
    after = profile.read_text(encoding="utf-8")
    assert after.startswith(before.decode("utf-8").rstrip("\n"))
    assert "[credentials.refs]" in after and "dg" not in after.split("[credentials.refs]")[1].split("\"")[::2]
    from sleeptalk.wizard import _stored_secrets

    assert _stored_secrets(profile) == {"DEEPGRAM_API_KEY": "dg", "CARTESIA_API_KEY": "tts"}
    assert env_path.read_text(encoding="utf-8").splitlines() == ["UNRELATED=keep"]


def test_ensure_ready_converges_and_never_mints(tmp_path, monkeypatch):
    profile = _write_standalone_config(tmp_path)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))

    calls: list[object] = []

    async def fake_doctor(path, *, env=None, live=True, surface=None):
        calls.append(("doctor", str(path), live))
        return DoctorReport(rungs=[Rung("env vars present", OK)])

    def boom_launch(*_a, **_k):
        raise AssertionError("ensure must never mint a call")

    monkeypatch.setattr(host, "run_doctor", fake_doctor)
    monkeypatch.setattr(host, "launch_detached_call", boom_launch)

    first = host.ensure(profile="default", config=str(profile), live=False)
    second = host.ensure(profile="default", config=str(profile), live=False)

    assert first["status"] == receipts.STATUS_READY
    assert second["status"] == receipts.STATUS_READY
    assert first["schema_version"] == second["schema_version"] == 1
    assert "join_url" not in first and "join_url" not in second
    assert "url" not in first and "url" not in second
    assert first["agent"] == "pi"
    assert all(live is False for _n, _p, live in calls)


def test_ensure_needs_input_names_missing_keys_without_values(tmp_path, monkeypatch):
    profile = _write_standalone_config(tmp_path, "web.toml")
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))

    async def fake_doctor(path, *, env=None, live=True, surface=None):
        return DoctorReport(
            rungs=[
                Rung(
                    "env vars present",
                    FAIL,
                    [
                        "OPENAI_API_KEY missing — set it to an OpenAI API key",
                        "DEEPGRAM_API_KEY missing — set DEEPGRAM_API_KEY=sk-leak-should-not-appear",
                    ],
                )
            ]
        )

    monkeypatch.setattr(host, "run_doctor", fake_doctor)
    receipt = host.ensure(config=str(profile), live=False)
    assert receipt["status"] == receipts.STATUS_NEEDS_INPUT
    assert receipt["missing"] == ["env_vars"]
    blob = json.dumps(receipt)
    assert "sk-leak-should-not-appear" not in blob
    assert "set it to an OpenAI API key" not in blob


def test_start_voice_web_receipt(monkeypatch, tmp_path):
    profile = _write_standalone_config(tmp_path)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))

    def fake_launch(request):
        assert request.config_path.endswith("default.toml")
        assert request.echo is False
        return "https://example.test/call/abc#fragment-secret"

    monkeypatch.setattr(host, "launch_detached_call", fake_launch)
    receipt = host.start_voice_web(config=str(profile), ttl_seconds=120)
    assert receipt["status"] == receipts.STATUS_STARTED
    assert receipt["schema_version"] == 1
    assert receipt["join_url"].startswith("https://example.test/call/")
    assert receipt["expires_at"].endswith("Z")
    assert receipt["agent"]["type"] == "pi"
    assert receipt["agent"]["slug"] == "default"
    assert receipt["continuity"] in {"new", "continues", "reset"}
    assert receipt["callId"]
    assert "pid" not in receipt
    assert "config_path" not in receipt
    # The receipt says which way the call travels, read from the link itself.
    assert receipt["delivery"] == "hosted_relay" and "hosted relay" in receipt["say"]


def test_start_voice_receipt_names_a_tailnet_call_by_its_link(monkeypatch, tmp_path):
    """The VPS agent called a relay link a tailnet call (2026-09-22); the receipt
    now states the delivery so the agent has nothing to infer."""
    profile = _write_standalone_config(tmp_path)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(host, "launch_detached_call", lambda request: "https://ubuntu.tail1234.ts.net:8443/#/call/abc/def")
    receipt = host.start_voice_web(config=str(profile), ttl_seconds=120)
    assert receipt["delivery"] == "tailnet" and "no time limit" in receipt["say"]
    monkeypatch.setattr(host, "launch_detached_call", lambda request: "http://127.0.0.1:5123/#/call/abc/def")
    assert host.start_voice_web(config=str(profile), ttl_seconds=120)["delivery"] == "local"


def test_ensure_stores_the_tailnet_choice_on_an_existing_profile_and_asks_otherwise(tmp_path, monkeypatch):
    profile = _write_standalone_config(tmp_path)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))

    async def available(*, environ=None):
        return {"available": True, "name": "ubuntu.tail1234.ts.net"}

    async def fake_doctor(path, *, env=None, live=True, surface=None):
        return DoctorReport(rungs=[Rung("env vars present", OK)])

    monkeypatch.setattr("sleeptalk.tailnet.detect", available)
    monkeypatch.setattr(host, "run_doctor", fake_doctor)
    first = host.ensure(config=str(profile), live=False)
    assert first["status"] == receipts.STATUS_READY
    assert first["tailnet"]["available"] is True and first["tailnet"]["selected"] is False
    assert "rerun this command with --tailnet" in first["tailnet"]["ask"] and ".ts.net" in first["tailnet"]["ask"]
    chosen = host.ensure(config=str(profile), live=False, tailnet=True)
    assert chosen["tailnet"]["selected"] is True and "ask" not in chosen["tailnet"]
    assert 'delivery = "tailnet"' in profile.read_text()
    # The choice is remembered: the next plain ensure still reports it selected.
    again = host.ensure(config=str(profile), live=False)
    assert again["tailnet"]["selected"] is True and "ask" not in again["tailnet"]


def test_ensure_no_tailnet_removes_the_stored_choice(tmp_path, monkeypatch):
    profile = _write_standalone_config(tmp_path)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))

    async def available(*, environ=None):
        return {"available": True, "name": "ubuntu.tail1234.ts.net"}

    async def fake_doctor(path, *, env=None, live=True, surface=None):
        return DoctorReport(rungs=[Rung("env vars present", OK)])

    monkeypatch.setattr("sleeptalk.tailnet.detect", available)
    monkeypatch.setattr(host, "run_doctor", fake_doctor)
    host.ensure(config=str(profile), live=False, tailnet=True)
    assert 'delivery = "tailnet"' in profile.read_text()
    cleared = host.ensure(config=str(profile), live=False, no_tailnet=True)
    assert cleared["tailnet"]["selected"] is False and "ask" not in cleared["tailnet"]
    assert 'delivery = "tailnet"' not in profile.read_text()
    # Later calls stay off the tailnet; a plain ensure may offer it again.
    assert host.ensure(config=str(profile), live=False)["tailnet"]["selected"] is False


def test_caller_context_defaults_to_hosted_web_for_agent_commands():
    context = resolve_caller_context(explicit=None, env={})

    assert context.kind is CallerKind.CHAT
    assert delivery_for(context) is VoiceDelivery.REMOTE_WEB
    assert surface_for(context) == "web"


@pytest.mark.parametrize(
    "channel",
    ["chat", "telegram", "wechat", "slack", "whatsapp", "imessage", "future-chat"],
)
def test_any_chat_context_uses_hosted_web(channel):
    context = resolve_caller_context(
        explicit=None,
        env={"SLEEPTALK_CALLER_CONTEXT": channel},
    )

    assert delivery_for(context) is VoiceDelivery.REMOTE_WEB
    assert surface_for(context) == "web"


def test_explicit_caller_context_wins_over_environment():
    context = resolve_caller_context(
        explicit="terminal",
        env={"SLEEPTALK_CALLER_CONTEXT": "telegram"},
    )

    assert context.kind is CallerKind.LOCAL
    assert delivery_for(context) is VoiceDelivery.TERMINAL


@pytest.mark.parametrize("channel", ["local", "terminal", "desktop"])
def test_local_caller_aliases_stay_local(channel):
    context = resolve_caller_context(explicit=channel, env={})

    assert context.kind is CallerKind.LOCAL
    assert delivery_for(context) is VoiceDelivery.TERMINAL


def test_explicit_web_resolves_to_plain_local_browser() -> None:
    caller = resolve_caller_context(explicit=None, env={})

    plan = launch_plan_for(
        surface="web",
        caller=caller,
        rendezvous_override=None,
        json_requested=False,
    )

    assert plan.delivery is VoiceDelivery.LOCAL_WEB
    assert plan.structured is False


def test_agent_default_resolves_to_structured_remote_browser() -> None:
    caller = resolve_caller_context(explicit=None, env={})

    plan = launch_plan_for(
        surface=None,
        caller=caller,
        rendezvous_override=None,
        json_requested=False,
    )

    assert plan.delivery is VoiceDelivery.REMOTE_WEB
    assert plan.structured is True


def test_named_chat_context_keeps_explicit_web_remote() -> None:
    caller = resolve_caller_context(explicit="telegram", env={})

    plan = launch_plan_for(
        surface="web",
        caller=caller,
        rendezvous_override=None,
        json_requested=False,
    )

    assert plan.delivery is VoiceDelivery.REMOTE_WEB
    assert plan.structured is True


def test_explicit_rendezvous_overrides_local_web_default() -> None:
    caller = resolve_caller_context(explicit=None, env={})

    plan = launch_plan_for(
        surface="web",
        caller=caller,
        rendezvous_override="https://calls.example.test",
        json_requested=False,
    )

    assert plan.delivery is VoiceDelivery.REMOTE_WEB
    assert plan.structured is False


def test_cli_accepts_an_arbitrary_chat_context(monkeypatch, capsys):
    from sleeptalk import cli

    monkeypatch.setattr(
        host,
        "start_voice_web",
        lambda **_kwargs: {
            "schema_version": 1,
            "status": receipts.STATUS_STARTED,
            "operation": receipts.OPERATION_START_VOICE,
            "surface": "web",
            "join_url": "https://example.test/call/abc#secret",
        },
    )
    monkeypatch.setattr(
        cli,
        "_run_local_profile",
        lambda *_args, **_kwargs: pytest.fail("local voice must not start"),
    )

    code = cli.main(
        [
            "host",
            "start-voice",
            "--profile",
            "default",
            "--caller-context",
            "future-chat-surface",
        ]
    )

    assert code == cli.EXIT_OK
    assert '"surface":"web"' in capsys.readouterr().out


def test_cli_host_start_voice_routes_telegram_to_hosted_link(monkeypatch, capsys):
    from sleeptalk import cli

    monkeypatch.setenv("SLEEPTALK_CALLER_CONTEXT", "telegram")
    monkeypatch.setattr(
        host,
        "start_voice_web",
        lambda **_kwargs: {
            "schema_version": 1,
            "status": receipts.STATUS_STARTED,
            "operation": receipts.OPERATION_START_VOICE,
            "surface": "web",
            "join_url": "https://example.test/call/abc#secret",
        },
    )
    monkeypatch.setattr(
        cli,
        "_run_local_profile",
        lambda *_args, **_kwargs: pytest.fail("local voice must not start"),
    )

    code = cli.main(["host", "start-voice", "--profile", "default"])

    assert code == cli.EXIT_OK
    assert '"join_url":"https://example.test/call/abc#secret"' in capsys.readouterr().out


def test_cli_host_start_voice_busy_is_one_receipt_and_exit_2(monkeypatch, capsys):
    from sleeptalk import cli

    monkeypatch.setenv("SLEEPTALK_CALLER_CONTEXT", "chat")
    monkeypatch.setattr(
        host,
        "start_voice_web",
        lambda **_kwargs: receipts.error_receipt(
            operation=receipts.OPERATION_START_VOICE,
            code="busy",
            message="Profile 'default' already has a live call.",
        ),
    )
    monkeypatch.setattr(
        cli,
        "_run_local_profile",
        lambda *_args, **_kwargs: pytest.fail("busy hosted start must not run locally"),
    )

    assert cli.main(["host", "start-voice", "--profile", "default"]) == 2
    output = capsys.readouterr().out.splitlines()
    assert len(output) == 1
    assert json.loads(output[0])["status"] == receipts.STATUS_ERROR


def test_cli_host_start_voice_defaults_to_hosted_web(monkeypatch, capsys):
    from sleeptalk import cli

    monkeypatch.delenv("SLEEPTALK_CALLER_CONTEXT", raising=False)
    calls = []
    monkeypatch.setattr(
        cli,
        "_run_local_profile",
        lambda **_kwargs: pytest.fail("agent-facing start must default to Web"),
    )
    monkeypatch.setattr(
        host,
        "start_voice_web",
        lambda **kwargs: calls.append(kwargs)
        or {
            "schema_version": 1,
            "status": receipts.STATUS_STARTED,
            "operation": receipts.OPERATION_START_VOICE,
            "surface": "web",
            "join_url": "https://example.test/call/abc#secret",
        },
    )

    code = cli.main(["host", "start-voice", "--profile", "maxwell"])

    assert code == cli.EXIT_OK
    assert calls[0]["profile"] == "maxwell"
    assert '"surface":"web"' in capsys.readouterr().out


def test_cli_host_start_voice_explicit_terminal_stays_local(monkeypatch):
    from sleeptalk import cli

    calls = []
    monkeypatch.setattr(
        cli,
        "_run_local_profile",
        lambda *, profile, config: calls.append((profile, config)) or cli.EXIT_OK,
    )
    monkeypatch.setattr(
        host,
        "start_voice_web",
        lambda **_kwargs: pytest.fail("explicit Terminal must not start Web"),
    )

    code = cli.main(["host", "start-voice", "terminal", "--profile", "maxwell"])

    assert code == cli.EXIT_OK
    assert calls == [("maxwell", None)]


def test_cli_host_start_voice_explicit_web_prints_local_url(monkeypatch, capsys):
    from sleeptalk import cli

    calls = []
    monkeypatch.delenv("SLEEPTALK_CALLER_CONTEXT", raising=False)
    monkeypatch.setattr(
        host,
        "start_web_voice",
        lambda **kwargs: calls.append(kwargs)
        or SimpleNamespace(join_url="http://127.0.0.1:49183/#/call/id/token"),
    )

    code = cli.main(["host", "start-voice", "web", "--profile", "maxwell"])

    assert code == cli.EXIT_OK
    assert calls[0]["local"] is True
    assert capsys.readouterr().out == "http://127.0.0.1:49183/#/call/id/token\n"


def test_cli_host_start_voice_explicit_web_json_is_opt_in(monkeypatch, capsys):
    from sleeptalk import cli

    calls = []
    monkeypatch.delenv("SLEEPTALK_CALLER_CONTEXT", raising=False)
    monkeypatch.setattr(
        host,
        "start_voice_web",
        lambda **kwargs: calls.append(kwargs)
        or {
            "schema_version": 1,
            "status": receipts.STATUS_STARTED,
            "operation": receipts.OPERATION_START_VOICE,
            "surface": "web",
            "join_url": "http://127.0.0.1:49183/#/call/id/token",
        },
    )

    code = cli.main(
        ["host", "start-voice", "web", "--profile", "maxwell", "--json"]
    )

    assert code == cli.EXIT_OK
    assert calls[0]["local"] is True
    assert json.loads(capsys.readouterr().out)["join_url"].startswith(
        "http://127.0.0.1:"
    )


def test_local_start_uses_terminal_without_rewriting_profile(monkeypatch, tmp_path):
    from sleeptalk import cli

    monkeypatch.setenv("CARTESIA_API_KEY", "test-cartesia-key")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-deepgram-key")
    profile = _write_standalone_config(tmp_path)
    monkeypatch.setattr("sleeptalk.registries.locate_binary", lambda binary, fallbacks=(): f"/usr/bin/{binary}")
    before = profile.read_text(encoding="utf-8")
    observed = []
    monkeypatch.setattr(
        cli,
        "_run_loaded_config",
        lambda path, config, *, skip_checks: observed.append(
            (path, config.transport.type, skip_checks)
        )
        or cli.EXIT_OK,
    )

    code = cli._run_local_profile(profile="default", config=str(profile))

    assert code == cli.EXIT_OK
    assert observed == [(profile, "terminal", False)]
    assert profile.read_text(encoding="utf-8") == before


def test_local_start_validates_the_terminal_invocation_not_saved_discord(
    monkeypatch, tmp_path
):
    from sleeptalk import cli

    monkeypatch.setenv("CARTESIA_API_KEY", "test-cartesia-key")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-deepgram-key")
    profile = _write_standalone_config(tmp_path)
    monkeypatch.setattr("sleeptalk.registries.locate_binary", lambda binary, fallbacks=(): f"/usr/bin/{binary}")
    original = profile.read_text(encoding="utf-8").replace(
        'type = "web"', 'type = "discord"'
    )
    profile.write_text(original, encoding="utf-8")
    observed = []
    monkeypatch.setattr(
        cli,
        "_run_loaded_config",
        lambda path, config, *, skip_checks: observed.append(config.transport.type)
        or cli.EXIT_OK,
    )
    monkeypatch.setattr(
        "sleeptalk.sysdeps.missing_terminal_extra", lambda: None
    )

    code = cli._run_local_profile(profile="default", config=str(profile))

    assert code == cli.EXIT_OK
    assert observed == ["terminal"]
    assert profile.read_text(encoding="utf-8") == original


def test_live_profile_run_holds_the_same_lock_as_detached_links(
    monkeypatch, tmp_path, capsys
):
    from sleeptalk import cli

    home = tmp_path / "home"
    profile = tmp_path / "alice.toml"
    monkeypatch.setenv("SLEEPTALK_HOME", str(home))
    identity = call_registry.ProfileIdentity.from_config(
        config_path=str(profile), profile="alice"
    )
    lease = call_registry.acquire_profile_lease(
        root=home / "calls", identity=identity
    )
    config = SimpleNamespace(
        agent=SimpleNamespace(slug="alice"),
        log=SimpleNamespace(level="INFO", latency=False, latency_path=""),
    )
    monkeypatch.setattr(
        "sleeptalk.logging_setup.setup_file_logging", lambda **_kwargs: tmp_path / "log"
    )
    monkeypatch.setattr(
        "sleeptalk.logging_setup.setup_latency_log", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        cli,
        "build_session",
        lambda _config: pytest.fail("busy profile must not build another session"),
    )
    try:
        code = cli._run_loaded_config(profile, config, skip_checks=True)
    finally:
        lease.close()

    assert code == cli.EXIT_CONFIG
    assert "already has a live call" in capsys.readouterr().err


def test_start_web_voice_shared_with_link(monkeypatch, tmp_path):
    profile = _write_standalone_config(tmp_path)

    def fake_launch(request):
        return "https://example.test/join#x"

    monkeypatch.setattr(host, "launch_detached_call", fake_launch)
    started = host.start_web_voice(config=str(profile), echo=False)
    assert started.join_url == "https://example.test/join#x"
    assert started.agent_type == "pi"


def test_start_voice_error_receipt(monkeypatch, tmp_path):
    profile = _write_standalone_config(tmp_path)

    def boom(request):
        raise CallProviderError("rendezvous down")

    monkeypatch.setattr(host, "launch_detached_call", boom)
    receipt = host.start_voice_web(config=str(profile))
    assert receipt["status"] == receipts.STATUS_ERROR
    assert receipt["code"] == "provider"
    assert "rendezvous down" in receipt["error"]


def test_start_voice_busy_receipt(monkeypatch, tmp_path):
    profile = _write_standalone_config(tmp_path)

    def busy(_request):
        raise host.DetachedCallBusyError("Profile 'default' already has a live call.")

    monkeypatch.setattr(host, "launch_detached_call", busy)
    receipt = host.start_voice_web(config=str(profile))

    assert receipt["status"] == receipts.STATUS_ERROR
    assert receipt["code"] == "busy"
    assert "already has a live call" in receipt["error"]


def test_start_voice_profile_not_found_has_typed_error(tmp_path):
    receipt = host.start_voice_web(config=str(tmp_path / "missing.toml"))

    assert receipt["status"] == receipts.STATUS_ERROR
    assert receipt["code"] == "profile_not_found"


def test_cli_host_ensure_json(tmp_path, monkeypatch):
    from sleeptalk import cli

    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    code = cli.main(
        [
            "host",
            "ensure",
            "--profile",
            "nope",
            "--surface",
            "web",
            "--route",
            "hybrid",
            "--agent",
            "auto",
            "--json",
        ]
    )
    assert code == cli.EXIT_CONFIG


@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_cli_host_list_prints_one_json_line(monkeypatch, capsys, flags):
    from sleeptalk import cli

    monkeypatch.setattr(
        host,
        "list_voice_calls",
        lambda: {
            "schema_version": 1,
            "status": "listed",
            "operation": "list",
            "calls": [],
        },
    )

    assert cli.main(["host", "list", *flags]) == cli.EXIT_OK
    assert capsys.readouterr().out.splitlines() == [
        '{"calls":[],"operation":"list","schema_version":1,"status":"listed"}'
    ]


def test_cli_host_stop_prints_receipt_and_maps_not_found_to_exit_2(
    monkeypatch, capsys
):
    from sleeptalk import cli

    monkeypatch.setattr(
        host,
        "stop_voice_call",
        lambda call_id: {
            "schema_version": 1,
            "status": receipts.STATUS_ERROR,
            "operation": "stop",
            "code": "not_found",
            "error": f"Call {call_id} was not found.",
        },
    )

    assert cli.main(["host", "stop", "missing-call"]) == cli.EXIT_CONFIG
    output = capsys.readouterr().out.splitlines()
    assert len(output) == 1
    assert json.loads(output[0])["operation"] == "stop"


@pytest.mark.parametrize("argv", [["abc"], ["--call-id", "abc"], ["--callId", "abc"]])
def test_cli_host_stop_takes_the_call_id_either_way(argv, monkeypatch, capsys):
    from sleeptalk import cli

    stopped = []
    monkeypatch.setattr(host, "stop_voice_call", lambda call_id: stopped.append(call_id) or
                        {"schema_version": 1, "status": receipts.STATUS_READY, "operation": "stop"})
    assert cli.main(["host", "stop", *argv]) == cli.EXIT_OK
    assert stopped == ["abc"]


def test_cli_host_stop_without_an_id_says_how(capsys):
    from sleeptalk import cli

    assert cli.main(["host", "stop"]) == cli.EXIT_CONFIG
    assert "sleeptalk host stop <callId>" in capsys.readouterr().err


def test_cli_host_reset_access_replaces_local_credential(tmp_path, monkeypatch, capsys):
    from sleeptalk import cli
    from sleeptalk.service_grant import ServiceGrantStore

    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path))
    origin = "https://call.example.test"
    store = ServiceGrantStore()
    original = store.load_or_create_pending(origin)

    assert (
        cli.main(["host", "reset-access", "--rendezvous", origin])
        == cli.EXIT_OK
    )

    replacement = store.load(origin)
    assert replacement is not None
    assert replacement.installation_id != original.installation_id
    output = capsys.readouterr().out
    assert origin in output
    assert replacement.installation_id not in output
    assert replacement.secret not in output


def test_stop_voice_call_maps_registry_errors_to_codes(monkeypatch):
    monkeypatch.setattr(
        host,
        "stop_call",
        lambda _call_id: (_ for _ in ()).throw(
            host.CallStopTimeoutError("call did not stop")
        ),
    )

    receipt = host.stop_voice_call("safe-call-id")

    assert receipt["status"] == receipts.STATUS_ERROR
    assert receipt["code"] == "timeout"


def test_continuity_label_evidence():
    assert (
        host.continuity_label(
            conversation_id="",
            session_continuity=True,
            agent_type="pi",
            agent_slug="bot",
        )
        == "new"
    )
    assert (
        host.continuity_label(
            conversation_id="pi:bot:new-web-call",
            session_continuity=True,
            agent_type="pi",
            agent_slug="bot",
        )
        == "new"
    )
    assert (
        host.continuity_label(
            conversation_id="sess-1",
            session_continuity=True,
            agent_type="pi",
            agent_slug="bot",
        )
        == "continues"
    )
    assert (
        host.continuity_label(
            conversation_id="sess-1",
            session_continuity=False,
            agent_type="pi",
            agent_slug="bot",
        )
        == "reset"
    )


@pytest.fixture
def recorded_start(tmp_path, monkeypatch):
    from sleeptalk.call_provider import CallContext
    from sleeptalk.detached_call import DetachedCallRequest

    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    profile = _write_standalone_config(tmp_path)
    request = DetachedCallRequest(
        rendezvous_url="https://example.test", context=CallContext("default", "", "terminal-user"),
        ttl_seconds=300, config_path=str(profile), profile="default", request_id="request-0001",
    )
    root = call_registry.registry_root_for_config(profile)
    identity = request.identity()
    lease = call_registry.acquire_profile_lease(root=root, identity=identity)
    try:
        call_registry.reserve_start_request(
            root=root, identity=identity, request_id=request.request_id,
            fingerprint=request.fingerprint(), ttl_seconds=300,
        )
        with call_registry.register_live_call(root=root, identity=identity, call_id=request.call_id):
            call_registry.observe_start_request(
                root=root, identity=identity, request_id=request.request_id,
                stage="link_minted", join_url="https://example.test/#private-join-capability",
            )
            yield request, root
    finally:
        lease.close()


def _replay_host(request, **overrides):
    return host.start_voice_web(**{
        "config": request.config_path, "request_id": request.request_id,
        "rendezvous_url": request.rendezvous_url, **overrides,
    })


def test_host_recovery_preserves_original_expiry_and_call_id(recorded_start):
    from datetime import datetime, timezone

    request, root = recorded_start
    progress = call_registry.get_start_request(root=root, identity=request.identity(), request_id=request.request_id)
    first = _replay_host(request)
    second = _replay_host(request)
    assert first == second
    assert first["status"] == "started"
    assert first["callId"] == request.call_id
    assert first["join_url"] == "https://example.test/#private-join-capability"
    assert first["expires_at"] == datetime.fromtimestamp(progress["expires_at"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert first["audio_acceptance"] == "unknown"
    assert first["observations"][0]["stage"] == "link_minted"


@pytest.mark.parametrize("stage,state", [("link_consumed", "active"), ("ended", "ended"), ("failed", "failed")])
def test_host_returns_progress_for_replayed_attempt_without_available_link(recorded_start, stage, state):
    request, root = recorded_start
    call_registry.observe_start_request(root=root, identity=request.identity(), request_id=request.request_id, stage=stage)
    receipt = _replay_host(request)
    assert receipt["status"] == "observed"
    assert receipt["operation"] == "start_voice"
    assert receipt["call_state"] == state
    assert receipt["callId"] == request.call_id
    assert "join_url" not in receipt
    assert "private-join-capability" not in json.dumps(receipt)


def test_host_recovery_reports_conflicting_options(recorded_start):
    request, _ = recorded_start
    receipt = _replay_host(request, ttl_seconds=120)
    assert receipt["status"] == "conflict"
    assert receipt["conflicts"] == ["request_options"]
    assert "join_url" not in receipt


def test_host_recovery_reports_expiry_without_new_start(recorded_start, monkeypatch):
    request, root = recorded_start
    before = call_registry.get_start_request(root=root, identity=request.identity(), request_id=request.request_id)
    monkeypatch.setattr(call_registry.time, "time", lambda: before["expires_at"] + 1)
    receipt = _replay_host(request)
    assert receipt["status"] == "observed"
    assert receipt["call_state"] == "expired"
    assert receipt["callId"] == request.call_id
    assert "join_url" not in receipt


def test_host_status_never_returns_recovery_capability(recorded_start):
    request, root = recorded_start
    receipt = host.voice_call_status(config=request.config_path, request_id=request.request_id)
    assert receipt["status"] == "observed"
    assert receipt["operation"] == "call_status"
    assert receipt["call_state"] == "active"
    assert "private-join-capability" not in json.dumps(receipt)
    assert request.config_path not in json.dumps(receipt)
    assert list(root.glob("*/requests/*.capability"))


def test_host_status_and_start_handle_damaged_registry_without_leaking_contents(recorded_start):
    request, root = recorded_start
    record = next(root.glob("*/requests/*.json"))
    record.write_text("damaged-secret-state")
    for receipt in [host.voice_call_status(config=request.config_path, request_id=request.request_id), _replay_host(request)]:
        assert receipt["status"] == "error"
        assert receipt["code"] == "internal"
        assert "damaged-secret-state" not in json.dumps(receipt)
        assert str(root) not in json.dumps(receipt)


def test_host_status_unknown_and_invalid_request_are_typed(tmp_path):
    profile = _write_standalone_config(tmp_path)
    assert host.voice_call_status(config=str(profile), request_id="unknown-request")["code"] == "not_found"
    assert host.voice_call_status(config=str(profile), request_id="../bad")["code"] == "config"
    assert host.start_voice_web(config=str(profile), request_id="../bad")["code"] == "config"


def test_cli_request_id_and_status_recover_existing_call(recorded_start, capsys, monkeypatch):
    from sleeptalk import cli

    monkeypatch.delenv("SLEEPTALK_CALLER_CONTEXT", raising=False)
    request, _ = recorded_start
    assert cli.main([
        "host", "start-voice", "--config", request.config_path,
        "--rendezvous", request.rendezvous_url, "--request-id", request.request_id,
    ]) == cli.EXIT_OK
    start = json.loads(capsys.readouterr().out)
    assert start["callId"] == request.call_id
    assert cli.main(["host", "status", "--config", request.config_path, "--request-id", request.request_id]) == cli.EXIT_OK
    progress = json.loads(capsys.readouterr().out)
    assert progress["callId"] == start["callId"]
    assert "join_url" not in progress


def test_cli_conflicting_request_exits_two(recorded_start, capsys, monkeypatch):
    from sleeptalk import cli

    monkeypatch.delenv("SLEEPTALK_CALLER_CONTEXT", raising=False)
    request, _ = recorded_start
    assert cli.main([
        "host", "start-voice", "--config", request.config_path,
        "--rendezvous", request.rendezvous_url, "--request-id", request.request_id,
        "--ttl-seconds", "120",
    ]) == cli.EXIT_CONFIG
    assert json.loads(capsys.readouterr().out)["status"] == "conflict"


def test_cli_terminal_rejects_request_recovery_before_running(monkeypatch, capsys):
    from sleeptalk import cli

    monkeypatch.setattr(cli, "_run_local_profile", lambda **kwargs: pytest.fail("unsupported recovery must not launch"))
    assert cli.main(["host", "start-voice", "terminal", "--request-id", "request-0001"]) == cli.EXIT_CONFIG
    assert json.loads(capsys.readouterr().out)["code"] == "config"



def test_host_status_handles_corrupt_timestamp_as_registry_failure(recorded_start):
    request, root = recorded_start
    path = next(root.glob("*/requests/*.json"))
    value = json.loads(path.read_text())
    value["observations"]["link_minted"] = 1e100
    path.write_text(json.dumps(value))
    assert host.voice_call_status(config=request.config_path, request_id=request.request_id)["code"] == "internal"
    assert _replay_host(request)["code"] == "internal"



def test_host_suppresses_url_when_call_ends_before_receipt(recorded_start, monkeypatch):
    request, root = recorded_start
    def ended_while_returning(request):
        call_registry.observe_start_request(root=root, identity=request.identity(), request_id=request.request_id, stage="ended")
        return "https://example.test/#private-join-capability"
    monkeypatch.setattr(host, "launch_detached_call", ended_while_returning)
    receipt = _replay_host(request)
    assert receipt["status"] == "observed"
    assert receipt["call_state"] == "ended"
    assert "private-join-capability" not in json.dumps(receipt)



def test_replay_rejects_replaced_profile_instead_of_reporting_new_agent(recorded_start):
    request, _ = recorded_start
    profile = Path(request.config_path)
    profile.write_text(profile.read_text().replace('name = "Host Test"', 'name = "Replacement Agent"'))
    receipt = _replay_host(request)
    assert receipt["status"] == "conflict"
    assert receipt["conflicts"] == ["request_options"]
    assert "Replacement Agent" not in json.dumps(receipt)
    assert "join_url" not in receipt


def test_host_fingerprint_and_receipt_use_same_config_snapshot(tmp_path, monkeypatch):
    from sleeptalk.detached_call import profile_snapshot

    path = _write_standalone_config(tmp_path)
    read_profile = host._read_profile_config
    original = read_profile(path)
    expected = profile_snapshot(original)
    def replace_after_read(path):
        path.write_text(path.read_text().replace('name = "Host Test"', 'name = "Changed During Read"'))
        return original
    def inspect_request(request):
        assert request.profile_snapshot == expected
        assert request.profile_snapshot != profile_snapshot(read_profile(path))
        raise CallProviderError("test boundary")
    monkeypatch.setattr(host, "_read_profile_config", replace_after_read)
    monkeypatch.setattr(host, "launch_detached_call", inspect_request)
    assert host.start_voice_web(config=str(path), request_id="snapshot-race")["status"] == "error"


@pytest.mark.parametrize("status, expected", [("ready", 0), ("failed", 2)])
def test_phone_json_returns_detached_receipt(monkeypatch, capsys, status, expected):
    from sleeptalk import cli, remote_settings

    receipt = {"status": status, "operation": "settings_link"}

    def start(*, profile):
        assert profile == "maxwell"
        return receipt

    monkeypatch.setattr(remote_settings, "start_detached", start)
    monkeypatch.setattr(remote_settings, "run_link", lambda **kwargs: pytest.fail("must detach"))
    assert cli.main(["settings", "--phone", "--json", "--profile", "maxwell"]) == expected
    assert json.loads(capsys.readouterr().out) == receipt
