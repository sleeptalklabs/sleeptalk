"""Load eval data files shipped with the sleeptalk package."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache
def evals_root() -> Path:
    return Path(__file__).resolve().parent / "data"


def invariant_spec_path() -> Path:
    return evals_root() / "invariants" / "session-health.json"


def public_manifest_path() -> Path:
    return evals_root() / "manifest.json"
