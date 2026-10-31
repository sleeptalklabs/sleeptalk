"""Bring your own agent: profiles, generic ACP, remote bridge, probes, CLI."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from sleeptalk.adapter_cli import TEMPLATES as SCAFFOLDS
from sleeptalk.adapter_cli import scaffold
from sleeptalk.adapter_cli import test_adapter as adapter_test_command
from sleeptalk.agents.bridge_lite import CommandAgent, HttpAgent
from sleeptalk.config import AgentConfig, SleeptalkConfig, _validate_component_choices, load_resolved_config
from sleeptalk.conformance.runner import run_adapter_test
from sleeptalk.defaults import register_defaults
from sleeptalk.registries import AGENTS
from sleeptalk.runtime_probe import probe_runtime

register_defaults()
TEMPLATES = Path(__file__).resolve().parents[1] / "sleeptalk" / "templates" / "byo"
PATH_ENV = {"PATH": "/usr/bin:/bin"}


def _problems(**agent) -> list[str]:
    return _validate_component_choices(SleeptalkConfig(agent=AgentConfig(**agent)), env={})


# ---- profiles -------------------------------------------------------------


def test_profiles_validate_private_options() -> None:
    assert _problems(type="command", options={"command": ["node", "agent.mjs"], "env": ["OPENAI_API_KEY"]}) == []
    assert _problems(type="command", options={"command": ["x"], "protocol": "acp"}, permission_mode="pending") == []
    assert _problems(type="http", options={"url": "https://agent.example.com/turn", "token_env": "T"}) == []
    assert _problems(type="bridge", options={"url": "wss://agent.example.com/v1/agent-bridge", "token_env": "T"}) == []
    assert _problems(type="command", options={}) != []
    assert _problems(type="command", options={"command": ["x"], "protocol": "grpc"}) != []
    assert _problems(type="bridge", options={"url": "ws://agent.example.com/v1/agent-bridge", "token_env": "T"}) != []
    assert _problems(type="bridge", options={"url": "wss://agent.example.com/v1/agent-bridge?token=x", "token_env": "T"}) != []
    assert any("do not apply" in problem for problem in _problems(type="http", options={"url": "https://a.example/t"}, model="gpt"))


def test_existing_hermes_acp_type_is_unchanged() -> None:
    assert AGENTS.get("acp").name == "acp"
    assert AGENTS.get("hermes").name == "acp"
    assert AGENTS.get("command").name == "command"


def test_http_and_bridge_declare_their_token() -> None:
    config = AgentConfig(type="http", options={"url": "https://a.example/t", "token_env": "MY_AGENT_TOKEN"})
    assert "MY_AGENT_TOKEN" in AGENTS.get("http").required_env_for(config)
    assert AGENTS.get("http").required_env_for(AgentConfig(type="http", options={"url": "https://a.example/t"})) == {}


# ---- generic ACP over type = "command" ------------------------------------


def _acp_agent(tmp_path: Path, **agent):
    config = AgentConfig(
        type="command",
        workspace_dir=str(tmp_path),
        options={"protocol": "acp", "command": [sys.executable, str(TEMPLATES / "acp-python" / "agent.py")]},
        **agent,
    )
    return AGENTS.get("command").factory(config, PATH_ENV)


def test_generic_acp_defaults_to_pending_permissions(tmp_path: Path) -> None:
    agent = _acp_agent(tmp_path)
    assert agent.permission_mode == "pending"
    assert agent.profile.binary == sys.executable
    assert _acp_agent(tmp_path, permission_mode="deny").permission_mode == "deny"


async def test_generic_acp_template_passes_fixture_and_live_conformance(tmp_path: Path) -> None:
    for mode in ("fixture", "live"):
        report = await run_adapter_test(lambda: _acp_agent(tmp_path), adapter="command (acp)", mode=mode)
        assert not report.failed, report.format_text()
    statuses = {check.id: check.status for check in report.checks}
    assert statuses["abort.recovers"] == "pass"


async def test_generic_acp_spawns_with_the_allowlisted_environment(tmp_path: Path, monkeypatch) -> None:
    probe = tmp_path / "env_acp.py"
    probe.write_text(
        (TEMPLATES / "acp-python" / "agent.py").read_text().replace(
            'turn.say(f"You said: {text.strip()}.")',
            'import os; turn.say("Secret is " + ("present." if os.environ.get("GITHUB_TOKEN") else "absent."))',
        )
    )
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-should-not-leak")
    config = AgentConfig(type="command", workspace_dir=str(tmp_path), options={"protocol": "acp", "command": [sys.executable, str(probe)]})
    agent = AGENTS.get("command").factory(config, {**PATH_ENV, "GITHUB_TOKEN": "ghp-should-not-leak"})
    try:
        assert await agent.respond("hi") == "Secret is absent."
    finally:
        await agent.close()


# ---- remote bridge --------------------------------------------------------


async def test_bridge_type_talks_to_a_reference_server(tmp_path: Path) -> None:
    from sleeptalk.agent_bridge import AgentBridgeServer

    import importlib.util

    # A unique module name: "server" would trip the import-boundary guard.
    spec = importlib.util.spec_from_file_location("byo_bridge_template", TEMPLATES / "bridge-python" / "server.py")
    assert spec is not None and spec.loader is not None
    bridge_template = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge_template)
    server = AgentBridgeServer(token="dev-token", agent=bridge_template.Agent())
    host, port = await server.start_ws("127.0.0.1", 0)
    config = AgentConfig(type="bridge", workspace_dir=str(tmp_path), options={"url": f"ws://{host}:{port}/v1/agent-bridge", "token_env": "BRIDGE_TOKEN"})
    try:
        report = await run_adapter_test(
            lambda: AGENTS.get("bridge").factory(config, {"BRIDGE_TOKEN": "dev-token"}),
            adapter="bridge",
            mode="fixture",
        )
        assert not report.failed, report.format_text()
        assert {c.id: c.status for c in report.checks}["tool.lifecycle"] == "pass"
        live = await run_adapter_test(
            lambda: AGENTS.get("bridge").factory(config, {"BRIDGE_TOKEN": "dev-token"}),
            adapter="bridge",
            mode="live",
        )
        assert not live.failed, live.format_text()
        assert {c.id: c.status for c in live.checks}["abort.recovers"] == "pass"
        wrong = await probe_runtime(config, env={"BRIDGE_TOKEN": "wrong"})
        assert wrong["login"] == "unavailable"
        right = await probe_runtime(config, env={"BRIDGE_TOKEN": "dev-token"})
        assert right["protocol"] == "available" and right["login"] == "available"
    finally:
        await server.aclose()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
async def test_node_bridge_template_passes_fixture_and_live_conformance(tmp_path: Path) -> None:
    import asyncio
    import os
    import re

    proc = await asyncio.create_subprocess_exec(
        "node",
        str(TEMPLATES / "bridge-node" / "server.mjs"),
        env={"PATH": os.environ["PATH"], "PORT": "0", "AGENT_BRIDGE_TOKEN": "dev-token"},
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        assert proc.stdout is not None
        banner = (await asyncio.wait_for(proc.stdout.readline(), timeout=10)).decode()
        port = re.search(r":(\d+)\s*$", banner.strip()).group(1)
        config = AgentConfig(
            type="bridge",
            workspace_dir=str(tmp_path),
            options={"url": f"tcp://127.0.0.1:{port}", "token_env": "BRIDGE_TOKEN"},
        )

        def factory():
            return AGENTS.get("bridge").factory(config, {"BRIDGE_TOKEN": "dev-token"})

        for mode in ("fixture", "live"):
            report = await run_adapter_test(factory, adapter="bridge (node)", mode=mode)
            assert not report.failed, report.format_text()
        statuses = {c.id: c.status for c in report.checks}
        assert statuses["abort.recovers"] == "pass"

        agent = factory()
        try:
            await agent.warmup()
            for _turn in range(2):  # long replies, one connection
                text = " ".join(["Word."] * 50)
                spoken = [s async for s in agent.respond_streaming(text)]
                assert len(spoken) >= 40
        finally:
            await agent.close()

        wrong = await probe_runtime(config, env={"BRIDGE_TOKEN": "nope"})
        assert wrong["login"] == "unavailable"
    finally:
        proc.terminate()
        await proc.wait()


async def test_bridge_without_token_fails_clearly() -> None:
    from sleeptalk.agent_bridge import AgentBridgeError

    config = AgentConfig(type="bridge", options={"url": "ws://127.0.0.1:9/v1/agent-bridge", "token_env": "UNSET_BRIDGE_TOKEN"})
    agent = AGENTS.get("bridge").factory(config, {})
    with pytest.raises(AgentBridgeError, match="UNSET_BRIDGE_TOKEN"):
        await agent.warmup()
    await agent.close()


# ---- readiness probes -----------------------------------------------------


async def test_command_probe_reports_hello_without_sending_a_prompt(tmp_path: Path) -> None:
    log = tmp_path / "frames.log"
    script = tmp_path / "agent.py"
    script.write_text(
        "import json, sys\n"
        f"log = open({str(log)!r}, 'a')\n"
        "for line in sys.stdin:\n"
        "    log.write(line); log.flush()\n"
        "    if json.loads(line)['type'] == 'hello':\n"
        "        print(json.dumps({'type': 'hello.ok', 'capabilities': {}}), flush=True)\n"
    )
    config = AgentConfig(type="command", workspace_dir=str(tmp_path), options={"command": [sys.executable, str(script)]})
    result = await probe_runtime(config, env=PATH_ENV)
    assert result["adapter"] == "command"
    assert result["protocol"] == "available"
    assert "advertised" in result["next_action"]
    kinds = [json.loads(line)["type"] for line in log.read_text().splitlines()]
    assert "turn.prompt" not in kinds

    missing = AgentConfig(type="command", workspace_dir=str(tmp_path), options={"command": ["no-such-agent-xyz"]})
    assert (await probe_runtime(missing, env=PATH_ENV))["protocol"] == "unavailable"


async def test_http_probe_distinguishes_unreachable(tmp_path: Path, unused_tcp_port: int) -> None:
    config = AgentConfig(type="http", workspace_dir=str(tmp_path), options={"url": f"http://127.0.0.1:{unused_tcp_port}/turn"})
    result = await probe_runtime(config, env={})
    assert result["protocol"] == "unavailable"


# ---- CLI ------------------------------------------------------------------


@pytest.mark.parametrize(("kind", "language"), sorted(SCAFFOLDS))
def test_every_scaffold_writes_a_loadable_profile(kind: str, language: str, tmp_path: Path) -> None:
    target = tmp_path / f"{kind}-{language}"
    assert scaffold("my-agent", kind=kind, language=language, directory=str(target), out=open("/dev/null", "w")) == 0
    profile = target / "sleeptalk.agent.toml"
    assert profile.is_file()
    text = "\n".join(path.read_text() for path in target.rglob("*") if path.is_file())
    assert "AGENT_NAME" not in text and "AGENT_MODULE" not in text
    if kind != "python":  # the plugin type exists only once the package is installed
        config = load_resolved_config(profile, check_env=False, check_binaries=False)
        assert config.agent.slug == "my-agent"


def test_scaffold_refuses_bad_names_and_non_empty_directories(tmp_path: Path) -> None:
    sink = open("/dev/null", "w")
    assert scaffold("My Agent", kind="command", language="python", directory=str(tmp_path / "a"), out=sink) == 2
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "keep.txt").write_text("mine")
    assert scaffold("ok", kind="command", language="python", directory=str(tmp_path / "b"), out=sink) == 2
    assert (tmp_path / "b" / "keep.txt").read_text() == "mine"
    assert scaffold("ok", kind="acp", language="node", directory=str(tmp_path / "c"), out=sink) == 2


def test_adapter_test_command_on_a_fresh_scaffold(tmp_path: Path, capsys, monkeypatch) -> None:
    target = tmp_path / "echo"
    scaffold("echo", kind="command", language="python", directory=str(target), out=open("/dev/null", "w"))
    profile = target / "sleeptalk.agent.toml"
    profile.write_text(profile.read_text().replace('"python3"', json.dumps(sys.executable)))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert adapter_test_command(str(profile), mode="fixture", as_json=True) == 0
    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert report["operation"] == "adapter_test"
    assert report["result"] == "pass"
    assert report["capabilities"]["permissions"]["level"] == "supported"


def test_adapter_test_reports_a_broken_profile(tmp_path: Path) -> None:
    profile = tmp_path / "sleeptalk.agent.toml"
    profile.write_text('[agent]\ntype = "command"\n\n[agent.options]\nprotocol = "lite"\n')
    assert adapter_test_command(str(profile), mode="offline", as_json=True) == 2


async def test_offline_mode_never_prompts(tmp_path: Path) -> None:
    log = tmp_path / "frames.log"
    script = tmp_path / "agent.py"
    script.write_text(
        "import sys\n"
        f"log = open({str(log)!r}, 'a')\n"
        "for line in sys.stdin:\n"
        "    log.write(line); log.flush()\n"
    )
    config = AgentConfig(type="command", workspace_dir=str(tmp_path), options={"command": [sys.executable, str(script)], "hello_timeout_s": 0})
    report = await run_adapter_test(lambda: AGENTS.get("command").factory(config, PATH_ENV), adapter="command", mode="offline")
    assert not report.failed, report.format_text()
    assert "turn.prompt" not in log.read_text()


def test_new_types_are_registered_but_kept_out_of_the_init_menu() -> None:
    from sleeptalk.wizard import agent_menu_entries

    assert {"command", "http", "bridge"} <= set(AGENTS.names())
    offered = {value for _label, value in agent_menu_entries(detected={})}
    assert not offered & {"command", "http", "bridge"}
    assert "codex" in offered


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_node_templates_parse() -> None:
    import subprocess

    for path in (
        TEMPLATES / "command-node" / "agent.mjs",
        TEMPLATES / "http-node" / "server.mjs",
        TEMPLATES / "bridge-node" / "server.mjs",
    ):
        subprocess.run(["node", "--check", str(path)], check=True)


def test_agent_classes_are_exported() -> None:
    assert CommandAgent.__name__ == "CommandAgent"
    assert HttpAgent.__name__ == "HttpAgent"


def test_http_python_template_refuses_an_open_address_without_a_token() -> None:
    import subprocess

    refused = subprocess.run(
        [sys.executable, str(TEMPLATES / "http-python" / "server.py")],
        env={"PATH": "/usr/bin:/bin", "HOST": "0.0.0.0", "PORT": "0"},
        capture_output=True, text=True, timeout=20,
    )
    assert refused.returncode != 0 and "AGENT_TOKEN" in refused.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_http_node_template_refuses_an_open_address_without_a_token() -> None:
    import subprocess

    refused = subprocess.run(
        [shutil.which("node"), str(TEMPLATES / "http-node" / "server.mjs")],
        env={"PATH": "/usr/bin:/bin", "HOST": "0.0.0.0", "PORT": "0"},
        capture_output=True, text=True, timeout=20,
    )
    assert refused.returncode != 0 and "AGENT_TOKEN" in refused.stderr


def test_http_python_template_compares_the_token_in_constant_time() -> None:
    source = (TEMPLATES / "http-python" / "server.py").read_text()
    assert "hmac.compare_digest" in source and '== f"Bearer' not in source


# ---- bridge controls ------------------------------------------------------


class _SteerableAgent:
    """Remote agent that accepts steering and permission decisions, rejects cancel."""

    def __init__(self) -> None:
        from sleeptalk.adapter import (
            AgentCapabilityManifest,
            CapabilitySupport,
            SupportLevel,
            VoiceSessionCapability,
            WorkerCapability,
        )

        yes, no = CapabilitySupport(SupportLevel.SUPPORTED), CapabilitySupport(SupportLevel.UNSUPPORTED)
        self.agent_capabilities = AgentCapabilityManifest(
            contract_version=1, structured_events=yes, tool_lifecycle=no, permissions=yes,
            clarification=no, session_identity=no, status=no, usage=no, steering=yes,
            targeted_cancellation=yes, workers=WorkerCapability(SupportLevel.UNSUPPORTED),
            voice_sessions=VoiceSessionCapability(SupportLevel.UNSUPPORTED),
        )
        self.steers: list[str] = []
        self._permission = None
        self._aborted = False

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def abort(self) -> None:
        self._aborted = True

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        return text

    def respond_streaming(self, text: str, *, on_event=None):
        import asyncio

        from sleeptalk.adapter import PermissionDecision, PermissionRequested, publish_event

        async def stream():
            self._aborted = False
            if text == "permission":
                self._permission = asyncio.get_running_loop().create_future()
                publish_event(on_event, PermissionRequested(request_id="perm-1", prompt="Delete dist?", options=frozenset({PermissionDecision.DENY, PermissionDecision.ALLOW_ONCE})))
                decision = await self._permission
                yield "Deleted it." if decision is PermissionDecision.ALLOW_ONCE else "Left it alone."
                return
            for index in range(30):
                if self._aborted:
                    return
                yield f"Sentence {index}."
                await asyncio.sleep(0.01)

        return stream()

    async def control(self, command):
        from sleeptalk.adapter import Cancel, ControlAccepted, ControlRejected, ResolvePermission, Steer

        if isinstance(command, Steer):
            self.steers.append(command.instruction)
            return ControlAccepted(resource="steer-1")
        if isinstance(command, ResolvePermission) and self._permission is not None and not self._permission.done():
            self._permission.set_result(command.decision)
            return ControlAccepted(request_id=command.request_id)
        if isinstance(command, Cancel):
            return ControlRejected(reason="nothing to cancel", retryable=False)
        return ControlRejected(reason="unknown request")


async def _bridge_with(agent, tmp_path: Path):
    from sleeptalk.agent_bridge import AgentBridgeServer

    server = AgentBridgeServer(token="dev-token", agent=agent)
    host, port = await server.start_ws("127.0.0.1", 0)
    config = AgentConfig(type="bridge", workspace_dir=str(tmp_path), options={"url": f"ws://{host}:{port}/v1/agent-bridge", "token_env": "T"})
    return server, (lambda: AGENTS.get("bridge").factory(config, {"T": "dev-token"}))


async def test_bridge_controls_wait_for_the_agents_answer(tmp_path: Path) -> None:
    import asyncio

    from sleeptalk.adapter import Cancel, ControlAccepted, ControlRejected, Steer, adapter_control

    remote = _SteerableAgent()
    server, factory = await _bridge_with(remote, tmp_path)
    agent = factory()
    try:
        await agent.warmup()
        accepted = await adapter_control(agent, Steer(instruction="be brief"))
        assert isinstance(accepted, ControlAccepted) and accepted.resource == "steer-1"
        rejected = await adapter_control(agent, Cancel(target_id="w1"))
        assert isinstance(rejected, ControlRejected) and rejected.reason == "nothing to cancel"

        # A control sent mid-stream neither steals sentences nor gets lost.
        spoken: list[str] = []

        async def consume() -> None:
            async for sentence in agent.respond_streaming("count"):
                spoken.append(sentence)

        task = asyncio.create_task(consume())
        while len(spoken) < 3:
            await asyncio.sleep(0.005)
        mid = await adapter_control(agent, Steer(instruction="faster"))
        await asyncio.wait_for(task, timeout=10)
        assert isinstance(mid, ControlAccepted)
        assert len(spoken) == 30
        assert remote.steers == ["be brief", "faster"]
    finally:
        await agent.close()
        await server.aclose()


async def test_bridge_permissions_and_steering_pass_fixture_conformance(tmp_path: Path) -> None:
    server, factory = await _bridge_with(_SteerableAgent(), tmp_path)
    try:
        report = await run_adapter_test(factory, adapter="bridge", mode="fixture")
        statuses = {c.id: c.status for c in report.checks}
        assert statuses["permissions"] == "pass", report.format_text()
        assert statuses["steering"] == "pass", report.format_text()
        assert statuses["voice_sessions.reject"] == "pass"
    finally:
        await server.aclose()


async def test_bridge_control_without_an_answer_is_rejected_as_retryable(tmp_path: Path, monkeypatch) -> None:
    from sleeptalk.adapter import ControlRejected, Steer, adapter_control
    from sleeptalk.agents import bridge_remote

    monkeypatch.setattr(bridge_remote, "CONTROL_TIMEOUT_S", 0.2)
    remote = _SteerableAgent()
    server, factory = await _bridge_with(remote, tmp_path)
    agent = factory()

    import asyncio

    release = asyncio.Event()

    async def silent_until_released(_command):
        await release.wait()
        return ControlRejected(reason="too late")

    remote.control = silent_until_released
    try:
        await agent.warmup()
        result = await adapter_control(agent, Steer(instruction="hello?"))
        assert isinstance(result, ControlRejected) and result.retryable
        assert "did not acknowledge" in result.reason
    finally:
        # The reference server handles controls inline; free it before teardown.
        release.set()
        await agent.close()
        await server.aclose()


def test_command_agents_declare_their_program_for_readiness_checks(tmp_path: Path) -> None:
    from sleeptalk.config import collect_missing_binaries

    agent_dir = tmp_path / "bin"
    agent_dir.mkdir()
    script = agent_dir / "my-agent"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)

    present = SleeptalkConfig(agent=AgentConfig(type="command", workspace_dir=str(tmp_path), options={"command": ["bin/my-agent"]}))
    assert collect_missing_binaries(present, env={}) == []

    missing = SleeptalkConfig(agent=AgentConfig(type="command", workspace_dir=str(tmp_path), options={"command": ["release-proof-missing-agent"]}))
    problems = collect_missing_binaries(missing, env={})
    assert problems and "release-proof-missing-agent" in problems[0]
    assert "[agent.options].command" in problems[0]

    assert AGENTS.get("http").binary_for(AgentConfig(type="http", options={"url": "https://a.example/t"})) == ""
