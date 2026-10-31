"""The tailnet driver: status, prerequisites, publish and unpublish through a
stand-in `tailscale` CLI. Nothing here talks to Tailscale."""
from __future__ import annotations

import json
from pathlib import Path
import stat

import pytest

from sleeptalk import tailnet


def fake_tailscale(tmp_path: Path, *, state: str = "Running", dns: str = "vps.tail1234.ts.net.",
                   certs: list[str] | None = None, serve_exit: int = 0, serve_err: str = "",
                   serve_status: dict | None = None) -> Path:
    log = tmp_path / "ts-calls.jsonl"
    status = {"BackendState": state, "Self": {"DNSName": dns, "TailscaleIPs": ["100.64.0.7"]},
              "CertDomains": ["vps.tail1234.ts.net"] if certs is None else certs}
    script = tmp_path / "bin" / "tailscale"
    script.parent.mkdir(exist_ok=True)
    script.write_text(f'''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
with open({str(log)!r}, "a") as f:
    f.write(json.dumps(args) + "\\n")
if args[:2] == ["status", "--json"]:
    print({json.dumps(status)!r}, end=""); sys.exit(0)
if args[:3] == ["serve", "status", "--json"]:
    print({json.dumps(serve_status or {})!r}, end=""); sys.exit(0)
if args[:1] == ["serve"]:
    sys.stderr.write({serve_err!r}); sys.exit({serve_exit})
sys.exit(2)
''')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))


def calls(tmp_path: Path) -> list[list[str]]:
    path = tmp_path / "ts-calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def test_prepare_returns_the_tailnet_origin_on_a_dedicated_port(tmp_path):
    fake_tailscale(tmp_path)
    binary, origin = await tailnet.prepare(environ={"PATH": str(tmp_path / "bin")})
    assert binary.endswith("/tailscale")
    assert origin == "https://vps.tail1234.ts.net:8443"
    assert tailnet.TailnetStatus("h.ts.net", True, True).origin(443) == "https://h.ts.net"


async def test_publish_and_unpublish_use_serve_for_the_loopback_port(tmp_path):
    binary = str(fake_tailscale(tmp_path))
    await tailnet.publish(binary, 54321)
    await tailnet.unpublish(binary)
    assert calls(tmp_path) == [
        ["serve", "--bg", "--yes", "--https=8443", "http://127.0.0.1:54321"],
        ["serve", "--https=8443", "off"],
    ]


async def test_published_provider_clears_a_stale_entry_then_publishes_and_removes(tmp_path):
    fake_tailscale(tmp_path)
    async with tailnet.published_provider(environ={"PATH": str(tmp_path / "bin")}) as provider:
        assert provider.public_origin == "https://vps.tail1234.ts.net:8443"
        port = provider.port
    assert [c[:3] for c in calls(tmp_path)] == [
        ["status", "--json"],
        ["serve", "--https=8443", "off"],
        ["serve", "--bg", "--yes"],
        ["serve", "--https=8443", "off"],
    ]
    assert calls(tmp_path)[2][-1] == f"http://127.0.0.1:{port}"


def test_missing_binary_is_a_named_failure(tmp_path):
    with pytest.raises(tailnet.TailnetError) as failure:
        tailnet.require({"PATH": str(tmp_path / "nowhere")})
    assert failure.value.code == "tailscale_missing"


async def test_not_connected_is_a_named_failure(tmp_path):
    binary = str(fake_tailscale(tmp_path, state="NeedsLogin", dns=""))
    with pytest.raises(tailnet.TailnetError) as failure:
        await tailnet.status(binary)
    assert failure.value.code == "tailscale_not_running"


async def test_https_disabled_points_at_the_admin_console(tmp_path):
    binary = str(fake_tailscale(tmp_path, certs=[]))
    state = await tailnet.status(binary)
    assert state.https_enabled is False
    with pytest.raises(tailnet.TailnetError) as failure:
        tailnet.require_https(state)
    assert failure.value.code == "tailscale_https_disabled" and tailnet.ADMIN_DNS_URL in str(failure.value)


async def test_serve_permission_problem_names_the_operator_fix(tmp_path):
    binary = str(fake_tailscale(tmp_path, serve_exit=1, serve_err="Access denied: serve config denied; need operator"))
    with pytest.raises(tailnet.TailnetError) as failure:
        await tailnet.publish(binary, 1)
    assert failure.value.code == "tailscale_operator_required" and "--operator" in str(failure.value)


async def test_other_serve_failures_are_generic_and_value_free(tmp_path):
    binary = str(fake_tailscale(tmp_path, serve_exit=1, serve_err="something with a secret token=abc123"))
    with pytest.raises(tailnet.TailnetError) as failure:
        await tailnet.publish(binary, 1)
    assert failure.value.code == "tailscale_serve_failed" and "abc123" not in str(failure.value)


# --- wiring -----------------------------------------------------------------

def _web_profile(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "voice.toml"
    path.write_text(
        '[transport]\ntype = "web"\n' + extra + '\n[agent]\ntype = "pi"\nname = "Host Test"\nslug = "voice"\n'
        f'workspace_dir = {json.dumps(str(tmp_path))}\n'
    )
    return path


def test_start_voice_tailnet_flag_reaches_the_detached_request(tmp_path, monkeypatch):
    from sleeptalk import host
    from sleeptalk.call_provider import CallProviderError
    seen = {}

    def inspect(request):
        seen["request"] = request
        raise CallProviderError("test boundary")
    monkeypatch.setattr(host, "launch_detached_call", inspect)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path))
    receipt = host.start_voice_web(config=str(_web_profile(tmp_path)), local=True, tailnet=True)
    assert receipt["status"] == "error"
    assert seen["request"].tailnet is True and seen["request"].local_rendezvous is False


def test_profile_delivery_setting_selects_tailnet_without_the_flag(tmp_path, monkeypatch):
    from sleeptalk import host
    from sleeptalk.call_provider import CallProviderError
    seen = {}

    def inspect(request):
        seen["request"] = request
        raise CallProviderError("test boundary")
    monkeypatch.setattr(host, "launch_detached_call", inspect)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path))
    path = _web_profile(tmp_path, '[transport.options]\ndelivery = "tailnet"\n')
    host.start_voice_web(config=str(path), local=True)
    assert seen["request"].tailnet is True and seen["request"].local_rendezvous is False



def test_profile_delivery_this_computer_opens_every_call_locally(tmp_path, monkeypatch):
    """`delivery = "local"`: a link the agent asks for opens on localhost, no relay."""
    from sleeptalk import host
    from sleeptalk.call_provider import CallProviderError
    seen = []

    def inspect(request):
        seen.append(request)
        raise CallProviderError("test boundary")
    monkeypatch.setattr(host, "launch_detached_call", inspect)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path))
    path = _web_profile(tmp_path, '[transport.options]\ndelivery = "local"\n')
    host.start_voice_web(config=str(path))
    host.start_voice_web(config=str(path), tailnet=True)
    assert [(r.local_rendezvous, r.tailnet) for r in seen] == [(True, False), (False, True)]

def test_detached_child_command_carries_the_tailnet_flag(tmp_path, monkeypatch):
    from sleeptalk import detached_call
    from sleeptalk.call_provider import CallContext
    request = detached_call.DetachedCallRequest(
        rendezvous_url="https://example.test", context=CallContext("a", "", "u"), ttl_seconds=60,
        echo=True, profile="voice", tailnet=True,
    )
    plain = detached_call.DetachedCallRequest(
        rendezvous_url="https://example.test", context=CallContext("a", "", "u"), ttl_seconds=60,
        echo=True, profile="voice",
    )
    assert request.fingerprint() != plain.fingerprint()
    parser = detached_call._child_parser()
    args = parser.parse_args(["--child", "--ready-file", "r", "--call-id", "c", "--registry-root", str(tmp_path),
                              "--profile-key", "k", "--profile", "voice", "--profile-lock-fd", "3", "--rendezvous", "https://example.test",
                              "--agent-instance", "a", "--conversation-id", "", "--platform-user-id", "u", "--ttl-seconds", "60", "--tailnet"])
    assert args.tailnet is True


async def test_web_call_over_the_tailnet_uses_the_published_provider(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from sleeptalk import web_call
    from sleeptalk.local_rendezvous import LocalCallProvider
    seen = {}

    @asynccontextmanager
    async def published(**kwargs):
        async with LocalCallProvider(public_origin="https://vps.tail1234.ts.net:8443") as provider:
            seen["provider"] = provider
            yield provider
    monkeypatch.setattr("sleeptalk.tailnet.published_provider", published)

    async def run(*, provider, **kwargs):
        seen["ran_with"] = provider
    monkeypatch.setattr(web_call, "_run_profile_web_call", run)
    await web_call.run_profile_web_call(config_path="x", rendezvous_url="https://unused.test", context=None,
                                        ttl_seconds=60, on_ready=lambda url: None, tailnet=True)
    assert seen["ran_with"] is seen["provider"] and seen["provider"].public_origin == "https://vps.tail1234.ts.net:8443"


# --- detection and the setup question ---------------------------------------

async def test_detect_is_value_free_and_never_raises(tmp_path):
    fake_tailscale(tmp_path)
    ready = await tailnet._detect(environ={"PATH": str(tmp_path / "bin")})
    assert ready == {"available": True, "code": "", "name": "vps.tail1234.ts.net", "origin": "https://vps.tail1234.ts.net:8443"}
    missing = await tailnet._detect(environ={"PATH": str(tmp_path / "nowhere")})
    assert missing["available"] is False and missing["code"] == "tailscale_missing"
    fake_tailscale(tmp_path, certs=[])
    off = await tailnet._detect(environ={"PATH": str(tmp_path / "bin")})
    assert off["available"] is False and off["code"] == "tailscale_https_disabled"


def test_profile_round_trips_the_delivery_choice(tmp_path):
    from sleeptalk.wizard import WizardAnswers, load_existing_answers, profile_toml
    import tomllib
    answers = WizardAnswers(surface="web", delivery="tailnet", agent_type="codex", slug="voice", name="Voice")
    document = profile_toml(answers)
    assert tomllib.loads(document)["transport"]["options"]["delivery"] == "tailnet"
    assert "delivery" not in profile_toml(WizardAnswers(surface="web", agent_type="codex", slug="voice", name="Voice"))
    assert "delivery" not in profile_toml(WizardAnswers(surface="terminal", delivery="tailnet", agent_type="codex", slug="voice", name="Voice"))
    profiles = tmp_path / "profiles"; profiles.mkdir()
    (profiles / "voice.toml").write_text(document)
    assert load_existing_answers("voice", profiles_dir=profiles).delivery == "tailnet"


async def _available(*, environ=None):
    return {"available": True, "code": "", "name": "mac.tail1234.ts.net", "origin": "https://mac.tail1234.ts.net:8443"}


async def test_setup_offers_the_tailnet_once_and_remembers_yes(monkeypatch):
    from sleeptalk.wizard import WizardAnswers, ask_tailnet_delivery
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    monkeypatch.setattr("sleeptalk.tailnet.detect", _available)
    prompts = []

    def select(prompt, **kwargs):
        prompts.append(prompt)
        return SimpleNamespace(ask_async=AsyncMock(return_value="tailnet"))
    monkeypatch.setattr("questionary.select", select)
    answers = WizardAnswers(surface="web")
    await ask_tailnet_delivery(answers)
    assert answers.delivery == "tailnet" and len(prompts) == 1 and "mac.tail1234.ts.net" in prompts[0]
    await ask_tailnet_delivery(answers)            # already chosen: not asked again
    assert len(prompts) == 1
    await ask_tailnet_delivery(WizardAnswers(surface="terminal"))   # not a browser call: never asked
    assert len(prompts) == 1


async def test_setup_stays_silent_without_a_ready_tailnet(monkeypatch):
    from sleeptalk.wizard import WizardAnswers, ask_tailnet_delivery
    from unittest.mock import Mock
    select = Mock(side_effect=AssertionError("must not prompt"))
    monkeypatch.setattr("questionary.select", select)
    answers = WizardAnswers(surface="web")
    await ask_tailnet_delivery(answers)            # conftest: Tailscale absent
    assert answers.delivery == "" and not select.called



async def test_setup_says_why_when_tailnet_https_is_off(monkeypatch, capsys):
    from sleeptalk.wizard import WizardAnswers, ask_tailnet_delivery
    from unittest.mock import Mock

    async def https_off(*, environ=None):
        return {"available": False, "code": "tailscale_https_disabled", "message": "HTTPS certificates are off"}
    monkeypatch.setattr("sleeptalk.tailnet.detect", https_off)
    select = Mock(side_effect=AssertionError("must not prompt"))
    monkeypatch.setattr("questionary.select", select)
    answers = WizardAnswers(surface="web")
    await ask_tailnet_delivery(answers)
    assert "HTTPS certificates are off" in capsys.readouterr().out
    assert answers.delivery == "" and not select.called

def test_ensure_tailnet_flag_is_stored_and_reported(tmp_path, monkeypatch):
    from sleeptalk import host, receipts
    from sleeptalk.doctor import OK, DoctorReport, Rung
    home = tmp_path / "home"
    answers = tmp_path / "answers.toml"
    answers.write_text('name = "Voice"\nstt_key = "deepgram-secret"\ntts_key = "cartesia-secret"\ntts_voice_id = "voice-1"\n')
    monkeypatch.setenv("SLEEPTALK_HOME", str(home))
    monkeypatch.setattr("sleeptalk.tailnet.detect", _available)

    async def ok(*args, **kwargs):
        return []
    async def doctor(path, *, env=None, live=True, surface=None):
        return DoctorReport(rungs=[Rung("ready", OK)])
    monkeypatch.setattr("sleeptalk.setup_core.validate_answers", ok)
    monkeypatch.setattr(host, "run_doctor", doctor)

    receipt = host.ensure(profile="default", surface="web", route="cascade", agent="pi", answers=str(answers), tailnet=True)
    assert receipt["status"] == receipts.STATUS_READY
    assert receipt["tailnet"] == {"available": True, "selected": True, "name": "mac.tail1234.ts.net"}
    assert 'delivery = "tailnet"' in (home / "profiles" / "default.toml").read_text()
    assert "secret" not in str(receipt)


def _serving(port: int) -> dict:
    return {"TCP": {"8443": {"HTTPS": True}},
            "Web": {"vps.tail1234.ts.net:8443": {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{port}"}}}}}


def _dead_pid() -> int:
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _record(tmp_path: Path, *, port: int, pid: int) -> Path:
    path = tmp_path / "home" / "tailnet-serve.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"https_port": 8443, "port": port, "pid": pid}))
    return path


async def test_published_provider_records_its_entry_privately_and_forgets_it(tmp_path):
    fake_tailscale(tmp_path)
    async with tailnet.published_provider(environ={"PATH": str(tmp_path / "bin")}) as provider:
        record = tmp_path / "home" / "tailnet-serve.json"
        assert json.loads(record.read_text())["port"] == provider.port
        assert record.stat().st_mode & 0o777 == 0o600
    assert not record.exists()


async def test_a_dead_calls_serve_entry_is_removed(tmp_path):
    binary = str(fake_tailscale(tmp_path, serve_status=_serving(40001)))
    record = _record(tmp_path, port=40001, pid=_dead_pid())
    await tailnet.clear_stale(binary)
    assert calls(tmp_path) == [["serve", "status", "--json"], ["serve", "--https=8443", "off"]]
    assert not record.exists()


async def test_an_entry_sleeptalk_did_not_leave_is_never_touched(tmp_path):
    binary = str(fake_tailscale(tmp_path, serve_status=_serving(55555)))
    _record(tmp_path, port=40001, pid=_dead_pid())
    await tailnet.clear_stale(binary)
    assert calls(tmp_path) == [["serve", "status", "--json"]]


async def test_a_live_calls_entry_is_left_alone(tmp_path):
    import os

    binary = str(fake_tailscale(tmp_path, serve_status=_serving(40001)))
    record = _record(tmp_path, port=40001, pid=os.getpid())
    await tailnet.clear_stale(binary)
    assert calls(tmp_path) == []
    assert record.exists()


def test_host_stop_after_a_kill_clears_the_entry(tmp_path):
    fake_tailscale(tmp_path, serve_status=_serving(40001))
    record = _record(tmp_path, port=40001, pid=_dead_pid())
    tailnet.clear_stale_after_kill({"PATH": str(tmp_path / "bin")})
    assert calls(tmp_path) == [["serve", "status", "--json"], ["serve", "--https=8443", "off"]]
    assert not record.exists()


def test_no_tailnet_skips_the_stored_choice_for_one_call(tmp_path, monkeypatch):
    """An agent asked for a normal web link on a tailnet profile had no way
    to get one: the stored choice won every time."""
    from sleeptalk import host
    from sleeptalk.call_provider import CallProviderError
    seen = {}

    def inspect(request):
        seen["request"] = request
        raise CallProviderError("test boundary")
    monkeypatch.setattr(host, "launch_detached_call", inspect)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path))
    path = _web_profile(tmp_path, '[transport.options]\ndelivery = "tailnet"\n')
    host.start_voice_web(config=str(path), no_tailnet=True)
    assert seen["request"].tailnet is False
    assert 'delivery = "tailnet"' in path.read_text()  # one call only
    assert host.start_voice_web(config=str(path), tailnet=True, no_tailnet=True)["status"] == "error"


def test_cli_refuses_tailnet_and_no_tailnet_together(capsys):
    from sleeptalk import cli

    assert cli.main(["host", "start-voice", "--tailnet", "--no-tailnet", "--json"]) == cli.EXIT_CONFIG
    assert "not both" in capsys.readouterr().err


def test_the_web_transport_accepts_every_delivery_the_settings_page_saves():
    """The settings page saved delivery = "local" that the call then refused to load."""
    from sleeptalk.defaults import register_defaults
    from sleeptalk.registries import TRANSPORTS
    from sleeptalk.settings_page import _WRITABLE
    from sleeptalk.transports.web import WebOptions

    register_defaults()
    assert TRANSPORTS.get("web").options_schema is WebOptions
    for choice in _WRITABLE["transport.options.delivery"][2]:
        stored = choice if choice in ("tailnet", "local") else ""
        assert WebOptions.model_validate({"delivery": stored}).delivery == stored
