"""Item 2 (SLEEPTALK_HOME resolution), item 3 (file logging), service units."""

from __future__ import annotations

import json
import logging
import platform
import tomllib

import pytest

from sleeptalk import home, service
from sleeptalk.config import SleeptalkConfig
from sleeptalk.logging_setup import setup_file_logging


# ---------------------------------------------------------------------------
# home resolution tiers
# ---------------------------------------------------------------------------


def test_tier2_env_wins(tmp_path):
    layout = home.resolve_layout(cwd=tmp_path, env={"SLEEPTALK_HOME": str(tmp_path / "h")})
    assert layout.tier == 2
    assert layout.profiles_dir == tmp_path / "h" / "profiles"
    assert layout.env_path == tmp_path / "h" / ".env"
    assert "SLEEPTALK_HOME" in layout.reason


@pytest.fixture
def private_home(tmp_path, monkeypatch):
    monkeypatch.setattr(home, "DEFAULT_HOME", tmp_path / ".sleeptalk-home")
    return tmp_path / ".sleeptalk-home"


def test_untrusted_repo_context_is_named_and_skipped(tmp_path, private_home):
    profiles = tmp_path / "config" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "bot.toml").write_text("[agent]\n")
    layout = home.resolve_layout(cwd=tmp_path, env={})
    assert layout.tier == 4
    assert layout.root == private_home
    assert layout.untrusted_repo == tmp_path
    assert "sleeptalk trust" in home.announce(layout)


def test_trust_file_others_can_write_trusts_nothing(tmp_path, private_home):
    home.set_trusted(tmp_path, True)
    assert tmp_path.resolve() in home.trusted_repos()
    assert oct((private_home / home.TRUST_FILE).stat().st_mode & 0o777) == "0o600"
    (private_home / home.TRUST_FILE).chmod(0o622)
    assert home.trusted_repos() == frozenset()
    (private_home / home.TRUST_FILE).chmod(0o600)
    home.set_trusted(tmp_path, False)
    assert home.trusted_repos() == frozenset()


def test_cwd_env_never_feeds_the_agent(tmp_path, monkeypatch):
    from sleeptalk.config import _load_env

    monkeypatch.delenv("SLEEPTALK_HOME", raising=False)
    monkeypatch.delenv("NODE_OPTIONS", raising=False)
    cloned = tmp_path / "cloned"
    cloned.mkdir()
    (cloned / ".env").write_text("NODE_OPTIONS=--require ./x.js\n")
    profile_home = tmp_path / "h" / "profiles"
    profile_home.mkdir(parents=True)
    monkeypatch.chdir(cloned)
    assert "NODE_OPTIONS" not in _load_env(profile_home / "p.toml")


def test_tier3_repo_context_detected(tmp_path, private_home):
    profiles = tmp_path / "config" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "bot.toml").write_text("[agent]\n")
    nested = tmp_path / "server" / "app"
    nested.mkdir(parents=True)
    home.set_trusted(tmp_path, True)
    layout = home.resolve_layout(cwd=nested, env={})
    assert layout.tier == 3
    assert layout.root == tmp_path
    assert layout.profiles_dir == profiles
    assert "SLEEPTALK_HOME" in layout.reason  # names the override


def test_tier3_pyproject_detection(tmp_path, private_home):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\n[project.scripts]\nsleeptalk = "sleeptalk.cli:main"\n'
    )
    home.set_trusted(tmp_path, True)
    layout = home.resolve_layout(cwd=tmp_path, env={})
    assert layout.tier == 3
    assert "pyproject.toml" in layout.reason


def test_tier4_default_home(tmp_path, monkeypatch):
    monkeypatch.setattr(home, "DEFAULT_HOME", tmp_path / ".sleeptalk")
    layout = home.resolve_layout(cwd=tmp_path / "nowhere", env={})
    assert layout.tier == 4
    assert layout.profiles_dir == tmp_path / ".sleeptalk" / "profiles"


def test_tier3_never_crosses_home(tmp_path, monkeypatch):
    # a stray profiles dir ABOVE $HOME must not be detected from below it
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "config" / "profiles").mkdir(parents=True)
    (tmp_path / "config" / "profiles" / "x.toml").write_text("")
    cwd = tmp_path / "home" / "deep"
    cwd.mkdir(parents=True)
    monkeypatch.setattr(home, "DEFAULT_HOME", tmp_path / "home" / ".sleeptalk")
    layout = home.resolve_layout(cwd=cwd, env={})
    assert layout.tier == 4


def test_announce_names_tier_and_reason(tmp_path):
    layout = home.resolve_layout(cwd=tmp_path, env={"SLEEPTALK_HOME": str(tmp_path)})
    line = home.announce(layout)
    assert "tier 2" in line and str(tmp_path) in line


# ---------------------------------------------------------------------------
# item 3: file logging
# ---------------------------------------------------------------------------


def test_log_config_schema():
    config = SleeptalkConfig.model_validate({"log": {"level": "DEBUG"}})
    assert config.log.level == "DEBUG"
    assert SleeptalkConfig().log.level == "WARNING"


def test_file_logging_writes_and_rotates_config(tmp_path):
    path = setup_file_logging(logs_dir=tmp_path / "logs", slug="bot", level="INFO")
    logging.getLogger("sleeptalk-test").info("hello")
    assert path.is_file()
    from sleeptalk.logging_setup import BACKUP_COUNT, MAX_BYTES

    assert MAX_BYTES == 1024 * 1024 and BACKUP_COUNT == 1


# ---------------------------------------------------------------------------
# service units
# ---------------------------------------------------------------------------


def test_systemd_unit_contract(tmp_path):
    config = tmp_path / "profiles" / "bot.toml"
    config.parent.mkdir(parents=True)
    config.write_text("")
    unit = service.render_unit("bot", config, system="Linux")
    assert unit.kind == "systemd"
    assert "Restart=on-failure" in unit.contents
    assert "RestartPreventExitStatus=2" in unit.contents
    assert "0 = clean stop" in unit.contents  # SIGTERM/exit-0 note in comments
    assert '"run" "--config"' in unit.contents and str(config) in unit.contents
    assert unit.path.name == "sleeptalk-bot.service"


def test_launchd_unit_contract(tmp_path):
    config = tmp_path / "bot.toml"
    config.write_text("")
    unit = service.render_unit("bot", config, system="Darwin")
    assert unit.kind == "launchd"
    assert "SuccessfulExit" in unit.contents  # only restart after crashes
    # config errors exit 0 under launchd, out of KeepAlive's reach
    assert "<key>SLEEPTALK_SERVICE</key>" in unit.contents
    assert str(config) in unit.contents


def test_unsupported_platform_errors(tmp_path):
    config = tmp_path / "bot.toml"
    config.write_text("")
    with pytest.raises(ValueError, match="unsupported on Windows"):
        service.render_unit("bot", config, system="Windows")


def _unit_path(tmp_path):
    if platform.system() == "Linux":
        return tmp_path / "sleeptalk-bot.service"
    return tmp_path / "ai.sleeptalk.bot.plist"


def test_install_uninstall_one_file(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "LAUNCHD_DIR", tmp_path)
    monkeypatch.setattr(service, "SYSTEMD_DIR", tmp_path)
    config = tmp_path / "bot.toml"
    config.write_text(
        "[transport]\ntype = \"null\"\n\n[agent]\ntype = \"pi\"\nslug = \"bot\"\n"
        f"workspace_dir = {json.dumps(str(tmp_path))}\n"
    )
    lines = []
    assert service.install("bot", config, yes=True, out=lines.append) == 0, lines
    path = _unit_path(tmp_path)
    assert path.is_file()
    assert any("About to write the service definition" in line for line in lines)
    assert any("This writes a definition only" in line for line in lines)
    assert service.uninstall("bot", yes=True, out=lines.append) == 0
    assert not path.exists()
    assert service.status("bot", out=lines.append) == 1


def test_install_declined_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "LAUNCHD_DIR", tmp_path)
    monkeypatch.setattr(service, "SYSTEMD_DIR", tmp_path)
    config = tmp_path / "bot.toml"
    config.write_text("")
    assert service.install("bot", config, yes=False, prompt=lambda _: "n") == 1
    assert not _unit_path(tmp_path).exists()


def test_quiet_library_loggers():
    from sleeptalk.logging_setup import quiet_library_loggers

    quiet_library_loggers()
    assert logging.getLogger("discord.ext.voice_recv").level == logging.WARNING
    assert logging.getLogger("discord.gateway").level == logging.WARNING
