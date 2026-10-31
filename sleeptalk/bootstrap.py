"""Install a verified wheel into a new environment without replacing a runtime."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile


class BootstrapError(ValueError):
    pass


def inventory() -> dict:
    return {
        "schema_version": 1, "operation": "inventory", "status": "ready",
        "os": platform.system(), "architecture": platform.machine(),
        "python": platform.python_version(),
        "package_version": importlib.metadata.version("sleeptalk"),
        "uv": bool(shutil.which("uv")),
        "pip": bool(importlib.util.find_spec("pip")),
        "network": "unknown", "background_lifetime": "unknown",
        "service_access": "unknown", "durable_storage": "unknown",
    }


@contextmanager
def installation_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / ".install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def install_artifact(artifact: Path, sha256: str, *, surface: str, agent: str, root: Path | None = None) -> dict:
    if surface not in {"web", "terminal", "discord"} or not re.fullmatch(r"[a-f0-9]{64}", sha256):
        raise BootstrapError("Select a supported surface and the release's SHA-256 checksum.")
    if not artifact.is_file() or artifact.suffix != ".whl":
        raise BootstrapError("Provide the compatible release wheel as a local file.")
    from sleeptalk.defaults import register_defaults
    from sleeptalk.registries import AGENTS
    register_defaults()
    registration = next((r for r in AGENTS.registrations() if agent in (r.name, *r.aliases)), None)
    if registration is None:
        raise BootstrapError("Select a registered voice adapter.")
    extras = sorted({surface, "credentials"} | ({registration.python_extra.removeprefix("sleeptalk[").removesuffix("]")} if registration.python_extra else set()))
    extra_text = ",".join(extras)
    key = hashlib.sha256(f"{sha256}:{extra_text}".encode()).hexdigest()
    runtime_root = root or Path.home() / ".local" / "share" / "sleeptalk" / "runtimes"
    target = runtime_root / key
    with installation_lock(runtime_root):
        # Copy first, hash the copy, and install only those verified bytes.
        with tempfile.TemporaryDirectory(prefix=".artifact-", dir=runtime_root) as staging:
            wheel = Path(staging) / artifact.name
            shutil.copyfile(artifact, wheel)
            with wheel.open("rb") as stream:
                actual_sha = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual_sha != sha256:
                raise BootstrapError("The artifact checksum does not match the release.")
            receipt = {"schema_version": 1, "operation": "bootstrap", "status": "ready", "runtime_id": key, "artifact_sha256": sha256, "extras": extras}
            if (target / "complete.json").is_file():
                if json.loads((target / "complete.json").read_text()) != receipt:
                    raise BootstrapError("The installed artifact metadata is inconsistent.")
                try:
                    _run([str(target / "bin" / "python"), "-I", "-c", "from sleeptalk.cli import main"])
                except (OSError, subprocess.SubprocessError):
                    raise BootstrapError("The completed runtime failed its import check. Keep it for diagnosis and install a verified replacement artifact.") from None
                return receipt
            if target.exists():
                # An incomplete environment was never made available for calls.
                shutil.rmtree(target)
            uv = shutil.which("uv")
            try:
                if uv:
                    _run([uv, "--no-config", "venv", "--python", sys.executable, str(target)])
                    _run([uv, "--no-config", "pip", "install", "--python", str(target / "bin" / "python"), f"{wheel}[{extra_text}]"])
                else:
                    _run([sys.executable, "-m", "venv", str(target)])
                    _run([str(target / "bin" / "python"), "-m", "pip", "install", f"{wheel}[{extra_text}]"])
                _run([str(target / "bin" / "python"), "-I", "-c", "from sleeptalk.cli import main"])
                marker = target / ".complete.json.tmp"
                with marker.open("w") as stream:
                    stream.write(json.dumps(receipt, sort_keys=True))
                    stream.flush()
                    os.fsync(stream.fileno())
                marker.chmod(0o600)
                os.replace(marker, target / "complete.json")
            except (OSError, subprocess.SubprocessError):
                raise BootstrapError("Isolated installation failed. Retry the same artifact; existing runtimes remain unchanged.") from None
            return receipt


def _run(command: list[str]) -> None:
    subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=True, timeout=180)
