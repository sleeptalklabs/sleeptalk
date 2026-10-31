"""Setting up type = "model": vendor, model, one key, and a profile that carries only a reference."""

from __future__ import annotations

import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sleeptalk import setup_core
from sleeptalk import model_only_setup as setup
from sleeptalk import wizard
from sleeptalk.agent_auth import auth_scope
from sleeptalk.defaults import register_defaults
from sleeptalk.wizard import WizardAnswers, answers_from_mapping, load_existing_answers, profile_toml, required_answer_fields

register_defaults()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("sleeptalk.credential_store.default_root", lambda: tmp_path / "private")
    from sleeptalk.credential_store import CredentialStore

    return CredentialStore()


class _Console:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, text: str = "", **_kwargs) -> None:
        self.lines.append(str(text))


def _script(monkeypatch, *, selects=(), texts=()) -> list[str]:
    import questionary

    prompts: list[str] = []
    select = SimpleNamespace(ask_async=AsyncMock(side_effect=list(selects)))
    monkeypatch.setattr(questionary, "select", lambda *_args, **_kwargs: select)
    remaining = list(texts)

    async def ask_text(prompt: str, *, default: str = "", password: bool = False) -> str:
        prompts.append(prompt)
        value = remaining.pop(0)
        return default if value == "<default>" else value

    monkeypatch.setattr(wizard, "_ask_text", ask_text)
    return prompts


def _verify(monkeypatch, *results: str) -> list[tuple[str, str, str]]:
    seen: list[tuple[str, str, str]] = []
    queue = list(results)

    async def verify_key(choice, model, key):
        seen.append((choice.base_url, model, key))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(setup, "verify_key", verify_key)
    return seen


# ---------------------------------------------------------------------------
# Presets and the profile
# ---------------------------------------------------------------------------


def test_presets_map_to_the_allowed_key_shapes():
    assert setup.auth_choice("openrouter").base_url == "https://openrouter.ai/api/v1"
    assert setup.auth_choice("openai").protocol == "openai-completions"
    assert setup.auth_choice("anthropic").protocol == "anthropic-messages"
    other = setup.auth_choice("other", "https://api.groq.com/openai/v1/")
    assert other.base_url == "https://api.groq.com/openai/v1" and other.mode == "api_key"
    assert setup.normalize_vendor("") == "openrouter"
    with pytest.raises(setup.ModelSetupError):
        setup.normalize_vendor("azure")
    with pytest.raises(setup.ModelSetupError, match="HTTPS"):
        setup.auth_choice("other", "http://example.com/v1")
    assert setup.auth_choice("other", "http://localhost:1234/v1/").base_url == "http://localhost:1234/v1"
    ollama = setup.auth_choice("ollama")
    assert ollama.base_url == "http://127.0.0.1:11434/v1" and ollama.protocol == "openai-completions"
    assert setup.key_optional(ollama) and setup.key_optional(setup.auth_choice("other", "http://localhost:1234/v1"))
    assert not setup.key_optional(setup.auth_choice("openrouter"))
    assert setup.key_required("openrouter") and not setup.key_required("ollama")
    assert setup.vendor_for_base_url("http://127.0.0.1:11434/v1") == "ollama"
    assert setup.ollama_origin("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434"
    with pytest.raises(setup.ModelSetupError, match="required"):
        setup.auth_choice("other", "")
    assert setup.default_model("openrouter") and setup.default_model("other") == ""
    assert setup.vendor_for_base_url("https://api.anthropic.com") == "anthropic"
    assert setup.vendor_for_base_url("https://example.test/v1") == "other"


def test_profile_toml_carries_model_and_a_reference_never_a_key():
    answers = WizardAnswers(agent_type="model", model_vendor="openrouter", model="anthropic/claude-opus-5",
                            model_key="sk-or-secret", model_credential_ref="credential:" + "a" * 32)
    document = profile_toml(answers)
    assert "sk-or-secret" not in document
    parsed = tomllib.loads(document)
    assert parsed["agent"]["model"] == "anthropic/claude-opus-5"
    assert parsed["agent"]["auth"] == {
        "mode": "api_key", "protocol": "openai-completions",
        "base_url": "https://openrouter.ai/api/v1", "credential_ref": "credential:" + "a" * 32,
    }
    from sleeptalk.config import SleeptalkConfig, _validate_component_choices

    assert _validate_component_choices(SleeptalkConfig.model_validate(parsed), env={}) == []
    # Without a stored reference there is no auth table yet.
    assert "auth" not in tomllib.loads(profile_toml(WizardAnswers(agent_type="model", model="m")))["agent"]
    # Other agents are untouched.
    assert "model" not in tomllib.loads(profile_toml(WizardAnswers(agent_type="pi", model="m")))["agent"]


def test_existing_model_profile_round_trips_into_answers(tmp_path):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    answers = WizardAnswers(agent_type="model", model_vendor="other", model_base_url="https://api.groq.com/openai/v1",
                            model="llama", model_credential_ref="credential:" + "b" * 32, name="Groqy", slug="groqy")
    (profiles / "groqy.toml").write_text(profile_toml(answers))
    loaded = load_existing_answers("groqy", profiles)
    assert (loaded.agent_type, loaded.model_vendor, loaded.model_base_url, loaded.model, loaded.model_credential_ref) == (
        "model", "other", "https://api.groq.com/openai/v1", "llama", "credential:" + "b" * 32)
    assert loaded.model_key == ""
    assert profile_toml(loaded).count("[agent.auth]") == 1
    (profiles / "anth.toml").write_text(profile_toml(WizardAnswers(
        agent_type="model", model_vendor="anthropic", model="claude-opus-5", model_credential_ref="credential:" + "c" * 32, slug="anth")))
    assert load_existing_answers("anth", profiles).model_vendor == "anthropic"


def test_answers_file_defaults_the_vendor_and_requires_the_key():
    answers = answers_from_mapping({"agent_type": "model", "model": "anthropic/claude-opus-5", "model_key": "k", "name": "Bot"})
    assert answers.model_vendor == "openrouter"
    assert "model" in required_answer_fields(answers)
    assert "model_key" in required_answer_fields(answers)
    assert "model_key" not in required_answer_fields(WizardAnswers(agent_type="model", model_credential_ref="credential:" + "d" * 32))
    assert "model_base_url" in required_answer_fields(WizardAnswers(agent_type="model", model_vendor="other"))
    assert "model" not in required_answer_fields(WizardAnswers(agent_type="pi"))
    with pytest.raises(ValueError, match="model_vendor"):
        answers_from_mapping({"agent_type": "model", "model_vendor": "azure"})


# ---------------------------------------------------------------------------
# The interactive step
# ---------------------------------------------------------------------------


async def test_choose_model_vendor_verifies_stores_and_forgets_the_key(monkeypatch, store):
    prompts = _script(monkeypatch, selects=["openrouter"], texts=["<default>", "sk-or-secret"])
    seen = _verify(monkeypatch, "available")
    answers = WizardAnswers(agent_type="model")
    console = _Console()
    assert await wizard.choose_model_vendor(answers, console, store) is None
    assert seen == [("https://openrouter.ai/api/v1", setup.default_model("openrouter"), "sk-or-secret")]
    assert answers.model_vendor == "openrouter" and answers.model == setup.default_model("openrouter")
    assert answers.model_key == "" and answers.model_credential_ref.startswith("credential:")
    status = store.status(answers.model_credential_ref)
    assert status.provider == auth_scope(setup.auth_choice("openrouter"))
    assert store.get(answers.model_credential_ref) == "sk-or-secret"
    assert "sk-or-secret" not in "\n".join(console.lines)
    assert any("no tools" in line.lower() for line in console.lines)
    assert any("✓" in line for line in console.lines)
    assert prompts[-1].startswith("API key")


async def test_choose_model_vendor_asks_again_after_a_rejected_key(monkeypatch, store):
    _script(monkeypatch, selects=["anthropic"], texts=["claude-opus-5", "bad", "good"])
    seen = _verify(monkeypatch, "unavailable", "available")
    answers = WizardAnswers(agent_type="model")
    console = _Console()
    assert await wizard.choose_model_vendor(answers, console, store) is None
    assert [key for _url, _model, key in seen] == ["bad", "good"]
    assert store.get(answers.model_credential_ref) == "good"
    assert any("rejected this key" in line for line in console.lines)
    assert answers.model_vendor == "anthropic" and answers.model_base_url == ""


async def test_choose_model_vendor_other_endpoint_must_be_https(monkeypatch, store):
    _script(monkeypatch, selects=["other"], texts=["http://example.com/v1", "https://api.groq.com/openai/v1/", "llama-3", "gsk-secret"])
    _verify(monkeypatch, "unknown")
    answers = WizardAnswers(agent_type="model")
    console = _Console()
    assert await wizard.choose_model_vendor(answers, console, store) is None
    assert answers.model_vendor == "other" and answers.model_base_url == "https://api.groq.com/openai/v1"
    assert any("HTTPS" in line for line in console.lines)
    assert any("Could not confirm" in line for line in console.lines)
    parsed = tomllib.loads(profile_toml(answers))
    assert parsed["agent"]["auth"]["base_url"] == "https://api.groq.com/openai/v1"


async def test_choose_model_vendor_keeps_a_saved_key_on_reedit(monkeypatch, store):
    choice = setup.auth_choice("openrouter")
    reference = store.put_validated(auth_scope(choice), "sk-or-saved")
    answers = WizardAnswers(agent_type="model", model_vendor="openrouter", model="old/model", model_credential_ref=reference)
    prompts = _script(monkeypatch, selects=["openrouter"], texts=["new/model", ""])
    seen = _verify(monkeypatch, "available")
    assert await wizard.choose_model_vendor(answers, _Console(), store) is None
    assert prompts[-1] == "API key (leave empty to keep the saved one):"
    assert seen == [("https://openrouter.ai/api/v1", "new/model", "sk-or-saved")]
    assert answers.model_credential_ref == reference and answers.model == "new/model"


async def test_choose_model_vendor_switching_vendor_needs_a_new_key(monkeypatch, store):
    reference = store.put_validated(auth_scope(setup.auth_choice("openrouter")), "sk-or-saved")
    answers = WizardAnswers(agent_type="model", model_vendor="openrouter", model="x", model_credential_ref=reference)
    prompts = _script(monkeypatch, selects=["anthropic"], texts=["<default>", "sk-ant-new"])
    _verify(monkeypatch, "available")
    assert await wizard.choose_model_vendor(answers, _Console(), store) is None
    assert prompts[-1] == "API key:"
    assert answers.model == setup.default_model("anthropic")
    assert answers.model_credential_ref != reference
    assert store.get(answers.model_credential_ref) == "sk-ant-new"


async def test_choose_model_vendor_back_is_honoured(monkeypatch, store):
    _script(monkeypatch, selects=[wizard._BACK])
    assert await wizard.choose_model_vendor(WizardAnswers(agent_type="model"), _Console(), store) == wizard._BACK
    _script(monkeypatch, selects=["openai"], texts=[wizard._BACK])
    assert await wizard.choose_model_vendor(WizardAnswers(agent_type="model"), _Console(), store) == wizard._BACK


async def test_agent_menu_offers_just_a_model_last_among_products():
    values = [value for _label, value in wizard.agent_menu_entries(detected={})]
    assert values[len(wizard.PRODUCT_AGENTS) - 1] == "model"
    label = next(label for label, value in wizard.agent_menu_entries() if value == "model")
    assert label.startswith("Just a model") and "ready" in label


# ---------------------------------------------------------------------------
# Headless setup and validation
# ---------------------------------------------------------------------------


async def test_validate_answers_checks_the_key_live_only(monkeypatch):
    seen = _verify(monkeypatch, "unavailable")
    answers = WizardAnswers(agent_type="model", model_vendor="openrouter", model="m", model_key="k", name="Bot",
                            stt_key="s", tts_key="t")
    assert await wizard.validate_answers(answers, live=False) == []
    monkeypatch.setattr("sleeptalk.stt_roundtrip.deepgram_roundtrip", AsyncMock(return_value=("hello", 10)))
    monkeypatch.setattr(setup_core, "_prove_selected_voice", AsyncMock(return_value=None))
    problems = await wizard.validate_answers(answers, live=True)
    assert any("rejected this key" in problem for problem in problems)
    assert seen and seen[0][2] == "k"
    bad = WizardAnswers(agent_type="model", model_vendor="other", model="m", model_key="k", name="Bot")
    assert any("model_base_url" in problem for problem in await wizard.validate_answers(bad, live=False))


async def test_ensure_setup_stores_the_key_and_writes_only_a_reference(tmp_path, monkeypatch, store):
    from sleeptalk.setup import ensure_setup

    monkeypatch.setattr("sleeptalk.setup.ensure_dependencies", lambda *args, **kwargs: ())
    answers = WizardAnswers(agent_type="model", model_vendor="openrouter", model="anthropic/claude-opus-5",
                            model_key="sk-or-headless", name="Bot", slug="bot", workspace_dir=str(tmp_path),
                            stt_key="deepgram-key", tts_provider="deepgram", tts_key="deepgram-key")
    profiles, env_path = tmp_path / "profiles", tmp_path / ".env"
    result = await ensure_setup(answers, profiles_dir=profiles, env_path=env_path, live=False)
    assert result.ready, result.messages
    document = (profiles / "bot.toml").read_text()
    assert "sk-or-headless" not in document
    assert not env_path.exists() or "sk-or-headless" not in env_path.read_text()
    parsed = tomllib.loads(document)
    reference = parsed["agent"]["auth"]["credential_ref"]
    assert store.get(reference) == "sk-or-headless"
    assert answers.model_key == "" and answers.model_credential_ref == reference
    missing = await ensure_setup(WizardAnswers(agent_type="model", model_vendor="openrouter", name="Bot", stt_key="s", tts_key="t"),
                                 profiles_dir=profiles, env_path=env_path, live=False)
    assert not missing.ready and any("model missing" in m for m in missing.messages)
    assert any("model_key missing" in m for m in missing.messages)


# ---------------------------------------------------------------------------
# Ollama: local, no key
# ---------------------------------------------------------------------------


def test_store_key_uses_the_placeholder_only_for_loopback(store):
    reference = setup.store_key(store, setup.auth_choice("ollama"), "")
    assert store.get(reference) == setup.PLACEHOLDER_KEY
    with pytest.raises(setup.ModelSetupError, match="empty"):
        setup.store_key(store, setup.auth_choice("openrouter"), "")


async def test_choose_ollama_lists_installed_models_and_asks_no_key(monkeypatch, store):
    monkeypatch.setattr(setup, "ollama_models", AsyncMock(return_value=["llama3.2:latest", "qwen3:8b"]))
    prompts = _script(monkeypatch, selects=["ollama", "qwen3:8b"])
    seen = _verify(monkeypatch, "available")
    answers = WizardAnswers(agent_type="model")
    console = _Console()
    assert await wizard.choose_model_vendor(answers, console, store) is None
    assert prompts == []  # no model text prompt, no key prompt
    assert seen == [("http://127.0.0.1:11434/v1", "qwen3:8b", "")]
    assert answers.model_vendor == "ollama" and answers.model == "qwen3:8b"
    assert store.get(answers.model_credential_ref) == setup.PLACEHOLDER_KEY
    parsed = tomllib.loads(profile_toml(answers))
    assert parsed["agent"]["auth"]["base_url"] == "http://127.0.0.1:11434/v1"
    assert any("answers here" in line for line in console.lines)


async def test_choose_ollama_when_it_is_not_running_says_how_to_start_it(monkeypatch, store):
    monkeypatch.setattr(setup, "ollama_models", AsyncMock(return_value=[]))
    _script(monkeypatch, selects=["ollama"], texts=["llama3.2"])
    _verify(monkeypatch, "unknown")
    answers = WizardAnswers(agent_type="model")
    console = _Console()
    assert await wizard.choose_model_vendor(answers, console, store) is None
    assert any("ollama serve" in line for line in console.lines)
    assert any("ollama pull llama3.2" in line for line in console.lines)
    assert answers.model == "llama3.2" and answers.model_credential_ref


async def test_choose_ollama_typed_name_from_the_list(monkeypatch, store):
    monkeypatch.setattr(setup, "ollama_models", AsyncMock(return_value=["llama3.2:latest"]))
    _script(monkeypatch, selects=["ollama", "__type__"], texts=["mistral"])
    _verify(monkeypatch, "available")
    answers = WizardAnswers(agent_type="model")
    assert await wizard.choose_model_vendor(answers, _Console(), store) is None
    assert answers.model == "mistral"


async def test_ollama_answers_file_needs_no_key(tmp_path, monkeypatch, store):
    from sleeptalk.setup import ensure_setup

    monkeypatch.setattr("sleeptalk.setup.ensure_dependencies", lambda *args, **kwargs: ())
    answers = answers_from_mapping({"agent_type": "model", "model_vendor": "ollama", "model": "llama3.2", "name": "Local",
                                    "stt_key": "s", "tts_key": "t", "workspace_dir": str(tmp_path)})
    assert "model_key" not in required_answer_fields(answers)
    assert await wizard.validate_answers(answers, live=False) == []
    result = await ensure_setup(answers, profiles_dir=tmp_path / "profiles", env_path=tmp_path / ".env", live=False)
    assert result.ready, result.messages
    parsed = tomllib.loads((tmp_path / "profiles" / "local.toml").read_text())
    assert store.get(parsed["agent"]["auth"]["credential_ref"]) == setup.PLACEHOLDER_KEY


async def test_ollama_models_reads_api_tags(monkeypatch):
    from aiohttp import web

    async def tags(request):
        return web.json_response({"models": [{"name": "llama3.2:latest"}, {"name": "qwen3:8b"}, {"name": "llama3.2:latest"}]})

    app = web.Application()
    app.router.add_get("/api/tags", tags)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        assert await setup.ollama_models(f"http://127.0.0.1:{port}/v1") == ["llama3.2:latest", "qwen3:8b"]
    finally:
        await runner.cleanup()
    assert await setup.ollama_models("http://127.0.0.1:1/v1") == []
