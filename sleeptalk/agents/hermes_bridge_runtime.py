"""Owned stdio ACP/TUI bridge for the pinned Hermes Python agent.

Executed with -I -S: only the selected source and dependency directory enter
sys.path. This module uses no Sleeptalk imports inside the Hermes environment.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextlib
import copy
import importlib
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import uuid

TOOLS = frozenset({"terminal", "read_file", "write_file", "patch", "search_files"})
# Must equal hermes_bridge.HERMES_VERSION; this file runs without importing Sleeptalk.
HERMES_VERSION = "0.21.7"
# Every path that could replace the selected credential or model mid-session.
# Each must exist on the pinned AIAgent, or startup fails instead of silently
# leaving a newer recovery path enabled.
DISABLED_RECOVERY = (
    "_try_refresh_codex_client_credentials", "_try_refresh_nous_client_credentials",
    "_try_refresh_vertex_client_credentials", "_try_refresh_copilot_client_credentials",
    "_try_refresh_anthropic_client_credentials", "_try_refresh_env_client_credentials",
    "_try_recover_stale_copilot_credential", "_adopt_openai_credentials",
    "_recover_with_credential_pool", "_try_activate_fallback",
)


def initialize(config, credential):
    # Prevent external configuration and discovery before run_agent imports.
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False
    import hermes_cli.env_loader as loader
    loader.load_hermes_dotenv = lambda *a, **k: []
    import hermes_cli.config as hermes_config
    hermes_config.load_config = lambda *a, **k: {
        "model": {"default": config["model"], "provider": config["provider"]},
        "mcp_servers": {}, "compression": {"enabled": False},
        "terminal": {"backend": "local", "cwd": config["workspace"]},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "checkpoints": {"enabled": False},
    }
    import tools.registry as registry
    def builtins(*a, **k):
        modules = ["tools.file_tools", "tools.terminal_tool"]
        for module in modules:
            importlib.import_module(module)
        return modules
    registry.discover_builtin_tools = builtins
    import hermes_cli.plugins as plugins
    plugins.discover_plugins = lambda *a, **k: None
    from run_agent import AIAgent

    def seal_client(client):
        effective_key = getattr(client, "auth_token", None) or getattr(client, "api_key", None)
        if effective_key != credential or str(client.base_url).rstrip("/") != runtime_url.rstrip("/"):
            client.close()
            raise ValueError("Hermes changed the selected credential destination")
        client._client.follow_redirects = False
        if config["protocol"] == "openai-codex-responses":
            encoded = credential.split(".")[1]
            account = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))["https://api.openai.com/auth"]["chatgpt_account_id"]
            if client.default_headers.get("ChatGPT-Account-ID") != account:
                client.close()
                raise ValueError("Hermes changed the selected OAuth account routing")
        return client

    class GuardedAgent(AIAgent):
        def _create_openai_client(self, client_kwargs, *, reason, shared):
            return seal_client(super()._create_openai_client(client_kwargs, reason=reason, shared=shared))

        def _rebuild_anthropic_client(self):
            super()._rebuild_anthropic_client()
            self._anthropic_client = seal_client(self._anthropic_client)

        def _execute_tool_calls(self, message, messages, task_id, api_call_count=0):
            # Gate the whole batch before any sequential, concurrent, inline,
            # delegate or tool-search dispatcher can run. Unknown tools remain
            # unavailable even if the model invents them.
            calls = list(message.tool_calls or [])
            approved = []
            for call in calls:
                if call.function.name in TOOLS and self._bridge.approve(call.id, call.function.name):
                    approved.append(call)
                else:
                    messages.append({"role": "tool", "tool_call_id": call.id,
                                     "content": '{"error":"Tool permission denied"}'})
            if self._bridge.cancelled:
                for call in approved:
                    messages.append({"role": "tool", "tool_call_id": call.id,
                                     "content": '{"error":"Tool permission cancelled"}'})
                return
            if approved:
                permitted = copy.copy(message)
                permitted.tool_calls = approved
                super()._execute_tool_calls(permitted, messages, task_id, api_call_count)

    protocol = config["protocol"]
    provider = "anthropic" if protocol == "anthropic-messages" and config["base_url"] == "https://api.anthropic.com" else "custom"
    runtime_url = config["base_url"]
    if protocol == "openai-codex-responses":
        provider = "openai-codex"
        runtime_url += "/codex"
    api_mode = {"anthropic-messages": "anthropic_messages", "openai-completions": "chat_completions",
                "openai-responses": "codex_responses", "openai-codex-responses": "codex_responses"}[protocol]
    agent = GuardedAgent(provider=provider, model=config["model"], api_key=credential,
                         base_url=runtime_url, api_mode=api_mode,
                         enabled_toolsets=["terminal", "file"], quiet_mode=True,
                         skip_context_files=True, skip_memory=True, load_soul_identity=False,
                         save_trajectories=False, checkpoints_enabled=False, fallback_model=None,
                         session_id=uuid.uuid4().hex, platform="sleeptalk")
    agent.session_cwd = config["workspace"]
    from tools.terminal_tool import register_task_env_overrides
    register_task_env_overrides(agent.session_id, {"cwd": config["workspace"]})
    client = agent._anthropic_client if protocol == "anthropic-messages" else agent.client
    seal_client(client)
    if agent.model != config["model"] or agent.api_mode != api_mode:
        raise ValueError("Hermes changed the selected runtime binding")
    if protocol == "anthropic-messages" and bool(agent._is_anthropic_oauth) != (config["mode"] == "oauth"):
        raise ValueError("Hermes changed the selected authentication mode")
    # An expired selected credential must not switch to a login store, keychain,
    # provider pool or fallback model on a later turn.
    missing = [name for name in DISABLED_RECOVERY if not callable(getattr(agent, name, None))]
    if missing:
        raise ValueError(f"Hermes {HERMES_VERSION} recovery paths changed: {', '.join(missing)}")
    for name in DISABLED_RECOVERY:
        setattr(agent, name, lambda *a, **k: False)
    agent._persist_disabled = True
    agent._print_fn = lambda *a, **k: None
    return agent


class Bridge:
    def __init__(self, config, agent, output):
        self.config, self.agent, self.output = config, agent, output
        self.loop = asyncio.get_running_loop()
        self.session_id = agent.session_id
        self.session_created = False
        self.history = []
        self.pending = {}
        self.turn = None
        self.cancelled = False
        agent._bridge = self

    def send(self, value):
        self.output.write(json.dumps(value, separators=(",", ":")) + "\n")
        self.output.flush()

    def event(self, kind, payload):
        self.send({"jsonrpc": "2.0", "method": "event", "params": {
            "type": kind, "session_id": self.session_id, "payload": payload}})

    async def permission(self, call_id, tool_name):
        if self.cancelled or self.config["permission_mode"] == "deny":
            return False
        request_id = "permission-" + uuid.uuid4().hex
        future = self.loop.create_future()
        self.pending[request_id] = future
        if self.config["transport"] == "acp":
            self.send({"jsonrpc": "2.0", "id": request_id, "method": "session/request_permission", "params": {
                "sessionId": self.session_id, "toolCall": {"toolCallId": call_id, "title": tool_name},
                "options": [{"optionId": "once", "kind": "allow_once", "name": "Allow once"},
                            {"optionId": "deny", "kind": "reject_once", "name": "Deny"}]}})
        else:
            self.event("approval.request", {"request_id": request_id,
                                           "message": f"Allow the Hermes {tool_name} operation?", "choices": ["once", "deny"]})
        try:
            return await asyncio.wait_for(future, 120) is True and not self.cancelled
        except (TimeoutError, asyncio.CancelledError):
            return False
        finally:
            self.pending.pop(request_id, None)

    def approve(self, call_id, tool_name):
        # Exceptions and disconnected/timed-out consumers cannot bypass this
        # boundary, unlike upstream observer hooks that swallow exceptions.
        try:
            return asyncio.run_coroutine_threadsafe(self.permission(call_id, tool_name), self.loop).result(125) is True
        except (Exception, concurrent.futures.CancelledError):
            return False

    def interrupt(self):
        self.cancelled = True
        for future in self.pending.values():
            if not future.done():
                future.set_result(False)
        self.agent.interrupt()

    async def prompt(self, text, request_id):
        try:
            result = await asyncio.to_thread(self.agent.run_conversation, text,
                                            conversation_history=self.history, task_id=self.session_id)
            if result.get("error"):
                raise ValueError("Provider turn failed")
            self.history = result.get("messages", [])
            answer = str(result.get("final_response") or "")
            if self.config["transport"] == "acp":
                self.send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": self.session_id, "update": {"sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": answer}}}})
                self.send({"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "cancelled" if self.cancelled else "end_turn"}})
            else:
                self.event("message.delta", {"text": answer})
                self.event("run.completed", {})
        except Exception:
            if self.config["transport"] == "acp":
                self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": "Selected Hermes provider turn failed"}})
            else:
                self.event("run.failed", {"message": "Selected Hermes provider turn failed"})

    async def handle(self, frame):
        if not isinstance(frame, dict):
            raise ValueError("Invalid frame")
        request_id = frame.get("id")
        if request_id in self.pending and "result" in frame:
            outcome = frame["result"].get("outcome", {})
            future = self.pending[request_id]
            if not future.done():
                future.set_result(outcome.get("outcome") == "selected" and outcome.get("optionId") == "once")
            return
        method, params = frame.get("method"), frame.get("params") or {}
        if method == "sleeptalk/probe":
            result = {"nonce": self.config["nonce"], "credential_bound": True, "workspace": self.agent.session_cwd,
                      "model": self.agent.model, "permission_mode": self.config["permission_mode"], "version": self.config.get("version", HERMES_VERSION)}
        elif method == "initialize":
            result = {"protocolVersion": 1, "agentInfo": {"name": "sleeptalk-hermes", "version": self.config.get("version", HERMES_VERSION)}, "agentCapabilities": {}}
        elif method in {"session/new", "session.create"}:
            if self.session_created or Path(params.get("cwd", "")).resolve() != Path(self.config["workspace"]) or params.get("mcpServers"):
                raise ValueError("Session does not match selected workspace")
            self.session_created = True
            result = {"sessionId" if method == "session/new" else "session_id": self.session_id}
        elif method in {"session/prompt", "prompt.submit"}:
            self.require_session(params)
            if self.turn is not None and not self.turn.done():
                raise ValueError("A turn is already active")
            if method == "session/prompt":
                prompt = params.get("prompt", [])
                if not isinstance(prompt, list) or any(p.get("type") != "text" for p in prompt):
                    raise ValueError("Only text prompts are accepted")
                text = "\n".join(p["text"] for p in prompt)
            else:
                text = params["text"]
            self.cancelled = False
            self.turn = asyncio.create_task(self.prompt(text, request_id))
            if method == "session/prompt":
                return
            result = {"accepted": True}
        elif method in {"session/cancel", "session.interrupt"}:
            self.require_session(params)
            self.interrupt()
            result = {}
        elif method == "approval.respond":
            self.require_session(params)
            if len(self.pending) != 1 or params.get("request_id") not in self.pending:
                raise ValueError("No matching pending operation")
            future = self.pending[params["request_id"]]
            if not future.done():
                future.set_result(params.get("choice") == "once")
            result = {}
        else:
            raise ValueError("Unsupported isolated bridge operation")
        if request_id is not None:
            self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def require_session(self, params):
        if not self.session_created or (params.get("sessionId") or params.get("session_id")) != self.session_id:
            raise ValueError("Unknown session")

    async def serve(self):
        reader = asyncio.StreamReader(limit=1024 * 1024)
        transport, _ = await self.loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        if self.config["transport"] == "gateway":
            self.event("gateway.ready", {"version": self.config.get("version", HERMES_VERSION)})
        try:
            while line := await reader.readline():
                frame = None
                try:
                    frame = json.loads(line)
                    await self.handle(frame)
                except Exception:
                    if isinstance(frame, dict) and "id" in frame:
                        self.send({"jsonrpc": "2.0", "id": frame["id"], "error": {"code": -32602, "message": "Isolated bridge request rejected"}})
        finally:
            self.interrupt()
            transport.close()
            if self.turn is not None:
                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(self.turn, 2)


def main():
    root, site = sys.argv[1:]
    sys.path[:0] = [root, site]
    config = json.loads(os.environ.pop("SLEEPTALK_HERMES_CONFIG"))
    credential = os.environ.pop("SLEEPTALK_HERMES_CREDENTIAL")
    os.chdir(config["workspace"])
    output = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
    # Discard Python and native FD diagnostics. The non-inheritable duplicated
    # descriptor is reserved for our bounded protocol, never tool processes.
    discarded = os.open(os.devnull, os.O_WRONLY)
    os.dup2(discarded, 1)
    os.dup2(discarded, 2)
    os.close(discarded)
    sys.stderr = open(os.devnull, "w")
    sys.stdout = sys.stderr
    logging.disable(logging.CRITICAL)
    # Startup must be inert even if a future dependency adds metadata fetches
    # or a subprocess probe. Real provider validation is performed by the host.
    connect, popen = socket.socket.connect, subprocess.Popen
    def startup_io(*a, **k):
        raise OSError("External IO is disabled during isolated initialization")
    socket.socket.connect, subprocess.Popen = startup_io, startup_io
    try:
        agent = initialize(config, credential)
    finally:
        socket.socket.connect, subprocess.Popen = connect, popen
    del credential
    async def run():
        bridge = Bridge(config, agent, output)
        await bridge.serve()
    try:
        asyncio.run(run())
    finally:
        agent.close()


if __name__ == "__main__":
    main()
