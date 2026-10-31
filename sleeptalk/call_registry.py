"""Private lifecycle state for detached voice-call child processes."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import signal
import subprocess
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping

from sleeptalk.home import ENV_VAR as SLEEPTALK_HOME_ENV, resolve_layout
from sleeptalk.operator_errors import ErrorCode

_CALL_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_PROFILE_KEY = re.compile(r"^[a-f0-9]{64}$")
_RECORD_VERSION = 1
_FORCED_EXIT_WAIT_SECONDS = 1.0


class CallRegistryError(RuntimeError):
    """Local call state could not complete the requested operation."""

    code: ErrorCode = "internal"


class CallRequestConflictError(CallRegistryError):
    """The same request ID was supplied with different call options."""

    code: ErrorCode = "config"


class CallNotFoundError(CallRegistryError):
    """No live child owns the requested call id."""

    code: ErrorCode = "not_found"


class CallStopTimeoutError(CallRegistryError):
    """The child remained alive after forced shutdown."""

    code: ErrorCode = "timeout"


class CallBusyError(CallRegistryError):
    """Another detached child already owns the profile."""

    code: ErrorCode = "busy"


@dataclass(frozen=True, slots=True)
class ProfileIdentity:
    """Canonical private identity plus chat-safe profile label."""

    key: str
    profile: str

    @classmethod
    def from_config(cls, *, config_path: str, profile: str) -> ProfileIdentity:
        canonical = str(Path(config_path).expanduser().resolve())
        key = hashlib.sha256(f"profile:{canonical}".encode()).hexdigest()
        return cls(key=key, profile=profile)

    @classmethod
    def for_echo(cls, *, agent_instance: str, profile: str) -> ProfileIdentity:
        key = hashlib.sha256(f"echo:{agent_instance}".encode()).hexdigest()
        return cls(key=key, profile=profile)


@dataclass(frozen=True, slots=True)
class CallRecord:
    version: int
    call_id: str
    profile: str
    profile_key: str
    pid: int
    process_started: str
    started_at: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CallRecord:
        record = cls(
            version=int(value["version"]),
            call_id=str(value["call_id"]),
            profile=str(value["profile"]),
            profile_key=str(value["profile_key"]),
            pid=int(value["pid"]),
            process_started=str(value["process_started"]),
            started_at=str(value["started_at"]),
        )
        if record.version != _RECORD_VERSION:
            raise ValueError("unsupported call record version")
        if _CALL_ID.fullmatch(record.call_id) is None:
            raise ValueError("invalid call id")
        if _PROFILE_KEY.fullmatch(record.profile_key) is None:
            raise ValueError("invalid profile key")
        if record.pid <= 0 or not record.process_started:
            raise ValueError("invalid process identity")
        return record


@dataclass(frozen=True, slots=True)
class StopOutcome:
    record: CallRecord
    forced: bool


@dataclass(slots=True)
class ProfileLease:
    """An exclusive profile lock that can be inherited by one child."""

    fd: int
    path: Path
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        os.close(self.fd)
        self._closed = True


def new_call_id() -> str:
    """Return an opaque identifier containing only chat-safe characters."""
    return secrets.token_urlsafe(18)


def default_registry_root() -> Path:
    return resolve_layout().root / "calls"


def registry_root_for_config(config_path: str | Path) -> Path:
    """Choose profile state independently of the invoking working directory."""
    if os.environ.get(SLEEPTALK_HOME_ENV, "").strip():
        return default_registry_root()
    profile_path = Path(config_path).expanduser().resolve()
    root = profile_path.parent
    if root.name == "profiles":
        root = root.parent
        if root.name == "config":
            root = root.parent
    return root / "calls"


def acquire_profile_lease(
    *, identity: ProfileIdentity, root: Path | None = None
) -> ProfileLease:
    """Claim a profile before spawn, or fail without starting a child."""
    registry_root = root or default_registry_root()
    profile_dir = _ensure_profile_dir(registry_root, identity.key)
    lock_path = profile_dir / "profile.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise CallBusyError(
            f"Profile {identity.profile!r} already has a live call."
        ) from exc
    os.set_inheritable(descriptor, True)
    return ProfileLease(fd=descriptor, path=lock_path)


@contextmanager
def hold_inherited_profile_lease(descriptor: int) -> Iterator[None]:
    """Keep an inherited lease for this child, but not its subprocesses."""
    os.set_inheritable(descriptor, False)
    try:
        yield
    finally:
        os.close(descriptor)


@contextmanager
def register_live_call(
    *,
    identity: ProfileIdentity,
    call_id: str,
    root: Path | None = None,
) -> Iterator[CallRecord]:
    """Publish one child record while holding its unforgeable ownership lock."""
    _require_call_id(call_id)
    registry_root = root or default_registry_root()
    profile_dir = _ensure_profile_dir(registry_root, identity.key)
    lock_path = profile_dir / f"{call_id}.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:  # A call id collision is vanishingly unlikely.
        os.close(lock_fd)
        raise CallRegistryError("The generated call id is already active.") from exc

    record_path = profile_dir / f"{call_id}.json"
    try:
        record = CallRecord(
            version=_RECORD_VERSION,
            call_id=call_id,
            profile=identity.profile,
            profile_key=identity.key,
            pid=os.getpid(),
            process_started=_process_started(os.getpid()),
            started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        _write_record(record_path, record)
        yield record
    finally:
        record_path.unlink(missing_ok=True)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)


def list_live_calls(*, root: Path | None = None) -> list[CallRecord]:
    """Return live records and remove records left behind by dead children."""
    registry_root = root or default_registry_root()
    if not registry_root.is_dir():
        return []
    records: list[CallRecord] = []
    for record_path in sorted(registry_root.glob("*/*.json")):
        record = _load_record(record_path)
        if record is None or not _ownership_lock_is_held(record, registry_root):
            _remove_stale_record(record_path)
            continue
        records.append(record)
    return sorted(records, key=lambda record: (record.started_at, record.call_id))


def stop_call(
    call_id: str,
    *,
    root: Path | None = None,
    timeout_seconds: float = 5.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> StopOutcome:
    """Signal one verified child with TERM, then KILL after the timeout."""
    _require_call_id(call_id, not_found=True)
    registry_root = root or default_registry_root()
    record_path = _find_record_path(registry_root, call_id)
    record = _load_record(record_path) if record_path is not None else None
    if (
        record is None
        or not _ownership_lock_is_held(record, registry_root)
        or not _process_matches(record)
    ):
        if record_path is not None:
            _remove_stale_record(record_path)
        raise CallNotFoundError(f"Call {call_id} was not found.")

    try:
        os.kill(record.pid, signal.SIGTERM)
    except ProcessLookupError:
        _remove_stale_record(record_path)
        return StopOutcome(record=record, forced=False)
    except PermissionError as exc:
        raise CallRegistryError(f"Call {call_id} could not be signaled.") from exc
    deadline = monotonic() + max(0.0, timeout_seconds)
    while monotonic() < deadline:
        if not _process_matches(record):
            record_path.unlink(missing_ok=True)
            return StopOutcome(record=record, forced=False)
        sleep(min(0.05, max(0.0, deadline - monotonic())))

    if not _ownership_lock_is_held(record, registry_root) or not _process_matches(record):
        record_path.unlink(missing_ok=True)
        return StopOutcome(record=record, forced=False)
    try:
        os.kill(record.pid, signal.SIGKILL)
    except ProcessLookupError:
        _remove_stale_record(record_path)
        return StopOutcome(record=record, forced=False)
    except PermissionError as exc:
        raise CallRegistryError(f"Call {call_id} could not be killed.") from exc
    forced_deadline = monotonic() + _FORCED_EXIT_WAIT_SECONDS
    while monotonic() < forced_deadline:
        if not _process_matches(record):
            _remove_stale_record(record_path)
            # A killed call never ran its cleanup; drop the tailnet serve
            # entry it may have left pointing at its dead loopback port.
            try:
                from sleeptalk.tailnet import clear_stale_after_kill

                clear_stale_after_kill()
            except Exception:  # noqa: BLE001 - the stop itself succeeded
                pass
            return StopOutcome(record=record, forced=True)
        sleep(0.05)
    raise CallStopTimeoutError(f"Call {call_id} did not stop after SIGKILL.")


def _require_call_id(call_id: str, *, not_found: bool = False) -> None:
    if _CALL_ID.fullmatch(call_id) is None:
        if not_found:
            raise CallNotFoundError(f"Call {call_id!r} was not found.")
        raise ValueError("call id must contain only URL-safe characters")


def _ensure_profile_dir(root: Path, profile_key: str) -> Path:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    profile_dir = root / profile_key
    profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile_dir.chmod(0o700)
    return profile_dir


def _write_record(path: Path, record: CallRecord) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        payload = json.dumps(asdict(record), separators=(",", ":"), sort_keys=True)
        os.write(descriptor, payload.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_record(path: Path | None) -> CallRecord | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return None
        return CallRecord.from_mapping(value)
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return None


def _find_record_path(root: Path, call_id: str) -> Path | None:
    matches = list(root.glob(f"*/{call_id}.json")) if root.is_dir() else []
    return matches[0] if len(matches) == 1 else None


def _ownership_lock_is_held(record: CallRecord, root: Path) -> bool:
    lock_path = root / record.profile_key / f"{record.call_id}.lock"
    try:
        descriptor = os.open(lock_path, os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _remove_stale_record(record_path: Path) -> None:
    request_path = record_path.parent / "requests" / record_path.name
    if request_path.exists():
        with _request_lock(request_path):
            request_path.with_suffix(".capability").unlink(missing_ok=True)
            record = _read_request(request_path)
            if record is not None and record["state"] not in _TERMINAL_START_STATES:
                record["state"] = "ended"
                _write_private_json(request_path, record)
    record_path.unlink(missing_ok=True)
    record_path.with_suffix(".lock").unlink(missing_ok=True)


def _process_started(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    started = result.stdout.strip()
    if result.returncode != 0 or not started:
        raise CallRegistryError("Could not identify the detached call process.")
    return started


def _process_matches(record: CallRecord) -> bool:
    try:
        if _process_started(record.pid) != record.process_started:
            return False
    except CallRegistryError:
        return False
    result = subprocess.run(
        ["ps", "-ww", "-o", "command=", "-p", str(record.pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and record.call_id in result.stdout


_START_STATES = frozenset({"starting", "active", "ended", "failed", "expired"})
_PROGRESS_STAGES = frozenset({
    "runtime_ready", "link_minted", "host_signaling_connected", "link_consumed",
    "waiting_for_permission", "microphone_ready", "browser_connected",
    "speech_received", "agent_response", "media_playing", "response_played",
    "ended", "failed", "redemption_notifications_unavailable",
})
_TERMINAL_START_STATES = frozenset({"ended", "failed", "expired"})


def request_call_id(request_id: str, identity: ProfileIdentity) -> str:
    """Bind an explicit start request to one opaque call identity."""
    _require_call_id(request_id)
    return hashlib.sha256(f"{identity.key}:{request_id}".encode()).hexdigest()[:32]


def _request_paths(root: Path, identity: ProfileIdentity, request_id: str):
    call_id = request_call_id(request_id, identity)
    profile_dir = _ensure_profile_dir(root, identity.key)
    requests = profile_dir / "requests"
    requests.mkdir(mode=0o700, exist_ok=True)
    requests.chmod(0o700)
    return requests / f"{call_id}.json", requests / f"{call_id}.capability"


@contextmanager
def _request_lock(path: Path):
    descriptor = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _write_private_json(path: Path, value: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _read_request(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get("state") not in _START_STATES:
            raise ValueError("invalid start request record")
        if not isinstance(value.get("callId"), str) or _CALL_ID.fullmatch(value["callId"]) is None:
            raise ValueError("invalid call identity")
        if not isinstance(value.get("profile"), str) or not isinstance(value.get("fingerprint"), str):
            raise ValueError("invalid request identity")
        for field in ("created_at", "expires_at"):
            if not isinstance(value.get(field), (float, int)) or not math.isfinite(value[field]):
                raise ValueError("invalid request deadline")
        observations = value.get("observations")
        if not isinstance(observations, dict) or any(
            stage not in _PROGRESS_STAGES or not isinstance(at, (float, int)) or not math.isfinite(at)
            for stage, at in observations.items()
        ):
            raise ValueError("invalid observations")
        timestamps = [value["created_at"], value["expires_at"], *observations.values()]
        if any(isinstance(at, bool) or not 0 <= at <= 253402300799 for at in timestamps):
            raise ValueError("invalid observation timestamp")
        return value
    except (ValueError, TypeError, KeyError, OSError) as exc:
        # A damaged record is never permission to launch the request again.
        raise CallRegistryError("The saved start request could not be read.") from exc


def reserve_start_request(
    *, identity: ProfileIdentity, request_id: str, fingerprint: str,
    ttl_seconds: int, root: Path | None = None,
) -> dict[str, object]:
    """Persist a start before spawning. The caller must own the profile lease."""
    if ttl_seconds <= 0:
        raise ValueError("call expiry must be positive")
    root = root or default_registry_root()
    path, _ = _request_paths(root, identity, request_id)
    with _request_lock(path):
        existing = _read_request(path)
        if existing is not None:
            return existing
        now = time.time()
        record = {
            "callId": request_call_id(request_id, identity),
            "profile": identity.profile, "fingerprint": fingerprint,
            "state": "starting", "created_at": now,
            "expires_at": now + ttl_seconds, "observations": {},
        }
        _write_private_json(path, record)
        return record


def observe_start_request(
    *, identity: ProfileIdentity, request_id: str, stage: str,
    root: Path | None = None, join_url: str | None = None,
) -> None:
    """Record only named observations, never event payloads or transcripts."""
    if stage not in _PROGRESS_STAGES:
        raise ValueError("unknown call progress stage")
    root = root or default_registry_root()
    path, capability = _request_paths(root, identity, request_id)
    with _request_lock(path):
        record = _read_request(path)
        if record is None or record["state"] in _TERMINAL_START_STATES:
            return
        observations = record["observations"]
        now = time.time()
        if stage in observations:
            return
        observations[stage] = now
        if stage in {"ended", "failed"}:
            record["state"] = stage
        elif stage == "link_minted":
            record["state"] = "active"
            if join_url is not None and now < record["expires_at"]:
                _write_private_json(capability, {"url": join_url})
        if stage in {"link_consumed", "browser_connected", "ended", "failed"}:
            capability.unlink(missing_ok=True)
        _write_private_json(path, record)


def get_start_request(
    *, identity: ProfileIdentity, request_id: str, root: Path | None = None,
    fingerprint: str | None = None,
) -> dict[str, object] | None:
    """Return redacted progress, reconciling expired URLs and dead owners."""
    root = root or default_registry_root()
    path, capability = _request_paths(root, identity, request_id)
    with _request_lock(path):
        record = _read_request(path)
        if record is None:
            return None
        if fingerprint is not None and fingerprint != record["fingerprint"]:
            raise CallRequestConflictError("This request ID belongs to different call options.")
        observations = record["observations"]
        if time.time() >= record["expires_at"]:
            capability.unlink(missing_ok=True)
            if not {"link_consumed", "browser_connected"}.intersection(observations):
                if record["state"] not in _TERMINAL_START_STATES:
                    record["state"] = "expired"
        if record["state"] not in _TERMINAL_START_STATES:
            # The lease spans the reservation-to-child-registration interval.
            # A lost parent before spawn therefore cannot strand a retry.
            live = _load_record(root / identity.key / f"{record['callId']}.json")
            if live is not None:
                if not _ownership_lock_is_held(live, root):
                    record["state"] = "ended"
            elif record["state"] == "active":
                record["state"] = "ended"
            else:
                try:
                    lease = acquire_profile_lease(identity=identity, root=root)
                except CallBusyError:
                    pass
                else:
                    lease.close()
                    record["state"] = "failed"
        if record["state"] in _TERMINAL_START_STATES:
            capability.unlink(missing_ok=True)
        _write_private_json(path, record)
        return {
            "callId": record["callId"], "profile": record["profile"],
            "state": record["state"], "expires_at": record["expires_at"],
            "observations": dict(observations),
            "audio_acceptance": "unknown",
        }


def recover_start_url(
    *, identity: ProfileIdentity, request_id: str, root: Path | None = None,
) -> str | None:
    """Read the capability only for an explicitly matching start request."""
    root = root or default_registry_root()
    path, capability = _request_paths(root, identity, request_id)
    with _request_lock(path):
        record = _read_request(path)
        if record is None or record["state"] in _TERMINAL_START_STATES:
            capability.unlink(missing_ok=True)
            return None
        if time.time() >= record["expires_at"] or {"link_consumed", "browser_connected"}.intersection(record["observations"]):
            capability.unlink(missing_ok=True)
            return None
        if not capability.exists():
            return None
        try:
            return str(json.loads(capability.read_text())["url"])
        except (OSError, ValueError, KeyError) as exc:
            raise CallRegistryError("The saved call link could not be recovered.") from exc
