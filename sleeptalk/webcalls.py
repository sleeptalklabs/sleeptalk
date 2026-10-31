"""Web-call runtime entry registry (r2-runtime-standalone).

Package callers (``cli.py`` and ``detached_call.py``) resolve the package-owned
Web host through this registry. A deployment may register another compatible
runner at startup without making the installed package depend on a checkout.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

__all__ = ["WebCallUnavailable", "register_web_call", "resolve_web_call", "web_call_registered"]

_runner: Callable[..., Any] | None = None


class WebCallUnavailable(RuntimeError):
    """No web-call runtime is registered in this environment."""


def register_web_call(runner: Callable[..., Any]) -> None:
    """Register the web-call runtime entry point (server-side, at boot)."""
    global _runner
    _runner = runner


def web_call_registered() -> bool:
    return _runner is not None


def resolve_web_call() -> Callable[..., Any]:
    """The registered ``run_profile_web_call``-shaped callable.

    Raises :class:`WebCallUnavailable` with instructions when the current
    environment has no web-call runtime (e.g. the standalone package venv).
    """
    if _runner is None:
        for module_name, attr in (("sleeptalk.web_call", "run_profile_web_call"),):
            try:
                module = importlib.import_module(module_name)
            except (ImportError, ModuleNotFoundError):
                continue
            candidate = getattr(module, attr, None)
            if callable(candidate):
                register_web_call(candidate)
                break
    if _runner is None:
        raise WebCallUnavailable(
            "The web-call runtime is not available in this environment. "
            "Install sleeptalk with the web extra."
        )
    return _runner
