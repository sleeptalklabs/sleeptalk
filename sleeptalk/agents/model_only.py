"""Talk to a model directly: no agent framework, no tools, one streamed request per turn.

Registered as ``type = "model"``. The credential comes only from the explicit
``[agent.auth]`` selection that ``resolve_agent_auth_env`` places in the
environment; this module never reads an ambient provider key. Two wire
protocols are accepted, both API-key mode: OpenAI chat completions (OpenRouter,
OpenAI, Ollama, and any compatible gateway) and Anthropic messages. Plain
http is accepted only on loopback, which is where Ollama lives.

The adapter keeps the conversation in memory for the life of the process and
forgets it on close. There are no tools and there never will be here; tools are
what the agent adapters are for.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Literal

from pydantic import Field

from sleeptalk.adapter import (
    AgentCapabilityManifest,
    AgentError,
    AssistantDelta,
    CapabilitySupport,
    SupportLevel,
    ThinkingStarted,
    VoiceSessionCapability,
    WorkerCapability,
    publish_event,
)
from sleeptalk.agents.provider_auth import ProviderSelection
from sleeptalk.registries import ComponentOptions
from sleeptalk.streaming import SentenceStreamer
from sleeptalk.voice_prompt import VOICE_SPEECH_CONTRACT

AGENT_TYPE = "model"
PROTOCOLS = ("openai-completions", "anthropic-messages")

#: Vendor presets the wizard offers. Anything else is "other OpenAI-compatible"
#: with a pasted HTTPS base URL.
PRESETS: dict[str, dict[str, str]] = {
    "openrouter": {
        "label": "OpenRouter",
        "protocol": "openai-completions",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "anthropic/claude-opus-5",
        "console": "https://openrouter.ai/keys",
    },
    "openai": {
        "label": "OpenAI",
        "protocol": "openai-completions",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-5.5",
        "console": "https://platform.openai.com/api-keys",
    },
    "anthropic": {
        "label": "Anthropic",
        "protocol": "anthropic-messages",
        "base_url": "https://api.anthropic.com",
        "model": "claude-opus-5",
        "console": "https://console.anthropic.com/settings/keys",
    },
    "ollama": {
        "label": "Ollama",
        "protocol": "openai-completions",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "",  # whatever is pulled locally; setup lists them
        "console": "",
    },
}

OPENROUTER_BASE_URL = PRESETS["openrouter"]["base_url"]
_ANTHROPIC_VERSION = "2023-06-01"
_FRAME_QUEUE_SIZE = 256
_EOF = object()


class ModelOnlyError(RuntimeError):
    """A turn could not run against the selected model endpoint."""


class ModelOnlyOptions(ComponentOptions):
    """Non-secret ``[agent.options]`` for a model-only profile."""

    #: The reply budget. A reasoning model spends it on thinking first: at
    #: 1024, Qwen 3.6 Flash thought through all of it and never answered.
    max_tokens: int = Field(default=4096, gt=0, le=65536)
    #: "off" asks OpenRouter to skip the model's thinking: a spoken answer in
    #: half a second rather than after 7-16 s of reasoning. A model that must
    #: reason is asked again with it on.
    reasoning: Literal["off", "on"] = "off"
    #: Prior user/assistant exchanges kept in the request; 0 forgets everything between turns.
    history_turns: int = Field(default=20, ge=0, le=200)
    connect_timeout_s: float = Field(default=10.0, gt=0, le=120)
    idle_timeout_s: float = Field(default=60.0, gt=0, le=600)
    temperature: float | None = Field(default=None, ge=0, le=2)


def _supported() -> CapabilitySupport:
    return CapabilitySupport(SupportLevel.SUPPORTED)


def _unsupported() -> CapabilitySupport:
    return CapabilitySupport(SupportLevel.UNSUPPORTED)


MANIFEST = AgentCapabilityManifest(
    contract_version=1,
    structured_events=_supported(),
    tool_lifecycle=_unsupported(),
    permissions=_unsupported(),
    clarification=_unsupported(),
    session_identity=_unsupported(),
    status=_unsupported(),
    usage=_unsupported(),
    steering=_unsupported(),
    targeted_cancellation=_unsupported(),
    workers=WorkerCapability(SupportLevel.UNSUPPORTED),
    voice_sessions=VoiceSessionCapability(SupportLevel.UNSUPPORTED),
)


def system_prompt(persona_instructions: str = "") -> str:
    persona = (persona_instructions or "").strip()
    return f"{VOICE_SPEECH_CONTRACT}\n\n{persona}" if persona else VOICE_SPEECH_CONTRACT


def check_selection(selection: ProviderSelection) -> None:
    if selection.mode != "api_key":
        raise ModelOnlyError("Model-only profiles use an API key; subscription logins belong to their own agents.")
    if selection.protocol not in PROTOCOLS:
        raise ModelOnlyError("Model-only profiles speak OpenAI chat completions or Anthropic messages.")


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


def build_request(
    selection: ProviderSelection,
    *,
    model: str,
    system: str,
    messages: list[dict[str, str]],
    options: ModelOnlyOptions,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """The URL, headers, and JSON body for one streamed turn. Pure, so tests can pin it."""

    check_selection(selection)
    if selection.protocol == "anthropic-messages":
        url = selection.base_url + "/v1/messages"
        headers = {
            "x-api-key": selection.credential,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": options.max_tokens,
            "system": system,
            "messages": messages,
            "stream": True,
        }
    else:
        url = selection.base_url + "/chat/completions"
        headers = {
            "authorization": f"Bearer {selection.credential}",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        if selection.base_url == OPENROUTER_BASE_URL:
            headers["x-title"] = "Sleeptalk"
        body = {
            "model": model,
            "max_tokens": options.max_tokens,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": True,
        }
        if selection.base_url == OPENROUTER_BASE_URL and options.reasoning == "off":
            body["reasoning"] = {"enabled": False}
    if options.temperature is not None:
        body["temperature"] = options.temperature
    return url, headers, body


def parse_event(protocol: str, data: str) -> tuple[str, str] | None:
    """One SSE ``data:`` payload -> ("text"|"reasoning"|"done"|"error", detail), or None to ignore.

    "done" carries why the model stopped when it says: "length" is a reply
    budget used up, which with no text means it was all spent thinking.
    """

    if data == "[DONE]":
        return ("done", "")
    try:
        payload = json.loads(data)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        return ("error", str(message) if message else "the model endpoint reported an error")
    if protocol == "anthropic-messages":
        kind = payload.get("type")
        if kind == "content_block_delta":
            delta = payload.get("delta")
            if isinstance(delta, dict):
                if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                    return ("text", delta["text"])
                if delta.get("type") == "thinking_delta":
                    return ("reasoning", "")
            return None
        if kind == "message_delta":
            delta = payload.get("delta")
            if isinstance(delta, dict) and delta.get("stop_reason") == "max_tokens":
                return ("done", "length")
            return None
        if kind == "message_stop":
            return ("done", "")
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    choice = choices[0]
    delta = choice.get("delta")
    if isinstance(delta, dict):
        content = delta.get("content")
        if isinstance(content, str) and content:
            return ("text", content)
        reasoning = delta.get("reasoning") or delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            return ("reasoning", "")
    if choice.get("finish_reason"):
        return ("done", "length" if choice["finish_reason"] == "length" else "")
    return None


async def _iter_sse_data(content: Any) -> AsyncIterator[str]:
    """Yield each SSE event's joined ``data`` payload from a streamed body."""

    buffer = b""
    data_lines: list[str] = []

    def _flush() -> str | None:
        if not data_lines:
            return None
        joined = "\n".join(data_lines)
        data_lines.clear()
        return joined

    async for chunk in content.iter_any():
        buffer += chunk
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            raw, buffer = buffer[:newline], buffer[newline + 1 :]
            line = raw.decode("utf-8", errors="replace").rstrip("\r")
            if not line:
                event = _flush()
                if event is not None:
                    yield event
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            # "event:", "id:", and ":" comment lines carry nothing we need.
    tail = buffer.decode("utf-8", errors="replace").strip()
    if tail.startswith("data:"):
        data_lines.append(tail[5:].lstrip())
    event = _flush()
    if event is not None:
        yield event


def _status_error(name: str, status: int, body: str) -> ModelOnlyError:
    detail = ""
    with contextlib.suppress(ValueError, TypeError, AttributeError):
        parsed = json.loads(body)
        error = parsed.get("error") if isinstance(parsed, dict) else None
        detail = str(error.get("message") if isinstance(error, dict) else error or "").strip()
    if status in {401, 403}:
        return ModelOnlyError(f"{name}: the model endpoint rejected the credential (HTTP {status}); re-enter it in setup")
    if status == 404:
        return ModelOnlyError(f"{name}: the model endpoint has no such model or path (HTTP 404){': ' + detail if detail else ''}")
    if status == 429:
        return ModelOnlyError(f"{name}: the model endpoint is rate limiting this key (HTTP 429)")
    return ModelOnlyError(f"{name}: the model endpoint returned HTTP {status}{': ' + detail if detail else ''}")


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class ModelOnlyAgent:
    """AgentAdapter over one streamed chat request per turn."""

    def __init__(
        self,
        selection: ProviderSelection,
        *,
        model: str,
        options: ModelOnlyOptions | None = None,
        persona_instructions: str = "",
        name: str = AGENT_TYPE,
    ) -> None:
        check_selection(selection)
        if not model or not model.strip():
            raise ModelOnlyError("Model-only profiles need an explicit model name")
        self.selection = selection
        self.model = model.strip()
        self.options = options or ModelOnlyOptions()
        self.name = name
        self.system = system_prompt(persona_instructions)
        self.agent_capabilities = MANIFEST
        self.keepalive_interval_s = 0.0
        self._history: list[dict[str, str]] = []
        self._http: Any = None
        self._lock = asyncio.Lock()
        self._abort = asyncio.Event()
        self._active = False
        self._closed = False
        # Set once the endpoint refused a turn without reasoning ("Reasoning is
        # mandatory"): later turns ask with it on rather than failing first.
        self._reasoning_required = False

    # -- state -------------------------------------------------------------

    @property
    def history(self) -> list[dict[str, str]]:
        return list(self._history)

    def _request_messages(self, text: str) -> list[dict[str, str]]:
        keep = self.options.history_turns * 2
        prior = self._history[-keep:] if keep else []
        return [*prior, {"role": "user", "content": text}]

    def _remember(self, text: str, reply: str) -> None:
        self._history.append({"role": "user", "content": text})
        if reply:
            self._history.append({"role": "assistant", "content": reply})
        else:
            # A turn with no reply cannot be replayed (Anthropic rejects an
            # empty assistant turn and two user turns in a row are noise).
            self._history.pop()

    async def _session(self) -> Any:
        import aiohttp

        if self._closed:
            raise ModelOnlyError(f"{self.name} is closed")
        if self._http is None:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=None,
                    sock_connect=self.options.connect_timeout_s,
                    sock_read=self.options.idle_timeout_s,
                ),
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        return self._http

    # -- AgentAdapter --------------------------------------------------------

    def respond_streaming(
        self,
        text: str,
        *,
        on_event: Callable[..., Any] | None = None,
    ) -> AsyncIterator[str]:
        async def _stream() -> AsyncIterator[str]:
            import aiohttp

            async with self._lock:
                session = await self._session()
                self._abort.clear()
                self._active = True
                streamer = SentenceStreamer()
                messages = self._request_messages(text)
                options = self.options.model_copy(update={"reasoning": "on"}) if self._reasoning_required else self.options
                url, headers, body = build_request(
                    self.selection, model=self.model, system=self.system,
                    messages=messages, options=options,
                )
                frames: asyncio.Queue[Any] = asyncio.Queue(maxsize=_FRAME_QUEUE_SIZE)
                spoken: list[str] = []
                thinking_fired = False

                async def _pump() -> None:
                    request = body
                    try:
                        while True:
                            async with session.post(url, json=request, headers=headers) as response:
                                if response.status >= 400:
                                    detail = (await response.content.read(65536)).decode("utf-8", errors="replace")
                                    if response.status == 400 and "reasoning" in request and "reasoning" in detail.lower():
                                        # This model can't skip its thinking: ask again with it on.
                                        self._reasoning_required = True
                                        request = {k: v for k, v in request.items() if k != "reasoning"}
                                        continue
                                    raise _status_error(self.name, response.status, detail)
                                async for data in _iter_sse_data(response.content):
                                    await frames.put(data)
                            break
                        await frames.put(_EOF)
                    except asyncio.CancelledError:
                        raise
                    except ModelOnlyError as exc:
                        await frames.put(exc)
                    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                        await frames.put(ModelOnlyError(f"{self.name}: request failed: {type(exc).__name__}"))

                pump = asyncio.create_task(_pump(), name=f"{self.name}-turn")
                try:
                    while True:
                        getter = asyncio.ensure_future(frames.get())
                        aborted = asyncio.ensure_future(self._abort.wait())
                        try:
                            done, _ = await asyncio.wait(
                                {getter, aborted},
                                timeout=self.options.idle_timeout_s,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                        finally:
                            aborted.cancel()
                            if not getter.done():
                                getter.cancel()
                        if getter not in done:
                            if aborted in done:
                                return
                            raise ModelOnlyError(f"{self.name}: the model sent nothing for {self.options.idle_timeout_s:.0f}s")
                        frame = getter.result()
                        if isinstance(frame, ModelOnlyError):
                            publish_event(on_event, AgentError(code="model", message=str(frame), retryable=True))
                            raise frame
                        if frame is _EOF:
                            tail = streamer.flush()
                            if tail and not self._abort.is_set():
                                spoken.append(tail)
                                yield tail
                            return
                        parsed = parse_event(self.selection.protocol, frame)
                        if parsed is None:
                            continue
                        kind, detail = parsed
                        if kind == "error":
                            error = ModelOnlyError(f"{self.name}: {detail}")
                            publish_event(on_event, AgentError(code="model", message=detail, retryable=True))
                            raise error
                        if kind == "reasoning":
                            if not thinking_fired:
                                thinking_fired = True
                                publish_event(on_event, ThinkingStarted())
                            continue
                        if kind == "done":
                            tail = streamer.flush()
                            if tail and not self._abort.is_set():
                                spoken.append(tail)
                                yield tail
                            if detail == "length" and not spoken:
                                message = (f"the model used its whole reply budget ({options.max_tokens} tokens) "
                                           "thinking and never answered; raise max_tokens or pick a non-reasoning model")
                                publish_event(on_event, AgentError(code="model", message=message, retryable=True))
                                raise ModelOnlyError(f"{self.name}: {message}")
                            return
                        publish_event(on_event, AssistantDelta(text=detail))
                        for sentence in streamer.feed(detail):
                            if self._abort.is_set():
                                return
                            spoken.append(sentence)
                            yield sentence
                finally:
                    self._active = False
                    if not pump.done():
                        pump.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await pump
                    self._remember(text, " ".join(spoken).strip())

        return _stream()

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        parts: list[str] = []
        async with asyncio.timeout(timeout):
            async for sentence in self.respond_streaming(text):
                parts.append(sentence)
        return " ".join(parts)

    async def abort(self) -> None:
        if self._active:
            self._abort.set()

    async def warmup(self) -> None:
        await self._session()

    async def close(self) -> None:
        self._closed = True
        self._abort.set()
        self._history.clear()
        if self._http is not None:
            await self._http.close()
            self._http = None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

CONFIG_FIELDS = frozenset({
    "auth", "model", "provider", "options", "name", "slug", "persona_instructions", "persona_file",
    "workspace_dir", "on_incompatible",
})


def selection_from_env(env: Mapping[str, str]) -> ProviderSelection:
    selection = ProviderSelection.from_environment(env)
    if selection is None:
        raise ModelOnlyError(
            "Model-only profiles need an explicit model credential: run setup and choose a vendor, or add [agent.auth] to the profile."
        )
    check_selection(selection)
    return selection


def build_model_agent(config: Any, env: dict[str, str]) -> ModelOnlyAgent:
    selection = selection_from_env(env)
    options = ModelOnlyOptions.model_validate(getattr(config, "options", None) or {})
    return ModelOnlyAgent(
        selection,
        model=str(getattr(config, "model", "") or ""),
        options=options,
        persona_instructions=str(getattr(config, "persona_instructions", "") or ""),
        name=str(getattr(config, "slug", "") or AGENT_TYPE),
    )


async def probe(config: Any, env: dict[str, str]) -> dict[str, str]:
    """Readiness for the wizard: is the credential bound, and does the endpoint accept it?"""

    from sleeptalk.agents.provider_auth import verify

    result = {"status": "needs_input", "protocol": "unknown", "workspace": "available",
              "permissions": "available", "login": "unknown", "next_action": ""}
    try:
        selection = selection_from_env(env)
        options = ModelOnlyOptions.model_validate(getattr(config, "options", None) or {})
        del options
    except (ModelOnlyError, ValueError) as exc:
        return {**result, "next_action": str(exc)}
    model = str(getattr(config, "model", "") or "").strip()
    if not model:
        return {**result, "protocol": "available", "next_action": "Choose a model name for the selected vendor."}
    login = await verify(selection, model)
    if login == "available":
        return {**result, "status": "ready", "protocol": "available", "login": "available"}
    if login == "unavailable":
        return {**result, "protocol": "available", "login": "unavailable",
                "next_action": "The endpoint rejected this key. Re-enter it privately in setup."}
    return {**result, "protocol": "available", "login": "unknown",
            "next_action": f"Could not confirm that {model} is available to this key; the first call will tell."}


def register(agents: Any) -> None:
    if AGENT_TYPE in agents:
        return
    agents.register(
        AGENT_TYPE,
        build_model_agent,
        options_schema=ModelOnlyOptions,
        config_fields=CONFIG_FIELDS,
        probe=lambda config, env: probe(config, env),
        aliases=("model-only", "llm"),
        install_hint="model-only needs no install: choose a vendor and enter one API key in setup",
    )
