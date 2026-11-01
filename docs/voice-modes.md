# Choose your voice mode

Sleeptalk is built for a conversation that can continue while an agent works.
Your voice mode decides who handles that conversation and how work is handed
off. It is separate from where you call: browser, terminal, or Discord.

There are two conversation styles. Local versus cloud speech is a choice
within Cascade; GPT Realtime and GPT Live are implementations of Hybrid.

| Conversation style | Who talks with you | Who does the work | Speech choices |
| --- | --- | --- | --- |
| Cascade | Your configured agent, through separate listening and speaking models | The same agent harness; several background workers on eligible agents | Cloud providers, Local speech, or different providers for listening and speaking |
| Hybrid | An OpenAI voice model | Your configured agent receives specialist requests | GPT Realtime, or the separate GPT Live browser preview |

In Cascade, choose **provider → model → voice** for speech. The provider is
the API account, such as OpenRouter; a model such as Grok Voice or Kokoro may
be served through more than one provider. Local runs speech on the host and
needs no speech key. See the [complete speech-provider guide](speech-providers.md).

Your call surface (browser, terminal, or Discord) and browser delivery (local,
tailnet, or hosted link) are separate from the conversation style. Not every
combination is available; GPT Live currently accepts only browser calls.
Selecting an OpenAI voice model keeps your configured agent as the specialist
with its own tools and workspace.

## Your agent owns the conversation: Cascade

Choose a speech provider such as Cartesia, Deepgram, OpenRouter, or Local.
Sleeptalk transcribes your words, passes them to your agent, and speaks its
reply. Quick questions stay in the conversation; larger tasks can go to fresh
worker sessions of the same agent while you keep talking.

Cascade supports [multiple background workers](background-workers.md).
Three workers run at once by default; additional work queues. It is available
across browser, terminal, and Discord, subject to the selected agent's
capabilities. “Just a model” has no tools or worker factory; command adapters
also do not receive a Sleeptalk worker factory.

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
  One specialist task runs at a time; a new request cancels the previous one.
  Terminal and browser calls have historical live checks; Discord is preview.
  See [OpenAI Realtime](realtime-hybrid.md).
- **GPT Live** uses a separate continuous-audio implementation. The browser
  preview keeps listening and speaking while eligible agents run background
  workers; otherwise it uses one backend task at a time. Terminal
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
