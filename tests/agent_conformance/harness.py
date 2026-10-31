"""Pytest view of ``sleeptalk.conformance.harness``."""

from __future__ import annotations

from sleeptalk.conformance import harness as _harness

from ._outcomes import wrap_module

__all__ = [name for name in dir(_harness) if not name.startswith("__")]
wrap_module(globals(), _harness, __all__)
