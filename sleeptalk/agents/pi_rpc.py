from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import json
import logging
import secrets
import signal
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from sleeptalk.adapter import (
    AgentCapabilityManifest,
    AgentControl,
    Cancel,
    CapabilitySupport,
    ControlAccepted,
    ControlRejected,
    ControlResult,
    PermissionDecision,
    PermissionRequested,
    ResolvePermission,
    SessionIdentified,
    StatusChanged,
    Steer,
    SupportLevel,
    ToolEvent,
    ToolPhase,
    UsageReported,
    AgentRunState,
    VoiceSessionCapability,
    WorkerCapability,
    WorkerEvidence,
    publish_event,
    workbench_event,
)
from sleeptalk.agents.omp_workers import (
    is_subagent_frame,
    is_worker_tool_name,
    worker_from_lifecycle,
    worker_from_progress,
    worker_from_task_tool,
)
from sleeptalk.text import (
    extract_speakable_text as _extract_speakable_text,
)
from sleeptalk.streaming import (
    KEEPALIVE,
    DeadlineClock,
    EventGate,
    SentenceStreamer,
    StdioJsonRpcProcess,
    ToolVocab,
    await_activity,
    narrate_tool,
)

logger = logging.getLogger("sleeptalk.agents.pi_rpc")

# Narration vocabulary for pi tool names: narration kind + which arg field
# holds the spoken subject. "searching the web for" is pi's phrasing.
TOOL_VOCAB = ToolVocab(
    kinds={
        "bash": "bash",
        "shell": "bash",
        "run": "bash",
        "read": "read",
        "write": "write",
        "edit": "edit",
        "search": "search",
        "web_search": "search",
        "websearch": "search",
    },
    subject_args={
        "bash": ("command",),
        "read": ("path", "file_path"),
        "write": ("path", "file_path"),
        "edit": ("path", "file_path"),
        "search": ("query",),
    },
    verbs={
        "bash": "running",
        "read": "reading",
        "write": "writing",
        "edit": "editing",
        "search": "searching the web for",
    },
)


def tool_detail_from_call(tool_name: str, args: Any) -> str:
    """Gerund phrase describing a tool call, for spoken progress narration.

    "bash" {command:"git status"} -> "running git status". Empty string when
    there is nothing worth saying. Backward-compat shim over TOOL_VOCAB.
    """
    if not isinstance(args, dict):
        return ""
    return narrate_tool(tool_name, args, TOOL_VOCAB)[1]


def _assistant_text(message: Any) -> str:
    """Flatten an assistant message's content blocks into visible text."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(message.get("text") or "")


def _assistant_error(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    return str(message.get("errorMessage") or "").strip()


def _result_text(result: Any) -> str:
    """Flatten Pi's tool result content blocks into terminal text."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts = [
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if parts:
                return "".join(parts)
        for key in ("text", "output", "message"):
            if result.get(key) is not None:
                return str(result[key])
    return str(result)


def _new_snapshot_text(previous: str, current: str) -> str:
    """Return only the new suffix from Pi's cumulative partialResult."""
    if current.startswith(previous):
        return current[len(previous) :]
    # A tool may replace its progress display instead of appending to it.
    # Preserve the new snapshot rather than silently losing that update.
    return current if current != previous else ""


class PiRPCError(RuntimeError):
    pass


_SUPPORTED = CapabilitySupport(SupportLevel.SUPPORTED)
_UNSUPPORTED = CapabilitySupport(SupportLevel.UNSUPPORTED)

PI_RPC_MANIFEST = AgentCapabilityManifest(
    contract_version=1,
    structured_events=_SUPPORTED,
    tool_lifecycle=_SUPPORTED,
    permissions=_UNSUPPORTED,
    clarification=_UNSUPPORTED,
    session_identity=_SUPPORTED,
    status=_SUPPORTED,
    usage=_SUPPORTED,
    steering=_SUPPORTED,
    targeted_cancellation=_SUPPORTED,
    workers=WorkerCapability(SupportLevel.UNSUPPORTED),
    voice_sessions=VoiceSessionCapability(SupportLevel.UNSUPPORTED),
)

OMP_RPC_MANIFEST = replace(
    PI_RPC_MANIFEST,
    workers=WorkerCapability(
        SupportLevel.SUPPORTED, evidence=WorkerEvidence.LIFECYCLE_NATIVE
    ),
)


class PiRPCClient(StdioJsonRpcProcess):
    """Async client for pi coding agent in --mode rpc."""

    agent_capabilities = PI_RPC_MANIFEST
    warmup_timeout = 15.0
    permission_load_timeout = 15.0

    def __init__(
        self,
        *,
        cwd: str | None = None,
        model: str = "",
        provider: str = "",
        provider_slug: str = "",
        name: str = "pi-discord",
        no_session: bool = True,
        extra_args: list[str] | None = None,
        pi_bin: str = "",
        keepalive_interval_s: float = 0.0,
        env: dict[str, str] | None = None,
        permission_mode: str = "auto",
    ) -> None:
        from sleeptalk.harness_versions import pi_install_spec

        super().__init__(
            env=env,
            cwd=cwd or os.getcwd(),
            error_cls=PiRPCError,
            binary="pi",
            binary_fallback=os.path.expanduser("~/.local/bin/pi"),
            install_hint=f"pi binary not found. Install globally: npm install -g {pi_install_spec()}",
        )
        self.permission_mode = permission_mode.strip().lower() or "auto"
        if self.permission_mode not in {"auto", "pending", "deny"}:
            raise ValueError("Pi permission mode must be auto, pending, or deny")
        self._permission_nonce = ""
        self._permission_ready = asyncio.Event()
        self._permissions: dict[str, str] = {}
        self._permission_counter = itertools.count(1)
        self._sdk_launch = None
        if self.permission_mode != "auto":
            self.agent_capabilities = replace(self.agent_capabilities, permissions=_SUPPORTED)
            if isinstance(self, OmpRPCClient):
                self.agent_capabilities = replace(self.agent_capabilities, workers=WorkerCapability(SupportLevel.UNSUPPORTED), steering=_UNSUPPORTED)
        self.model = model
        self.provider = provider
        self.provider_slug = provider_slug
        self.name = name
        # The protected OMP SDK deliberately uses an in-memory session.
        self.no_session = no_session or (isinstance(self, OmpRPCClient) and self.permission_mode != "auto")
        # A folder the conversation is saved in and continued from on every
        # start (use_saved_session); empty: --no-session decides.
        self.session_dir = ""
        self.extra_args = extra_args or []
        self.pi_bin = pi_bin
        self.keepalive_interval_s = keepalive_interval_s
        self._effective_identity: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._consumer_lock = asyncio.Lock()
        self._needs_settlement = False
        self._prompt_id: str | None = None
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._stream_abort: asyncio.Event | None = None
        self._req_counter = itertools.count(1)
        self._rpc_pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.upstream_session_id = ""
        self._identified = False
        self._last_usage: dict[str, Any] | None = None
        self._on_event: Callable[..., Any] | None = None
        self._idle_task: asyncio.Task[None] | None = None

    def use_saved_session(self, directory: str | os.PathLike[str]) -> bool:
        """Keep the conversation in ``directory`` and pick it up again on
        every start: a long-lived bot answers with the same memory after a
        restart. Call before the first start. False when this launch can't
        save one (OMP's protected launch runs through its SDK)."""
        if isinstance(self, OmpRPCClient) and self.permission_mode != "auto":
            return False
        self.session_dir = str(directory)
        self.no_session = False
        return True

    async def start_new_conversation(self) -> None:
        """The next turn starts a fresh conversation. Saved ones are set
        aside next to the folder (``<folder>-earlier``), never deleted."""
        if self.session_dir:
            folder = Path(self.session_dir)
            earlier = folder.with_name(folder.name + "-earlier")
            for saved in folder.glob("*.jsonl"):
                earlier.mkdir(mode=0o700, parents=True, exist_ok=True)
                with contextlib.suppress(OSError):
                    saved.replace(earlier / saved.name)
        await self._terminate()

    async def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            await self._await_permission_extension()
            return
        if self._closed:
            raise PiRPCError("Pi RPC client is closed.")

        if self.permission_mode != "auto" and self.extra_args:
            raise PiRPCError("Protected Pi/OMP launches do not accept extra_args")

        pi_bin = self._resolve_binary(self.pi_bin)
        self._check_binary(pi_bin)

        args = [pi_bin, "--mode", "rpc"]
        if isinstance(self, OmpRPCClient) or "omp" in Path(pi_bin).name:
            # OMP's approval mode is separate from Pi's project-file trust.
            if self.permission_mode == "auto":
                args.append("--auto-approve")
        else:
            args.extend(["--approve" if self.permission_mode == "auto" else "--no-approve", "-n", self.name])
        if self.session_dir:
            args.extend(["--session-dir", self.session_dir, "--continue"])
        elif self.no_session:
            args.append("--no-session")
        if self.model:
            args.extend(["--model", self.model])
        if self.provider:
            args.extend(["--provider", self.provider])
        args.extend(self.extra_args)
        environment = dict(self._subprocess_env)
        if self.permission_mode != "auto":
            self._permission_nonce = secrets.token_hex(16)
            self._permission_ready.clear()
            environment.update({
                "SLEEPTALK_PERMISSION_MODE": self.permission_mode,
                "SLEEPTALK_PERMISSION_NONCE": self._permission_nonce,
                "SLEEPTALK_ONBOARDING_PROBE": "0",
            })
            args.extend(["--no-extensions", "--extension", str(Path(__file__).with_name("pi_permission_extension.js"))])

        if self.permission_mode != "auto" and isinstance(self, OmpRPCClient):
            from sleeptalk.agents.omp_sdk import prepare
            try:
                self._sdk_launch = prepare(pi_bin, self.cwd, self.provider, self.model, environment, self.permission_mode, self._permission_nonce, probe=False)
            except ValueError as exc:
                raise PiRPCError(str(exc)) from None
            args, environment = self._sdk_launch.args, self._sdk_launch.env

        self._needs_settlement = False
        self._prompt_id = None
        self._effective_identity.clear()
        self._permissions.clear()
        try:
            await self._spawn(args, env=environment)
        except BaseException:
            await self._terminate()
            raise
        await self._await_permission_extension()

    async def _terminate(self) -> None:
        group = self._proc.pid if self._sdk_launch is not None and self._proc is not None else None
        if group is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGTERM)
        try:
            with contextlib.suppress(ProcessLookupError):
                await super()._terminate()
        finally:
            if group is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(group, signal.SIGKILL)
            if self._sdk_launch is not None:
                self._sdk_launch.close()
                self._sdk_launch = None

    async def _await_permission_extension(self) -> None:
        if self.permission_mode == "auto":
            return
        try:
            await asyncio.wait_for(self._permission_ready.wait(), timeout=self.permission_load_timeout)
        except TimeoutError:
            await self._terminate()
            raise PiRPCError("The selected runtime did not load its required permission extension") from None
        except BaseException:
            await self._terminate()
            raise

    def latency_identity(self) -> dict[str, Any]:
        return {
            "adapter": type(self).__name__,
            "requestedAgentModel": self.model or None,
            "requestedAgentProvider": self.provider or None,
            "agentModel": None,
            "agentProvider": None,
            "agentThinkingLevel": None,
            "agentThinkingLevelStatus": "unknown",
            **self._effective_identity,
        }

    def _record_identity(self, data: dict[str, Any]) -> None:
        session_id = data.get("sessionId")
        if isinstance(session_id, str) and session_id and session_id != self.upstream_session_id:
            self.upstream_session_id = session_id
            self._identified = False
        model = data.get("model")
        if isinstance(model, dict):
            for source, target in (("id", "agentModel"), ("provider", "agentProvider")):
                value = model.get(source)
                if isinstance(value, str):
                    self._effective_identity[target] = value
        level = data.get("thinkingLevel")
        if isinstance(level, str):
            self._effective_identity["agentThinkingLevel"] = level
            self._effective_identity["agentThinkingLevelStatus"] = "observed"

    def _latency_event(self, callback: Callable | None, stage: str) -> None:
        if callback is not None:
            callback(workbench_event("latency", stage=stage, **self.latency_identity()))

    def _try_worker(
        self, event: dict[str, Any], on_event: Callable[..., Any] | None
    ) -> bool:
        return False

    def _on_stale_event(self, event: dict[str, Any]) -> None:
        return None

    def _start_idle_drain(self) -> None:
        return None

    async def _stop_idle_drain(self) -> None:
        return None

    async def _route_message(self, event: dict[str, Any]) -> None:
        if event.get("type") == "extension_ui_request":
            await self._handle_ui(event)
            return
        req_id = event.get("id")
        if event.get("type") == "agent_settled" or (
            event.get("type") == "agent_end" and event.get("isTerminal")
        ):
            self._needs_settlement = False
            self._permissions.clear()
        if (
            event.get("type") == "response"
            and req_id == self._prompt_id
            and event.get("success") is False
        ):
            self._needs_settlement = False
        if event.get("type") == "response" and req_id in self._rpc_pending:
            future = self._rpc_pending.pop(str(req_id), None)
            if future is not None and not future.done():
                future.set_result(event)
            return
        await self._event_queue.put(event)

    async def _rpc(self, payload: dict[str, Any], *, timeout: float = 10.0) -> dict[str, Any]:
        req_id = str(payload.get("id") or f"rpc-{next(self._req_counter)}")
        payload = {**payload, "id": req_id}
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._rpc_pending[req_id] = future
        try:
            await self._send_raw(payload)
            async with asyncio.timeout(timeout):
                return await future
        except TimeoutError:
            return {}
        finally:
            self._rpc_pending.pop(req_id, None)

    async def _handle_ui(self, event: dict[str, Any]) -> None:
        method = event.get("method")
        if method == "setStatus" and event.get("statusKey") == "sleeptalk-permissions":
            try:
                proof = json.loads(event.get("statusText", ""))
            except (TypeError, ValueError):
                return
            if isinstance(proof, dict) and proof.get("version") == 1 and proof.get("nonce") == self._permission_nonce and proof.get("mode") == self.permission_mode and isinstance(proof.get("workspace"), str) and Path(proof["workspace"]).resolve() == Path(self.cwd).resolve():
                self._permission_ready.set()
            return
        if method == "cancel":
            upstream_id = str(event.get("id") or "")
            self._permissions = {key: value for key, value in self._permissions.items() if value != upstream_id}
            return
        if method not in {"confirm", "select", "input", "editor"}:
            return
        if method == "confirm" and self.permission_mode == "pending" and self._on_event is not None:
            request_id = f"pi-permission-{next(self._permission_counter)}"
            self._permissions[request_id] = str(event["id"])
            publish_event(self._on_event, PermissionRequested(
                request_id=request_id,
                prompt=str(event.get("message") or event.get("title") or "Allow tool?"),
                options=frozenset({PermissionDecision.DENY, PermissionDecision.ALLOW_ONCE}),
            ))
            return
        response: dict[str, Any] = {"type": "extension_ui_response", "id": event["id"]}
        if self.permission_mode != "auto":
            response["cancelled"] = True
        elif method == "confirm":
            response["confirmed"] = True
        elif method == "select":
            options = event.get("options") or []
            response["value"] = options[0] if options else ""
        else:
            response["value"] = ""
        await self._send_raw(response)

    async def _cancel_permissions(self) -> None:
        pending = list(self._permissions.values())
        self._permissions.clear()
        for upstream_id in pending:
            with contextlib.suppress(PiRPCError, BrokenPipeError, ConnectionResetError, OSError):
                await self._send_raw({"type": "extension_ui_response", "id": upstream_id, "cancelled": True})

    async def close(self) -> None:
        self._permissions.clear()
        await super().close()

    async def _send_raw(self, cmd: dict[str, Any]) -> None:
        await self._send_json(cmd)

    async def respond(
        self,
        message: str,
        timeout: float = 300.0,
        queue_timeout: float = 30.0,
    ) -> str:
        """Send a prompt and return the full natural-language response."""
        parts: list[str] = []
        async for sentence in self.respond_streaming(
            message, timeout=timeout, queue_timeout=queue_timeout
        ):
            parts.append(sentence)
        return _extract_speakable_text(" ".join(parts))

    async def respond_streaming(
        self,
        message: str,
        timeout: float = 300.0,
        on_event: Callable[[str], None] | None = None,
        queue_timeout: float = 30.0,
    ) -> AsyncIterator[str]:
        """Own one stream from admission through its final output.

        Queueing is local because Pi's uncorrelated follow-up events cannot be
        assigned to separate callers. Abort uses the independent command lock.
        """
        deadline = asyncio.get_running_loop().time() + queue_timeout
        self._latency_event(on_event, "agent_admission_started")
        if queue_timeout <= 0:
            if self._consumer_lock.locked():
                raise PiRPCError("Pi is busy; warmup skipped.")
            await self._consumer_lock.acquire()
        else:
            try:
                async with asyncio.timeout(queue_timeout):
                    await self._consumer_lock.acquire()
            except TimeoutError as exc:
                raise PiRPCError("Pi request timed out waiting for the active request.") from exc
        try:
            await self._stop_idle_drain()
            async with self._lock:
                await self._ensure_started()
            await self._wait_until_idle(deadline, wait=queue_timeout > 0)
            self._latency_event(on_event, "agent_admitted")
            async with contextlib.aclosing(
                self._respond_owned(message, timeout, on_event)
            ) as stream:
                async for sentence in stream:
                    yield sentence
        finally:
            self._consumer_lock.release()
            self._start_idle_drain()

    async def _wait_until_idle(self, deadline: float, *, wait: bool) -> None:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if wait and remaining <= 0:
                raise PiRPCError("Pi request timed out waiting for previous work to settle.")
            state = await self._rpc(
                {"type": "get_state"}, timeout=min(5.0, remaining) if wait else 5.0
            )
            data = state.get("data")
            if not state.get("success") or not isinstance(data, dict):
                raise PiRPCError("Pi could not confirm whether previous work has settled.")
            self._record_identity(data)
            idle = (
                data.get("isStreaming") is False
                and not data.get("isCompacting", False)
                and not data.get("pendingMessageCount", 0)
                and not self._needs_settlement
            )
            # A state response is a wire-order boundary. Preserve detached worker
            # events from completed runs without presenting their assistant text.
            settled_in_queue = False
            while not self._event_queue.empty():
                event = self._event_queue.get_nowait()
                self._on_stale_event(event)
                settled_in_queue |= event.get("type") == "agent_settled" or (
                    event.get("type") == "agent_end" and bool(event.get("isTerminal"))
                )
            if idle:
                return
            if not wait:
                raise PiRPCError("Pi is busy; warmup skipped.")
            if settled_in_queue:
                continue
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                try:
                    async with asyncio.timeout(max(0, remaining)):
                        event = await self._event_queue.get()
                except TimeoutError as exc:
                    raise PiRPCError(
                        "Pi request timed out waiting for previous work to settle."
                    ) from exc
                self._on_stale_event(event)
                if event.get("type") == "agent_settled" or (
                    event.get("type") == "agent_end" and event.get("isTerminal")
                ):
                    break

    async def _respond_owned(
        self, message: str, timeout: float, on_event: Callable[[str], None] | None
    ) -> AsyncIterator[str]:
        abort_event = asyncio.Event()
        accepted = False
        settled = False
        try:
            async with self._lock:
                self._on_event = on_event
                self._stream_abort = abort_event
                prompt_id = f"prompt-{next(self._req_counter)}"
                self._prompt_id = prompt_id
                self._needs_settlement = True
                await self._send_raw({"id": prompt_id, "type": "prompt", "message": message})
                self._latency_event(on_event, "agent_prompt_sent")

            preflight_events: deque[dict[str, Any]] = deque()
            started = False
            saw_text = False
            # A failed model call, held until the run ends: pi and omp retry
            # rate limits, overloads and stalls on their own, and raising on
            # the first failure turned a recovered tool turn into an apology.
            failed = ""
            streamer = SentenceStreamer()
            gate = EventGate(on_event)
            tool_snapshots: dict[str, str] = {}
            clock = DeadlineClock(
                timeout,
                idle_error=lambda: PiRPCError("Pi response timed out."),
                hard_error=lambda: PiRPCError("Pi response exceeded maximum time."),
                keepalive_interval_s=self.keepalive_interval_s,
                on_keepalive=lambda: gate.emit("keepalive"),
            )
            while not settled:
                if abort_event.is_set():
                    return
                if accepted and preflight_events:
                    event = preflight_events.popleft()
                else:
                    get_task = asyncio.ensure_future(self._event_queue.get())
                    abort_task = asyncio.ensure_future(abort_event.wait())
                    try:
                        which = await await_activity(
                            # Abort wins when both waiters complete together.
                            clock, {"abort": abort_task, "event": get_task}
                        )
                    finally:
                        for task in (get_task, abort_task):
                            if not task.done():
                                task.cancel()
                                with contextlib.suppress(asyncio.CancelledError):
                                    await task
                    if which == KEEPALIVE:
                        continue
                    if which == "abort":
                        return
                    event = get_task.result()
                # Any activity (text, thinking, tool events) resets the idle
                # clock — only sustained silence kills a turn. Coding turns
                # run for minutes with events flowing the whole time.
                clock.note_activity()

                etype = event.get("type")
                if etype == "response":
                    if event.get("id") != prompt_id:
                        continue
                    if not event.get("success", True):
                        raise PiRPCError(f"Pi prompt failed: {event.get('error', 'unknown error')}")
                    accepted = True
                    continue
                if self._try_worker(event, on_event):
                    continue
                if not accepted:
                    preflight_events.append(event)
                    continue
                if etype == "agent_start":
                    started = True
                    streamer.clear_buffer()
                    self._emit_identity(on_event)
                    publish_event(on_event, StatusChanged(state=AgentRunState.WORKING))
                elif etype == "message_update" and started:
                    delta = event.get("assistantMessageEvent", {})
                    delta_type = delta.get("type")
                    if delta_type == "thinking_delta":
                        gate.fire("thinking")
                        gate.emit(workbench_event("thinking"))
                    elif delta_type == "text_delta":
                        gate.fire("text")
                        raw_text = str(delta.get("delta", ""))
                        if raw_text:
                            saw_text = True
                            gate.emit(workbench_event("text", text=raw_text))
                        for sentence in streamer.feed(raw_text):
                            clock.note_audible()
                            yield sentence
                elif etype == "message_end" and started:
                    message = (
                        event.get("message")
                        if isinstance(event.get("message"), dict)
                        else {}
                    )
                    if str(message.get("role") or "") != "assistant":
                        continue
                    error = _assistant_error(message)
                    if error:
                        logger.warning("agent model call failed: %s", error)
                        failed = error
                        continue
                    failed = ""
                    if saw_text:
                        continue
                    raw_text = _assistant_text(message)
                    if not raw_text:
                        continue
                    saw_text = True
                    gate.fire("text")
                    gate.emit(workbench_event("text", text=raw_text))
                    for sentence in streamer.feed(raw_text):
                        clock.note_audible()
                        yield sentence
                elif etype == "tool_execution_start":
                    gate.fire("tool")
                    tool_name = str(event.get("toolName", "")).strip()
                    if tool_name:
                        # Detail BEFORE the kind event so narration picks
                        # it up in the same tick.
                        detail = narrate_tool(tool_name, event.get("args"), TOOL_VOCAB)[1]
                        if detail:
                            gate.emit(f"tooldetail:{detail}")
                            clock.note_audible()
                        gate.emit(f"tool:{tool_name}")
                        gate.emit(
                            workbench_event(
                                "tool_start",
                                name=tool_name,
                                detail=detail,
                                input=event.get("args"),
                                tool_call_id=str(event.get("toolCallId", "")),
                            )
                        )
                elif etype == "tool_execution_update":
                    tool_call_id = str(event.get("toolCallId", ""))
                    current = _result_text(event.get("partialResult"))
                    previous = tool_snapshots.get(tool_call_id, "")
                    new_text = _new_snapshot_text(previous, current)
                    tool_snapshots[tool_call_id] = current
                    if new_text:
                        gate.emit(
                            workbench_event(
                                "tool_update",
                                tool_call_id=tool_call_id,
                                content=new_text,
                                status="running",
                            )
                        )
                elif etype == "tool_execution_end":
                    tool_call_id = str(event.get("toolCallId", ""))
                    current = _result_text(event.get("result"))
                    previous = tool_snapshots.pop(tool_call_id, "")
                    new_text = _new_snapshot_text(previous, current)
                    is_error = bool(event.get("isError", False) or event.get("error"))
                    gate.emit(
                        workbench_event(
                            "tool_result",
                            tool_call_id=tool_call_id,
                            content=new_text,
                            status="failed" if is_error else "done",
                            is_error=is_error,
                        )
                    )
                    publish_event(
                        on_event,
                        ToolEvent(
                            name=str(event.get("toolName") or ""),
                            call_id=tool_call_id or None,
                            phase=ToolPhase.FAILED if is_error else ToolPhase.COMPLETED,
                        ),
                    )
                elif etype == "auto_retry_start":
                    logger.warning(
                        "agent retrying (attempt %s of %s)",
                        event.get("attempt"), event.get("maxAttempts"),
                    )
                    failed = ""
                elif etype == "auto_retry_end":
                    if not event.get("success", True):
                        raise PiRPCError(
                            str(event.get("finalError") or failed or "retries exhausted").splitlines()[0]
                        )
                elif etype == "agent_settled":
                    if started:
                        if failed:
                            raise PiRPCError(failed.splitlines()[0])
                        settled = True
                    publish_event(on_event, StatusChanged(state=AgentRunState.COMPLETED))
                    self._emit_usage(on_event)
                elif etype == "agent_end":
                    self._last_usage = _usage_from_messages(event.get("messages"))
                    messages = event.get("messages")
                    if isinstance(messages, list):
                        last = next(
                            (
                                message
                                for message in reversed(messages)
                                if isinstance(message, dict) and message.get("role") == "assistant"
                            ),
                            None,
                        )
                        error = _assistant_error(last) if last is not None else ""
                        if error:
                            if error != failed:
                                logger.warning("agent run ended with an error: %s", error)
                            failed = error
                        elif not saw_text:
                            raw_text = next(
                                (
                                    text
                                    for message in reversed(messages)
                                    if isinstance(message, dict) and message.get("role") == "assistant"
                                    for text in (_assistant_text(message),)
                                    if text
                                ),
                                "",
                            )
                            if raw_text:
                                saw_text = True
                                gate.fire("text")
                                for sentence in streamer.feed(raw_text):
                                    clock.note_audible()
                                    yield sentence
                    # One low-level run completed; pi may still continue through
                    # retry, compaction, or queued follow-ups. Keep waiting for
                    # agent_settled — except omp, which never emits agent_settled
                    # and marks the terminal run with isTerminal instead.
                    if event.get("isTerminal"):
                        if failed:
                            raise PiRPCError(failed.splitlines()[0])
                        settled = True
                        self._emit_usage(on_event)
                elif etype == "extension_error":
                    pass

            tail = streamer.flush()
            if tail:
                yield tail
        except (PiRPCError, asyncio.CancelledError, GeneratorExit):
            if accepted and not settled and not abort_event.is_set():
                await self.abort()
            raise
        finally:
            async with self._lock:
                if self._stream_abort is abort_event:
                    self._stream_abort = None

    async def abort(self) -> None:
        """Abort the owned prompt without cancelling work we are waiting behind."""
        await self._cancel_permissions()
        async with self._lock:
            stream_abort = self._stream_abort
            if stream_abort is None:
                return
            if self._proc is not None and self._proc.returncode is None:
                await self._send_raw({"type": "abort"})
            stream_abort.set()

    async def warmup(self) -> None:
        async with self._lock:
            await self._ensure_started()
            state = await self._rpc({"type": "get_state"}, timeout=5.0)
            data = state.get("data") if isinstance(state.get("data"), dict) else {}
            self._record_identity(data)
        # Warmup never enters the local queue ahead of real work.
        await self.respond(
            "Say OK.", timeout=self.warmup_timeout, queue_timeout=0
        )

    async def control(self, command: AgentControl) -> ControlResult:
        if isinstance(command, ResolvePermission):
            if command.decision not in {PermissionDecision.DENY, PermissionDecision.ALLOW_ONCE}:
                return ControlRejected(reason="Decision was not offered by the agent", retryable=False)
            upstream_id = self._permissions.pop(command.request_id, None)
            if upstream_id is None:
                return ControlRejected(reason="Unknown or expired permission request", retryable=False)
            try:
                await self._send_raw({"type": "extension_ui_response", "id": upstream_id, "confirmed": command.decision is PermissionDecision.ALLOW_ONCE})
            except (PiRPCError, BrokenPipeError, ConnectionResetError, OSError):
                return ControlRejected(reason="Permission connection closed", retryable=False)
            return ControlAccepted(request_id=command.request_id)
        await self._ensure_started()
        if isinstance(command, Steer):
            result = await self._rpc({"type": "steer", "message": command.instruction})
            if result.get("success"):
                return ControlAccepted(resource=self.upstream_session_id or None)
            return ControlRejected(
                reason=str(result.get("error") or "pi rejected steer"),
                retryable=True,
            )
        if isinstance(command, Cancel):
            await self.abort()
            return ControlAccepted(request_id=command.target_id)
        return ControlRejected(reason="unsupported control", retryable=False)

    def _emit_identity(self, on_event: Callable[..., Any] | None) -> None:
        if self._identified or not self.upstream_session_id:
            return
        self._identified = True
        publish_event(
            on_event,
            SessionIdentified(
                upstream_session_id=self.upstream_session_id,
                resumable=not self.no_session,
            ),
        )

    def _emit_usage(self, on_event: Callable[..., Any] | None) -> None:
        usage = self._last_usage
        if not usage:
            return
        cost = usage.get("cost")
        minor = None
        if isinstance(cost, dict) and cost.get("total") is not None:
            minor = int(round(float(cost["total"]) * 100))
        elif isinstance(cost, (int, float)):
            minor = int(round(float(cost) * 100))
        publish_event(
            on_event,
            UsageReported(
                input_tokens=_as_int(usage.get("input")),
                output_tokens=_as_int(usage.get("output")),
                cost_minor_units=minor,
                currency="USD" if minor is not None else None,
            ),
        )


class OmpRPCClient(PiRPCClient):
    agent_capabilities = OMP_RPC_MANIFEST
    warmup_timeout = 60.0

    async def _spawn(self, args: list[str], env: dict[str, str] | None = None) -> None:
        if self._sdk_launch is None:
            await super()._spawn(args, env)
            return
        self._proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, cwd=self.cwd, env=env, start_new_session=True,
        )
        self._reader_task = asyncio.create_task(self._read_stdout(), name=self.reader_task_name)

    async def close(self) -> None:
        await self._stop_idle_drain()
        await super().close()

    async def _ensure_started(self) -> None:
        was_alive = self._proc is not None and self._proc.returncode is None
        await super()._ensure_started()
        if was_alive:
            return
        await self._subscribe_subagents()

    def _on_stale_event(self, event: dict[str, Any]) -> None:
        self._try_worker(event, self._on_event)

    def _start_idle_drain(self) -> None:
        if self._closed or self._idle_task is not None:
            return
        self._idle_task = asyncio.create_task(
            self._idle_drain(), name="omp-idle-drain"
        )

    async def _stop_idle_drain(self) -> None:
        task = self._idle_task
        self._idle_task = None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _idle_drain(self) -> None:
        try:
            while not self._closed:
                event = await self._event_queue.get()
                if not self._try_worker(event, self._on_event):
                    continue
        except asyncio.CancelledError:
            raise

    async def _subscribe_subagents(self) -> None:
        await self._rpc({"type": "set_subagent_subscription", "level": "progress"})

    def _try_worker(
        self, event: dict[str, Any], on_event: Callable[..., Any] | None
    ) -> bool:
        etype = str(event.get("type") or "")
        if is_subagent_frame(event):
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            worker = None
            if etype == "subagent_lifecycle":
                worker = worker_from_lifecycle(payload)
            elif etype == "subagent_progress":
                worker = worker_from_progress(payload)
            if worker is not None:
                publish_event(on_event, worker)
            return True
        if etype in {"tool_execution_start", "tool_execution_update", "tool_execution_end"}:
            name = str(event.get("toolName") or event.get("tool_name") or "")
            if is_worker_tool_name(name):
                if etype != "tool_execution_update":
                    worker = worker_from_task_tool(event)
                    if worker is not None:
                        publish_event(on_event, worker)
                return True
        return False


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _usage_from_messages(messages: Any) -> dict[str, Any] | None:
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if isinstance(message, dict) and isinstance(message.get("usage"), dict):
            return message["usage"]
    return None
