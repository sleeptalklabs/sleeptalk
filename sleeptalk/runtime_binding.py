"""Bind a voice profile to one host, directory, and adapter configuration."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess

from pydantic import BaseModel, ConfigDict, Field


class BindingError(ValueError):
    """A value-free binding failure suitable for an operator receipt."""


class RuntimeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int = Field(default=1, ge=1, le=1)
    host: str = Field(pattern=r"^[a-f0-9]{64}$")
    workspace: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime: str = Field(pattern=r"^[a-f0-9]{64}$")


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def host_identity() -> str:
    """Read only the OS machine identity, never account or login databases."""
    try:
        if platform.system() == "Linux":
            machine = Path("/etc/machine-id").read_text().strip()
            if not re.fullmatch(r"[a-fA-F0-9]{32}", machine):
                raise BindingError("The execution host has no stable machine identity.")
        elif platform.system() == "Darwin":
            result = subprocess.run(
                ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, timeout=5, check=True,
            )
            match = re.search(rb'"IOPlatformUUID"\s*=\s*"([A-Fa-f0-9-]+)"', result.stdout)
            if not match:
                raise BindingError("The execution host has no stable machine identity.")
            machine = match.group(1).decode()
        else:
            raise BindingError("Runtime binding is supported on macOS and Linux.")
        return _digest([platform.system(), machine, os.getuid()])
    except (OSError, subprocess.SubprocessError) as exc:
        raise BindingError("The execution host identity could not be verified.") from None


def canonical_workspace(value: str) -> Path:
    if not value:
        raise BindingError("Select the intended workspace explicitly.")
    try:
        path = Path(value).expanduser().resolve(strict=True)
        if not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
            raise BindingError("The intended workspace is not accessible.")
        return path
    except (OSError, RuntimeError, ValueError):
        raise BindingError("The intended workspace is not accessible.") from None


def _workspace_digest(path: Path, info: os.stat_result) -> str:
    # The path and inode name the folder; a folder replaced at the same path
    # gets a new inode. The disk number (st_dev) is left out: macOS renumbers
    # volumes at boot, so every bound profile looked replaced after a restart.
    return _digest([str(path), info.st_ino])


def _bound_before_disk_numbers_dropped(stored: str, path: Path, info: os.stat_result) -> bool:
    """Match a binding whose workspace digest also hashed the disk number."""
    disks = {info.st_dev}
    if platform.system() == "Darwin":
        # macOS numbers volumes makedev(1, n) in mount order.
        disks.update(0x01000000 + n for n in range(1024))
    return any(_digest([str(path), disk, info.st_ino]) == stored for disk in disks)


def bind(agent, *, host: str | None = None) -> RuntimeBinding:
    path = canonical_workspace(agent.workspace_dir)
    info = path.stat()
    runtime = agent.model_dump(exclude={"source_path", "name", "slug", "persona_instructions", "persona_file"})
    if runtime.get("auth") is None:
        runtime.pop("auth", None)
    if runtime["type"] in {"acp", "hermes", "hermes-acp", "hermes_acp", "hermes_gateway", "hermes-gateway"}:
        if runtime["options"].get("hermes_root") == "":
            runtime["options"].pop("hermes_root")
    runtime["workspace_dir"] = str(path)
    return RuntimeBinding(
        host=host or host_identity(),
        workspace=_workspace_digest(path, info),
        runtime=_digest(runtime),
    )


def verify(config, *, workspace: str | None = None) -> None:
    if workspace is not None:
        requested = canonical_workspace(workspace)
        configured = canonical_workspace(config.agent.workspace_dir)
        if requested != configured:
            raise BindingError("The profile belongs to a different workspace.")
    if config.binding is None:
        if workspace is not None:
            raise BindingError("This profile has no runtime binding; migrate it explicitly.")
        return
    actual = bind(config.agent)
    if actual.host != config.binding.host:
        raise BindingError("The profile belongs to a different host or service user.")
    if actual.workspace != config.binding.workspace:
        path = canonical_workspace(config.agent.workspace_dir)
        if not _bound_before_disk_numbers_dropped(config.binding.workspace, path, path.stat()):
            raise BindingError("The bound workspace has moved or been replaced.")
    if actual.runtime != config.binding.runtime:
        raise BindingError("The bound voice runtime or its permission settings changed.")


def toml(binding: RuntimeBinding) -> str:
    return "\n[binding]\nversion = 1\n" + "".join(
        # A workspace path can hold quotes or a line break: escape every value.
        f'{key} = {json.dumps(str(value), ensure_ascii=False)}\n' for key, value in binding.model_dump().items() if key != "version"
    )
