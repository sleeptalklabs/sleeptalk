from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from sleeptalk import launcher
from sleeptalk.config import ConfigError


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "state"
    (home / "profiles").mkdir(parents=True)
    (home / "sleeptalk.toml").write_text('[transport]\ntype = "web"\n')
    monkeypatch.setenv("SLEEPTALK_HOME", str(home))
    monkeypatch.chdir(project)
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(launcher.sys.stdout, "isatty", lambda: True)
    return project


def profile(workspace: Path, name="voice", *, surface="web", directory=None) -> Path:
    path = launcher.resolve_layout().profiles_dir / f"{name}.toml"
    path.write_text(
        f'[agent]\ntype = "pi"\nname = "{name}"\nslug = "{name}"\n'
        f'workspace_dir = {json.dumps(str(directory or workspace))}\n'
        f'[transport]\ntype = "{surface}"\n'
    )
    return path


def test_first_run_opens_setup_for_current_workspace(workspace, monkeypatch):
    setup = Mock(return_value=0)
    monkeypatch.setattr("sleeptalk.wizard.run_init", setup)
    assert launcher.run(surface="terminal") == 0
    setup.assert_called_once_with(
        profile=None, settings=False, surface="terminal", workspace=str(workspace)
    )


def test_first_run_requires_a_terminal(workspace, monkeypatch, capsys):
    setup = Mock()
    monkeypatch.setattr("sleeptalk.wizard.run_init", setup)
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: False)
    assert launcher.run() == 2
    setup.assert_not_called()
    assert "Setup here needs an interactive terminal" in capsys.readouterr().err


def test_single_matching_profile_is_offered_with_create_and_adopted_without_secret_reads(workspace, monkeypatch):
    path = profile(workspace, "voice")
    profile(workspace, "another-project", directory=workspace.parent)
    prompt = Mock(return_value=Mock(ask=Mock(return_value=path)))
    monkeypatch.setattr("questionary.select", prompt)
    launch = Mock(return_value=0)
    monkeypatch.setattr(launcher, "launch", launch)
    monkeypatch.setattr("sleeptalk.config.resolve_credential_env", Mock(side_effect=AssertionError))
    monkeypatch.setattr("sleeptalk.config.collect_missing_binaries", Mock(side_effect=AssertionError))
    assert launcher.run() == 0
    launch.assert_called_once_with(path, surface=None)
    assert launcher.selected_profile(workspace) == path
    titles = [choice.title for choice in prompt.call_args.kwargs["choices"]]
    # The other workspace's profile is never offered here.
    assert titles == ["voice  (pi, web)", "Create a new profile"]
    assert prompt.call_args.kwargs["default"] is None


def test_every_launch_offers_the_profiles_with_the_last_choice_preselected(workspace, monkeypatch):
    profile(workspace, "first")
    selected = profile(workspace, "second")
    prompt = Mock(return_value=Mock(ask=Mock(return_value=selected)))
    monkeypatch.setattr("questionary.select", prompt)
    launch = Mock(return_value=0)
    monkeypatch.setattr(launcher, "launch", launch)
    assert launcher.run() == 0
    assert prompt.call_args.kwargs["default"] is None
    assert launcher.run() == 0
    assert prompt.call_count == 2
    assert launch.call_count == 2
    assert prompt.call_args.kwargs["default"].value == selected
    titles = [choice.title for choice in prompt.call_args.kwargs["choices"]]
    assert titles == ["first  (pi, web)", "second  (pi, web)", "Create a new profile"]
    assert launcher.selected_profile(workspace) == selected


def test_switching_profiles_starts_from_the_new_profile_transport(workspace, monkeypatch):
    first = profile(workspace, "first", surface="discord")
    second = profile(workspace, "second")
    launcher.remember_profile(first, workspace, surface="web")
    monkeypatch.setattr("questionary.select", Mock(return_value=Mock(ask=Mock(return_value=second))))
    launch = Mock(return_value=0)
    monkeypatch.setattr(launcher, "launch", launch)
    assert launcher.run() == 0
    launch.assert_called_once_with(second, surface=None)
    assert launcher.selected_profile(workspace) == second
    assert launcher.selected_surface(workspace) is None


def test_without_a_terminal_the_remembered_profile_launches_directly(workspace, monkeypatch):
    path = profile(workspace)
    profile(workspace, "second")
    launcher.remember_profile(path, workspace, surface="web")
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("questionary.select", Mock(side_effect=AssertionError("no prompt without a terminal")))
    launch = Mock(return_value=0)
    monkeypatch.setattr(launcher, "launch", launch)
    assert launcher.run() == 0
    launch.assert_called_once_with(path, surface="web")


def test_without_a_terminal_and_nothing_remembered_setup_is_refused(workspace, monkeypatch, capsys):
    profile(workspace)
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(launcher, "launch", Mock(side_effect=AssertionError))
    assert launcher.run() == 2
    assert "Setup here needs an interactive terminal" in capsys.readouterr().err


def test_template_profile_is_never_offered(workspace, monkeypatch):
    """A fresh clone ships config/profiles/example.toml; it is a template, not a bot."""
    profile(workspace, launcher.TEMPLATE_PROFILE)
    setup = Mock(return_value=0)
    monkeypatch.setattr("sleeptalk.wizard.run_init", setup)
    monkeypatch.setattr("questionary.select", Mock(side_effect=AssertionError("no prompt for a template")))
    assert launcher.run() == 0
    setup.assert_called_once_with(profile=None, settings=False, surface=None, workspace=str(workspace))
    assert launcher.selected_profile(workspace) is None


def test_remembered_template_selection_is_ignored(workspace, monkeypatch):
    path = profile(workspace, launcher.TEMPLATE_PROFILE)
    launcher.remember_profile(path, workspace)
    setup = Mock(return_value=0)
    monkeypatch.setattr("sleeptalk.wizard.run_init", setup)
    monkeypatch.setattr(launcher, "launch", Mock(side_effect=AssertionError))
    assert launcher.run() == 0
    setup.assert_called_once_with(profile=None, settings=False, surface=None, workspace=str(workspace))


def test_create_new_profile_choice_opens_first_run_setup(workspace, monkeypatch):
    profile(workspace, "voice")
    monkeypatch.setattr("questionary.select", Mock(return_value=Mock(ask=Mock(return_value=launcher.NEW_PROFILE))))
    setup = Mock(return_value=0)
    monkeypatch.setattr("sleeptalk.wizard.run_init", setup)
    monkeypatch.setattr(launcher, "launch", Mock(side_effect=AssertionError))
    assert launcher.run(surface="web") == 0
    setup.assert_called_once_with(profile=None, settings=False, surface="web", workspace=str(workspace))
    assert launcher.selected_profile(workspace) is None


def test_cancelled_selection_does_not_launch(workspace, monkeypatch):
    profile(workspace, "first")
    profile(workspace, "second")
    monkeypatch.setattr("questionary.select", Mock(return_value=Mock(ask=Mock(return_value=None))))
    launch = Mock()
    monkeypatch.setattr(launcher, "launch", launch)
    assert launcher.run() == 0
    launch.assert_not_called()
    assert launcher.selected_profile(workspace) is None


def test_settings_edits_current_profile_without_launching(workspace, monkeypatch):
    path = profile(workspace)
    launcher.remember_profile(path, workspace, surface="web")
    setup = Mock(return_value=0)
    monkeypatch.setattr("sleeptalk.wizard.run_init", setup)
    monkeypatch.setattr("questionary.select", Mock(side_effect=AssertionError("one profile needs no prompt")))
    monkeypatch.setattr(launcher, "launch", Mock(side_effect=AssertionError))
    assert launcher.run(settings=True) == 0
    setup.assert_called_once_with(
        profile="voice", settings=True, surface=None, workspace=str(workspace)
    )


def test_settings_adopts_the_only_profile_when_nothing_is_remembered(workspace, monkeypatch):
    path = profile(workspace)
    setup = Mock(return_value=0)
    monkeypatch.setattr("sleeptalk.wizard.run_init", setup)
    monkeypatch.setattr("questionary.select", Mock(side_effect=AssertionError("one profile needs no prompt")))
    assert launcher.run(settings=True) == 0
    setup.assert_called_once_with(
        profile="voice", settings=True, surface=None, workspace=str(workspace)
    )
    assert launcher.selected_profile(workspace) == path


def test_settings_with_several_profiles_asks_which_to_edit(workspace, monkeypatch):
    first = profile(workspace, "first")
    second = profile(workspace, "second")
    launcher.remember_profile(first, workspace, surface="web")
    prompt = Mock(return_value=Mock(ask=Mock(return_value=second)))
    monkeypatch.setattr("questionary.select", prompt)
    setup = Mock(return_value=0)
    monkeypatch.setattr("sleeptalk.wizard.run_init", setup)
    monkeypatch.setattr(launcher, "launch", Mock(side_effect=AssertionError))
    assert launcher.run(settings=True) == 0
    assert prompt.call_args.args[0] == "Which profile should this workspace edit?"
    assert prompt.call_args.kwargs["default"].value == first
    setup.assert_called_once_with(
        profile="second", settings=True, surface=None, workspace=str(workspace)
    )
    assert launcher.selected_profile(workspace) == second


def test_one_shot_surface_keeps_remembered_default(workspace, monkeypatch):
    path = profile(workspace, surface="discord")
    launcher.remember_profile(path, workspace, surface="web")
    monkeypatch.setattr("questionary.select", Mock(return_value=Mock(ask=Mock(return_value=path))))
    launch = Mock(return_value=0)
    monkeypatch.setattr(launcher, "launch", launch)
    assert launcher.run(surface="terminal") == 0
    launch.assert_called_with(path, surface="terminal")
    assert launcher.run() == 0
    launch.assert_called_with(path, surface="web")
    assert launcher.selected_surface(workspace) == "web"


def test_selections_are_independent_and_use_canonical_workspace(workspace):
    first = profile(workspace, "first")
    second_workspace = workspace.parent / "second"
    second_workspace.mkdir()
    second = profile(workspace, "second", directory=second_workspace)
    alias = workspace.parent / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    launcher.remember_profile(first, alias, surface="web")
    launcher.remember_profile(second, second_workspace, surface="terminal")
    launcher.remember_profile(first, workspace, surface="web")
    assert launcher.selected_profile(workspace) == first
    assert launcher.selected_profile(alias) == first
    assert launcher.selected_profile(second_workspace) == second
    assert launcher.selected_surface(second_workspace) == "terminal"
    assert len(list(launcher._selection_path(workspace).parent.iterdir())) == 2


def test_stale_selection_recovers_to_setup(workspace, monkeypatch):
    path = profile(workspace)
    launcher.remember_profile(path, workspace)
    path.unlink()
    setup = Mock(return_value=0)
    monkeypatch.setattr("sleeptalk.wizard.run_init", setup)
    assert launcher.run() == 0
    assert setup.call_args.kwargs["profile"] is None


def test_saved_profile_moved_to_another_workspace_never_launches(workspace, monkeypatch, capsys):
    path = profile(workspace)
    launcher.remember_profile(path, workspace)
    profile(workspace, directory=workspace.parent)
    launch = Mock()
    monkeypatch.setattr(launcher, "launch", launch)
    assert launcher.run() == 2
    launch.assert_not_called()
    assert "different workspace" in capsys.readouterr().err


def test_relative_profile_workspace_uses_profile_root(workspace, monkeypatch):
    home = launcher.resolve_layout().root
    path = profile(workspace, directory=".")
    assert launcher._workspace(launcher._read_profile(path)) == home


def test_launch_refuses_other_workspace_before_installing(workspace, monkeypatch):
    path = profile(workspace, directory=workspace.parent)
    dependencies = Mock()
    monkeypatch.setattr("sleeptalk.setup.ensure_dependencies", dependencies)
    assert launcher.launch(path) == 2
    dependencies.assert_not_called()


def test_terminal_override_installs_then_loads_effective_transport(workspace, monkeypatch):
    path = profile(workspace)
    original = path.read_bytes()
    config = launcher._read_profile(path)
    installed = Mock(return_value=())
    monkeypatch.setattr("sleeptalk.setup.ensure_dependencies", installed)

    def load(path_arg, *, surface):
        installed.assert_called_once_with("terminal", repair=True)
        assert path_arg == path and surface == "terminal"
        return config

    monkeypatch.setattr(launcher, "load_runtime_config", load)
    run = Mock(return_value=0)
    monkeypatch.setattr("sleeptalk.cli._run_loaded_config", run)
    assert launcher.launch(path, surface="terminal") == 0
    run.assert_called_once_with(path, config, skip_checks=False)
    assert path.read_bytes() == original


def test_web_call_opens_local_browser_and_releases_lease(workspace, monkeypatch):
    from sleeptalk import call_registry

    path = profile(workspace)
    config = launcher._read_profile(path)
    monkeypatch.setattr("sleeptalk.setup.ensure_dependencies", lambda *a, **kw: ())
    monkeypatch.setattr(launcher, "load_runtime_config", lambda *a, **kw: config)
    opened = Mock(return_value=True)
    monkeypatch.setattr(launcher.webbrowser, "open", opened)
    calls = []

    async def web_call(**kwargs):
        calls.append(kwargs)
        with pytest.raises(call_registry.CallBusyError):
            call_registry.acquire_profile_lease(
                root=call_registry.registry_root_for_config(path),
                identity=call_registry.ProfileIdentity.from_config(config_path=str(path), profile=path.stem),
            )
        kwargs["on_ready"]("http://127.0.0.1:1234/call")

    monkeypatch.setattr("sleeptalk.webcalls.resolve_web_call", lambda: web_call)
    assert launcher.launch(path) == 0
    assert calls[0]["local"] is True
    assert calls[0]["config_path"] == str(path)
    # A local browser call has no chat identity; the ticket still needs valid identifiers.
    payload = calls[0]["context"].as_payload()
    assert payload["platformUserId"] == "local-user" and payload["conversationId"]
    opened.assert_called_once_with("http://127.0.0.1:1234/call")
    lease = call_registry.acquire_profile_lease(
        root=call_registry.registry_root_for_config(path),
        identity=call_registry.ProfileIdentity.from_config(config_path=str(path), profile=path.stem),
    )
    lease.close()


def test_discord_override_collects_missing_discord_setup(workspace, monkeypatch):
    path = profile(workspace)
    setup = Mock(return_value=0)
    monkeypatch.setattr("sleeptalk.wizard.run_init", setup)
    assert launcher.launch(path, surface="discord") == 0
    setup.assert_called_once_with(
        profile="voice", settings=False, surface="discord", workspace=str(workspace)
    )


def test_startup_error_has_settings_hint_and_no_traceback(workspace, monkeypatch, capsys):
    path = profile(workspace)
    monkeypatch.setattr("sleeptalk.setup.ensure_dependencies", lambda *a, **kw: ())
    monkeypatch.setattr(launcher, "load_runtime_config", Mock(side_effect=ConfigError(["Missing key"])))
    assert launcher.launch(path) == 2
    error = capsys.readouterr().err
    assert "Missing key" in error and "sleeptalk settings" in error
    assert "Traceback" not in error


def test_browser_unavailable_leaves_visible_call_link(monkeypatch, capsys):
    monkeypatch.setattr(launcher.webbrowser, "open", Mock(return_value=False))
    launcher._open_call("http://127.0.0.1:1234/call")
    assert "http://127.0.0.1:1234/call" in capsys.readouterr().out


def test_stale_binding_still_lists_and_opens_settings(workspace, monkeypatch):
    """A profile bound to a directory that was since replaced is not a dead end."""
    path = profile(workspace, "voice")
    path.write_text(
        path.read_text()
        + '[binding]\nversion = 1\nhost = "' + "0" * 64 + '"\nworkspace = "'
        + "1" * 64 + '"\nruntime = "' + "2" * 64 + '"\n'
    )
    setup = Mock(return_value=0)
    monkeypatch.setattr("sleeptalk.wizard.run_init", setup)
    monkeypatch.setattr("questionary.select", Mock(return_value=Mock(ask=Mock(return_value=path))))

    assert launcher.run(settings=True) == 0

    setup.assert_called_once_with(profile="voice", settings=True, surface=None, workspace=str(workspace))


def test_stale_binding_blocks_the_call_with_a_repair_hint(workspace, monkeypatch, capsys):
    path = profile(workspace, "voice")
    path.write_text(
        path.read_text()
        + '[binding]\nversion = 1\nhost = "' + "0" * 64 + '"\nworkspace = "'
        + "1" * 64 + '"\nruntime = "' + "2" * 64 + '"\n'
    )
    launcher.remember_profile(path, workspace)

    assert launcher.launch(path, surface="terminal") == 2

    err = capsys.readouterr().err
    assert "binding" in err.lower() or "host" in err.lower()
    assert "sleeptalk settings" in err


def test_the_settings_page_opens_only_where_a_browser_can(monkeypatch):
    monkeypatch.setattr(launcher, "_has_terminal", lambda: True)
    monkeypatch.setattr(launcher.webbrowser, "get", lambda: object())
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    assert launcher.settings_page_first({}) is True
    assert launcher.settings_page_first({"SSH_CONNECTION": "10.0.0.2 5 10.0.0.1 22"}) is False
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    assert launcher.settings_page_first({}) is False
    assert launcher.settings_page_first({"WAYLAND_DISPLAY": "wayland-0"}) is True

    def no_browser():
        raise launcher.webbrowser.Error("none")

    monkeypatch.setattr(launcher.webbrowser, "get", no_browser)
    assert launcher.settings_page_first({"DISPLAY": ":0"}) is False
    monkeypatch.setattr(launcher.webbrowser, "get", lambda: object())
    monkeypatch.setattr(launcher, "_has_terminal", lambda: False)
    assert launcher.settings_page_first({"DISPLAY": ":0"}) is False


@pytest.mark.parametrize(
    ("argv", "browser", "expected"),
    [
        ([], True, ("page", "")),
        (["settings"], True, ("page", "")),
        (["settings", "--profile", "voice"], True, ("page", "voice")),
        (["settings", "--terminal"], True, ("wizard", True, None)),
        (["--terminal"], True, ("wizard", False, "terminal")),
        ([], False, ("wizard", False, None)),
        (["settings", "--open"], False, ("page", "")),
    ],
)
def test_sleeptalk_opens_the_settings_page_unless_asked_otherwise(monkeypatch, argv, browser, expected):
    from sleeptalk import cli, settings_page

    seen = []
    monkeypatch.setattr(launcher, "settings_page_first", lambda: browser)
    monkeypatch.setattr(settings_page, "run", lambda profile="": seen.append(("page", profile)) or 0)
    monkeypatch.setattr(launcher, "run", lambda settings=False, surface=None: seen.append(("wizard", settings, surface)) or 0)
    assert cli.main(argv) == 0
    assert seen == [expected]
