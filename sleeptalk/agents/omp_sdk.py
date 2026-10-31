"""Prepare the pinned OMP SDK runtime without inheriting ambient startup code."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import tempfile

from sleeptalk.harness_versions import omp_sdk_policy

SDK_VERSION = omp_sdk_policy()["recommended"]
SDK_VERSIONS = tuple(omp_sdk_policy()["verified"])


@dataclass
class SDKLaunch:
    args: list[str]
    env: dict[str, str]
    home: tempfile.TemporaryDirectory

    def close(self) -> None:
        self.home.cleanup()


def prepare(binary: str, workspace: str, provider: str, model: str,
            env: dict[str, str], mode: str, nonce: str, *, probe: bool) -> SDKLaunch:
    if mode not in {"pending", "deny"} or provider not in {"openai", "anthropic"} or not model:
        raise ValueError("OMP SDK requires an explicit native OpenAI or Anthropic model and pending/deny permissions")
    root = Path(binary).resolve().parent.parent
    try:
        package = json.loads((root / "package.json").read_text())
        supported = package.get("name") == "@oh-my-pi/pi-coding-agent" and package.get("version") in SDK_VERSIONS
    except (OSError, ValueError, AttributeError):
        supported = False
    if not supported or not (root / "src/sdk.ts").is_file():
        raise ValueError(f"Select the npm OMP SDK {SDK_VERSION} executable through pi_bin (verified versions: {', '.join(SDK_VERSIONS)}); the standalone compiled CLI cannot provide isolated SDK startup")
    bun = shutil.which("bun", path=env.get("PATH", ""))
    if not bun:
        raise ValueError("Install Bun 1.3.14 or newer on the selected runtime PATH for isolated OMP SDK startup")
    home = tempfile.TemporaryDirectory(prefix="sleeptalk-omp-")
    # Bun reads config and .env before evaluating our launcher. Override these
    # at the process boundary, including its inherited preload mechanisms.
    environment = {key: env[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "TMP", "TEMP") if key in env}
    key = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
    if key in env:
        environment[key] = env[key]
    environment.update(HOME=home.name, USERPROFILE=home.name,
                       PI_CODING_AGENT_DIR=str(Path(home.name) / ".omp/agent"),
                       XDG_CONFIG_HOME=str(Path(home.name) / ".config"),
                       XDG_CACHE_HOME=str(Path(home.name) / ".cache"),
                       SLEEPTALK_PERMISSION_MODE=mode, SLEEPTALK_PERMISSION_NONCE=nonce,
                       SLEEPTALK_ONBOARDING_PROBE="1" if probe else "0")
    config = Path(home.name) / "bunfig.toml"
    config.write_text("[install]\nauto = \"disable\"\n")
    args = [bun, "--no-env-file", "--no-install", f"--config={config}",
            str(Path(__file__).with_name("omp_sdk_launcher.mjs")), str(root), workspace, provider, model]
    return SDKLaunch(args, environment, home)
