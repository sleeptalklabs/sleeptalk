"""Narration style: what a tool call sounds like."""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from sleeptalk.policy import NARRATION_STYLES, VoicePolicy, narration_style_or_default
from sleeptalk.sounds import narration_clip_path, sounds_dir
from sleeptalk.voice_tool_narration import play_tool_keyboard


class _Ctx:
    session_id = "s"
    turn_id = 1

    def __init__(self) -> None:
        self.events: list[object] = []

    def emit(self, event: object) -> None:
        self.events.append(event)


class _Gate:
    def __init__(self) -> None:
        self.claims: list[str] = []

    async def claim(self, pool: str) -> None:
        self.claims.append(pool)


def test_default_is_silence() -> None:
    assert VoicePolicy().narration_style == "silence"


def test_unknown_styles_fall_back_to_silence() -> None:
    """A typo gets the default, not the louder keyboard or narrated styles."""
    for value in ("", None, "quiet", "KEYBOARDS", 7, object()):
        assert narration_style_or_default(value) == "silence"


def test_known_styles_round_trip_case_insensitively() -> None:
    for style in NARRATION_STYLES:
        assert narration_style_or_default(style.upper()) == style


def test_only_keyboard_resolves_to_a_clip() -> None:
    assert narration_clip_path("narrated") is None
    assert narration_clip_path("silence") is None


@pytest.mark.skipif(sounds_dir() is None, reason="no sounds/ directory in this tree")
def test_keyboard_clip_is_a_short_playable_wav() -> None:
    """play_file decodes WAV only, and a per-tool clip must not outlast the tool."""
    path = narration_clip_path("keyboard")
    assert path is not None and path.suffix == ".wav"
    with wave.open(str(path), "rb") as clip:
        seconds = clip.getnframes() / clip.getframerate()
        assert clip.getframerate() == 48_000
        assert clip.getnchannels() == 2
        assert 0.5 <= seconds <= 4.0, seconds


@pytest.mark.asyncio
async def test_keyboard_without_clip_playback_stays_silent() -> None:
    """A surface that cannot play files degrades to silence, never to speech."""
    ctx = _Ctx()
    gate = _Gate()
    await play_tool_keyboard(gate, ctx=ctx, tool="bash", play_clip=None)
    assert ctx.events == []
    assert gate.claims == []


@pytest.mark.asyncio
async def test_keyboard_emits_progress_only_when_audio_played() -> None:
    ctx = _Ctx()
    gate = _Gate()

    async def refused(_tool: str, _ctx: object) -> bool:
        return False

    await play_tool_keyboard(gate, ctx=ctx, tool="bash", play_clip=refused)
    assert ctx.events == []

    async def played(_tool: str, _ctx: object) -> bool:
        return True

    await play_tool_keyboard(gate, ctx=ctx, tool="bash", play_clip=played)
    assert len(ctx.events) == 1


def test_clip_falls_back_to_the_packaged_copy_when_the_checkout_file_is_missing(monkeypatch, tmp_path: Path) -> None:
    import sleeptalk.sounds as sounds
    monkeypatch.setattr("sleeptalk.sounds.sounds_dir", lambda: tmp_path)
    assert narration_clip_path("keyboard") == Path(sounds.__file__).with_name("assets") / "keyboard.wav"


def test_clip_path_is_none_when_no_copy_exists(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("sleeptalk.sounds.sounds_dir", lambda: tmp_path)
    monkeypatch.setattr("sleeptalk.sounds.NARRATION_CLIPS", {"keyboard": "missing-clip.wav"})
    assert narration_clip_path("keyboard") is None


@pytest.mark.asyncio
async def test_clip_call_passes_the_turn_only_to_surfaces_that_take_one(tmp_path: Path) -> None:
    """Discord queues the clip inside a turn; terminal and web take just the path."""
    from sleeptalk.sounds import play_clip_on

    seen: list[tuple] = []

    async def device_backed(path: Path) -> None:
        seen.append(("device", path))

    async def per_turn(path: Path, *, turn_id: int) -> bool:
        seen.append(("turn", path, turn_id))
        return turn_id == 3

    clip = tmp_path / "keyboard.wav"
    assert await play_clip_on(device_backed, clip, 3) is True
    assert await play_clip_on(per_turn, clip, 3) is True
    assert await play_clip_on(per_turn, clip, 4) is False
    assert seen == [("device", clip), ("turn", clip, 3), ("turn", clip, 4)]
