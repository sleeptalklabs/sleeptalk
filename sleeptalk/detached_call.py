"""Launch a bounded live-call host without blocking the invoking agent.

The parent waits only until the child mints a ticket, prints the shareable URL,
and exits. The detached child owns the WebRTC connection until expiry or
hangup, so an agent tool call can return and send the URL through its existing
chat surface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import hashlib
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from sleeptalk.call_provider import (
    CallContext,
    CallProviderError,
    run_ticket_echo_call,
)
from sleeptalk.call_registry import (
    CallBusyError,
    CallRegistryError,
    ProfileIdentity,
    acquire_profile_lease,
    default_registry_root,
    hold_inherited_profile_lease,
    new_call_id,
    registry_root_for_config,
    register_live_call,
    request_call_id,
    reserve_start_request,
    get_start_request,
    recover_start_url,
    observe_start_request,
)
from sleeptalk.operator_errors import ErrorCode, error_code_for, validate_error_code

_START_TIMEOUT_SECONDS = 180.0


class DetachedCallError(CallProviderError):
    """The background host could not start or mint its ticket."""

    def __init__(self, message: str, *, code: ErrorCode = "internal") -> None:
        super().__init__(message)
        self.code = code


class DetachedCallBusyError(DetachedCallError):
    """The requested profile already owns a detached call child."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="busy")


class DetachedCallTimeoutError(DetachedCallError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="timeout")


class DetachedCallCancelledError(DetachedCallError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="cancelled")


class DetachedCallUnconfiguredError(DetachedCallError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="unconfigured")


class DetachedCallReplay(DetachedCallError):
    """An existing attempt has no unconsumed URL to return."""

    def __init__(self, progress: dict[str, object]) -> None:
        self.progress = progress
        state = progress["state"]
        super().__init__(
            f"Call {progress['callId']} is {state}. Use a new request ID for a new call.",
            code="busy" if state in {"starting", "active"} else "not_found",
        )


def profile_snapshot(config) -> str:
    """Freeze the resolved profile and binding without persisting their values."""
    values = config.model_dump(mode="json", exclude={"source_path": True, "agent": {"source_path"}})
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class DetachedCallRequest:
    rendezvous_url: str
    context: CallContext
    ttl_seconds: int
    config_path: str = ""
    echo: bool = False
    profile: str = ""
    local_rendezvous: bool = False
    tailnet: bool = False
    call_id: str = field(default_factory=new_call_id)
    request_id: str = ""
    profile_snapshot: str = ""

    def identity(self) -> ProfileIdentity:
        if self.config_path:
            return ProfileIdentity.from_config(
                config_path=self.config_path,
                profile=self.profile or Path(self.config_path).stem,
            )
        return ProfileIdentity.for_echo(
            agent_instance=self.context.agent_instance, profile=self.profile or "echo"
        )

    def __post_init__(self) -> None:
        if self.request_id:
            object.__setattr__(self, "call_id", request_call_id(self.request_id, self.identity()))
            if self.config_path and not self.profile_snapshot:
                from sleeptalk.config import ConfigError, SleeptalkConfig, resolve_config_document
                try:
                    config = SleeptalkConfig.model_validate(resolve_config_document(self.config_path).raw)
                except (ConfigError, OSError, ValueError):
                    raise DetachedCallError("The call profile could not be resolved for recovery.", code="config") from None
                object.__setattr__(self, "profile_snapshot", profile_snapshot(config))

    def fingerprint(self) -> str:
        values = [self.rendezvous_url, self.context.agent_instance,
                  self.context.conversation_id, self.context.platform_user_id,
                  self.ttl_seconds, self.echo, self.local_rendezvous, self.tailnet, self.profile_snapshot]
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def _recover_request(request: DetachedCallRequest, root: Path) -> str | None:
    if not request.request_id:
        return None
    progress = get_start_request(
        identity=request.identity(), request_id=request.request_id,
        root=root, fingerprint=request.fingerprint(),
    )
    if progress is None:
        return None
    url = recover_start_url(identity=request.identity(), request_id=request.request_id, root=root)
    if url is not None:
        return url
    raise DetachedCallReplay(progress)


def launch_detached_call(request: DetachedCallRequest) -> str:
    """Start a call host and return its URL once the child reports ready."""

    registry_root = (
        registry_root_for_config(request.config_path)
        if request.config_path else default_registry_root()
    )
    identity = request.identity()
    if recovered := _recover_request(request, registry_root):
        return recovered
    try:
        profile_lease = acquire_profile_lease(root=registry_root, identity=identity)
    except CallBusyError as exc:
        if recovered := _recover_request(request, registry_root):
            return recovered
        raise DetachedCallBusyError(str(exc)) from exc
    except (CallRegistryError, OSError) as exc:
        raise DetachedCallError("Local call state is unavailable.") from exc
    try:
        # A competing parent may have completed between the first lookup and lease.
        if recovered := _recover_request(request, registry_root):
            profile_lease.close()
            return recovered
        if request.request_id:
            reserve_start_request(
                root=registry_root, identity=identity, request_id=request.request_id,
                fingerprint=request.fingerprint(), ttl_seconds=request.ttl_seconds,
            )
        # In the private registry (0700), not the shared temp dir.
        descriptor, ready_name = tempfile.mkstemp(prefix="call-ready-", suffix=".json", dir=registry_root)
        os.close(descriptor)
        ready_path = Path(ready_name)
    except BaseException:
        profile_lease.close()
        raise
    # Under the private call registry (0700), never a fixed name in a shared
    # temp directory: the host's output can carry tracebacks and agent stderr.
    log_path = registry_root / "live-link-hosts.log"
    # Values ride as --flag=value: a generated ID may begin with "-", which
    # argparse would otherwise read as the next option.
    options = {
        "ready-file": str(ready_path), "call-id": request.call_id, "registry-root": str(registry_root),
        "profile-key": identity.key, "profile": identity.profile, "profile-lock-fd": str(profile_lease.fd),
        "rendezvous": request.rendezvous_url, "agent-instance": request.context.agent_instance,
        "conversation-id": request.context.conversation_id, "platform-user-id": request.context.platform_user_id,
        "ttl-seconds": str(request.ttl_seconds),
        **({"request-id": request.request_id} if request.request_id else {}),
        **({"config": request.config_path} if request.config_path else {}),
    }
    command = [sys.executable, "-m", "sleeptalk.detached_call", "--child",
               *(f"--{name}={value}" for name, value in options.items())]
    if request.echo:
        command.append("--echo")
    if request.local_rendezvous:
        command.append("--local-rendezvous")
    if request.tailnet:
        command.append("--tailnet")

    process = None
    try:
        log_fd = os.open(
            log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600,
        )
        with os.fdopen(log_fd, "ab") as log:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                pass_fds=(profile_lease.fd,),
            )
        profile_lease.close()
        deadline = time.monotonic() + _START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            payload = _read_ready_file(ready_path)
            if payload is not None:
                if error := payload.get("error"):
                    try:
                        code = validate_error_code(
                            str(payload.get("code", "provider"))
                        )
                    except ValueError:
                        code = "internal"
                    raise DetachedCallError(str(error), code=code)
                url = (
                    recover_start_url(identity=identity, request_id=request.request_id, root=registry_root)
                    if request.request_id else str(payload.get("url", ""))
                )
                if not url:
                    if request.request_id:
                        progress = get_start_request(
                            root=registry_root, identity=identity, request_id=request.request_id,
                        )
                        if progress is not None:
                            raise DetachedCallReplay(progress)
                    raise DetachedCallError("The background call host returned no link.")
                return url
            exit_code = process.poll()
            if exit_code is not None:
                raise DetachedCallError(
                    "The background call host stopped before creating a link. "
                    f"See {log_path}."
                )
            time.sleep(0.05)
        process.terminate()
        raise DetachedCallTimeoutError(
            "The background call host took too long to start. " f"See {log_path}."
        )
    except KeyboardInterrupt as exc:
        if process is not None:
            process.terminate()
        raise DetachedCallCancelledError(
            "The background call host was cancelled while starting."
        ) from exc
    finally:
        profile_lease.close()
        ready_path.unlink(missing_ok=True)


def _read_ready_file(path: Path) -> dict[str, object] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _report(path: Path, **payload: object) -> None:
    # The parent removes the rendezvous file after consuming the URL. A later
    # runtime failure belongs in the host log and must not recreate temp state.
    # Opened without O_CREAT and without following a link: another user can't
    # point this at a file of the caller's by planting a symlink in the temp dir.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload))


async def _run_child(args: argparse.Namespace) -> None:
    ready_path = Path(args.ready_file)
    context = CallContext(
        agent_instance=args.agent_instance,
        conversation_id=args.conversation_id,
        platform_user_id=args.platform_user_id,
    )

    request_id = getattr(args, "request_id", "")

    def progress(stage: str) -> None:
        if request_id:
            observe_start_request(
                root=Path(args.registry_root), request_id=request_id,
                identity=ProfileIdentity(key=args.profile_key, profile=args.profile), stage=stage,
            )

    def ready(url: str) -> None:
        if request_id:
            observe_start_request(
                root=Path(args.registry_root), request_id=request_id,
                identity=ProfileIdentity(key=args.profile_key, profile=args.profile),
                stage="link_minted", join_url=url,
            )
        if request_id:
            _report(ready_path, ready=True, pid=os.getpid())
        else:
            _report(ready_path, url=url, pid=os.getpid())

    if args.echo:
        if args.local_rendezvous or args.tailnet:
            from sleeptalk.local_rendezvous import run_local_echo_call

            await run_local_echo_call(
                context,
                ttl_seconds=args.ttl_seconds,
                on_ready=ready,
                tailnet=args.tailnet,
            )
        else:
            await run_ticket_echo_call(
                args.rendezvous,
                context,
                ttl_seconds=args.ttl_seconds,
                on_ready=ready,
            )
        return
    if not args.config:
        raise DetachedCallUnconfiguredError(
            "A runtime profile is required for an agent call. Pass --config PATH."
        )
    try:
        from sleeptalk.webcalls import resolve_web_call
    except ImportError as exc:  # pragma: no cover - sleeptalk is always importable
        raise DetachedCallUnconfiguredError(
            "The web-call runtime is unavailable. Install the sleeptalk package with the web extra and try again."
        ) from exc
    try:
        run_profile_web_call = resolve_web_call()
    except RuntimeError as exc:
        raise DetachedCallUnconfiguredError(str(exc)) from exc
    await run_profile_web_call(
        config_path=args.config,
        rendezvous_url=args.rendezvous,
        context=context,
        ttl_seconds=args.ttl_seconds,
        on_ready=ready,
        local=args.local_rendezvous and not args.tailnet,
        **({"tailnet": True} if args.tailnet else {}),
        **({"on_progress": progress} if request_id else {}),
    )


async def _run_registered_child(args: argparse.Namespace) -> None:
    identity = ProfileIdentity(key=args.profile_key, profile=args.profile)
    with hold_inherited_profile_lease(args.profile_lock_fd):
        with register_live_call(
            root=Path(args.registry_root),
            identity=identity,
            call_id=args.call_id,
        ):
            request_id = getattr(args, "request_id", "")
            async def expire_capability() -> None:
                await asyncio.sleep(args.ttl_seconds)
                get_start_request(identity=identity, request_id=request_id, root=Path(args.registry_root))
            expiry = asyncio.create_task(expire_capability()) if request_id else None
            stage = "ended"
            try:
                await _run_child(args)
            except Exception:
                stage = "failed"
                raise
            finally:
                if expiry is not None:
                    expiry.cancel()
                if request_id:
                    observe_start_request(
                        root=Path(args.registry_root), identity=identity,
                        request_id=request_id, stage=stage,
                    )


async def _run_child_supervised(args: argparse.Namespace) -> None:
    """Cancel the child task on TERM/INT so existing async cleanup unwinds."""
    task = asyncio.create_task(_run_registered_child(args))
    loop = asyncio.get_running_loop()

    def request_stop(_sig: signal.Signals) -> None:
        if not task.done():
            task.cancel()

    installed: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_stop, sig)
            installed.append(sig)
        except NotImplementedError:  # pragma: no cover - v0.1 supports POSIX hosts.
            pass
    try:
        await task
    except asyncio.CancelledError:
        if ready_file := getattr(args, "ready_file", ""):
            _report(
                Path(ready_file),
                error="The background call host was cancelled while starting.",
                code="cancelled",
            )
        return
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


def _child_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--call-id", required=True)
    parser.add_argument("--request-id", default="")
    parser.add_argument("--registry-root", required=True)
    parser.add_argument("--profile-key", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--profile-lock-fd", type=int, required=True)
    parser.add_argument("--rendezvous", required=True)
    parser.add_argument("--agent-instance", required=True)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--platform-user-id", required=True)
    parser.add_argument("--ttl-seconds", type=int, required=True)
    parser.add_argument("--config", default="")
    parser.add_argument("--echo", action="store_true")
    parser.add_argument("--local-rendezvous", action="store_true")
    parser.add_argument("--tailnet", action="store_true")
    return parser


def _setup_child_logging(args: argparse.Namespace) -> None:
    """Give the call host the profile's log file and turn timeline, as `sleeptalk run` has.

    Without this, a call started from an agent's link wrote its latency
    traces under whatever directory the host was launched from. Best effort:
    a profile that won't load is reported by the call itself.
    """
    if getattr(args, "echo", False) or not getattr(args, "config", ""):
        return
    from sleeptalk.config import load_resolved_config
    from sleeptalk.logging_setup import setup_profile_logging

    try:
        config = load_resolved_config(args.config, check_env=False, check_binaries=False, verify_binding=False)
        setup_profile_logging(args.config, config)
    except Exception:  # noqa: BLE001 — logging must never stop a call from starting.
        return


def _log_shutdown(args: argparse.Namespace) -> None:
    """How long the profile stayed busy after its call ended.

    A closed call holds the profile until this process exits; a slow exit is
    what "already has a live call" right after hanging up looks like.
    """
    from sleeptalk import web_call

    ended = web_call.call_ended_at
    if ended is None:
        return
    try:
        from sleeptalk.voice_loop.telemetry import log_latency_event

        log_latency_event(surface=f"{args.profile}-web", transport="web", persona=args.profile,
                          stage="call_host_exit", shutdownMs=round((time.monotonic() - ended) * 1000))
    except Exception:  # noqa: BLE001 - a log line never fails the exit
        pass


def main(argv: list[str] | None = None) -> int:
    from sleeptalk.logging_setup import redact_default_output

    redact_default_output()
    args = _child_parser().parse_args(argv)
    _setup_child_logging(args)
    try:
        asyncio.run(_run_child_supervised(args))
        _log_shutdown(args)
    except Exception as exc:  # noqa: BLE001 — child must report every startup failure.
        from sleeptalk.config import ConfigError

        fallback: ErrorCode
        if isinstance(exc, CallProviderError):
            fallback = "provider"
        elif isinstance(exc, ConfigError):
            fallback = "config"
        else:
            fallback = "internal"
        _report(
            Path(args.ready_file),
            error=str(exc),
            code=error_code_for(exc, fallback=fallback),
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
