"""sleeptalk eval — contract and session-health tiers."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sleeptalk.evals._paths import evals_root
from sleeptalk.evals.session_health import (
    analyze_session_health,
    load_jsonl,
    render_session_health,
)


def run_health(
    *,
    latency_log: Path,
    events_log: Path | None = None,
    minimum_turns: int = 1,
) -> int:
    result = analyze_session_health(
        load_jsonl(latency_log),
        load_jsonl(events_log) if events_log else None,
        minimum_turns=max(minimum_turns, 1),
    )
    print(render_session_health(result))
    return 0 if result.passed else 1


def run_contract(*, pytest_paths: list[str] | None = None) -> int:
    package_root = evals_root().parent
    tests_root = package_root / "tests"
    if not tests_root.is_dir():
        print(
            "sleeptalk eval contract requires a source checkout with sleeptalk/tests/. "
            "Run: uv run pytest sleeptalk/tests/test_voice_ux_standard.py",
            file=sys.stderr,
        )
        return 2

    targets = pytest_paths or [
        "tests/test_voice_ux_standard.py",
        "tests/test_session_health.py",
    ]
    command = [sys.executable, "-m", "pytest", *[str(package_root / target) for target in targets], "-q"]
    completed = subprocess.run(command, cwd=package_root, check=False)
    return int(completed.returncode)
