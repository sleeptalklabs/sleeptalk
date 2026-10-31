from pathlib import Path

import pytest

from sleeptalk.providers import collect_tts_audio
from sleeptalk.media_audio import pcm_to_wav
from sleeptalk.speech import temp_audio


@pytest.mark.asyncio
async def test_tts_temp_dir_is_empty_after_in_memory_session_ends(tmp_path, monkeypatch):
    root = tmp_path / "sleeptalk-tts"
    process_dir = root / "test-process"
    monkeypatch.setattr(temp_audio, "TTS_TEMP_ROOT", root)
    monkeypatch.setattr(temp_audio, "_PROCESS_DIR", process_dir)
    temp_audio.scavenge_tts_temp_dir()

    class MemoryTTS:
        async def synthesize(self, _text):
            yield b"\x01\x00" * 480

    assert await collect_tts_audio(MemoryTTS(), "private response") == pcm_to_wav(b"\x01\x00" * 480)
    assert list(root.rglob("*.wav")) == []


@pytest.mark.asyncio
async def test_file_only_tts_fallback_is_deleted_when_consumed(tmp_path, monkeypatch):
    root = tmp_path / "sleeptalk-tts"
    process_dir = root / "test-process"
    monkeypatch.setattr(temp_audio, "TTS_TEMP_ROOT", root)
    monkeypatch.setattr(temp_audio, "_PROCESS_DIR", process_dir)
    temp_audio.scavenge_tts_temp_dir()

    class FileOnlyTTS:
        async def synthesize_wav(self, _text) -> Path:
            return temp_audio.write_tts_temp_file(b"RIFF-file-WAVE")

    assert await collect_tts_audio(FileOnlyTTS(), "private response") == b"RIFF-file-WAVE"
    assert list(root.rglob("*.wav")) == []


def test_a_shared_tts_directory_someone_else_can_write_is_never_used(tmp_path, monkeypatch):
    root = tmp_path / "sleeptalk-tts-shared"
    root.mkdir(mode=0o777)
    root.chmod(0o777)
    monkeypatch.setattr(temp_audio, "TTS_TEMP_ROOT", root)
    monkeypatch.setattr(temp_audio, "_PROCESS_DIR", root / "test-process")
    temp_audio.scavenge_tts_temp_dir()
    try:
        written = temp_audio.write_tts_temp_file(b"RIFF-private-WAVE")
        assert root not in written.parents
        assert temp_audio._is_private_dir(temp_audio.TTS_TEMP_ROOT)
        assert oct(written.stat().st_mode & 0o777) == "0o600"
    finally:
        temp_audio.clear_tts_temp_files()
