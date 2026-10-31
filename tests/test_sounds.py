"""Narration clips ship inside the package; a checkout's sounds/ overrides them."""
from __future__ import annotations

from pathlib import Path

from sleeptalk import sounds


def test_keyboard_clip_resolves_from_the_package_outside_a_checkout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sounds.sounds_dir.cache_clear()
    clip = sounds.narration_clip_path("keyboard")
    assert clip is not None and clip.is_file()
    assert clip == Path(sounds.__file__).with_name("assets") / "keyboard.wav"
    assert sounds.narration_clip_path("silence") is None
    assert sounds.narration_clip_path("narrated") is None


def test_a_checkout_sounds_dir_overrides_the_packaged_clip(tmp_path, monkeypatch):
    (tmp_path / "sounds").mkdir()
    (tmp_path / "sounds" / "keyboard.wav").write_bytes(b"RIFF")
    monkeypatch.chdir(tmp_path)
    sounds.sounds_dir.cache_clear()
    assert sounds.narration_clip_path("keyboard") == tmp_path / "sounds" / "keyboard.wav"
    sounds.sounds_dir.cache_clear()


def test_new_profiles_default_to_normal_speed():
    from sleeptalk.wizard import WizardAnswers
    assert WizardAnswers().tts_speed == 1.0


def test_working_slices_cut_the_clip_into_short_playable_pieces():
    import wave

    from sleeptalk.sounds import WORKING_SLICE_SECONDS, narration_clip_path, working_slices

    pieces = working_slices("keyboard")
    assert len(pieces) > 1
    assert all(0 < seconds <= WORKING_SLICE_SECONDS + 1e-6 for _path, seconds in pieces)
    with wave.open(str(narration_clip_path("keyboard")), "rb") as source:
        whole = source.getnframes() / source.getframerate()
    assert abs(sum(seconds for _path, seconds in pieces) - whole) < 0.01
    with wave.open(str(pieces[0][0]), "rb") as first:
        assert first.getsampwidth() == 2 and first.getnframes() > 0
    assert working_slices("narrated") == ()
