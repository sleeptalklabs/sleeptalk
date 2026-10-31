"""Workspace-aware front door for setup, settings, and calls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import webbrowser

from sleeptalk.config import ConfigError, SleeptalkConfig, load_resolved_config, load_runtime_config
from sleeptalk.home import announce, resolve_layout

_SURFACES = {"web", "terminal", "discord"}


def _selection_path(workspace: Path) -> Path:
    workspace = workspace.resolve()
    key = hashlib.sha256(str(workspace).encode()).hexdigest()
    return resolve_layout(cwd=workspace).root / "workspaces" / f"{key}.json"


def remember_profile(
    profile_path: Path, workspace: Path, *, surface: str | None = None
) -> None:
    """Atomically save this workspace's choice, independently of other workspaces."""
    if surface is not None and surface not in _SURFACES:
        raise ValueError(f"Unknown call destination: {surface}")
    workspace = workspace.resolve()
    path = _selection_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    contents = json.dumps({
        "workspace": str(workspace),
        "profile": str(profile_path.resolve()),
        "surface": surface,
    }) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _selection(workspace: Path) -> dict[str, object]:
    try:
        saved = json.loads(_selection_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(saved, dict) or saved.get("workspace") != str(workspace.resolve()):
        return {}
    return saved


def selected_profile(workspace: Path) -> Path | None:
    value = _selection(workspace).get("profile")
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_absolute() and path.is_file() else None


def selected_surface(workspace: Path) -> str | None:
    value = _selection(workspace).get("surface")
    return value if isinstance(value, str) and value in _SURFACES else None


def _read_profile(path: Path) -> SleeptalkConfig:
    """Read a profile for listing, picking, or editing.

    A stale runtime binding must not hide the profile or block ``sleeptalk
    settings``: saving there binds it again. Starting the call re-verifies.
    """
    return load_resolved_config(
        path, env={}, check_env=False, check_binaries=False, verify_binding=False
    )


def _workspace(config: SleeptalkConfig) -> Path | None:
    from sleeptalk.defaults import _profile_root

    value = config.agent.workspace_dir
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = _profile_root(config.source_path) / path
    return path.resolve()


def _require_terminal() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ConfigError([
            "Setup here needs an interactive terminal. Run sleeptalk in one (over SSH: "
            "ssh -t), or run sleeptalk settings --phone for a link to set it up from your phone."
        ])


def _setup(*, workspace: Path, profile: Path | None = None,
           settings: bool = False, surface: str | None = None) -> int:
    from sleeptalk.wizard import run_init

    _require_terminal()
    return run_init(
        profile=profile.stem if profile else None,
        settings=settings,
        surface=surface,
        workspace=str(workspace),
    )


#: The tracked template profile. It documents the format and is never a bot.
TEMPLATE_PROFILE = "example"
#: Selection value for "Create a new profile" in the workspace prompt.
NEW_PROFILE = "__new_profile__"


def _matching_profiles(layout, workspace: Path) -> list[tuple[Path, SleeptalkConfig]]:
    """Profiles in this layout that belong to the workspace, template excluded."""
    choices = []
    for candidate in sorted(layout.profiles_dir.glob("*.toml")):
        if candidate.stem == TEMPLATE_PROFILE:
            continue
        try:
            config = _read_profile(candidate)
        except ConfigError:
            continue
        if _workspace(config) == workspace:
            choices.append((candidate, config))
    return choices


def _profile_title(path: Path, config: SleeptalkConfig) -> str:
    return f"{path.stem}  ({config.agent.type}, {config.transport.type})"


def _has_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def settings_page_first(environ: dict[str, str] | None = None) -> bool:
    """Whether `sleeptalk` and `sleeptalk settings` open the settings page.

    Someone at a terminal with a browser on this computer gets the page. Over
    SSH, on a Linux machine with no display, or when no browser is set up, the
    terminal wizard stays: the page can't open where they are. Without a
    terminal (a script or a service) the launcher keeps its old behavior.
    """
    env = os.environ if environ is None else environ
    if not _has_terminal() or env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
        return False
    if sys.platform.startswith("linux") and not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return False
    try:
        webbrowser.get()
    except webbrowser.Error:
        return False
    return True


def _pick_profile(
    choices: list[tuple[Path, SleeptalkConfig]], *, default: Path | None, message: str
) -> Path | str | None:
    """One prompt: every workspace profile plus "Create a new profile".

    Returns the chosen path, ``NEW_PROFILE``, or None when the prompt was
    dismissed. The remembered profile starts under the pointer.
    """
    from sleeptalk.wizard import pick_profile

    _require_terminal()
    options = [(_profile_title(path, config), path) for path, config in choices]
    options.append(("Create a new profile", NEW_PROFILE))
    return pick_profile(message, options, default=default)


def run(*, settings: bool = False, surface: str | None = None) -> int:
    """Launch the current workspace.

    With no profiles at all, first-run setup starts immediately. Otherwise
    every launch lists the workspace's profiles plus "Create a new profile",
    with the last choice under the pointer, so switching and adding are
    always one keypress away. The choice is remembered as that default and as
    the profile ``sleeptalk settings`` edits. Without a terminal (a script or
    a service), the remembered profile launches directly. ``settings`` only
    asks when there is more than one profile to edit.
    """
    try:
        workspace = Path.cwd().resolve()
        layout = resolve_layout(cwd=workspace)
        if layout.tier == 3 or layout.untrusted_repo is not None:
            print(announce(layout))
        remembered = selected_profile(workspace)
        if remembered is not None and remembered.stem == TEMPLATE_PROFILE:
            # An older launcher could remember the template; forget it.
            remembered = None
        if remembered is not None and _workspace(_read_profile(remembered)) != workspace:
            raise ConfigError(["The saved profile belongs to a different workspace."])
        choices = _matching_profiles(layout, workspace)
        if not choices:
            return _setup(workspace=workspace, profile=None, settings=settings, surface=surface)
        profile = remembered
        if settings:
            ask = len(choices) > 1
            if not ask and profile is None:
                # The only profile is the one to edit.
                profile = choices[0][0]
                remember_profile(profile, workspace)
        else:
            ask = _has_terminal() or profile is None
        if ask:
            picked = _pick_profile(
                choices,
                default=remembered,
                message="Which profile should this workspace edit?" if settings
                else "Which profile should this workspace use?",
            )
            if picked is None:
                return 0
            if picked == NEW_PROFILE:
                return _setup(workspace=workspace, profile=None, settings=settings, surface=surface)
            profile = picked
            # Re-picking the same profile keeps its call destination; a
            # different profile starts from its own transport.
            kept = selected_surface(workspace) if profile == remembered else None
            remember_profile(profile, workspace, surface=kept)
        if settings or profile is None:
            return _setup(workspace=workspace, profile=profile, settings=settings, surface=surface)
        return launch(profile, surface=surface or selected_surface(workspace))
    except KeyboardInterrupt:
        return 0
    except (ConfigError, OSError, ValueError) as exc:
        print(f"sleeptalk: {exc}\nRun sleeptalk settings to change your setup.", file=sys.stderr)
        return 2


def launch(profile_path: Path, surface: str | None = None) -> int:
    """Start one call through the existing runtime lifecycle."""
    try:
        workspace = Path.cwd().resolve()
        saved = _read_profile(profile_path)
        if _workspace(saved) != workspace:
            raise ConfigError(["This profile belongs to a different workspace. Run sleeptalk there."])
        destination = surface or saved.transport.type
        if destination not in _SURFACES:
            raise ConfigError([f"Choose a browser, terminal, or Discord call, not {destination!r}."])
        if destination == "discord" and not saved.transport.guild_id:
            return _setup(workspace=workspace, profile=profile_path, surface="discord")

        from sleeptalk.setup import ensure_dependencies

        problems = ensure_dependencies(destination, repair=True)
        if problems:
            raise ConfigError(list(problems))
        config = load_runtime_config(profile_path, surface=destination)
        print(f"sleeptalk: {config.agent.name} · {destination} · {workspace}")
        print("Change your setup with sleeptalk settings.")
        if destination != "web":
            from sleeptalk.cli import _run_loaded_config

            return _run_loaded_config(profile_path, config, skip_checks=False)
        return _launch_web(profile_path, config)
    except KeyboardInterrupt:
        print("sleeptalk: call ended")
        return 0
    except Exception as exc:
        print(f"sleeptalk: {exc}\nRun sleeptalk settings to change your setup.", file=sys.stderr)
        return 2


def _open_call(url: str) -> None:
    print(f"Open your call: {url}", flush=True)
    try:
        opened = webbrowser.open(url)
    except (OSError, webbrowser.Error):
        opened = False
    if not opened:
        print("Open the link above in a browser on this computer.", flush=True)


def _launch_web(profile_path: Path, config: SleeptalkConfig) -> int:
    from sleeptalk.call_provider import CallContext, LOCAL_RENDEZVOUS_URL
    from sleeptalk.call_registry import ProfileIdentity, acquire_profile_lease, registry_root_for_config
    from sleeptalk.web_call import _context_for_profile
    from sleeptalk.webcalls import resolve_web_call

    identity = ProfileIdentity.from_config(
        config_path=str(profile_path), profile=config.agent.slug or profile_path.stem
    )
    lease = acquire_profile_lease(root=registry_root_for_config(profile_path), identity=identity)
    try:
        asyncio.run(resolve_web_call()(
            config_path=str(profile_path),
            rendezvous_url=LOCAL_RENDEZVOUS_URL,
            context=_context_for_profile(
                config, CallContext(agent_instance=config.agent.slug, conversation_id="", platform_user_id="")
            ),
            ttl_seconds=300,
            on_ready=_open_call,
            local=str(config.transport.options.get("delivery", "")).strip() != "tailnet",
            tailnet=str(config.transport.options.get("delivery", "")).strip() == "tailnet",
        ))
    finally:
        lease.close()
    return 0
