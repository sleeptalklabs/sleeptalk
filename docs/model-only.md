# Just a model: `type = "model"`

Talk to a model directly. No agent framework, no tools, no files. One API key
from OpenRouter, OpenAI, Anthropic, or any OpenAI-compatible endpoint, or a
model already pulled into a local Ollama, and you are on a call in a couple of
minutes.

This is the on-ramp and the diagnostic baseline. It is not the product. When
you want the thing on the call to read your repo or run a command, pick an
agent in `sleeptalk settings`; that is what the agent adapters are for, and
this adapter never grows tools.

## What it does

- Streams one chat request per turn and speaks the sentences as they arrive.
- Remembers the conversation for the life of the process. `history_turns`
  bounds how much is replayed; `0` forgets everything between turns.
- Fires the "thinking" filler only when the model streams reasoning first.
- Barge-in aborts the request; what was spoken is what the model is told it said.

## What it never does

- No tools, no file access, no web search, no memory past the process.
- No OAuth or subscription logins. Those tokens belong to their own agents.
- No plain `http://` off this machine. HTTPS anywhere; `http://` only on
  `127.0.0.1` or `localhost`, which is where Ollama and LM Studio live.
- No ambient keys. `OPENAI_API_KEY` in the environment is ignored; the profile
  names one stored credential and nothing else is consulted.

## Set it up

### First run

`sleeptalk` lists it last under "Which agent harness do you want to talk to?" as
**Just a model**. The step asks for:

1. **Vendor**: OpenRouter (default), OpenAI, Anthropic, Ollama, or another
   OpenAI-compatible endpoint, for which you paste the base URL.
2. **Model name**, prefilled per vendor. For Ollama the step lists the models
   you have pulled and you pick one.
3. **API key**, masked. It is checked against the vendor before it is stored.
   OpenRouter and Anthropic can confirm the model too; other endpoints may
   only confirm the key, and the first call tells the rest. Ollama asks for
   no key. A local server entered as "other" may leave the key empty.

The key goes into the private credential store under a scope that names the
exact endpoint. The profile carries a reference:

```toml
[agent]
type = "model"
model = "anthropic/claude-opus-5"

[agent.auth]
mode = "api_key"
protocol = "openai-completions"
base_url = "https://openrouter.ai/api/v1"
credential_ref = "credential:0123456789abcdef0123456789abcdef"
```

Editing the profile in `sleeptalk settings` offers to keep the saved key.
Switching vendors asks for a new one, because the reference is scoped to the
endpoint it was verified against.

### Answers file

```toml
agent_type = "model"
model_vendor = "openrouter"      # openrouter | openai | anthropic | other
model = "anthropic/claude-opus-5"
model_key = "sk-or-..."          # stored privately, never written to the profile or .env
# model_base_url = "https://api.groq.com/openai/v1"   # only for model_vendor = "other"
```

`model_vendor` defaults to `openrouter`. Headless setup verifies the key live,
stores it, and writes the reference. For `model_vendor = "ollama"` leave
`model_key` out.

### Ollama

```sh
ollama serve
ollama pull llama3.2
sleeptalk            # Just a model → Ollama → pick llama3.2
```

The endpoint is `http://127.0.0.1:11434/v1`. Ollama ignores the bearer token,
so the profile's reference points at the placeholder `ollama`; nothing secret
is stored. If Ollama is not running when you set up, the step says how to
start it and lets you type the model name anyway. A model you have not pulled
fails the first call with a 404 naming it.

### Through an agent

The private entry path is the same one Hermes uses. Collect the credential on
the masked page, then create the profile with the reference:

```sh
sleeptalk host auth start --request-id model-001 \
  --mode api_key --protocol openai-completions \
  --base-url https://openrouter.ai/api/v1 --model anthropic/claude-opus-5 \
  --user-browser-local

sleeptalk host input start --agent model --model anthropic/claude-opus-5 \
  --agent-auth-mode api_key --agent-auth-protocol openai-completions \
  --agent-auth-url https://openrouter.ai/api/v1 \
  --agent-credential-ref 'credential:<from the receipt>' ...
```

`sleeptalk host probe-runtime --agent model ...` reports `ready` when the
stored key answers for the selected model.

## Key shapes

| Vendor | Protocol | Endpoint |
| --- | --- | --- |
| OpenRouter | `openai-completions` | `https://openrouter.ai/api/v1` |
| OpenAI | `openai-completions` | `https://api.openai.com/v1` |
| Anthropic | `anthropic-messages` | `https://api.anthropic.com` |
| Ollama | `openai-completions` | `http://127.0.0.1:11434/v1` (no key) |
| Other | `openai-completions` | any canonical HTTPS base URL, or `http://` on loopback |

Keys are not pattern-matched. The live probe is the check.

## Options

```toml
[agent.options]
max_tokens = 1024        # per reply
history_turns = 20       # prior exchanges replayed; 0 = none
connect_timeout_s = 10
idle_timeout_s = 60      # silence from the endpoint before the turn fails
# temperature = 0.7
```

`persona_instructions` is appended to the shared spoken-reply contract as the
system prompt.

## Errors you may hear about

- **rejected the credential (HTTP 401/403)**: re-enter the key in settings.
- **no such model or path (HTTP 404)**: the model name is wrong for that
  vendor, or the base URL is missing its `/v1`.
- **sent nothing for 60s**: the endpoint stalled; raise `idle_timeout_s` for
  slow models or check the vendor's status page.
