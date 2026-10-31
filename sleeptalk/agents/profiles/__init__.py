"""ACP launch profiles."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AcpProfile:
    name: str
    binary: str
    extra_args: tuple[str, ...] = ()
    extension_namespaces: frozenset[str] = frozenset()
    # Where the binary lands when the vendor installer puts it somewhere that
    # is not always on PATH, tried in order after PATH lookup fails.
    binary_fallbacks: tuple[str, ...] = ()
    # What to tell a human when the binary cannot be found at all.
    install_hint: str = ""


__all__ = ["AcpProfile"]
