# OpenAI Realtime

For GPT-Live-1 continuous browser calls, see the separate
[GPT Live browser preview](gpt-live.md). The behavior below describes
the existing GPT Realtime route.

Choose **OpenAI Realtime** as the speech service in `sleeptalk settings`
(terminal), on the settings page (`sleeptalk settings`), or on the
phone settings page an agent sends with `sleeptalk host settings-link`. It
needs one OpenAI API key with billing set up.

Realtime hears you and speaks in one step, in one of OpenAI's own voices
(Marin by default; Cedar and eight earlier voices). Anything it can't know
from the conversation, such as files, code, the web, or your history, it
hands to your agent, then tells you what the agent found. In the code this
is the `hybrid` route and the hand-off is its `ask_specialist` tool.

## What to expect

- It starts answering sooner than the speech services that listen one turn
  at a time (OpenRouter, Venice, Groq, Together), because it doesn't wait for
  a transcript first (see [Audio first](#audio-first)).
- It costs more: OpenAI bills Realtime per minute of audio in and out.
- Realtime answers general knowledge, definitions, simple math and everyday
  explanations itself, right away, including "explain it, don't search" and
  "I'd like to know about…". It hands the agent your own things
  (projects, files, code, tasks, accounts), live information (news,
  weather, prices, anything "today"), and work that needs tools. It can
  still answer something your agent should have looked up; ask it to check
  with the agent and it will.
- While the agent works, it says so, and you can keep talking. One agent
  task runs at a time; "Wait, stop" followed by a new request cancels the
  running task and takes the new one.

## Checked live

- Terminal calls on macOS, 2026-09-25.
- Browser calls from an iPhone to an Ubuntu VPS, over the tailnet and the
  hosted relay, with hand-offs to the agent, 2026-09-25.

Discord voice with Realtime has not been checked live yet and stays preview.
The call page's voice menu doesn't list Realtime voices yet; change the voice
in settings.

## Set it up without the menus

Tell listeners that the voice is AI-generated.

The settings menus above are the usual way. For a scripted setup, create a
temporary answers file:

```toml
name = "realtime"
surface = "terminal"
agent_type = "pi"
route_type = "hybrid"
language = "en-US"
realtime_key = "..."
realtime_model = "gpt-realtime-2.1"
realtime_voice = "marin"
speech_model = "gpt-4o-mini-tts"
transcription_model = "gpt-4o-mini-transcribe"
```

Run setup and remove the temporary file because it contains a key:

```bash
sleeptalk init --answers /tmp/sleeptalk-realtime.toml
rm /tmp/sleeptalk-realtime.toml
```

The generated profile has a `[route]` table and no `[stt]` or `[tts]` table.
Setup keeps `OPENAI_API_KEY` in this computer's private store; the profile
holds only a reference to it. Run the provider checks before the first call:

```bash
sleeptalk doctor --config config/profiles/realtime.toml
```

Start the selected surface:

```bash
sleeptalk run --config config/profiles/realtime.toml
```

### Audio first

The gated design played nothing until OpenAI had transcribed the turn and the
ownership gate had read the transcript. Transcription starts only after the
utterance is committed, so every answer waited one transcription round trip.

Audio first is now the default. When nothing is running and nothing is
waiting for a yes, no, or clarification, Hybrid plays Realtime's reply as soon
as it produces output. Realtime has heard the audio, and its own
`ask_specialist` call decides whether the agent does the work. The transcript
still arrives, but only to be shown and logged. If the transcript arrives
first, or a task is running, the turn goes through the gate as before.
`hybrid.intent.committed` carries the reason `audio_first` on turns that
skipped the gate.

The tradeoff: the regex gate no longer catches a question that Realtime should
have handed off. To go back to the gated design, add this line to the
profile's `[route]` table:

```toml
audio_first = false
```

Terminal microphone, browser-link speech, and Discord microphone checks need a
real OpenAI key, audio hardware, and the selected Discord guild. Automated
tests use recorded PCM and provider fakes. They do not replace live operator
checks.
