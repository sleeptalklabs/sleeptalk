from __future__ import annotations

from types import SimpleNamespace

import pytest

from sleeptalk.hermes_connect import (
    SESSION_TOKEN_ENV,
    AuthRequired,
    NotDashboard,
    Ready,
    TokenDead,
    Unreachable,
    attach_hermes,
    parse_gateway_target,
    probe_dashboard,
    probe_message,
    resolve_session_token,
    socket_url,
)


def test_parse_http_url_keeps_token_and_rewrites_path() -> None:
    target = parse_gateway_target("http://127.0.0.1:9119/?token=abc")
    assert target.origin == "http://127.0.0.1:9119"
    assert target.ws_url == "ws://127.0.0.1:9119/api/ws"
    assert target.token == "abc"


def test_parse_ws_url_strips_token_from_stored_url() -> None:
    target = parse_gateway_target("ws://127.0.0.1:9119/api/ws?token=abc")
    assert target.ws_url == "ws://127.0.0.1:9119/api/ws"
    assert target.token == "abc"


def test_socket_url_appends_token() -> None:
    assert socket_url("ws://127.0.0.1:9119/api/ws", "abc") == (
        "ws://127.0.0.1:9119/api/ws?token=abc"
    )


def test_resolve_session_token_prefers_env() -> None:
    url = "http://127.0.0.1:9119/?token=from-url"
    assert resolve_session_token(url, {SESSION_TOKEN_ENV: "from-env"}) == "from-env"
    assert resolve_session_token(url, {}) == "from-url"


@pytest.mark.asyncio
async def test_probe_ready() -> None:
    async def handshake(_url: str, *, timeout: float = 4.0) -> str:
        del timeout
        return "ready"

    probe = await probe_dashboard("http://127.0.0.1:9119", handshake=handshake)
    assert isinstance(probe, Ready)
    assert probe.ws_url == "ws://127.0.0.1:9119/api/ws"
    assert "ready" in probe_message(probe)


@pytest.mark.asyncio
async def test_probe_auth_required_and_token_dead() -> None:
    async def needs_auth(_url: str, *, timeout: float = 4.0) -> str:
        del timeout
        return "auth_required"

    async def dead(_url: str, *, timeout: float = 4.0) -> str:
        del timeout
        return "token_dead"

    auth = await probe_dashboard(token="", handshake=needs_auth)
    assert isinstance(auth, AuthRequired)
    assert SESSION_TOKEN_ENV in probe_message(auth)

    dead_probe = await probe_dashboard(token="old", handshake=dead)
    assert isinstance(dead_probe, TokenDead)
    assert "4401" in probe_message(dead_probe)
    assert "connect hermes" in probe_message(dead_probe)


@pytest.mark.asyncio
async def test_probe_does_not_swallow_handshake_bugs() -> None:
    async def boom(_url: str, *, timeout: float = 4.0) -> str:
        del timeout
        raise RuntimeError("probe fixture exploded")

    with pytest.raises(RuntimeError, match="probe fixture exploded"):
        await probe_dashboard(handshake=boom)


@pytest.mark.asyncio
async def test_probe_unreachable_and_not_dashboard() -> None:
    async def down(_url: str, *, timeout: float = 4.0) -> str:
        del timeout
        return "unreachable"

    async def wrong(_url: str, *, timeout: float = 4.0) -> str:
        del timeout
        return "not_dashboard"

    missing = await probe_dashboard(handshake=down)
    assert isinstance(missing, Unreachable)
    assert "hermes dashboard --host 127.0.0.1 --port 9119 --no-open" in probe_message(missing)
    assert "messaging gateway" in probe_message(missing)

    other = await probe_dashboard("http://127.0.0.1:18789", handshake=wrong)
    assert isinstance(other, NotDashboard)
    assert "18789" in probe_message(other)


@pytest.mark.asyncio
async def test_attach_hermes_writes_tokenless_url() -> None:
    async def handshake(url: str, *, timeout: float = 4.0) -> str:
        del timeout
        assert "token=secret" in url
        return "ready"

    answers = SimpleNamespace(
        agent_type="pi", gateway_url="", gateway_session_token=""
    )
    probe = await attach_hermes(
        answers,
        raw_url="http://127.0.0.1:9119/?token=secret",
        handshake=handshake,
    )
    assert isinstance(probe, Ready)
    assert answers.agent_type == "hermes_gateway"
    assert answers.gateway_url == "ws://127.0.0.1:9119/api/ws"
    assert answers.gateway_session_token == "secret"
    assert "token=" not in answers.gateway_url


def test_profile_toml_strips_query_token() -> None:
    from sleeptalk.wizard import WizardAnswers, env_entries, profile_toml

    answers = WizardAnswers(
        agent_type="hermes_gateway",
        gateway_url="http://127.0.0.1:9119/?token=secret",
        gateway_session_token="secret",
        name="Lab",
        slug="lab-hermes",
        guild_id="1",
        client_id="2",
        token="x",
        stt_key="s",
        tts_key="t",
        tts_voice_id="v",
    )
    text = profile_toml(answers)
    assert 'gateway_url = "ws://127.0.0.1:9119/api/ws"' in text
    assert "token=" not in text
    entries = env_entries(answers)
    assert entries[SESSION_TOKEN_ENV] == "secret"


def test_pick_connect_slug(tmp_path) -> None:
    from sleeptalk.wizard import pick_connect_slug

    (tmp_path / "only.toml").write_text("[agent]\n", encoding="utf-8")
    assert pick_connect_slug(tmp_path) == "only"
    (tmp_path / "two.toml").write_text("[agent]\n", encoding="utf-8")
    try:
        pick_connect_slug(tmp_path)
        raise AssertionError("expected a slug prompt")
    except ValueError as exc:
        assert "--slug" in str(exc)


@pytest.mark.asyncio
async def test_run_connect_hermes_writes_profile(tmp_path, monkeypatch) -> None:
    from sleeptalk.home import Layout
    from sleeptalk.wizard import run_connect_hermes

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "lab.toml").write_text(
        "[agent]\ntype = \"pi\"\nname = \"Lab\"\nslug = \"lab\"\n"
        "workspace_dir = \".\"\n\n[transport]\ntype = \"discord\"\n"
        "guild_id = \"1\"\nclient_id = \"2\"\ntoken_env = \"LAB_TOKEN\"\n"
        "\n[stt]\nprovider = \"deepgram\"\nmodel = \"nova-3\"\nlanguage = \"en-US\"\n"
        "\n[tts]\nprovider = \"cartesia\"\nvoice_id = \"v\"\nlanguage = \"en-US\"\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("LAB_TOKEN=abc\nDEEPGRAM_API_KEY=dg\nCARTESIA_API_KEY=ca\n")

    monkeypatch.setattr(
        "sleeptalk.home.resolve_layout",
        lambda: Layout(root=tmp_path, tier=2, reason="test"),
    )
    monkeypatch.setattr("sleeptalk.home.announce", lambda layout: "home")

    lines: list[str] = []
    code = await run_connect_hermes(
        url="http://127.0.0.1:9119/?token=secret",
        live=False,
        out=lines.append,
    )
    assert code == 0
    text = (profiles / "lab.toml").read_text(encoding="utf-8")
    assert 'type = "hermes_gateway"' in text
    assert "ws://127.0.0.1:9119/api/ws" in text
    assert "token=" not in text
    # The session token lives in the private store; the profile only references it.
    import tomllib
    from sleeptalk.credential_store import CredentialStore
    from sleeptalk.config import SleeptalkConfig, resolve_agent_token_env
    env_file = tmp_path / ".env"
    assert "secret" not in (env_file.read_text(encoding="utf-8") if env_file.exists() else "")
    reference = tomllib.loads(text)["credentials"]["refs"][SESSION_TOKEN_ENV]
    assert CredentialStore().get(reference) == "secret" and "secret" not in text
    config = SleeptalkConfig.model_validate(tomllib.loads(text))
    assert resolve_agent_token_env(config, {})[SESSION_TOKEN_ENV] == "secret"


@pytest.mark.asyncio
async def test_doctor_hermes_rung(monkeypatch) -> None:
    from sleeptalk.config import AgentConfig, SleeptalkConfig
    from sleeptalk.doctor import _hermes_gateway_rung
    from sleeptalk.hermes_connect import Ready, Unreachable

    async def ready(url: str, *, token: str = "", timeout: float = 4.0, handshake=None):
        del url, token, timeout, handshake
        return Ready(origin="http://127.0.0.1:9119", ws_url="ws://127.0.0.1:9119/api/ws")

    async def down(url: str, *, token: str = "", timeout: float = 4.0, handshake=None):
        del url, token, timeout, handshake
        return Unreachable(tried=("http://127.0.0.1:9119",))

    config = SleeptalkConfig(agent=AgentConfig(type="hermes_gateway", gateway_url="ws://127.0.0.1:9119/api/ws"))
    monkeypatch.setattr("sleeptalk.hermes_connect.probe_dashboard", ready)
    ok = await _hermes_gateway_rung(config, {SESSION_TOKEN_ENV: "x"})
    assert ok.status == "ok"

    monkeypatch.setattr("sleeptalk.hermes_connect.probe_dashboard", down)
    fail = await _hermes_gateway_rung(config, {SESSION_TOKEN_ENV: "x"})
    assert fail.status == "fail"
    assert "hermes dashboard --host" in fail.lines[0]


@pytest.mark.asyncio
async def test_guild_id_cannot_be_bot_user_id(monkeypatch) -> None:
    from sleeptalk.discord_check import (
        BotIdentity,
        DiscordCheckError,
        Guild,
        check_guild_membership,
    )

    async def fake_bot(_token: str) -> BotIdentity:
        return BotIdentity(name="lab", id="111111111111111111")

    async def fake_guilds(_token: str) -> list[Guild]:
        return [Guild(id="999", name="lab")]

    monkeypatch.setattr("sleeptalk.discord_check.fetch_bot", fake_bot)
    monkeypatch.setattr("sleeptalk.discord_check.list_guilds", fake_guilds)
    with pytest.raises(DiscordCheckError, match="bot's user id"):
        await check_guild_membership("tok", "111111111111111111", "client")


# ---- real sockets: current dashboards ------------------------------------------------

PAGE_TOKEN = "page-token-abcdefghijklmnopqrstuvwxyz0123"


async def _dashboard(*, envelope: bool = True, reject_at_upgrade: bool = True, ws_status: int | None = None):
    import json as _json

    from aiohttp import WSMsgType, web

    async def page(request):
        return web.Response(text=f'<html><script>window.__HERMES_SESSION_TOKEN__="{PAGE_TOKEN}";</script></html>', content_type="text/html")

    async def socket(request):
        if ws_status is not None:
            return web.Response(status=ws_status)
        if request.query.get("token") != PAGE_TOKEN and reject_at_upgrade:
            return web.Response(status=401)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        event = {"type": "gateway.ready", "payload": {}}
        await ws.send_str(_json.dumps({"jsonrpc": "2.0", "method": "event", "params": event} if envelope else event))
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                break
        return ws

    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_get("/api/ws", socket)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}"


@pytest.mark.asyncio
async def test_real_dashboard_ready_event_in_jsonrpc_envelope_is_recognized() -> None:
    runner, origin = await _dashboard(reject_at_upgrade=False)
    try:
        assert isinstance(await probe_dashboard(origin), Ready)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_real_dashboard_rejecting_the_upgrade_needs_a_token() -> None:
    runner, origin = await _dashboard()
    try:
        assert isinstance(await probe_dashboard(origin), AuthRequired)
        assert isinstance(await probe_dashboard(origin, token="wrong-token-value"), TokenDead)
        assert isinstance(await probe_dashboard(origin, token=PAGE_TOKEN), Ready)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_other_service_on_the_port_is_not_a_dashboard() -> None:
    runner, origin = await _dashboard(ws_status=404)
    try:
        assert isinstance(await probe_dashboard(origin), NotDashboard)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_attach_reads_a_local_dashboard_token_without_saving_it() -> None:
    from sleeptalk.hermes_connect import read_local_session_token

    runner, origin = await _dashboard()
    try:
        assert await read_local_session_token(origin) == PAGE_TOKEN
        answers = SimpleNamespace(agent_type="pi", gateway_url="", gateway_session_token="")
        probe = await attach_hermes(answers, raw_url=origin, env={})
        assert isinstance(probe, Ready)
        assert answers.agent_type == "hermes_gateway"
        assert answers.gateway_url.endswith("/api/ws")
        assert answers.gateway_session_token == ""  # changes on every dashboard start
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_page_tokens_are_only_read_from_this_machine() -> None:
    from sleeptalk.hermes_connect import read_local_session_token

    assert await read_local_session_token("https://dashboard.example.com") == ""


def test_running_dashboard_ports_are_discovered() -> None:
    from sleeptalk.hermes_connect import DEFAULT_ORIGIN, running_dashboard_origins

    lines = [
        "/opt/hermes/venv/bin/python -m hermes dashboard --host 127.0.0.1 --port 9139 --no-open",
        "node server.js --port 3000",
        "hermes web --port=8123",
    ]
    assert running_dashboard_origins(lines) == [DEFAULT_ORIGIN, "http://127.0.0.1:9139", "http://127.0.0.1:8123"]


@pytest.mark.asyncio
async def test_calls_prefer_a_local_dashboard_current_token(monkeypatch) -> None:
    from sleeptalk import hermes_connect
    from sleeptalk.agents import hermes_gateway_adapter as adapter

    captured: list[str] = []

    class Link:
        def __init__(self, url, *, api_key=""):
            captured.append(url)

        async def start(self):
            raise ConnectionError("stop after capturing the URL")

    async def fresh(url, *, timeout=4.0):
        return "fresh-token-after-restart"

    monkeypatch.setattr(adapter, "WebsocketTuiLink", Link)
    monkeypatch.setattr(hermes_connect, "read_local_session_token", fresh)
    client = adapter.HermesGatewayClient(base_url="ws://127.0.0.1:9119/api/ws", session_token="stale-token", cwd=".", permission_mode="pending")
    with pytest.raises(Exception):
        await client._connect()
    assert "token=fresh-token-after-restart" in captured[0]

    captured.clear()
    remote = adapter.HermesGatewayClient(base_url="wss://dashboard.example.com/api/ws", session_token="configured", cwd=".", permission_mode="pending")
    with pytest.raises(Exception):
        await remote._connect()
    assert "token=configured" in captured[0]


def test_a_local_dashboard_needs_no_saved_session_token():
    """Joining a local dashboard saves no token (it changes on every start and
    is read at call time), so loading that profile must not demand one."""
    from sleeptalk.config import AgentConfig
    from sleeptalk.defaults import register_defaults
    from sleeptalk.registries import AGENTS

    register_defaults()
    hermes = AGENTS.get("hermes_gateway")
    assert hermes.required_env_for(AgentConfig(type="hermes_gateway", gateway_url="ws://127.0.0.1:9119/api/ws")) == {}
    assert hermes.required_env_for(AgentConfig(type="hermes_gateway", gateway_url="wss://hermes.example.com/api/ws"))
