"""Fail if the sleeptalk package imports server/, app.*, or pipecat."""

from __future__ import annotations

import ast
from pathlib import Path

_FORBIDDEN_ROOTS = ("app", "server", "pipecat")


def package_root() -> Path:
    return Path(__file__).resolve().parent


def find_package_import_violations(root: Path | None = None) -> list[str]:
    root = root or package_root()
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "_import_boundary.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = _imported_module(node)
            if imported and _is_forbidden(imported):
                rel = path.relative_to(root)
                violations.append(f"sleeptalk/{rel}: {imported}")
    return violations


def _imported_module(node: ast.AST) -> str:
    if isinstance(node, ast.Import):
        return node.names[0].name
    if isinstance(node, ast.ImportFrom):
        return node.module or ""
    return ""


def _is_forbidden(module: str) -> bool:
    return (
        module in _FORBIDDEN_ROOTS
        or module.startswith("app.")
        or module.startswith("server.")
        or module.startswith("pipecat.")
    )


def main() -> int:
    violations = find_package_import_violations()
    if not violations:
        return 0
    print("sleeptalk must not import server/, app.*, or pipecat:")
    for violation in violations:
        print(f"- {violation}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
