"""System dependencies — the bare-VPS preflight.

sleeptalk runs on machines that were fresh five minutes ago. The runtime
needs three things pip can't fully provide: ffmpeg (audio transcode),
libopus (Discord voice codec), and davey (Discord's native DAVE encryption).
This module detects them, knows how to install them per package manager,
and stays silent when everything is already fine.

Kept pure and synchronous so the wizard, doctor, and tests all share it.
"""

from __future__ import annotations

import ctypes.util
import importlib.util
from importlib.metadata import requires
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import Requirement


def runtime_requirements(extra: str, *, core: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Concrete repair requirements from this installed release's metadata.

    Keep versions and Python/platform markers in pyproject.toml. Include only
    the requested extra and the core audio dependencies a repair must restore;
    installing an extra must never upgrade Sleeptalk or an agent SDK.
    """
    selected = []
    for value in requires("sleeptalk") or ():
        requirement = Requirement(value)
        marker = requirement.marker
        is_core = marker is None or marker.evaluate({"extra": ""})
        if is_core:
            if requirement.name not in core:
                continue
        elif not marker.evaluate({"extra": extra}):
            continue
        requirement.marker = None
        selected.append(str(requirement))
    return tuple(selected)


_CORE_AUDIO_PACKAGES = ("numpy", "onnxruntime", "soxr", "audioop-lts")

# python.org macOS builds do not search Homebrew's lib dir, so find_library
# stays None after `brew install opus`. These are the same paths load_opus
# already probes at runtime.
LIBOPUS_PATHS = (
    "/opt/homebrew/lib/libopus.dylib",
    "/usr/local/lib/libopus.dylib",
)


@dataclass(frozen=True)
class Dep:
    name: str
    purpose: str  # human, hearable: what breaks without it
    required: bool  # False = warn-only (degraded, not dead)


DEPS: tuple[Dep, ...] = (
    Dep("ffmpeg", "audio transcoding for playback", True),
    Dep("libopus", "Discord's voice codec — no voice without it", True),
    Dep("davey", "Discord's native voice encryption (DAVE)", False),
)


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def have_libopus() -> bool:
    if ctypes.util.find_library("opus") is not None:
        return True
    return any(Path(path).is_file() for path in LIBOPUS_PATHS)


def have_davey() -> bool:
    return importlib.util.find_spec("davey") is not None


TERMINAL_EXTRA_HINT = (
    "Terminal needs the sleeptalk[terminal] extra "
    "(pip install 'sleeptalk[terminal]')."
)
TERMINAL_EXTRA_PACKAGES = runtime_requirements("terminal", core=_CORE_AUDIO_PACKAGES)
TERMINAL_EXTRA_MODULES = ("sounddevice", "numpy", "pywebrtc_audio", "onnxruntime", "soxr")


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _all_modules_available(names: tuple[str, ...]) -> bool:
    return all(_module_available(name) for name in names)


def missing_terminal_extra() -> str | None:
    if _all_modules_available(TERMINAL_EXTRA_MODULES):
        return None
    return TERMINAL_EXTRA_HINT


LOCAL_EXTRA_HINT = (
    "Local speech needs the sleeptalk[local] extra "
    "(pip install 'sleeptalk[local]')."
)
LOCAL_EXTRA_PACKAGES = runtime_requirements("local", core=_CORE_AUDIO_PACKAGES)
LOCAL_EXTRA_MODULES = ("sherpa_onnx", "onnx", "numpy", "soxr")


def missing_local_extra() -> str | None:
    if _all_modules_available(LOCAL_EXTRA_MODULES):
        return None
    return LOCAL_EXTRA_HINT


def missing_streaming_extra() -> str | None:
    from importlib.metadata import PackageNotFoundError, version
    try:
        if _all_modules_available(("moonshine_voice",)) and version("moonshine-voice") == "0.1.5":
            return None
    except PackageNotFoundError:
        pass
    return "Moonshine Streaming needs sleeptalk[local-streaming] (moonshine-voice==0.1.5)."


def streaming_extra_install_command() -> list[str]:
    return python_packages_install_command(*LOCAL_EXTRA_PACKAGES, *runtime_requirements("local-streaming"))


def install_streaming_extra() -> str | None:
    run_install(streaming_extra_install_command())
    importlib.invalidate_caches()
    return missing_local_extra() or missing_streaming_extra()


def local_extra_install_command() -> list[str]:
    return python_packages_install_command(*LOCAL_EXTRA_PACKAGES)


def install_local_extra() -> str | None:
    extra = missing_local_extra()
    if extra is None:
        return None
    run_install(local_extra_install_command())
    importlib.invalidate_caches()
    return missing_local_extra()


def python_packages_install_command(*packages: str) -> list[str]:
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", sys.executable, *packages]
    return [sys.executable, "-m", "pip", "install", *packages]


def terminal_extra_install_command() -> list[str]:
    return python_packages_install_command(*TERMINAL_EXTRA_PACKAGES)


def install_terminal_extra() -> str | None:
    extra = missing_terminal_extra()
    if extra is None:
        return None
    run_install(terminal_extra_install_command())
    importlib.invalidate_caches()
    return missing_terminal_extra()



WEB_EXTRA_HINT = (
    "Browser calls need numpy, onnxruntime and soxr "
    "(pip install 'sleeptalk[web]' or reinstall sleeptalk)."
)
# Silero VAD + Smart Turn. Core dependencies since 2026-09; this repair covers
# environments installed before that. Without them capture degrades to RMS energy.
WEB_EXTRA_PACKAGES = runtime_requirements("web", core=_CORE_AUDIO_PACKAGES)
WEB_EXTRA_MODULES = ("numpy", "onnxruntime", "soxr")


def missing_web_extra() -> str | None:
    if _all_modules_available(WEB_EXTRA_MODULES):
        return None
    return WEB_EXTRA_HINT


def web_extra_install_command() -> list[str]:
    return python_packages_install_command(*WEB_EXTRA_PACKAGES)


def install_web_extra() -> str | None:
    extra = missing_web_extra()
    if extra is None:
        return None
    run_install(web_extra_install_command())
    importlib.invalidate_caches()
    return missing_web_extra()


DISCORD_EXTRA_HINT = (
    "Discord needs the sleeptalk[discord] extra "
    "(pip install 'sleeptalk[discord]')."
)
DISCORD_EXTRA_PACKAGES = runtime_requirements("discord", core=_CORE_AUDIO_PACKAGES)
DISCORD_EXTRA_MODULES = (
    "discord",
    "discord.ext.voice_recv",
    "nacl",
    "numpy",
    "onnxruntime",
    "soxr",
    *(("audioop",) if sys.version_info >= (3, 13) else ()),
)


def missing_discord_extra() -> str | None:
    if _all_modules_available(DISCORD_EXTRA_MODULES):
        return None
    return DISCORD_EXTRA_HINT


def discord_extra_install_command() -> list[str]:
    return python_packages_install_command(*DISCORD_EXTRA_PACKAGES)


def install_discord_extra() -> str | None:
    extra = missing_discord_extra()
    if extra is None:
        return None
    run_install(discord_extra_install_command())
    importlib.invalidate_caches()
    return missing_discord_extra()


_CHECKS = {"ffmpeg": have_ffmpeg, "libopus": have_libopus, "davey": have_davey}


def deps_for_surface(surface: str) -> tuple[Dep, ...]:
    """Native dependencies actually used by one invocation surface."""
    if surface.strip().lower() == "discord":
        return DEPS
    return ()


def ensure_required_for_surface(surface: str, *, repair: bool) -> list[str]:
    """Repair safe native dependencies, then return remaining required gaps."""
    selected = deps_for_surface(surface)
    missing = [dep for dep in selected if dep.required and not _CHECKS[dep.name]()]
    if repair and missing:
        package_manager = detect_package_manager()
        for dep in list(missing):
            command = install_command(dep.name, package_manager)
            if not can_auto_install_command(command):
                continue
            ok, _detail = run_install(command)
            if ok and _CHECKS[dep.name]():
                missing.remove(dep)
    return [f"{dep.name} missing — {manual_hint(dep.name)}" for dep in missing]


def can_auto_install_command(command: list[str] | None) -> bool:
    return bool(command) and command[0] != "sudo"


def missing_deps() -> list[Dep]:
    """Deps whose check fails on this machine, in DEPS order."""
    return [dep for dep in DEPS if not _CHECKS[dep.name]()]


def detect_package_manager() -> str | None:
    """'apt' | 'dnf' | 'brew' | None (we can't help -> print manual hints)."""
    for pm in ("apt-get", "dnf", "brew"):
        if shutil.which(pm):
            return {"apt-get": "apt"}.get(pm, pm)
    return None


def install_command(dep: str, pm: str | None) -> list[str] | None:
    """The one command that installs `dep`, or None if we don't know how.

    davey is a pip package — it installs into the *current* interpreter's
    environment regardless of OS package manager.
    """
    if dep == "davey":
        return [sys.executable, "-m", "pip", "install", "davey"]
    if dep == "ffmpeg":
        return {
            "apt": ["sudo", "apt-get", "install", "-y", "ffmpeg"],
            "dnf": ["sudo", "dnf", "install", "-y", "ffmpeg"],
            "brew": ["brew", "install", "ffmpeg"],
        }.get(pm or "")
    if dep == "libopus":
        return {
            "apt": ["sudo", "apt-get", "install", "-y", "libopus0", "libopus-dev"],
            "dnf": ["sudo", "dnf", "install", "-y", "opus", "opus-devel"],
            "brew": ["brew", "install", "opus"],
        }.get(pm or "")
    return None


def manual_hint(dep: str) -> str:
    """Fix-stating line when no package manager is available."""
    return {
        "ffmpeg": "install ffmpeg via your OS package manager (apt/dnf/brew)",
        "libopus": "install libopus (Debian/Ubuntu: sudo apt install libopus0 libopus-dev; macOS: brew install opus)",
        "davey": f"pip install davey into this environment ({sys.executable} -m pip install davey)",
    }[dep]


def run_install(cmd: list[str]) -> tuple[bool, str]:
    """Run an install command; returns (ok, tail-of-output for errors)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        return False, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return False, "install timed out after 10 minutes"
    if result.returncode == 0:
        return True, ""
    tail = (result.stderr or result.stdout).strip().splitlines()
    return False, tail[-1] if tail else f"exit code {result.returncode}"
