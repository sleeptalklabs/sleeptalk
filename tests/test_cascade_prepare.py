from __future__ import annotations

import pytest

from sleeptalk.routes.cascade import CascadeRoute


class _FakeAgent:
    name = "FakeAgent"

    def __init__(self) -> None:
        self.warmup_calls = 0

    async def respond_streaming(self, text: str, *, on_event=None):
        del text, on_event
        if False:
            yield ""

    async def abort(self) -> None:
        return None

    async def warmup(self) -> None:
        self.warmup_calls += 1
        raise RuntimeError("Pi response exceeded maximum time.")

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_a_failed_warmup_does_not_stop_cascade_preparing() -> None:
    """Warmup is "Say OK." It primes the agent. It does not make the route work."""
    agent = _FakeAgent()
    route = CascadeRoute(agent=agent, tts=None)
    await route.prepare(session=None)
    await route.prepare(session=None)

    assert agent.warmup_calls == 1
    assert route._prepared is True
    assert route._policy_runtime is not None
    await route.close()


@pytest.mark.asyncio
async def test_provider_preparation_runs_while_agent_is_still_cold() -> None:
    import asyncio
    from types import SimpleNamespace

    agent_started = asyncio.Event()
    providers_ready = asyncio.Event()
    calls = set()

    class ColdAgent(_FakeAgent):
        async def warmup(self):
            agent_started.set()
            await providers_ready.wait()
            raise TimeoutError("cold agent")

    class Speech:
        def __init__(self, name):
            self.name = name

        async def warmup(self):
            await agent_started.wait()
            calls.add(self.name)
            if len(calls) == 2:
                providers_ready.set()

    route = CascadeRoute(agent=ColdAgent(), stt=Speech("stt"), tts=Speech("tts"))
    await asyncio.wait_for(route.prepare(SimpleNamespace(config=None)), 1)
    assert calls == {"stt", "tts"}
    assert route._prepared
    await route.close()
