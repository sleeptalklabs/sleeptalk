# sleeptalk-check.wav

~1s of speech ("sleeptalk check"), 16 kHz mono 16-bit PCM WAV, used by
`sleeptalk doctor`'s STT round-trip check (`sleeptalk/stt_roundtrip.py`).

Currently machine-recorded via macOS `say` (Samantha voice) as a placeholder.
Replace with a human recording when available — keep the filename and format.
The clip must always contain real speech: a tone transcribes empty and the
round-trip check false-fails.

# voice-unavailable.wav

A 0.48 s two-note falling tone (E5 then B4, soft attack and decay, about
-12 dBFS), 48 kHz stereo 16-bit PCM WAV. Played when a turn fails and the
speaking voice can't say so either (the TTS itself is down), so the caller
hears something instead of silence; the page shows the reply as text.
Generated, not recorded; any replacement should stay short, quiet and
wordless (it plays in every language).
