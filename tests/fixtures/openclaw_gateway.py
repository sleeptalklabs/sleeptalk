from __future__ import annotations

import asyncio
from typing import Any

from sleeptalk.agents.openclaw_gateway_adapter import (
    OpenClawGatewayError,
    PROTOCOL,
)

DEFAULT_METHODS = (
    "chat.send",
    "chat.abort",
    "sessions.create",
    "sessions.resolve",
    "sessions.steer",
    "sessions.abort",
    "sessions.subscribe",
    "sessions.messages.subscribe",
    "exec.approval.resolve",
)


class FakeGatewaySession:
    def __init__(
        self,
        *,
        protocol: int = PROTOCOL,
        methods: tuple[str, ...] = DEFAULT_METHODS,
        reject_connect: bool = False,
        fail_first_send: bool = False,
        fail_first_send_timeout: bool = False,
        fail_connect: bool = False,
        disconnect_before_challenge: bool = False,
        startup_sidecars: bool | int = False,
        extra_events: list[dict[str, Any]] | None = None,
        labels_in_use: tuple[str, ...] = (),
    ) -> None:
        self.labels_in_use = set(labels_in_use)
        self.protocol = protocol
        self.methods = methods
        self.reject_connect = reject_connect
        self.fail_first_send = fail_first_send
        self.fail_first_send_timeout = fail_first_send_timeout
        self.fail_connect = fail_connect
        self.disconnect_before_challenge = disconnect_before_challenge
        self.startup_sidecars = startup_sidecars
        self.extra_events = list(extra_events or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.connect_count = 0
        self.created = False
        self.send_keys: list[str] = []
        self._session_key = "s1"
        self._generation = 0
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def connect(self) -> None:
        if self.fail_connect:
            raise OpenClawGatewayError("disconnected", retryable=True)
        self.connect_count += 1
        self._generation += 1
        self._events = asyncio.Queue()
        if self.disconnect_before_challenge:
            await self._events.put({"event": "_disconnect", "payload": {}})
            return
        await self._events.put(
            {"event": "connect.challenge", "payload": {"nonce": "test-nonce", "ts": 1}}
        )

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = params or {}
        self.calls.append((method, payload))
        return await self.handle(method, payload)

    async def events(self) -> asyncio.Queue[dict[str, Any]]:
        return self._events

    async def close(self) -> None:
        self._generation += 1
        old = self._events
        self._events = asyncio.Queue()
        old.put_nowait({"event": "_disconnect", "payload": {}})

    async def push(self, event: dict[str, Any], generation: int | None = None) -> None:
        if generation is not None and generation != self._generation:
            return
        await self._events.put(event)

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "connect":
            if self.reject_connect:
                raise OpenClawGatewayError("AUTH_TOKEN_MISMATCH", authentication_failed=True)
            if self.startup_sidecars:
                self.startup_sidecars = int(self.startup_sidecars) - 1
                raise OpenClawGatewayError(
                    "unavailable",
                    retryable=True,
                    reason="startup-sidecars",
                    retry_after_ms=1,
                )
            return {
                "type": "hello-ok",
                "protocol": self.protocol,
                "features": {
                    "methods": list(self.methods),
                    "events": ["chat", "session.tool", "tick"],
                },
                "snapshot": {},
                "auth": {"role": "operator", "scopes": list(params.get("scopes") or [])},
                "policy": {},
            }
        if method == "sessions.create":
            label = str(params.get("label") or "")
            if label in self.labels_in_use:
                # As the real gateway answers (INVALID_REQUEST).
                raise OpenClawGatewayError(f"label already in use: {label}")
            self.labels_in_use.add(label)
            self.created = True
            return {"ok": True, "key": "s1"}
        if method == "sessions.resolve":
            return {"ok": True, "key": str(params.get("key") or "s1")}
        if method == "sessions.subscribe":
            return {"ok": True, "subscribed": True}
        if method == "sessions.messages.subscribe":
            return {"ok": True}
        if method == "chat.send":
            key = str(params.get("idempotencyKey") or "")
            self.send_keys.append(key)
            if self.fail_first_send:
                self.fail_first_send = False
                raise OpenClawGatewayError("disconnected", retryable=True)
            if self.fail_first_send_timeout:
                self.fail_first_send_timeout = False
                raise TimeoutError("OpenClaw gateway request timed out")
            session_key = str(params.get("sessionKey") or "s1")
            self._session_key = session_key
            generation = self._generation
            asyncio.get_running_loop().create_task(
                self._emit_prompt(str(params.get("message") or ""), session_key, generation)
            )
            return {"runId": "run-1", "status": "started", "sessionKey": session_key}
        if method == "chat.abort":
            asyncio.get_running_loop().create_task(
                self.push(
                    {
                        "event": "chat",
                        "payload": {
                            "state": "aborted",
                            "runId": str(params.get("runId") or "run-1"),
                            "sessionKey": str(params.get("sessionKey") or self._session_key),
                            "seq": 99,
                        },
                    }
                )
            )
            return {"ok": True}
        if method == "sessions.abort":
            return {"ok": True}
        if method == "sessions.steer":
            return {"ok": True}
        if method == "exec.approval.resolve":
            asyncio.get_running_loop().create_task(self._finish_after_ask())
            return {"ok": True}
        raise OpenClawGatewayError(f"unknown method {method}")

    async def _emit_prompt(self, text: str, session_key: str, generation: int) -> None:
        extras, self.extra_events = self.extra_events, []
        for event in extras:
            if generation != self._generation:
                return
            await self.push(event, generation)
        if generation != self._generation:
            return
        key = text.strip().lower()
        if key == "hang":
            return
        if key == "permission":
            await self.push(
                {
                    "event": "exec.approval.requested",
                    "payload": {
                        "id": "perm-1",
                        "createdAtMs": 1,
                        "expiresAtMs": 2,
                        "request": {
                            "sessionKey": session_key,
                            "runId": "run-1",
                            "command": "ls",
                            "allowedDecisions": ["allow-once", "deny"],
                        },
                    },
                },
                generation,
            )
            return
        await self._emit_work(key, session_key, generation)

    async def _emit_work(self, key: str, session_key: str, generation: int) -> None:
        await self.push(
            {
                "event": "session.operation",
                "payload": {"phase": "thinking", "sessionKey": session_key},
            },
            generation,
        )
        if key in {"tool", "agent-tool"}:
            tool_event = "agent" if key == "agent-tool" else "session.tool"
            start_payload: dict[str, Any] = {
                "name": "terminal",
                "phase": "start",
                "sessionKey": session_key,
                "runId": "run-1",
                "args": {"command": "cd /repo && git status"},
            }
            end_payload: dict[str, Any] = {
                "name": "terminal",
                "phase": "end",
                "sessionKey": session_key,
                "runId": "run-1",
            }
            if key == "agent-tool":
                start_payload = {
                    "stream": "tool",
                    "sessionKey": session_key,
                    "runId": "run-1",
                    "data": {
                        "phase": "start",
                        "name": "terminal",
                        "args": {"command": "cd /repo && git status"},
                    },
                }
                end_payload = {
                    "stream": "tool",
                    "sessionKey": session_key,
                    "runId": "run-1",
                    "data": {"phase": "end", "name": "terminal"},
                }
            await self.push(
                {"event": tool_event, "payload": start_payload},
                generation,
            )
            await self.push(
                {"event": tool_event, "payload": end_payload},
                generation,
            )
        if key == "fail":
            await self.push(
                {
                    "event": "chat",
                    "payload": {
                        "state": "error",
                        "errorMessage": "boom",
                        "runId": "run-1",
                        "sessionKey": session_key,
                        "seq": 1,
                    },
                },
                generation,
            )
            return
        await self.push(
            {
                "event": "chat",
                "payload": {
                    "state": "delta",
                    "deltaText": "Bitcoin is up. ",
                    "runId": "run-1",
                    "sessionKey": session_key,
                    "seq": 1,
                },
            },
            generation,
        )
        if key == "slow":
            return
        await self.push(
            {
                "event": "chat",
                "payload": {
                    "state": "delta",
                    "deltaText": "It gained 2%.",
                    "runId": "run-1",
                    "sessionKey": session_key,
                    "seq": 2,
                },
            },
            generation,
        )
        await self.push(
            {
                "event": "chat",
                "payload": {
                    "state": "final",
                    "runId": "run-1",
                    "sessionKey": session_key,
                    "seq": 3,
                },
            },
            generation,
        )

    async def _finish_after_ask(self) -> None:
        session_key = self._session_key
        await self.push(
            {
                "event": "chat",
                "payload": {
                    "state": "delta",
                    "deltaText": "Approved.",
                    "runId": "run-1",
                    "sessionKey": session_key,
                    "seq": 10,
                },
            }
        )
        await self.push(
            {
                "event": "chat",
                "payload": {
                    "state": "final",
                    "runId": "run-1",
                    "sessionKey": session_key,
                    "seq": 11,
                },
            }
        )
