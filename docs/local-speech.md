# Local speech: no key, runs on this machine

Pick **Local** as the speech provider and Sleeptalk listens and talks with
models on your own computer. Speech processing needs no account or per-minute
bill; committed text still goes to your selected agent. With "Just a model" pointed at Ollama, the
whole call is offline.

English only. Measured on an Apple Silicon Mac; see the numbers below before
expecting cloud-grade latency.

## What you get

| Role | Model | Size | License | Measured |
| --- | --- | --- | --- | --- |
| Listening, default | Moonshine Small Streaming (English) | 143 MB | MIT | live, revisable partial text while you speak |
| Listening, option | Moonshine tiny (English) | 108 MB | MIT | about 15 ms to decode a one-second utterance |
| Voice, default | Kokoro v1.0 82M, 28 English voices (Heart first) | 350 MB | Apache-2.0 | about 480 ms for a short sentence |
| Voice, option | Kyutai Pocket TTS, Bria and Loona | 98 MB | CC-BY-4.0 in ONNX bundle; see below | timings below predate the reference-encoder fix |
| Voice, option | Piper Lessac medium | 21 MB | MIT | about 180 ms for a short sentence |

Moonshine Small Streaming processes audio continuously, so browser text appears
while you speak. The final transcript commits only after the turn ends. Existing
profiles that explicitly select Moonshine Tiny keep that batch recognizer.

Local browser calls automatically use early Smart Turn scoring, faster barge-in,
and shorter opening phrases. There is no experimental flag. The agent can hand
the first phrase to TTS at roughly 65 characters, then longer fragments around
140 characters, with sentence and clause boundaries preferred. The next phrase
is synthesized while the current one plays. See
[local voice responsiveness](local-voice-responsiveness.md) for timing and scope.

Piper and Kokoro still synthesize each phrase as a batch; this is phrase-level
streaming, not continuous audio generation within a phrase. Splitting can
change phrasing and pauses, so voice quality still needs a listening check.
Interrupting discards pending audio and stops further phrases. Native inference
may finish its current phrase in the background; the next request waits for
that computation rather than running two synthesis calls at once.

## Set it up

Run `sleeptalk` and choose **Local** at "Speech provider (listening and
speaking)". The step does three things, and asks before each install:

1. Installs the local runtimes if missing: `sleeptalk[local,local-streaming]`,
   including sherpa-onnx for TTS and `moonshine-voice==0.1.5` for streaming STT.
2. Downloads the models it needs into the cache, with a progress line. Each
   download is checked against a pinned SHA-256 before it is used.
3. Proves a turn: transcribes the bundled test clip and synthesizes the
   selected voice.

No key prompt appears. The profile ends up as:

```toml
[stt]
provider = "local"
model = "moonshine-small-streaming-en"

[tts]
provider = "local"
model = "kokoro-en"
voice_id = "af_heart"
```

### Other voices

Local voices use speech-friendly English before synthesis, without an extra
model request: `~$20` becomes “about 20 dollars,” `$12.50` becomes “12 dollars
and 50 cents,” Markdown links use their label, and bare HTTP/HTTPS or `www`
URLs use just the domain. Paths and query strings in those URLs are not read
aloud. Formatting markers are removed and `~/` is spoken as “your home
folder.” This applies to Kokoro, Piper, and Pocket TTS; the cleanup happens
at the audio boundary and does not rewrite the displayed reply. It is a
deterministic formatter, not a semantic summarizer of code or equations.

Kokoro has 28 English voices (`af_heart`, `af_bella`, `bm_george`, and so
on); British voices (`bf_`, `bm_`) use Kokoro's British pronunciations.
Piper's Lessac (`lessac`) is the smallest download (21 MB) and the quickest,
with a plainer voice. Pick either in `sleeptalk settings` under the voice
step; choosing a voice pulls its model on first use and switches the model
for you, so you never have to set `model` by hand. Profiles made before
Kokoro became the default keep Lessac.

The `kokoro-en` profile ID now uses the full-precision v1.0 bundle. The adapter
offers its 28 English voices; other languages are not enabled by this upgrade.
Existing named voices such as `af_sarah` keep their names with updated speaker
mappings. Numeric Kokoro voice IDs are rejected because their meanings changed
between versions. The old `af` blend (Bella and Sarah mixed) speaks as Bella.

### Try Kyutai Pocket TTS

Open **Settings → Speech → Local**, choose **Kyutai Pocket TTS** as the speaking
model, then **Pocket Bria** or **Pocket Loona**. Download the model if prompted
and save these choices. Keep Moonshine Small Streaming for listening. Kokoro
remains the default and can be selected again in the same picker.

```toml
[tts]
provider = "local"
model = "pocket-tts-en"
voice_id = "pocket-bria"       # or pocket-loona
```

This uses the pinned sherpa-onnx INT8 export (2026-01-26), on CPU with two threads
and five generation steps. It works with the existing `local` runtime; no
PyTorch service or speech API is needed. Bria and Loona are the shipped reference
voices. The setup download verifies SHA-256 before installation.

When **Kyutai Pocket TTS** is selected, **Create a Kyutai voice** appears below
the voice picker (including in new-profile setup). Name your voice and upload a
clear recording of one speaker: ideally about 10 seconds, between 3 and 30 seconds,
up to 20 MB. WAV, MP3, and M4A work when supported by your browser. The browser
converts the recording to mono 24 kHz WAV before sending it through the authenticated
settings connection. Silent, malformed, and out-of-range recordings are rejected.
The full accepted recording is used for voice conditioning; Sleeptalk overrides
the ONNX engine's default 10-second reference limit for uploaded voices.
On first use it also prepares `encoder-dynamic-v1.onnx` beside the downloaded
model. This fixes the pinned export's input padding, which otherwise crops every
reference to 1.04 seconds. The original downloaded file stays unchanged. Preparing
the derived encoder needs the `onnx` package included in `sleeptalk[local]`.
Cloning is an approximation, and a successful upload does not guarantee a close
speaker match. Preview it before using it in a call.

**Save voice** adds it to your library; use its play button to hear generated speech,
then **Use voice** and save the speech choices. Preview requires the local runtime
and Pocket model to be installed. Uploading does not change the active profile.
These controls and custom voices are exclusive to Pocket; Kokoro and Piper keep
their existing voice pickers.

Recordings stay on the Sleeptalk host in `$SLEEPTALK_HOME/voices/pocket`, or
`~/.sleeptalk/voices/pocket` when that variable is unset. Include that directory
in VPS backups or the container state volume. Profiles store an opaque voice ID,
not an audio path. No transcript, training run, or external speech API is needed.

Audio is delivered from native decoder callbacks through a bounded 160 ms queue
and a continuous resampler to the app's 48 kHz PCM stream. Interruptions discard
queued audio and tell the native callback to stop. An in-flight native compute
step can still finish; subsequent synthesis waits for it instead of overlapping.
This export generates a segment's latents before decoding its audio chunks, so
it is not the native Pocket Python runtime's token-by-token streaming path.
Existing local browser phrase scheduling keeps agent text segments short.

The following historical measurements predate the reference-encoder fix. Longer
references now require more conditioning work, so these are not measurements of
the corrected runtime.

On this M2 Pro, a short first synthesis took about 980 ms to first PCM including
model load. A subsequent short sentence started at 347 ms and finished synthesis
at 527 ms for 2.56 seconds of audio. This is a small local synthesis check, not a
microphone test or an end-of-speech-to-answer measurement.

A follow-up comparison used identical text, both models warm, alternating model
order, and three repeats per text. First-PCM medians on this Mac were:

| Input | Pocket Bria | Kokoro Heart |
| --- | ---: | ---: |
| Short reply (27 characters) | 235 ms | 317 ms |
| Medium reply (65 characters) | 350 ms | 530 ms |
| Longer passage (133 characters) | 703 ms | 680 ms |

Pocket saved about 80–180 ms on these shorter inputs, but was slightly slower
on the longer passage. These timings exclude endpointing and agent generation.
Native speaking speed is fixed at 1x in this export; the speed control does not
speed up Pocket.

The [pinned model archive](https://github.com/k2-fsa/sherpa-onnx/releases/tag/tts-models)
ships a CC-BY-4.0 license file, while its README additionally says non-commercial.
That bundle's metadata is distinct from the upstream Pocket TTS repository's
license. Its README attributes the reference clips to Kyutai's
[delayed-streams-modeling audio](https://github.com/kyutai-labs/delayed-streams-modeling/tree/main/audio).
See the [runtime documentation](https://k2-fsa.github.io/sherpa/onnx/tts/pocket.html).

### Answers file

```toml
speech_provider = "local"     # no speech_key
```

Headless setup downloads the models before validating, and stops with the
install hint if the runtime extra is not present. It never installs packages
on its own.

### Where the models live

`~/.cache/sleeptalk/models` by default, or `$XDG_CACHE_HOME/sleeptalk/models`,
or wherever `SLEEPTALK_MODELS_DIR` points. Delete a model's directory to make
setup fetch it again. Nothing is bundled with the package and nothing goes
into the repository.

Kokoro v1.0 uses the `kokoro-en-v1_0` cache folder, separate from the old
`kokoro-en` folder. Setup downloads the new bundle and verifies its pinned
checksum; an old ready marker cannot satisfy the new version. Existing v0.19
files are left intact. After updating Sleeptalk, choose Local speech in settings
to fetch v1.0 before starting a call that uses Kokoro.

## Limits

- **English only.** The current local adapters expose English; the wizard
  says so if the profile language is anything else.
- **Streaming runtime support.** The pinned Moonshine runtime has Apple silicon
  wheels for macOS 15+ and Linux x86-64/ARM64 wheels. Intel Macs can select
  Moonshine Tiny, which uses sherpa-onnx. VPS performance has not been measured.
- **CPU only.** No GPU is used; thread counts are tuned per model.
- **Mixing works.** Local listening with a cloud voice, or the reverse, is
  fine through "Mix providers".

## Options

To keep the batch recognizer and customize its thread count:

```toml
[stt]
provider = "local"
model = "moonshine-tiny-en"
[stt.options]
num_threads = 4

[tts]
provider = "local"
voice_id = "af_sarah"        # implies model = "kokoro-en"
speed = 1.25
[tts.options]
num_threads = 8
```
