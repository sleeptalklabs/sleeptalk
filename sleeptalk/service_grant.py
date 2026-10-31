"""Origin-scoped credentials for the hosted browser-link service."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .home import resolve_layout


class ServiceGrantError(RuntimeError):
    """A local hosted-service credential is missing or unsafe."""


@dataclass(frozen=True, slots=True)
class ServiceGrant:
    origin: str
    installation_id: str
    secret: str
    enrollment_request_id: str
    enrolled: bool
    version: int = 1

    @property
    def authorization(self) -> str:
        return f"SleeptalkGrant v1.{self.installation_id}.{self.secret}"

    @property
    def secret_hash(self) -> str:
        return hashlib.sha256(self.secret.encode("utf-8")).hexdigest()


class ServiceGrantStore:
    """Restricted, atomic storage keyed by canonical service origin."""

    def __init__(self, root: Path | None = None) -> None:
        layout = resolve_layout()
        self.root = root or layout.root / "credentials" / "rendezvous"

    def load(self, origin: str) -> ServiceGrant | None:
        canonical = canonical_origin(origin)
        path = self._path(canonical)
        if not path.exists():
            return None
        self._validate_path(path)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            grant = ServiceGrant(
                origin=str(value["origin"]),
                installation_id=str(value["installation_id"]),
                secret=str(value["secret"]),
                enrollment_request_id=str(value["enrollment_request_id"]),
                enrolled=bool(value["enrolled"]),
                version=int(value["version"]),
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ServiceGrantError("The hosted-service credential file is unreadable.") from exc
        if (
            grant.version != 1
            or grant.origin != canonical
            or not _is_token(grant.installation_id, 20, 64)
            or not _is_token(grant.secret, 32, 128)
            or not _is_token(grant.enrollment_request_id, 16, 128)
        ):
            raise ServiceGrantError("The hosted-service credential file is invalid.")
        return grant

    def load_or_create_pending(self, origin: str) -> ServiceGrant:
        canonical = canonical_origin(origin)
        with self._locked(canonical):
            current = self.load(canonical)
            if current is not None:
                return current
            grant = ServiceGrant(
                origin=canonical,
                installation_id=secrets.token_urlsafe(18),
                secret=secrets.token_urlsafe(32),
                enrollment_request_id=secrets.token_urlsafe(18),
                enrolled=False,
            )
            self._write(grant)
            return self.load(canonical) or grant

    def mark_enrolled(self, grant: ServiceGrant) -> ServiceGrant:
        with self._locked(grant.origin):
            current = self.load(grant.origin)
            if current is None or (
                current.installation_id != grant.installation_id
                or current.secret != grant.secret
                or current.enrollment_request_id != grant.enrollment_request_id
            ):
                raise ServiceGrantError(
                    "The hosted-service credential changed during enrollment."
                )
            if current.enrolled:
                return current
            active = replace(current, enrolled=True)
            self._write(active)
            return active

    def reset(self, origin: str) -> ServiceGrant:
        canonical = canonical_origin(origin)
        with self._locked(canonical):
            path = self._path(canonical)
            if path.exists():
                self._validate_path(path)
                path.unlink()
            grant = ServiceGrant(
                origin=canonical,
                installation_id=secrets.token_urlsafe(18),
                secret=secrets.token_urlsafe(32),
                enrollment_request_id=secrets.token_urlsafe(18),
                enrolled=False,
            )
            self._write(grant)
            return self.load(canonical) or grant

    def _path(self, canonical: str) -> Path:
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def _write(self, grant: ServiceGrant) -> None:
        self._ensure_root()
        target = self._path(grant.origin)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".grant-", dir=self.root)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(asdict(grant), handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            os.chmod(target, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()

    @contextmanager
    def _locked(self, canonical: str):
        import fcntl

        self._ensure_root()
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        lock_path = self.root / f".{digest}.lock"
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _ensure_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        details = self.root.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise ServiceGrantError("The hosted-service credential directory is unsafe.")
        os.chmod(self.root, 0o700)

    @staticmethod
    def _validate_path(path: Path) -> None:
        try:
            details = path.lstat()
        except OSError as exc:
            raise ServiceGrantError("The hosted-service credential file cannot be inspected.") from exc
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise ServiceGrantError("The hosted-service credential path must be a regular file.")
        if stat.S_IMODE(details.st_mode) != 0o600:
            raise ServiceGrantError("The hosted-service credential file must have mode 0600.")
        if hasattr(os, "getuid") and details.st_uid != os.getuid():
            raise ServiceGrantError("The hosted-service credential file has the wrong owner.")


def canonical_origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ServiceGrantError("The hosted-service origin is invalid.")
    path = parsed.path.rstrip("/")
    if path:
        raise ServiceGrantError("The hosted-service origin cannot contain a path.")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), "", "", ""))


def _is_token(value: str, minimum: int, maximum: int) -> bool:
    return minimum <= len(value) <= maximum and all(
        character.isalnum() or character in "-_" for character in value
    )
