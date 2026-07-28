"""Anthropic SSE translation: terminator correctness and claim release.

The shim had 704 lines and no tests. Two of the bugs covered here broke the
agent loop outright — the client received a tool_use and then an end_turn that
overwrote it, so the tool was never run.
"""

from __future__ import annotations

import json

import httpx
import pytest

from openvidia import anthropic_shim
from openvidia import proxy_state as ps

pytestmark = pytest.mark.asyncio


def make_state(keys=("nvapi-a", "nvapi-b")):
    return ps.ProxyState(
        keys=list(keys),
        stats=ps.ProxyStats(),
        index_path=None,
        log_cb=lambda _m: None,
    )


def sse(chunks: list[dict], done: bool = True) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    if done:
        body += "data: [DONE]\n\n"
    return body.encode()


class ScriptedClient:
    """Returns one streaming response built from a canned SSE body."""

    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status

    def build_request(self, method, url, **kw):
        return httpx.Request(method, url, headers=kw.get("headers") or {})

    async def send(self, req, stream=False):
        return httpx.Response(
            self.status,
            content=self.body,
            headers={"content-type": "text/event-stream"},
            request=req,
        )


class FakeRequest:
    def __init__(self, disconnect_after: int | None = None):
        self._calls = 0
        self._limit = disconnect_after

    async def is_disconnected(self) -> bool:
        self._calls += 1
        return self._limit is not None and self._calls > self._limit


async def collect(state, body, request=None, model="test/model"):
    client = ScriptedClient(body)
    events: list[tuple[str, dict]] = []
    raw = b""
    gen = anthropic_shim._stream_anthropic(
        state,
        {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        model,
        client,
        request or FakeRequest(),
    )
    async for piece in gen:
        raw += piece
    for block in raw.decode().split("\n\n"):
        if not block.strip():
            continue
        name = data = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if name and data:
            try:
                events.append((name, json.loads(data)))
            except json.JSONDecodeError:
                pass
    return events


def names(events):
    return [n for n, _ in events]


TOOL_CALL_STREAM = sse(
    [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "chatcmpl-tool-1",
                                "function": {"name": "Bash", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"cmd":"ls"}'}}]}}
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
)


# --------------------------------------------------------------------------- #
# The bug that stopped the agent loop
# --------------------------------------------------------------------------- #


async def test_tool_use_stop_reason_is_not_overwritten():
    """A second message_delta used to follow with stop_reason end_turn."""
    state = make_state()
    events = await collect(state, TOOL_CALL_STREAM)

    deltas = [d for n, d in events if n == "message_delta"]
    assert len(deltas) == 1, f"expected exactly one message_delta, got {len(deltas)}"
    assert deltas[0]["delta"]["stop_reason"] == "tool_use"


async def test_exactly_one_message_stop():
    state = make_state()
    events = await collect(state, TOOL_CALL_STREAM)
    assert names(events).count("message_stop") == 1


async def test_each_block_is_closed_exactly_once():
    state = make_state()
    events = await collect(state, TOOL_CALL_STREAM)
    stops = [d["index"] for n, d in events if n == "content_block_stop"]
    assert len(stops) == len(set(stops)), f"duplicate content_block_stop: {stops}"


async def test_two_tool_calls_do_not_repeat_the_terminator():
    """message_delta/message_stop sat inside the tool loop: N calls, N copies."""
    body = sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "t1",
                                    "function": {"name": "A", "arguments": "{}"},
                                },
                                {
                                    "index": 1,
                                    "id": "t2",
                                    "function": {"name": "B", "arguments": "{}"},
                                },
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    state = make_state()
    events = await collect(state, body)
    assert names(events).count("message_stop") == 1
    assert names(events).count("message_delta") == 1


# --------------------------------------------------------------------------- #
# Block indexing
# --------------------------------------------------------------------------- #


async def test_tool_only_reply_starts_at_index_zero():
    """next_block_index was hardcoded to 1, leaving a hole at content[0]."""
    state = make_state()
    events = await collect(state, TOOL_CALL_STREAM)
    starts = [d["index"] for n, d in events if n == "content_block_start"]
    assert starts == [0], f"first content block must be index 0, got {starts}"


async def test_text_then_tool_indices_are_contiguous():
    body = sse(
        [
            {"choices": [{"delta": {"content": "thinking"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "t1",
                                    "function": {"name": "A", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    state = make_state()
    events = await collect(state, body)
    starts = [d["index"] for n, d in events if n == "content_block_start"]
    assert starts == [0, 1]


# --------------------------------------------------------------------------- #
# Termination when upstream misbehaves
# --------------------------------------------------------------------------- #


async def test_stream_without_finish_reason_still_terminates():
    """No finish_reason and no tool calls used to emit no message_stop at all."""
    body = sse([{"choices": [{"delta": {"content": "hello"}}]}])
    state = make_state()
    events = await collect(state, body)
    assert names(events).count("message_stop") == 1
    assert names(events).count("message_delta") == 1


async def test_empty_stream_still_terminates():
    state = make_state()
    events = await collect(state, sse([]))
    assert "message_stop" in names(events)


async def test_stream_with_no_done_marker_terminates():
    body = sse([{"choices": [{"delta": {"content": "x"}}]}], done=False)
    state = make_state()
    events = await collect(state, body)
    assert names(events).count("message_stop") == 1


# --------------------------------------------------------------------------- #
# Usage
# --------------------------------------------------------------------------- #


async def test_usage_comes_from_upstream_not_from_a_character_count():
    body = sse(
        [
            {"choices": [{"delta": {"content": "hello world"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 34}},
        ]
    )
    state = make_state()
    events = await collect(state, body)
    delta = next(d for n, d in events if n == "message_delta")
    assert delta["usage"]["output_tokens"] == 34


# --------------------------------------------------------------------------- #
# In-flight claim release
# --------------------------------------------------------------------------- #


async def test_claim_is_released_on_a_normal_stream():
    state = make_state()
    await collect(state, TOOL_CALL_STREAM)
    assert all(state.key_states[k].in_flight == 0 for k in state.keys)


async def test_claim_is_released_when_the_client_disconnects_midway():
    """No try/finally meant a disconnect leaked the claim for the process life."""
    state = make_state()
    body = sse([{"choices": [{"delta": {"content": f"chunk{i}"}}]} for i in range(20)])

    await collect(state, body, request=FakeRequest(disconnect_after=2))

    assert all(state.key_states[k].in_flight == 0 for k in state.keys)


async def test_claim_is_released_when_the_generator_is_abandoned():
    """A client that stops reading closes the generator with GeneratorExit."""
    state = make_state()
    body = sse([{"choices": [{"delta": {"content": f"c{i}"}}]} for i in range(50)])
    client = ScriptedClient(body)

    gen = anthropic_shim._stream_anthropic(
        state,
        {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        "m",
        client,
        FakeRequest(),
    )
    async for _ in gen:
        break
    await gen.aclose()

    assert all(state.key_states[k].in_flight == 0 for k in state.keys)
