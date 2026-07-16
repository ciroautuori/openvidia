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

import json
import time
import uuid
from typing import Any, AsyncGenerator

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse


from .proxy_state import ProxyState


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
    effective_model = model_override or "deepseek-ai/deepseek-v4-pro"

    payload: dict[str, Any] = {
        "model": effective_model,
        "messages": messages,
    }

    # Tools translation.
    tools = body.get("tools", [])
    if tools:
        payload["tools"] = _tools_to_chat_tools(tools)

    # Pass-through optional parameters (only those compatible with chat/completions).
    for key in (
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "stream",
    ):
        val = body.get(key)
        if val is not None:
            payload[key] = val

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

    # Usage mapping (prompt/completion → input/output/total).
    usage = chat_data.get("usage", {})
    resp_usage = {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }

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

    # Key rotation (same pattern as the catch-all proxy).
    resp = None
    used_key = None
    used_idx = None
    for idx, k in candidates:
        if not state.key_can_send_rpm(k):
            continue
        hdrs = {
            "Authorization": f"Bearer {k}",
            "Content-Type": "application/json",
            "User-Agent": "openvidia/2.0",
        }
        try:
            req = client.build_request(
                "POST", upstream, json=chat_payload, headers=hdrs
            )
            resp = await client.send(req, stream=True)
        except httpx.ReadTimeout:
            state.log_cb(
                f"  responses shim: key[{idx}] ReadTimeout (rotating, cooldown 30s)"
            )
            state.mark_key_failed(k)
            continue
        except httpx.HTTPError as e:
            err_msg = str(e) or type(e).__name__
            state.log_cb(
                f"  responses shim: key[{idx}] {err_msg} (rotating, cooldown 30s)"
            )
            state.mark_key_failed(k)
            continue

        if resp.status_code == 200:
            used_key = k
            used_idx = idx
            break

        # Non-200: rotate to the next key.
        err_status = resp.status_code
        await resp.aread()
        await resp.aclose()
        resp = None
        state.log_cb(f"  responses shim: key[{idx}] HTTP {err_status}")
        state.mark_key_failed(k, status=err_status)

    if resp is None or used_key is None:
        # All keys exhausted on the primary model → try a preset fallback model.
        from .proxy_app import _get_fallback_model

        fb_model = _get_fallback_model(state, model)
        if fb_model and fb_model != model:
            state.log_cb(
                f"  responses shim: all keys failed for {model}, fallback to {fb_model}"
            )
            chat_payload["model"] = fb_model
            for idx, k in candidates:
                if not state.key_can_send_rpm(k):
                    continue
                hdrs = {
                    "Authorization": f"Bearer {k}",
                    "Content-Type": "application/json",
                    "User-Agent": "openvidia/2.0",
                }
                try:
                    req = client.build_request(
                        "POST", upstream, json=chat_payload, headers=hdrs
                    )
                    resp = await client.send(req, stream=True)
                except httpx.ReadTimeout:
                    state.mark_key_failed(k)
                    continue
                except httpx.HTTPError:
                    state.mark_key_failed(k)
                    continue
                if resp.status_code == 200:
                    used_key = k
                    used_idx = idx
                    model = fb_model
                    break
                err_status = resp.status_code
                await resp.aread()
                await resp.aclose()
                resp = None
                state.log_cb(f"  responses shim fallback: key[{idx}] HTTP {err_status}")
                state.mark_key_failed(k, status=err_status)

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

    # Final response.completed event.
    yield _sse_event(
        "response.completed",
        {
            "type": "response.completed",
            "response": {
                "id": resp_id,
                "object": "response",
                "created_at": created_ts,
                "model": model,
                "output": [],
                "status": "completed",
            },
        },
    )

    await resp.aclose()


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
    for idx, k in candidates:
        if not state.key_can_send_rpm(k):
            continue
        hdrs = {
            "Authorization": f"Bearer {k}",
            "Content-Type": "application/json",
            "User-Agent": "openvidia/2.0",
        }
        try:
            req = client.build_request(
                "POST", upstream, json=chat_payload, headers=hdrs
            )
            resp = await client.send(req)
        except httpx.ReadTimeout:
            state.log_cb(
                f"  responses shim: key[{idx}] ReadTimeout (rotating, cooldown 30s)"
            )
            state.mark_key_failed(k)
            continue
        except httpx.HTTPError as e:
            err_msg = str(e) or type(e).__name__
            state.log_cb(
                f"  responses shim: key[{idx}] {err_msg} (rotating, cooldown 30s)"
            )
            state.mark_key_failed(k)
            continue
        if resp.status_code == 200:
            used_key = k
            used_idx = idx
            break
        err_status = resp.status_code
        await resp.aclose()
        resp = None
        state.log_cb(f"  responses shim: key[{idx}] HTTP {err_status}")
        state.mark_key_failed(k, status=err_status)
        continue

    if resp is None or used_key is None:
        return JSONResponse({"error": "all keys failed"}, status_code=503)

    state.stats.success += 1
    state.stats.record_key_usage(used_key, ok=True)
    state.record_request(used_key)
    if used_idx is not None:
        state.log_cb(f"✔ key[{used_idx}] OK")

    chat_data = resp.json()
    await resp.aclose()

    responses_data = _chat_response_to_responses(
        chat_data, model_override or chat_payload["model"]
    )
    return JSONResponse(responses_data)
