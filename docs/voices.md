# Voices

Every speaking provider has one golden default and a couple of curated
picks. You hear them during setup, you can choose one on the private key
page, and you can change your mind in the middle of a call without leaving
the page.

## The curated list

| Provider | Default | Also | Where to find more |
| --- | --- | --- | --- |
| Cartesia | Skylar | Daniel, Gemma | [play.cartesia.ai/voices](https://play.cartesia.ai/voices) |
| ElevenLabs | Brittney | Mark | [Voice library](https://elevenlabs.io/app/voice-library) |
| Deepgram | Hannah (`flux-hannah-en`) | Kit (`flux-kit-en`) | [TTS models](https://developers.deepgram.com/docs/tts-models) |
| OpenRouter | Eve (Grok Voice) | Ara, Hannah and Kit (Flux, free), Thalia (Aura-2), Sky (Kokoro) | [Speech models](https://openrouter.ai/models?output_modalities=speech) |
| Venice | Eve (Grok Voice) | Ara, Tara (Orpheus), Sky (Kokoro) | [Speech](https://docs.venice.ai/api-reference/endpoint/audio/speech) |
| Together | Skylar (Cartesia Sonic) | Daniel, Tara (Orpheus), Sky (Kokoro) | [Text-to-speech](https://docs.together.ai/docs/text-to-speech) |
| Groq | Troy (Orpheus) | Hannah | [Text-to-speech](https://console.groq.com/docs/text-to-speech) |

Deepgram has no separate voice ID: the model name is the voice. On
OpenRouter, Venice, and Together the voice name picks the model too, so any
voice from any of their speech models works as a custom ID. Kokoro is
offered everywhere as the budget pick and is never a default.

Any other voice from the provider's own library works too. Paste its ID
where a voice is asked for, and Sleeptalk proves it by synthesizing one
short line before it is accepted. The list lives in
`sleeptalk/voice_catalog.py`.

## Choosing one

- **The setup wizard** plays the curated voices for your provider and lets
  you pick, or paste an ID.
- **An agent's key request** (on the settings page, on the host or a phone)
  shows the same picks under the service you chose. An
  agent can pin a voice for you with `--tts-voice-id` on
  `sleeptalk host input link`; the page then validates exactly that one.
- **In the profile**, the choice is `[tts] voice_id`. Empty means the
  English default for your provider, or a language-matched voice for other
  languages.

## Cloning a voice in settings

Select **ElevenLabs** or **Cartesia** under **Speech service**, save the
service's key, and open its voice picker (also available on the **Voice**
page). **Clone a voice** accepts a name, recording language, and an audio
file after you confirm permission to upload and clone the recording.

- **ElevenLabs:** instant cloning, with 10–120 seconds accepted by this UI;
  aim for 1–2 minutes of clean speech from one speaker.
- **Cartesia:** 10–60 seconds; Sonic 3.6 and newer can use up to 60 seconds,
  while older models use the first 10 seconds.

The browser converts the recording to mono 48 kHz WAV without trimming it.
The host uploads it with the selected profile's saved key and does not
write the reference audio to disk. Clones remain in the provider account;
Cartesia clones are created private. The provider plan and API key must
allow cloning. This does not install a local model.

Use **My cloned voices** to preview and select a clone. Creating a voice
does not select it automatically. **Refresh my voices** reloads the
account's voices, including clones created in the provider dashboard.
ElevenLabs clones requiring verification cannot be previewed or selected
from this list until verification is completed in ElevenLabs and the list
is refreshed. Provider acceptance is not legal clearance. Professional
ElevenLabs cloning remains a provider-dashboard workflow.

If a creation request times out, refresh the list or check the provider
dashboard before uploading again: the provider may already have created
the voice. Sleeptalk does not automatically retry cloning requests.

Kyutai Pocket TTS has a separate, local-only upload flow; see
[local speech](local-speech.md).

## Changing it during a call

On the browser call page, open the settings wheel and tap **Voices**. The
list holds the voices of the service speaking in this call, in the call's
language; tap one and the next reply uses it. The host tries the voice on a
sample line with your saved key first; if the provider refuses it, the
current voice stays and the page says why (rejected key, no permission, out
of credit, unavailable voice). Local voices switch the same way (a Kokoro
voice needs the Kokoro model downloaded).

A voice that works is saved to the profile at once, so the next call starts
with it. Nothing else in the profile is touched.

For any other voice, **More voices** opens the settings page on the computer
running your agent, where you can paste a voice ID or browse the provider's
library.

What the wheel cannot do:

- **Change provider.** A different provider needs its own key, and keys
  never enter a call. Use `sleeptalk settings` or the key page for that.

## The rest of the wheel

The same menu holds playback speed (1.0 is normal, the default for new
profiles), tool narration, and reply length. Each applies to the live call
immediately and lasts for that call; set the defaults for later calls on the
settings page (`sleeptalk settings`). A voice you switch to mid-call
is saved to the profile.
