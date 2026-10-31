"""``type = "bridge"``: a remote agent that implements Agent Bridge v1.

The protocol lives in ``sleeptalk.agent_bridge`` and
``docs/agent-bridge-protocol.md``. This module only makes the existing client
selectable from a profile: it reads the credential from the environment,
connects lazily, and stops speaking the moment a turn is aborted.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any
from urllib.parse import urlparse

from pydantic import Field, field_validator

from sleeptalk.adapter import (
    CONTRACT_VERSION,
    AgentCapabilityManifest,
    AgentControl,
    ControlAccepted,
    ControlRejected,
    ControlResult,
    SupportLevel,
)
from sleeptalk.agent_bridge import AgentBridgeClient, AgentBridgeError, assert_transport_allowed
from sleeptalk.agents.bridge_lite import _control_frame, _request_id, manifest_from_caps
from sleeptalk.registries import ComponentOptions


VOICE_SESSIONS_REASON = (
    "agent-initiated voice sessions need the host-owned request flow in "
    "docs/agent-initiated-sessions.md"
)
CONTROL_TIMEOUT_S = 5.0


class BridgeAgentOptions(ComponentOptions):
    """``[agent.options]`` for ``type = "bridge"``."""

    url: str
    token_env: str = Field(min_length=1)
    connect_timeout_s: float = Field(default=10.0, gt=0, le=120)

    @field_validator("url")
    @classmethod
    def _valid_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"wss", "ws", "tls", "tcp"}:
            raise ValueError("url must start with wss:// (ws:// or tcp:// only on loopback)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("url must not carry credentials, a query, or a fragment; use token_env")
        try:
            assert_transport_allowed(parsed.scheme, parsed.hostname or "")
        except AgentBridgeError as exc:
            raise ValueError(str(exc)) from exc
        return value


class BridgeAgent:
    """AgentAdapter for a remote Agent Bridge v1 server."""

    def __init__(
        self,
        options: BridgeAgentOptions,
        *,
        env: Mapping[str, str],
        name: str = "bridge",
        client: AgentBridgeClient | None = None,
    ) -> None:
        self.options = options
        self.name = name
        self._token = env.get(options.token_env, "").strip()
        self._client = client or AgentBridgeClient()
        self._connect_lock = asyncio.Lock()
        self._abort = asyncio.Event()
        self._active = False
        self.keepalive_interval_s = 0.0

    @property
    def agent_capabilities(self) -> AgentCapabilityManifest:
        caps = {} if self._client.link is None else self._client.capabilities
        return manifest_from_caps(caps, voice_sessions_reason=VOICE_SESSIONS_REASON)

    async def _connected(self) -> AgentBridgeClient:
        async with self._connect_lock:
            if self._client.link is not None:
                return self._client
            if not self._token:
                raise AgentBridgeError(
                    "auth",
                    f"{self.options.token_env} is not set; export the bridge token before starting",
                    fatal=True,
                )
            await asyncio.wait_for(
                self._client.connect(self.options.url, self._token),
                self.options.connect_timeout_s,
            )
            return self._client

    def respond_streaming(
        self,
        text: str,
        *,
        on_event: Callable[..., Any] | None = None,
    ) -> AsyncIterator[str]:
        async def _stream() -> AsyncIterator[str]:
            client = await self._connected()
            self._abort.clear()
            self._active = True
            try:
                async with contextlib.aclosing(
                    client.respond_streaming(text, on_event=on_event)
                ) as inner:
                    async for sentence in inner:
                        if self._abort.is_set():
                            return
                        yield sentence
            finally:
                self._active = False

        return _stream()

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        client = await self._connected()
        return await client.respond(text, timeout=timeout)

    async def abort(self) -> None:
        if not self._active or self._client.link is None:
            return
        self._abort.set()
        with contextlib.suppress(AgentBridgeError):
            await self._client.abort()

    async def warmup(self) -> None:
        client = await self._connected()
        with contextlib.suppress(AgentBridgeError, TimeoutError):
            await client.warmup()

    async def close(self) -> None:
        await self._client.close()

    async def control(self, command: AgentControl) -> ControlResult:
        built = _control_frame(command, self._client._turn_seq, "")
        if built is None:
            return ControlRejected(reason=f"{type(command).__name__} is not supported by this bridge adapter")
        field, frame = built
        if getattr(self.agent_capabilities, field).level is not SupportLevel.SUPPORTED:
            return ControlRejected(reason=f"agent did not advertise {field}")
        client = await self._connected()
        frame.pop("id", None)
        frame["v"] = CONTRACT_VERSION
        try:
            reply = await client.send_control(frame, timeout=CONTROL_TIMEOUT_S)
        except AgentBridgeError as exc:
            return ControlRejected(reason=str(exc), retryable=exc.retryable)
        except TimeoutError:
            return ControlRejected(
                reason=f"the agent did not acknowledge {frame['type']} within {CONTROL_TIMEOUT_S:.0f}s",
                retryable=True,
            )
        if reply.get("type") == "control.rejected":
            return ControlRejected(
                reason=str(reply.get("reason") or "rejected by the agent"),
                retryable=bool(reply.get("retryable")),
            )
        return ControlAccepted(
            request_id=reply.get("requestId") or _request_id(command),
            resource=reply.get("resource"),
        )


async def probe_bridge(agent: BridgeAgent) -> dict[str, Any]:
    base = {"status": "needs_input", "protocol": "unknown", "login": "unknown"}
    try:
        await agent._connected()
    except AgentBridgeError as exc:
        if exc.code == "auth":
            return {**base, "protocol": "available", "login": "unavailable", "next_action": "The bridge rejected or lacks the credential. Check the variable named by token_env."}
        if exc.code == "version_mismatch":
            return {**base, "protocol": "unavailable", "next_action": "The remote agent speaks a different Agent Bridge major version."}
        return {**base, "protocol": "unavailable", "next_action": f"The bridge handshake failed ({exc.code})."}
    except (OSError, TimeoutError) as exc:
        return {**base, "protocol": "unavailable", "next_action": f"The bridge is not reachable ({type(exc).__name__}). Start the remote agent or fix [agent.options].url."}
    finally:
        with contextlib.suppress(Exception):
            await agent.close()
    return {**base, "protocol": "available", "login": "available", "next_action": "The bridge accepted the credential. Run `sleeptalk adapter test --live` to prove a spoken turn."}


__all__ = ["BridgeAgent", "BridgeAgentOptions", "probe_bridge"]
