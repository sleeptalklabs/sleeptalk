"""Repository-wide test isolation."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _private_diagnostic_environment(monkeypatch):
    """Runtime setup mutates these; traces must never cross tests or hit real logs."""
    for name in ("SLEEPTALK_LATENCY_LOG_ENABLED", "VOICE_LATENCY_LOG_PATH", "SLEEPTALK_TELEMETRY_STDOUT"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_tailnet_on_this_host(monkeypatch):
    """Tailscale on the developer's machine must not add prompts or receipt
    fields to tests that never asked for it. Tests that want it patch
    ``sleeptalk.tailnet.detect`` themselves."""
    async def unavailable(*, environ=None):
        return {"available": False, "code": "tailscale_missing", "message": "Tailscale is not installed on this host."}
    monkeypatch.setattr("sleeptalk.tailnet.detect", unavailable)


@pytest.fixture(autouse=True)
def _no_host_keys(request, monkeypatch):
    """Key discovery must never read the developer's own ~/.hermes/.env or
    credential store from a test. Tests marked ``real_key_discovery`` supply
    their own home and profile directory."""
    if request.node.get_closest_marker("real_key_discovery"):
        return
    monkeypatch.setattr("sleeptalk.key_discovery.discover", lambda **kwargs: [])


@pytest.fixture(autouse=True)
def _private_credential_store(request, tmp_path, monkeypatch):
    """Profiles keep keys in the private store. Tests get a file store of
    their own and never touch the developer's Keychain or real store. The
    store's own tests choose their roots and backends explicitly."""
    monkeypatch.setattr("sleeptalk.credential_store.default_root", lambda: tmp_path / "credential-store")
    monkeypatch.setattr("sleeptalk.credential_store._exclude_from_backups", lambda root: None)
    if request.module.__name__.rsplit(".", 1)[-1] == "test_credential_store":
        return
    monkeypatch.setattr("sleeptalk.credential_store._keyring_backend", lambda: None)
