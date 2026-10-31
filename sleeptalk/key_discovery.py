"""Find a speech-capable key the user already has on this host.

An agent that runs on OpenRouter, Groq, Together, or Venice keeps its key
somewhere on the machine: in its environment, in its own dotenv file, or,
for Sleeptalk's "just a model" profiles, in Sleeptalk's credential store.
Those services also listen and speak, so the same key can give the user
voice with nothing new to sign up for.

Only places the user owns count: the process environment and the agents'
own files under the home directory. Hermes keeps inline keys for custom
providers in ``~/.hermes/config.yaml``, OpenClaw keeps its providers in
``~/.openclaw/openclaw.json``, and pi keeps logins in ``~/.pi/agent/auth.json``
and custom providers in ``models.json`` beside it, so those files are read too.
A ``!command`` credential in pi's models.json is never run. A project's ``.env`` is never a source,
because a cloned repository can ship one, and a key planted there would be
offered to the user as "yours" and carry their conversations through a
stranger's account. Dotenv files are read literally: ``${NAME}`` is not
expanded, so a file cannot alias another secret from the environment.

Discovery only ever reports *where* a key is, never its value. Reading the
value happens later, once the user has said yes, in ``key_reuse``.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from sleeptalk.speech.openai_audio import PRESETS

_REFERENCE = re.compile(r"credential:[0-9a-f]{32}")


def _own_file(path: Path) -> bool:
    """A regular file this user owns: another account's file is never read as "yours"."""
    try:
        info = path.stat()
    except OSError:
        return False
    import stat

    return stat.S_ISREG(info.st_mode) and (not hasattr(os, "getuid") or info.st_uid == os.getuid())


@dataclass(frozen=True)
class ReusableKey:
    provider: str
    env: str
    kind: str  # "environment" | "dotenv" | "credential" | "hermes_config" | "openclaw_config" | "pi_config"
    location: str  # a file path or a credential reference; never a value
    source: str  # what a person reads: "~/.hermes/.env", "the model key of profile 'default'"
    # Where inside the location the value sits: a variable name for a dotenv
    # file when it differs from ``env``, or a dotted path in a YAML file.
    field: str = ""

    def public(self) -> dict[str, str]:
        return {"provider": self.provider, "env": self.env, "source": self.source, "kind": self.kind}


# Hosts of the services that listen and speak; a custom provider pointing at
# one of them is that service, whatever the agent calls it.
_PRESET_HOSTS = {
    "openrouter.ai": "openrouter", "api.groq.com": "groq", "api.together.xyz": "together",
    "api.together.ai": "together", "api.venice.ai": "venice",
}


# Where discover() looks, in words a person reads before agreeing to it.
PLACES = ("the environment", "~/.hermes", "~/.openclaw", "~/.pi/agent", "~/.omp/agent",
          "Sleeptalk's own model profiles")


def provider_for_url(url: str) -> str:
    from urllib.parse import urlsplit

    host = (urlsplit(str(url).strip()).hostname or "").lower()
    return _PRESET_HOSTS.get(host, "")


def read_yaml_mappings(text: str) -> dict:
    """Nested mappings of scalars from a YAML file an agent wrote itself.

    Enough for Hermes's config: ``key: value`` lines and nested blocks by
    indentation. Lists and anything fancier are skipped, never guessed.
    """
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if stripped.startswith("- "):
            continue
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            stack = [(-1, root)]
        key, sep, value = stripped.partition(":")
        if not sep or not key.strip() or " " in key.strip():
            continue
        key = key.strip().strip("'\"")
        value = value.strip()
        if not value:
            child: dict = {}
            stack[-1][1][key] = child
            stack.append((indent, child))
        else:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            stack[-1][1][key] = value
    return root


def hermes_config_path(home: Path, environment: dict[str, str]) -> Path:
    return Path(environment.get("HERMES_HOME") or (home / ".hermes")) / "config.yaml"


def read_json5_document(text: str) -> dict:
    """OpenClaw's config is JSON5-flavoured JSON: comments and trailing commas allowed."""
    import json

    text = re.sub(r"(?m)^\s*//[^\n]*", "", text)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    document = json.loads(text, strict=False)
    return document if isinstance(document, dict) else {}


def pi_agent_dirs(home: Path, environment: dict[str, str]) -> list[tuple[Path, str]]:
    """pi's config directory (overridable) and oh-my-pi's, with their labels."""
    pi_dir = Path(environment.get("PI_CODING_AGENT_DIR") or (home / ".pi" / "agent"))
    return [
        (pi_dir, "~/.pi/agent" if pi_dir == home / ".pi" / "agent" else str(pi_dir)),
        (home / ".omp" / "agent", "~/.omp/agent"),
    ]


def _pi_keys(home: Path, environment: dict[str, str]) -> list[ReusableKey]:
    """pi and oh-my-pi: ``auth.json`` logins by provider name, and ``models.json``
    custom providers with a literal or ``$VAR`` key. Shell-command keys are skipped."""
    import json

    found: list[ReusableKey] = []
    for directory, label in pi_agent_dirs(home, environment):
        auth_path = directory / "auth.json"
        if _own_file(auth_path):
            try:
                auth = json.loads(auth_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                auth = {}
            for name, entry in (auth.items() if isinstance(auth, dict) else []):
                provider = str(name).strip().lower()
                if provider in PRESETS and isinstance(entry, dict) and entry.get("type") == "api_key" \
                        and isinstance(entry.get("key"), str) and entry["key"].strip():
                    found.append(ReusableKey(provider=provider, env=PRESETS[provider].env, kind="pi_config",
                                             location=str(auth_path), source=f"{label}/auth.json ({name})", field=f"{name}.key"))
        models_path = directory / "models.json"
        if _own_file(models_path):
            try:
                models = json.loads(models_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                models = {}
            providers = models.get("providers") if isinstance(models, dict) and isinstance(models.get("providers"), dict) else {}
            for name, block in providers.items():
                if not isinstance(block, dict):
                    continue
                base_url = str(block.get("baseUrl") or "")
                provider = provider_for_url(base_url)
                # A block's name counts only when it sets no endpoint of its
                # own; "openrouter" pointed at your gateway holds that
                # gateway's key, which must never go to openrouter.ai.
                if not provider and not base_url.strip() and str(name).strip().lower() in PRESETS:
                    provider = str(name).strip().lower()
                api_key = block.get("apiKey")
                if not provider or not isinstance(api_key, str) or not api_key.strip():
                    continue
                if api_key.startswith("!"):
                    continue  # a shell command: never run on the user's behalf
                variable = api_key[2:-1] if api_key.startswith("${") and api_key.endswith("}") else \
                    api_key[1:] if api_key.startswith("$") and not api_key.startswith("$$") else ""
                if variable:
                    if environment.get(variable, "").strip():
                        found.append(ReusableKey(provider=provider, env=PRESETS[provider].env, kind="environment",
                                                 location=variable, source="this host's environment", field=variable))
                else:
                    found.append(ReusableKey(provider=provider, env=PRESETS[provider].env, kind="pi_config",
                                             location=str(models_path), source=f"{label}/models.json (providers.{name})",
                                             field=f"providers.{name}.apiKey"))
    return found


def _openclaw_keys(home: Path, environment: dict[str, str]) -> list[ReusableKey]:
    """``models.providers.<id>`` in ``~/.openclaw/openclaw.json``: an inline key, a
    ``${VAR}`` placeholder, or a secret reference ``{"source": "env", "id": VAR}``."""
    path = home / ".openclaw" / "openclaw.json"
    if not _own_file(path):
        return []
    try:
        config = read_json5_document(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    models = config.get("models") if isinstance(config.get("models"), dict) else {}
    providers = models.get("providers") if isinstance(models.get("providers"), dict) else {}
    dotenv = home / ".openclaw" / ".env"
    dotenv_names = _dotenv_names(dotenv) if _own_file(dotenv) else set()
    found: list[ReusableKey] = []
    for name, block in providers.items():
        if not isinstance(block, dict):
            continue
        base_url = str(block.get("baseUrl") or block.get("base_url") or "")
        provider = provider_for_url(base_url)
        # The name counts only for a block with no endpoint of its own.
        if not provider and not base_url.strip() and str(name).strip().lower() in PRESETS:
            provider = str(name).strip().lower()
        if not provider:
            continue
        env = PRESETS[provider].env
        api_key = block.get("apiKey")
        variable = ""
        if isinstance(api_key, dict) and api_key.get("source") == "env" and isinstance(api_key.get("id"), str):
            variable = api_key["id"]
        elif isinstance(api_key, str) and api_key.startswith("${") and api_key.endswith("}"):
            variable = api_key[2:-1]
        if isinstance(api_key, str) and api_key.strip() and not variable:
            found.append(ReusableKey(provider=provider, env=env, kind="openclaw_config", location=str(path),
                                     source=f"~/.openclaw/openclaw.json (models.providers.{name})",
                                     field=f"models.providers.{name}.apiKey"))
        elif variable and variable in dotenv_names:
            found.append(ReusableKey(provider=provider, env=env, kind="dotenv", location=str(dotenv),
                                     source="~/.openclaw/.env", field=variable))
        elif variable and environment.get(variable, "").strip():
            found.append(ReusableKey(provider=provider, env=env, kind="environment", location=variable,
                                     source="this host's environment", field=variable))
    return found


def _hermes_blocks(config: dict) -> list[tuple[str, dict]]:
    """(dotted path, mapping) for every place Hermes keeps a provider."""
    blocks = []
    model = config.get("model")
    if isinstance(model, dict):
        blocks.append(("model", model))
    providers = config.get("providers")
    if isinstance(providers, dict):
        blocks.extend((f"providers.{name}", block) for name, block in providers.items() if isinstance(block, dict))
    return blocks


def _hermes_keys(home: Path, environment: dict[str, str]) -> list[ReusableKey]:
    path = hermes_config_path(home, environment)
    if not _own_file(path):
        return []
    try:
        config = read_yaml_mappings(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return []
    label = "~/.hermes/config.yaml" if path == home / ".hermes" / "config.yaml" else str(path)
    dotenv = path.parent / ".env"
    dotenv_names = _dotenv_names(dotenv) if _own_file(dotenv) else set()
    found: list[ReusableKey] = []
    for dotted, block in _hermes_blocks(config):
        base_url = str(block.get("base_url") or block.get("api") or "")
        provider = provider_for_url(base_url)
        # The provider name counts only for a block with no endpoint of its own.
        if not provider and not base_url.strip() and str(block.get("provider", "")).strip().lower() in PRESETS:
            provider = str(block["provider"]).strip().lower()
        if not provider:
            continue
        env = PRESETS[provider].env
        inline = str(block.get("api_key") or "").strip()
        key_env = str(block.get("key_env") or "").strip()
        if inline and not inline.startswith("${"):
            found.append(ReusableKey(provider=provider, env=env, kind="hermes_config", location=str(path),
                                     source=f"{label} ({dotted})", field=f"{dotted}.api_key"))
        elif inline.startswith("${") and inline.endswith("}") and inline[2:-1] in dotenv_names:
            found.append(ReusableKey(provider=provider, env=env, kind="dotenv", location=str(dotenv),
                                     source="~/.hermes/.env" if dotenv == home / ".hermes" / ".env" else str(dotenv), field=inline[2:-1]))
        elif key_env and key_env in dotenv_names:
            found.append(ReusableKey(provider=provider, env=env, kind="dotenv", location=str(dotenv),
                                     source="~/.hermes/.env" if dotenv == home / ".hermes" / ".env" else str(dotenv), field=key_env))
    return found


def dotenv_sources(home: Path, environment: dict[str, str]) -> list[tuple[Path, str]]:
    """Dotenv files agents keep keys in, all under the user's own home."""
    hermes_home = Path(environment.get("HERMES_HOME") or (home / ".hermes"))
    return [
        (hermes_home / ".env", "~/.hermes/.env" if hermes_home == home / ".hermes" else str(hermes_home / ".env")),
        (home / ".openclaw" / ".env", "~/.openclaw/.env"),
    ]


def _dotenv_names(path: Path) -> set[str]:
    """Variable names defined in a dotenv file; values are read and discarded."""
    try:
        from dotenv import dotenv_values

        values = dotenv_values(path, interpolate=False)
    except Exception:
        return set()
    return {name for name, value in values.items() if value}


def _profile_credentials(profiles_dir: Path) -> list[ReusableKey]:
    """Model-only profiles whose model vendor is one of the audio services."""
    found: list[ReusableKey] = []
    if not profiles_dir.is_dir():
        return found
    by_base = {preset.base_url.rstrip("/"): preset for preset in PRESETS.values()}
    for path in sorted(profiles_dir.glob("*.toml")):
        try:
            document = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        agent = document.get("agent") if isinstance(document.get("agent"), dict) else {}
        auth = agent.get("auth") if isinstance(agent.get("auth"), dict) else {}
        base_url = str(auth.get("base_url", "")).rstrip("/")
        reference = auth.get("credential_ref")
        preset = by_base.get(base_url)
        if agent.get("type") == "model" and preset and isinstance(reference, str) and _REFERENCE.fullmatch(reference):
            found.append(ReusableKey(provider=preset.name, env=preset.env, kind="credential", location=reference,
                                     source=f"the model key of profile '{path.stem}'"))
    return found


def discover(*, environment: dict[str, str] | None = None, home: Path | None = None,
             profiles_dir: Path | None = None) -> list[ReusableKey]:
    """Every reusable key on this host, one per provider, most trusted source first.

    Sleeptalk's own store wins (the key is already validated and private),
    then the process environment, then the agents' dotenv files.
    """
    environment = dict(os.environ) if environment is None else environment
    home = home or Path.home()
    if profiles_dir is None:
        from sleeptalk.host import resolve_layout

        profiles_dir = resolve_layout().profiles_dir
    candidates: list[ReusableKey] = list(_profile_credentials(profiles_dir))
    for preset in PRESETS.values():
        if environment.get(preset.env, "").strip():
            candidates.append(ReusableKey(provider=preset.name, env=preset.env, kind="environment",
                                          location=preset.env, source="this host's environment"))
    for path, label in dotenv_sources(home, environment):
        if not _own_file(path):
            continue
        names = _dotenv_names(path)
        for preset in PRESETS.values():
            if preset.env in names:
                candidates.append(ReusableKey(provider=preset.name, env=preset.env, kind="dotenv",
                                              location=str(path), source=label))
    # Hermes's custom providers (Venice through `provider: custom`, for one)
    # keep the key in config.yaml or under a variable name of their own.
    candidates.extend(_hermes_keys(home, environment))
    candidates.extend(_openclaw_keys(home, environment))
    candidates.extend(_pi_keys(home, environment))
    seen: set[str] = set()
    result: list[ReusableKey] = []
    for key in candidates:
        if key.provider not in seen:
            seen.add(key.provider)
            result.append(key)
    return result


def _literal_key(value: str) -> str:
    """A key as written in a config file, or "" when it is a placeholder or a command.

    pi's escapes: ``$$`` starts a literal ``$``, ``$!`` a literal ``!``.
    """
    if value.startswith("$$"):
        return value[1:]
    if value.startswith("$!"):
        return value[1:]
    if value.startswith(("$", "!")):
        return ""
    return value


def read_value(key: ReusableKey, *, environment: dict[str, str] | None = None, store=None) -> str:
    """The key's value, fetched at the moment of reuse. Never logged or returned in a receipt."""
    if key.kind == "environment":
        environment = dict(os.environ) if environment is None else environment
        return environment.get(key.field or key.env, "").strip()
    if key.kind in {"dotenv", "hermes_config", "openclaw_config", "pi_config"} and not _own_file(Path(key.location)):
        return ""
    if key.kind == "dotenv":
        from dotenv import dotenv_values

        return (dotenv_values(Path(key.location), interpolate=False).get(key.field or key.env) or "").strip()
    if key.kind in {"hermes_config", "openclaw_config", "pi_config"}:
        import json

        text = Path(key.location).read_text(encoding="utf-8")
        node: object = (read_yaml_mappings(text) if key.kind == "hermes_config"
                        else read_json5_document(text) if key.kind == "openclaw_config" else json.loads(text))
        for part in key.field.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        return _literal_key(str(node).strip()) if isinstance(node, str) else ""
    if key.kind == "credential":
        from sleeptalk.credential_store import CredentialStore

        return (store or CredentialStore()).get(key.location).strip()
    raise ValueError(f"unknown key source {key.kind!r}")
