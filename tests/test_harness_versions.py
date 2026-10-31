from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import subprocess

import pytest

from sleeptalk import harness_versions as versions
from sleeptalk.adapter_cli import add_parser, run
from sleeptalk.agents.readiness import install_missing, probe


def test_existing_older_pi_is_retained_and_missing_pi_install_is_pinned(monkeypatch):
    commands = []
    monkeypatch.setattr("sleeptalk.sysdeps.run_install", lambda cmd: (commands.append(cmd) or True, ""))
    monkeypatch.setattr("sleeptalk.registries.locate_binary", lambda *args: "/old/pi")
    existing = probe("pi")
    assert existing.ready
    assert install_missing(existing) == []
    assert commands == []
    monkeypatch.setattr("sleeptalk.registries.locate_binary", lambda *args: None)
    missing = probe("pi")
    assert not missing.ready
    install_missing(missing)
    assert commands == [["npm", "install", "-g", versions.pi_install_spec()]]
    pi = versions.inventory()["harnesses"]["pi"]
    assert pi["recommended"] in pi["verified"]


def test_existing_omp_is_retained_and_missing_install_is_pinned(monkeypatch):
    commands = []
    monkeypatch.setattr("sleeptalk.sysdeps.run_install", lambda cmd: (commands.append(cmd) or True, ""))
    monkeypatch.setattr("sleeptalk.registries.locate_binary", lambda *args: "/old/omp")
    assert install_missing(probe("omp")) == []
    assert commands == []
    monkeypatch.setattr("sleeptalk.registries.locate_binary", lambda *args: None)
    install_missing(probe("omp"))
    version = versions.inventory()["harnesses"]["omp"]["recommended"]
    assert commands == [["sh", "-c", f"curl -fsSL https://omp.sh/install | sh -s -- --binary --ref v{version}"]]


def test_compiled_omp_version_does_not_claim_protected_sdk_support(monkeypatch):
    monkeypatch.setattr(versions.shutil, "which", lambda binary: "/old/omp")
    monkeypatch.setattr(versions.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "omp/17.2.12\n", ""))
    result = versions.inspect_omp("omp")
    assert result["version"] == "17.2.12" and result["compatibility"] == "verified"
    assert result["protected_sdk"]["compatibility"] == "unavailable"


@pytest.mark.parametrize("version,status", [("18.2.1", "verified"), ("18.10.2", "verified"), ("18.11.0", "unverified")])
def test_omp_sdk_inventory_is_exact_and_separate_from_cli(monkeypatch, tmp_path, version, status):
    binary = tmp_path / "dist/cli.js"
    binary.parent.mkdir()
    binary.touch()
    (tmp_path / "src").mkdir()
    (tmp_path / "src/sdk.ts").touch()
    (tmp_path / "package.json").write_text(json.dumps({"name": "@oh-my-pi/pi-coding-agent", "version": version}))
    monkeypatch.setattr(versions.shutil, "which", lambda _: str(binary))
    monkeypatch.setattr(versions.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, f"omp/{version}\n", ""))
    assert versions.inspect_omp(str(binary))["protected_sdk"] == {"version": version, "compatibility": status}


@pytest.mark.parametrize("version,status", [("0.84.0", "verified"), ("1.1.0", "verified"),
                                           ("0.50.0", "unverified"), ("2.0.0", "unverified"),
                                           ("1.1.0-local", "unverified")])
def test_age_alone_does_not_reject_a_pi_version(version, status):
    assert versions.classify_pi(version) == status


def test_version_probe_does_not_inherit_credentials_or_project(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("NODE_OPTIONS", "must-not-run")
    monkeypatch.setattr(versions.shutil, "which", lambda binary: "/old/pi")
    def execute(argv, **kwargs):
        assert argv == ["/old/pi", "--version"]
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert "NODE_OPTIONS" not in kwargs["env"]
        assert kwargs["cwd"] == kwargs["env"]["HOME"]
        assert Path(kwargs["cwd"]).is_dir()
        return subprocess.CompletedProcess(argv, 0, "0.84.0\n", "")
    monkeypatch.setattr(versions.subprocess, "run", execute)
    assert versions.inspect_pi("pi")["compatibility"] == "verified"


def test_unknown_output_and_failed_version_probes_are_not_verified(monkeypatch):
    monkeypatch.setattr(versions.shutil, "which", lambda binary: "/wrapper/pi")
    monkeypatch.setattr(versions.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "a custom build", ""))
    assert versions.inspect_pi("pi")["compatibility"] == "unverified"
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("pi", 10)
    monkeypatch.setattr(versions.subprocess, "run", timeout)
    assert versions.inspect_pi("pi")["compatibility"] == "unavailable"


@pytest.mark.parametrize("source,payload,expected", [
    ({"kind": "npm", "package": "@example/pi"}, b'{"version":"1.2.3"}', "1.2.3"),
    ({"kind": "pypi", "package": "sdk"}, b'{"info":{"version":"2.3.4"}}', "2.3.4"),
    ({"kind": "github", "repository": "example/pi"}, b'{"tag_name":"v18.2.1","prerelease":false}', "18.2.1"),
    ({"kind": "cursor-installer"}, b'DOWNLOAD_URL="https://downloads.cursor.com/lab/2026.10.01-e373342/${OS}/${ARCH}/agent-cli-package.tar.gz"', "2026.10.01-e373342"),
])
def test_only_release_metadata_is_interpreted(monkeypatch, source, payload, expected):
    def fetch(request, timeout):
        assert request.full_url.startswith("https://")
        assert timeout == 15
        return io.BytesIO(payload)
    monkeypatch.setattr(versions, "urlopen", fetch)
    assert versions.latest_version(source) == expected


def test_release_audit_distinguishes_changed_unavailable_and_unchanged(monkeypatch):
    def fetch(source):
        if source["kind"] == "broken":
            raise OSError("offline")
        return source["kind"]
    monkeypatch.setattr(versions, "latest_version", fetch)
    manifest = {"harnesses": {key: {"latest_observed": "1.0.0", "upstream": {"kind": value}}
                             for key, value in [("same", "1.0.0"), ("new", "1.1.0"), ("bad", "broken")]}}
    report = versions.audit_releases(manifest)
    assert report["same"]["changed"] is False
    assert report["new"]["changed"] is True
    assert report["bad"] == {"latest": None, "changed": None, "status": "unavailable"}


@pytest.mark.parametrize("payload", [b'{"tag_name":"v2.0.0-beta","prerelease":true}', b'{}', b'not json'])
def test_bad_or_prerelease_metadata_is_not_a_stable_release(monkeypatch, payload):
    monkeypatch.setattr(versions, "urlopen", lambda *a, **kw: io.BytesIO(payload))
    with pytest.raises((ValueError, KeyError)):
        versions.latest_version({"kind": "github", "repository": "example/pi"})


def test_public_version_command_is_offline_by_default(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline inventory must not launch a process or access the network")
    monkeypatch.setattr(versions, "urlopen", forbidden)
    monkeypatch.setattr(versions.subprocess, "run", forbidden)
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["adapter", "versions", "--json"])
    assert run(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report["harnesses"]) == {"pi", "omp", "hermes", "openclaw", "codex",
                                       "claude_code", "claude_sdk", "cursor_cli", "cursor_sdk"}


@pytest.mark.parametrize("status,changed,exit_code", [("ok", False, 0), ("ok", True, 1),
                                                      ("unavailable", None, 2)])
def test_audit_exit_codes(monkeypatch, capsys, status, changed, exit_code):
    monkeypatch.setattr(versions, "audit_releases", lambda _: {"pi": {"status": status, "changed": changed}})
    args = argparse.Namespace(pi_bin=None, upstream=True, json=True, fail_on_change=True)
    assert versions.run(args) == exit_code


def test_cursor_pins_agree_with_inventory_and_existing_install_is_preserved(monkeypatch):
    import importlib.util
    import tomllib
    from sleeptalk.agents import cursor_sdk_isolation
    root = Path(__file__).parents[1]
    spec = versions.cursor_sdk_install_spec()
    assert spec in tomllib.loads((root / 'pyproject.toml').read_text())['project']['optional-dependencies']['cursor']
    locked = tomllib.loads((root / 'uv.lock').read_text())['package']
    assert next(p['version'] for p in locked if p['name'] == 'cursor-sdk') == versions.cursor_sdk_policy()['recommended']
    assert cursor_sdk_isolation.SDK_VERSION == versions.cursor_sdk_policy()['recommended']
    monkeypatch.setattr(importlib.util, 'find_spec', lambda _: object())
    monkeypatch.setattr('sleeptalk.sysdeps.run_install', lambda _: pytest.fail('Existing SDK must not be upgraded'))
    # A key picks the SDK runtime this test is about, wherever the Cursor CLI is.
    assert install_missing(probe('cursor', env={'CURSOR_API_KEY': 'test'})) == []


@pytest.mark.parametrize('release', ['1.0.29', '1.0.31', '1.0.32', '1.0.37', '1.0.30', '2.0.0'])
def test_cursor_protected_bridge_uses_exact_verified_allowlist(monkeypatch, tmp_path, release):
    from types import SimpleNamespace
    from sleeptalk.agents import cursor_sdk_isolation as isolated
    root = tmp_path / 'cursor_sdk/_vendor/bridge'
    for name in ['bin/node', 'dist/bin/cursor-sdk-bridge.js']:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    monkeypatch.setattr(isolated, 'distribution', lambda _: SimpleNamespace(version=release, locate_file=lambda p: tmp_path / p))
    if release in versions.cursor_sdk_policy()['verified']:
        assert isolated.bundled_bridge() == (str(root / 'bin/node'), str(root / 'dist/bin/cursor-sdk-bridge.js'))
    else:
        with pytest.raises(ValueError, match='exact verified'):
            isolated.bundled_bridge()


def test_cursor_handshake_does_not_claim_full_protocol_verification(monkeypatch):
    monkeypatch.setattr(versions.shutil, 'which', lambda _: '/old/agent')
    monkeypatch.setattr(versions.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, '2026.09.02-c22c1a3\n', ''))
    result = versions.inspect_cursor('agent')
    assert result['compatibility'] == 'handshake-only'
    assert result['version'] == '2026.09.02-c22c1a3'


@pytest.mark.parametrize('harness', ['codex', 'openclaw', 'openclaw_acp', 'claude_code'])
def test_existing_harness_installations_are_not_upgraded(monkeypatch, harness):
    import importlib.util
    monkeypatch.setattr('sleeptalk.registries.locate_binary', lambda *a: '/existing/agent')
    monkeypatch.setattr(importlib.util, 'find_spec', lambda _: object())
    monkeypatch.setattr('sleeptalk.sysdeps.run_install', lambda _: pytest.fail('Must retain existing runtime'))
    assert install_missing(probe(harness)) == []


def test_claude_sdk_project_lock_and_readiness_agree():
    import tomllib
    from sleeptalk.agents.readiness import EXTRA_PACKAGES
    root = Path(__file__).parents[1]
    spec = versions.install_spec('claude_sdk')
    assert spec in tomllib.loads((root / 'pyproject.toml').read_text())['project']['optional-dependencies']['claude']
    locked = tomllib.loads((root / 'uv.lock').read_text())['package']
    assert next(p['version'] for p in locked if p['name'] == 'claude-agent-sdk') == versions.inventory()['harnesses']['claude_sdk']['recommended']
    assert spec in str(EXTRA_PACKAGES)


def test_all_reviewed_codex_schemas_are_packaged_with_expected_hashes():
    import hashlib
    root = Path(__file__).parents[1] / 'sleeptalk/agents/codex_app_server_schema'
    codex = versions.inventory()['harnesses']['codex']
    assert set(codex['verified']) == set(codex['schemas'])
    for schema in codex['schemas'].values():
        assert hashlib.sha256((root / schema['file']).read_bytes()).hexdigest() == schema['sha256']
    assert (root / 'VERSION').read_text().strip() == codex['recommended']


@pytest.mark.parametrize('harness,inventory_key,engine_strict', [
    ('codex', 'codex', False), ('claude_code', 'claude_code', True),
    ('openclaw', 'openclaw', True), ('openclaw_acp', 'openclaw', True),
])
def test_missing_runtime_installs_only_the_exact_recommendation(monkeypatch, harness, inventory_key, engine_strict):
    import importlib.util
    commands = []
    monkeypatch.setattr('sleeptalk.registries.locate_binary', lambda *a: None)
    monkeypatch.setattr(importlib.util, 'find_spec', lambda _: object())
    monkeypatch.setattr('sleeptalk.sysdeps.run_install', lambda cmd: (commands.append(cmd) or True, ''))
    install_missing(probe(harness))
    assert commands == [['npm', 'install', *(['--engine-strict'] if engine_strict else []), '-g', versions.install_spec(inventory_key)]]
