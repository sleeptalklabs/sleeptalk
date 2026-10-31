from __future__ import annotations

import importlib.util
import os
import shlex
from dataclasses import dataclass
from typing import Any

from sleeptalk.harness_versions import cursor_sdk_install_spec, install_spec

EXTRA_PACKAGES: dict[str, tuple[str, ...]] = {
    "sleeptalk[claude]": (install_spec("claude_sdk"),),
    "sleeptalk[cursor]": (cursor_sdk_install_spec(),),
}


@dataclass(frozen=True)
class Need:
    kind: str
    key: str
    hint: str
    found: bool
    install_cmd: tuple[str, ...] = ()

    @property
    def command_text(self) -> str:
        """The install as a copy-pasteable line, or "" when there is none."""

        return shlex.join(self.install_cmd) if self.install_cmd else ""


@dataclass(frozen=True)
class AgentReadiness:
    name: str
    needs: tuple[Need, ...]

    @property
    def ready(self) -> bool:
        return all(need.found for need in self.needs)

    @property
    def missing(self) -> tuple[Need, ...]:
        return tuple(need for need in self.needs if not need.found)

    def menu_suffix(self) -> str:
        if not self.needs:
            return "ready"
        if self.ready:
            keys = ", ".join(need.key for need in self.needs)
            return f"ready ({keys})"
        found = [need.key for need in self.needs if need.found]
        missing = self.missing
        if found and missing:
            return f"{found[0]} found, {missing[0].hint}"
        return f"not installed — {missing[0].hint}"

    def missing_lines(self) -> list[str]:
        return [need.hint for need in self.missing]

    def installable(self) -> tuple[Need, ...]:
        return tuple(need for need in self.missing if need.install_cmd)


def extra_packages(extra: str) -> tuple[str, ...]:
    """Real installable distributions behind a `sleeptalk[...]` extra name.

    The extra itself is not resolvable from an index — sleeptalk is not
    published there — so anything shown to a human has to name these instead.
    """

    return EXTRA_PACKAGES.get(extra) or (extra,)


def extra_install_command(extra: str) -> tuple[str, ...]:
    from sleeptalk.sysdeps import python_packages_install_command

    return tuple(python_packages_install_command(*extra_packages(extra)))


def install_missing(report: AgentReadiness) -> list[str]:
    from sleeptalk.sysdeps import run_install

    lines: list[str] = []
    for need in report.installable():
        ok, detail = run_install(list(need.install_cmd))
        importlib.invalidate_caches()
        if ok:
            lines.append(f"installed {need.key}")
        else:
            lines.append(f"{need.key} install failed ({detail or 'unknown error'}). {need.hint}")
    return lines


def probe_registration(registration: Any, section: Any) -> AgentReadiness:
    """Check the dependencies declared by one resolved registration."""

    needs: list[Need] = []
    binary = registration.binary_for(section)
    if binary:
        needs.append(
            Need(
                kind="binary",
                key=binary,
                hint=registration.install_hint
                or f"install '{binary}' and put it on PATH",
                found=registration.find_binary(section) is not None,
                install_cmd=registration.binary_install,
            )
        )
    module = registration.python_module_for(section)
    if module:
        extra = registration.python_extra
        if extra:
            names = ", ".join(extra_packages(extra))
            hint = f"{module} is missing — needs {names}"
        elif not binary:
            hint = registration.install_hint or f"install the package that provides {module}"
        else:
            hint = f"install the package that provides {module}"
        needs.append(
            Need(
                kind="python_module",
                key=module,
                hint=hint,
                found=importlib.util.find_spec(module) is not None,
                install_cmd=extra_install_command(extra) if extra else (),
            )
        )
    return AgentReadiness(name=registration.name, needs=tuple(needs))


def probe(
    name: str,
    section: Any | None = None,
    *,
    env: dict[str, str] | None = None,
) -> AgentReadiness:
    """Resolve an agent exactly as construction does, then check that runtime."""

    from sleeptalk.agents.construction import resolve_agent
    from sleeptalk.config import AgentConfig

    resolved = section if section is not None else AgentConfig(type=name)
    resolved_env = dict(os.environ) if env is None else dict(env)
    if env is None and name.replace("-", "_") in {"cursor", "cursor_acp"}:
        from sleeptalk.defaults import _cursor_api_key_present

        if _cursor_api_key_present():
            resolved_env.setdefault("CURSOR_API_KEY", "available")
    construction = resolve_agent(resolved, resolved_env)
    return probe_registration(construction.registration, resolved)
