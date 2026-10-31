import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from sleeptalk import bootstrap, runtime_binding
from sleeptalk.config import AgentConfig, ConfigError, SleeptalkConfig, load_config
from sleeptalk.runtime_binding import BindingError, bind, verify


@pytest.fixture(autouse=True)
def machine(monkeypatch):
    monkeypatch.setattr(runtime_binding, "host_identity", lambda: "a" * 64)


def bound(path):
    agent = AgentConfig(type="codex", workspace_dir=str(path))
    return SleeptalkConfig(agent=agent, binding=bind(agent))


def test_same_basename_distinct_directories_and_worktrees(tmp_path):
    first = tmp_path / "a" / "project"
    second = tmp_path / "b" / "project"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    cfg = bound(first)
    verify(cfg, workspace=str(first))
    with pytest.raises(BindingError, match="different workspace"):
        verify(cfg, workspace=str(second))
    assert cfg.binding.workspace != bound(second).binding.workspace


def test_symlink_alias_matches_but_replacement_does_not(tmp_path):
    directory = tmp_path / "project"
    directory.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(directory)
    cfg = bound(directory)
    verify(cfg, workspace=str(link))
    directory.rename(tmp_path / "old")
    directory.mkdir()
    with pytest.raises(BindingError, match="replaced"):
        verify(cfg)


class _Renumbered:
    """A stat result whose disk number changed, as macOS does at boot."""

    def __init__(self, info, disk):
        self._info, self.st_dev = info, disk

    def __getattr__(self, name):
        return getattr(self._info, name)


def _renumber_disk(monkeypatch, disk):
    real = Path.stat
    monkeypatch.setattr(Path, "stat", lambda self, **kw: _Renumbered(real(self, **kw), disk))


def test_a_renumbered_disk_is_not_a_replaced_workspace(tmp_path, monkeypatch):
    cfg = bound(tmp_path)
    _renumber_disk(monkeypatch, 0x01000022)
    verify(cfg)


def test_a_binding_that_hashed_the_disk_number_still_loads_after_renumbering(tmp_path, monkeypatch):
    path = tmp_path.resolve()
    info = path.stat()
    legacy = runtime_binding._digest([str(path), 0x01000022, info.st_ino])
    cfg = bound(tmp_path)
    cfg = cfg.model_copy(update={"binding": cfg.binding.model_copy(update={"workspace": legacy})})
    monkeypatch.setattr(runtime_binding.platform, "system", lambda: "Darwin")
    _renumber_disk(monkeypatch, 0x01000020)
    verify(cfg)
    path.rename(tmp_path.parent / "old")
    path.mkdir()
    with pytest.raises(BindingError, match="replaced"):
        verify(cfg)


def test_host_agent_and_permissions_cannot_change(tmp_path, monkeypatch):
    cfg = bound(tmp_path)
    cfg.agent.permission_mode = "skip"
    with pytest.raises(BindingError, match="permission"):
        verify(cfg)
    cfg.agent.permission_mode = ""
    monkeypatch.setattr(runtime_binding, "host_identity", lambda: "b" * 64)
    with pytest.raises(BindingError, match="different host"):
        verify(cfg)


def test_legacy_profile_needs_deliberate_migration(tmp_path):
    cfg = SleeptalkConfig(agent=AgentConfig(workspace_dir=str(tmp_path)))
    verify(cfg)
    with pytest.raises(BindingError, match="migrate"):
        verify(cfg, workspace=str(tmp_path))


def test_config_loader_rechecks_binding_before_start(tmp_path):
    cfg = bound(tmp_path)
    profile = tmp_path / "voice.toml"
    profile.write_text(f'[transport]\ntype="web"\n[agent]\ntype="codex"\nworkspace_dir={json.dumps(str(tmp_path))}\n' + runtime_binding.toml(cfg.binding))
    load_config(profile, check_env=False, check_binaries=False)
    profile.write_text(profile.read_text().replace('type="codex"', 'type="pi"'))
    with pytest.raises(ConfigError, match="runtime"):
        load_config(profile, check_env=False, check_binaries=False)


def test_bootstrap_checksum_precedes_install(tmp_path, monkeypatch):
    wheel = tmp_path / "sleeptalk-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"artifact")
    monkeypatch.setattr(bootstrap, "_run", lambda *_: pytest.fail("unverified artifact ran"))
    with pytest.raises(bootstrap.BootstrapError, match="checksum"):
        bootstrap.install_artifact(wheel, "0" * 64, surface="web", agent="codex", root=tmp_path / "runtimes")


def test_partial_bootstrap_retries_without_replacing_complete_runtime(tmp_path, monkeypatch):
    wheel = tmp_path / "sleeptalk-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"artifact")
    digest = hashlib.sha256(b"artifact").hexdigest()
    root = tmp_path / "runtimes"
    existing = root / "existing-live-runtime"
    existing.mkdir(parents=True)
    sentinel = existing / "active"
    sentinel.write_text("keep")
    commands = []
    fail = [True]
    monkeypatch.setattr(bootstrap.shutil, "which", lambda *_: "/usr/bin/uv")

    def run(command):
        commands.append(command)
        if "venv" in command:
            Path(command[-1]).mkdir()
        elif "install" in command and fail[0]:
            fail[0] = False
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(bootstrap, "_run", run)
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.install_artifact(wheel, digest, surface="web", agent="codex", root=root)
    result = bootstrap.install_artifact(wheel, digest, surface="web", agent="codex", root=root)
    count = len(commands)
    assert bootstrap.install_artifact(wheel, digest, surface="web", agent="codex", root=root) == result
    assert len(commands) == count + 1
    assert sentinel.read_text() == "keep"
    assert str(tmp_path) not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["on-request", "never", None])
async def test_runtime_probe_never_starts_or_resumes_conversation(tmp_path, monkeypatch, policy):
    from sleeptalk.runtime_probe import probe_runtime
    from sleeptalk.agents.codex_app_server import CodexAppServerClient
    calls = []

    async def spawn(self, args):
        calls.append("spawn")

    async def handshake(self):
        calls.append("initialize")

    async def rpc(self, method, params):
        calls.append(method)
        if method == "account/read":
            return {"account": {"email": "sentinel@example.com"}, "requiresOpenaiAuth": True}
        assert method == "config/read"
        assert params["cwd"] == str(tmp_path)
        return {"config": {"approval_policy": policy}}

    monkeypatch.setattr(CodexAppServerClient, "_launch_args", lambda _: ["codex", "app-server"])
    monkeypatch.setattr(CodexAppServerClient, "_spawn", spawn)
    monkeypatch.setattr(CodexAppServerClient, "_handshake", handshake)
    monkeypatch.setattr(CodexAppServerClient, "_rpc", rpc)
    result = await probe_runtime(AgentConfig(type="codex", workspace_dir=str(tmp_path)))
    assert result["status"] == ("ready" if policy == "on-request" else "needs_input")
    assert calls == ["spawn", "initialize", "account/read", "config/read"]
    assert "sentinel" not in json.dumps(result)
    assert str(tmp_path) not in json.dumps(result)


@pytest.mark.asyncio
async def test_unsupported_or_approval_bypassing_runtime_is_not_ready(tmp_path):
    from sleeptalk.runtime_probe import probe_runtime
    for agent in ("pi", "custom", "codex"):
        result = await probe_runtime(AgentConfig(type=agent, workspace_dir=str(tmp_path), permission_mode="skip"))
        assert result["status"] != "ready"
