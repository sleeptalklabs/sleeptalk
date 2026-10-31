"""Switching the live voice from the call page: proven first, then applied
to the primary and its fallbacks, then saved to the profile."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from sleeptalk import profile_edit
from sleeptalk.presentation import PresentationState
from sleeptalk.session import VoiceUnavailableError, _switch_adapter_voice


@dataclass(frozen=True)
class Spec:
    provider: str = "cartesia"
    model: str = "sonic-3"
    voice_id: str = "old-voice"
    speed: float = 1.0


class Adapter:
    def __init__(self, spec: Spec, *, fail_for: set[str] = frozenset()):
        self.spec = spec
        self.fail_for = fail_for
        self.synthesized: list[str] = []

    async def synthesize_wav(self, text: str) -> Path:
        if self.spec.voice_id in self.fail_for:
            raise RuntimeError("provider said 404 for that voice")
        self.synthesized.append(self.spec.voice_id)
        path = Path(f"/tmp/sleeptalk-voice-test-{id(self)}.wav")
        path.write_bytes(b"RIFF")
        return path


def kernel_with(primary: Adapter, *fallbacks: Adapter):
    from sleeptalk.session import RealtimeSession
    kernel = object.__new__(RealtimeSession)
    kernel._built = SimpleNamespace(tts=primary, tts_fallbacks=list(fallbacks), policy=SimpleNamespace(language="en-US"))
    kernel._route = None
    return kernel


async def test_switch_proves_the_voice_then_moves_primary_and_fallbacks():
    primary, fallback = Adapter(Spec()), Adapter(Spec(voice_id="old-voice"))
    kernel = kernel_with(primary, fallback)
    assert kernel.current_tts_voice() == ("cartesia", "old-voice")
    assert await kernel.set_tts_voice("62ae83ad-4f6a-430b-af41-a9bede9286ca") == "62ae83ad-4f6a-430b-af41-a9bede9286ca"
    assert primary.synthesized == ["62ae83ad-4f6a-430b-af41-a9bede9286ca"]
    assert primary.spec.voice_id == fallback.spec.voice_id == "62ae83ad-4f6a-430b-af41-a9bede9286ca"
    assert kernel.current_tts_voice() == ("cartesia", "62ae83ad-4f6a-430b-af41-a9bede9286ca")


async def test_a_bad_voice_is_refused_and_nothing_changes():
    primary, fallback = Adapter(Spec(), fail_for={"nope"}), Adapter(Spec())
    kernel = kernel_with(primary, fallback)
    with pytest.raises(VoiceUnavailableError) as failure:
        await kernel.set_tts_voice("nope")
    assert failure.value.code == "voice_unavailable"
    assert primary.spec.voice_id == fallback.spec.voice_id == "old-voice"
    with pytest.raises(VoiceUnavailableError):
        await kernel.set_tts_voice("   ")


def test_deepgram_switches_the_model_and_drops_its_socket():
    class Flux:
        def __init__(self):
            self.spec = Spec(provider="deepgram", model="flux-hannah-en", voice_id="")
            self.detached = 0
        def set_voice(self, voice_id):
            self.spec = replace(self.spec, model=voice_id, voice_id="")
            self.detached += 1
    flux = Flux()
    _switch_adapter_voice(flux, "flux-kit-en")
    assert flux.spec.model == "flux-kit-en" and flux.detached == 1
    kernel = kernel_with(flux)
    assert kernel.current_tts_voice() == ("deepgram", "flux-kit-en")


def test_presentation_carries_the_voice_block_with_curated_picks():
    state = PresentationState(tts_provider="cartesia", tts_voice_id="db6b0ed5-d5d3-463d-ae85-518a07d3c2b4")
    voice = state.to_json()["voice"]
    assert voice["name"] == "Skylar" and voice["voiceId"] == "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"
    assert [v["name"] for v in voice["curated"]] == ["Skylar", "Daniel", "Gemma", "Parker", "Archie"]
    assert voice["libraryUrl"].startswith("https://") and voice["notice"] == ""
    assert PresentationState(tts_provider="cartesia", tts_voice_id="custom-123").to_json()["voice"]["name"] == "Custom"
    assert "voice" not in PresentationState().to_json()


def test_profile_edit_sets_the_voice_and_keeps_everything_else(tmp_path):
    quoted = tmp_path / "quoted.toml"
    quoted.write_text('["agent"]\n"type" = "pi"\n\n["tts"]\n"provider" = "cartesia"\n"voice_id" = "old"\n"language" = "en-US"\n\n["binding"]\n"host" = "h"\n')
    assert profile_edit.set_tts_voice(quoted, "new-voice") is True
    assert quoted.read_text() == '["agent"]\n"type" = "pi"\n\n["tts"]\n"provider" = "cartesia"\n"voice_id" = "new-voice"\n"language" = "en-US"\n\n["binding"]\n"host" = "h"\n'
    assert profile_edit.set_tts_voice(quoted, "new-voice") is False
    plain = tmp_path / "plain.toml"
    plain.write_text('[tts]\nprovider = "cartesia"\n\n[log]\nlatency = true\n')
    profile_edit.set_tts_voice(plain, "v1")
    assert plain.read_text() == '[tts]\nprovider = "cartesia"\nvoice_id = "v1"\n\n[log]\nlatency = true\n'
    none = tmp_path / "none.toml"
    none.write_text('[agent]\ntype = "pi"\n')
    profile_edit.set_tts_voice(none, "v2")
    assert none.read_text().endswith('[tts]\nvoice_id = "v2"\n')


async def test_web_session_applies_a_voice_and_saves_it(tmp_path):
    from sleeptalk.web_voice.session import WebCallSession
    from tests.runtime.test_web_call import FakeStream, _SpeedKernelSession

    profile = tmp_path / "voice.toml"
    profile.write_text('[tts]\nprovider = "cartesia"\nvoice_id = "old"\n')

    class Kernel(_SpeedKernelSession):
        def current_tts_voice(self):
            return "cartesia", "old"
        async def set_tts_voice(self, voice_id):
            if voice_id == "bad":
                raise VoiceUnavailableError("permission_denied")
            return voice_id
    kernel = Kernel()
    session = WebCallSession(stream=FakeStream(), call_sid="opaque", session=kernel, profile_path=str(profile))
    assert session._presentation.to_json()["voice"]["voiceId"] == "old"
    session.apply_settings({"ttsVoice": "62ae83ad-4f6a-430b-af41-a9bede9286ca"})
    await asyncio.gather(*session._agent_state_tasks)
    assert session._presentation.tts_voice_id == "62ae83ad-4f6a-430b-af41-a9bede9286ca"
    assert session._presentation.voice_notice == "Voice changed."
    assert 'voice_id = "62ae83ad-4f6a-430b-af41-a9bede9286ca"' in profile.read_text()
    session.apply_settings({"ttsVoice": "bad"})
    await asyncio.gather(*session._agent_state_tasks)
    assert session._presentation.tts_voice_id == "62ae83ad-4f6a-430b-af41-a9bede9286ca"
    assert "can't use that voice" in session._presentation.voice_notice


async def test_voice_ids_are_one_printable_ascii_token_and_never_reach_other_endpoints():
    """A call peer can name a voice, so the ID must not carry path tricks
    into a provider URL nor break the profile file (security pass 2026-09-20)."""
    from sleeptalk.speech.elevenlabs import voice_path
    assert voice_path("../../v1/voices/abc/edit?x=1") == "/v1/text-to-speech/..%2F..%2Fv1%2Fvoices%2Fabc%2Fedit%3Fx%3D1"
    assert voice_path("kPzsL2i3teMYv0FxEYQ6") == "/v1/text-to-speech/kPzsL2i3teMYv0FxEYQ6"
    primary = Adapter(Spec())
    kernel = kernel_with(primary)
    for bad in ("x😀", "two words", "tab\tid", "x" * 201):
        with pytest.raises(VoiceUnavailableError):
            await kernel.set_tts_voice(bad)
    assert primary.synthesized == []
    assert await kernel.set_tts_voice("s3://voice-cloning-zero-shot/abc/manifest.json") == "s3://voice-cloning-zero-shot/abc/manifest.json"


async def test_a_local_adapter_that_cannot_switch_voices_is_refused():
    local = Adapter(Spec(provider="local", model="piper", voice_id="en_US-amy"))
    kernel = kernel_with(local)
    with pytest.raises(VoiceUnavailableError) as failure:
        await kernel.set_tts_voice("anything")
    assert failure.value.code == "voice_unsupported" and local.synthesized == [] and local.spec.voice_id == "en_US-amy"


def test_profile_edit_refuses_a_value_the_profile_could_not_load(tmp_path):
    profile = tmp_path / "p.toml"
    original = '[tts]\nprovider = "local"\nvoice_id = "old"\n'
    profile.write_text(original)
    with pytest.raises(ValueError):
        profile_edit.set_tts_voice(profile, "x😀")
    assert profile.read_text() == original and not list(tmp_path.glob(".sleeptalk-profile-*"))


def test_profile_edit_sets_delivery_inside_or_after_transport_options(tmp_path):
    bare = tmp_path / "bare.toml"
    bare.write_text('[agent]\ntype = "pi"\n\n[transport]\ntype = "web"\n\n[tts]\nprovider = "cartesia"\n')
    assert profile_edit.set_delivery(bare, "tailnet") is True
    assert bare.read_text().endswith('\n[transport.options]\ndelivery = "tailnet"\n')
    assert profile_edit.set_delivery(bare, "tailnet") is False
    existing = tmp_path / "existing.toml"
    existing.write_text('[transport]\ntype = "web"\n\n[transport.options]\nregion = "eu"\n\n[tts]\nprovider = "cartesia"\n')
    profile_edit.set_delivery(existing, "tailnet")
    assert existing.read_text() == '[transport]\ntype = "web"\n\n[transport.options]\nregion = "eu"\ndelivery = "tailnet"\n\n[tts]\nprovider = "cartesia"\n'
    import tomllib
    assert tomllib.loads(existing.read_text())["transport"]["options"]["delivery"] == "tailnet"


async def test_local_voices_switch_mid_call_when_the_adapter_can():
    class LocalAdapter(Adapter):
        def set_voice(self, voice_id):
            from dataclasses import replace
            self.spec = replace(self.spec, voice_id=voice_id)

    local = LocalAdapter(Spec(provider="local", model="piper-lessac-medium", voice_id="lessac"))
    kernel = kernel_with(local)
    assert await kernel.set_tts_voice("bf_emma") == "bf_emma"
    assert local.spec.voice_id == "bf_emma" and local.synthesized
