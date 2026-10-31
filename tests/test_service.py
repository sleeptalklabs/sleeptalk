from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest

from sleeptalk import service


@pytest.fixture
def profile(tmp_path):
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    config = tmp_path / "voice.toml"
    config.write_text('[agent]\nslug = "voice"\nworkspace_dir = ' + json.dumps(str(workspace)) + '\n')
    return config


@pytest.mark.parametrize("slug", ["../escape", "voice;touch", "voice\nExecStart=bad", "-flag", "", "x" * 65, "CAPS", "name/path"])
def test_every_service_boundary_rejects_unsafe_slug(slug, tmp_path, monkeypatch):
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: pytest.fail("must not run a command"))
    for render in (service.render_systemd, service.render_launchd):
        with pytest.raises(ValueError, match="Service slug"):
            render(slug, tmp_path / "config.toml")
    assert service.install(slug, tmp_path / "config.toml", yes=True, out=lambda _: None) == 1
    assert service.uninstall(slug, yes=True, out=lambda _: None) == 1
    assert service.status(slug, out=lambda _: None) == 1


def test_systemd_quotes_each_argument_and_disables_variable_expansion(tmp_path, monkeypatch):
    config = tmp_path / 'a $HOME %h "quoted"\\path.toml'
    binary = tmp_path / 'python $SPECIAL %h "quoted"'
    monkeypatch.setattr(service, "_command", lambda: [str(binary), "-m", "sleeptalk.cli"])
    unit = service.render_systemd("voice", config)
    line = next(line for line in unit.contents.splitlines() if line.startswith("ExecStart="))
    assert line.startswith('ExecStart=":')
    assert '$HOME' in line and '$SPECIAL' in line and '%%h' in line
    assert '$$HOME' not in line
    assert '\\"quoted\\"' in line and '\\\\path.toml' in line
    assert '"-m" "sleeptalk.cli" "run" "--config"' in line
    assert "StartLimitBurst=5" in unit.contents.split("[Service]")[0]
    assert "RestartPreventExitStatus=2" in unit.contents
    assert "Environment=PYTHONUNBUFFERED=1" in unit.contents.splitlines()


def test_launchd_runs_the_sleeptalk_script_with_its_own_log(tmp_path, monkeypatch):
    """Login Items names a job by its program: through /bin/sh it read "sh"."""
    bin_dir = tmp_path / "venv & <xml> $()" / "bin"
    bin_dir.mkdir(parents=True)
    script = bin_dir / "sleeptalk"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    monkeypatch.setattr(service.sys, "executable", str(bin_dir / "python3"))
    monkeypatch.setattr(service, "LAUNCHD_LOG_DIR", tmp_path / "Logs")
    config = tmp_path / 'voice & <xml> $(touch x); %.toml'
    payload = plistlib.loads(service.render_launchd("voice", config).contents.encode())
    assert payload["ProgramArguments"] == [str(script), "run", "--config", str(config)]
    assert payload["StandardOutPath"] == payload["StandardErrorPath"] == str(tmp_path / "Logs" / "voice.log")
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    environment = payload["EnvironmentVariables"]
    assert environment["SLEEPTALK_HOME"] == str(tmp_path)
    # Status lines reach the log as they are printed, not when a buffer fills.
    assert environment["PYTHONUNBUFFERED"] == "1"
    assert environment["SLEEPTALK_SERVICE"] == "launchd"
    # Without the script, the pinned interpreter runs the module.
    script.unlink()
    monkeypatch.setattr(service, "_command", lambda: ["/pinned/python3", "-m", "sleeptalk.cli"])
    payload = plistlib.loads(service.render_launchd("voice", config).contents.encode())
    assert payload["ProgramArguments"][:3] == ["/pinned/python3", "-m", "sleeptalk.cli"]


@pytest.mark.parametrize("code,service_env,expected", [(2, "launchd", 0), (2, None, 2), (1, "launchd", 1), (0, "launchd", 0)])
def test_a_setup_error_under_launchd_ends_the_job_instead_of_looping(monkeypatch, code, service_env, expected):
    from sleeptalk import cli

    monkeypatch.setattr(cli, "_main", lambda argv=None: code)
    if service_env:
        monkeypatch.setenv("SLEEPTALK_SERVICE", service_env)
    else:
        monkeypatch.delenv("SLEEPTALK_SERVICE", raising=False)
    assert cli.main([]) == expected


def test_the_service_path_keeps_only_folders_that_exist(tmp_path, monkeypatch):
    real = tmp_path / "bin"
    real.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([str(real), str(tmp_path / "gone"), "relative/bin"]))
    parts = service._service_path().split(os.pathsep)
    assert str(real) in parts
    assert str(tmp_path / "gone") not in parts and "relative/bin" not in parts


def test_home_for_both_profile_layouts_and_explicit_working_directory(tmp_path):
    for profile_dir in (tmp_path / "profiles", tmp_path / "config" / "profiles"):
        unit = service.render_launchd("voice", profile_dir / "voice.toml", workspace=tmp_path / "app")
        payload = plistlib.loads(unit.contents.encode())
        assert payload["EnvironmentVariables"]["SLEEPTALK_HOME"] == str(tmp_path)
        assert payload["WorkingDirectory"] == str(tmp_path / "app")


def test_preflight_requires_explicit_existing_workspace_and_does_not_create_it(profile, tmp_path):
    report = service.preflight("voice", profile)
    assert report["status"] == "ready"
    assert report["readiness_scope"] == "unit_file_only"
    assert set(report["service_context"].values()) == {"unknown"}
    profile.write_text('[agent]\nslug="voice"\n')
    assert "explicit agent.workspace_dir" in service.preflight("voice", profile)["problems"][0]
    missing = tmp_path / "must-not-create"
    profile.write_text('[agent]\nslug="voice"\nworkspace_dir=' + json.dumps(str(missing)) + '\n')
    assert service.preflight("voice", profile)["status"] == "blocked"
    assert not missing.exists()


def test_preflight_credential_status_never_returns_key_or_promises_service_access(profile, tmp_path, monkeypatch):
    from sleeptalk.config import AgentConfig, SleeptalkConfig
    config = SleeptalkConfig(agent=AgentConfig(slug="voice", workspace_dir=str(tmp_path)))
    config = NS(**(config.__dict__ | {"credentials": NS(refs={"DEEPGRAM_API_KEY": "credential:" + "a" * 32})}))
    monkeypatch.setattr("sleeptalk.config.load_resolved_config", lambda *a, **k: config)
    (tmp_path / "store.sqlite3").touch()
    class FakeStore:
        root = tmp_path
        def get(self, ref):
            pytest.fail("preflight must not retrieve a key")
        def status(self, ref):
            return NS(backend="file", available=True, value="sentinel-secret")
    monkeypatch.setattr("sleeptalk.credential_store.CredentialStore", FakeStore)
    report = service.preflight("voice", profile)
    assert report["status"] == "ready"
    assert report["credentials"][0]["available_in_current_process"]
    assert report["credentials"][0]["service_context"] == "unknown"
    assert "sentinel-secret" not in json.dumps(report)


def test_preflight_blocks_terminal_and_slug_mismatch(profile):
    assert service.preflight("wrong", profile)["status"] == "blocked"
    profile.write_text(profile.read_text() + '\n[transport]\ntype="terminal"\n')
    report = service.preflight("voice", profile)
    assert report["status"] == "blocked"
    assert "attached human session" in report["problems"][0]


def test_install_remains_opt_in_and_writes_only_after_consent(profile, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "LAUNCHD_DIR", tmp_path / "agents")
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: pytest.fail("must not start or inspect real jobs"))
    assert service.install("voice", profile, prompt=lambda _: "n", out=lambda _: None) == 1
    assert not (tmp_path / "agents").exists()
    messages = []
    assert service.install("voice", profile, yes=True, out=messages.append) == 0
    unit = tmp_path / "agents" / "ai.sleeptalk.voice.plist"
    assert plistlib.loads(unit.read_bytes())["WorkingDirectory"] == str(profile.parent / "workspace with spaces")
    assert "No service was started or restarted" in "\n".join(messages)
    assert sorted(path.name for path in unit.parent.iterdir()) == [unit.name]


def test_uninstall_retains_state_and_does_not_claim_to_stop_loaded_job(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "LAUNCHD_DIR", tmp_path)
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: pytest.fail("must not stop real jobs"))
    unit = tmp_path / "ai.sleeptalk.voice.plist"
    unit.write_text("definition")
    credentials = tmp_path / "saved-credentials"
    credentials.write_text("sentinel-secret")
    messages = []
    assert service.uninstall("voice", yes=True, out=messages.append) == 0
    output = "\n".join(messages)
    assert "launchctl bootout" in output
    assert "Loaded-job state is unchanged" in output
    assert "credentials are retained" in output
    assert "sentinel-secret" not in output
    assert credentials.read_text() == "sentinel-secret"
    assert not unit.exists()


def test_status_checks_loaded_job_even_after_definition_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "LAUNCHD_DIR", tmp_path)
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return NS(returncode=0, stdout="PID Status Label\n123 0 ai.sleeptalk.voice\n456 0 unrelated\n")
    monkeypatch.setattr(service.subprocess, "run", run)
    messages = []
    assert service.status("voice", out=messages.append) == 0
    assert calls[0][0] == ["launchctl", "list"]
    assert "shell" not in calls[0][1]
    assert "Definition: absent" in messages[0]
    assert messages[1] == "Loaded job: PID=123, last_exit=0"
    assert messages[2] == f"Log: {service.launchd_log('voice')}"
    assert "unrelated" not in "\n".join(messages)


def test_systemd_status_does_not_print_manager_environment_or_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "SYSTEMD_DIR", tmp_path)
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: NS(returncode=0, stdout="ActiveState=active\nMainPID=99\nEnvironment=sentinel-secret\nLog=sentinel-secret\n"))
    messages = []
    assert service.status("voice", out=messages.append) == 0
    assert "ActiveState=active" in messages
    assert "sentinel-secret" not in "\n".join(messages)


def test_missing_service_manager_reports_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "SYSTEMD_DIR", tmp_path)
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    def unavailable(*a, **k):
        raise FileNotFoundError("sentinel-secret")
    monkeypatch.setattr(service.subprocess, "run", unavailable)
    messages = []
    assert service.status("voice", out=messages.append) == 1
    assert "state is unknown" in messages[-1]
    assert "sentinel-secret" not in "\n".join(messages)


def test_start_reloads_a_loaded_launchd_job_and_waits_for_its_bootout(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(service, "LAUNCHD_DIR", tmp_path)
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(service.time, "sleep", lambda _s: None)
    (tmp_path / "ai.sleeptalk.voice.plist").write_text("x")
    calls, bootstraps = [], iter([5, 0])
    def run(command, **kwargs):
        calls.append(command[:2])
        code = next(bootstraps) if command[1] == "bootstrap" else 0
        return NS(returncode=code, stdout="")
    monkeypatch.setattr(service.subprocess, "run", run)
    service.start("voice")
    # Loaded: unload first, then bootstrap until the old job has gone.
    assert calls == [["launchctl", "print"], ["launchctl", "bootout"], ["launchctl", "bootstrap"], ["launchctl", "bootstrap"]]


def test_start_needs_a_definition_and_reports_a_refusing_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(service, "SYSTEMD_DIR", tmp_path)
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    with pytest.raises(ValueError, match="Install the service definition first"):
        service.start("voice")
    (tmp_path / "sleeptalk-voice.service").write_text("x")
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return NS(returncode=1 if "restart" in command else 0, stdout="")
    monkeypatch.setattr(service.subprocess, "run", run)
    with pytest.raises(ValueError, match="didn't start"):
        service.start("voice")
    assert [c[2] for c in calls if c[0] == "systemctl" and c[2] != "show-environment"] == ["daemon-reload", "enable", "restart"]


def test_running_and_stop_read_the_service_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(service, "LAUNCHD_DIR", tmp_path)
    monkeypatch.setattr(service, "SYSTEMD_DIR", tmp_path)
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    listing = "PID Status Label\n123 0 ai.sleeptalk.voice\n- 1 ai.sleeptalk.idle\n"
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return NS(returncode=0, stdout=listing)
    monkeypatch.setattr(service.subprocess, "run", run)
    assert service.running("voice") is True
    assert service.running("idle") is False and service.running("absent") is False
    definition = tmp_path / "ai.sleeptalk.voice.plist"
    definition.write_text("x")
    listing = "PID Status Label\n"
    service.stop("voice")
    assert commands[-1] == ["launchctl", "bootout", f"gui/{os.getuid()}/ai.sleeptalk.voice"]
    # launchd loads every file in LaunchAgents at login: a stopped bot leaves none.
    assert not definition.exists()
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: NS(returncode=3, stdout="inactive\n"))
    assert service.running("voice") is False


def test_start_on_linux_keeps_the_bot_after_logout_or_says_how(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(service, "SYSTEMD_DIR", tmp_path)
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    monkeypatch.setattr(service.getpass, "getuser", lambda: "me")
    (tmp_path / "sleeptalk-voice.service").write_text("x")
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["loginctl", "show-user"]:
            return NS(returncode=0, stdout="Linger=no\n")
        return NS(returncode=1 if command[:2] == ["loginctl", "enable-linger"] and refuse else 0, stdout="")
    monkeypatch.setattr(service.subprocess, "run", run)
    refuse = False
    assert service.start("voice") == ""
    assert ["loginctl", "enable-linger", "me"] in calls
    refuse = True
    assert "sudo loginctl enable-linger me" in service.start("voice")


def test_without_a_user_service_manager_the_bot_runs_in_the_background(tmp_path, monkeypatch):
    """A container or a root login without systemd --user: start falls back to a
    background process that running() sees and stop() ends."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(service, "SYSTEMD_DIR", tmp_path)
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    (tmp_path / "sleeptalk-voice.service").write_text("x")
    real_run = subprocess.run
    def run(command, **kwargs):
        if command[0] in {"systemctl", "loginctl"}:
            return NS(returncode=1, stdout="")
        return real_run(command, **kwargs)
    monkeypatch.setattr(service.subprocess, "run", run)
    # Stands in for `python -m sleeptalk.cli run --config voice.toml`: same markers, just waits.
    monkeypatch.setattr(service, "_command", lambda: [sys.executable, "-c", "import time; time.sleep(60)", "sleeptalk.cli"])
    config = tmp_path / "voice.toml"
    config.write_text("")
    note = service.start("voice", config, tmp_path)
    try:
        assert "background" in note
        assert service.running("voice") is True
        assert service.log_hint("voice", config) == str(service.background_log("voice"))
        pid = int((tmp_path / "state" / "sleeptalk" / "bots" / "voice.pid").read_text())
        # A PID that isn't this bot never counts as it.
        (tmp_path / "state" / "sleeptalk" / "bots" / "other.pid").write_text(f"{os.getpid()}\n")
        assert service.running("other") is False
    finally:
        service.stop("voice")
    assert service.running("voice") is False
    assert not _alive(pid)


def _alive(pid):
    out = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def test_preflight_names_what_makes_a_profile_unloadable(profile):
    # A browser delivery saved into a Discord bot profile once made it fail
    # with only "the profile or runtime binding is invalid".
    profile.write_text(profile.read_text() + '\n[transport]\ntype = "discord"\n\n[transport.options]\ndelivery = "local"\n')
    problem = service.preflight("voice", profile)["problems"][0]
    assert "can't be loaded" in problem and "delivery" in problem


def test_failure_says_why_a_bot_is_down_in_its_own_last_words(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(service, "LAUNCHD_LOG_DIR", tmp_path)
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    printed = {"state": "not running", "code": "0"}
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: NS(
        returncode=0, stdout=f"\tstate = {printed['state']}\n\tlast exit code = {printed['code']}\n"))
    (tmp_path / "voice.log").write_text(
        "sleeptalk: transport=discord agent=pi stt=x tts=y policy()\n"
        "Traceback (most recent call last):\n  File \"x.py\", line 1\n"
        "sleeptalk: Discord rejected this bot's token. Paste a new one.\n"
        "sleeptalk INFO: received SIGTERM\n")
    assert service.failure("voice") == "Discord rejected this bot's token. Paste a new one."
    # launchd never ran it: the log holds nothing new, so say what launchd means.
    printed["code"] = "78"
    assert "never ran" in service.failure("voice") and str(tmp_path / "voice.log") in service.failure("voice")
    printed["state"] = "running"
    assert service.failure("voice") is None
    (tmp_path / "voice.log").write_text("sleeptalk config has 2 problem(s):\n  - KEY_A missing\n  - KEY_B missing\n")
    printed.update(state="not running", code="0")
    assert service.failure("voice") == "sleeptalk config has 2 problem(s): KEY_A missing; KEY_B missing"
    (tmp_path / "voice.log").write_text("sleeptalk: shutdown complete\n")
    printed.update(state="not running", code="0")
    assert service.failure("voice") is None


def test_failure_on_linux_reads_the_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    def run(command, **kwargs):
        if command[:2] == ["systemctl", "--user"]:
            return NS(returncode=0, stdout="ActiveState=failed\n")
        return NS(returncode=0, stdout="sleeptalk: the call stopped: OSError: boom.\n")
    monkeypatch.setattr(service.subprocess, "run", run)
    assert service.failure("voice") == "the call stopped: OSError: boom."
