"""Translate packaged conformance outcomes into pytest skip/fail."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any

import pytest

from sleeptalk.conformance.harness import ConformanceFailure, ConformanceSkip


def pytest_outcomes(fn: Callable[..., Any]) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def run_async(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except ConformanceSkip as exc:
                pytest.skip(str(exc))
            except ConformanceFailure as exc:
                pytest.fail(str(exc))

        return run_async

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ConformanceSkip as exc:
            pytest.skip(str(exc))
        except ConformanceFailure as exc:
            pytest.fail(str(exc))

    return run


def wrap_module(namespace: dict[str, Any], source: Any, names: list[str]) -> None:
    for name in names:
        value = getattr(source, name)
        namespace[name] = pytest_outcomes(value) if callable(value) and (
            name.startswith("case_") or name in {"skip_unless", "require", "missing"}
        ) else value
