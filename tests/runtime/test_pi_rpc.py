import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sleeptalk.text import (
    SpeakableStreamFilter as _SpeakableStreamFilter,
    extract_sentence as _extract_sentence,
    extract_speakable_text as _extract_speakable_text,
)
from sleeptalk.agents.pi_rpc import (
    OmpRPCClient,
    PiRPCClient,
    PiRPCError,
)

pytestmark = pytest.mark.usefixtures("dummy_cli_bins")


class FakeProcess:
    def __init__(self, stdout_lines: list[str]):
        self.stdout_lines = stdout_lines
        self._stdout_idx = 0
        self._returncode = None
        self.stdin = MagicMock()
        self.stdin.write = MagicMock(side_effect=self._write_stdin)
        self._stdout_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.stdin.drain = AsyncMock()
        self.stdout = MagicMock()
        self.stdout.read = AsyncMock(side_effect=self._read_stdout)

    def _write_stdin(self, raw: bytes) -> None:
        command = json.loads(raw)
        kind = command['type']
        if kind == 'get_state':
            events = [{
                'type': 'response', 'id': command['id'], 'success': True,
                'data': {'isStreaming': False},
            }]
        elif kind == 'prompt':
            events = [
                {'type': 'response', 'id': command['id'], 'success': True},
                *[json.loads(line) for line in self.stdout_lines],
            ]
        elif kind == 'set_thinking_level':
            events = [json.loads(line) for line in self.stdout_lines
                      if json.loads(line).get('id') == command['id']]
        else:
            events = []
        for event in events:
            self._stdout_queue.put_nowait((json.dumps(event) + '\n').encode())

    async def _read_stdout(self, n: int) -> bytes:
        return await self._stdout_queue.get()

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def wait(self) -> int:
        return 0

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


@pytest.fixture
def client():
    return PiRPCClient(cwd="/tmp", no_session=True)


@pytest.mark.asyncio
async def test_prompt_basic_response(client):
    events = [
        {"type": "agent_start"},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "Hello"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": " world"}},
        {"type": "agent_settled"},
    ]
    proc = FakeProcess([json.dumps(e) for e in events])

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await client.respond("Say hello")

    assert result == "Hello world"
    # Verify the prompt was sent
    calls = [call for call in proc.stdin.write.call_args_list
             if json.loads(call[0][0])["type"] != "get_state"]
    assert len(calls) == 1
    sent = json.loads(calls[0][0][0].decode("utf-8"))
    assert sent["type"] == "prompt"
    assert sent["message"] == "Say hello"


@pytest.mark.asyncio
async def test_prompt_skips_thinking_and_code():
    events = [
        {"type": "agent_start"},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "Here is the code:\n\n"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "```python\nprint(1)\n```\n\nDone."}},
        {"type": "agent_settled"},
    ]
    proc = FakeProcess([json.dumps(e) for e in events])
    client = PiRPCClient(cwd="/tmp", no_session=True)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await client.respond("Write hello world")

    assert result == "Here is the code: Done."


@pytest.mark.asyncio
async def test_prompt_timeout():
    # Only agent_start, never agent_settled
    events = [
        {"type": "agent_start"},
    ]
    proc = FakeProcess([json.dumps(e) for e in events])
    client = PiRPCClient(cwd="/tmp", no_session=True)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        # The idle and hard limits share this deadline; agent_start moves the
        # idle one, so which message wins depends on timing. Both are a timeout.
        with pytest.raises(PiRPCError, match="timed out|exceeded maximum time"):
            await client.respond("Say hello", timeout=0.1)


@pytest.mark.asyncio
async def test_auto_approve_confirm():
    events = [
        {"type": "agent_start"},
        {"type": "extension_ui_request", "id": "req-1", "method": "confirm", "title": "Allow?"},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "OK"}},
        {"type": "agent_settled"},
    ]
    proc = FakeProcess([json.dumps(e) for e in events])
    client = PiRPCClient(cwd="/tmp", no_session=True)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await client.respond("Do something dangerous")

    assert result == "OK"
    calls = [call for call in proc.stdin.write.call_args_list
             if json.loads(call[0][0])["type"] != "get_state"]
    # First call is prompt, second should be extension_ui_response
    assert len(calls) == 2
    response = json.loads(calls[1][0][0].decode("utf-8"))
    assert response["type"] == "extension_ui_response"
    assert response["confirmed"] is True


@pytest.mark.asyncio
async def test_auto_approve_select():
    events = [
        {"type": "agent_start"},
        {"type": "extension_ui_request", "id": "req-2", "method": "select", "title": "Pick one", "options": ["A", "B"]},
        {"type": "agent_settled"},
    ]
    proc = FakeProcess([json.dumps(e) for e in events])
    client = PiRPCClient(cwd="/tmp", no_session=True)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await client.respond("Pick")

    calls = [call for call in proc.stdin.write.call_args_list
             if json.loads(call[0][0])["type"] != "get_state"]
    response = json.loads(calls[1][0][0].decode("utf-8"))
    assert response["value"] == "A"


@pytest.mark.asyncio
async def test_prompt_streaming_on_event_fires():
    events = [
        {"type": "agent_start"},
        {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0, "delta": "hmm"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "Hi there."}},
        {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash", "args": {}},
        {"type": "agent_settled"},
    ]
    proc = FakeProcess([json.dumps(e) for e in events])
    client = PiRPCClient(cwd="/tmp", no_session=True)

    seen: list[str] = []
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        async for _ in client.respond_streaming("Hi", on_event=seen.append):
            pass

    semantic = [event for event in seen if not event.startswith("workbench:")]
    visual = [
        json.loads(event.removeprefix("workbench:"))
        for event in seen
        if event.startswith("workbench:")
        and json.loads(event.removeprefix("workbench:")).get("kind") != "latency"
    ]
    assert semantic == ["thinking", "text", "tool", "tool:bash"]
    assert [event["kind"] for event in visual] == ["thinking", "text", "tool_start"]
    assert visual[1]["text"] == "Hi there."
    assert visual[2]["name"] == "bash"


@pytest.mark.asyncio
async def test_tool_progress_streams_partial_result_without_repeating_snapshots():
    events = [
        {"type": "agent_start"},
        {
            "type": "tool_execution_start",
            "toolCallId": "c1",
            "toolName": "bash",
            "args": {"command": "printf 'one\\ntwo\\n'"},
        },
        {
            "type": "tool_execution_update",
            "toolCallId": "c1",
            "partialResult": {"content": [{"type": "text", "text": "one\n"}]},
        },
        {
            "type": "tool_execution_update",
            "toolCallId": "c1",
            "partialResult": {"content": [{"type": "text", "text": "one\ntwo\n"}]},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "c1",
            "toolName": "bash",
            "result": {"content": [{"type": "text", "text": "one\ntwo\n"}]},
            "isError": False,
        },
        {"type": "agent_settled"},
    ]
    proc = FakeProcess([json.dumps(e) for e in events])
    client = PiRPCClient(cwd="/tmp", no_session=True)

    seen: list[str] = []
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        async for _ in client.respond_streaming("Run it", on_event=seen.append):
            pass

    visual = [
        json.loads(event.removeprefix("workbench:"))
        for event in seen
        if event.startswith("workbench:")
        and json.loads(event.removeprefix("workbench:")).get("kind") != "latency"
    ]
    assert [event["kind"] for event in visual] == [
        "tool_start",
        "tool_update",
        "tool_update",
        "tool_result",
    ]
    assert visual[1]["content"] == "one\n"
    assert visual[2]["content"] == "two\n"
    assert visual[3]["content"] == ""
    assert visual[3]["status"] == "done"


def test_extract_speakable_text_strips_code():
    text = "Here is code:\n```python\nprint(1)\n```\nDone."
    assert _extract_speakable_text(text) == "Here is code: Done."


def test_extract_speakable_text_strips_headings_and_hashes():
    from sleeptalk.text import shape_voice_reply

    assert shape_voice_reply("# Hello there.") == "Hello there."
    assert "95ffb75" not in shape_voice_reply(
        "Landed 95ffb75ea43193c2dc6a8dd1eaa86cce2f975c5e in text.py."
    )
    assert shape_voice_reply("**Bold** heading") == "Bold heading"
    assert shape_voice_reply("1.") == ""
    assert "defaced" in shape_voice_reply("I defaced the landing copy.")
    assert "1234567" in shape_voice_reply("Ticket 1234567 is open.")
    spoken = _extract_speakable_text(
        "# Summary\nI landed the voice filter in text.py. 95ffb75ea43193c2dc6a8dd1eaa86cce2f975c5e"
    )
    assert "hash" not in spoken.lower()
    assert "#" not in spoken
    assert "text.py" in spoken or "text.py" in spoken.replace(" ", "")


def test_extract_speakable_text_keeps_inline_code_content():
    text = "I updated `main.py` for you."
    assert _extract_speakable_text(text) == "I updated main.py for you."


def test_extract_sentence_splits_on_terminator():
    sentence, remainder = _extract_sentence("First one. Then two.")
    assert sentence == "First one."
    assert remainder == "Then two."


def test_extract_sentence_incomplete_returns_empty():
    sentence, remainder = _extract_sentence("no terminator yet")
    assert sentence == ""
    assert remainder == "no terminator yet"


def test_extract_sentence_does_not_split_on_file_extensions():
    sentence, remainder = _extract_sentence(
        "curl is in lib/content.ts:3 and appears twice on the page."
    )
    assert sentence == "curl is in lib/content.ts:3 and appears twice on the page."
    assert remainder == ""


def test_stream_filter_strips_fence_across_deltas():
    f = _SpeakableStreamFilter()
    out = f.feed("Before ``")
    out += f.feed("`python\nx = 1\n```")
    out += f.feed(" After.")
    assert out == "Before  After."


def test_stream_filter_keeps_inline_code_content():
    f = _SpeakableStreamFilter()
    assert f.feed("I updated `main.py` now.") == "I updated main.py now."


@pytest.mark.asyncio
async def test_prompt_streaming_yields_sentences_incrementally():
    events = [
        {"type": "agent_start"},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "First sentence. Sec"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "ond one!"}},
        {"type": "agent_settled"},
    ]
    proc = FakeProcess([json.dumps(e) for e in events])
    client = PiRPCClient(cwd="/tmp", no_session=True)

    sentences = []
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        async for sentence in client.respond_streaming("Hi"):
            sentences.append(sentence)

    assert sentences == ["First sentence.", "Second one!"]


@pytest.mark.asyncio
async def test_prompt_streaming_flushes_first_clause_early():
    events = [
        {"type": "agent_start"},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "Let me pull up the latency logs for this turn, and then we"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": " can see exactly where the time went."}},
        {"type": "agent_settled"},
    ]
    proc = FakeProcess([json.dumps(e) for e in events])
    client = PiRPCClient(cwd="/tmp", no_session=True)

    sentences = []
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        async for sentence in client.respond_streaming("Hi"):
            sentences.append(sentence)

    assert sentences[0] == "Let me pull up the latency logs for this turn,"
    assert sentences[1].endswith("time went.")


@pytest.mark.asyncio
async def test_prompt_streaming_short_first_sentence_not_split():
    events = [
        {"type": "agent_start"},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "Sure, one sec."}},
        {"type": "agent_settled"},
    ]
    proc = FakeProcess([json.dumps(e) for e in events])
    client = PiRPCClient(cwd="/tmp", no_session=True)

    sentences = []
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        async for sentence in client.respond_streaming("Hi"):
            sentences.append(sentence)

    assert sentences == ["Sure, one sec."]


@pytest.mark.asyncio
async def test_abort_interrupts_streaming_prompt():
    # agent_start arrives but agent_settled never does; abort must wake the loop
    events = [
        {"type": "agent_start"},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "Working on it."}},
    ]

    class HangingProcess(FakeProcess):
        async def _read_stdout(self, n: int) -> bytes:
            if self._stdout_idx >= len(self.stdout_lines):
                await asyncio.sleep(60)
                return b""
            return await super()._read_stdout(n)

    proc = HangingProcess([json.dumps(e) for e in events])
    client = PiRPCClient(cwd="/tmp", no_session=True)

    sentences = []
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        async def run():
            async for sentence in client.respond_streaming("Hi", timeout=30.0):
                sentences.append(sentence)

        task = asyncio.create_task(run())
        await asyncio.sleep(0.3)
        await client.abort()
        await asyncio.wait_for(task, timeout=5.0)

    assert sentences == ["Working on it."]


def test_extract_speakable_text_empty_fallback():
    text = "```python\nprint(1)\n```"
    assert _extract_speakable_text(text) == "I've made the changes. Let me know if you need anything else."


def _assistant(text: str = "", *, error: str = "") -> dict:
    message = {"role": "assistant", "content": [{"type": "text", "text": text}] if text else []}
    if error:
        message["stopReason"] = "error"
        message["errorMessage"] = error
    return message


async def _omp_answer(events: list[dict]) -> str:
    proc = FakeProcess([json.dumps(e) for e in events])
    client = OmpRPCClient(cwd="/tmp", no_session=True, pi_bin="/bin/sh")
    try:
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            return await client.respond("what's new in the markets?")
    finally:
        await client.close()


def _tool_turn_then(*after: dict) -> list[dict]:
    return [
        {"type": "agent_start"},
        {"type": "tool_execution_start", "toolName": "web_search", "toolCallId": "c1", "args": {}},
        {"type": "tool_execution_end", "toolName": "web_search", "toolCallId": "c1",
         "result": {"content": [{"type": "text", "text": "..."}]}},
        *after,
    ]


@pytest.mark.asyncio
async def test_omp_error_it_retries_does_not_fail_the_turn():
    failed = _assistant(error="429 Provider returned error: rate-limited upstream")
    answer = _assistant("Stocks fell today.")
    events = _tool_turn_then(
        {"type": "message_end", "message": failed},
        {"type": "agent_end", "messages": [failed], "isTerminal": False},
        {"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3},
        {"type": "agent_start"},
        {"type": "message_update",
         "assistantMessageEvent": {"type": "text_delta", "delta": "Stocks fell today."}},
        {"type": "message_end", "message": answer},
        {"type": "auto_retry_end", "success": True, "attempt": 1},
        {"type": "agent_end", "messages": [answer], "isTerminal": True},
    )
    assert "Stocks fell today." in await _omp_answer(events)


@pytest.mark.asyncio
async def test_omp_retries_used_up_fails_with_the_final_error():
    failed = _assistant(error="429 rate-limited")
    events = _tool_turn_then(
        {"type": "message_end", "message": failed},
        {"type": "agent_end", "messages": [failed], "isTerminal": False},
        {"type": "auto_retry_start", "attempt": 1, "maxAttempts": 1},
        {"type": "auto_retry_end", "success": False, "finalError": "429 still rate-limited"},
    )
    with pytest.raises(PiRPCError, match="still rate-limited"):
        await _omp_answer(events)


@pytest.mark.asyncio
async def test_omp_error_it_does_not_retry_fails_when_the_run_ends():
    failed = _assistant(error="400 context length exceeded\nraw-http-request=/tmp/x.json")
    events = _tool_turn_then(
        {"type": "message_end", "message": failed},
        {"type": "agent_end", "messages": [failed], "isTerminal": True},
    )
    with pytest.raises(PiRPCError, match="^400 context length exceeded$"):
        await _omp_answer(events)
