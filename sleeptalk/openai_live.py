"""GPT-Live primary WebSocket. Audio is continuous, never committed as turns.

The browser keeps its existing WebRTC connection to Sleeptalk; only this
trusted host connects to OpenAI. No credentials travel to the browser.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from sleeptalk.openai_realtime import OpenAIRealtimeConfig, PCM16MonoResampler

LIVE_MODEL = "gpt-live-1"
logger = logging.getLogger(__name__)


class OpenAILiveError(RuntimeError):
    """A failed Live connection or rejected command (without credential data)."""


class OpenAILiveClient:
    def __init__(self, config: OpenAIRealtimeConfig, *, instructions: str,
                 http_session_factory=None, timeout: float = 15.0) -> None:
        self.config = config
        self.instructions = instructions
        self.timeout = timeout
        self._factory = http_session_factory or aiohttp.ClientSession
        self._http = self._ws = self._reader = None
        self._ready = asyncio.Event()
        self._ended = asyncio.Event()
        self._closing = False
        self._close_task = None
        self._failure: Exception | None = None
        self._events: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._send_lock = asyncio.Lock()
        self._input = PCM16MonoResampler(48_000, 24_000)
        self.session_id = ""
        self.usage: dict[str, Any] | None = None
        self.finalized = False

    @property
    def url(self) -> str:
        parts = urlsplit(self.config.base_url)
        if parts.scheme not in {"https", "http"} or parts.query or parts.fragment or parts.username:
            raise OpenAILiveError("Invalid OpenAI Live base URL")
        return urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc,
                           parts.path.rstrip("/") + "/live/sessions", "", ""))

    async def connect(self) -> None:
        if self._ws is not None:
            return
        try:
            async with asyncio.timeout(self.timeout):
                self._http = self._factory()
                self._ws = await self._http.ws_connect(
                    self.url, headers={"Authorization": f"Bearer {self.config.api_key}"},
                    heartbeat=20, max_msg_size=2 * 1024 * 1024,
                )
                self._reader = asyncio.create_task(self._read(), name="openai-live-reader")
                await self.send({"type": "session.start", "session": {
                    "model": LIVE_MODEL, "instructions": self.instructions,
                    "audio": {"format": {"type": "audio/pcm", "rate": 24000},
                              "output": {"voice": self.config.voice}},
                    "delegation": {"type": "client"}, "store": False,
                }})
                await self._ready.wait()
                self.check_health()
                if not self.session_id:
                    raise OpenAILiveError("OpenAI Live did not confirm session startup")
        except BaseException as exc:
            await self._dispose()
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, OpenAILiveError):
                raise
            status = getattr(exc, "status", None)
            raise OpenAILiveError(f"OpenAI Live connection failed ({status or type(exc).__name__})") from None

    def check_health(self) -> None:
        if self._failure:
            raise self._failure
        if self._closing:
            raise OpenAILiveError("OpenAI Live session is closed")

    async def send(self, event: dict) -> str:
        if event.get("type") != "session.close":
            self.check_health()
        elif self._failure:
            raise self._failure
        if self._ws is None:
            raise OpenAILiveError("OpenAI Live is not connected")
        event = {"event_id": uuid.uuid4().hex, **event}
        try:
            async with self._send_lock, asyncio.timeout(self.timeout):
                await self._ws.send_json(event)
        except (aiohttp.ClientError, TimeoutError, ConnectionError) as exc:
            raise OpenAILiveError(f"OpenAI Live send failed ({type(exc).__name__})") from None
        return event["event_id"]

    async def append_audio(self, pcm: bytes) -> None:
        self.check_health()
        # Bound each wire event even if a caller supplies a large frame.
        for offset in range(0, len(pcm), 9600):
            wire = self._input.feed(pcm[offset:offset + 9600])
            if wire:
                await self.send({"type": "session.input_audio.append",
                                 "audio": base64.b64encode(wire).decode("ascii")})

    async def context(self, kind: str, text: str, delegation_id: str | None = None) -> None:
        if kind not in {"thinking", "commentary", "instructions"}:
            raise ValueError("unknown Live context kind")
        # Each append permits 500 tokens. UTF-8 bytes bound tokens conservatively,
        # including scripts whose characters tokenize into multiple tokens.
        pending = text
        while pending:
            chunk = pending.encode("utf-8")[:450].decode("utf-8", errors="ignore")
            pending = pending[len(chunk):]
            await self.send({"type": f"session.{kind}.append", "content": chunk,
                             "delegation_id": delegation_id})

    async def next_event(self) -> dict:
        event = await self._events.get()
        if isinstance(event, Exception):
            raise event
        return event

    def _fail(self, exc: Exception) -> None:
        self._failure = exc
        logger.warning("%s", exc)
        self._ready.set()
        self._ended.set()
        # A failed stream must wake consumers even when the event queue is full.
        while not self._events.empty():
            self._events.get_nowait()
        self._events.put_nowait(exc)

    async def _read(self) -> None:
        try:
            async for message in self._ws:
                if message.type == aiohttp.WSMsgType.ERROR:
                    raise OpenAILiveError("OpenAI Live WebSocket failed")
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                event = json.loads(message.data)
                kind = event.get("type")
                if kind == "error":
                    code = str(event.get("error", {}).get("code", "provider_error"))
                    if self._closing:
                        # Closing rejects pending context appends, then sends the
                        # final session receipt. Drain errors without discarding it.
                        logger.debug("OpenAI Live closing command result (%s)", code[:80])
                        continue
                    # Fail visibly; never log a provider body that may echo inputs.
                    raise OpenAILiveError(f"OpenAI Live rejected a command ({code[:80]})")
                if kind == "session.started":
                    session = event.get("session", {})
                    self.session_id = str(session.get("id", ""))
                    if session.get("model", LIVE_MODEL) != LIVE_MODEL:
                        raise OpenAILiveError("OpenAI Live confirmed a different model")
                    self._ready.set()
                if kind == "session.closed":
                    self.usage = event.get("usage", {})
                    self.finalized = True
                    self._ended.set()
                self._events.put_nowait(event)
                if kind == "session.closed":
                    break
            if not self.finalized and not self._closing:
                raise OpenAILiveError("OpenAI Live disconnected before final usage")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(exc if isinstance(exc, OpenAILiveError) else
                       OpenAILiveError(f"OpenAI Live stream failed ({type(exc).__name__})"))

    async def close(self) -> None:
        # Route shutdown may cancel the playback task that first noticed a
        # disconnect. Preserve one graceful close across those racing callers.
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="openai-live-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        if self._closing:
            return
        try:
            if self._ws is not None and not self._failure and not self.finalized:
                self._closing = True
                await self.send({"type": "session.close"})
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._ended.wait(), self.timeout)
        finally:
            self._closing = True
            await self._dispose()

    async def _dispose(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
            self._reader = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._http is not None:
            await self._http.close()
            self._http = None


async def check_openai_live_connectivity(config: OpenAIRealtimeConfig) -> None:
    """Prove Live startup and finalization; not microphone or speech quality."""
    client = OpenAILiveClient(config, instructions="Wait quietly for the caller.")
    try:
        await client.connect()
    finally:
        await client.close()
    if not client.finalized:
        raise OpenAILiveError("OpenAI Live closed without final session usage")
