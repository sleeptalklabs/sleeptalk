"""Cascade route runs the shared contract suite (r2-route-contract-tests).

The same fixtures will be applied to the native adapter
(r2-native-contract) without copy/paste — this module proves the suite
against the first production route.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.runtime.route_contract import RouteHarness, StallAgent, route_contract_case
from sleeptalk.routes.cascade import CascadeRoute


class ContractAgent:
    """Deterministic streaming agent sized for the contract cases."""

    name = "ContractAgent"

    def __init__(self, sentences: list[str], *, delay: float = 0.001) -> None:
        self._sentences = sentences
        self._delay = delay
        self.aborted = 0

    async def respond_streaming(self, text: str, *, on_event=None):
        for sentence in self._sentences:
            await asyncio.sleep(self._delay)
            yield sentence

    async def abort(self) -> None:
        self.aborted += 1

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _make_route() -> CascadeRoute:
    return CascadeRoute(agent=StallAgent(["First.", "Second.", "Third."]), tts=None)


async def make_harness() -> RouteHarness:
    """Fresh prepared harness per invocation — cases never share state."""
    route = _make_route()
    harness = RouteHarness(route=route)
    await route.prepare(None)
    return harness


class _Factory:
    """Sync callable adapter: creates and prepares a fresh harness."""

    async def __call__(self) -> RouteHarness:
        return await make_harness()

    async def unprepared_route(self):
        """A fresh, unprepared harness route for illegal-transition cases."""
        from sleeptalk.routes.cascade import CascadeRoute
        from sleeptalk.transports.terminal_audio import AudioDeviceError  # noqa: F401
        from tests.runtime.route_contract import StallAgent

        return CascadeRoute(agent=StallAgent(["Unprepared."]), tts=None)

    @property
    def unprepared(self) -> RouteHarness:
        return RouteHarness(route=_make_route())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", route_contract_case.cases, ids=lambda c: c.name
)
async def test_cascade_route_contract(case) -> None:
    await route_contract_case.run_case(case, _Factory())
