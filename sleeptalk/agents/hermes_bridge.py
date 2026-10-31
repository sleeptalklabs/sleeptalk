"""Launch the pinned Hermes library before ambient Python or Hermes startup."""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
import base64
from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
import tempfile

from pydantic import BaseModel, ConfigDict

from sleeptalk.harness_versions import inventory


class HermesOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hermes_root: str = ""


SOURCE_VERSIONS = json.loads(Path(__file__).with_name("hermes_bridge_versions.json").read_text())
HERMES_VERSION = inventory()["harnesses"]["hermes"]["recommended"]
HERMES_TAG = SOURCE_VERSIONS[HERMES_VERSION]["tag"]
HERMES_PYTHON = SOURCE_VERSIONS[HERMES_VERSION]["python"]
HERMES_REVISION = SOURCE_VERSIONS[HERMES_VERSION]["revision"]
HERMES_REPOSITORY = "https://github.com/NousResearch/hermes-agent.git"
SOURCE_HASHES = SOURCE_VERSIONS[HERMES_VERSION]["hashes"]
HERMES_SETUP_HINT = (
    f"clone it with `git clone --depth 1 --branch {HERMES_TAG} {HERMES_REPOSITORY} <dir>`, create a "
    f"virtual environment with `python{HERMES_PYTHON} -m venv <dir>/venv` and `<dir>/venv/bin/pip install -e '<dir>[anthropic]'`, then set pi_bin to that environment's "
    "python and options.hermes_root to <dir> (docs/bring-your-own-agent.md, Hermes)"
)


def source_version(root: Path) -> str:
    """Match all reviewed private seams from one release; reject mixed trees."""
    for version, record in SOURCE_VERSIONS.items():
        try:
            if all(hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected
                   for relative, expected in record["hashes"].items()):
                return version
        except OSError:
            pass
    raise ValueError(f"options.hermes_root is not an unmodified verified Hermes source; {HERMES_SETUP_HINT}")


@dataclass
class HermesLaunch:
    args: list[str]
    env: dict[str, str]
    home: tempfile.TemporaryDirectory

    def close(self):
        self.home.cleanup()


def prepare(agent, env: dict[str, str], *, transport: str, nonce: str = "") -> HermesLaunch:
    if agent.permission_mode not in {"pending", "deny"}:
        raise ValueError("Hermes onboarding requires pending or deny permissions")
    if agent.gateway_url or agent.gateway_session_id:
        raise ValueError("The isolated Hermes bridge creates a fresh local session; select no gateway URL or existing session")
    root = Path(str(agent.options.get("hermes_root", ""))).expanduser().resolve()
    python = Path(agent.pi_bin).expanduser().absolute()
    if not agent.pi_bin or not python.is_file() or not (python.parent.parent / "pyvenv.cfg").is_file():
        raise ValueError(f"Set pi_bin to the pinned Hermes virtual environment's python (Hermes {HERMES_VERSION}); {HERMES_SETUP_HINT}")
    if not isinstance(agent.options.get("hermes_root"), str) or not agent.options["hermes_root"].strip():
        raise ValueError(f"Set options.hermes_root to an unmodified Hermes {HERMES_VERSION} checkout ({HERMES_TAG}); {HERMES_SETUP_HINT}")
    selected_version = source_version(root)
    sites = sorted((python.parent.parent / "lib").glob("python*/site-packages"))
    if len(sites) != 1:
        raise ValueError("Hermes needs exactly one selected virtual environment site-packages directory")
    workspace = Path(agent.workspace_dir).resolve(strict=True)
    if not workspace.is_dir() or not agent.model:
        raise ValueError("Hermes requires an explicit existing workspace and model")
    from sleeptalk.agents.provider_auth import ProviderSelection
    explicit = ProviderSelection.from_environment(env)
    selected = None if explicit is None else {"mode": explicit.mode, "protocol": explicit.protocol, "base_url": explicit.base_url}
    credential = env.get("SLEEPTALK_PROVIDER_CREDENTIAL", "")
    if selected is None:
        if agent.provider not in {"openai", "anthropic"}:
            raise ValueError("Select an explicit provider and private authentication configuration")
        anthropic = agent.provider == "anthropic"
        selected = {"mode": "api_key", "protocol": "anthropic-messages" if anthropic else "openai-completions",
                    "base_url": "https://api.anthropic.com" if anthropic else "https://api.openai.com/v1"}
        credential = env.get("ANTHROPIC_API_KEY" if anthropic else "OPENAI_API_KEY", "")
    if not credential or selected.get("protocol") not in {"anthropic-messages", "openai-completions", "openai-responses", "openai-codex-responses"}:
        raise ValueError("The selected Hermes provider credential or protocol is unavailable")
    home = tempfile.TemporaryDirectory(prefix="sleeptalk-hermes-")
    environment = {key: env[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "TMP", "TEMP") if key in env}
    config = {"version": selected_version, "workspace": str(workspace), "model": agent.model, "provider": agent.provider,
              "permission_mode": agent.permission_mode, "transport": transport, "nonce": nonce, **selected}
    environment.update(HOME=home.name, USERPROFILE=home.name, HERMES_HOME=home.name,
                       XDG_CONFIG_HOME=home.name, XDG_CACHE_HOME=home.name, HERMES_SAFE_MODE="1",
                       TERMINAL_ENV="local", TERMINAL_CWD=str(workspace),
                       SLEEPTALK_HERMES_CONFIG=json.dumps(config), SLEEPTALK_HERMES_CREDENTIAL=credential)
    args = [str(python), "-I", "-S", str(Path(__file__).with_name("hermes_bridge_runtime.py")), str(root), str(sites[0])]
    return HermesLaunch(args, environment, home)


def isolated(agent) -> bool:
    """True when the profile explicitly selects the pinned isolated runtime."""
    return bool(str(agent.options.get("hermes_root", "") or "").strip())


def protected_config(agent):
    """The bridge configuration, or None to run the installed Hermes over ACP.

    The installed Hermes is the default whatever its version. The pinned
    isolated runtime is an explicit choice through options.hermes_root; it and
    explicit model authentication require permissions that keep approvals.
    """
    protected = agent.permission_mode in {"pending", "deny"}
    explicit_auth = getattr(agent, "auth", None) is not None
    if not protected and (explicit_auth or isolated(agent)):
        raise ValueError("Explicit Hermes authentication and isolated installation require pending or deny permissions")
    if explicit_auth and not isolated(agent):
        raise ValueError("Explicit Hermes authentication needs the isolated installation: set options.hermes_root")
    return agent if isolated(agent) else None


async def _inspect(agent, launch: HermesLaunch, nonce: str) -> None:
    from sleeptalk.agents.onboarding_rpc import _stdio
    response = await _stdio(launch.args, launch.home.name, launch.env,
                            {"jsonrpc": "2.0", "id": "onboarding", "method": "sleeptalk/probe", "params": {}})
    evidence = response.get("result", {})
    if evidence.get("nonce") != nonce or evidence.get("credential_bound") is not True or evidence.get("workspace") != str(Path(agent.workspace_dir).resolve()) or evidence.get("model") != agent.model or evidence.get("permission_mode") != agent.permission_mode:
        raise ValueError("Hermes runtime did not preserve the selected session binding")


async def probe(agent, env: dict[str, str], transport: str):
    import secrets
    from sleeptalk.agents.provider_auth import ProviderSelection, verify
    nonce = secrets.token_hex(16)
    launch = prepare(agent, env, transport=transport, nonce=nonce)
    try:
        await _inspect(agent, launch, nonce)
        config = json.loads(launch.env["SLEEPTALK_HERMES_CONFIG"])
        selected_env = {"SLEEPTALK_PROVIDER_AUTH": json.dumps({k: config[k] for k in ("mode", "protocol", "base_url")}),
                        "SLEEPTALK_PROVIDER_CREDENTIAL": launch.env["SLEEPTALK_HERMES_CREDENTIAL"]}
        selection = ProviderSelection.from_environment(selected_env)
        login = await verify(selection, agent.model)
        return {"status": "ready" if login == "available" else "needs_input", "protocol": "available",
                "workspace": "available", "permissions": "available", "login": login,
                "next_action": "" if login == "available" else "The isolated Hermes runtime is bound. Validate the selected provider credential and model privately, then retry."}
    finally:
        launch.close()


async def probe_configuration(agent, auth: Mapping[str, str]) -> dict[str, str]:
    """Check a protected install before encrypted entry of a real credential.

    The synthetic credential never leaves the owned startup process, whose
    initialization blocks network and subprocess IO. This is configuration
    evidence only; a real credential must pass probe() before profile creation.
    """
    import secrets
    from sleeptalk.agents.construction import resolve_agent
    from sleeptalk.config import SleeptalkConfig, _validate_component_choices

    result = {"status": "needs_input", "configuration": "unknown", "protocol": "unknown",
              "workspace": "unknown", "permissions": "unknown", "login": "unknown",
              "next_action": "Select the explicit protected Hermes installation before private provider entry."}
    launch = None
    try:
        if agent.permission_mode not in {"pending", "deny"} or agent.gateway_url or agent.gateway_session_id:
            raise ValueError("Configuration-only entry requires a fresh protected Hermes session")
        explicit = agent.runtime in {"acp", "gateway"} or agent.type in {"acp", "hermes_acp", "hermes-acp", "hermes_gateway", "hermes-gateway"}
        if not explicit:
            raise ValueError("Pin the intended Hermes ACP or gateway runtime before private provider entry")
        construction = resolve_agent(agent, {})
        name = construction.registration.name
        if name not in {"acp", "hermes_gateway"} or (construction.selection and construction.selection.fallback_from is not None):
            raise ValueError("Configuration-only entry supports the selected Hermes ACP or gateway bridge")
        if _validate_component_choices(SleeptalkConfig(agent=agent), env={}):
            raise ValueError("Correct the selected Hermes configuration before private provider entry")
        mode, protocol = auth.get("mode"), auth.get("protocol")
        if protocol == "openai-codex-responses":
            claim = base64.urlsafe_b64encode(json.dumps({"https://api.openai.com/auth": {
                "chatgpt_account_id": "sleeptalk-configuration-only"}}).encode()).decode().rstrip("=")
            credential = "synthetic." + claim + ".not-a-signature"
        elif protocol == "anthropic-messages":
            credential = "sk-ant-oat01-configuration-only" if mode == "oauth" else "sk-ant-api03-configuration-only"
        else:
            credential = "sleeptalk-configuration-only"
        environment = {"SLEEPTALK_PROVIDER_AUTH": json.dumps(dict(auth)),
                       "SLEEPTALK_PROVIDER_CREDENTIAL": credential}
        nonce = secrets.token_hex(16)
        launch = prepare(agent, environment, transport="acp" if name == "acp" else "gateway", nonce=nonce)
        async with asyncio.timeout(20):
            await _inspect(agent, launch, nonce)
        return {**result, "configuration": "available", "protocol": "available",
                "workspace": "available", "permissions": "available",
                "next_action": "Enter the selected provider credential privately. Authentication remains unverified until the real credential passes its provider probe."}
    except (ValueError, KeyError) as exc:
        return {**result, "next_action": str(exc)}
    except Exception:
        return {**result, "next_action": "The isolated Hermes configuration check failed or timed out; verify its pinned installation before private provider entry."}
    finally:
        if launch is not None:
            launch.close()
