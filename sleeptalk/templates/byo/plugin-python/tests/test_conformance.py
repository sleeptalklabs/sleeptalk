"""Run Sleeptalk's adapter conformance suite against this plugin.

    uv run --with pytest --with pytest-asyncio pytest
"""

from sleeptalk.conformance.runner import run_adapter_test

from AGENT_MODULE import Agent, Options


async def test_fixture_conformance() -> None:
    report = await run_adapter_test(lambda: Agent(Options(), {}), adapter="AGENT_NAME", mode="fixture")
    assert not report.failed, report.format_text()
