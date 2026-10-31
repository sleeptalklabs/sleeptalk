from __future__ import annotations

import ast
import asyncio
import importlib.util
from pathlib import Path

import pytest

from sleeptalk.adapter import SessionIdentified, adapter_capabilities, typed_events
from sleeptalk.registries import AGENTS

_EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "custom_agent.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("custom_agent_example", _EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_example_imports_only_public_sleeptalk() -> None:
    source = _EXAMPLE.read_text()
    assert "server.app" not in source
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "asyncio", "collections", "sleeptalk"}
    sleeptalk_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        and node.module.startswith("sleeptalk.")
    }
    assert sleeptalk_modules == {
        "sleeptalk.adapter",
        "sleeptalk.registries",
        "sleeptalk.streaming",
    }


@pytest.mark.asyncio
async def test_custom_agent_warmup_stream_identity_close() -> None:
    example = _load_example()
    agent = example.CustomAgent()
    await agent.warmup()
    assert agent.warmed
    assert agent.upstream_session_id == "custom-echo-1"
    caps = adapter_capabilities(agent)
    assert caps.session_identity.level.value == "supported"

    events: list[object] = []
    sentences = [
        sentence
        async for sentence in agent.respond_streaming(
            "hello there",
            on_event=typed_events(events.append),
        )
    ]
    assert sentences
    assert any("hello there" in sentence for sentence in sentences)
    identified = [event for event in events if isinstance(event, SessionIdentified)]
    assert identified
    assert identified[0].upstream_session_id == "custom-echo-1"

    await agent.close()
    assert agent.closed
    with pytest.raises(RuntimeError, match="closed"):
        async for _ in agent.respond_streaming("again"):
            pass


@pytest.mark.asyncio
async def test_custom_agent_abort_idle_and_mid_stream() -> None:
    example = _load_example()
    idle = example.CustomAgent()
    await idle.abort()

    agent = example.CustomAgent(pause_s=30.0)
    await agent.warmup()
    seen: list[str] = []

    async def _collect() -> list[str]:
        out: list[str] = []
        async for sentence in agent.respond_streaming("hello there", on_event=seen.append):
            out.append(sentence)
            await agent.abort()
        return out

    sentences = await asyncio.wait_for(_collect(), timeout=1.0)
    assert sentences == [sentences[0]]
    assert "thinking" in seen
    assert "text" in seen
    await agent.close()


@pytest.mark.asyncio
async def test_custom_agent_tool_narration_and_register() -> None:
    example = _load_example()
    example.register()
    built = AGENTS.build(example.AGENT_NAME, None, {})
    assert isinstance(built, example.CustomAgent)
    events: list[str] = []
    sentences = [
        sentence
        async for sentence in built.respond_streaming("please run a tool", on_event=events.append)
    ]
    assert sentences
    assert "tool" in events
    assert "tooldetail:running echo hello" in events
    assert "tool:bash" in events
    await built.close()
