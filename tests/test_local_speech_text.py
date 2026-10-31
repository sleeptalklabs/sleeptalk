"""Local engines get spoken prose while the caller retains its written text."""
from types import SimpleNamespace

import pytest

from sleeptalk.providers import ProviderSpec
from sleeptalk.speech.local_text import prepare_local_speech
from sleeptalk.speech.local_tts import LocalTTS


@pytest.mark.parametrize("written,spoken", [
    ("It costs ~$20 per month.", "It costs about 20 dollars per month."),
    ("The total is $12.50, not $1.01.", "The total is 12 dollars and 50 cents, not 1 dollar and 1 cent."),
    ("$0.05 or $0.00", "5 cents or 0 dollars"),
    ("$1,250.99 and $2k", "1250 dollars and 99 cents and 2 thousand dollars"),
    ("£1.01 and €2.50", "1 pound and 1 penny and 2 euros and 50 cents"),
    ("The adjustment is $-2.50.", "The adjustment is minus 2 dollars and 50 cents."),
    ("~ 30% faster & ≈5 times smaller.", "about 30 percent faster and about 5 times smaller."),
    ("Read [the setup guide](https://example.com/docs/setup?token=secret).", "Read the setup guide."),
    ('See [the guide](https://example.com/a(b) "Title").', "See the guide."),
    ("See https://www.example.com/a/b?token=secret&x=$5#install.", "See example dot com."),
    ("Go to <https://example.org/long/path> or www.example.net/start.", "Go to example dot org or example dot net."),
    ("Open (https://example.org/a?q=1).", "Open example dot org."),
    ("Use **Kokoro**, not ~~the old voice~~.", "Use Kokoro, not the old voice."),
    ("Open `~/projects/sleeptalk`.", "Open your home folder, projects, sleeptalk."),
    ("Use $HOME and main.py with version 1.2.3.", "Use HOME and main.py with version 1.2.3."),
    ("The symbol is $ or ~.", "The symbol is or."),
    ("**This works. The ending is in another chunk", "This works. The ending is in another chunk"),
    ("now.**", "now."),
    ("## Ready ✅ → [done] | {great} # @ ^ = + \\ /", "Ready done great"),
    ("*** __ ~~ ``` # @ $ % & + = → ✅", ""),
    ("It is not ready. Do not deploy.", "It is not ready. Do not deploy."),
])
def test_spoken_forms_preserve_meaning_without_reading_url_syntax(written, spoken):
    assert prepare_local_speech(written) == spoken
    assert prepare_local_speech(spoken) == spoken


@pytest.mark.parametrize("written,spoken", [
    ("See https://example.com/long/path?token=secret. It costs $12.50.", "See example dot com. It costs 12 dollars and 50 cents."),
    ("Read [the setup guide](https://example.com/long/path?token=secret).", "Read the setup guide."),
    ("Read [a very long, detailed setup guide with extra information about getting started](https://example.com/a(b)?q=secret).", "Read a very long, detailed setup guide with extra information about getting started."),
    ("**The first sentence is ready. The second sentence is ready too.**", "The first sentence is ready. The second sentence is ready too."),
])
def test_streaming_never_speaks_partial_links_or_prices(written, spoken):
    from sleeptalk.streaming import LOCAL_PHRASE_STREAMING, SentenceStreamer

    token = LOCAL_PHRASE_STREAMING.set(True)
    try:
        for size in range(1, len(written) + 1):
            streamer = SentenceStreamer()
            chunks = []
            for index in range(0, len(written), size):
                chunks.extend(streamer.feed(written[index:index + size]))
            chunks.append(streamer.flush())
            assert " ".join(filter(None, map(prepare_local_speech, chunks))) == spoken, (size, chunks)
    finally:
        LOCAL_PHRASE_STREAMING.reset(token)


@pytest.mark.parametrize("model,voice", [("kokoro-en", "af_heart"), ("piper-lessac-medium", "lessac"), ("pocket-tts-en", "pocket-bria")])
@pytest.mark.parametrize("streaming", [True, False])
async def test_all_local_audio_paths_use_speech_forms(monkeypatch, model, voice, streaming):
    import numpy as np

    received = []

    def generate(text, **kwargs):
        received.append(text)
        return SimpleNamespace(samples=np.ones(960, dtype=np.float32) * 0.1, sample_rate=48000)

    async def pocket(engine, text, *args):
        received.append(text)
        yield b"\x01\x00" * 960

    monkeypatch.setattr("sleeptalk.speech.pocket_tts.stream_pocket", pocket)
    tts = LocalTTS(ProviderSpec(provider="local", model=model, voice_id=voice, language="en-US"))
    tts._tts = SimpleNamespace(generate=generate)
    written = "**Pay ~$20.** See [the guide](https://example.com/private?q=secret). ✅"
    try:
        if streaming:
            audio = b"".join([chunk async for chunk in tts.stream_pcm(written)])
        else:
            audio = await tts.synthesize_wav_bytes(written)
        assert audio
        assert " ".join(received) == "Pay about 20 dollars. See the guide."
        assert written == "**Pay ~$20.** See [the guide](https://example.com/private?q=secret). ✅"
    finally:
        await tts.close()
