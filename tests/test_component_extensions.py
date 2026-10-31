"""A new component must work through profile loading and the real session."""

from types import SimpleNamespace
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import Field

from sleeptalk.builder import build_session
from sleeptalk.config import ConfigError, load_config
from sleeptalk.defaults import STTComponents, register_defaults
from sleeptalk.providers import ProviderCapabilities
from sleeptalk.registries import (
    AGENTS,
    STT_PROVIDERS,
    TRANSPORTS,
    TTS_PROVIDERS,
    ComponentOptions,
    Registry,
)
from sleeptalk.routes.audio_output import AudioOutputAccounting


class PrivateOptions(ComponentOptions):
    marker: str
    repetitions: int = Field(default=1, ge=1, le=3)


@pytest.fixture
def components(monkeypatch):
    register_defaults()
    for registry in (AGENTS, STT_PROVIDERS, TTS_PROVIDERS, TRANSPORTS):
        monkeypatch.setattr(registry, "_registrations", dict(registry._registrations))
    instances = {}

    class Agent:
        def __init__(self, config, env):
            self.options = config.options
            self.closed = 0
            instances["agent"] = self

        async def warmup(self):
            pass

        async def respond(self, text, timeout=300):
            return "A complete response."

        async def respond_streaming(self, text, *, on_event=None):
            yield await self.respond(text)

        async def abort(self):
            pass

        async def close(self):
            self.closed += 1

    class STT:
        def __init__(self, spec):
            self.spec = spec
            self.inputs = []
            self.closed = 0
            instances["stt"] = self

        async def warmup(self):
            pass

        async def transcribe_pcm(self, pcm):
            self.inputs.append(pcm)
            return "Tell me about the orchard."

        async def close(self):
            self.closed += 1

    class TTS:
        capabilities = ProviderCapabilities(supports_streaming=True)

        def __init__(self, spec, env):
            self.spec = spec
            self.closed = 0
            instances["tts"] = self

        async def warmup(self):
            pass

        async def stream_pcm(self, text):
            yield b"\x01\x00" * 480

        def synthesize(self, text):
            return self.stream_pcm(text)

        async def close(self):
            self.closed += 1

    class Output:
        def __init__(self):
            self.chunks = []
            self.usage = AudioOutputAccounting(turn_id=1)

        async def write(self, pcm, turn_id):
            self.chunks.append(pcm)
            self.usage.generated(len(pcm))
            self.usage.written(len(pcm))

        def accounting(self):
            return self.usage

        async def discard_turn(self, turn_id, reason):
            pass

        async def close(self):
            pass

    class Surface:
        def __init__(self, config, env):
            self.options = config.transport.options
            self.output = Output()
            instances["surface"] = self

        async def run(self, session):
            self.session = session
            self.outcome = await session.run_turn(
                pcm=b"\x02\x00" * 480, output=self.output
            )

    AGENTS.register("fixture-agent", Agent, options_schema=PrivateOptions)
    STT_PROVIDERS.register(
        "fixture-stt",
        lambda spec, env: STTComponents(rest=STT(spec), streaming=None),
        options_schema=PrivateOptions,
    )
    TTS_PROVIDERS.register("fixture-tts", TTS, options_schema=PrivateOptions)
    TRANSPORTS.register("fixture-surface", Surface, options_schema=PrivateOptions)
    return instances


def profile(tmp_path, *, extra=""):
    path = tmp_path / "profile.toml"
    path.write_text(
        '[agent]\ntype = "fixture-agent"\n[agent.options]\nmarker = "agent"\n'
        '[transport]\ntype = "fixture-surface"\n[transport.options]\nmarker = "surface"\n'
        '[stt]\nprovider = "fixture-stt"\n[stt.options]\nmarker = "stt"\n'
        '[tts]\nprovider = "fixture-tts"\n[tts.options]\nmarker = "tts"\n'
        + extra
    )
    return path


async def test_unknown_components_run_with_private_settings_without_core_edits(components, tmp_path):
    config = load_config(profile(tmp_path), check_env=False, check_binaries=False)
    session = build_session(config, env={})
    await session.run()
    await session.aclose()

    assert components["surface"].session is session
    assert components["surface"].outcome.status == "completed"
    assert components["surface"].output.chunks
    assert components["agent"].options == {"marker": "agent", "repetitions": 1}
    assert components["surface"].options["marker"] == "surface"
    assert components["stt"].spec.options["marker"] == "stt"
    assert components["tts"].spec.options["marker"] == "tts"
    assert components["stt"].inputs == [b"\x02\x00" * 480]
    assert all(components[name].closed == 1 for name in ("agent", "stt", "tts"))


@pytest.mark.parametrize("extra", ['typo = true', 'repetitions = 0', 'model = "duplicate"'])
def test_private_options_are_strict(components, tmp_path, extra):
    with pytest.raises(ConfigError):
        load_config(profile(tmp_path, extra=extra), check_env=False, check_binaries=False)


def test_fallback_private_options_are_validated(components, tmp_path):
    path = profile(tmp_path, extra='[[tts.fallback]]\nprovider = "fixture-tts"\n[tts.fallback.options]\nmarker = "backup"\n')
    config = load_config(path, check_env=False, check_binaries=False)
    assert config.tts.fallback[0].options == {"marker": "backup", "repetitions": 1}
    path.write_text(path.read_text().replace('marker = "backup"', 'typo = "backup"'))
    with pytest.raises(ConfigError):
        load_config(path, check_env=False, check_binaries=False)


def test_installed_plugin_loads_only_when_selected_and_once(monkeypatch):
    registry = Registry("fixture", entry_point_group="sleeptalk.fixture")
    loads = []

    def load():
        loads.append("selected")
        return lambda: registry.register("selected", lambda config, env: config)

    def unselected():
        pytest.fail("unselected plugin was imported")

    monkeypatch.setattr(
        "sleeptalk.registries.metadata.entry_points",
        lambda **kwargs: [SimpleNamespace(name="selected", load=load), SimpleNamespace(name="other", load=unselected)],
    )
    assert registry.names() == ["other", "selected"]
    assert loads == []
    assert registry.build("selected", "value", {}) == "value"
    assert registry.get("selected").name == "selected"
    assert loads == ["selected"]


def test_failed_plugin_registration_is_rolled_back_and_retryable(monkeypatch):
    registry = Registry("fixture", entry_point_group="sleeptalk.fixture")
    attempts = []

    def register():
        registry.register("broken", lambda config, env: None)
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("incomplete setup")

    monkeypatch.setattr(
        "sleeptalk.registries.metadata.entry_points",
        lambda **kwargs: [SimpleNamespace(name="broken", load=lambda: register)],
    )
    with pytest.raises(ValueError, match="incomplete setup"):
        registry.get("broken")
    assert registry._registrations == {}
    assert registry.get("broken").name == "broken"
    assert attempts == [1, 1]


def test_distribution_entry_point_is_discovered_in_a_fresh_process(tmp_path):
    dist_info = tmp_path / "fixture_extension-1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: fixture-extension\nVersion: 1.0\n")
    (dist_info / "entry_points.txt").write_text(
        "[sleeptalk.agents]\ninstalled-agent = installed_extension:register\n"
    )
    (tmp_path / "installed_extension.py").write_text(
        "from sleeptalk.registries import AGENTS, ComponentOptions\n"
        "class Options(ComponentOptions):\n    endpoint: str\n"
        "def register():\n"
        "    AGENTS.register('installed-agent', lambda config, env: config.options, options_schema=Options)\n"
    )
    config_path = tmp_path / "profile.toml"
    config_path.write_text(
        '[transport]\ntype = "null"\n[agent]\ntype = "installed-agent"\n'
        '[agent.options]\nendpoint = "https://fixture.invalid"\n'
    )
    script = """
import sys
from sleeptalk.config import load_config
from sleeptalk.registries import AGENTS
from sleeptalk.doctor import _agent_runtime_rung
assert 'installed_extension' not in sys.modules
config = load_config(sys.argv[1], check_env=False, check_binaries=False)
assert AGENTS.build(config.agent.type, config.agent, {}) == {'endpoint': 'https://fixture.invalid'}
assert _agent_runtime_rung(config, {}).status == 'ok'
print('installed extension loaded')
"""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(tmp_path), str(Path(__file__).resolve().parents[1])))}
    result = subprocess.run(
        [sys.executable, "-c", script, str(config_path)],
        env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "installed extension loaded"


def test_private_schema_cannot_shadow_common_defaults(components, tmp_path, monkeypatch):
    from dataclasses import replace
    from sleeptalk.provider_factory import build_tts
    from sleeptalk.providers import ProviderSpec

    class ShadowOptions(ComponentOptions):
        base_url: str = "https://wrong.invalid"

    registration = TTS_PROVIDERS.get("fixture-tts")
    monkeypatch.setitem(TTS_PROVIDERS._registrations, "fixture-tts", replace(registration, options_schema=ShadowOptions))
    with pytest.raises(ConfigError, match="common"):
        load_config(profile(tmp_path), check_env=False, check_binaries=False)
    with pytest.raises(ValueError, match="shadows common"):
        build_tts(ProviderSpec("fixture-tts", options={"base_url": "https://chosen.invalid"}), {})


def test_direct_provider_spec_rejects_undeclared_options():
    from sleeptalk.provider_factory import build_tts
    from sleeptalk.providers import ProviderSpec

    with pytest.raises(ValueError, match="does not declare private options"):
        build_tts(ProviderSpec("cartesia", options={"typo": True}), {})


@pytest.mark.parametrize("field", ["provider", "model", "voice_id", "language", "temperature", "speed", "options"])
def test_direct_provider_spec_rejects_duplicate_common_fields(field):
    from sleeptalk.provider_factory import build_tts
    from sleeptalk.providers import ProviderSpec

    with pytest.raises(ValueError, match="duplicate common fields"):
        build_tts(ProviderSpec("cartesia", options={field: "shadow"}), {})


@pytest.mark.parametrize("selected", ["existing", "alias"])
def test_installed_plugin_cannot_shadow_registered_name(monkeypatch, selected):
    registry = Registry("fixture", entry_point_group="sleeptalk.fixture")
    registry.register("existing", lambda *_: None, aliases=("alias",))
    monkeypatch.setattr("sleeptalk.registries.metadata.entry_points", lambda **_: [
        SimpleNamespace(name=selected, load=lambda: pytest.fail("conflicting plugin loaded"))
    ])
    with pytest.raises(ValueError, match="conflicts"):
        registry.get(selected)


def test_duplicate_installed_plugins_are_rejected(monkeypatch):
    registry = Registry("fixture", entry_point_group="sleeptalk.fixture")
    entry = SimpleNamespace(name="duplicate", load=lambda: pytest.fail("duplicate loaded"))
    monkeypatch.setattr("sleeptalk.registries.metadata.entry_points", lambda **_: [entry, entry])
    with pytest.raises(ValueError, match="Multiple installed plugins"):
        registry.get("duplicate")


def test_plugin_failure_rolls_back_other_registries_and_forbids_replacement(monkeypatch):
    registry = Registry("fixture", entry_point_group="sleeptalk.fixture")
    other = Registry("other")
    original = lambda *_: "original"
    other.register("existing", original)

    def register():
        registry.register("selected", lambda *_: None)
        other.register("partial", lambda *_: None)
        other.register("existing", lambda *_: "replaced", replace=True)

    monkeypatch.setattr("sleeptalk.registries.metadata.entry_points", lambda **_: [
        SimpleNamespace(name="selected", load=lambda: register)
    ])
    with pytest.raises(ValueError, match="Cannot replace"):
        registry.get("selected")
    assert registry._registrations == {}
    assert other.names() == ["existing"]
    assert other.build("existing", None, {}) == "original"
