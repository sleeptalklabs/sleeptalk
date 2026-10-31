import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from sleeptalk import wizard
from sleeptalk.registries import TTS_PROVIDERS


async def test_local_download_confirmation_counts_only_missing_models(monkeypatch):
    from sleeptalk.speech import local_models as lm

    monkeypatch.setattr(lm, "is_ready", lambda m, root=None: m.id == "kokoro-en")
    select = Mock(return_value=SimpleNamespace(ask_async=AsyncMock(return_value=wizard._BACK)))
    monkeypatch.setattr("questionary.select", select)
    console = Mock()
    assert not await wizard._confirm_local_download([lm.get_model("kokoro-en"), lm.get_model("piper-lessac-medium")], console)
    assert select.call_args.kwargs["choices"][0].title == "Download and use (~21 MB)"
    select.reset_mock()
    assert await wizard._confirm_local_download([lm.get_model("kokoro-en")], console)
    select.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["proof", "audition"])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled", "no_close"])
async def test_temporary_tts_is_closed_after_synthesis(monkeypatch, tmp_path, mode, outcome):
    sample = tmp_path / "sample.wav"
    error = {
        "failure": RuntimeError("provider failed"),
        "cancelled": asyncio.CancelledError(),
    }.get(outcome)
    tts = SimpleNamespace(synthesize_wav=AsyncMock(return_value=sample, side_effect=error))
    if outcome != "no_close":
        tts.close = AsyncMock()

    answers = wizard.WizardAnswers(tts_provider="cartesia", tts_voice_id="voice", tts_key="key")
    if mode == "proof":
        monkeypatch.setattr(TTS_PROVIDERS, "build", lambda *_args: tts)
        operation = wizard._prove_selected_voice(answers)
    else:
        import questionary
        from sleeptalk import voice_list, voice_sample
        from sleeptalk.speech import cartesia

        monkeypatch.setattr(cartesia, "CartesiaTTS", lambda *_args, **_kwargs: tts)
        monkeypatch.setattr(voice_list, "list_voices", AsyncMock(return_value=[]))
        voice = SimpleNamespace(id="voice", name="Voice", description="", language="en")
        monkeypatch.setattr(voice_list, "curate", lambda *_args: ([voice], ""))
        select = SimpleNamespace(ask_async=AsyncMock(side_effect=["voice", wizard._BACK]))
        confirm = SimpleNamespace(ask_async=AsyncMock(return_value=True))
        monkeypatch.setattr(questionary, "select", lambda *_args, **_kwargs: select)
        monkeypatch.setattr(questionary, "confirm", lambda *_args, **_kwargs: confirm)

        async def play(path):
            assert path == sample
            if outcome != "no_close":
                tts.close.assert_awaited_once()
            return True

        monkeypatch.setattr(voice_sample, "play_file", play)
        operation = wizard._audition_voice(answers, Mock())

    if outcome == "cancelled" or (outcome == "failure" and mode == "proof"):
        with pytest.raises(type(error)):
            await operation
    else:
        result = await operation
        expected = sample if mode == "proof" else wizard._BACK if outcome == "failure" else "voice"
        assert result == expected
    if outcome != "no_close":
        tts.close.assert_awaited_once()
