"""Private credential persistence. Callers validate with the provider before writing.

References and metadata are safe to return to an installing agent. ``get`` is
runtime-only. This prevents accidental disclosure, not access by unrestricted
programs running as the same user. Local deletion never revokes a provider key.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from typing import Iterable, Iterator
import uuid


class CredentialError(RuntimeError):
    """A value-free storage failure safe to return to the operator."""


class CredentialNotFoundError(CredentialError):
    """A removed reference can be entered again without resetting other steps."""


class CredentialImpactError(CredentialError):
    def __init__(self, profiles: tuple[str, ...]):
        self.profiles = profiles
        super().__init__("Confirm all affected profiles before changing this credential.")


@dataclass(frozen=True)
class CredentialStatus:
    reference: str
    provider: str
    backend: str
    profiles: tuple[str, ...]
    available: bool
    generation: int = 0


@dataclass(frozen=True)
class CredentialCommit:
    reference: str
    provider: str
    generation: int


def default_root() -> Path:
    return Path.home() / ".local" / "share" / "sleeptalk" / "credentials"


def _private_file(path: Path) -> int:
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    details = os.fstat(fd)
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
        os.close(fd)
        raise CredentialError("Credential storage must be owned by the current user.")
    os.fchmod(fd, 0o600)
    return fd


def _keyring_backend():
    try:
        import keyring
        backend = keyring.get_keyring()
        name = type(backend).__module__
        # Do not accept plaintext, fail, chained, or third-party backends.
        if name == "keyring.backends.macOS" or (
            name == "keyring.backends.SecretService" and os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        ):
            return backend
    except ImportError:
        return None
    except Exception:
        raise CredentialError("The OS credential store could not be opened in this session.") from None
    return None


def keychain_available() -> bool:
    """Whether this session can use the OS store (the keyring package plus a
    macOS Keychain or an unlocked Secret Service)."""
    try:
        return _keyring_backend() is not None
    except CredentialError:
        return False


def _exclude_from_backups(root: Path) -> None:
    """Keep a plaintext file store out of Time Machine; a sticky, per-folder flag."""
    if sys.platform != "darwin":
        return
    import subprocess

    try:
        subprocess.run(["tmutil", "addexclusion", str(root)], check=False, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


def storage_kind(root: Path | None = None) -> str:
    """Where keys actually live, for telling the user: "keychain",
    "secret_service", "file", or "none" before the first key. Read only."""
    database = (root or default_root()) / "store.sqlite3"
    if not database.is_file():
        return "none"
    try:
        db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            row = db.execute("SELECT value FROM settings WHERE key='backend'").fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return "none"
    if not row:
        return "none"
    if row[0] == "keyring":
        return "keychain" if sys.platform == "darwin" else "secret_service"
    return "file"


class CredentialStore:
    def __init__(self, root: Path | str | None = None, *, backend: str = "auto"):
        self.root = Path(root) if root is not None else default_root()
        if backend not in {"auto", "file", "keyring"}:
            raise CredentialError("Unknown credential storage backend.")
        self._requested_backend = backend
        self._keyring = None

    @contextmanager
    def _database(self) -> Iterator[sqlite3.Connection]:
        lock_fd = None
        db = None
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.root.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise CredentialError("Credential directory must be owned by the current user.")
            self.root.chmod(0o700)
            lock_fd = _private_file(self.root / "store.lock")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            os.close(_private_file(self.root / "store.sqlite3"))
            db = sqlite3.connect(self.root / "store.sqlite3", isolation_level=None)
            db.execute("PRAGMA secure_delete=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS credentials (
                    ref TEXT PRIMARY KEY, provider TEXT NOT NULL, value TEXT, slot TEXT);
                CREATE TABLE IF NOT EXISTS profiles (
                    ref TEXT NOT NULL REFERENCES credentials(ref) ON DELETE CASCADE,
                    profile TEXT NOT NULL, PRIMARY KEY(ref, profile));
                CREATE TABLE IF NOT EXISTS cleanup (slot TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, digest TEXT NOT NULL, action TEXT NOT NULL, ref TEXT NOT NULL,
                    provider TEXT NOT NULL, generation INTEGER NOT NULL);
            """)
            row = db.execute("SELECT value FROM settings WHERE key='backend'").fetchone()
            if row:
                backend = row[0]
                if self._requested_backend != "auto" and self._requested_backend != backend:
                    raise CredentialError("This store already uses a different backend.")
            else:
                backend = self._requested_backend
                if backend == "auto":
                    backend = "keyring" if _keyring_backend() is not None else "file"
                db.execute("INSERT INTO settings VALUES ('backend', ?)", (backend,))
            self.backend = backend
            if backend == "file" and not db.execute("SELECT 1 FROM settings WHERE key='backup_excluded'").fetchone():
                _exclude_from_backups(self.root)
                db.execute("INSERT OR IGNORE INTO settings VALUES ('backup_excluded', '1')")
            if backend == "keyring":
                self._keyring = _keyring_backend()
                if self._keyring is None:
                    raise CredentialError("The OS credential store is unavailable in this session.")
                self._recover(db)
            yield db
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("Credential storage failed; check access to the private store.") from None
        finally:
            if db is not None:
                db.close()
            if lock_fd is not None:
                os.close(lock_fd)

    def _recover(self, db: sqlite3.Connection) -> None:
        for (slot,) in db.execute("SELECT slot FROM cleanup").fetchall():
            # Entries can be abandoned writes or old versions; active slots stay.
            if not db.execute("SELECT 1 FROM credentials WHERE slot=?", (slot,)).fetchone():
                if self._keyring.get_password("sleeptalk", slot) is not None:
                    self._keyring.delete_password("sleeptalk", slot)
                db.execute("DELETE FROM cleanup WHERE slot=?", (slot,))

    @staticmethod
    def _reference(reference: str) -> None:
        if not re.fullmatch(r"credential:[0-9a-f]{32}", reference):
            raise CredentialError("Invalid credential reference.")

    @staticmethod
    def _validated(provider: str, value: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", provider):
            raise CredentialError("Invalid credential provider.")
        if not isinstance(value, str) or not value or len(value) > 32768 or "\x00" in value:
            raise CredentialError("Credential value is empty or invalid.")

    @staticmethod
    def _row(db: sqlite3.Connection, reference: str):
        CredentialStore._reference(reference)
        row = db.execute("SELECT provider, value, slot FROM credentials WHERE ref=?", (reference,)).fetchone()
        if row is None:
            raise CredentialNotFoundError("Credential reference was not found.")
        return row

    def _value(self, row) -> str:
        value = row[1] if self.backend == "file" else self._keyring.get_password("sleeptalk", row[2])
        if value is None:
            raise CredentialError("Credential is missing from its store; enter a replacement privately.")
        return value

    def put_validated(
        self, provider: str, value: str, *, profile_id: str | None = None,
        request_id: str | None = None,
    ) -> str:
        """Persist an already provider-validated secret independently of profile creation."""
        self._validated(provider, value)
        if request_id is not None and (not request_id or len(request_id) > 512):
            raise CredentialError("Invalid credential request identifier.")
        identifier = uuid.uuid4() if request_id is None else uuid.uuid5(uuid.NAMESPACE_URL, provider + "\0" + request_id)
        reference = "credential:" + identifier.hex
        with self._database() as db:
            if db.execute("SELECT 1 FROM credentials WHERE ref=?", (reference,)).fetchone():
                if self._value(self._row(db, reference)) != value:
                    raise CredentialError("This credential request already completed with a different value.")
                if profile_id is not None:
                    db.execute("INSERT OR IGNORE INTO profiles VALUES (?, ?)", (reference, profile_id))
            else:
                self._write(db, reference, provider, value, profile_id=profile_id)
        return reference

    def _write(self, db, reference, provider, value, *, profile_id=None, old_slot=None, operation_id=None, commit_marker=None):
        slot = None
        stored_value = value
        if self.backend == "keyring":
            slot = uuid.uuid4().hex
            # This durable intent precedes the external write. Recovery removes
            # an uncommitted slot even if the process dies inside set_password.
            db.execute("INSERT INTO cleanup VALUES (?)", (slot,))
            self._keyring.set_password("sleeptalk", slot, value)
            if self._keyring.get_password("sleeptalk", slot) != value:
                raise CredentialError("Credential storage verification failed.")
            stored_value = None
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("INSERT INTO credentials VALUES (?, ?, ?, ?) ON CONFLICT(ref) DO UPDATE SET value=excluded.value, slot=excluded.slot", (reference, provider, stored_value, slot))
            generation = self._generation(db, reference) + 1
            db.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("generation:" + reference, str(generation)))
            if operation_id:
                db.execute("INSERT INTO settings VALUES (?, ?)", (operation_id, str(generation)))
            if commit_marker:
                db.execute("INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?)", (*commit_marker, reference, provider, generation))
            if profile_id is not None:
                db.execute("INSERT OR IGNORE INTO profiles VALUES (?, ?)", (reference, profile_id))
            if slot:
                db.execute("DELETE FROM cleanup WHERE slot=?", (slot,))
            if old_slot:
                db.execute("INSERT OR IGNORE INTO cleanup VALUES (?)", (old_slot,))
            if self._value(self._row(db, reference)) != value:
                raise CredentialError("Credential storage verification failed.")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        if self.backend == "keyring":
            self._recover(db)

    def operation_result(self, operation_id: str) -> CredentialCommit | None:
        """Read a historical commit marker without reading its credential value."""
        with self._database() as db:
            row = db.execute("SELECT ref, provider, generation FROM operations WHERE id=?", (operation_id,)).fetchone()
            return CredentialCommit(*row) if row else None

    def commit_validated(self, provider: str, value: str, *, operation_id: str, operation_digest: str,
                         reference: str | None = None, expected_generation: int = 0,
                         affected_profiles: Iterable[str] = ()) -> CredentialCommit:
        """Commit once after the caller authenticates an immutable operation ID.

        The marker and reference switch share the durable transaction. A retry
        returns that original result even if a later operation changes the key.
        """
        if not re.fullmatch(r"[A-Za-z0-9:_-]{1,256}", operation_id):
            raise CredentialError("Invalid credential operation identifier.")
        if not re.fullmatch(r"[a-f0-9]{64}", operation_digest):
            raise CredentialError("Invalid credential operation digest.")
        self._validated(provider, value)
        action = "replace" if reference is not None else "create"
        reference = reference or "credential:" + uuid.uuid5(uuid.NAMESPACE_URL, "remote:" + operation_id).hex
        self._reference(reference)
        with self._database() as db:
            previous = db.execute("SELECT ref, provider, generation, digest, action FROM operations WHERE id=?", (operation_id,)).fetchone()
            if previous:
                if previous[:2] != (reference, provider) or previous[3:] != (operation_digest, action):
                    raise CredentialError("Credential operation conflicts with its original scope.")
                return CredentialCommit(*previous[:3])
            row = db.execute("SELECT provider, value, slot FROM credentials WHERE ref=?", (reference,)).fetchone()
            if (action == 'replace' and row is None) or (action == 'create' and row is not None):
                raise CredentialError('Credential existence changed before this operation.')
            if self._generation(db, reference) != expected_generation or (row is not None and row[0] != provider):
                raise CredentialError("Credential generation or provider changed.")
            self._check_impact(db, reference, affected_profiles)
            self._write(db, reference, provider, value, old_slot=row[2] if row else None,
                        commit_marker=(operation_id, operation_digest, action))
            return CredentialCommit(reference, provider, expected_generation + 1)

    def get(self, reference: str) -> str:
        """Return a secret only to a runtime component, never to status output."""
        with self._database() as db:
            return self._value(self._row(db, reference))

    def default_reference(self, provider: str) -> str | None:
        """A previously validated key selected for reuse by interactive setup."""
        with self._database() as db:
            row = db.execute(
                "SELECT c.ref FROM settings s JOIN credentials c ON s.value=c.ref "
                "WHERE s.key=? AND c.provider=?",
                ("default:" + provider, provider),
            ).fetchone()
            return row[0] if row else None

    def set_default_reference(self, provider: str, reference: str) -> None:
        with self._database() as db:
            if self._row(db, reference)[0] != provider:
                raise CredentialError("Credential reference belongs to another provider.")
            db.execute(
                "INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("default:" + provider, reference),
            )

    def status(self, reference: str) -> CredentialStatus:
        with self._database() as db:
            row = self._row(db, reference)
            available = row[1] is not None if self.backend == "file" else self._keyring.get_password("sleeptalk", row[2]) is not None
            return CredentialStatus(reference, row[0], self.backend, self._profiles(db, reference), available, self._generation(db, reference))

    @staticmethod
    def _generation(db, reference):
        row = db.execute("SELECT value FROM settings WHERE key=?", ("generation:" + reference,)).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _profiles(db, reference) -> tuple[str, ...]:
        return tuple(row[0] for row in db.execute("SELECT profile FROM profiles WHERE ref=? ORDER BY profile", (reference,)))

    def move_to_keyring(self) -> int:
        """Move every key from the file into the OS store; returns how many moved.

        Each value is written and read back from the Keychain before one
        transaction points every row at it and switches the store's backend,
        so an interruption leaves the file store as it was. The file is then
        vacuumed so the old values don't linger in free pages.
        """
        backend = _keyring_backend()
        if backend is None:
            raise CredentialError("The OS credential store isn't available in this session; "
                                  "install sleeptalk[credentials] and run this from your desktop session.")
        with self._database() as db:
            if self.backend == "keyring":
                return 0
            self._keyring = backend
            # Slots an interrupted move wrote but never committed.
            for (slot,) in db.execute("SELECT slot FROM cleanup").fetchall():
                if backend.get_password("sleeptalk", slot) is not None:
                    backend.delete_password("sleeptalk", slot)
                db.execute("DELETE FROM cleanup WHERE slot=?", (slot,))
            moves: list[tuple[str, str]] = []
            for reference, value in db.execute("SELECT ref, value FROM credentials").fetchall():
                if value is None:
                    raise CredentialError("A stored key has no value to move; repair the store first.")
                slot = uuid.uuid4().hex
                db.execute("INSERT INTO cleanup VALUES (?)", (slot,))
                backend.set_password("sleeptalk", slot, value)
                if backend.get_password("sleeptalk", slot) != value:
                    raise CredentialError("Credential storage verification failed.")
                moves.append((reference, slot))
            db.execute("BEGIN IMMEDIATE")
            try:
                for reference, slot in moves:
                    db.execute("UPDATE credentials SET value=NULL, slot=? WHERE ref=?", (slot, reference))
                    db.execute("DELETE FROM cleanup WHERE slot=?", (slot,))
                db.execute("UPDATE settings SET value='keyring' WHERE key='backend'")
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            self.backend = "keyring"
            db.execute("VACUUM")
            return len(moves)

    def references(self) -> list[tuple[str, str]]:
        """Every stored reference and its provider, oldest first; never a value."""
        with self._database() as db:
            return [(row[0], row[1]) for row in db.execute("SELECT ref, provider FROM credentials ORDER BY rowid")]

    def bind_profile(self, reference: str, profile_id: str) -> None:
        with self._database() as db:
            self._row(db, reference)
            db.execute("INSERT OR IGNORE INTO profiles VALUES (?, ?)", (reference, profile_id))

    @contextmanager
    def bind_profile_generation(self, generations: dict[str, int], profile_id: str):
        """Keep replacement/removal excluded until the reference-only profile is written."""
        with self._database() as db:
            for reference, generation in generations.items():
                self._row(db, reference)
                if self._generation(db, reference) != generation:
                    raise CredentialError("Credential changed before profile creation.")
            for reference in generations:
                db.execute("INSERT OR IGNORE INTO profiles VALUES (?, ?)", (reference, profile_id))
            yield

    def unbind_profile(self, reference: str, profile_id: str) -> None:
        with self._database() as db:
            self._row(db, reference)
            db.execute("DELETE FROM profiles WHERE ref=? AND profile=?", (reference, profile_id))

    def replace_validated(self, reference: str, value: str, *, affected_profiles: Iterable[str], expected_generation: int | None = None, request_id: str | None = None) -> None:
        with self._database() as db:
            row = self._row(db, reference)
            self._validated(row[0], value)
            self._check_impact(db, reference, affected_profiles)
            operation_id = "replace:" + hashlib.sha256((reference + "\0" + request_id).encode()).hexdigest() if request_id else None
            if operation_id and db.execute("SELECT value FROM settings WHERE key=?", (operation_id,)).fetchone():
                if self._value(row) != value:
                    raise CredentialError("This replacement request already completed; inspect current status before another replacement.")
                return
            if expected_generation is not None and self._generation(db, reference) != expected_generation:
                raise CredentialError("This credential changed during setup. Inspect current status before replacing it.")
            self._write(db, reference, row[0], value, old_slot=row[2], operation_id=operation_id)

    def replacement_completed(self, reference: str, request_id: str) -> bool:
        """Reconcile a committed replacement whose acknowledgement was lost."""
        operation_id = "replace:" + hashlib.sha256((reference + "\0" + request_id).encode()).hexdigest()
        with self._database() as db:
            self._row(db, reference)
            row = db.execute("SELECT value FROM settings WHERE key=?", (operation_id,)).fetchone()
            return row is not None and int(row[0]) == self._generation(db, reference)

    @staticmethod
    def _check_impact(db, reference, affected_profiles):
        profiles = CredentialStore._profiles(db, reference)
        if set(profiles) != set(affected_profiles):
            raise CredentialImpactError(profiles)

    def remove(self, reference: str, *, affected_profiles: Iterable[str]) -> None:
        """Delete locally after explicit impact acknowledgement; never revoke upstream."""
        with self._database() as db:
            row = self._row(db, reference)
            self._check_impact(db, reference, affected_profiles)
            db.execute("BEGIN IMMEDIATE")
            if row[2]:
                db.execute("INSERT OR IGNORE INTO cleanup VALUES (?)", (row[2],))
            db.execute("DELETE FROM credentials WHERE ref=?", (reference,))
            db.execute("COMMIT")
            if self.backend == "keyring":
                self._recover(db)

    def migrate_env_validated(
        self, source: Path, env_name: str, provider: str, *, validate,
        profile_id: str | None = None, remove_old: bool = False,
    ) -> str:
        """Migrate one explicitly authorized .env entry after live validation.

        ``validate(value)`` must raise on validation failure. The original file
        remains unless removal is explicitly requested. A failed removal leaves
        the verified destination intact, so existing runtime references survive.
        """
        from dotenv import dotenv_values
        import io
        import tempfile

        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", env_name):
            raise CredentialError("Invalid credential environment field.")
        try:
            source = Path(source)
            if source.is_symlink() or not source.is_file():
                raise CredentialError("Choose an existing regular credential file.")
            original = source.read_text()
            value = dotenv_values(stream=io.StringIO(original), interpolate=False).get(env_name)
            if not value:
                raise CredentialError("The selected credential is absent from the authorized file.")
            validate(value)
            migration_id = hashlib.sha256((str(source.resolve()) + "\0" + env_name).encode()).hexdigest()
            reference = self.put_validated(
                provider, value, profile_id=profile_id, request_id="migration:" + migration_id,
            )
            if self.get(reference) != value:
                raise CredentialError("Credential migration verification failed.")
            if remove_old:
                # The dotenv parser preserves multiline entries and comments;
                # remove exactly the authorized binding, not matching text.
                from dotenv.parser import parse_stream
                updated = "".join(binding.original.string for binding in parse_stream(io.StringIO(original)) if binding.key != env_name)
                if source.read_text() != original:
                    raise CredentialError("The source changed during migration; keep its old copy and retry removal.")
                fd, temporary = tempfile.mkstemp(prefix=".credential-migration-", dir=source.parent)
                try:
                    with os.fdopen(fd, "w") as stream:
                        stream.write(updated)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, source)
                    parent_fd = os.open(source.parent, os.O_RDONLY)
                    try:
                        os.fsync(parent_fd)
                    finally:
                        os.close(parent_fd)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            return reference
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("Credential migration failed; check the authorized source and provider validation.") from None
