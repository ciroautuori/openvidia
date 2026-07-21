"""
Responses API → chat/completions shim.

Translates /v1/responses (the API used by Codex CLI) into
/v1/chat/completions, which the proxy already knows how to forward to NVIDIA
NIM. The translation is bidirectional:

  - request:  input (string | items[])        → messages[]
  - response: chat completion                  → output items (text, function_call)
  - streaming: SSE chat chunks                  → SSE Responses events
  - tools:    function definitions             → chat tools, and back

No abstractions — just payload translation. All names, signatures and control
flow are frozen; only prose (docstrings/comments) is reformatted.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, AsyncGenerator

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse


from . import config
from .proxy_state import ProxyState


# ── Bounded rotation + saturation fast-fail ─────────────────────────────
# Codex CLI blocks historically when /v1/responses rotated serially across
# ALL 25 candidate keys with no per-attempt timeout (client default read=
# 120s): 25 × 120s = up to 50 minutes before returning 503. We now cap the
# number of keys we actually send to per rotation phase (the weighted
# least-loaded ordering already puts the best keys first), give each send a
# bounded connect+read+write+pool timeout, and fast-fail the whole loop as
# soon as the live (RPM-eligible) pool is too small to bother.
# Upstream gateway timeouts: the provider's edge gave up waiting for the
# model. Every key would hit the same wall, so these must not be charged to
# the key that happened to carry the request.
_GATEWAY_TIMEOUTS = {502, 503, 504}

_MAX_ROTATE_ATTEMPTS = 5          # hard cap on sends per model phase
_ROTATE_SEND_TIMEOUT = httpx.Timeout(**config.httpx_timeout_kwargs())
_MIN_LIVE_FRACTION = 0.2          # <20% of valid keys live → 503 fast


# A reasoning model withholds its first byte until it has finished thinking —
# measured at 117-144s for z-ai/glm-5.2 on the NVIDIA free tier, for prompts
# of any size. During that window the SSE socket carries nothing, and a client
# (or any proxy in between) cannot tell a thinking model from a dead
# connection. Emit an SSE comment periodically: comments are stripped by the
# SSE parser before events are dispatched, so the client's protocol handling
# never sees them — the bytes exist purely to keep the stream demonstrably
# alive.
_KEEPALIVE_INTERVAL = 10.0
_KEEPALIVE_BYTES = b": keepalive\n\n"


async def _keepalive_until(task, result_box: list):
    """Yield keepalive comments until ``task`` finishes; append its result."""
    while True:
        try:
            result_box.append(
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=_KEEPALIVE_INTERVAL
                )
            )
            return
        except asyncio.TimeoutError:
            yield _KEEPALIVE_BYTES
        except Exception:  # noqa: BLE001 — surfaced to the caller via the box
            result_box.append((None, None, None))
            return


async def _rotation_phase(client, upstream, payload, headers_factory,
                          state, candidates, *, max_attempts, timeout,
                          stream, log_tag, seen_429_box):
    """Single bounded rotation phase. Returns (resp_or_None, used_key, used_idx)."""
    attempts = 0
    for idx, k in candidates:
        if not state.key_can_send_rpm(k):
            continue
        if attempts >= max_attempts:
            break
        attempts += 1
        hdrs = headers_factory(k, idx)
        # Claim the key BEFORE sending. get_candidate_keys() scores a key by
        # in_flight + recent RPM, and neither is set until a request finishes:
        # without this claim, N requests arriving together all score every key
        # at zero, tie-break on index, and pile onto key[0] while the rest of
        # the pool idles. That is how a 26-key pool produces 429s.
        state.begin_in_flight(k)
        released = False
        resp = None
        try:
            req = client.build_request("POST", upstream, json=payload, headers=hdrs, timeout=timeout)
            resp = await client.send(req, stream=stream)
        except httpx.ReadTimeout:
            # The key connected and the upstream ACCEPTED the request — it is
            # simply still thinking. Two things must NOT happen here:
            #   - cooling the key down, which blames 25 healthy keys for one
            #     slow model and drains the pool
            #   - rotating, since the next key runs the same model and will
            #     wait exactly as long
            # Stop the phase instead and let the caller fall back to a
            # different (faster) model, which is the real escalation.
            state.log_cb(
                f"  {log_tag}: key[{idx}] no first byte in {timeout.read:.0f}s "
                f"— model too slow, not a key fault"
            )
            state.end_in_flight(k)
            released = True
            break
        except httpx.HTTPError as e:
            err_msg = str(e) or type(e).__name__
            state.log_cb(f"  {log_tag}: key[{idx}] {err_msg} (rotating)")
            state.end_in_flight(k)
            released = True
            state.mark_key_failed(k)
            continue
        finally:
            # A 200 keeps the claim: the key stays busy for as long as the
            # stream runs, and the caller releases it when the body ends.
            if not released and (resp is None or resp.status_code != 200):
                state.end_in_flight(k)
        if resp.status_code == 200:
            return resp, k, idx
        err_status = resp.status_code
        # Read error body for detailed logging before closing
        error_body = ""
        try:
            error_body = await resp.aread()
            error_body = error_body.decode('utf-8', errors='replace')[:500]
        except Exception:
            pass
        await resp.aclose()
        state.log_cb(f"  {log_tag}: key[{idx}] HTTP {err_status}")
        if err_status == 429:
            seen_429_box[0] = True
        # A gateway timeout is the MODEL being slow, not the key being bad:
        # NVIDIA's edge gave up waiting for it, and every other key would hit
        # the same wall. Cooling keys down for it empties the pool one 504 at
        # a time, which is precisely what a 26-key pool exists to prevent.
        if err_status in _GATEWAY_TIMEOUTS:
            state.log_cb(
                f"  {log_tag}: HTTP {err_status} is an upstream gateway timeout "
                f"— key[{idx}] left healthy"
            )
            continue
        state.mark_key_failed(k, status=err_status, error_body=error_body if error_body else None)
    return None, None, None


def _live_pool_snapshot(state, candidates):
    """Return ``(live, total_pool)`` for saturation gating.

    ``live`` = candidates that are cooldown-free AND RPM-eligible right now
    (the set a rotation loop could actually succeed on). ``total_pool`` is
    the FULL pool size (including cooldown keys) — weighing by the full pool
    rather than just `len(candidates)` is what makes the saturation gate
    fire correctly: when most of the 25 keys are on cooldown, the live
    fraction genuinely drops below threshold, which is exactly when we want
    to skip rotation and go to model-fallback / 503 instead of serially
    hammering the few surviving keys.
    """
    live = sum(1 for _, k in candidates if state.key_can_send_rpm(k) and not state.is_key_on_cooldown(k))
    total_pool = len(state.keys)
    return live, total_pool



# ── Defensive sanitization of chat/completions messages ────────────────
def _sanitize_chat_messages(messages: list[dict]) -> list[dict]:
    """
    Guarantee every message is a valid OpenAI chat/completions message so
    upstream never rejects a request with::

        data did not match any variant of untagged enum
        ChatCompletionRequestToolMessageContent

    Coercion rules:
      - content is forced to str. Lists are flattened to text; dicts are
        JSON-encoded; None becomes "" (or " " for tool/user fallbacks).
      - tool messages require a non-null string content and a tool_call_id;
        if the caller forgot the id we synthesize one (NVIDIA enforces the
        match against a prior assistant tool_calls entry).
      - assistant messages carrying tool_calls get content coerced to ""
        (never None) — NVIDIA NIM rejects content:null on non-tool messages.
      - Unknown roles are dropped silently.
    """
    out: list[dict] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            continue
        content = m.get("content")
        tool_calls = m.get("tool_calls")

        # Flatten array-shaped content to a text string. Sources include
        # tool_call_output arrays, Codex content parts, and multimodal
        # payloads we reduce to plain text.
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    if part.get("type") in ("text", "input_text", "output_text"):
                        parts.append(str(part.get("text", "")))
                    else:
                        parts.append(json.dumps(part, ensure_ascii=False))
            content = chr(10).join(p for p in parts if p)
        elif isinstance(content, dict):
            content = json.dumps(content, ensure_ascii=False)
        elif content is None:
            content = ""
        elif not isinstance(content, str):
            content = str(content)

        if role == "tool":
            tcid = m.get("tool_call_id") or ""
            if not tcid:
                # No matching tool_call_id → synthesize one so the upstream
                # schema validator accepts the tool response.
                tcid = f"call_{uuid.uuid4().hex[:24]}"
            if not content:
                content = " "
            out.append({"role": "tool", "tool_call_id": tcid, "content": content})
            continue

        if role == "assistant" and tool_calls:
            # NVIDIA NIM rejects content: null on assistant messages; use "".
            clean_calls = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                args = fn.get("arguments", "")
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                if not args:
                    args = "{}"
                clean_calls.append(
                    {
                        "id": tc.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                        "type": "function",
                        "function": {
                            "name": fn.get("name", ""),
                            "arguments": args,
                        },
                    }
                )
            if not clean_calls:
                # Malformed tool_calls: degrade to a plain assistant text turn.
                out.append({"role": "assistant", "content": content or " "})
                continue
            out.append(
                {
                    "role": "assistant",
                    "content": content or "",
                    "tool_calls": clean_calls,
                }
            )
            continue

        out.append({"role": role, "content": content or " "})
    return out


# ── Request: Responses input → chat/completions messages ─────────────


def _input_to_messages(input_data: Any) -> list[dict]:
    """
    Convert the Responses API `input` field into chat/completions messages.

    `input` is either a bare string (→ single user message) or an array of
    InputItems. Codex CLI sends messages with role="developer" (mapped to
    "system") and content parts with type="input_text" (mapped to plain text).
    """
    if isinstance(input_data, str):
        return [{"role": "user", "content": input_data}]

    messages: list[dict] = []
    for item in input_data:
        typ = item.get("type", "message")

        if typ == "message":
            role = item.get("role", "user")
            # Codex uses "developer" → remap to "system" for chat/completions.
            if role == "developer":
                role = "system"
            content = item.get("content", "")
            # content may be a string or an array of content parts.
            if isinstance(content, list):
                # Codex uses type="input_text"; OpenAI standard uses type="text".
                text_parts = [
                    p.get("text", "")
                    for p in content
                    if p.get("type") in ("text", "input_text")
                ]
                content = "\n".join(text_parts)
            messages.append({"role": role, "content": content})

        elif typ == "function_call_output":
            # Result of a prior tool call — emit as a tool message.
            call_id = item.get("call_id", "")
            output = item.get("output", "")
            if isinstance(output, dict):
                output = json.dumps(output)
            messages.append(
                {"role": "tool", "tool_call_id": call_id, "content": str(output)}
            )

        elif typ == "function_call":
            # A function call from a previous turn — reconstruct as an
            # assistant message with tool_calls so context is preserved.
            name = item.get("name", "")
            arguments = item.get("arguments", "")
            call_id = item.get("call_id", "")
            messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                }
            )
        # Unknown item types are ignored — forward compatibility.

    return messages


def _tools_to_chat_tools(tools: list[dict]) -> list[dict]:
    """
    Convert Responses tools into chat/completions tools[].

    Codex CLI sends two function shapes:
      - flat:    {type:"function", name:"x", description:"...", parameters:{...}}
      - nested:  {type:"function", function:{name:"x", ...}}

    Non-function types (namespace, web_search, image_generation) are filtered
    out because NVIDIA NIM does not understand them.
    """
    chat_tools = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        # Flat shape (Codex): name and parameters at the top level.
        if "name" in tool:
            chat_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {}),
                    },
                }
            )
        # Nested shape (OpenAI standard).
        elif "function" in tool:
            fn = tool["function"]
            chat_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": fn.get("name", ""),
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {}),
                    },
                }
            )
    return chat_tools


def _build_chat_payload(body: dict, model_override: str | None) -> dict:
    """Build the chat/completions payload from a Responses request body."""
    messages = []

    # Instructions (system prompt) → leading system message.
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})

    messages.extend(_input_to_messages(body.get("input", "")))

    # model_override (from state.active_model) takes precedence; otherwise use
    # the NVIDIA default — never forward "openvidia/openvidia" to NVIDIA.
    # No hardcoded model: resolved live from the user's selection.
    from .proxy_app import default_model
    effective_model = model_override or default_model()

    payload: dict[str, Any] = {
        "model": effective_model,
        "messages": messages,
    }

    # Tools translation.
    tools = body.get("tools", [])
    if tools:
        payload["tools"] = _tools_to_chat_tools(tools)

    # tool_choice translation (Responses → chat/completions).
    # Responses uses: "auto" | "none" | "required" | {type:"function", name}
    tc = body.get("tool_choice")
    if tc is not None:
        if isinstance(tc, str):
            payload["tool_choice"] = tc
        elif isinstance(tc, dict):
            if tc.get("type") == "auto":
                payload["tool_choice"] = "auto"
            elif tc.get("type") == "none":
                payload["tool_choice"] = "none"
            elif tc.get("type") == "required":
                payload["tool_choice"] = "required"
            elif tc.get("type") == "function":
                payload["tool_choice"] = {
                    "type": "function",
                    "function": {"name": tc.get("name", "")},
                }

    # parallel_tool_calls passthrough (Codex sends it).
    if "parallel_tool_calls" in body:
        payload["parallel_tool_calls"] = body["parallel_tool_calls"]

    # metadata passthrough (Codex may attach; NVIDIA ignores unknown fields).
    if body.get("metadata"):
        payload["metadata"] = body["metadata"]

    # stop sequences passthrough.
    if body.get("stop"):
        payload["stop"] = body["stop"]

    # stream_options: Codex sends {"include_usage": true} on streaming
    # requests so the final SSE chunk carries token usage. NVIDIA NIM
    # accepts stream_options.include_usage.
    if body.get("stream_options"):
        payload["stream_options"] = body["stream_options"]

    # Pass-through optional parameters (only those compatible with chat/completions).
    for key in (
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "stream",
        "seed",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "user",
    ):
        val = body.get(key)
        if val is not None:
            payload[key] = val

    # request-level response format: Codex may ask for {"type":"text"} or
    # {"type":"json_object"}; forward to NVIDIA if present.
    if body.get("response_format"):
        payload["response_format"] = body["response_format"]

    payload["messages"] = _sanitize_chat_messages(payload["messages"])
    return payload


# ── Response: chat/completions → Responses output ────────────────────


def _chat_response_to_responses(chat_data: dict, model: str) -> dict:
    """Translate a non-streaming chat/completions response into Responses format."""
    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    created = chat_data.get("created", int(time.time()))

    output: list[dict] = []
    choice = (chat_data.get("choices") or [{}])[0]
    msg = choice.get("message", {})

    # Tool calls → function_call items.
    tool_calls = msg.get("tool_calls", [])
    if tool_calls:
        for tc in tool_calls:
            fn = tc.get("function", {})
            output.append(
                {
                    "type": "function_call",
                    "id": f"fc_{uuid.uuid4().hex[:24]}",
                    "call_id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", ""),
                }
            )

    # Text content → message item with a content array (output_text).
    text = msg.get("content")
    if text:
        output.append(
            {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex[:24]}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            }
        )

    # Status derived from finish_reason.
    finish = choice.get("finish_reason", "stop")
    status = "completed" if finish == "stop" else "incomplete"

    # Usage mapping (prompt/completion → input/output/total). NVIDIA may
    # return completion_tokens_details to pass through if present.
    usage = chat_data.get("usage", {})
    resp_usage = {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }
    ctd = usage.get("completion_tokens_details")
    if isinstance(ctd, dict) and ctd:
        resp_usage["output_tokens_details"] = ctd
    ptd = usage.get("prompt_tokens_details")
    if isinstance(ptd, dict) and ptd:
        resp_usage["input_tokens_details"] = ptd

    return {
        "id": resp_id,
        "object": "response",
        "created_at": created,
        "model": model,
        "output": output,
        "status": status,
        "usage": resp_usage,
    }


# ── Streaming: SSE chat chunks → SSE Responses events ─────────────────


def _sse_event(event_type: str, data: dict) -> bytes:
    """Serialize a single SSE event for the Responses streaming protocol."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()


async def _stream_responses(
    state: ProxyState,
    chat_payload: dict,
    model: str,
    client,
    request: Request,
) -> AsyncGenerator[bytes, None]:
    """
    Forward chat/completions with stream:true to NVIDIA and re-translate the
    SSE chat chunks into Responses-protocol SSE events.
    """
    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    created_ts = int(time.time())

    # Opening events: response.created then response.in_progress (Codex
    # expects both before any output).
    yield _sse_event(
        "response.created",
        {
            "type": "response.created",
            "response": {
                "id": resp_id,
                "object": "response",
                "created_at": created_ts,
                "model": model,
                "output": [],
                "status": "in_progress",
            },
        },
    )
    yield _sse_event(
        "response.in_progress",
        {
            "type": "response.in_progress",
            "response": {
                "id": resp_id,
                "object": "response",
                "created_at": created_ts,
                "model": model,
                "output": [],
                "status": "in_progress",
            },
        },
    )

    upstream = "https://integrate.api.nvidia.com/v1/chat/completions"

    # Candidate keys resolved inside the generator, not before — avoids
    # concurrent requests sharing a single precomputed list.
    async with state.lock:
        candidates = state.get_candidate_keys()

    # Key rotation: bounded attempts + saturation fast-fail (was serial
    # across ALL candidates with the client default 120s read → Codex block).
    resp = None
    used_key = None
    used_idx = None

    _live, _valid = _live_pool_snapshot(state, candidates)
    if _valid and _live < max(1, int(_valid * _MIN_LIVE_FRACTION)):
        state.log_cb(
            f"  responses shim: pool saturated ({_live}/{_valid} live) → skip primary "
            f"(fallback/503)"
        )
    else:

        def _hdr(k, idx):
            return {"Authorization": f"Bearer {k}", "Content-Type": "application/json", "User-Agent": "openvidia/2.0"}

        _box: list = []
        _task = asyncio.ensure_future(
            _rotation_phase(
                client, upstream, chat_payload, _hdr, state, candidates,
                max_attempts=_MAX_ROTATE_ATTEMPTS,
                timeout=_ROTATE_SEND_TIMEOUT,
                stream=True, log_tag="responses shim",
                seen_429_box=[False],
            )
        )
        # No deadline and no model substitution: the selected model is THE
        # model. We wait for it (keepalives keep the stream alive meanwhile)
        # and never silently answer from a different one.
        async for _ka in _keepalive_until(_task, _box):
            yield _ka
        resp, used_key, used_idx = _box[0]

    if resp is None or used_key is None:
        # Definitive failure: emit error + response.failed and terminate.
        yield _sse_event(
            "error",
            {
                "type": "error",
                "code": "server_error",
                "message": "all keys failed",
                "param": None,
            },
        )
        yield _sse_event(
            "response.failed",
            {
                "type": "response.failed",
                "response": {
                    "id": resp_id,
                    "object": "response",
                    "created_at": created_ts,
                    "model": model,
                    "output": [],
                    "status": "failed",
                },
            },
        )
        return

    state.stats.success += 1
    state.stats.record_key_usage(used_key, ok=True)
    state.record_request(used_key)
    if used_idx is not None:
        state.log_cb(f"✔ key[{used_idx}] OK")

    # Accumulators for the streamed output. Codex (Responses API) uses item
    # type "message" with a content array of output_text parts — NOT item
    # type "text".
    text_item_id = f"msg_{uuid.uuid4().hex[:24]}"
    text_started = False
    text_full = ""
    # output_index: 0 = text (if any), then tool_calls in arrival order.
    tool_calls_map: dict[int, dict] = {}  # index → tool_call state
    next_output_index = 0

    async for line in resp.aiter_lines():
        if await request.is_disconnected():
            break
        if not line or not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str.strip() == "[DONE]":
            break

        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta", {})

        # Text delta → response.output_text.delta
        text_content = delta.get("content")
        if text_content:
            if not text_started:
                text_started = True
                text_output_index = next_output_index
                next_output_index += 1
                yield _sse_event(
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "output_index": text_output_index,
                        "item": {
                            "type": "message",
                            "id": text_item_id,
                            "role": "assistant",
                            "status": "in_progress",
                            "content": [],
                        },
                    },
                )
            yield _sse_event(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": text_item_id,
                    "output_index": text_output_index,
                    "delta": text_content,
                },
            )
            text_full += text_content

        # Tool call deltas → function_call item add + arguments deltas.
        tc_deltas = delta.get("tool_calls", [])
        for tc in tc_deltas:
            idx = tc.get("index", 0)
            if idx not in tool_calls_map:
                fc_id = f"fc_{uuid.uuid4().hex[:24]}"
                call_id = tc.get("id", f"call_{uuid.uuid4().hex[:24]}")
                tc_output_index = next_output_index
                next_output_index += 1
                tool_calls_map[idx] = {
                    "fc_id": fc_id,
                    "call_id": call_id,
                    "name": tc.get("function", {}).get("name", ""),
                    "arguments": "",
                    "output_index": tc_output_index,
                }
                yield _sse_event(
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "output_index": tc_output_index,
                        "item": {
                            "type": "function_call",
                            "id": fc_id,
                            "call_id": call_id,
                            "name": tc.get("function", {}).get("name", ""),
                            "arguments": "",
                            "status": "in_progress",
                        },
                    },
                )
            tcm = tool_calls_map[idx]
            args_delta = tc.get("function", {}).get("arguments", "")
            if args_delta:
                tcm["arguments"] += args_delta
                yield _sse_event(
                    "response.function_call_arguments.delta",
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": tcm["fc_id"],
                        "output_index": tcm["output_index"],
                        "delta": args_delta,
                    },
                )

        # Finish: close all open items, then emit response.completed.
        finish = choice.get("finish_reason")
        if finish:
            if text_started:
                yield _sse_event(
                    "response.output_text.done",
                    {
                        "type": "response.output_text.done",
                        "item_id": text_item_id,
                        "output_index": text_output_index,
                        "text": text_full,
                    },
                )
                yield _sse_event(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": text_output_index,
                        "item": {
                            "type": "message",
                            "id": text_item_id,
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "output_text", "text": text_full}],
                        },
                    },
                )
            for idx, tcm in tool_calls_map.items():
                yield _sse_event(
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": tcm["fc_id"],
                        "output_index": tcm["output_index"],
                        "arguments": tcm["arguments"],
                    },
                )
                yield _sse_event(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": tcm["output_index"],
                        "item": {
                            "type": "function_call",
                            "id": tcm["fc_id"],
                            "call_id": tcm["call_id"],
                            "name": tcm["name"],
                            "arguments": tcm["arguments"],
                            "status": "completed",
                        },
                    },
                )
            break

    # Capture usage from the terminal chunk (NVIDIA streams usage only when
    # stream_options.include_usage=true). If absent we report zeros — Codex
    # tolerates it but misses token accounting; we forward it when upstream
    # sends it.
    final_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    if isinstance(chunk, dict):
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            final_usage = {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }

    # Final response.completed event. A non-empty output array lets Codex
    # reconstruct the assistant message instead of relying on delta replay.
    final_output: list[dict] = []
    if text_started:
        final_output.append(
            {
                "type": "message",
                "id": text_item_id,
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text_full}],
            }
        )
    for tcm in tool_calls_map.values():
        final_output.append(
            {
                "type": "function_call",
                "id": tcm["fc_id"],
                "call_id": tcm["call_id"],
                "name": tcm["name"],
                "arguments": tcm["arguments"],
                "status": "completed",
            }
        )
    yield _sse_event(
        "response.completed",
        {
            "type": "response.completed",
            "response": {
                "id": resp_id,
                "object": "response",
                "created_at": created_ts,
                "model": model,
                "output": final_output,
                "status": "completed",
                "usage": final_usage,
            },
        },
    )

    await resp.aclose()
    # Release the load-balancer claim taken in _rotation_phase. Without this
    # the key looks permanently busy and the scheduler stops choosing it.
    state.end_in_flight(used_key)


# ── Entry point ─────────────────────────────────────────────────────────


async def handle_responses(
    request: Request,
    state: ProxyState,
    client,
) -> JSONResponse | StreamingResponse:
    """
    Handle POST /v1/responses.

    Forwards directly to NVIDIA chat/completions (this path is internal — it
    does not go through the catch-all proxy_app handler).
    """
    raw = await request.body()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    state.log_cb(
        f"  responses: model={body.get('model', '?')} stream={body.get('stream', False)} tools={len(body.get('tools', []))}"
    )

    model_override = state.active_model
    chat_payload = _build_chat_payload(body, model_override)

    # Auto-compaction: summarize oversized history in place (trim fallback).
    from .compaction import maybe_compact

    chat_payload["messages"] = await maybe_compact(
        chat_payload["messages"], state=state, client=client, log=state.log_cb
    )

    want_stream = body.get("stream", False)

    if want_stream:
        chat_payload["stream"] = True
        return StreamingResponse(
            _stream_responses(
                state,
                chat_payload,
                model_override or chat_payload["model"],
                client,
                request,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming: resolve candidates right before the HTTP loop.
    async with state.lock:
        candidates = state.get_candidate_keys()
    if not candidates:
        return JSONResponse({"error": "no keys available"}, status_code=503)

    upstream = "https://integrate.api.nvidia.com/v1/chat/completions"
    used_key = None
    used_idx = None
    resp = None

    # Bounded attempts + saturation fast-fail (was serial across all
    # candidates with the 120s client default → Codex block).
    _live, _valid = _live_pool_snapshot(state, candidates)
    if _valid and _live < max(1, int(_valid * _MIN_LIVE_FRACTION)):
        state.log_cb(
            f"  responses shim: pool saturated ({_live}/{_valid} live) → 503 fast"
        )
    else:

        def _hdr(k, idx):
            return {"Authorization": f"Bearer {k}", "Content-Type": "application/json", "User-Agent": "openvidia/2.0"}

        resp, used_key, used_idx = await _rotation_phase(
            client, upstream, chat_payload, _hdr, state, candidates,
            max_attempts=_MAX_ROTATE_ATTEMPTS,
            timeout=_ROTATE_SEND_TIMEOUT,
            stream=False, log_tag="responses shim",
            seen_429_box=[False],
        )
        if used_idx is not None and isinstance(resp, httpx.Response):
            await resp.aread()

    if resp is None or used_key is None:
        return JSONResponse({"error": "all keys failed (pool saturated)" if _live else "all keys failed"}, status_code=503)

    state.stats.success += 1
    state.stats.record_key_usage(used_key, ok=True)
    state.record_request(used_key)
    if used_idx is not None:
        state.log_cb(f"✔ key[{used_idx}] OK")

    chat_data = resp.json()
    await resp.aclose()
    state.end_in_flight(used_key)  # release the load-balancer claim

    responses_data = _chat_response_to_responses(
        chat_data, model_override or chat_payload["model"]
    )
    return JSONResponse(responses_data)
