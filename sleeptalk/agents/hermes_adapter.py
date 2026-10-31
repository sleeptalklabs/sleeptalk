"""Hermes ACP fallback — drives `hermes acp` (Agent Communication Protocol).

Voice prefers `HermesGatewayClient` (TUI Gateway JSON-RPC). This client is
the local/offline path when that gateway is missing or incompatible.

Hermes ACP speaks JSON-RPC 2.0 over stdio (NDJSON). The flow:
initialize -> session/new -> session/prompt, with streaming session/update
notifications carrying text chunks, thought chunks, and tool call events.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from dataclasses import replace
from typing import Any

from sleeptalk.agent_environment import agent_environment
from sleeptalk.agents.acp import read_workspace_text

from sleeptalk.adapter import (
    AgentCapabilityManifest,
    AgentControl,
    Cancel,
    ControlAccepted,
    ControlRejected,
    ControlResult,
    PermissionDecision,
    PermissionRequested,
    ResolvePermission,
    AssistantDelta,
    CapabilitySupport,
    SupportLevel,
    VoiceSessionCapability,
    WorkerCapability,
    WorkerEvidence,
    WorkerEvent,
    WorkerPhase,
    publish_event,
    workbench_event,
)
from sleeptalk.agents.hermes_workers import (
    is_acp_delegate,
    worker_from_acp,
    worker_from_live_manifest,
)
from sleeptalk.text import extract_speakable_text
from sleeptalk.streaming import (
    KEEPALIVE,
    DeadlineClock,
    EventGate,
    SentenceStreamer,
    ToolVocab,
    await_activity,
    narrate_tool,
)

# Narration vocabulary for ACP tool kinds. Hermes narrates from the
# tool_call title; unmapped kinds fall back to the title verbatim.
TOOL_VOCAB = ToolVocab(
    kinds={
        "execute": "bash",
        "read": "read",
        "edit": "edit",
        "delete": "edit",
        "move": "edit",
        "search": "search",
        "fetch": "search",
        "think": "other",
    },
    fallback_to_subject=True,
)


ACP_REQUEST_TIMEOUT = 30.0


class HermesACPError(RuntimeError):
    pass


_PERMISSION_KINDS = {
    "allow_once": PermissionDecision.ALLOW_ONCE,
    "allow_session": PermissionDecision.ALLOW_SESSION,
    "reject_once": PermissionDecision.DENY,
    "deny_once": PermissionDecision.DENY,
    "deny": PermissionDecision.DENY,
    "reject": PermissionDecision.DENY,
}


def _permission_choices(raw: Any) -> dict[PermissionDecision, str]:
    choices: dict[PermissionDecision, str] = {}
    for option in raw if isinstance(raw, list) else []:
        if not isinstance(option, dict):
            continue
        option_id = option.get("optionId") or option.get("id")
        kind = option.get("kind", option_id)
        if not isinstance(option_id, str) or not isinstance(kind, str):
            continue
        normalized_kind = kind.replace("-", "_")
        decision = _PERMISSION_KINDS.get(normalized_kind)
        if option_id in {"allow_always", "reject_always", "deny_always"} or normalized_kind in {"reject_always", "deny_always"}:
            continue
        if option_id == "allow_session" and normalized_kind.startswith("allow_"):
            decision = PermissionDecision.ALLOW_SESSION
        elif normalized_kind in {"allow_always", "allow_session"}:
            decision = None
        if option_id and decision is not None:
            choices.setdefault(decision, option_id)
    return choices


def _permission_response(req_id: Any, option_id: str | None) -> dict[str, Any]:
    outcome = {"outcome": "selected", "optionId": option_id} if option_id else {"outcome": "cancelled"}
    return {"jsonrpc": "2.0", "id": req_id, "result": {"outcome": outcome}}


_SUPPORTED = CapabilitySupport(SupportLevel.SUPPORTED)
_UNSUPPORTED = CapabilitySupport(SupportLevel.UNSUPPORTED)

HERMES_ACP_MANIFEST = AgentCapabilityManifest(
    contract_version=1,
    structured_events=_SUPPORTED,
    tool_lifecycle=_SUPPORTED,
    permissions=_SUPPORTED,
    clarification=_UNSUPPORTED,
    session_identity=_UNSUPPORTED,
    status=_UNSUPPORTED,
    usage=_UNSUPPORTED,
    steering=_UNSUPPORTED,
    targeted_cancellation=_SUPPORTED,
    workers=WorkerCapability(
        SupportLevel.SUPPORTED, evidence=WorkerEvidence.CORRELATED_CHILD
    ),
    voice_sessions=VoiceSessionCapability(SupportLevel.UNSUPPORTED),
)


class HermesACPClient:
    """AgentAdapter for Hermes Agent over ACP."""

    agent_capabilities = HERMES_ACP_MANIFEST

    def __init__(
        self,
        *,
        cwd: str | None = None,
        name: str = "hermes",
        extra_args: list[str] | None = None,
        permission_mode: str = "auto",
        keepalive_interval_s: float = 0.0,
        env: dict[str, str] | None = None,
        bridge_config=None,
        binary: str = "hermes",
        binary_fallback: str = "~/.local/bin/hermes",
        install_hint: str = (
            "hermes binary not found. Install: "
            "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
        ),
    ) -> None:
        self._bridge_config = bridge_config
        if bridge_config is not None:
            self.agent_capabilities = replace(HERMES_ACP_MANIFEST, tool_lifecycle=_UNSUPPORTED,
                                              workers=WorkerCapability(SupportLevel.UNSUPPORTED))
        self._bridge_launch = None
        self._env = agent_environment(env)
        self.cwd = cwd or os.getcwd()
        self.name = name
        self.extra_args = extra_args or []
        # ACP server binary + arg shape: `<binary> acp ...`. OpenClaw reuses
        # this client with binary="openclaw" (the `openclaw acp` bridge).
        self.binary = binary
        self.binary_fallback = binary_fallback
        self.install_hint = install_hint
        self.permission_mode = permission_mode.strip().lower() or "auto"
        if self.permission_mode not in {"auto", "deny", "pending"}:
            raise ValueError("Hermes permission mode must be auto, deny, or pending")
        self._permission_counter = itertools.count(1)
        self._permissions: dict[str, tuple[Any, dict[PermissionDecision, str]]] = {}
        self.keepalive_interval_s = keepalive_interval_s
        self._effective_identity: dict[str, Any] = {}
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        self._active_prompt: asyncio.Future | None = None
        self._cancelled_prompt: asyncio.Future | None = None
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._session_file: Path | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._req_counter = itertools.count(1)
        self._session_id: str = ""
        self._closed = False
        self._on_event: Callable[..., Any] | None = None
        self._known_delegates: set[str] = set()
        self._idle_task: asyncio.Task | None = None
        self._watch_tasks: dict[str, asyncio.Task] = {}
        self._claimed_manifests: set[str] = set()
        self._hermes_home = Path(
            self._env.get("HERMES_HOME") or (Path.home() / ".hermes")
        ).expanduser()

    # ---- process lifecycle ----

    async def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.returncode is None and self._session_id:
            return
        if self._closed:
            raise HermesACPError("Hermes ACP client is closed.")

        environment, cwd = self._env, self.cwd
        if self._bridge_config is not None:
            from sleeptalk.agents.hermes_bridge import prepare
            if self._bridge_launch is not None:
                self._bridge_launch.close()
            self._bridge_launch = prepare(self._bridge_config, self._env, transport="acp")
            argv = self._bridge_launch.args
            environment, cwd = self._bridge_launch.env, self._bridge_launch.home.name
        else:
            acp_bin = shutil.which(self.binary, path=self._env.get("PATH"))
            if acp_bin is None and self.binary_fallback:
                acp_bin = os.path.expanduser(self.binary_fallback)
            if not acp_bin or not Path(acp_bin).exists():
                raise HermesACPError(self.install_hint)
            argv = [acp_bin, "acp", *self.extra_args]

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env=environment,
            )
            self._session_id = ""
            # Request ids are per-connection; a fresh process starts a fresh space.
            self._req_counter = itertools.count(1)
            self._reader_task = asyncio.create_task(
                self._read_stdout(), name="hermes-acp-reader"
            )
            initialized = await self._request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": True, "writeTextFile": False}
                    },
                },
            )
            info = initialized.get("agentInfo")
            if isinstance(info, dict) and isinstance(info.get("version"), str):
                self._effective_identity["agentVersion"] = info["version"]
            result = await self._continue_saved_session(initialized)
            if result is None:
                result = await self._request(
                    "session/new", {"cwd": self.cwd, "mcpServers": []}
                )
                self._session_id = str(result.get("sessionId", ""))
                self._save_session_id()
            if not self._session_id:
                raise HermesACPError("Hermes did not return a sessionId.")
            models = result.get("models")
            if isinstance(models, dict) and isinstance(models.get("currentModelId"), str):
                self._effective_identity["agentModel"] = models["currentModelId"]
            self._record_session_thinking(result.get("configOptions"))
        except BaseException:
            try:
                await self._restart()
            finally:
                if self._bridge_launch is not None:
                    self._bridge_launch.close()
                    self._bridge_launch = None
            raise

    def use_saved_session(self, directory: str | os.PathLike[str]) -> bool:
        """Keep the Hermes session ID in ``directory`` and resume that session
        on every start: a long-lived bot answers with the same memory after a
        restart. Call before the first start. False for the protected launch,
        whose Hermes home is temporary."""
        if self._bridge_config is not None:
            return False
        self._session_file = Path(directory) / "hermes-session-id"
        return True

    async def start_new_conversation(self) -> None:
        """The next turn starts a fresh Hermes session; the old one stays in
        Hermes's own history."""
        if self._session_file is not None:
            with contextlib.suppress(OSError):
                self._session_file.unlink()
        await self._restart()

    def _save_session_id(self) -> None:
        if self._session_file is None or not self._session_id:
            return
        try:
            self._session_file.write_text(self._session_id + "\n", encoding="utf-8")
            self._session_file.chmod(0o600)
        except OSError:
            return

    async def _continue_saved_session(self, initialized: dict[str, Any]) -> dict[str, Any] | None:
        """Resume the saved session; None to start a new one. Hermes replays
        the old conversation as session/update events before it answers;
        they are history, not a turn, so they are dropped."""
        if self._session_file is None:
            return None
        try:
            saved = self._session_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        caps = initialized.get("agentCapabilities") if isinstance(initialized, dict) else None
        caps = caps if isinstance(caps, dict) else {}
        sessions = caps.get("sessionCapabilities") if isinstance(caps.get("sessionCapabilities"), dict) else {}
        method = "session/resume" if "resume" in sessions else "session/load" if caps.get("loadSession") else ""
        if not saved or not method:
            return None
        try:
            result = await self._request(method, {"sessionId": saved, "cwd": self.cwd, "mcpServers": []})
        except HermesACPError:
            return None
        finally:
            while not self._event_queue.empty():
                self._event_queue.get_nowait()
        self._session_id = str(result.get("sessionId") or saved)
        return result

    def latency_identity(self) -> dict[str, Any]:
        return {
            "adapter": type(self).__name__,
            "agentModel": None,
            "agentProvider": None,
            "agentThinkingLevel": None,
            "agentThinkingLevelStatus": "unknown",
            **self._effective_identity,
        }

    def _record_session_thinking(self, options: Any) -> None:
        if not isinstance(options, list):
            return
        for option in options:
            if not isinstance(option, dict) or option.get("category") != "thought_level":
                continue
            current = option.get("currentValue")
            if isinstance(current, str):
                self._effective_identity["agentThinkingLevel"] = current
                self._effective_identity["agentThinkingLevelStatus"] = "observed"
            return

    def _latency_event(self, callback: Callable | None, stage: str) -> None:
        if callback is not None:
            callback(workbench_event("latency", stage=stage, **self.latency_identity()))

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        buffer = b""
        try:
            while True:
                chunk = await self._proc.stdout.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while True:
                    newline = buffer.find(b"\n")
                    if newline == -1:
                        break
                    line = buffer[:newline]
                    if line.endswith(b"\r"):
                        line = line[:-1]
                    buffer = buffer[newline + 1 :]
                    if not line:
                        continue
                    try:
                        message = json.loads(line.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    await self._handle_message(message)
        except asyncio.CancelledError:
            pass
        finally:
            # EOF can arrive before initialize or session/new replies. Settle
            # every waiter so an exited agent cannot leave startup hanging.
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(HermesACPError("Hermes ACP connection closed."))
            self._pending.clear()

    async def _handle_message(self, message: dict[str, Any]) -> None:
        # Response to one of our requests
        if "id" in message and ("result" in message or "error" in message):
            future = self._pending.pop(message["id"], None)
            if future is not None and not future.done():
                if "error" in message:
                    future.set_exception(
                        HermesACPError(str(message["error"]))
                    )
                else:
                    future.set_result(message.get("result") or {})
            return
        # Agent -> client request (permissions, fs access).
        if "method" in message and "id" in message:
            await self._handle_agent_request(message)
            return
        # Notification
        if message.get("method") == "session/update":
            await self._event_queue.put(message)

    async def _handle_agent_request(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        req_id = message["id"]
        if method == "session/request_permission":
            params = message.get("params") or {}
            options = _permission_choices(params.get("options"))
            if self.permission_mode == "pending" and self._on_event is not None:
                request_id = f"hermes-permission-{next(self._permission_counter)}"
                self._permissions[request_id] = (req_id, options)
                tool = params.get("toolCall") or {}
                publish_event(self._on_event, PermissionRequested(
                    request_id=request_id,
                    prompt=(f"Allow the Hermes {tool.get('title')} operation?" if self._bridge_config is not None
                            and tool.get("title") in {"terminal", "read_file", "write_file", "patch", "search_files"}
                            else "Allow the pending agent tool operation?"),
                    options=frozenset(options) | {PermissionDecision.DENY},
                    tool_call_id=str(tool.get("toolCallId") or "") or None,
                ))
                return
            decision = PermissionDecision.DENY
            if self.permission_mode == "auto":
                for candidate in (PermissionDecision.ALLOW_SESSION, PermissionDecision.ALLOW_ONCE):
                    if candidate in options:
                        decision = candidate
                        break
            await self._send(_permission_response(req_id, options.get(decision)))
            return
        if method == "fs/read_text_file":
            path = str(message.get("params", {}).get("path", "") or "")
            try:
                content = read_workspace_text(self.cwd, path)
                await self._send(
                    {"jsonrpc": "2.0", "id": req_id, "result": {"content": content}}
                )
            except OSError as exc:
                await self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32602, "message": str(exc)},
                    }
                )
            return
        # Unknown client-bound request: decline cleanly so the agent falls
        # back to its own tools.
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unsupported: {method}"},
            }
        )

    async def control(self, command: AgentControl) -> ControlResult:
        if isinstance(command, ResolvePermission):
            pending = self._permissions.get(command.request_id)
            if pending is None:
                return ControlRejected(reason="Unknown or expired permission request", retryable=False)
            req_id, options = pending
            if command.decision is not PermissionDecision.DENY and command.decision not in options:
                return ControlRejected(reason="Decision was not offered by the agent", retryable=False)
            self._permissions.pop(command.request_id)
            try:
                await self._send(_permission_response(req_id, options.get(command.decision)))
            except (HermesACPError, BrokenPipeError, ConnectionResetError, OSError):
                return ControlRejected(reason="Permission connection closed", retryable=False)
            return ControlAccepted(request_id=command.request_id)
        if isinstance(command, Cancel):
            await self.abort()
            return ControlAccepted(request_id=command.target_id)
        return ControlRejected(reason="Unsupported control", retryable=False)

    async def _cancel_permissions(self) -> None:
        pending = list(self._permissions.values())
        self._permissions.clear()
        for req_id, _ in pending:
            with contextlib.suppress(HermesACPError, BrokenPipeError, ConnectionResetError, OSError):
                await self._send(_permission_response(req_id, None))

    async def _send(self, message: dict[str, Any]) -> None:
        async with self._command_lock:
            await self._write_command(message)

    async def _write_command(self, message: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        await self._proc.stdin.drain()

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        req_id = next(self._req_counter)
        future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        try:
            async with asyncio.timeout(ACP_REQUEST_TIMEOUT):
                await self._send(
                    {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
                )
                return await future
        except TimeoutError as exc:
            raise HermesACPError(f"Hermes {method} timed out.") from exc
        finally:
            self._pending.pop(req_id, None)
            if not future.done():
                future.cancel()

    # ---- AgentAdapter protocol ----

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        parts: list[str] = []
        async for sentence in self.respond_streaming(text, timeout=timeout):
            parts.append(sentence)
        return extract_speakable_text(" ".join(parts))

    async def _restart(self) -> None:
        """Tear down a wedged process/session; next turn spawns a fresh one.

        Unlike close(), does not mark the client permanently closed. Used when
        the ACP server errors internally: its prompt queue can jam behind the
        dead turn (every later prompt answered "Queued for the next turn."),
        so reusing the session is not safe.
        """
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        self._reader_task = None
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        self._proc = None
        self._session_id = ""
        self._permissions.clear()
        self._effective_identity.clear()
        for pending in self._pending.values():
            if not pending.done():
                pending.set_exception(HermesACPError("Hermes session restarted."))
        self._pending.clear()
        await self._stop_idle_drain()
        self._known_delegates.clear()
        self._claimed_manifests.clear()
        self._cancel_watches()
        while not self._event_queue.empty():
            self._event_queue.get_nowait()

    async def respond_streaming(
        self,
        text: str,
        timeout: float = 300.0,
        on_event: Callable[[str], None] | None = None,
    ) -> AsyncIterator[str]:
        self._latency_event(on_event, "agent_admission_started")
        async with self._lock:
            await self._stop_idle_drain()
            self._on_event = on_event
            await self._ensure_started()
            self._latency_event(on_event, "agent_admitted")

            req_id = next(self._req_counter)
            future = asyncio.get_running_loop().create_future()
            self._pending[req_id] = future
            self._active_prompt = future
            try:
                await self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "method": "session/prompt",
                        "params": {
                            "sessionId": self._session_id,
                            "prompt": [{"type": "text", "text": text}],
                        },
                    }
                )
            except (Exception, asyncio.CancelledError):
                future.cancel()
                self._active_prompt = None
                self._pending.pop(req_id, None)
                await self._restart()
                raise
            self._latency_event(on_event, "agent_prompt_sent")

            streamer = SentenceStreamer()
            gate = EventGate(on_event)
            clock = DeadlineClock(
                timeout,
                idle_error=lambda: HermesACPError("Hermes response timed out."),
                hard_error=lambda: HermesACPError(
                    "Hermes response exceeded maximum time."
                ),
                keepalive_interval_s=self.keepalive_interval_s,
                on_keepalive=lambda: gate.emit("keepalive"),
            )
            prompt_task = asyncio.ensure_future(future)
            get_task = None

            # ACP ordering guarantee: all session/update notifications for a
            # turn arrive before the session/prompt response. So we wait on
            # BOTH the event queue and the prompt future, and once the future
            # completes we drain any queued updates before finishing.
            try:
                while True:
                    if future.done():
                        if self._event_queue.empty():
                            break
                        event = self._event_queue.get_nowait()
                    else:
                        get_task = asyncio.ensure_future(self._event_queue.get())
                        which = await await_activity(
                            clock, {"event": get_task, "prompt": prompt_task}
                        )
                        if which != "event":
                            get_task.cancel()
                            if which == KEEPALIVE:
                                continue  # re-arm and keep waiting
                            continue  # prompt completed; drain at top of loop
                        event = get_task.result()
                    clock.note_activity()  # any event resets the idle clock
                    update = event.get("params", {}).get("update", {})
                    kind = update.get("sessionUpdate")

                    if kind == "agent_thought_chunk":
                        gate.fire("thinking")
                        gate.emit(workbench_event("thinking"))
                    elif kind == "agent_message_chunk":
                        gate.fire("text")
                        content = update.get("content", {})
                        if content.get("type") == "text":
                            raw_text = str(content.get("text", ""))
                            if raw_text:
                                gate.emit(workbench_event("text", text=raw_text))
                            for sentence in streamer.feed(raw_text):
                                clock.note_audible()
                                yield sentence
                    elif kind in ("tool_call", "tool_call_update"):
                        if self._publish_delegate(update, on_event):
                            continue
                        tool_kind = str(update.get("kind", "")).strip()
                        title = str(update.get("title", "")).strip()
                        mapped, detail = narrate_tool(tool_kind, title, TOOL_VOCAB)
                        if kind == "tool_call":
                            gate.fire("tool")
                            # Detail BEFORE the kind event (narration picks it
                            # up in the same tick).
                            if detail:
                                gate.emit(f"tooldetail:{detail}")
                                clock.note_audible()
                            if tool_kind:
                                gate.emit(f"tool:{mapped}")
                        gate.emit(
                            workbench_event(
                                "tool_start" if kind == "tool_call" else "tool_update",
                                name=tool_kind,
                                tool_kind=mapped,
                                detail=detail,
                                title=title,
                                tool_call_id=str(update.get("toolCallId", "")),
                                content=update.get("content", ""),
                                status=update.get("status", ""),
                            )
                        )

                result = future.result()  # raises HermesACPError on agent error
                _ = result  # stopReason lives here; nothing else needed
            except HermesACPError:
                # The ACP server can wedge after an internal error: its prompt
                # queue jams and every later turn gets "Queued for the next
                # turn." Restart the session so the NEXT turn starts clean.
                await self.abort()
                future.cancel()
                await self._restart()
                raise
            except (asyncio.CancelledError, GeneratorExit):
                await self.abort()
                try:
                    await asyncio.wait_for(asyncio.shield(future), timeout=2.0)
                    while not self._event_queue.empty():
                        self._event_queue.get_nowait()
                except (TimeoutError, HermesACPError):
                    future.cancel()
                    await self._restart()
                raise
            finally:
                if get_task is not None and not get_task.done():
                    get_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await get_task
                if self._active_prompt is future:
                    self._active_prompt = None
                self._pending.pop(req_id, None)
                await self._cancel_permissions()

            tail = streamer.flush()
            if tail:
                yield tail
        self._start_idle_drain()

    async def abort(self) -> None:
        if self._bridge_config is not None and self._proc is not None and self._proc.returncode is None and self._session_id:
            # Cancel the batch before resolving its last pending approval;
            # otherwise an earlier approved operation can begin in between.
            await self._send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": self._session_id}})
            await self._cancel_permissions()
            return
        await self._cancel_permissions()
        prompt = self._active_prompt
        async with self._command_lock:
            if (
                prompt is None
                or prompt is not self._active_prompt
                or prompt.done()
                or self._cancelled_prompt is prompt
            ):
                return
            if self._proc is not None and self._proc.returncode is None and self._session_id:
                self._cancelled_prompt = prompt
                await self._write_command(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/cancel",
                        "params": {"sessionId": self._session_id},
                    }
                )

    async def warmup(self) -> None:
        if self._lock.locked():
            raise HermesACPError("Hermes is busy; warmup skipped.")
        await self.respond("Say OK.", timeout=30.0)

    async def close(self) -> None:
        self._closed = True
        self._permissions.clear()
        await self._stop_idle_drain()
        self._cancel_watches()
        self._known_delegates.clear()
        self._claimed_manifests.clear()
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        if self._proc:
            if self._proc.returncode is None:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
            self._proc = None
        self._session_id = ""
        if self._bridge_launch is not None:
            self._bridge_launch.close()
            self._bridge_launch = None

    def _publish_delegate(
        self,
        update: dict[str, Any],
        on_event: Callable[..., Any] | None,
    ) -> bool:
        if not is_acp_delegate(update, self._known_delegates):
            return False
        worker = worker_from_acp(update, known_ids=self._known_delegates)
        if worker is None:
            return True
        self._known_delegates.add(worker.worker_id)
        publish_event(on_event, worker)
        if worker.phase in {
            WorkerPhase.COMPLETED,
            WorkerPhase.FAILED,
            WorkerPhase.CANCELLED,
        }:
            self._known_delegates.discard(worker.worker_id)
            self._cancel_watch(worker.worker_id)
        else:
            self._ensure_watch(worker.worker_id)
        return True

    def _start_idle_drain(self) -> None:
        if self._closed or self._idle_task is not None:
            return
        self._idle_task = asyncio.create_task(
            self._idle_drain(), name="hermes-acp-idle-drain"
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
                update = event.get("params", {}).get("update", {})
                if not isinstance(update, dict):
                    continue
                kind = update.get("sessionUpdate")
                if kind in ("tool_call", "tool_call_update"):
                    self._publish_delegate(update, self._on_event)
                    continue
                if kind == "agent_message_chunk":
                    content = update.get("content", {})
                    raw_text = ""
                    if isinstance(content, dict) and content.get("type") == "text":
                        raw_text = str(content.get("text", ""))
                    if raw_text:
                        publish_event(self._on_event, AssistantDelta(text=raw_text))
        except asyncio.CancelledError:
            raise

    def _ensure_watch(self, worker_id: str) -> None:
        existing = self._watch_tasks.get(worker_id)
        if existing is not None and not existing.done():
            return
        self._watch_tasks[worker_id] = asyncio.create_task(
            self._watch_live_manifest(worker_id),
            name=f"hermes-acp-watch-{worker_id}",
        )

    def _cancel_watch(self, worker_id: str) -> None:
        task = self._watch_tasks.pop(worker_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _cancel_watches(self) -> None:
        for worker_id in list(self._watch_tasks):
            self._cancel_watch(worker_id)

    async def _watch_live_manifest(self, worker_id: str) -> None:
        started = asyncio.get_running_loop().time()
        wall_start = time.time()
        root = self._hermes_home / "cache" / "delegation" / "live"
        try:
            while worker_id in self._known_delegates and not self._closed:
                event = self._scan_live_manifests(root, worker_id, wall_start)
                if event is not None:
                    publish_event(self._on_event, event)
                    if event.phase in {
                        WorkerPhase.COMPLETED,
                        WorkerPhase.FAILED,
                        WorkerPhase.CANCELLED,
                    }:
                        self._known_delegates.discard(worker_id)
                    return
                await asyncio.sleep(0.25)
                if asyncio.get_running_loop().time() - started > 3600:
                    return
        except asyncio.CancelledError:
            raise

    def _scan_live_manifests(
        self, root: Path, worker_id: str, wall_start: float
    ) -> WorkerEvent | None:
        if not root.is_dir():
            return None
        try:
            children = list(root.iterdir())
        except OSError:
            return None
        for child in children:
            manifest = child / "manifest.json"
            key = str(manifest)
            if key in self._claimed_manifests:
                continue
            try:
                if not manifest.is_file() or manifest.stat().st_mtime < wall_start - 2:
                    continue
            except OSError:
                continue
            event = worker_from_live_manifest(manifest, worker_id)
            if event is not None:
                self._claimed_manifests.add(key)
                return event
        return None


def tool_detail_from_title(mapped_kind: str, title: str) -> str:
    """Gerund phrase from an ACP tool_call title, for progress narration.

    Backward-compat shim over the shared vocab (takes an already-mapped
    narration kind; new code uses narrate_tool with TOOL_VOCAB).
    """
    return narrate_tool(mapped_kind, title, _MAPPED_KIND_VOCAB)[1]


# tool_detail_from_title's callers pass an already-mapped narration kind, so
# its vocab maps narration kinds to themselves.
_MAPPED_KIND_VOCAB = ToolVocab(
    kinds={k: k for k in ("bash", "read", "write", "edit", "search", "other")},
    fallback_to_subject=True,
)
