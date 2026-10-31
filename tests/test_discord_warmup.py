from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("discord")

from sleeptalk.agents.pi_rpc import PiRPCError
from sleeptalk.discord.bot import _warmup


class _Kernel:
    async def prepare(self) -> None:
        raise PiRPCError("Pi response exceeded maximum time.")


class _Speech:
    def __init__(self) -> None:
        self.warmed = 0

    async def warmup(self) -> None:
        self.warmed += 1


class _Agent:
    def __init__(self) -> None:
        self.aborted = 0

    async def abort(self) -> None:
        self.aborted += 1


@pytest.mark.asyncio
async def test_discord_kernel_owns_speech_preparation_and_skips_legacy_clips() -> None:
    stt = _Speech()
    tts = _Speech()
    agent = _Agent()
    bot = SimpleNamespace(
        kernel=_Kernel(),
        agent=agent,
        stt=stt,
        tts=tts,
        policy=SimpleNamespace(
            preamble_pools={},
            reassure_tool_pools={},
            narration_phrases={},
        ),
        _line_clips={},
        preamble_clips={},
        narration_clips={},
    )

    await _warmup(bot)

    assert agent.aborted == 1
    assert stt.warmed == 0
    assert tts.warmed == 0
