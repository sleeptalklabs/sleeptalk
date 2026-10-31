"""Agent Bridge Lite: bring your own agent with a command or a web endpoint.

Bridge Lite is a profile of Agent Bridge v1 (``docs/agent-bridge-protocol.md``).
It uses the same frame names and fields. It drops the handshake requirement,
credit, replay, resume and heartbeat, because a local pipe or one streamed
HTTP request already provides ordering, backpressure and liveness.

The minimum an agent implements::

    host  -> agent  {"type":"turn.prompt","turnId":1,"text":"hello"}
    agent -> host   {"type":"assistant.delta","turnId":1,"text":"Hi there."}
    agent -> host   {"type":"turn.completed","turnId":1}
    host  -> agent  {"type":"turn.abort","turnId":1}
    agent -> host   {"type":"turn.aborted","turnId":1}

Everything else in Bridge v1 is optional and becomes available once the agent
advertises it in ``hello.ok``. Sentence splitting, preambles, narration and
keepalive stay in the host.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import ipaddress
import json
import logging
import os
import shutil
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator

from sleeptalk.adapter import (
    CONTRACT_VERSION,
    AgentCapabilityManifest,
    AgentControl,
    AgentError,
    AnswerClarification,
    AssistantDelta,
    Cancel,
    CapabilitySupport,
    ControlAccepted,
    ControlRejected,
    ControlResult,
    ResolvePermission,
    Steer,
    SupportLevel,
    VoiceSessionCapability,
    WorkerCapability,
    publish_event,
)
from sleeptalk.agent_bridge import canonicalize, frame_to_event
from sleeptalk.agent_environment import agent_environment
from sleeptalk.registries import ComponentOptions
from sleeptalk.streaming import SentenceStreamer
from sleeptalk.text import extract_speakable_text

logger = logging.getLogger(__name__)

LITE_PROFILE = "lite"
MAX_LINE_BYTES = 1 << 20
FRAME_QUEUE_SIZE = 256
STDERR_TAIL_LINES = 20
CONTROL_REPLY_TIMEOUT_S = 2.0

#: Always passed to a command agent even with ``inherit_env = false``.
BASE_ENV_NAMES = frozenset(
    {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM",
        "TMPDIR", "TMP", "TEMP", "TZ", "PWD",
        "SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT", "WINDIR", "APPDATA",
        "LOCALAPPDATA", "USERPROFILE",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
        "VIRTUAL_ENV", "NVM_DIR", "PYENV_ROOT", "UV_CACHE_DIR",
    }
)

MANIFEST_FIELDS = (
    "structured_events",
    "tool_lifecycle",
    "permissions",
    "clarification",
    "session_identity",
    "status",
    "usage",
    "steering",
    "targeted_cancellation",
    "workers",
)

#: Capabilities that need a host -> agent message during a turn.
MID_TURN_CONTROL = ("permissions", "clarification", "steering", "targeted_cancellation")

_TERMINAL_TYPES = frozenset({"turn.completed", "turn.aborted", "error"})
_EOF = object()


class BridgeLiteError(RuntimeError):
    """A Bridge Lite agent failed a turn. The message is safe to show."""


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


class CommandAgentOptions(ComponentOptions):
    """``[agent.options]`` for ``type = "command"``."""

    command: list[str] = Field(min_length=1)
    protocol: Literal["lite", "acp"] = "lite"
    env: list[str] = Field(default_factory=list)
    inherit_env: bool = False
    hello_timeout_s: float = Field(default=1.0, ge=0, le=30)
    # Until the program sends its first frame of any kind, a turn fails after
    # this long instead of idle_timeout_s: it is probably not speaking Bridge Lite.
    first_frame_timeout_s: float = Field(default=30.0, gt=0, le=3600)
    idle_timeout_s: float = Field(default=120.0, gt=0, le=3600)
    abort_grace_s: float = Field(default=2.0, gt=0, le=30)

    @field_validator("command")
    @classmethod
    def _non_empty_program(cls, value: list[str]) -> list[str]:
        if not value[0].strip():
            raise ValueError("command[0] must name a program")
        return value


# Agents with a verified ACP entry point. Built-in adapters are preferred
# where Sleeptalk has one.
KNOWN_ACP_COMMANDS: dict[str, tuple[tuple[str, ...], str]] = {
    "opencode": (("opencode", "acp"), ""),
    "agent": (("agent", "acp"), 'Cursor also has a built-in adapter: type = "cursor_acp".'),
    "hermes": (("hermes", "acp"), 'Hermes also has built-in adapters: type = "hermes_gateway" or "acp".'),
    "openclaw": (("openclaw", "acp"), 'OpenClaw also has a built-in adapter: type = "openclaw".'),
}


def command_setup_hint(command: list[str] | tuple[str, ...], protocol: str) -> str:
    """A fix for a command that looks wrong for its protocol, or ""."""

    if not command:
        return ""
    program = os.path.basename(str(command[0]))
    known = KNOWN_ACP_COMMANDS.get(program)
    if known is None:
        return ""
    acp_command, extra = known
    suggested = "[" + ", ".join(f'"{part}"' for part in acp_command) + "]"
    if protocol == "lite":
        hint = (
            f"`{program}` is an ACP agent, and without protocol = \"acp\" Sleeptalk expects "
            f"Bridge Lite frames it will never send. Set protocol = \"acp\" and "
            f"command = {suggested}."
        )
    elif tuple(command[1:]) != acp_command[1:] and acp_command[1] not in command[1:]:
        hint = f"`{program}` starts its interactive app without `{acp_command[1]}`. Use command = {suggested}."
    else:
        return ""
    return f"{hint} {extra}".strip()


class HttpAgentOptions(ComponentOptions):
    """``[agent.options]`` for ``type = "http"``."""

    url: str
    token_env: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    connect_timeout_s: float = Field(default=10.0, gt=0, le=120)
    idle_timeout_s: float = Field(default=120.0, gt=0, le=3600)

    @field_validator("url")
    @classmethod
    def _valid_url(cls, value: str) -> str:
        check_http_url(value)
        return value

    @field_validator("headers")
    @classmethod
    def _no_secret_headers(cls, value: dict[str, str]) -> dict[str, str]:
        for name in value:
            if name.strip().lower() in {"authorization", "cookie", "x-api-key", "api-key"}:
                raise ValueError(
                    f"header {name!r} carries a secret; use token_env instead of the profile"
                )
        return value


def is_loopback_host(host: str) -> bool:
    if host in {"localhost"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("url must start with https:// (or http:// on loopback)")
    if not parsed.hostname:
        raise ValueError("url must include a host")
    if parsed.username or parsed.password:
        raise ValueError("url must not contain credentials; use token_env")
    if parsed.scheme == "http" and not is_loopback_host(parsed.hostname):
        raise ValueError("plain http:// is only allowed on loopback; use https://")


def command_environment(
    options: CommandAgentOptions,
    env: Mapping[str, str],
) -> dict[str, str]:
    """The environment a command agent receives.

    Voice credentials never pass. Without ``inherit_env`` only a small base
    set plus the names the profile lists pass through.
    """

    source = agent_environment(env)
    if options.inherit_env:
        return source
    allowed = BASE_ENV_NAMES | set(options.env)
    return {
        name: value
        for name, value in source.items()
        if name in allowed or name.startswith("LC_")
    }


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


def _level(raw: Any) -> tuple[SupportLevel, str | None]:
    if isinstance(raw, str):
        raw = {"level": raw}
    if isinstance(raw, bool):
        raw = {"level": "supported" if raw else "unsupported"}
    if not isinstance(raw, dict):
        return SupportLevel.UNSUPPORTED, None
    try:
        level = SupportLevel(str(raw.get("level") or "unsupported"))
    except ValueError:
        return SupportLevel.UNSUPPORTED, None
    reason = raw.get("reason")
    if level is SupportLevel.UNAVAILABLE:
        return level, str(reason or "unavailable")
    return level, None


def _snake(name: str) -> str:
    out = []
    for char in name:
        if char.isupper():
            out.append("_")
            out.append(char.lower())
        else:
            out.append(char)
    return "".join(out)


def manifest_from_caps(
    caps: Mapping[str, Any] | None,
    *,
    unavailable: Mapping[str, str] | None = None,
    voice_sessions_reason: str = "voice sessions are not part of Bridge Lite",
) -> AgentCapabilityManifest:
    """Build a manifest from ``hello.ok.capabilities`` (either key style).

    ``unavailable`` forces named fields to UNAVAILABLE with a reason when the
    agent advertises them as supported but this transport cannot carry them.
    """

    normalized = {_snake(str(key)): value for key, value in (caps or {}).items()}
    unavailable = dict(unavailable or {})
    fields: dict[str, CapabilitySupport] = {}
    for name in MANIFEST_FIELDS:
        level, reason = _level(normalized.get(name))
        if name in unavailable and level is SupportLevel.SUPPORTED:
            level, reason = SupportLevel.UNAVAILABLE, unavailable[name]
        support_cls = WorkerCapability if name == "workers" else CapabilitySupport
        fields[name] = support_cls(level, reason)
    return AgentCapabilityManifest(
        contract_version=CONTRACT_VERSION,
        voice_sessions=VoiceSessionCapability(SupportLevel.UNAVAILABLE, voice_sessions_reason),
        **fields,
    )


def floor_manifest() -> AgentCapabilityManifest:
    return manifest_from_caps({})


def _host_offer() -> dict[str, Any]:
    return {name: {"level": "supported"} for name in MANIFEST_FIELDS}


# ---------------------------------------------------------------------------
# Shared turn handling
# ---------------------------------------------------------------------------


@dataclass
class _Turn:
    turn_id: int
    streamer: SentenceStreamer


class _LiteTurns:
    """Frame-to-sentence translation for one agent, independent of transport."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.agent_capabilities = floor_manifest()
        self._warned: set[str] = set()

    def _warn_once(self, key: str, message: str, *args: Any) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning("agent %s: " + message, self.name, *args)

    def _supported(self, field: str) -> bool:
        support = getattr(self.agent_capabilities, field)
        return support.level is SupportLevel.SUPPORTED

    def apply_hello(self, canon: Mapping[str, Any], *, unavailable: Mapping[str, str] | None = None) -> None:
        caps = canon.get("capabilities")
        self.agent_capabilities = manifest_from_caps(
            caps if isinstance(caps, dict) else {},
            unavailable=unavailable,
        )

    def handle(
        self,
        turn: _Turn,
        canon: dict[str, Any],
        on_event: Callable[..., Any] | None,
    ) -> tuple[list[str], bool]:
        """Return (sentences to speak, turn finished).

        Raises ``BridgeLiteError`` for an ``error`` frame other than ``abort``.
        """

        frame_turn = canon.get("turnId")
        if frame_turn is not None and frame_turn != turn.turn_id:
            return [], False
        kind = canon.get("type")
        if kind == "assistant.delta":
            text = str(canon.get("text") or "")
            if text:
                publish_event(on_event, AssistantDelta(text=text))
            return turn.streamer.feed(text), False
        if kind == "sentence":
            out = []
            tail = turn.streamer.flush()
            if tail:
                out.append(tail)
            spoken = extract_speakable_text(str(canon.get("text") or "")).strip()
            if spoken:
                out.append(spoken)
            return out, False
        if kind == "turn.completed":
            tail = turn.streamer.flush()
            if not tail and canon.get("text"):
                tail = extract_speakable_text(str(canon["text"])).strip()
            return ([tail] if tail else []), True
        if kind == "turn.aborted":
            return [], True
        if kind == "error":
            code = str(canon.get("code") or "agent")
            message = str(canon.get("message") or "agent reported an error")
            if code == "abort":
                return [], True
            publish_event(
                on_event,
                AgentError(
                    code=code,
                    message=message,
                    retryable=bool(canon.get("retryable")),
                    fatal=bool(canon.get("fatal")),
                ),
            )
            raise BridgeLiteError(f"{self.name}: {message}")
        if kind in {"permission.requested", "clarification.requested"}:
            field = "permissions" if kind == "permission.requested" else "clarification"
            if not self._supported(field):
                self._warn_once(
                    kind,
                    "ignored %s because hello.ok did not advertise %s as supported",
                    kind,
                    field,
                )
                return [], False
        if kind in {"hello.ok", "control.accepted", "control.rejected", "heartbeat", "credit"}:
            return [], False
        try:
            event = frame_to_event(canon)
        except (ValueError, KeyError, TypeError):
            self._warn_once(f"bad:{kind}", "ignored malformed %s frame", kind)
            return [], False
        if event is None:
            self._warn_once(f"unknown:{kind}", "ignored unknown frame type %r", kind)
            return [], False
        publish_event(on_event, event)
        return [], False


def _control_frame(command: AgentControl, turn_id: int, frame_id: str) -> tuple[str, dict[str, Any]] | None:
    base = {"v": CONTRACT_VERSION, "id": frame_id, "turnId": turn_id}
    if isinstance(command, ResolvePermission):
        return "permissions", {
            **base,
            "type": "permission.resolve",
            "requestId": command.request_id,
            "decision": command.decision.value,
        }
    if isinstance(command, AnswerClarification):
        return "clarification", {
            **base,
            "type": "clarification.answer",
            "requestId": command.request_id,
            "answer": command.answer,
        }
    if isinstance(command, Steer):
        return "steering", {
            **base,
            "type": "steer",
            "instruction": command.instruction,
            "targetId": command.target_id,
        }
    if isinstance(command, Cancel):
        return "targeted_cancellation", {
            **base,
            "type": "cancel",
            "targetId": command.target_id,
        }
    return None


def _request_id(command: AgentControl) -> str | None:
    return getattr(command, "request_id", None)


# ---------------------------------------------------------------------------
# type = "command"
# ---------------------------------------------------------------------------


class CommandAgent:
    """Runs a local command that speaks Bridge Lite frames over stdio."""

    def __init__(
        self,
        options: CommandAgentOptions,
        *,
        env: Mapping[str, str],
        cwd: str | None = None,
        name: str = "command",
    ) -> None:
        self.options = options
        self.name = name
        self.cwd = cwd or os.getcwd()
        self._env = command_environment(options, env)
        self._turns = _LiteTurns(name)
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._frames: asyncio.Queue[Any] = asyncio.Queue(maxsize=FRAME_QUEUE_SIZE)
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=STDERR_TAIL_LINES)
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._abort = asyncio.Event()
        self._turn_seq = 0
        self._active_turn: int | None = None
        self._settle_task: asyncio.Task[None] | None = None
        self._control_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._control_seq = 0
        self._hello_seen = asyncio.Event()
        self._any_frame = False
        self._closed = False
        self.keepalive_interval_s = 0.0
        #: Configuration problems found before anything runs; reported by adapter test.
        self.setup_warnings: tuple[str, ...] = tuple(
            hint for hint in (command_setup_hint(options.command, "lite"),) if hint
        )

    # ---- AgentAdapter extras ----

    @property
    def agent_capabilities(self) -> AgentCapabilityManifest:
        return self._turns.agent_capabilities

    # ---- process ----

    def resolve_program(self) -> str | None:
        program = os.path.expanduser(self.options.command[0])
        if os.sep in program or (os.altsep and os.altsep in program):
            path = program if os.path.isabs(program) else os.path.join(self.cwd, program)
            return path if os.path.exists(path) else None
        return shutil.which(program, path=self._env.get("PATH"))

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if self._closed:
                raise BridgeLiteError(f"{self.name} is closed")
            if self._proc is not None and self._proc.returncode is None:
                return
            await self._stop_process()
            program = self.resolve_program()
            if program is None:
                raise BridgeLiteError(
                    f"{self.name}: command not found: {self.options.command[0]!r} "
                    "(check [agent.options].command, PATH, and workspace_dir)"
                )
            for warning in self.setup_warnings:
                logger.warning("agent %s: %s", self.name, warning)
            self._frames = asyncio.Queue(maxsize=FRAME_QUEUE_SIZE)
            self._hello_seen = asyncio.Event()
            self._any_frame = False
            self._stderr_tail.clear()
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    program,
                    *self.options.command[1:],
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.cwd,
                    env=self._env,
                    limit=MAX_LINE_BYTES,
                )
            except OSError as exc:
                raise BridgeLiteError(f"{self.name}: could not start command: {exc}") from exc
            self._tasks = [
                asyncio.create_task(self._read_stdout(self._proc), name=f"{self.name}-stdout"),
                asyncio.create_task(self._read_stderr(self._proc), name=f"{self.name}-stderr"),
            ]
            await self._send(
                {
                    "v": CONTRACT_VERSION,
                    "type": "hello",
                    "profile": LITE_PROFILE,
                    "client": {"name": "sleeptalk"},
                    "capabilities": _host_offer(),
                },
                required=False,
            )
            if self.options.hello_timeout_s > 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._hello_seen.wait(), self.options.hello_timeout_s)

    async def _read_stdout(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        warned_text = False
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except ValueError:
                    logger.warning("agent %s: dropped a stdout line over %d bytes", self.name, MAX_LINE_BYTES)
                    continue
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    frame = json.loads(text)
                except json.JSONDecodeError:
                    frame = None
                if not isinstance(frame, dict) or "type" not in frame:
                    if not warned_text:
                        warned_text = True
                        logger.warning(
                            "agent %s: stdout line is not a JSON frame (print logs to stderr): %.120s",
                            self.name,
                            text,
                        )
                    continue
                canon = canonicalize(frame)
                kind = canon.get("type")
                self._any_frame = True
                if kind == "hello.ok":
                    self._turns.apply_hello(canon)
                    self._hello_seen.set()
                    continue
                if kind in {"control.accepted", "control.rejected"}:
                    waiter = self._control_waiters.pop(str(canon.get("ref") or ""), None)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(canon)
                    continue
                await self._frames.put(canon)
        except asyncio.CancelledError:
            return
        await self._frames.put(_EOF)

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        try:
            while True:
                try:
                    line = await proc.stderr.readline()
                except ValueError:
                    continue
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    logger.info("agent %s stderr: %s", self.name, text)
        except asyncio.CancelledError:
            return

    async def _send(self, frame: dict[str, Any], *, required: bool = True) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            if required:
                raise BridgeLiteError(self._exit_message())
            return
        data = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        async with self._write_lock:
            try:
                proc.stdin.write(data)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                if required:
                    raise BridgeLiteError(self._exit_message()) from exc

    async def _exited_message(self, settle_s: float = 1.0) -> str:
        """The stop message once the process is reaped and its stderr drained.

        Stdout closing races the exit: the return code and the last stderr
        line may not be in yet, and the message should name both.
        """
        proc = self._proc
        if proc is not None:
            stderr_readers = [task for task in self._tasks if task.get_name().endswith("-stderr")]
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(
                    asyncio.gather(proc.wait(), *stderr_readers, return_exceptions=True),
                    settle_s,
                )
        return self._exit_message()

    def _exit_message(self) -> str:
        code = self._proc.returncode if self._proc is not None else None
        detail = f" (exit code {code})" if code is not None else ""
        last = self._stderr_tail[-1] if self._stderr_tail else ""
        hint = f": {last[:200]}" if last else ""
        env_hint = (
            "" if self.options.inherit_env else
            " If it needs an API key, list it in [agent.options].env."
        )
        return f"{self.name}: agent command stopped{detail}{hint}.{env_hint}"

    async def _stop_process(self) -> None:
        proc = self._proc
        self._proc = None
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        for waiter in self._control_waiters.values():
            if not waiter.done():
                waiter.cancel()
        self._control_waiters.clear()
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(Exception):
            if proc.stdin is not None:
                proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
            return
        except TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    # ---- turns ----

    async def _next_frame(self, timeout: float) -> Any:
        """Next frame, ``_EOF``, or ``None`` when aborted. Raises TimeoutError."""

        if self._abort.is_set():
            return None
        getter = asyncio.ensure_future(self._frames.get())
        aborted = asyncio.ensure_future(self._abort.wait())
        try:
            done, _ = await asyncio.wait(
                {getter, aborted},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            aborted.cancel()
            if not getter.done():
                getter.cancel()
        if getter in done:
            return getter.result()
        if aborted in done:
            return None
        raise TimeoutError

    async def _wait_settled(self) -> None:
        task = self._settle_task
        if task is not None:
            with contextlib.suppress(Exception):
                await task
            self._settle_task = None

    async def _settle(self, turn_id: int) -> None:
        """Consume the aborted turn's remaining frames, or restart the agent."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.options.abort_grace_s
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning(
                    "agent %s did not acknowledge turn.abort within %.1fs; restarting it",
                    self.name,
                    self.options.abort_grace_s,
                )
                await self._stop_process()
                return
            try:
                frame = await asyncio.wait_for(self._frames.get(), remaining)
            except TimeoutError:
                continue
            if frame is _EOF:
                return
            frame_turn = frame.get("turnId")
            if frame.get("type") in _TERMINAL_TYPES and frame_turn in (None, turn_id):
                return

    def respond_streaming(
        self,
        text: str,
        *,
        on_event: Callable[..., Any] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[str]:
        async def _stream() -> AsyncIterator[str]:
            async with self._lock:
                await self._wait_settled()
                await self._ensure_started()
                self._turn_seq += 1
                turn = _Turn(self._turn_seq, SentenceStreamer())
                self._abort.clear()
                self._active_turn = turn.turn_id
                idle = timeout if timeout is not None else self.options.idle_timeout_s
                try:
                    await self._send(
                        {
                            "v": CONTRACT_VERSION,
                            "type": "turn.prompt",
                            "id": f"p-{turn.turn_id}",
                            "turnId": turn.turn_id,
                            "text": text,
                            "mode": "stream",
                        }
                    )
                    while True:
                        silent = not self._any_frame
                        wait = min(idle, self.options.first_frame_timeout_s) if silent else idle
                        try:
                            frame = await self._next_frame(wait)
                        except TimeoutError:
                            await self._abort_turn(turn.turn_id)
                            if silent and not self._any_frame:
                                hint = " ".join(self.setup_warnings) or (
                                    "If it is an interactive app or an ACP agent, set protocol = \"acp\" "
                                    "and its ACP command; otherwise print JSON frames to stdout "
                                    "(docs/bring-your-own-agent.md)."
                                )
                                raise BridgeLiteError(
                                    f"{self.name}: the program has not sent a single Bridge Lite frame "
                                    f"in {wait:.0f}s. {hint}"
                                ) from None
                            raise BridgeLiteError(
                                f"{self.name}: agent sent nothing for {wait:.0f}s"
                            ) from None
                        if frame is None:
                            return
                        if frame is _EOF:
                            raise BridgeLiteError(await self._exited_message())
                        sentences, finished = self._turns.handle(turn, frame, on_event)
                        for sentence in sentences:
                            if self._abort.is_set():
                                return
                            yield sentence
                        if finished:
                            return
                finally:
                    self._active_turn = None

        return _stream()

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        parts: list[str] = []
        async with asyncio.timeout(timeout):
            async for sentence in self.respond_streaming(text):
                parts.append(sentence)
        return " ".join(parts)

    async def _abort_turn(self, turn_id: int) -> None:
        self._abort.set()
        await self._send(
            {
                "v": CONTRACT_VERSION,
                "type": "turn.abort",
                "id": f"a-{turn_id}",
                "turnId": turn_id,
            },
            required=False,
        )
        if self._settle_task is None or self._settle_task.done():
            self._settle_task = asyncio.create_task(
                self._settle(turn_id), name=f"{self.name}-settle"
            )

    async def abort(self) -> None:
        turn_id = self._active_turn
        if turn_id is None or self._abort.is_set():
            return
        await self._abort_turn(turn_id)

    async def warmup(self) -> None:
        await self._ensure_started()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._abort.set()
        await self._send({"v": CONTRACT_VERSION, "type": "session.close", "id": "c-1"}, required=False)
        if self._settle_task is not None:
            self._settle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._settle_task
        async with self._start_lock:
            await self._stop_process()

    async def control(self, command: AgentControl) -> ControlResult:
        self._control_seq += 1
        frame_id = f"k-{self._control_seq}"
        built = _control_frame(command, self._active_turn or self._turn_seq, frame_id)
        if built is None:
            return ControlRejected(reason=f"{type(command).__name__} is not part of Bridge Lite")
        field, frame = built
        if not self._turns._supported(field):
            return ControlRejected(reason=f"agent did not advertise {field}")
        waiter: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._control_waiters[frame_id] = waiter
        try:
            await self._send(frame)
        except BridgeLiteError as exc:
            self._control_waiters.pop(frame_id, None)
            return ControlRejected(reason=str(exc), retryable=True)
        try:
            reply = await asyncio.wait_for(waiter, CONTROL_REPLY_TIMEOUT_S)
        except TimeoutError:
            # Delivered; Lite agents may skip explicit acknowledgements.
            self._control_waiters.pop(frame_id, None)
            return ControlAccepted(request_id=_request_id(command))
        except asyncio.CancelledError:
            if waiter.cancelled():
                return ControlRejected(reason=f"{self.name}: agent stopped", retryable=True)
            raise
        if reply.get("type") == "control.rejected":
            return ControlRejected(
                reason=str(reply.get("reason") or "rejected by agent"),
                retryable=bool(reply.get("retryable")),
            )
        return ControlAccepted(
            request_id=reply.get("requestId") or _request_id(command),
            resource=reply.get("resource"),
        )


# ---------------------------------------------------------------------------
# type = "http"
# ---------------------------------------------------------------------------

HTTP_UNAVAILABLE = {
    name: "needs a message from Sleeptalk during a turn; use type = \"command\" or \"bridge\""
    for name in MID_TURN_CONTROL
}


async def _iter_lines(content: Any) -> AsyncIterator[str]:
    buffer = b""
    async for chunk in content.iter_any():
        buffer += chunk
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            line, buffer = buffer[:newline], buffer[newline + 1 :]
            yield line.decode("utf-8", errors="replace").rstrip("\r")
    if buffer:
        yield buffer.decode("utf-8", errors="replace").rstrip("\r")


def _text_frame(text: str) -> dict[str, Any]:
    return {"type": "assistant.delta", "text": text}


def _json_body_frames(body: Any) -> list[dict[str, Any]]:
    """Accept a non-streaming JSON reply: a frame, a list of frames, or {text}."""

    if isinstance(body, list):
        return [canonicalize(item) for item in body if isinstance(item, dict) and "type" in item]
    if isinstance(body, dict):
        if "type" in body:
            return [canonicalize(body)]
        for key in ("text", "reply", "output", "message", "content"):
            value = body.get(key)
            if isinstance(value, str):
                return [_text_frame(value), {"type": "turn.completed"}]
    if isinstance(body, str):
        return [_text_frame(body), {"type": "turn.completed"}]
    return []


async def iter_http_frames(response: Any) -> AsyncIterator[dict[str, Any]]:
    """Frames from a streamed HTTP reply: NDJSON, SSE, JSON, or plain text."""

    content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if content_type in {"application/x-ndjson", "application/jsonl", "application/json-seq", "application/stream+json"}:
        async for line in _iter_lines(response.content):
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                yield _text_frame(line)
                continue
            if isinstance(frame, dict) and "type" in frame:
                yield canonicalize(frame)
        return
    if content_type == "text/event-stream":
        data: list[str] = []
        async for line in _iter_lines(response.content):
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
                continue
            if line == "" and data:
                payload, data = "\n".join(data), []
                if payload.strip() == "[DONE]":
                    yield {"type": "turn.completed"}
                    return
                try:
                    frame = json.loads(payload)
                except json.JSONDecodeError:
                    yield _text_frame(payload)
                    continue
                if isinstance(frame, dict) and "type" in frame:
                    yield canonicalize(frame)
                elif isinstance(frame, str):
                    yield _text_frame(frame)
        if data:
            payload = "\n".join(data)
            with contextlib.suppress(json.JSONDecodeError):
                frame = json.loads(payload)
                if isinstance(frame, dict) and "type" in frame:
                    yield canonicalize(frame)
        return
    if content_type == "application/json":
        raw = await response.read()
        try:
            body = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return
        for frame in _json_body_frames(body):
            yield frame
        return
    async for chunk in response.content.iter_any():
        text = chunk.decode("utf-8", errors="replace")
        if text:
            yield _text_frame(text)


class HttpAgent:
    """Sends each turn as an HTTP POST and speaks the streamed reply."""

    def __init__(
        self,
        options: HttpAgentOptions,
        *,
        env: Mapping[str, str],
        name: str = "http",
        session_id: str = "",
    ) -> None:
        import secrets

        self.options = options
        self.name = name
        self.session_id = session_id or secrets.token_hex(8)
        self._token = env.get(options.token_env, "").strip() if options.token_env else ""
        self._turns = _LiteTurns(name)
        self._turns.agent_capabilities = manifest_from_caps({}, unavailable=HTTP_UNAVAILABLE)
        self._http: Any = None
        self._lock = asyncio.Lock()
        self._abort = asyncio.Event()
        self._turn_seq = 0
        self._active_turn: int | None = None
        self._closed = False
        self.keepalive_interval_s = 0.0

    @property
    def agent_capabilities(self) -> AgentCapabilityManifest:
        return self._turns.agent_capabilities

    def request_headers(self) -> dict[str, str]:
        headers = {
            **self.options.headers,
            "Content-Type": "application/json",
            "Accept": "application/x-ndjson, text/event-stream;q=0.9, application/json;q=0.8, text/plain;q=0.5",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _session(self) -> Any:
        if self._closed:
            raise BridgeLiteError(f"{self.name} is closed")
        if self._http is None:
            import aiohttp

            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=None,
                    sock_connect=self.options.connect_timeout_s,
                    sock_read=self.options.idle_timeout_s,
                )
            )
        return self._http

    def respond_streaming(
        self,
        text: str,
        *,
        on_event: Callable[..., Any] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[str]:
        async def _stream() -> AsyncIterator[str]:
            import aiohttp

            async with self._lock:
                session = await self._session()
                self._turn_seq += 1
                turn = _Turn(self._turn_seq, SentenceStreamer())
                self._abort.clear()
                self._active_turn = turn.turn_id
                idle = timeout if timeout is not None else self.options.idle_timeout_s
                frames: asyncio.Queue[Any] = asyncio.Queue(maxsize=FRAME_QUEUE_SIZE)

                async def _pump() -> None:
                    try:
                        async with session.post(
                            self.options.url,
                            json={
                                "v": CONTRACT_VERSION,
                                "type": "turn.prompt",
                                "id": f"p-{turn.turn_id}",
                                "turnId": turn.turn_id,
                                "sessionId": self.session_id,
                                "text": text,
                                "mode": "stream",
                            },
                            headers=self.request_headers(),
                        ) as response:
                            if response.status in {401, 403}:
                                raise BridgeLiteError(
                                    f"{self.name}: endpoint rejected the credential (HTTP {response.status}); "
                                    "check token_env"
                                )
                            if response.status >= 400:
                                raise BridgeLiteError(
                                    f"{self.name}: endpoint returned HTTP {response.status}"
                                )
                            async for frame in iter_http_frames(response):
                                await frames.put(frame)
                        await frames.put(_EOF)
                    except asyncio.CancelledError:
                        raise
                    except BridgeLiteError as exc:
                        await frames.put(exc)
                    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                        await frames.put(BridgeLiteError(f"{self.name}: request failed: {type(exc).__name__}"))

                pump = asyncio.create_task(_pump(), name=f"{self.name}-http-turn")
                try:
                    while True:
                        getter = asyncio.ensure_future(frames.get())
                        aborted = asyncio.ensure_future(self._abort.wait())
                        try:
                            done, _ = await asyncio.wait(
                                {getter, aborted},
                                timeout=idle,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                        finally:
                            aborted.cancel()
                            if not getter.done():
                                getter.cancel()
                        if getter not in done:
                            if aborted in done:
                                return
                            raise BridgeLiteError(f"{self.name}: agent sent nothing for {idle:.0f}s")
                        frame = getter.result()
                        if isinstance(frame, BridgeLiteError):
                            raise frame
                        if frame is _EOF:
                            tail = turn.streamer.flush()
                            if tail and not self._abort.is_set():
                                yield tail
                            return
                        if frame.get("type") == "hello.ok":
                            self._turns.apply_hello(frame, unavailable=HTTP_UNAVAILABLE)
                            continue
                        sentences, finished = self._turns.handle(turn, frame, on_event)
                        for sentence in sentences:
                            if self._abort.is_set():
                                return
                            yield sentence
                        if finished:
                            return
                finally:
                    self._active_turn = None
                    if not pump.done():
                        pump.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await pump

        return _stream()

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        parts: list[str] = []
        async with asyncio.timeout(timeout):
            async for sentence in self.respond_streaming(text):
                parts.append(sentence)
        return " ".join(parts)

    async def abort(self) -> None:
        if self._active_turn is not None:
            self._abort.set()

    async def warmup(self) -> None:
        await self.discover()

    async def discover(self) -> bool:
        """Optional capability discovery: ``GET url`` may return ``hello.ok``.

        Never starts work. Endpoints that don't answer GET keep the floor.
        """

        import aiohttp

        session = await self._session()
        headers = {k: v for k, v in self.request_headers().items() if k != "Content-Type"}
        headers["Accept"] = "application/json"
        try:
            async with session.get(
                self.options.url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.options.connect_timeout_s),
            ) as response:
                if response.status != 200:
                    return False
                body = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
            return False
        if isinstance(body, dict) and body.get("type") == "hello.ok":
            self._turns.apply_hello(canonicalize(body), unavailable=HTTP_UNAVAILABLE)
            return True
        return False

    async def close(self) -> None:
        self._closed = True
        self._abort.set()
        if self._http is not None:
            await self._http.close()
            self._http = None

    async def control(self, command: AgentControl) -> ControlResult:
        return ControlRejected(reason="HTTP Lite agents cannot receive messages during a turn")


# ---------------------------------------------------------------------------
# Readiness probes (no prompt is sent)
# ---------------------------------------------------------------------------


def _probe_result(**fields: Any) -> dict[str, Any]:
    return {"status": "needs_input", "protocol": "unknown", "login": "unknown", **fields}


async def probe_command(agent: CommandAgent) -> dict[str, Any]:
    if agent.resolve_program() is None:
        return _probe_result(
            protocol="unavailable",
            next_action=f"Install the agent command or fix [agent.options].command: {agent.options.command[0]!r} was not found.",
        )
    try:
        await agent.warmup()
        await asyncio.sleep(0)
        if agent._proc is None or agent._proc.returncode is not None:
            return _probe_result(protocol="unavailable", next_action=agent._exit_message())
        advertised = agent._hello_seen.is_set()
    except BridgeLiteError as exc:
        return _probe_result(protocol="unavailable", next_action=str(exc))
    finally:
        await agent.close()
    return _probe_result(
        protocol="available",
        next_action=(
            "The agent command starts"
            + (" and advertised its capabilities" if advertised else "; it did not answer hello, so only the talkable floor is assumed")
            + ". Run `sleeptalk adapter test --live` to prove a spoken turn."
        ),
    )


async def probe_http(agent: HttpAgent) -> dict[str, Any]:
    import aiohttp

    try:
        session = await agent._session()
        async with session.head(agent.options.url, headers=agent.request_headers()) as response:
            status = response.status
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        return _probe_result(protocol="unavailable", next_action=f"The endpoint is not reachable ({type(exc).__name__}). Start the service or fix [agent.options].url.")
    finally:
        await agent.close()
    if status in {401, 403}:
        return _probe_result(protocol="available", login="unavailable", next_action="The endpoint rejected the credential. Check the variable named by token_env.")
    if status >= 500:
        return _probe_result(protocol="unavailable", next_action=f"The endpoint returned HTTP {status}.")
    return _probe_result(
        protocol="available",
        next_action="The endpoint is reachable. Run `sleeptalk adapter test --live` to prove a spoken turn.",
    )


__all__ = [
    "BridgeLiteError",
    "CommandAgent",
    "CommandAgentOptions",
    "HttpAgent",
    "HttpAgentOptions",
    "check_http_url",
    "command_environment",
    "floor_manifest",
    "iter_http_frames",
    "manifest_from_caps",
]
