"""Release metadata gates for honest wheel / docs packaging."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PKG_ROOT
PYPROJECT = PKG_ROOT / "pyproject.toml"
REQUIRED_URL_KEYS = {
    "Homepage",
    "Documentation",
    "Repository",
    "Changelog",
    "Support",
}
FIRST_RUN_DOCS = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "launch-support-matrix.md",
)


@pytest.fixture(scope="module")
def project() -> dict:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["project"]


def test_requires_python_matches_support_contract(project: dict) -> None:
    assert project["requires-python"] == ">=3.11"
    assert (REPO_ROOT / "docs" / "launch-support-matrix.md").read_text(
        encoding="utf-8"
    ).count("| Python | 3.11+ |") == 1


def test_license_and_readme_are_declared(project: dict) -> None:
    assert project.get("license") == "MIT"
    assert project.get("readme") == "README.md"
    assert (PKG_ROOT / "README.md").is_file()
    assert (PKG_ROOT / "LICENSE").is_file()
    license_files = project.get("license-files") or []
    assert "LICENSE" in license_files


def test_project_urls_cover_support_and_source(project: dict) -> None:
    urls = project.get("urls") or {}
    missing = REQUIRED_URL_KEYS - set(urls)
    assert not missing, f"missing project.urls keys: {sorted(missing)}"
    assert "github.com/" in urls["Repository"] and urls["Repository"].rstrip("/").endswith("/sleeptalk")
    assert "launch-support-matrix.md" in urls["Support"]


def test_web_extra_exists_for_support_matrix_install(project: dict) -> None:
    extras = project.get("optional-dependencies") or {}
    assert "web" in extras
    assert "discord" in extras
    assert "terminal" in extras
    recv = [
        dep for dep in extras["discord"] if dep.startswith("discord-ext-voice-recv")
    ]
    assert recv == ["discord-ext-voice-recv==0.5.2a179"]
    from sleeptalk import sysdeps

    assert recv[0] in sysdeps.DISCORD_EXTRA_PACKAGES


def test_console_scripts_expose_sleeptalk(project: dict) -> None:
    scripts = project.get("scripts") or {}
    assert scripts.get("sleeptalk") == "sleeptalk.cli:main"


def test_first_run_docs_forbid_cd_server_launch_steps() -> None:
    pattern = re.compile(r"cd\s+server")
    offenders: list[str] = []
    for path in FIRST_RUN_DOCS:
        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], (
        "first-run docs must not teach `cd server` when the package path exists: "
        + ", ".join(offenders)
    )


def test_dockerfile_is_package_only() -> None:
    text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY server/" not in text
    assert 'ENTRYPOINT ["sleeptalk-entrypoint"]' in text
    assert "COPY sleeptalk/" in text
    inventory = json.loads(
        (REPO_ROOT / "sleeptalk" / "harness_versions.json").read_text(encoding="utf-8")
    )
    recommended = inventory["harnesses"]["pi"]["recommended"]
    assert f"@earendil-works/pi-coding-agent@{recommended}" in text
    entrypoint = (REPO_ROOT / "scripts" / "docker-entrypoint.sh").read_text(encoding="utf-8")
    # The package CLI runs as the non-root sleeptalk user.
    assert 'exec setpriv --reuid=sleeptalk --regid=sleeptalk --init-groups sleeptalk "$@"' in entrypoint


def test_prove_release_script_exists_and_is_executable() -> None:
    path = PKG_ROOT / "scripts" / "prove_release_wheel.sh"
    assert path.is_file()
    assert path.stat().st_mode & 0o111
