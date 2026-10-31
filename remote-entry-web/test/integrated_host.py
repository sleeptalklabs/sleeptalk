"""Actual host, durable stores and WebSocket relay with synthetic provider hooks."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib

import aiohttp
from aiohttp import web
import sleeptalk

# An optional uncommitted source snapshot supports parallel integration work.
if os.environ.get("SLEEPTALK_TEST_SOURCE"):
    sleeptalk.__path__.insert(0, os.environ["SLEEPTALK_TEST_SOURCE"])

from sleeptalk.credential_store import CredentialStore
from sleeptalk.private_input import InputSpec
from sleeptalk.remote_crypto import canonical
from sleeptalk.remote_host import RemoteHost, runtime_binding_digest
from sleeptalk.remote_relay import SetupRelay
from sleeptalk.remote_local import remote_invitation_url
from sleeptalk.remote_store import RemoteStore, identifier

ORIGIN = "https://setup.example.com"
MODEL_AUTH = os.environ.get("SLEEPTALK_BROWSER_MODEL_AUTH") == "1"


async def main():
    with tempfile.TemporaryDirectory(prefix="sleeptalk-browser-integration-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        (root / "leases").mkdir()
        credentials = CredentialStore(root / "credentials", backend="file")
        store = RemoteStore(root / "remote", credentials)
        spec = InputSpec(request_id="browser-fixture-1", profile="browser-fixture-1", workspace=str(workspace),
            agent="codex", surface="web", tts_provider="cartesia", profile_path=str(root / "profiles/first.toml"))
        if MODEL_AUTH:
            from sleeptalk.agent_auth import AgentAuthChoice
            spec = spec.model_copy(update={"agent": "acp", "runtime": "acp", "permission_mode": "deny",
                "model": "fixture-model", "provider": "custom",
                "options": {"hermes_root": str(root / "synthetic-hermes")},
                "auth_request": AgentAuthChoice(mode="api_key", protocol="openai-completions", base_url="https://provider.example/v1")})
        binding = runtime_binding_digest(spec)
        workspace_id = identifier()
        enrollment = store.create_enrollment(workspace_id=workspace_id, binding=binding, origin=ORIGIN,
            display_host="Synthetic host", display_workspace="Disposable workspace", workspace_spec=spec.model_dump())
        enrollment_id = enrollment["descriptor"]["enrollment_id"]
        relay = SetupRelay(ORIGIN, {store.host_id: store.public_key})
        runner = web.AppRunner(relay.app(), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        local = "http://127.0.0.1:" + str(runner.addresses[0][1])
        validations = 0
        preparations = 0
        setup_number = 0
        confirmed = None
        setup = None
        last_submission = None
        model_submission = None
        fail_next = False

        async def validator(provider, value, selected):
            nonlocal validations, fail_next
            assert (provider, value) in {("deepgram", "synthetic-provider-value"), ("cartesia", "synthetic-tts-value")}
            assert MODEL_AUTH or provider == "deepgram"
            assert selected.workspace == str(workspace)
            validations += 1
            if fail_next:
                fail_next = False
                raise ValueError("Synthetic provider rejection")
            await asyncio.sleep(0.1)
            return ({"stt_model": "nova-3"} if provider == "deepgram" else {"voice_id": "fixture-voice"}) if MODEL_AUTH else {"validated": True}

        if MODEL_AUTH:
            from sleeptalk.agents import provider_auth
            from sleeptalk import runtime_probe
            from sleeptalk.agent_auth import auth_scope
            async def verify_model(selected, model):
                nonlocal validations
                assert selected.mode == "api_key" and selected.protocol == "openai-completions"
                assert selected.base_url == "https://provider.example/v1" and model == "fixture-model"
                assert selected.credential == "synthetic-model-private-value"
                validations += 1
                return "available"
            async def probe_selected(agent, **kwargs):
                assert agent.type == "acp" and agent.model == "fixture-model" and agent.provider == "custom"
                assert agent.permission_mode == "deny" and agent.runtime == "acp"
                assert agent.auth.base_url == "https://provider.example/v1"
                assert credentials.status(agent.auth.credential_ref).provider == auth_scope(agent.auth)
                assert credentials.get(agent.auth.credential_ref) == "synthetic-model-private-value"
                return {"status": "ready"}
            provider_auth.verify = verify_model
            runtime_probe.probe_runtime = probe_selected

        async def prepare_profile(selected, refs, details, credential_store, choices, profile_path, lease_root):
            nonlocal preparations
            assert credential_store is credentials
            if MODEL_AUTH:
                from sleeptalk.private_input import prepare_profile as real_prepare_profile
                assert selected.auth_request is None and selected.auth is not None
                assert set(refs) == {"deepgram", "cartesia"}
                document = await real_prepare_profile(selected, refs, details, credential_store, choices, profile_path, lease_root)
                parsed = tomllib.loads(document)
                assert parsed["agent"]["auth"]["credential_ref"] == selected.auth.credential_ref
                assert parsed["credentials"]["refs"] == {"DEEPGRAM_API_KEY": refs["deepgram"], "CARTESIA_API_KEY": refs["cartesia"]}
                assert "synthetic-model-private-value" not in document and "synthetic-provider-value" not in document and "synthetic-tts-value" not in document
                preparations += 1
                return document
            assert set(refs) == {"deepgram"}
            preparations += 1
            return '# Synthetic browser proof profile\nname = "Browser proof"\n'

        async with aiohttp.ClientSession() as http:
            class LocalHostSession:
                def ws_connect(self, url, **kwargs):
                    assert url.startswith(ORIGIN + "/v1/hosts/")
                    return http.ws_connect(local + url[len(ORIGIN):], headers={"Host": "setup.example.com"}, **kwargs)

            host = RemoteHost(store, ORIGIN, lease_root=root / "leases", validator=validator,
                profile_preparer=prepare_profile, session=LocalHostSession())
            dispatch_error = None
            original_dispatch = host.dispatch
            async def checked_dispatch(*args):
                nonlocal dispatch_error
                try:
                    return await original_dispatch(*args)
                except Exception as exc:
                    import traceback
                    trace = traceback.extract_tb(exc.__traceback__)[-1]
                    dispatch_error = {'type':type(exc).__name__,'file':Path(trace.filename).name,'line':trace.lineno}
                    raise
            host.client.dispatch = checked_dispatch
            await host.start()
            try:
                await asyncio.wait_for(host.client.connected.wait(), 5)
                print(json.dumps(enrollment), flush=True)

                async def forward(message):
                    headers = {"Host": "setup.example.com", "Origin": ORIGIN,
                        "Authorization": message["authorization"], "Content-Type": "application/json"}
                    async with http.post(local + "/v1/setup/" + message["request_id"] + "/" + message["action"],
                                         headers=headers, data=canonical(message.get("body", {}))) as response:
                        result = await response.json()
                        assert response.status == 200, "Actual relay rejected synthetic browser request"
                        if dispatch_error:
                            return {'fixture_error':'Host dispatch rejected the synthetic request', 'cause':dispatch_error}
                        assert "payload" in result and "signature" in result, "Actual coordinator rejected synthetic browser request"
                        return result

                async def handle(message):
                    nonlocal confirmed, setup, setup_number, last_submission, model_submission, fail_next
                    action = message["action"]
                    if action == "invitation_url":
                        return {"url": remote_invitation_url(message["invitation"])}
                    if action == "confirm_local":
                        status = store.enrollment_status(enrollment_id)["payload"]
                        confirmed = store.confirm_enrollment(enrollment_id, status["fingerprint"], current_binding=binding)
                        return {"confirmed": True}
                    if action == "new_setup":
                        setup_number += 1
                        fail_next = message.get("fail_first", False)
                        selected = spec.model_copy(update={"request_id": f"browser-fixture-{setup_number}",
                            "profile": f"browser-fixture-{setup_number}", "profile_path": str(root / f"profiles/{setup_number}.toml")})
                        fields = {"deepgram.api_key": {"reference": None, "generation": 0, "profiles": []}}
                        if MODEL_AUTH:
                            fields.update({field: {"reference": None, "generation": 0, "profiles": []} for field in ("cartesia.api_key", "agent.credential")})
                        setup = {"kind": "setup", **store.create_request(operator_id=confirmed["operator_id"],
                            workspace_id=workspace_id, binding=binding, spec=selected.model_dump(),
                            fields=fields,
                            origin=ORIGIN, display_host="Synthetic host", display_workspace="Disposable workspace")}
                        # Publish newly added routes through the real owned WebSocket.
                        await host.client.socket.send_json({"type": "routes", "routes": host.routes()})
                        request_id = setup["descriptor"]["request_id"]
                        for _ in range(100):
                            if request_id in relay.hosts[store.host_id].routes:
                                return setup
                            await asyncio.sleep(0.01)
                        raise TimeoutError("New host route was not registered")
                    if action == "assert_failure_recovery":
                        state = store.request_state(setup["descriptor"]["request_id"])
                        assert state["profile_ready"] is True and state["failures"]["deepgram.api_key"] == 1
                        with credentials._database() as db:
                            count = db.execute("SELECT count(*) FROM operations").fetchone()[0]
                        return {"validations": validations, "commits": count, "profiles": preparations}
                    if action == "assert_once":
                        before = store.status(setup["descriptor"]["request_id"])["payload"]
                        assert before["profile_ready"] is True
                        replayed = model_submission if MODEL_AUTH else last_submission
                        replay = await forward(replayed)
                        with credentials._database() as db:
                            count, generation = db.execute("SELECT count(*), max(generation) FROM operations").fetchone()
                        state = store.request_state(setup["descriptor"]["request_id"])
                        field = state["fields"]["deepgram.api_key"]
                        assert credentials.get(field["reference"]) == "synthetic-provider-value"
                        if MODEL_AUTH:
                            document = Path(state["spec"]["profile_path"]).read_text()
                            parsed = tomllib.loads(document)
                            reference = state["fields"]["agent.credential"]["reference"]
                            auth = parsed["agent"]["auth"]
                            assert auth["credential_ref"] == reference
                            assert credentials.get(reference) == "synthetic-model-private-value"
                            assert credentials.status(reference).provider == auth_scope(spec.auth_request)
                            assert state["spec"]["auth_request"] == spec.auth_request.model_dump()
                            assert state["descriptor"]["model_auth"] == {**spec.auth_request.model_dump(), "model": spec.model, "provider": spec.provider}
                            assert len(parsed["credentials"]["refs"]) == 2
                            assert credentials.status(reference).generation == 1
                            assert "synthetic-model-private-value" not in document
                            return {"validations": validations, "commits": count, "generation": generation,
                                "profiles": preparations, "model_reference_scoped": True, "profile_auth_reference": True,
                                "replay_same": replay["payload"]["status"] == "saved" and replay["payload"]["submission_id"] == replayed["body"]["context"]["submission_id"]}
                        assert Path(state["spec"]["profile_path"]).read_text() == '# Synthetic browser proof profile\nname = "Browser proof"\n'
                        return {"validations": validations, "commits": count, "generation": generation,
                            "profiles": preparations, "replay_same": replay["payload"]["status"] == "saved" and replay["payload"]["submission_id"] == replayed["body"]["context"]["submission_id"]}
                    if action == "submission":
                        last_submission = message
                        if message["body"]["context"]["provider_field"] == "agent.credential":
                            model_submission = message
                    return await forward(message)

                while line := await asyncio.to_thread(sys.stdin.readline):
                    try:
                        result = await handle(json.loads(line))
                    except Exception as exc:
                        import traceback
                        trace = traceback.extract_tb(exc.__traceback__)[-1]
                        result = {"fixture_error": type(exc).__name__, "line": trace.lineno, "file": Path(trace.filename).name}
                    print(json.dumps(result), flush=True)
            finally:
                await host.close()
                await runner.cleanup()


asyncio.run(main())
