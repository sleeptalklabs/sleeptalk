# Choose your voice mode

Sleeptalk is built for a conversation that can continue while an agent works.
Your voice mode decides who handles that conversation and how work is handed
off. It is separate from where you call: browser, terminal, or Discord.

| Mode | Who talks with you | Who does the work | Background work |
| --- | --- | --- | --- |
| Cascade | Your configured agent, through separate listening and speaking models | The same agent harness | Several Sleeptalk workers on eligible agents; three run at once by default, more queue |
| Cascade with Local speech | Your configured agent; Moonshine listens and Kokoro, Piper, or Pocket TTS speaks on the host | Your selected agent or local model | Same worker behavior as Cascade; local speech also shares the host's CPU |
| GPT Realtime | An OpenAI voice model | Your configured agent receives specialist requests | One specialist task at a time; a new specialist request cancels the previous one |
| GPT Live, browser preview | GPT Live continuously listens and speaks | Your configured agent handles specialist work | Background workers on eligible agents when enabled; otherwise one backend task at a time |

These describe Sleeptalk's integrations. Selecting an OpenAI speech model does
not replace your agent, its tools, or its workspace with an OpenAI coding agent.

## Your agent owns the conversation: Cascade

Choose a speech provider such as Cartesia, Deepgram, OpenRouter, or Local.
Sleeptalk transcribes your words, passes them to your agent, and speaks its
reply. Quick questions stay in the conversation; larger tasks can go to fresh
worker sessions of the same agent while you keep talking.

This is the path for [multiple background workers](background-workers.md).
It is available across browser, terminal, and Discord, subject to the selected
agent's capabilities. “Just a model” has no tools or worker factory; command
adapters also do not receive a Sleeptalk worker factory.

## Speech stays on the host: Local

Local is a Cascade speech option, not a separate agent. Choose **Local** in
settings to install the optional runtimes and download speech models. Streaming
Moonshine Small and Kokoro are the defaults; Piper is a smaller voice option,
and Pocket TTS adds reference voices and a custom-voice workflow.

No speech API key is needed, and call audio is processed on the host. Text
still goes to your configured agent and its model. For a fully offline call
after setup, use a same-machine call with Local speech and a local model such
as Ollama. English only, CPU only. See [Local speech](local-speech.md).

## An OpenAI model handles voice: Realtime or GPT Live

Select **OpenAI Realtime** as the speech service, then choose its **Model**.
The shared settings category contains two implementations:

- **GPT Realtime** hears and speaks directly, handles conversational answers,
  and delegates workspace, tools, and current information to your agent.
  Terminal and browser calls have recorded live checks; Discord is preview.
  See [OpenAI Realtime](realtime-hybrid.md).
- **GPT Live** uses a separate continuous-audio implementation. The browser
  preview keeps listening and speaking while the specialist works. Terminal
  and Discord execution are rejected. Voice and language changes apply on the
  next call; standalone voice previews are disabled.
  See [GPT Live](gpt-live.md).

Both require an OpenAI API credential with access to the selected model.
Speech-provider charges and agent-model usage are separate. Do not infer
latency, pricing, concurrency, or interruption behavior from the shared
provider label.

Speaking over a reply and cancelling tool work are separate actions. Name the
task you want stopped and check its status. Jobs currently end with the call;
they are not persistent tasks that survive a hangup.

See the [support matrix](launch-support-matrix.md) for the release contract.
