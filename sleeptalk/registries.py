"""Registries, not branches.

Every component kind (transport, agent, STT, TTS) has a registry keyed by its
config name. The builder looks the name up, passes that section's config, and
gets an instance. Adding a component requires only: a new class + a
registration. Zero edits to the builder or config loader.

Registrations also declare which env vars (secrets) they need and an optional
cheap connectivity check, so startup validation can name the exact missing
var and where to get it.
"""

from __future__ import annotations

import os
import shutil
from importlib import metadata
from contextvars import ContextVar
from weakref import WeakSet
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")
_LOADING_PLUGIN: ContextVar[bool] = ContextVar("sleeptalk_loading_plugin", default=False)

# factory(config_section, env) -> component instance.
Factory = Callable[[Any, dict[str, str]], Any]
# check(config_section, env) -> None; raises on connectivity failure.
ConnectivityCheck = Callable[[Any, dict[str, str]], Awaitable[None]]
# probe(config_section, env) -> probe_runtime fields. Must never send a prompt
# or start user work; used by `host probe-runtime` for non-built-in adapters.
ReadinessProbe = Callable[[Any, dict[str, str]], Awaitable[dict[str, Any]]]


class ComponentOptions(BaseModel):
    """Base for a component's private, strictly validated profile settings."""

    model_config = ConfigDict(extra="forbid")


def locate_binary(name: str, fallbacks: tuple[str, ...] = ()) -> str | None:
    """`name` on PATH, else the first executable among `fallbacks`."""
    found = shutil.which(name)
    if found:
        return found
    for candidate in fallbacks:
        expanded = os.path.expanduser(candidate)
        if os.sep in expanded:
            if os.access(expanded, os.X_OK) and not os.path.isdir(expanded):
                return expanded
        else:
            found = shutil.which(expanded)
            if found:
                return found
    return None


def _section_is_acp(section: Any, registration_name: str) -> bool:
    runtime = str(getattr(section, "runtime", "") or "").strip().lower()
    agent_type = str(getattr(section, "type", "") or registration_name).replace("-", "_")
    return runtime == "acp" or agent_type.endswith("_acp")


@dataclass(frozen=True)
class Registration:
    name: str
    factory: Factory
    # env var -> human hint for where to obtain it; or a callable taking the
    # component's config section (transports get the full SleeptalkConfig) and
    # returning such a dict (for config-named secret vars like token_env).
    required_env: dict[str, str] | Callable[[Any], dict[str, str]] = field(
        default_factory=dict
    )
    # Config fields (beyond the discriminator) this component understands.
    # Fields explicitly set outside this set are a hard config error.
    config_fields: frozenset[str] = frozenset()
    check: ConnectivityCheck | None = None
    # CLI binary the component launches (empty = none) — a fixed name or a
    # callable of the config section — plus how to install it.
    binary: str | Callable[[Any], str] = ""
    # Well-known install locations tried when the binary is not on PATH
    # (vendor installers like ~/.local/bin, which shells don't always export).
    binary_fallbacks: tuple[str, ...] = ()
    install_hint: str = ""
    python_module: str = ""
    python_extra: str = ""
    python_module_on_acp: bool = False
    # When set, decides per section whether the python module is needed at
    # all; otherwise the module is needed unless the section runs over ACP.
    python_module_when: Callable[[Any], bool] | None = None
    binary_install: tuple[str, ...] = ()
    # Alternative config names that resolve to this registration.
    aliases: tuple[str, ...] = ()
    options_schema: type[BaseModel] | None = None
    provider_defaults: dict[str, str] = field(default_factory=dict)
    normalize_model: Callable[[str], str] | None = None
    conversation_context: Callable[[Any, str], str] | None = None
    probe: ReadinessProbe | None = None

    def validate_options(self, options: dict[str, Any]) -> dict[str, Any]:
        if self.options_schema is None:
            if options:
                raise ValueError(f"{self.name!r} does not declare private options: {', '.join(sorted(options))}")
            return {}
        return self.options_schema.model_validate(options).model_dump(mode="json")

    def python_module_for(self, section: Any) -> str:
        if not self.python_module:
            return ""
        if self.python_module_when is not None:
            return self.python_module if self.python_module_when(section) else ""
        if self.python_module_on_acp or not _section_is_acp(section, self.name):
            return self.python_module
        return ""

    def find_binary(self, section: Any) -> str | None:
        """Path of the declared binary, on PATH or at a fallback, or None."""
        binary = self.binary_for(section)
        if not binary:
            return None
        return locate_binary(binary, self.binary_fallbacks)

    def required_env_for(self, section: Any) -> dict[str, str]:
        if callable(self.required_env):
            return dict(self.required_env(section))
        return dict(self.required_env)

    def binary_for(self, section: Any) -> str:
        if callable(self.binary):
            return self.binary(section)
        return self.binary


class Registry(Generic[T]):
    _instances: WeakSet = WeakSet()

    def __init__(self, kind: str, *, entry_point_group: str = "") -> None:
        self.kind = kind
        self.entry_point_group = entry_point_group
        self._registrations: dict[str, Registration] = {}
        self._loading_plugins: set[str] = set()
        self._loaded_plugins: set[str] = set()
        self._entry_points: tuple | None = None
        self._instances.add(self)

    def register(
        self,
        name: str,
        factory: Factory,
        *,
        required_env: dict[str, str] | Callable[[Any], dict[str, str]] | None = None,
        config_fields: frozenset[str] | set[str] = frozenset(),
        check: ConnectivityCheck | None = None,
        binary: str | Callable[[Any], str] = "",
        binary_fallbacks: tuple[str, ...] = (),
        install_hint: str = "",
        python_module: str = "",
        python_extra: str = "",
        python_module_on_acp: bool = False,
        python_module_when: Callable[[Any], bool] | None = None,
        binary_install: tuple[str, ...] = (),
        aliases: tuple[str, ...] | list[str] = (),
        options_schema: type[BaseModel] | None = None,
        provider_defaults: dict[str, str] | None = None,
        normalize_model: Callable[[str], str] | None = None,
        conversation_context: Callable[[Any, str], str] | None = None,
        probe: ReadinessProbe | None = None,
        replace: bool = False,
    ) -> None:
        key = name.strip().lower()
        if not key:
            raise ValueError(f"{self.kind} registration requires a name.")
        if options_schema is not None and options_schema.model_config.get("extra") != "forbid":
            raise ValueError("Component option schemas must forbid unknown fields; inherit ComponentOptions.")
        collisions = {key, *(a.strip().lower() for a in aliases)} & self._registrations.keys()
        if collisions and (not replace or _LOADING_PLUGIN.get()):
            raise ValueError(f"Cannot replace registered {self.kind}: {', '.join(sorted(collisions))}")
        registration = Registration(
            name=key,
            factory=factory,
            required_env=required_env if required_env is not None else {},
            config_fields=frozenset(config_fields),
            check=check,
            binary=binary,
            binary_fallbacks=tuple(binary_fallbacks),
            install_hint=install_hint,
            python_module=python_module,
            python_extra=python_extra,
            python_module_on_acp=python_module_on_acp,
            python_module_when=python_module_when,
            binary_install=tuple(binary_install),
            aliases=tuple(a.strip().lower() for a in aliases),
            options_schema=options_schema,
            provider_defaults=dict(provider_defaults or {}),
            normalize_model=normalize_model,
            conversation_context=conversation_context,
            probe=probe,
        )
        self._registrations[key] = registration
        for alias in registration.aliases:
            self._registrations[alias] = registration

    def canonical_name(self, name: str) -> str:
        return self.get(name).name

    def get(self, name: str) -> Registration:
        key = (name or "").strip().lower()
        self._load_plugin(key)
        registration = self._registrations.get(key)
        if registration is None:
            valid = ", ".join(sorted(self._registrations)) or "(none registered)"
            raise ValueError(
                f"Unknown {self.kind} '{name}'. Valid options: {valid}."
            )
        return registration

    def get_optional(self, name: str) -> Registration | None:
        key = (name or "").strip().lower()
        self._load_plugin(key)
        return self._registrations.get(key)

    def build(self, name: str, config_section: Any, env: dict[str, str]) -> Any:
        return self.get(name).factory(config_section, env)

    def names(self) -> list[str]:
        """Canonical names (aliases hidden) for error messages."""
        return sorted({r.name for r in self._registrations.values()} | {ep.name.lower() for ep in self._plugins()})

    def registrations(self) -> tuple[Registration, ...]:
        """Already registered components, without importing discovered plugins."""
        by_name = {r.name: r for r in self._registrations.values()}
        return tuple(by_name[name] for name in sorted(by_name))

    def _plugins(self):
        if not self.entry_point_group:
            return ()
        if self._entry_points is None:
            self._entry_points = tuple(metadata.entry_points(group=self.entry_point_group))
        return self._entry_points

    def _load_plugin(self, key: str) -> None:
        if key in self._loaded_plugins:
            return
        candidates = [ep for ep in self._plugins() if ep.name.strip().lower() == key]
        if not candidates:
            return
        if len(candidates) != 1:
            raise ValueError(f"Multiple installed plugins provide {self.kind} {key!r}.")
        if key in self._loading_plugins:
            raise ValueError(f"Recursive loading of {self.kind} plugin {key!r}.")
        if key in self._registrations:
            raise ValueError(f"Installed plugin conflicts with registered {self.kind} {key!r}.")
        previous = {
            registry: (dict(registry._registrations), set(registry._loaded_plugins))
            for registry in self._instances
        }
        self._loading_plugins.add(key)
        token = _LOADING_PLUGIN.set(True)
        try:
            candidates[0].load()()
            if key not in self._registrations:
                raise ValueError("entry point did not register its advertised name")
            self._loaded_plugins.add(key)
        except Exception as exc:
            for registry, (registrations, loaded) in previous.items():
                registry._registrations = registrations
                registry._loaded_plugins = loaded
            raise ValueError(f"Could not load {self.kind} plugin {key!r}: {exc}") from exc
        finally:
            _LOADING_PLUGIN.reset(token)
            self._loading_plugins.remove(key)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get_optional(name) is not None


TRANSPORTS: Registry = Registry("transport", entry_point_group="sleeptalk.transports")
AGENTS: Registry = Registry("agent", entry_point_group="sleeptalk.agents")
STT_PROVIDERS: Registry = Registry("stt provider", entry_point_group="sleeptalk.stt")
TTS_PROVIDERS: Registry = Registry("tts provider", entry_point_group="sleeptalk.tts")
