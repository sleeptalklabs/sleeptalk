"""Repository-wide forbidden-construction guard (r2-provider-construction
audit rework).

Every production STT/TTS construction must go through
``sleeptalk.provider_factory`` (or the registries it wraps). This test
scans production source for direct provider-class instantiation outside
the boundary modules, so a future caller cannot quietly reintroduce
direct construction.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: Modules allowed to name provider classes: the construction boundary
#: itself, provider implementations (their own definitions), and tests.
ALLOWED_PREFIXES = (
    "sleeptalk/provider_factory.py",
    "sleeptalk/defaults.py",  # registry factories
    "sleeptalk/providers.py",  # protocol/test-seam registry
    "sleeptalk/speech/",  # provider implementations define/instantiate
    "sleeptalk/stream_tts.py",  # CartesiaStreamTTS definition
    "tests/",  # tests
)

#: Direct constructor calls that indicate boundary bypass.
CONSTRUCTOR_PATTERN = re.compile(
    r"\b(DeepgramSTT|DeepgramStreamingSTT|CartesiaTTS|ElevenLabsTTS|"
    r"HumeTTS|PlayHTTTS|RimeTTS|CartesiaStreamTTS)\s*\("
)


def production_python_files() -> list[Path]:
    roots = [REPO / "sleeptalk"]
    files: list[Path] = []
    for root in roots:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts or path.name.startswith("test_"):
                continue
            files.append(path)
    return files


def test_no_direct_provider_construction_outside_boundary() -> None:
    violations: list[str] = []
    for path in production_python_files():
        relative = path.relative_to(REPO).as_posix()
        if any(relative.startswith(prefix) for prefix in ALLOWED_PREFIXES):
            continue
        source = path.read_text(encoding="utf-8")
        for match in CONSTRUCTOR_PATTERN.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            violations.append(f"{relative}:{line}: {match.group(0)}")
    assert not violations, (
        "Direct provider construction outside the provider_factory boundary "
        "(construct through sleeptalk.provider_factory):\n" + "\n".join(violations)
    )
