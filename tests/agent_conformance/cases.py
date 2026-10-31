"""Pytest view of ``sleeptalk.conformance.cases``. Import these from a vendor card."""

from __future__ import annotations

from sleeptalk.conformance import cases as _cases

from ._outcomes import wrap_module

__all__ = [name for name in dir(_cases) if not name.startswith("__")]
wrap_module(globals(), _cases, __all__)
