"""Explicit user-service files and value-free installation checks.

Installation writes a definition only. Removal deletes a definition only.
Neither operation starts, stops, enables, or unloads an OS job; ``start`` and
``stop`` do that, and only when someone asks for it (the settings page's
Start and Stop buttons). On Linux, ``start`` also turns on lingering so the
job outlives the login session. On macOS, ``stop`` also deletes the
definition: launchd loads every file in LaunchAgents at login, and Login
Items keeps listing it.
"""
from __future__ import annotations

import getpass
import json
import os
import platform
import plistlib
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"
LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"
# Outside the profile folder: a file there can be locked to one app by macOS
# privacy labels (com.apple.macl), and launchd can't start a job whose log it
# can't open. One file per bot, so one bad file stops one bot.
LAUNCHD_LOG_DIR = Path.home() / "Library" / "Logs" / "sleeptalk"


@dataclass(frozen=True)
class Unit:
    path: Path
    contents: str
    kind: str
    hint: str


def _validate_slug(slug: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", slug):
        raise ValueError("Service slug must contain 1-64 lowercase letters, digits, hyphens, or underscores, starting with a letter or digit.")


def _safe_text(value: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Service paths and environment settings cannot contain control characters.")
    return value


def _command() -> list[str]:
    # Pin this installation instead of selecting another sleeptalk from PATH.
    # Keep the venv executable path: resolving its symlink would escape the venv.
    executable = os.path.abspath(sys.executable)
    _safe_text(executable)
    if not os.access(executable, os.X_OK):
        raise ValueError("The current Python executable is not available for a service.")
    return [executable, "-m", "sleeptalk.cli"]


def _launchd_command() -> list[str]:
    """The `sleeptalk` script beside this Python, so Login Items names the bot.

    macOS lists a launchd job by its program's file name: through /bin/sh it
    showed as "sh" from an unknown developer.
    """
    script = Path(os.path.abspath(sys.executable)).parent / "sleeptalk"
    if script.is_file() and os.access(script, os.X_OK):
        return [_safe_text(str(script))]
    return _command()


def launchd_log(slug: str) -> Path:
    return LAUNCHD_LOG_DIR / f"{slug}.log"


def _state_home(config_path: Path) -> Path:
    parent = config_path.parent
    if parent.name == "profiles":
        parent = parent.parent
        if parent.name == "config":
            parent = parent.parent
    return parent


def _service_path() -> str:
    paths = [str(Path(os.path.abspath(sys.executable)).parent)]
    paths.extend(part for part in os.environ.get("PATH", "").split(os.pathsep)
                 if part and Path(part).is_absolute() and Path(part).is_dir())
    paths.extend(["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    return _safe_text(os.pathsep.join(dict.fromkeys(paths)))


def _systemd_quote(value: str) -> str:
    value = _safe_text(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{value}"'


def render_systemd(slug: str, config_path: Path, *, workspace: Path | None = None) -> Unit:
    _validate_slug(slug)
    config_path = config_path.expanduser().resolve()
    working = workspace or _state_home(config_path)
    arguments = [*_command(), "run", "--config", str(config_path)]
    arguments[0] = ":" + arguments[0]
    command = " ".join(_systemd_quote(arg) for arg in arguments)
    contents = f"""[Unit]
Description=sleeptalk voice bot ({slug})
After=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
# Exit codes: 0 = clean stop (no restart), 2 = configuration error (no restart), anything else restarts.
ExecStart={command}
WorkingDirectory={_systemd_quote(str(working))}
Restart=on-failure
RestartPreventExitStatus=2
RestartSec=5
Environment={_systemd_quote('SLEEPTALK_HOME=' + str(_state_home(config_path)))}
Environment={_systemd_quote('PATH=' + _service_path())}
# Status lines (connected, command sync errors) reach the log as they're printed.
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""
    return Unit(
        path=SYSTEMD_DIR / f"sleeptalk-{slug}.service", contents=contents, kind="systemd",
        hint=(f"systemctl --user daemon-reload && systemctl --user enable --now sleeptalk-{slug}.service\n"
              f"For operation after logout, review: loginctl enable-linger {shlex.quote(getpass.getuser())}"),
    )


def render_launchd(slug: str, config_path: Path, *, workspace: Path | None = None) -> Unit:
    _validate_slug(slug)
    config_path = config_path.expanduser().resolve()
    working = workspace or _state_home(config_path)
    arguments = [*_launchd_command(), "run", "--config", str(config_path)]
    for value in [*arguments, str(working), str(config_path.parent)]:
        _safe_text(value)
    contents = plistlib.dumps({
        "Label": f"ai.sleeptalk.{slug}",
        "ProgramArguments": arguments,
        "WorkingDirectory": str(working),
        # PYTHONUNBUFFERED: status lines (connected, command sync errors) reach the log as printed.
        # SLEEPTALK_SERVICE: a configuration error exits 0, so launchd doesn't retry it forever.
        "EnvironmentVariables": {"SLEEPTALK_HOME": str(_state_home(config_path)), "PATH": _service_path(),
                                 "PYTHONUNBUFFERED": "1", "SLEEPTALK_SERVICE": "launchd"},
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 5,
        "StandardOutPath": str(launchd_log(slug)),
        "StandardErrorPath": str(launchd_log(slug)),
    }, sort_keys=False).decode("utf-8")
    path = LAUNCHD_DIR / f"ai.sleeptalk.{slug}.plist"
    return Unit(path, contents, "launchd", f"launchctl bootstrap gui/{os.getuid()} {shlex.quote(str(path))}")


def render_unit(slug: str, config_path: Path, *, system: str | None = None, workspace: Path | None = None) -> Unit:
    system = system or platform.system()
    if system == "Linux":
        return render_systemd(slug, config_path, workspace=workspace)
    if system == "Darwin":
        return render_launchd(slug, config_path, workspace=workspace)
    raise ValueError(f"Service installation is unsupported on {system}. Use a supported external supervisor.")


def unit_path_for(slug: str, *, system: str | None = None) -> Path:
    _validate_slug(slug)
    system = system or platform.system()
    if system == "Linux":
        return SYSTEMD_DIR / f"sleeptalk-{slug}.service"
    if system == "Darwin":
        return LAUNCHD_DIR / f"ai.sleeptalk.{slug}.plist"
    raise ValueError(f"User services are unsupported on {system}.")


def preflight(slug: str, config_path: Path) -> dict:
    """Check local file prerequisites without starting audio or probing login."""
    from sleeptalk.config import ConfigError, load_resolved_config
    from sleeptalk.credential_store import CredentialError, CredentialStore

    result = {
        "schema_version": 1, "operation": "service_preflight", "status": "blocked",
        "readiness_scope": "unit_file_only", "problems": [], "credentials": [],
        "service_context": {"workspace_access": "unknown", "credential_access": "unknown",
                            "adapter_authentication": "unknown", "reboot": "unknown",
                            "host_availability": "unknown"},
    }
    try:
        _validate_slug(slug)
        unit_path_for(slug)
        config_path = config_path.expanduser().resolve(strict=True)
        _safe_text(str(config_path))
        config = load_resolved_config(config_path, env={}, check_env=False, check_binaries=False)
        if config.agent.slug != slug:
            raise ValueError("The service slug does not match the selected profile.")
        if not config.agent.workspace_dir.strip():
            raise ValueError("Set an explicit agent.workspace_dir before installing a service.")
        workspace = Path(config.agent.workspace_dir).expanduser()
        if not workspace.is_absolute():
            workspace = _state_home(config_path) / workspace
        workspace = workspace.resolve(strict=True)
        if not workspace.is_dir() or not os.access(workspace, os.R_OK | os.X_OK):
            raise ValueError("The configured workspace is inaccessible in the current session.")
        _safe_text(str(workspace))
        if config.transport.type == "terminal":
            raise ValueError("Terminal audio needs an attached human session; unattended terminal service installation is unsupported.")
        result.update(workspace=str(workspace), config_path=str(config_path),
                      binding="verified_in_current_process" if config.binding is not None else "legacy_unbound",
                      python_executable=_command()[0], path=_service_path())
        refs = getattr(getattr(config, "credentials", None), "refs", {})
        store = CredentialStore()
        for field, reference in sorted(refs.items()):
            if not (store.root / "store.sqlite3").is_file():
                raise ValueError("A referenced credential store is unavailable in the current session.")
            metadata = store.status(reference)
            result["credentials"].append({"field": field, "reference": reference,
                "backend": metadata.backend, "available_in_current_process": metadata.available,
                "service_context": "unknown"})
            if not metadata.available:
                raise ValueError("A referenced credential is unavailable in the current session.")
        result["legacy_environment"] = "unknown" if not refs else "not_checked"
        result["status"] = "ready"
    except ConfigError as exc:
        # Name what is wrong: "the binding is invalid" alone sent people
        # looking at the binding when a setting was the problem.
        found = "; ".join(str(p) for p in getattr(exc, "problems", []) or []) or str(exc)
        result["problems"].append(f"The profile can't be loaded: {found}. Repair its configuration before service installation.")
    except CredentialError:
        result["problems"].append("Credential metadata could not be inspected in this user session.")
    except (ValueError, OSError) as exc:
        result["problems"].append(str(exc) if isinstance(exc, ValueError) else "A required local path is unavailable.")
    return result


def _stop_hint(slug: str, system: str) -> str:
    if system == "Linux":
        return f"systemctl --user disable --now sleeptalk-{slug}.service; systemctl --user daemon-reload"
    return f"launchctl bootout gui/{os.getuid()}/ai.sleeptalk.{slug}"


def install(slug: str, config_path: Path, *, yes: bool = False, out=print, prompt=input) -> int:
    report = preflight(slug, config_path)
    out(json.dumps(report, sort_keys=True))
    if report["status"] != "ready":
        return 1
    try:
        unit = render_unit(slug, config_path, workspace=Path(report["workspace"]))
        out(f"About to write the service definition: {unit.path}\n")
        out(unit.contents)
        out("This writes a definition only. Service login, credential access, and reboot behavior remain unverified.")
        if not yes and prompt("Write this file? [y/N] ").strip().lower() != "y":
            out("Aborted; nothing written.")
            return 1
        unit.path.parent.mkdir(parents=True, exist_ok=True)
        if unit.kind == "launchd":
            LAUNCHD_LOG_DIR.mkdir(parents=True, exist_ok=True)
        if unit.path.is_symlink():
            raise ValueError("Refusing to replace a symbolic-link service definition.")
        descriptor, temporary = tempfile.mkstemp(prefix=".sleeptalk-", dir=unit.path.parent)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(unit.contents)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, unit.path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        out(f"Wrote {unit.path}. No service was started or restarted.")
        out(f"Start it with: {unit.hint}")
        out(f"Remove the definition with: sleeptalk service uninstall {slug}")
        return 0
    except (ValueError, OSError) as exc:
        out(str(exc) if isinstance(exc, ValueError) else "Could not write the service definition; check directory access.")
        return 1


def uninstall(slug: str, *, yes: bool = False, out=print, prompt=input) -> int:
    try:
        path = unit_path_for(slug)
        out(f"Stopping or unloading the job is separate: {_stop_hint(slug, platform.system())}")
        out("Profiles, logs, runtime installations, and saved credentials are retained. Local deletion does not revoke provider keys.")
        if not path.exists() and not path.is_symlink():
            out(f"No definition exists at {path}. A previously loaded job may still run; inspect service status.")
            return 1
        if not yes and prompt(f"Delete only {path}? [y/N] ").strip().lower() != "y":
            out("Aborted; nothing deleted.")
            return 1
        path.unlink()
        out(f"Deleted the definition at {path}. Loaded-job state is unchanged and has not been verified.")
        return 0
    except (ValueError, OSError) as exc:
        out(str(exc) if isinstance(exc, ValueError) else "Could not remove the service definition; check directory access.")
        return 1


def status(slug: str, *, out=print) -> int:
    try:
        path = unit_path_for(slug)
        out(f"Definition: {'present' if path.is_file() else 'absent'} ({path})")
        if platform.system() == "Linux":
            command = ["systemctl", "--user", "show", f"sleeptalk-{slug}.service", "--property=LoadState,ActiveState,SubState,MainPID,Result", "--no-pager"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
            allowed = {"LoadState", "ActiveState", "SubState", "MainPID", "Result"}
            properties: dict[str, str] = {}
            for line in result.stdout.splitlines():
                key, separator, value = line.partition("=")
                if separator and key in allowed and re.fullmatch(r"[a-zA-Z0-9_-]+", value):
                    properties[key] = value
                    out(f"{key}={value}")
            if result.returncode:
                out("The user service manager could not report this job.")
                return 1
            # `systemctl show` exits 0 for a unit it has never loaded.
            if properties.get("LoadState", "loaded") != "loaded":
                out("No loaded job was reported for this slug.")
                return 1
            return 0
        result = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5, check=False)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) == 3 and fields[2] == f"ai.sleeptalk.{slug}":
                    if re.fullmatch(r"[0-9]+|-", fields[0]) and re.fullmatch(r"-?[0-9]+", fields[1]):
                        out(f"Loaded job: PID={fields[0]}, last_exit={fields[1]}")
                        if fields[0] == "-" and fields[1] == str(_LAUNCHD_SETUP_FAILED):
                            out("launchd couldn't start it (exit 78), so it never ran: most often it can't open its log file.")
                        out(f"Log: {launchd_log(slug)}")
                        return 0
            out("No loaded job was reported for this slug.")
        else:
            out("The user service manager could not report this job.")
        return 1
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        out(str(exc) if isinstance(exc, ValueError) else "The user service manager is unavailable; loaded-job state is unknown.")
        return 1


def _manager_env() -> dict[str, str]:
    """systemctl --user needs the user's runtime dir and bus. A process started
    outside a login session (an agent, cron, a service) often lacks both."""
    env = dict(os.environ)
    runtime = f"/run/user/{os.getuid()}"
    if not env.get("XDG_RUNTIME_DIR") and os.path.isdir(runtime):
        env["XDG_RUNTIME_DIR"] = runtime
    bus = os.path.join(env.get("XDG_RUNTIME_DIR", runtime), "bus")
    if not env.get("DBUS_SESSION_BUS_ADDRESS") and os.path.exists(bus):
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return env


def _manager(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=15, check=False, env=_manager_env())


# Where there's no user service manager (a container, root without a login
# session), the bot runs as a plain background process instead: it outlives the
# page and the login session, but not a reboot.
def _background_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "sleeptalk" / "bots"


def background_log(slug: str) -> Path:
    return _background_dir() / f"{slug}.log"


def _background_pid(slug: str) -> int | None:
    """The live background bot for this slug, if there is one."""
    try:
        pid = int((_background_dir() / f"{slug}.pid").read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    # A reused PID belongs to some other program: only ours counts.
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True,
                                 timeout=5, check=False).stdout
    return pid if "sleeptalk.cli" in command and f"{slug}.toml" in command else None


def _stop_background(slug: str) -> None:
    pid = _background_pid(slug)
    if pid is None:
        return
    os.kill(pid, signal.SIGTERM)  # the bot leaves its voice channel and exits
    for _attempt in range(40):
        if _background_pid(slug) is None:
            break
        time.sleep(0.25)
    else:
        os.kill(pid, signal.SIGKILL)
    (_background_dir() / f"{slug}.pid").unlink(missing_ok=True)


def _start_background(slug: str, config_path: Path, workspace: Path) -> None:
    config_path = config_path.expanduser().resolve()
    folder = _background_dir()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {**os.environ, "SLEEPTALK_HOME": str(_state_home(config_path)), "PATH": _service_path(), "PYTHONUNBUFFERED": "1"}
    log_fd = os.open(background_log(slug), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(log_fd, "ab") as log:
        process = subprocess.Popen([*_command(), "run", "--config", str(config_path)], cwd=workspace, env=env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True, close_fds=True)
    (folder / f"{slug}.pid").write_text(f"{process.pid}\n")


def running(slug: str) -> bool | None:
    """Whether the bot runs, as a job or in the background; None when the manager can't say."""
    try:
        _validate_slug(slug)
        if _background_pid(slug):
            return True
        if platform.system() == "Linux":
            result = _manager(["systemctl", "--user", "is-active", f"sleeptalk-{slug}.service"])
            return result.stdout.strip() in {"active", "activating", "reloading"}
        result = _manager(["launchctl", "list"])
        if result.returncode:
            return None
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[2] == f"ai.sleeptalk.{slug}":
                return fields[0].isdigit()
        return False
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return None


def _keep_after_logout() -> str:
    """Linux: a user service stops at logout unless lingering is on. Turn it on, or say how."""
    user = getpass.getuser()
    shown = _manager(["loginctl", "show-user", user, "--property=Linger"])
    if shown.stdout.strip() == "Linger=yes":
        return ""
    if _manager(["loginctl", "enable-linger", user]).returncode == 0:
        # Lingering starts the user's service manager; give its bus a moment.
        for _attempt in range(12):
            if _manager(["systemctl", "--user", "show-environment"]).returncode == 0:
                break
            time.sleep(0.25)
        return ""
    return (f"It stops when you log out of this computer. To keep it running, run once: "
            f"sudo loginctl enable-linger {shlex.quote(user)}")


def _start_job(slug: str, path: Path) -> str:
    if platform.system() == "Linux":
        note = _keep_after_logout()
        for command in (["systemctl", "--user", "daemon-reload"],
                        ["systemctl", "--user", "enable", f"sleeptalk-{slug}.service"],
                        ["systemctl", "--user", "restart", f"sleeptalk-{slug}.service"]):
            if _manager(command).returncode:
                raise ValueError("The user service manager didn't start the bot.")
        return note
    target = f"gui/{os.getuid()}/ai.sleeptalk.{slug}"
    # A loaded job keeps the definition it was loaded with: reload it so a
    # rewritten definition (a new token, a moved workspace) takes effect.
    if _manager(["launchctl", "print", target]).returncode == 0:
        _manager(["launchctl", "bootout", target])
    # bootout returns before the old job is gone; bootstrap fails until it is.
    for _attempt in range(10):
        if _manager(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)]).returncode == 0:
            return ""
        time.sleep(0.5)
    raise ValueError("The service manager didn't start the bot.")


def start(slug: str, config_path: Path | None = None, workspace: Path | None = None) -> str:
    """Load (or reload) an installed definition so the bot runs now and after login.

    Without a working service manager, and given the profile, the bot runs in
    the background instead. Returns a note for the person when something still
    needs their hand.
    """
    path = unit_path_for(slug)
    if not path.is_file():
        raise ValueError("Install the service definition first.")
    try:
        _stop_background(slug)
        try:
            return _start_job(slug, path)
        except (ValueError, OSError, subprocess.TimeoutExpired):
            if config_path is None or workspace is None:
                raise
        _start_background(slug, config_path, workspace)
        return ("There's no service manager for this user here, so it runs in the background: "
                "it keeps going after you log out, but start it again after this computer restarts.")
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("The service manager is unavailable.") from None


def stop(slug: str) -> None:
    """Stop the bot and keep it from starting at login.

    Linux keeps the disabled unit. macOS deletes the definition: launchd loads
    every file in LaunchAgents at login, so a kept one started the bot again.
    """
    _validate_slug(slug)
    linux = platform.system() == "Linux"
    command = (["systemctl", "--user", "disable", "--now", f"sleeptalk-{slug}.service"] if linux
               else ["launchctl", "bootout", f"gui/{os.getuid()}/ai.sleeptalk.{slug}"])
    try:
        _stop_background(slug)
        refused = _manager(command).returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        refused = True
    if refused and running(slug):
        raise ValueError("The service manager didn't stop the bot.")
    if not linux:
        unit_path_for(slug).unlink(missing_ok=True)


# launchd's "couldn't set up the job" code: it never started the program.
_LAUNCHD_SETUP_FAILED = 78


def failure(slug: str) -> str | None:
    """Why a bot that isn't running stopped, in its own last words; None when unknown."""
    try:
        _validate_slug(slug)
        if _background_pid(slug):
            return None
        if platform.system() == "Linux":
            shown = _manager(["systemctl", "--user", "show", f"sleeptalk-{slug}.service", "--property=ActiveState"])
            if shown.stdout.strip() in {"ActiveState=active", "ActiveState=activating"}:
                return None
            text = _manager(["journalctl", "--user", "-u", f"sleeptalk-{slug}.service", "-n", "40", "-o", "cat",
                             "--no-pager"]).stdout
            return _last_words(text)
        if background_log(slug).is_file():
            return _last_words(_tail(background_log(slug)))
        shown = _manager(["launchctl", "print", f"gui/{os.getuid()}/ai.sleeptalk.{slug}"])
        if shown.returncode == 0:
            if re.search(r"^\s*state = running$", shown.stdout, re.MULTILINE):
                return None
            code = re.search(r"^\s*last exit code = (\d+)", shown.stdout, re.MULTILINE)
            if code and int(code.group(1)) == _LAUNCHD_SETUP_FAILED:
                return (f"macOS couldn't start it, so it never ran. Most often it can't open its log file "
                        f"({launchd_log(slug)}): move that file aside, then start the bot again.")
        return _last_words(_tail(launchd_log(slug)))
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return None


def _tail(path: Path, size: int = 16_384) -> str:
    with path.open("rb") as log:
        log.seek(max(0, path.stat().st_size - size))
        return log.read().decode(errors="replace")


_NOT_A_REASON = re.compile(r"^(sleeptalk (DEBUG|INFO|WARNING):|sleeptalk: (shutdown complete|transport=)|"
                           r"\s|Traceback|shell-init:|job-working-directory:)")


def _last_words(text: str) -> str | None:
    """The last line the bot said about stopping: an error, not routine logging.

    A heading ending in a colon brings the "  - " items listed under it.
    """
    items: list[str] = []
    for line in reversed(text.splitlines()):
        if re.match(r"^\s+- ", line):
            items.insert(0, line.strip()[2:])
        elif line.strip() and not _NOT_A_REASON.match(line):
            line = line.strip().removeprefix("sleeptalk: ")
            if line.endswith(":") and items:
                line = f"{line} {'; '.join(items)}"
            return line if len(line) <= 300 else line[:299] + "…"
        elif line.strip():
            items = []
    return None


def log_hint(slug: str, config_path: Path) -> str:
    if background_log(slug).is_file() and (_background_dir() / f"{slug}.pid").is_file():
        return str(background_log(slug))
    if platform.system() == "Linux":
        return f"journalctl --user -u sleeptalk-{slug}.service -n 50"
    return str(launchd_log(slug))
