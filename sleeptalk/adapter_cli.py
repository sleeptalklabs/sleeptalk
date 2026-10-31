"""``sleeptalk adapter``: scaffold and test a bring-your-own agent.

    sleeptalk adapter new my-agent --kind command
    sleeptalk adapter test --config my-agent/sleeptalk.agent.toml --fixture
    sleeptalk adapter test --config my-agent/sleeptalk.agent.toml --live

See ``docs/bring-your-own-agent.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import shutil
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_CONFIG = 2

KINDS = ("command", "http", "acp", "bridge", "python")
LANGUAGES = ("python", "node")
PROFILE_NAME = "sleeptalk.agent.toml"
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")


@dataclass(frozen=True)
class Template:
    directory: str
    profile: str
    run_hint: str
    test_mode: str = "fixture"


TEMPLATES: dict[tuple[str, str], Template] = {
    ("command", "python"): Template(
        "command-python",
        '[agent]\ntype = "command"\nslug = "AGENT_NAME"\nworkspace_dir = "."\n\n'
        '[agent.options]\ncommand = ["python3", "agent.py"]\n'
        '# API keys the agent needs, by name. Values come from your environment or .env.\nenv = []\n',
        "Sleeptalk starts agent.py for you.",
    ),
    ("command", "node"): Template(
        "command-node",
        '[agent]\ntype = "command"\nslug = "AGENT_NAME"\nworkspace_dir = "."\n\n'
        '[agent.options]\ncommand = ["node", "agent.mjs"]\n'
        '# API keys the agent needs, by name. Values come from your environment or .env.\nenv = []\n',
        "Sleeptalk starts agent.mjs for you.",
    ),
    ("http", "python"): Template(
        "http-python",
        '[agent]\ntype = "http"\nslug = "AGENT_NAME"\n\n'
        '[agent.options]\nurl = "http://127.0.0.1:8765/sleeptalk/turn"\n'
        '# token_env = "AGENT_TOKEN"   # sent as Authorization: Bearer <value>\n',
        "Start the server first: python3 server.py",
    ),
    ("http", "node"): Template(
        "http-node",
        '[agent]\ntype = "http"\nslug = "AGENT_NAME"\n\n'
        '[agent.options]\nurl = "http://127.0.0.1:8765/sleeptalk/turn"\n'
        '# token_env = "AGENT_TOKEN"   # sent as Authorization: Bearer <value>\n',
        "Start the server first: node server.mjs",
    ),
    ("acp", "python"): Template(
        "acp-python",
        '[agent]\ntype = "command"\nslug = "AGENT_NAME"\nworkspace_dir = "."\n'
        '# pending asks the caller before tools run; deny refuses; auto approves.\npermission_mode = "pending"\n\n'
        '[agent.options]\nprotocol = "acp"\ncommand = ["python3", "agent.py"]\nenv = []\n',
        "Sleeptalk starts agent.py for you. Existing ACP agents only need the profile.",
        test_mode="live",
    ),
    ("bridge", "python"): Template(
        "bridge-python",
        '[agent]\ntype = "bridge"\nslug = "AGENT_NAME"\n\n'
        '[agent.options]\n# Use wss:// behind TLS anywhere but this machine.\n'
        'url = "ws://127.0.0.1:8766/v1/agent-bridge"\ntoken_env = "AGENT_BRIDGE_TOKEN"\n',
        "Start the server first: AGENT_BRIDGE_TOKEN=dev-token python3 server.py "
        "(and export AGENT_BRIDGE_TOKEN=dev-token for Sleeptalk)",
    ),
    ("bridge", "node"): Template(
        "bridge-node",
        '[agent]\ntype = "bridge"\nslug = "AGENT_NAME"\n\n'
        '[agent.options]\n# tcp:// works only on this machine; use tls://host:port with a certificate elsewhere.\n'
        'url = "tcp://127.0.0.1:8766"\ntoken_env = "AGENT_BRIDGE_TOKEN"\n',
        "Start the server first: AGENT_BRIDGE_TOKEN=dev-token node server.mjs "
        "(and export AGENT_BRIDGE_TOKEN=dev-token for Sleeptalk)",
    ),
    ("python", "python"): Template(
        "plugin-python",
        '[agent]\ntype = "AGENT_NAME"\nslug = "AGENT_NAME"\n\n'
        '[agent.options]\ngreeting = "You said"\n',
        "Install the package into Sleeptalk's environment first: uv pip install -e .",
    ),
}


def add_parser(sub: Any) -> None:
    parser = sub.add_parser("adapter", help="Scaffold and test your own agent adapter.")
    adapter_sub = parser.add_subparsers(dest="adapter_command", required=True)

    from sleeptalk.harness_versions import add_arguments

    versions = adapter_sub.add_parser("versions", help="Inspect harness compatibility and upstream releases without upgrading.")
    add_arguments(versions)

    new = adapter_sub.add_parser("new", help="Create a working agent you can edit.")
    new.add_argument("name", help="Adapter name: lowercase letters, digits, - or _.")
    new.add_argument("--kind", choices=KINDS, default="command",
                     help="command: a program over stdio (default). http: a web endpoint. "
                     "acp: an ACP agent. bridge: a remote Agent Bridge server. python: an installable plugin.")
    new.add_argument("--language", choices=LANGUAGES, default="python")
    new.add_argument("--dir", default=None, help="Target directory (default: ./<name>).")

    test = adapter_sub.add_parser("test", help="Check an agent against the Sleeptalk contract.")
    test.add_argument("--config", default=None, help="Profile whose [agent] section to test.")
    mode = test.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true",
                      help="Send a few short real prompts. Proves speech and abort; may spend tokens.")
    mode.add_argument("--fixture", action="store_true",
                      help="Send the scaffold's test prompts (hello, slow, hang, tool, permission, clarify).")
    test.add_argument("--json", action="store_true", help="Print one machine-readable JSON report.")


def run(args: argparse.Namespace) -> int:
    if args.adapter_command == "versions":
        from sleeptalk.harness_versions import run as versions
        return versions(args)
    if args.adapter_command == "new":
        return scaffold(args.name, kind=args.kind, language=args.language, directory=args.dir)
    if args.adapter_command == "test":
        mode = "live" if args.live else "fixture" if args.fixture else "offline"
        return test_adapter(args.config, mode=mode, as_json=args.json)
    return EXIT_CONFIG


# ---------------------------------------------------------------------------
# new
# ---------------------------------------------------------------------------


def _template_root() -> Path:
    return Path(str(resources.files("sleeptalk") / "templates" / "byo"))


def scaffold(name: str, *, kind: str, language: str, directory: str | None, out: Any = None) -> int:
    out = out or sys.stdout
    if not _NAME_RE.match(name):
        print("adapter name must start with a letter and use lowercase letters, digits, - or _", file=sys.stderr)
        return EXIT_CONFIG
    template = TEMPLATES.get((kind, language))
    if template is None:
        languages = sorted(lang for k, lang in TEMPLATES if k == kind)
        print(f"--kind {kind} is available in: {', '.join(languages)}", file=sys.stderr)
        return EXIT_CONFIG
    target = Path(directory or name).expanduser()
    if target.exists() and any(target.iterdir()):
        print(f"{target} already exists and is not empty; choose another --dir", file=sys.stderr)
        return EXIT_CONFIG
    module = name.replace("-", "_")
    source = _template_root() / template.directory
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = Path(str(path.relative_to(source)).replace("AGENT_MODULE", module))
        destination = target / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8")
        destination.write_text(
            text.replace("AGENT_MODULE", module).replace("AGENT_NAME", name),
            encoding="utf-8",
        )
        shutil.copymode(path, destination)
    profile = target / PROFILE_NAME
    profile.write_text(template.profile.replace("AGENT_NAME", name), encoding="utf-8")
    config = profile.as_posix()
    print(f"Created {kind} agent {name!r} in {target}/", file=out)
    print("", file=out)
    print("Next:", file=out)
    print(f"  1. {template.run_hint}", file=out)
    print(f"  2. sleeptalk adapter test --config {config} --{template.test_mode}", file=out)
    print("  3. Edit the marked reply function to call your model, then:", file=out)
    print(f"     sleeptalk adapter test --config {config} --live", file=out)
    print(f"  4. Copy the [agent] section from {PROFILE_NAME} into your Sleeptalk profile.", file=out)
    return EXIT_OK


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------


def load_agent_factory(config_path: str | None, *, announce: bool = True) -> tuple[str, Any]:
    """Resolve a profile and return (adapter label, fresh-agent factory)."""

    from sleeptalk.agent_environment import agent_environment
    from sleeptalk.agents.construction import build_agent
    from sleeptalk.builder import _load_env_from_config
    from sleeptalk.cli import _resolve_config_path
    from sleeptalk.config import load_resolved_config
    from sleeptalk.defaults import register_defaults

    register_defaults()
    path = _resolve_config_path(config_path, announce_resolution=announce)
    config = load_resolved_config(path, check_env=False, check_binaries=False)
    env = agent_environment(_load_env_from_config(config))
    env.pop(config.transport.token_env, None)
    agent_config = config.agent
    label = agent_config.type
    options = agent_config.options if isinstance(agent_config.options, dict) else {}
    if agent_config.type == "command" and options.get("protocol") == "acp":
        label = "command (acp)"

    def factory() -> Any:
        agent, _selection = build_agent(agent_config.model_copy(deep=True), dict(env))
        return agent

    return label, factory


def test_adapter(config_path: str | None, *, mode: str, as_json: bool, out: Any = None) -> int:
    from sleeptalk.config import ConfigError
    from sleeptalk.conformance.runner import run_adapter_test

    out = out or sys.stdout
    try:
        label, factory = load_agent_factory(config_path, announce=not as_json)
    except (ConfigError, ValueError, OSError) as exc:
        print(f"could not load the agent profile: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    report = asyncio.run(run_adapter_test(factory, adapter=label, mode=mode))  # type: ignore[arg-type]
    print(report.to_json() if as_json else report.format_text(), file=out)
    return EXIT_FAIL if report.failed else EXIT_OK


__all__ = ["add_parser", "run", "scaffold", "test_adapter"]
