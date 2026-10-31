"""type = "model": one streamed request per turn, no agent framework, no tools."""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest

from sleeptalk.adapter import AgentError, AssistantDelta, ThinkingStarted, typed_events
from sleeptalk.agents import model_only
from sleeptalk.agents.model_only import (
    ModelOnlyAgent,
    ModelOnlyError,
    ModelOnlyOptions,
    build_request,
    parse_event,
)
from sleeptalk.agents.provider_auth import ProviderSelection
from sleeptalk.defaults import register_defaults

register_defaults()

OPENROUTER = ProviderSelection("api_key", "openai-completions", "https://openrouter.ai/api/v1", "sk-or-test")
ANTHROPIC = ProviderSelection("api_key", "anthropic-messages", "https://api.anthropic.com", "sk-ant-test")


# ---------------------------------------------------------------------------
# A fake aiohttp session: the adapter imports aiohttp lazily and only needs
# ClientSession(...).post(...) as an async context manager with .status and
# .content.iter_any() / .read().
# ---------------------------------------------------------------------------


def sse(*payloads) -> bytes:
    return b"".join(
        (b"data: " + (p.encode() if isinstance(p, str) else json.dumps(p).encode()) + b"\n\n")
        for p in payloads
    )


class _Response:
    def __init__(self, status: int = 200, body: bytes = b"", *, hang: asyncio.Event | None = None, cancelled: asyncio.Event | None = None):
        self.status = status
        self.headers = {}
        self._body = body
        self._hang = hang
        self._cancelled = cancelled
        self.content = self

    async def iter_any(self):
        try:
            for start in range(0, len(self._body), 7):
                yield self._body[start:start + 7]
            if self._hang is not None:
                await self._hang.wait()
        except asyncio.CancelledError:
            if self._cancelled is not None:
                self._cancelled.set()
            raise

    async def iter_chunked(self, size: int):
        async for chunk in self.iter_any():
            yield chunk

    async def read(self, n: int = -1) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _Session:
    def __init__(self, responder):
        self.responder = responder
        self.calls: list[tuple[str, dict, dict]] = []
        self.closed = False

    def post(self, url, *, json=None, headers=None):
        self.calls.append((url, json, headers))
        return self.responder(url, json, headers)

    def get(self, url, **kwargs):
        return self.responder(url, kwargs.get("params"), kwargs.get("headers"))

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.close()
        return False


def fake_http(monkeypatch, responder) -> list[_Session]:
    sessions: list[_Session] = []

    def make(*_args, **_kwargs):
        session = _Session(responder)
        sessions.append(session)
        return session

    monkeypatch.setattr(aiohttp, "ClientSession", make)
    return sessions


def openai_chunks(*texts: str, reasoning: str = "") -> bytes:
    frames = []
    if reasoning:
        frames.append({"choices": [{"delta": {"reasoning": reasoning}}]})
    frames += [{"choices": [{"delta": {"content": text}}]} for text in texts]
    frames.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    return sse(*frames, "[DONE]")


def anthropic_chunks(*texts: str) -> bytes:
    frames = [{"type": "message_start"}, {"type": "content_block_start", "index": 0}]
    frames += [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": t}} for t in texts]
    frames += [{"type": "content_block_stop"}, {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}, {"type": "message_stop"}]
    return sse(*frames)


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


def test_openai_request_shape_puts_system_first_and_bearer_header():
    url, headers, body = build_request(
        OPENROUTER, model="anthropic/claude-opus-5", system="Speak.", messages=[{"role": "user", "content": "hi"}],
        options=ModelOnlyOptions(max_tokens=99),
    )
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert headers["authorization"] == "Bearer sk-or-test"
    assert headers["x-title"] == "Sleeptalk"
    assert body == {
        "model": "anthropic/claude-opus-5", "max_tokens": 99, "stream": True,
        "messages": [{"role": "system", "content": "Speak."}, {"role": "user", "content": "hi"}],
        "reasoning": {"enabled": False},
    }
    _, _, thinking = build_request(OPENROUTER, model="m", system="S", messages=[], options=ModelOnlyOptions(reasoning="on"))
    assert "reasoning" not in thinking
    assert ModelOnlyOptions().max_tokens == 4096


def test_anthropic_request_shape_uses_top_level_system_and_api_key_header():
    url, headers, body = build_request(
        ANTHROPIC, model="claude-opus-5", system="Speak.", messages=[{"role": "user", "content": "hi"}],
        options=ModelOnlyOptions(temperature=0.3),
    )
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "sk-ant-test"
    assert headers["anthropic-version"]
    assert "authorization" not in headers
    assert body["system"] == "Speak."
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["temperature"] == 0.3
    assert body["stream"] is True


@pytest.mark.parametrize("selection", [
    ProviderSelection("api_key", "openai-responses", "https://api.openai.com/v1", "sk"),
    ProviderSelection("api_key", "cursor", "https://api2.cursor.sh", "sk"),
])
def test_only_api_key_completions_or_messages_are_accepted(selection):
    with pytest.raises(ModelOnlyError):
        ModelOnlyAgent(selection, model="m")


def test_parse_event_covers_both_protocols():
    assert parse_event("openai-completions", "[DONE]") == ("done", "")
    assert parse_event("openai-completions", json.dumps({"choices": [{"delta": {"content": "Hi"}}]})) == ("text", "Hi")
    assert parse_event("openai-completions", json.dumps({"choices": [{"delta": {"reasoning": "hm"}}]})) == ("reasoning", "")
    assert parse_event("openai-completions", json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})) == ("done", "")
    assert parse_event("openai-completions", json.dumps({"error": {"message": "quota"}})) == ("error", "quota")
    assert parse_event("openai-completions", ": OPENROUTER PROCESSING") is None
    assert parse_event("anthropic-messages", json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Yo"}})) == ("text", "Yo")
    assert parse_event("anthropic-messages", json.dumps({"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "…"}})) == ("reasoning", "")
    assert parse_event("anthropic-messages", json.dumps({"type": "message_stop"})) == ("done", "")
    assert parse_event("openai-completions", json.dumps({"choices": [{"delta": {}, "finish_reason": "length"}]})) == ("done", "length")
    assert parse_event("anthropic-messages", json.dumps({"type": "message_delta", "delta": {"stop_reason": "max_tokens"}})) == ("done", "length")
    assert parse_event("anthropic-messages", json.dumps({"type": "ping"})) is None


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------


async def test_openai_stream_yields_sentences_events_and_history(monkeypatch):
    sessions = fake_http(monkeypatch, lambda *_: _Response(200, openai_chunks("Hello there. ", "How can I ", "help you today?")))
    agent = ModelOnlyAgent(OPENROUTER, model="anthropic/claude-opus-5", persona_instructions="Be kind.")
    events: list[object] = []
    sentences = [s async for s in agent.respond_streaming("hi", on_event=typed_events(events.append))]
    assert sentences == ["Hello there.", "How can I help you today?"]
    assert [type(e).__name__ for e in events] == ["AssistantDelta"] * 3
    assert isinstance(events[0], AssistantDelta)
    assert agent.history == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hello there. How can I help you today?"},
    ]
    url, body, headers = sessions[0].calls[0]
    assert body["messages"][0]["role"] == "system"
    assert "Be kind." in body["messages"][0]["content"]
    assert "sk-or-test" not in json.dumps(body)
    # The next turn replays the exchange.
    await agent.respond("again")
    assert [m["role"] for m in sessions[0].calls[1][1]["messages"]] == ["system", "user", "assistant", "user"]
    await agent.close()
    assert sessions[0].closed
    assert agent.history == []


async def test_anthropic_stream_and_thinking_fires_once(monkeypatch):
    body = sse(
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "a"}},
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "b"}},
    ) + anthropic_chunks("Sure thing. ", "Done.")
    fake_http(monkeypatch, lambda *_: _Response(200, body))
    agent = ModelOnlyAgent(ANTHROPIC, model="claude-opus-5")
    events: list[object] = []
    sentences = [s async for s in agent.respond_streaming("go", on_event=typed_events(events.append))]
    assert sentences == ["Sure thing.", "Done."]
    assert sum(isinstance(e, ThinkingStarted) for e in events) == 1


async def test_respond_joins_sentences(monkeypatch):
    fake_http(monkeypatch, lambda *_: _Response(200, openai_chunks("One. ", "Two.")))
    agent = ModelOnlyAgent(OPENROUTER, model="m")
    assert await agent.respond("x") == "One. Two."


async def test_rejected_credential_is_actionable_and_not_echoed(monkeypatch):
    fake_http(monkeypatch, lambda *_: _Response(401, b'{"error":{"message":"bad key sk-or-test"}}'))
    agent = ModelOnlyAgent(OPENROUTER, model="m")
    events: list[object] = []
    with pytest.raises(ModelOnlyError, match="rejected the credential") as info:
        [s async for s in agent.respond_streaming("x", on_event=typed_events(events.append))]
    assert "sk-or-test" not in str(info.value)
    assert any(isinstance(e, AgentError) for e in events)
    assert agent.history == []


async def test_missing_model_is_named(monkeypatch):
    fake_http(monkeypatch, lambda *_: _Response(404, b'{"error":{"message":"No endpoints found for nope/x"}}'))
    agent = ModelOnlyAgent(OPENROUTER, model="nope/x")
    with pytest.raises(ModelOnlyError, match="HTTP 404.*nope/x"):
        await agent.respond("x")


async def test_stream_error_frame_raises(monkeypatch):
    fake_http(monkeypatch, lambda *_: _Response(200, sse({"choices": [{"delta": {"content": "Partial "}}]}, {"error": {"message": "overloaded"}})))
    agent = ModelOnlyAgent(OPENROUTER, model="m")
    with pytest.raises(ModelOnlyError, match="overloaded"):
        await agent.respond("x")


async def test_abort_cancels_the_request(monkeypatch):
    hang, cancelled = asyncio.Event(), asyncio.Event()
    fake_http(monkeypatch, lambda *_: _Response(200, sse({"choices": [{"delta": {"content": "First sentence here. And then"}}]}), hang=hang, cancelled=cancelled))
    agent = ModelOnlyAgent(OPENROUTER, model="m")
    spoken = []
    async for sentence in agent.respond_streaming("x"):
        spoken.append(sentence)
        await agent.abort()
    assert spoken == ["First sentence here."]
    assert await asyncio.wait_for(cancelled.wait(), timeout=3)
    # What was spoken is what the model is told it said.
    assert agent.history[-1] == {"role": "assistant", "content": "First sentence here."}
    await agent.abort()  # idle abort is safe
    await agent.close()


async def test_idle_timeout_is_reported(monkeypatch):
    hang = asyncio.Event()
    fake_http(monkeypatch, lambda *_: _Response(200, b"", hang=hang))
    agent = ModelOnlyAgent(OPENROUTER, model="m", options=ModelOnlyOptions(idle_timeout_s=0.05))
    with pytest.raises(ModelOnlyError, match="sent nothing"):
        await agent.respond("x")


async def test_history_is_bounded(monkeypatch):
    sessions = fake_http(monkeypatch, lambda *_: _Response(200, openai_chunks("Ok.")))
    agent = ModelOnlyAgent(OPENROUTER, model="m", options=ModelOnlyOptions(history_turns=1))
    for text in ("a", "b", "c"):
        await agent.respond(text)
    replayed = sessions[0].calls[-1][1]["messages"]
    assert [m["content"] for m in replayed[1:]] == ["b", "Ok.", "c"]
    forgetful = ModelOnlyAgent(OPENROUTER, model="m", options=ModelOnlyOptions(history_turns=0))
    await forgetful.respond("a")
    await forgetful.respond("b")
    assert [m["content"] for m in sessions[-1].calls[-1][1]["messages"][1:]] == ["b"]


async def test_conformance_offline(monkeypatch):
    from sleeptalk.conformance.runner import run_adapter_test

    fake_http(monkeypatch, lambda *_: _Response(200, openai_chunks("Fine. ", "Thanks.")))
    report = await run_adapter_test(lambda: ModelOnlyAgent(OPENROUTER, model="m"), adapter="model", mode="offline")
    assert not report.failed, report.format_text()


# ---------------------------------------------------------------------------
# Registration, config, probe
# ---------------------------------------------------------------------------


def _auth(base_url="https://openrouter.ai/api/v1", protocol="openai-completions"):
    return {"mode": "api_key", "protocol": protocol, "base_url": base_url, "credential_ref": "credential:" + "1" * 32}


def test_profile_with_model_and_auth_validates_and_rejects_agent_only_fields():
    from sleeptalk.config import AgentConfig, SleeptalkConfig, _validate_component_choices

    good = SleeptalkConfig(agent=AgentConfig(type="model", model="anthropic/claude-opus-5", auth=_auth()))
    assert _validate_component_choices(good, env={}) == []
    for alias in ("model-only", "llm"):
        assert _validate_component_choices(SleeptalkConfig(agent=AgentConfig(type=alias, model="m", auth=_auth())), env={}) == []
    bad = SleeptalkConfig(agent=AgentConfig(type="model", model="m", auth=_auth(), pi_bin="/x/pi"))
    problems = _validate_component_choices(bad, env={})
    assert problems and "pi_bin" in problems[0]
    unknown_option = SleeptalkConfig(agent=AgentConfig(type="model", model="m", auth=_auth(), options={"url": "x"}))
    assert _validate_component_choices(unknown_option, env={})


def test_factory_requires_the_explicit_selection():
    from sleeptalk.config import AgentConfig
    from sleeptalk.registries import AGENTS

    section = AgentConfig(type="model", model="m")
    with pytest.raises(ModelOnlyError, match="explicit model credential"):
        AGENTS.get("model").factory(section, {})
    env = {"SLEEPTALK_PROVIDER_AUTH": json.dumps({"mode": "api_key", "protocol": "openai-completions", "base_url": "https://openrouter.ai/api/v1"}),
           "SLEEPTALK_PROVIDER_CREDENTIAL": "sk-or-test", "OPENAI_API_KEY": "ambient"}
    agent = AGENTS.get("model").factory(section, env)
    assert isinstance(agent, ModelOnlyAgent)
    assert agent.selection.credential == "sk-or-test"
    with pytest.raises(ModelOnlyError, match="explicit model name"):
        AGENTS.get("model").factory(AgentConfig(type="model"), env)


def test_registration_has_no_binary_and_is_ready():
    from sleeptalk.agents.readiness import probe
    from sleeptalk.registries import AGENTS

    registration = AGENTS.get("model")
    assert registration.binary_for(None) == ""
    assert probe("model").ready


async def test_probe_without_credential_needs_input_and_never_leaks(monkeypatch, tmp_path):
    from sleeptalk.config import AgentConfig
    from sleeptalk.runtime_probe import probe_runtime

    result = await probe_runtime(AgentConfig(type="model", model="m", workspace_dir=str(tmp_path)), env={"OPENAI_API_KEY": "ambient-secret"})
    assert result["adapter"] == "model"
    assert result["status"] == "needs_input"
    assert "explicit model credential" in result["next_action"]
    assert "ambient-secret" not in json.dumps(result)


async def test_probe_reports_verified_login(monkeypatch):
    from sleeptalk.config import AgentConfig

    seen = []

    async def verify(selection, model):
        seen.append((selection.base_url, model))
        return "available"

    monkeypatch.setattr("sleeptalk.agents.provider_auth.verify", verify)
    env = {"SLEEPTALK_PROVIDER_AUTH": json.dumps({"mode": "api_key", "protocol": "anthropic-messages", "base_url": "https://api.anthropic.com"}),
           "SLEEPTALK_PROVIDER_CREDENTIAL": "sk-ant-test"}
    result = await model_only.probe(AgentConfig(type="model", model="claude-opus-5"), env)
    assert result["status"] == "ready" and result["login"] == "available"
    assert seen == [("https://api.anthropic.com", "claude-opus-5")]
    missing = await model_only.probe(AgentConfig(type="model"), env)
    assert missing["status"] == "needs_input" and "model name" in missing["next_action"]


async def test_verify_openrouter_uses_the_per_key_model_listing(monkeypatch):
    from sleeptalk.agents.provider_auth import verify

    calls = []

    def responder(url, params, headers):
        calls.append((url, headers))
        if headers is None:  # the anonymous control request must be refused
            return _Response(401, b"{}")
        return _Response(200, json.dumps({"data": [{"id": "anthropic/claude-opus-5"}, {"id": "openai/gpt-5.5"}]}).encode())

    fake_http(monkeypatch, responder)
    assert await verify(OPENROUTER, "anthropic/claude-opus-5") == "available"
    assert await verify(OPENROUTER, "nope/model") == "unknown"
    assert calls[0][0] == "https://openrouter.ai/api/v1/models/user"
    assert calls[1][1] == {"Authorization": "Bearer sk-or-test"}

    fake_http(monkeypatch, lambda url, params, headers: _Response(401, b"{}"))
    assert await verify(OPENROUTER, "anthropic/claude-opus-5") == "unavailable"


# ---------------------------------------------------------------------------
# A real loopback server, Ollama-style: plain http, no auth, real sockets
# ---------------------------------------------------------------------------


async def _serve_ollama_style(model: str):
    from aiohttp import web

    seen: dict = {}

    async def models(request):
        name = request.match_info["name"]
        if name != model:
            return web.json_response({"error": {"message": "model not found"}}, status=404)
        return web.json_response({"id": name, "object": "model", "owned_by": "library"})

    async def chat(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = await request.json()
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for text in ("Local models ", "work too. ", "No key needed."):
            await response.write(sse({"choices": [{"delta": {"content": text}}]}))
        await response.write(sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}, "[DONE]"))
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/v1/models/{name}", models)
    app.router.add_post("/v1/chat/completions", chat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}/v1", seen


async def test_loopback_endpoint_verifies_without_an_auth_challenge_and_streams():
    from sleeptalk.agents.provider_auth import verify

    runner, base_url, seen = await _serve_ollama_style("llama3.2:latest")
    try:
        selection = ProviderSelection("api_key", "openai-completions", base_url, "ollama")
        assert await verify(selection, "llama3.2:latest") == "available"
        assert await verify(selection, "nope") == "unknown"
        agent = ModelOnlyAgent(selection, model="llama3.2:latest")
        assert await agent.respond("hi") == "Local models work too. No key needed."
        assert seen["auth"] == "Bearer ollama"
        assert seen["body"]["model"] == "llama3.2:latest"
        await agent.close()
    finally:
        await runner.cleanup()


def test_plain_http_is_loopback_only():
    with pytest.raises(ValueError, match="loopback"):
        ProviderSelection("api_key", "openai-completions", "http://example.com/v1", "k")
    assert ProviderSelection("api_key", "openai-completions", "http://localhost:80/v1", "k").base_url == "http://localhost/v1"


async def test_a_model_that_must_reason_is_asked_again_with_reasoning_on(monkeypatch):
    """OpenRouter refuses `reasoning: {enabled: false}` for models that can't
    skip thinking; the turn retries without it, and later turns don't ask."""
    def respond(url, body, headers):
        if "reasoning" in body:
            return _Response(400, b'{"error":{"message":"Reasoning is mandatory for this endpoint and cannot be disabled."}}')
        return _Response(200, openai_chunks("Fine. ", reasoning="hm"))

    sessions = fake_http(monkeypatch, respond)
    agent = ModelOnlyAgent(OPENROUTER, model="openai/o-mini")
    assert await agent.respond("x") == "Fine."
    assert await agent.respond("y") == "Fine."
    assert ["reasoning" in body for _url, body, _h in sessions[0].calls] == [True, False, False]


async def test_a_reply_budget_spent_on_thinking_is_named_not_silent(monkeypatch):
    """Qwen 3.6 Flash at 1024 tokens: all reasoning, finish_reason "length", no text."""
    body = sse({"choices": [{"delta": {"reasoning": "let me think"}}]},
               {"choices": [{"delta": {}, "finish_reason": "length"}]}, "[DONE]")
    fake_http(monkeypatch, lambda *_: _Response(200, body))
    agent = ModelOnlyAgent(OPENROUTER, model="qwen/qwen3.6-flash", options=ModelOnlyOptions(max_tokens=1024))
    events: list[object] = []
    with pytest.raises(ModelOnlyError, match="whole reply budget \\(1024 tokens\\) thinking"):
        [s async for s in agent.respond_streaming("x", on_event=typed_events(events.append))]
    assert any(isinstance(e, AgentError) for e in events)
    # A long answer cut at the budget still keeps what was said.
    fake_http(monkeypatch, lambda *_: _Response(200, sse({"choices": [{"delta": {"content": "Said this. And"}}]},
                                                          {"choices": [{"delta": {}, "finish_reason": "length"}]})))
    assert await ModelOnlyAgent(OPENROUTER, model="m").respond("x") == "Said this. And"
