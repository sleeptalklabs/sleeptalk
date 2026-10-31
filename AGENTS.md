# Working in this repository

Read [CONTRIBUTING.md](CONTRIBUTING.md) for setup and the test commands.

Before committing a change to code that a running `sleeptalk run` process
loads, run the focused tests for the changed behavior, then:

```bash
uv run python -c "from sleeptalk.cli import main"
uv run pytest -q
```

Read your diff before you commit and describe the diff, not the intent.
Never commit `.env`, `sleeptalk.toml`, or anything under `config/profiles/`
other than `example.toml`.

Live microphone, Discord speech, and service restart checks stay human gates.
Do not claim them from automated output.
