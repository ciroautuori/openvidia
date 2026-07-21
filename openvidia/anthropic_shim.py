"""
Anthropic Messages API → chat/completions shim.

Translates /v1/messages (the API used by Claude Code CLI) into
/v1/chat/completions, which the proxy already knows how to forward to NVIDIA
NIM. The translation is bidirectional:

  - request:   messages + system  → chat messages (system as leading message)
  - response:  chat completion     → Anthropic content blocks (text, tool_use)
  - streaming: SSE chat chunks     → SSE Anthropic events
  - tools:     Anthropic tool schema (input_schema) → OpenAI function (parameters)

This is a separate endpoint — it does not interfere with Claude Code's normal
operation pointing at api.anthropic.com. It activates only when the user
chooses to point ANTHROPIC_BASE_URL at localhost:1919.

No abstractions — just payload translation. All names, signatures and control
flow are frozen; only prose (docstrings/comments) is reformatted.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

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
                tcid = f"call_{uuid.uuid4().hex[:24]}"
            if not content:
                content = " "
            out.append({"role": "tool", "tool_call_id": tcid, "content": content})
            continue

        if role == "assistant" and tool_calls:
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


UPSTREAM = "https://integrate.api.nvidia.com/v1/chat/completions"

# 400/404 are deterministic on content (bad payload / nonexistent model):
# rotating keys cannot fix them and would only waste quota. Surface them
# directly to the client.
_CLIENT_ERR = {400, 404}
# Provider edge gave up waiting for the model — every key hits the same wall,
# so these must not be charged to the key that carried the request.
_GATEWAY_TIMEOUTS = {502, 503, 504}


def _extract_err(raw: bytes, status: int) -> str:
    """Best-effort extraction of a human-readable error message from an upstream error body."""
    try:
        d = json.loads(raw)
        m = d.get("error", {})
        if isinstance(m, dict):
            m = m.get("message")
        return str(m or d.get("detail") or raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace") if raw else f"HTTP {status}"


# ── Request: Anthropic Messages → chat/completions ──────────────────


def _anthropic_to_chat_messages(body: dict) -> list[dict]:
    """
    Convert Anthropic messages into chat/completions messages.

    Anthropic message content may be a string or an array of content blocks:
      - {type:"text", text:"..."}
      - {type:"image", source:{...}}            (dropped — NVIDIA has no vision)
      - {type:"tool_use", id, name, input}      → assistant tool_calls
      - {type:"tool_result", tool_use_id, content} → role:tool
    """
    messages: list[dict] = []

    # Anthropic carries the system prompt separately from the messages array.
    system = body.get("system")
    if system:
        if isinstance(system, list):
            # Anthropic system may be an array of {type:"text", text:"..."}.
            system = "\n".join(b.get("text", "") for b in system if b.get("type") == "text")
        if system:
            messages.append({"role": "system", "content": system})

    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Simple string content — pass through.
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        # Non-list, non-string — stringify defensively.
        if not isinstance(content, list):
            messages.append({"role": role, "content": str(content)})
            continue

        # Parse content blocks.
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        tool_results: list[dict] = []

        for block in content:
            btype = block.get("type", "text")

            if btype == "text":
                text_parts.append(block.get("text", ""))

            elif btype == "tool_use":
                # Assistant invoked a tool → assistant tool_calls entry.
                tool_calls.append(
                    {
                        "id": block.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    }
                )

            elif btype == "tool_result":
                # Tool result → role:tool message.
                tr_content = block.get("content", "")
                if isinstance(tr_content, list):
                    # Array of content blocks (text-only).
                    tr_content = "\n".join(
                        b.get("text", "") for b in tr_content if b.get("type") == "text"
                    )
                elif isinstance(tr_content, dict):
                    tr_content = json.dumps(tr_content)
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": str(tr_content),
                    }
                )

            elif btype == "image":
                # NVIDIA NIM has no vision support — inject a textual
                # placeholder so the model knows an image was present,
                # rather than silently dropping it.
                text_parts.append("[immagine omessa: il modello non supporta vision]")

        # Assemble the final message(s).
        if role == "assistant":
            msg_dict: dict[str, Any] = {"role": "assistant"}
            if text_parts:
                msg_dict["content"] = "\n".join(text_parts)
            if tool_calls:
                msg_dict["tool_calls"] = tool_calls
                if not text_parts:
                    msg_dict["content"] = None
            messages.append(msg_dict)
        elif role == "user":
            # User with tool_results → emit tool messages, then any text.
            if tool_results:
                messages.extend(tool_results)
            if text_parts:
                messages.append({"role": "user", "content": "\n".join(text_parts)})
        else:
            if text_parts:
                messages.append({"role": role, "content": "\n".join(text_parts)})

    return messages


def _anthropic_tools_to_chat_tools(tools: list[dict]) -> list[dict]:
    """
    Convert Anthropic tools into chat/completions tools.

    Anthropic shape:  {name, description, input_schema: {...}}
    OpenAI shape:     {type:"function", function:{name, description, parameters:{...}}}

    Non-function tools (computer_use, bash, text_editor, typed tools like
    "computer_20241022") are skipped — NVIDIA only understands function tools.
    """
    chat_tools = []
    for tool in tools:
        # Anthropic tags some tools with a type; "custom" is the generic
        # function tool. Anything else (computer, bash, etc.) is unsupported.
        if tool.get("type") and tool.get("type") != "custom":
            continue

        name = tool.get("name", "")
        if not name:
            continue

        chat_tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
        )
    return chat_tools


def _build_chat_payload(body: dict, model_override: str | None) -> dict:
    """Build the chat/completions payload from an Anthropic Messages request body."""
    messages = _anthropic_to_chat_messages(body)

    # model_override (from state.active_model) takes precedence.
    # No hardcoded model: resolved live from the user's selection.
    from .proxy_app import default_model

    effective_model = model_override or default_model()

    payload: dict[str, Any] = {
        "model": effective_model,
        "messages": messages,
    }

    # Tools translation + tool_choice mapping.
    tools = body.get("tools", [])
    if tools:
        payload["tools"] = _anthropic_tools_to_chat_tools(tools)
        # Anthropic tool_choice → OpenAI tool_choice
        tc = body.get("tool_choice")
        if tc:
            if isinstance(tc, dict) and tc.get("type") == "auto":
                payload["tool_choice"] = "auto"
            elif isinstance(tc, dict) and tc.get("type") == "any":
                payload["tool_choice"] = "required"
            elif isinstance(tc, dict) and tc.get("type") == "tool":
                payload["tool_choice"] = {
                    "type": "function",
                    "function": {"name": tc.get("name", "")},
                }

    # Parameter pass-through. Anthropic uses max_tokens (required), temperature,
    # top_p; OpenAI uses max_tokens or max_completion_tokens.
    max_tokens = body.get("max_tokens")
    if max_tokens:
        payload["max_tokens"] = max_tokens

    for key in ("temperature", "top_p", "stream"):
        val = body.get(key)
        if val is not None:
            payload[key] = val

    # stop_sequences → stop
    stop = body.get("stop_sequences")
    if stop:
        payload["stop"] = stop

    payload["messages"] = _sanitize_chat_messages(payload["messages"])
    # Dashboard thinking toggle — never overrides what the client asked for.
    from . import config as _cfg

    _cfg.apply_model_options(payload)
    return payload


# ── Response: chat/completions → Anthropic Messages ─────────────────


def _chat_to_anthropic_response(chat_data: dict, model: str) -> dict:
    """Translate a chat/completions response into Anthropic Messages format."""
    choice = (chat_data.get("choices") or [{}])[0]
    msg = choice.get("message", {})

    content_blocks: list[dict] = []

    # Tool calls → tool_use blocks. Arguments are parsed back into objects
    # to match Anthropic's input field (not a JSON string).
    tool_calls = msg.get("tool_calls", [])
    for tc in tool_calls:
        fn = tc.get("function", {})
        try:
            tool_input = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            tool_input = {}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
                "name": fn.get("name", ""),
                "input": tool_input,
            }
        )

    # Text content → text block.
    text = msg.get("content")
    if text:
        content_blocks.append({"type": "text", "text": text})

    # Anthropic requires a stop_reason — map from OpenAI's finish_reason.
    finish = choice.get("finish_reason", "stop")
    stop_reason_map = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "end_turn",
    }
    stop_reason = stop_reason_map.get(finish, "end_turn")

    # Usage mapping (prompt/completion → input/output).
    usage = chat_data.get("usage", {})
    anthropic_usage = {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
    }

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": anthropic_usage,
    }


# ── Streaming: SSE chat chunks → SSE Anthropic events ───────────────


def _sse_event(event_type: str, data: dict) -> bytes:
    """Serialize a single SSE event for the Anthropic streaming protocol."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()


async def _stream_anthropic(
    state: ProxyState,
    chat_payload: dict,
    model: str,
    client,
    request: Request,
) -> AsyncGenerator[bytes, None]:
    """Forward chat/completions with stream:true and re-translate SSE chunks into Anthropic events."""
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    # message_start — the opening event of the Anthropic stream.
    yield _sse_event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )

    # Candidate keys resolved inside the generator — avoids concurrent
    # requests sharing a precomputed list.
    async with state.lock:
        candidates = state.get_candidate_keys()

    # Key rotation (same pattern as the Responses shim).
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
        # Claim the key before sending — see _rotation_phase in
        # responses_shim.py for why the load balancer needs this.
        state.begin_in_flight(k)
        try:
            req = client.build_request("POST", UPSTREAM, json=chat_payload, headers=hdrs)
            resp = await client.send(req, stream=True)
        except httpx.ReadTimeout:
            # Slow model, not a bad key — see responses_shim._rotation_phase.
            state.log_cb(
                f"  anthropic shim: key[{idx}] no first byte — model too slow, not a key fault"
            )
            state.end_in_flight(k)
            break
        except httpx.HTTPError as e:
            err_msg = str(e) or type(e).__name__
            state.log_cb(f"  anthropic shim: key[{idx}] {err_msg} (rotating, cooldown 30s)")
            state.end_in_flight(k)
            state.mark_key_failed(k)
            continue

        if resp.status_code == 200:
            used_key = k  # claim held until the stream ends
            used_idx = idx
            break

        state.end_in_flight(k)
        err_status = resp.status_code
        err_raw = await resp.aread()
        await resp.aclose()
        resp = None
        state.log_cb(f"  anthropic shim: key[{idx}] HTTP {err_status}")
        if err_status in _CLIENT_ERR:
            # Deterministic error: surface to client, do not rotate or cooldown.
            yield _sse_event(
                "error",
                {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": _extract_err(err_raw, err_status),
                    },
                },
            )
            return
        if err_status in _GATEWAY_TIMEOUTS:
            # Provider edge gave up on the model; not this key's fault.
            state.log_cb(
                f"  anthropic shim: HTTP {err_status} is an upstream gateway "
                f"timeout — key[{idx}] left healthy"
            )
            continue
        state.mark_key_failed(k, status=err_status)

    if resp is None or used_key is None:
        yield _sse_event(
            "error",
            {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": "all keys failed",
                },
            },
        )
        return

    state.stats.success += 1
    state.stats.record_key_usage(used_key, ok=True)
    state.record_request(used_key)
    if used_idx is not None:
        state.log_cb(f"✔ key[{used_idx}] OK")

    # Accumulators for streamed content blocks.
    text_block_started = False
    text_block_index = 0
    text_full = ""

    # Tool calls: index → state.
    tool_map: dict[int, dict] = {}
    next_block_index = 1  # 0 = text block, then tool blocks

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

        # Text delta → content_block_delta (text_delta).
        text_content = delta.get("content")
        if text_content:
            if not text_block_started:
                text_block_started = True
                text_block_index = 0
                yield _sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": text_block_index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            yield _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": text_block_index,
                    "delta": {"type": "text_delta", "text": text_content},
                },
            )
            text_full += text_content

        # Tool call deltas → content_block_start (tool_use) + input_json_delta.
        tc_deltas = delta.get("tool_calls", [])
        for tc in tc_deltas:
            idx = tc.get("index", 0)
            if idx not in tool_map:
                toolu_id = f"toolu_{uuid.uuid4().hex[:24]}"
                call_id = tc.get("id", f"call_{uuid.uuid4().hex[:24]}")
                block_idx = next_block_index
                next_block_index += 1
                tool_map[idx] = {
                    "toolu_id": toolu_id,
                    "call_id": call_id,
                    "name": tc.get("function", {}).get("name", ""),
                    "arguments": "",
                    "block_idx": block_idx,
                }
                yield _sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": block_idx,
                        "content_block": {
                            "type": "tool_use",
                            "id": toolu_id,
                            "name": tc.get("function", {}).get("name", ""),
                            "input": {},
                        },
                    },
                )
            tcm = tool_map[idx]
            args_delta = tc.get("function", {}).get("arguments", "")
            if args_delta:
                tcm["arguments"] += args_delta
                yield _sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": tcm["block_idx"],
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": args_delta,
                        },
                    },
                )

        # Finish: close all open blocks, then message_delta + message_stop.
        finish = choice.get("finish_reason")
        if finish:
            # Close the text block.
            if text_block_started:
                yield _sse_event(
                    "content_block_stop",
                    {
                        "type": "content_block_stop",
                        "index": text_block_index,
                    },
                )
            # Close tool blocks.
            for tcm in tool_map.values():
                yield _sse_event(
                    "content_block_stop",
                    {
                        "type": "content_block_stop",
                        "index": tcm["block_idx"],
                    },
                )

            # stop_reason mapping.
            stop_map = {
                "stop": "end_turn",
                "length": "max_tokens",
                "tool_calls": "tool_use",
            }
            stop_reason = stop_map.get(finish, "end_turn")

            # message_delta carries stop_reason and a rough output token estimate.
            yield _sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": stop_reason,
                        "stop_sequence": None,
                    },
                    "usage": {"output_tokens": len(text_full) // 4},  # approx
                },
            )

            # message_stop — terminal event.
            yield _sse_event(
                "message_stop",
                {
                    "type": "message_stop",
                },
            )
            break

    # If upstream closed without a finish_reason, close gracefully anyway.
    if text_block_started:
        yield _sse_event(
            "content_block_stop",
            {
                "type": "content_block_stop",
                "index": text_block_index,
            },
        )
    for tcm in tool_map.values():
        yield _sse_event(
            "content_block_stop",
            {
                "type": "content_block_stop",
                "index": tcm["block_idx"],
            },
        )
        yield _sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 0},
            },
        )
        yield _sse_event("message_stop", {"type": "message_stop"})

    await resp.aclose()
    state.end_in_flight(used_key)  # release the load-balancer claim


# ── Entry point ─────────────────────────────────────────────────────────


async def handle_anthropic_messages(
    request: Request,
    state: ProxyState,
    client,
) -> JSONResponse | StreamingResponse:
    """
    Handle POST /v1/messages.

    Translates the Anthropic Messages API into chat/completions and forwards
    to NVIDIA NIM. This is a separate endpoint — it does not interfere with
    Claude Code's default routing to api.anthropic.com.
    """
    raw = await request.body()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return JSONResponse(
            {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "invalid JSON"},
            },
            status_code=400,
        )

    state.log_cb(
        f"  anthropic: model={body.get('model', '?')} stream={body.get('stream', False)} "
        f"tools={len(body.get('tools', []))}"
    )

    # Count images so the user gets a visible warning instead of silent drops.
    n_img = sum(
        1
        for m in body.get("messages", [])
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "image"
    )
    if n_img:
        state.log_cb(f"  ⚠ anthropic: {n_img} immagine/i omessa/e (NVIDIA NIM no-vision)")

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
            _stream_anthropic(
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
        return JSONResponse(
            {
                "type": "error",
                "error": {"type": "api_error", "message": "no keys available"},
            },
            status_code=503,
        )

    # Non-streaming: key rotation.
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
            req = client.build_request("POST", UPSTREAM, json=chat_payload, headers=hdrs)
            resp = await client.send(req)
        except httpx.ReadTimeout:
            # Slow model, not a bad key — see responses_shim._rotation_phase.
            state.log_cb(
                f"  anthropic shim: key[{idx}] no answer in time — model too slow, not a key fault"
            )
            break
        except httpx.HTTPError as e:
            err_msg = str(e) or type(e).__name__
            state.log_cb(f"  anthropic shim: key[{idx}] {err_msg} (rotating, cooldown 30s)")
            state.mark_key_failed(k)
            continue
        if resp.status_code == 200:
            used_key = k
            used_idx = idx
            break
        err_status = resp.status_code
        err_raw = await resp.aread()
        await resp.aclose()
        resp = None
        if err_status in _CLIENT_ERR:
            # Deterministic error: surface to client, do not rotate or cooldown.
            return JSONResponse(
                {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": _extract_err(err_raw, err_status),
                    },
                },
                status_code=err_status,
            )
        state.log_cb(f"  anthropic shim: key[{idx}] HTTP {err_status}")
        state.mark_key_failed(k, status=err_status)
        continue

    if resp is None or used_key is None:
        return JSONResponse(
            {
                "type": "error",
                "error": {"type": "api_error", "message": "all keys failed"},
            },
            status_code=503,
        )

    state.stats.success += 1
    state.stats.record_key_usage(used_key, ok=True)
    state.record_request(used_key)
    if used_idx is not None:
        state.log_cb(f"✔ key[{used_idx}] OK")

    chat_data = resp.json()
    await resp.aclose()

    anthropic_data = _chat_to_anthropic_response(chat_data, model_override or chat_payload["model"])
    return JSONResponse(anthropic_data)
