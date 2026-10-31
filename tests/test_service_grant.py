from __future__ import annotations

import os
from pathlib import Path

import pytest

from sleeptalk.service_grant import (
    ServiceGrantError,
    ServiceGrantStore,
    canonical_origin,
)


def test_pending_grant_is_origin_scoped_reused_and_restricted(tmp_path: Path) -> None:
    store = ServiceGrantStore(tmp_path / "credentials")

    first = store.load_or_create_pending("https://CALL.example.test/")
    second = store.load_or_create_pending("https://call.example.test")

    assert first == second
    assert first.origin == "https://call.example.test"
    assert first.enrolled is False
    assert first.authorization.startswith("SleeptalkGrant v1.")
    path = next(store.root.glob("*.json"))
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.root.stat().st_mode & 0o777 == 0o700


def test_mark_enrolled_persists_without_rotating_secret(tmp_path: Path) -> None:
    store = ServiceGrantStore(tmp_path / "credentials")
    pending = store.load_or_create_pending("https://call.example.test")

    active = store.mark_enrolled(pending)

    assert active.enrolled is True
    assert active.secret == pending.secret
    assert store.load(active.origin) == active


def test_different_origins_receive_different_credentials(tmp_path: Path) -> None:
    store = ServiceGrantStore(tmp_path / "credentials")

    hosted = store.load_or_create_pending("https://call.example.test")
    self_hosted = store.load_or_create_pending("https://self.example.test")

    assert hosted.installation_id != self_hosted.installation_id
    assert hosted.secret != self_hosted.secret


def test_reset_replaces_the_identity_and_leaves_it_pending(tmp_path: Path) -> None:
    store = ServiceGrantStore(tmp_path / "credentials")
    original = store.mark_enrolled(
        store.load_or_create_pending("https://call.example.test")
    )

    replacement = store.reset(original.origin)

    assert replacement.origin == original.origin
    assert replacement.installation_id != original.installation_id
    assert replacement.secret != original.secret
    assert replacement.enrolled is False
    assert store.load(original.origin) == replacement


def test_stale_enrollment_cannot_overwrite_a_reset(tmp_path: Path) -> None:
    store = ServiceGrantStore(tmp_path / "credentials")
    stale = store.load_or_create_pending("https://call.example.test")
    replacement = store.reset(stale.origin)

    with pytest.raises(ServiceGrantError, match="changed during enrollment"):
        store.mark_enrolled(stale)

    assert store.load(stale.origin) == replacement


def test_rejects_permissive_or_symlinked_credentials(tmp_path: Path) -> None:
    store = ServiceGrantStore(tmp_path / "credentials")
    grant = store.load_or_create_pending("https://call.example.test")
    path = next(store.root.glob("*.json"))
    os.chmod(path, 0o644)
    with pytest.raises(ServiceGrantError, match="0600"):
        store.load(grant.origin)

    path.unlink()
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(ServiceGrantError, match="regular file"):
        store.load(grant.origin)


@pytest.mark.parametrize(
    "value",
    [
        "http://",
        "ftp://call.example.test",
        "https://user:pass@call.example.test",
        "https://call.example.test/path",
        "https://call.example.test/?secret=yes",
    ],
)
def test_canonical_origin_rejects_credential_leak_surfaces(value: str) -> None:
    with pytest.raises(ServiceGrantError):
        canonical_origin(value)
