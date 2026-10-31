import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import time
import tomllib

from aiohttp.test_utils import TestClient, TestServer
import pytest

from sleeptalk.credential_store import CredentialStore
from sleeptalk.private_input import InputSession, InputSpec, atomic_json, cancel, lease, start, status

SENTINEL = "private-sentinel-never-in-receipts"


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr("sleeptalk.setup.ensure_dependencies", lambda *args, **kwargs: ())
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / ".sleeptalk"))
    async def ready(*args):
        return {"status": "ready"}
    monkeypatch.setattr("sleeptalk.runtime_probe.probe_runtime", ready)
    spec = InputSpec(request_id="request-123", profile="voice", workspace=str(tmp_path), agent="codex", surface="web", tts_provider="cartesia", profile_path=str(tmp_path / ".sleeptalk/profiles/voice.toml"))
    directory = tmp_path / "setup" / spec.request_id
    atomic_json(directory / "request.json", spec.model_dump())
    atomic_json(directory / "state.json", {"status": "waiting", "created_at": time.time(), "expires_at": time.time() + 900, "refs": {}})
    async def validate(provider, value, spec, voice):
        assert value == SENTINEL
        return {"stt_model": "nova-3"} if provider == "deepgram" else {"voice_id": "test-voice"}
    return InputSession(directory, store=CredentialStore(tmp_path / "store", backend="file"), validator=validate)


@pytest.fixture(autouse=True)
def _no_tailscale(monkeypatch):
    async def absent(*, environ=None):
        return {"available": False, "code": "tailscale_missing"}
    monkeypatch.setattr("sleeptalk.tailnet.detect", absent)


# The request is answered on the settings page: its token exchange and
# CSRF, and the request's own endpoints.
KEY = "/api/request/key"


async def client_for(session):
    from sleeptalk.settings_page import SettingsPage
    page = SettingsPage(session.profile_path, profiles_dir=session.profile_path.parent, environ={},
                        state_dir=session.directory.parent / "page-state", request=session)
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url("/"))[:-1]
    client.page = page
    return client


async def authenticate(client, session):
    page = client.page
    headers = {"Origin": page.origin}
    response = await client.post("/session", json={"token": page.token}, headers=headers)
    assert response.status == 200
    # aiohttp rejects IP cookies by default, as browsers do not.
    cookie = response.cookies[page.cookie_name].value
    return {**headers, "Cookie": page.cookie_name + "=" + cookie, "X-Settings-CSRF": (await response.json())["csrf"]}


async def view(client, headers):
    return (await (await client.get("/api/state", headers=headers)).json())["request"]


async def test_origin_host_csrf_and_no_secret_echo(session):
    async with await client_for(session) as client:
        response = await client.get("/", headers={"Host": "attacker.example"})
        assert response.status == 403
        response = await client.post("/session", json={"token": client.page.token})
        assert response.status == 403
        response = await client.get("/api/state")
        assert response.status == 401
        headers = await authenticate(client, session)
        response = await client.post(KEY, json={"provider": "deepgram", "value": SENTINEL}, headers={k: v for k, v in headers.items() if k != "X-Settings-CSRF"})
        assert response.status == 403
        response = await client.post(KEY, json={"provider": "deepgram", "value": SENTINEL}, headers=headers)
        assert response.status == 200
        assert SENTINEL not in await response.text()
        response = await client.get("/api/state", headers=headers)
        assert (await response.json())["request"]["saved"] == ["deepgram"]
        assert response.headers["Cache-Control"] == "no-store"
        for file in session.directory.rglob("*.json"):
            assert SENTINEL not in file.read_text()
        assert not session.profile_path.exists()


async def test_profile_written_only_after_every_requested_key(session):
    async with await client_for(session) as client:
        headers = await authenticate(client, session)
        response = await client.post(KEY, json={"provider": "deepgram", "value": SENTINEL}, headers=headers)
        assert response.status == 200 and (await response.json())["step"] == "speech"
        assert not session.profile_path.exists()
        response = await client.post(KEY, json={"provider": "cartesia", "value": SENTINEL}, headers=headers)
        assert response.status == 200, await response.text()
        assert (await response.json())["step"] == "done"
        document = session.profile_path.read_text()
        assert SENTINEL not in document
        parsed = tomllib.loads(document)
        assert parsed["binding"]["version"] == 1
        assert parsed["credentials"]["refs"]["CARTESIA_API_KEY"].startswith("credential:")
        assert session.profile_path.stat().st_mode & 0o777 == 0o600
        assert status(session.spec.request_id, root=session.directory.parent)["profile_ready"]
        assert not (session.profile_path.parent.parent / ".env").exists()
        # The page now edits the profile it just wrote.
        assert (await (await client.get("/api/state", headers=headers)).json())["profile"] == "voice"
        from sleeptalk.config import load_resolved_config
        cfg = load_resolved_config(session.profile_path, check_env=False, check_binaries=False)
        assert cfg.agent.workspace_dir == str(Path.home())


async def test_validation_failure_is_sanitized_and_step_remains_empty(session):
    async def fail(*args):
        raise RuntimeError("403 provider body echoed " + SENTINEL)
    session.validator = fail
    async with await client_for(session) as client:
        headers = await authenticate(client, session)
        response = await client.post(KEY, json={"provider": "deepgram", "value": SENTINEL}, headers=headers)
        assert response.status == 422
        assert await response.json() == {"status": "refused", "code": "permission_denied"}
        assert session.state["refs"] == {}


@pytest.mark.parametrize("mode", ["headphones", "speakers"])
async def test_terminal_profile_preserves_explicit_capture_and_runtime_choices(session, mode):
    session.spec = session.spec.model_copy(update={"surface": "terminal", "capture_mode": mode,
        "user_terminal_local": True, "runtime": "native", "permission_mode": "on-request"})
    async with await client_for(session) as client:
        headers = await authenticate(client, session)
        for provider in ("deepgram", "cartesia"):
            assert (await client.post(KEY, json={"provider": provider, "value": SENTINEL}, headers=headers)).status == 200
    from sleeptalk.config import load_resolved_config
    cfg = load_resolved_config(session.profile_path, check_env=False, check_binaries=False)
    assert cfg.transport.type == "terminal"
    assert cfg.transport.capture_mode == mode
    assert cfg.agent.runtime == "native"
    assert cfg.agent.permission_mode == "on-request"
    assert cfg.agent.on_incompatible == "fail"


def test_terminal_requires_user_device_and_capture_choice(session):
    from pydantic import ValidationError
    for update in ({"surface": "terminal"}, {"surface": "terminal", "capture_mode": "speakers"},
                   {"surface": "web", "user_terminal_local": True}):
        with pytest.raises(ValidationError):
            InputSpec.model_validate({**session.spec.model_dump(), **update})


@pytest.mark.parametrize("url", ["wss://user:secret@example.com", "wss://example.com/?token=secret", "wss://example.com/#secret", "file:///tmp/socket"])
def test_gateway_address_cannot_embed_secrets(session, url):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        InputSpec.model_validate({**session.spec.model_dump(), "gateway_url": url})


def test_gateway_runtime_selection_is_preserved(session):
    spec = InputSpec.model_validate({**session.spec.model_dump(), "agent": "openclaw", "runtime": "gateway",
        "gateway_url": "wss://gateway.example/ws", "permission_mode": "pending"})
    config = spec.agent_config()
    assert config.gateway_url == spec.gateway_url
    assert config.runtime == "gateway"
    assert config.permission_mode == "pending"
    assert config.on_incompatible == "fail"


async def test_irrelevant_agent_fields_never_produce_a_profile(session):
    session.spec = session.spec.model_copy(update={"gateway_url": "wss://example.com"})
    async with await client_for(session) as client:
        headers = await authenticate(client, session)
        await client.post(KEY, json={"provider": "deepgram", "value": SENTINEL}, headers=headers)
        response = await client.post(KEY, json={"provider": "cartesia", "value": SENTINEL}, headers=headers)
        assert response.status == 422
    assert not session.profile_path.exists()


async def test_surface_dependencies_checked_before_agent_or_provider_work(session, monkeypatch):
    monkeypatch.setattr("sleeptalk.setup.ensure_dependencies", lambda *args, **kwargs: ("missing",))
    async def forbidden(*args):
        pytest.fail("runtime must not launch before dependency check")
    monkeypatch.setattr("sleeptalk.runtime_probe.probe_runtime", forbidden)
    result = await start(session.spec, user_browser_local=True)
    assert result["status"] == "needs_input"
    assert "dependencies" in result["next_action"]
    assert not session.profile_path.exists()


async def test_cancel_and_expiry_preserve_validated_refs(session):
    async with await client_for(session) as client:
        headers = await authenticate(client, session)
        await client.post(KEY, json={"provider": "deepgram", "value": SENTINEL}, headers=headers)
        reference = session.state["refs"]["deepgram"]
        cancel(session.spec.request_id, root=session.directory.parent)
        response = await client.post(KEY, json={"provider": "cartesia", "value": SENTINEL}, headers=headers)
        assert response.status == 410
        assert session.store.get(reference) == SENTINEL
    state = json.loads((session.directory / "state.json").read_text())
    state["expires_at"] = time.time() - 1
    atomic_json(session.directory / "state.json", state)
    assert status(session.spec.request_id, root=session.directory.parent)["status"] == "cancelled"
    (session.directory / "cancel.json").unlink()
    assert status(session.spec.request_id, root=session.directory.parent)["status"] == "expired"


async def test_concurrent_submissions_commit_once_and_replay(session):
    calls = 0
    validator = session.validator
    async def slow(*args):
        nonlocal calls
        calls += 1
        await asyncio.sleep(.01)
        return await validator(*args)
    session.validator = slow
    async with await client_for(session) as client:
        headers = await authenticate(client, session)
        responses = await asyncio.gather(*(client.post(KEY, json={"provider": "deepgram", "value": SENTINEL}, headers=headers) for _ in range(2)))
        assert all(r.status == 200 for r in responses)
        assert calls == 1


def test_recover_credential_commit_before_state_commit(session):
    session.state["pending"] = {"deepgram": {"stt_model": "nova-3"}}
    session.save()
    ref = session.store.put_validated("deepgram", SENTINEL, request_id="request-123:deepgram")
    recovered = InputSession(session.directory, store=session.store)
    recovered.restore()
    assert recovered.state["refs"] == {"deepgram": ref}
    assert not recovered.profile_path.exists()


def test_new_invitation_recovers_progress_for_same_choices(session):
    ref = session.store.put_validated("deepgram", SENTINEL)
    session.state["refs"]["deepgram"] = ref
    session.state["details"] = {"deepgram": {"stt_model": "nova-3"}}
    session.save()
    next_dir = session.directory.parent / "request-456"
    atomic_json(next_dir / "request.json", {**session.spec.model_dump(), "request_id": "request-456"})
    atomic_json(next_dir / "state.json", {"status": "waiting", "expires_at": time.time()+900, "refs": {}})
    next_session = InputSession(next_dir, store=session.store)
    next_session.restore()
    assert next_session.state["refs"] == {"deepgram": ref}


async def test_bound_payload_and_unknown_fields(session):
    async with await client_for(session) as client:
        headers = await authenticate(client, session)
        response = await client.post(KEY, json={"provider": "deepgram", "value": "x"*17000}, headers=headers)
        assert response.status == 413
        response = await client.post(KEY, json={"provider": "unknown", "value": SENTINEL}, headers=headers)
        assert response.status == 422
        response = await client.post(KEY, json={"provider": "deepgram", "value": SENTINEL, "extra": 1}, headers=headers)
        assert response.status == 400


async def test_remote_and_unverified_runtime_fail_before_input(session, monkeypatch):
    from sleeptalk.private_input import InputError
    with pytest.raises(InputError, match="host input link"):
        await start(session.spec, user_browser_local=False)
    async def unavailable(*args):
        return {"status": "needs_input", "login": "unavailable"}
    monkeypatch.setattr("sleeptalk.runtime_probe.probe_runtime", unavailable)
    result = await start(session.spec, user_browser_local=True)
    assert result["status"] == "needs_input"
    assert "input_url" not in result


def test_profile_lease_rejects_concurrent_owner(tmp_path):
    from sleeptalk.private_input import InputError
    with lease(tmp_path / "profile.lock"):
        with pytest.raises(InputError):
            with lease(tmp_path / "profile.lock"):
                pass


async def test_cancel_during_final_probe_never_writes_profile(session, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed_probe(*args):
        entered.set()
        await release.wait()
        return {"status": "ready"}
    monkeypatch.setattr("sleeptalk.runtime_probe.probe_runtime", delayed_probe)
    async with await client_for(session) as client:
        headers = await authenticate(client, session)
        await client.post(KEY, json={"provider": "deepgram", "value": SENTINEL}, headers=headers)
        pending = asyncio.create_task(client.post(KEY, json={"provider": "cartesia", "value": SENTINEL}, headers=headers))
        await entered.wait()
        cancel(session.spec.request_id, root=session.directory.parent)
        release.set()
        assert (await pending).status == 410
        assert not session.profile_path.exists()


async def test_start_retries_share_one_child_reservation(session, monkeypatch):
    from sleeptalk import private_input
    spawned = []
    def launch(*args, **kwargs):
        spawned.append(args)
        async def ready():
            await asyncio.sleep(.1)
            atomic_json(session.directory / "invitation.json", {"input_url": "http://127.0.0.1:1/#synthetic"})
        asyncio.create_task(ready())
    monkeypatch.setattr(private_input.subprocess, "Popen", launch)
    results = await asyncio.gather(*(start(session.spec, user_browser_local=True, root=session.directory.parent) for _ in range(2)))
    assert len(spawned) == 1
    assert results[0]["input_url"] == results[1]["input_url"]


async def test_repair_replaces_the_key_in_place_and_keeps_its_profiles(session):
    ref = session.store.put_validated("deepgram", "old", profile_id="shared-profile")
    spec = {**session.spec.model_dump(), "replace_reference": ref, "affected_profiles": ["shared-profile"]}
    atomic_json(session.directory / "request.json", spec)
    repaired = InputSession(session.directory, store=session.store, validator=session.validator)
    async with await client_for(repaired) as client:
        headers = await authenticate(client, repaired)
        assert (await view(client, headers))["mode"] == "repair"
        response = await client.post(KEY, json={"provider": "deepgram", "value": SENTINEL}, headers=headers)
        assert response.status == 200, await response.text()
        assert (await response.json())["step"] == "done"
        assert repaired.store.get(ref) == SENTINEL
        assert repaired.store.status(ref).profiles == ("shared-profile",)
        assert not repaired.profile_path.exists()


def test_replacement_compare_and_swap_and_lost_ack(session):
    from sleeptalk.credential_store import CredentialError
    ref = session.store.put_validated("deepgram", "old")
    generation = session.store.status(ref).generation
    session.store.replace_validated(ref, SENTINEL, affected_profiles=[], expected_generation=generation, request_id="repair1")
    session.store.replace_validated(ref, SENTINEL, affected_profiles=[], expected_generation=generation, request_id="repair1")
    assert session.store.status(ref).generation == generation + 1
    with pytest.raises(CredentialError, match="changed during"):
        session.store.replace_validated(ref, "late", affected_profiles=[], expected_generation=generation, request_id="repair2")


async def test_discord_finish_produces_loadable_restricted_profile(session, monkeypatch):
    from sleeptalk.discord_check import Guild, VoiceChannel
    from sleeptalk.config import load_resolved_config
    spec = {**session.spec.model_dump(), "surface": "discord"}
    atomic_json(session.directory / "request.json", spec)
    async def validate(provider, *args):
        if provider == "discord":
            return {"application": {"id": "123", "name": "bot", "invite_url": "https://discord.com/oauth2/authorize"}, "guilds": [{"id": "456", "name": "server"}]}
        return {"stt_model": "nova-3"} if provider == "deepgram" else {"voice_id": "test-voice"}
    async def guilds(*args):
        return [Guild(id="456", name="server")]
    async def channels(*args):
        return [VoiceChannel(id="789", name="private", eligible=True, missing_permissions=())]
    monkeypatch.setattr("sleeptalk.discord_check.list_guilds", guilds)
    monkeypatch.setattr("sleeptalk.discord_check.list_voice_channels", channels)
    discord = InputSession(session.directory, store=session.store, validator=validate)
    async with await client_for(discord) as client:
        headers = await authenticate(client, discord)
        steps = []
        for provider in ("deepgram", "cartesia", "discord"):
            response = await client.post(KEY, json={"provider": provider, "value": SENTINEL}, headers=headers)
            assert response.status == 200, await response.text()
            steps.append((await response.json())["step"])
        assert steps == ["speech", "discord_token", "discord_place"]
        options = await (await client.post("/api/request/discord", json={"guild_id": "456"}, headers=headers)).json()
        assert options["application"]["name"] == "bot" and options["channels"][0]["id"] == "789"
        # Before a server is picked, the first server's channels come back with it.
        options = await (await client.post("/api/request/discord", json={"guild_id": ""}, headers=headers)).json()
        assert options["guild_id"] == "456" and options["channels"][0]["id"] == "789"
        response = await client.post("/api/request/finish", json={"guild_id": "456", "channel_id": "789", "operator_id": "111"}, headers=headers)
        assert response.status == 200, await response.text()
        assert (await response.json())["step"] == "done"
    cfg = load_resolved_config(discord.profile_path, check_env=False, check_binaries=False)
    assert cfg.transport.authorization_required is True
    assert cfg.transport.allowed_channel_ids == "789"
    assert cfg.transport.operator_ids == "111"
    assert cfg.transport.presence_auto_join is False


def test_damaged_state_has_value_free_error(session):
    from sleeptalk.private_input import InputError
    for damaged in ({}, {"status": "waiting", "expires_at": SENTINEL, "refs": {}}, {"status": "complete", "expires_at": 1, "refs": {SENTINEL: "bad"}}):
        atomic_json(session.directory / "state.json", damaged)
        with pytest.raises(InputError) as exc:
            status(session.spec.request_id, root=session.directory.parent)
        assert SENTINEL not in str(exc.value)


def test_repair_restores_commit_when_state_acknowledgement_was_lost(session):
    ref = session.store.put_validated("deepgram", "old")
    atomic_json(session.directory / "request.json", {**session.spec.model_dump(), "replace_reference": ref})
    repair = InputSession(session.directory, store=session.store)
    repair.state["pending"]["deepgram"] = {"stt_model": "nova-3"}
    repair.save()
    repair.store.replace_validated(ref, SENTINEL, affected_profiles=[], expected_generation=repair.state["generation"], request_id=repair.spec.request_id)
    recovered = InputSession(repair.directory, store=repair.store)
    recovered.restore()
    result = status(repair.spec.request_id, root=repair.directory.parent)
    assert result["credential_replaced"] is True
    assert result["profile_ready"] is False
    assert recovered.state["refs"]["deepgram"] == ref


def test_removed_retained_credential_reopens_only_that_step(session):
    refs = {p: session.store.put_validated(p, SENTINEL) for p in ("deepgram", "cartesia")}
    session.state["refs"] = refs.copy()
    session.state["details"] = {"deepgram": {"stt_model": "nova-3"}, "cartesia": {"voice_id": "voice"}}
    session.save()
    session.store.remove(refs["deepgram"], affected_profiles=[])
    recovered = InputSession(session.directory, store=session.store)
    recovered.restore()
    assert recovered.state["refs"] == {"cartesia": refs["cartesia"]}


def test_cancel_remains_terminal_without_live_child(session):
    result = cancel(session.spec.request_id, root=session.directory.parent)
    assert result["status"] == "cancelled"
    assert status(session.spec.request_id, root=session.directory.parent)["status"] == "cancelled"


async def test_shared_finalizer_preserves_native_model_provider_and_executable(session):
    session.spec = session.spec.model_copy(update={'agent':'omp','runtime':'native','permission_mode':'deny',
        'model':'selected-model','provider':'openai','pi_bin':'/synthetic/pinned/omp'})
    async with await client_for(session) as client:
        headers = await authenticate(client,session)
        for provider in ('deepgram','cartesia'):
            response = await client.post(KEY,json={'provider':provider,'value':SENTINEL},headers=headers)
            assert response.status == 200, await response.text()
    from sleeptalk.config import load_resolved_config
    cfg = load_resolved_config(session.profile_path,check_env=False,check_binaries=False)
    assert cfg.agent.model == 'selected-model' and cfg.agent.provider == 'openai'
    assert cfg.agent.pi_bin == '/synthetic/pinned/omp' and cfg.agent.permission_mode == 'deny'
    assert cfg.binding.runtime


async def test_failed_check_leaves_a_value_free_reason_in_status(session):
    """The page shows a code; the host keeps the exception class and HTTP status
    so an agent can say why a key check failed, without the message or the key."""
    async def broken(provider, value, spec, voice):
        raise RuntimeError("ModuleNotFoundError-ish failure mentioning " + value)
    session.validator = broken
    async with await client_for(session) as client:
        headers = await authenticate(client, session)
        response = await client.post(KEY, json={"provider": "cartesia", "value": SENTINEL}, headers=headers)
        assert response.status == 422 and (await response.json())["code"] == "validation_failed"
    report = status(session.spec.request_id, root=session.directory.parent)
    assert report["last_validation"] == {"provider": "cartesia", "code": "validation_failed", "error": "RuntimeError", "http_status": None}
    assert SENTINEL not in json.dumps(report)
    for file in session.directory.parent.rglob("*.json"):
        assert SENTINEL not in file.read_text()


async def test_english_default_voice_is_used_without_listing_voices(tmp_path, monkeypatch):
    """A Cartesia key with no requested voice validated against the wizard's
    English default; a real account with 957 voices used to fail here."""
    from sleeptalk.private_input import validate_provider
    from sleeptalk.wizard import DEFAULT_VOICES
    proved, listed = [], []

    async def prove(answers):
        proved.append(answers.tts_voice_id)
        sample = tmp_path / "sample.wav"; sample.write_bytes(b"x"); return sample
    monkeypatch.setattr("sleeptalk.setup_core._prove_selected_voice", prove)
    monkeypatch.setattr("sleeptalk.voice_discovery.default_catalogs", lambda keys: listed.append(keys) or [])
    spec = InputSpec(request_id="request-voice-1", profile="voice", workspace=str(tmp_path), agent="codex", surface="web",
                     stt_provider="deepgram", tts_provider="cartesia", language="en-US")
    result = await validate_provider("cartesia", SENTINEL, spec)
    assert result == {"voice_id": DEFAULT_VOICES["cartesia"]} and proved == [DEFAULT_VOICES["cartesia"]] and listed == []


async def test_voice_discovery_filters_by_language_before_ranking(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from sleeptalk.private_input import validate_provider
    from sleeptalk.voice_discovery import DiscoveredVoice

    async def prove(answers):
        sample = tmp_path / "sample.wav"; sample.write_bytes(b"x"); return sample
    monkeypatch.setattr("sleeptalk.setup_core._prove_selected_voice", prove)

    class Catalog:
        name = "elevenlabs"
        async def list_voices(self):
            return [DiscoveredVoice(provider="elevenlabs", voice_id="fr-1", name="Amelie", languages=["fr"]),
                    DiscoveredVoice(provider="elevenlabs", voice_id="es-1", name="Lucia", languages=["es"])]
    monkeypatch.setattr("sleeptalk.voice_discovery.default_catalogs", lambda keys: [Catalog()])
    spec = InputSpec(request_id="request-voice-1", profile="voice", workspace=str(tmp_path), agent="codex", surface="web",
                     stt_provider="deepgram", tts_provider="elevenlabs", language="es-ES")
    assert (await validate_provider("elevenlabs", SENTINEL, spec)) == {"voice_id": "es-1"}


async def test_the_request_offers_the_speaking_providers_voices(session):
    async with await client_for(session) as client:
        headers = await authenticate(client, session)
        request = await view(client, headers)
    assert {sv["id"] for sv in request["options"]["services"]} == {"deepgram", "cartesia"}
    assert [v["name"] for v in request["options"]["voices_by_service"]["cartesia"]["voices"]] == ["Skylar", "Daniel", "Gemma", "Parker", "Archie"]
    assert request["options"]["voices_by_service"]["cartesia"]["library_url"].startswith("https://")


async def test_requested_voice_is_bound_to_the_request(session):
    session.spec = session.spec.model_copy(update={"tts_voice_id": "requested-voice"})
    seen = []
    validator = session.validator

    async def validate(provider, value, spec, voice):
        seen.append((provider, voice))
        return await validator(provider, value, spec, voice)
    session.validator = validate
    async with await client_for(session) as client:
        headers = await authenticate(client, session)
        assert (await view(client, headers))["voice_locked"] == "requested-voice"
        response = await client.post(KEY, json={"provider": "cartesia", "value": SENTINEL, "voice_id": "other"}, headers=headers)
        assert response.status == 422 and seen == []
        response = await client.post(KEY, json={"provider": "cartesia", "value": SENTINEL}, headers=headers)
        assert response.status == 200
    assert seen == [("cartesia", "requested-voice")]


# --- Open provider choice: the request names no provider; the key page offers
# every one and the first key saved decides. ---

@pytest.fixture
def open_session(tmp_path, monkeypatch):
    monkeypatch.setattr("sleeptalk.setup.ensure_dependencies", lambda *args, **kwargs: ())
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / ".sleeptalk"))
    async def ready(*args):
        return {"status": "ready"}
    monkeypatch.setattr("sleeptalk.runtime_probe.probe_runtime", ready)
    spec = InputSpec(request_id="request-open-1", profile="voice", workspace=str(tmp_path), agent="codex", surface="web",
                     profile_path=str(tmp_path / ".sleeptalk/profiles/voice.toml"))
    assert spec.open_choice and spec.speech_providers == set()
    directory = tmp_path / "setup" / spec.request_id
    atomic_json(directory / "request.json", spec.model_dump())
    atomic_json(directory / "state.json", {"status": "waiting", "created_at": time.time(), "expires_at": time.time() + 900, "refs": {}})
    seen = []
    async def validate(provider, value, spec, voice):
        assert value == SENTINEL
        seen.append((provider, spec.stt_provider, spec.tts_provider, voice))
        return {"stt_model": "nova-3", "voice_id": voice or "test-voice"}
    session = InputSession(directory, store=CredentialStore(tmp_path / "store", backend="file"), validator=validate)
    session.seen = seen
    return session


def test_spec_provider_rules(tmp_path):
    base = dict(request_id="request-rules-1", profile="voice", workspace=str(tmp_path), agent="codex", surface="web")
    assert InputSpec(**base, tts_provider="cartesia").stt_provider == "deepgram"
    assert InputSpec(**base, stt_provider="cartesia", tts_provider="cartesia").speech_providers == {"cartesia"}
    with pytest.raises(ValueError, match="speaking provider"):
        InputSpec(**base, stt_provider="deepgram")
    with pytest.raises(ValueError, match="requested voice"):
        InputSpec(**base, tts_voice_id="flux-kit-en")
    chosen = InputSpec(**base).chosen("elevenlabs")
    assert (chosen.stt_provider, chosen.tts_provider, chosen.open_choice) == ("elevenlabs", "elevenlabs", False)
    assert InputSpec(**base, preferred_provider="cartesia").open_choice
    with pytest.raises(ValueError, match="preferred provider"):
        InputSpec(**base, preferred_provider="cartesia", tts_provider="cartesia")


async def test_open_request_offers_every_provider_and_the_first_key_decides(open_session):
    session = open_session
    assert session.allowed_providers() == {"cartesia", "deepgram", "elevenlabs", "openrouter", "venice", "groq", "together"}
    async with await client_for(session) as client:
        headers = await authenticate(client, session)
        request = await view(client, headers)
        assert request["choice"] == "open" and request["preferred"] == "" and request["step"] == "speech"
        assert [sv["id"] for sv in request["options"]["services"]] == ["deepgram", "cartesia", "elevenlabs", "openrouter", "groq", "together", "venice"] or \
            {sv["id"] for sv in request["options"]["services"]} == {"cartesia", "deepgram", "elevenlabs", "openrouter", "venice", "groq", "together"}
        response = await client.post(KEY, json={"provider": "deepgram", "value": SENTINEL, "voice_id": "flux-kit-en"}, headers=headers)
        assert response.status == 200, await response.text()
        # The validator saw Deepgram in both roles, with the page's voice, and the profile is written.
        assert session.seen == [("deepgram", "deepgram", "deepgram", "flux-kit-en")]
        assert session.state["provider"] == "deepgram" and session.allowed_providers() == {"deepgram"}
        assert (await response.json())["step"] == "done"
        # A second provider's key is no longer accepted.
        response = await client.post(KEY, json={"provider": "cartesia", "value": SENTINEL}, headers=headers)
        assert response.status in {410, 422}
    parsed = tomllib.loads(session.profile_path.read_text())
    assert parsed["stt"]["provider"] == parsed["tts"]["provider"] == "deepgram"
    assert parsed["tts"]["voice_id"] == "flux-kit-en"
    assert list(parsed["credentials"]["refs"]) == ["DEEPGRAM_API_KEY"]
    receipt = status(session.spec.request_id, root=session.directory.parent)
    assert receipt["profile_ready"] and receipt["speech_provider"] == "deepgram"


def test_restore_adopts_a_provider_saved_by_an_earlier_run(open_session):
    session = open_session
    reference = session.store.put_validated("cartesia", SENTINEL, request_id=session.spec.request_id + ":cartesia")
    atomic_json(session.progress_path, {"choices": session.spec.model_dump(exclude={"request_id"}),
                                        "refs": {"cartesia": reference}, "details": {"cartesia": {"stt_model": "ink-2", "voice_id": "v"}}})
    session.restore()
    assert session.state["provider"] == "cartesia" and session.choices.tts_provider == "cartesia"
    assert session.allowed_providers() == {"cartesia"}


# --- Reusing a profile's speech setup for a second surface (a Discord bot) ---

def write_source_profile(tmp_path, ref, *, refs=True):
    profiles = tmp_path / ".sleeptalk" / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "default.toml").write_text(
        '[agent]\ntype = "codex"\n\n[stt]\nprovider = "cartesia"\nmodel = "ink-2"\nlanguage = "en-GB"\n\n'
        '[tts]\nprovider = "cartesia"\nvoice_id = "voice-1"\nlanguage = "en-GB"\n'
        + (f'\n[credentials.refs]\nCARTESIA_API_KEY = "{ref}"\n' if refs else ""))


def test_speech_seed_takes_providers_voice_and_references_from_the_profile(tmp_path, monkeypatch):
    from sleeptalk.private_input import InputError, speech_seed
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / ".sleeptalk"))
    ref = "credential:" + "a" * 32
    write_source_profile(tmp_path, ref)
    spec = InputSpec(request_id="request-seed-1", profile="discord", workspace=str(tmp_path), agent="codex",
                     surface="discord", speech_from_profile="default")
    seeded, refs, details = speech_seed(spec)
    assert (seeded.stt_provider, seeded.tts_provider, seeded.language) == ("cartesia", "cartesia", "en-GB")
    assert refs == {"cartesia": ref}
    assert details == {"cartesia": {"stt_model": "ink-2", "voice_id": "voice-1"}}
    assert not seeded.open_choice and seeded.speech_from_profile == "default"
    with pytest.raises(InputError, match="don't pass a provider flag"):
        speech_seed(spec.model_copy(update={"stt_provider": "deepgram", "tts_provider": "deepgram"}))
    with pytest.raises(InputError, match="no readable speech setup"):
        speech_seed(spec.model_copy(update={"speech_from_profile": "missing"}))
    write_source_profile(tmp_path, ref, refs=False)
    with pytest.raises(InputError, match="outside the private store"):
        speech_seed(spec)


async def test_start_seeds_the_request_with_the_reused_speech_key(tmp_path, monkeypatch):
    """`host input link --surface discord --speech-from-profile default`: the
    new request begins with the speech key saved, so the page asks only for
    the bot token."""
    import subprocess
    from sleeptalk import private_input
    monkeypatch.setattr("sleeptalk.setup.ensure_dependencies", lambda *args, **kwargs: ())
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / ".sleeptalk"))
    async def ready(*args):
        return {"status": "ready"}
    monkeypatch.setattr("sleeptalk.runtime_probe.probe_runtime", ready)
    store = CredentialStore(tmp_path / "store", backend="file")
    ref = store.put_validated("cartesia", SENTINEL, request_id="earlier:cartesia")
    write_source_profile(tmp_path, ref)
    root = tmp_path / "setup"

    def fake_launch(command, **kwargs):
        directory = Path(command[command.index("--directory") + 1])
        assert command[-3] == "sleeptalk.setup_link"
        atomic_json(directory / "invitation.json", {"entry": "link", "setup_url": "https://call.example/#/setup/x"})
        return SimpleNamespace()
    monkeypatch.setattr(private_input.subprocess, "Popen", fake_launch)
    spec = InputSpec(request_id="request-seed-2", profile="discord", workspace=str(tmp_path), agent="codex",
                     surface="discord", speech_from_profile="default")
    receipt = await start(spec, user_browser_local=False, root=root, entry="link")
    assert receipt["entry"] == "link" and receipt["completed_providers"] == ["cartesia"]
    assert receipt["surface"] == "discord" and receipt["pending_providers"] == ["discord"]
    session = InputSession(root / spec.request_id, store=store)
    assert session.spec.tts_provider == "cartesia" and session.spec.language == "en-GB"
    assert session.allowed_providers() == {"cartesia", "discord"}
    session.restore()
    assert session.state["refs"] == {"cartesia": ref}
    assert session.state["details"]["cartesia"] == {"stt_model": "ink-2", "voice_id": "voice-1"}
    # A profile whose key left the store gets asked for it again.
    store.remove(ref, affected_profiles=[])
    session = InputSession(root / spec.request_id, store=store)
    session.restore()
    assert session.state["refs"] == {}


async def test_dependency_problems_are_named_in_the_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr("sleeptalk.setup.ensure_dependencies", lambda *args, **kwargs: ("Discord needs the sleeptalk[discord] extra.",))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / ".sleeptalk"))
    spec = InputSpec(request_id="request-deps-1", profile="discord", workspace=str(tmp_path), agent="codex", surface="discord")
    receipt = await start(spec, user_browser_local=False, root=tmp_path / "setup", entry="link")
    assert receipt["status"] == "needs_input" and receipt["problems"] == ["Discord needs the sleeptalk[discord] extra."]
    assert "sleeptalk[discord]" in receipt["next_action"]


async def test_invitation_opens_one_session_unless_the_browser_already_has_it(session):
    async with await client_for(session) as client:
        headers = await authenticate(client, session)
        origin = {"Origin": client.page.origin}
        client.session.cookie_jar.clear()
        response = await client.post("/session", json={"token": client.page.token}, headers=origin)
        assert response.status == 401
        response = await client.post("/session", json={"token": client.page.token},
                                     headers={**origin, "Cookie": headers["Cookie"]})
        assert response.status == 200




async def test_a_key_request_page_does_only_that_request(session):
    async with await client_for(session) as client:
        headers = await authenticate(client, session)
        for path, body in (("/api/profile/delete", {"confirm": "voice"}), ("/api/agent", {"agent": "pi"}),
                           ("/api/keys/remove-unused", {"references": ["credential:" + "a" * 32]}),
                           ("/api/set", {"setting": "policy.reply_length", "value": "full"})):
            response = await client.post(path, json=body, headers=headers)
            assert response.status == 403, path
        assert (await client.get("/api/state", headers=headers)).status == 200


async def test_an_aggregator_voice_for_another_language_comes_from_its_catalog(tmp_path, monkeypatch):
    """OpenRouter has no voice library to search; a Chinese profile with no
    voice picked used to stop with StopIteration (shown as validation_failed)."""
    from sleeptalk.private_input import validate_provider

    proved = []

    async def prove(answers):
        proved.append((answers.tts_model, answers.tts_voice_id))
        sample = tmp_path / "sample.wav"; sample.write_bytes(b"x"); return sample
    monkeypatch.setattr("sleeptalk.setup_core._prove_selected_voice", prove)
    spec = InputSpec(request_id="request-voice-1", profile="voice", workspace=str(tmp_path), agent="codex", surface="web",
                     tts_provider="openrouter", language="zh-CN")
    result = await validate_provider("openrouter", SENTINEL, spec, tts_model="qwen/qwen-audio-3.0-tts-flash")
    assert result == {"voice_id": "longanhuan_v3.6"}
    assert proved == [("qwen/qwen-audio-3.0-tts-flash", "longanhuan_v3.6")]
