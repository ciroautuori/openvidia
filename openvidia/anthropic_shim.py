"""
Shim Anthropic Messages API -> chat/completions.

Traduce /v1/messages (l'API usata da Claude Code CLI) in /v1/chat/completions
(che openvidia gia' sa inoltrare a NVIDIA NIM). Bidirezionale:
  - request:  messages + system -> chat messages (system come primo message)
  - response: chat completion -> Anthropic content blocks (text, tool_use)
  - streaming: SSE chat chunks -> SSE Anthropic events
  - tools:    Anthropic tool schema (input_schema) -> OpenAI function (parameters)

Endpoint SEPARATO — non interferisce con il funzionamento normale di Claude Code
che punta ad api.anthropic.com. Attivo solo se l'utente sceglie di puntare
ANTHROPIC_BASE_URL a localhost:1919.

Niente astrazioni — solo traduzione di payload.
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

UPSTREAM = "https://integrate.api.nvidia.com/v1/chat/completions"


# ── Request: Anthropic Messages -> chat/completions ──────────────────


def _anthropic_to_chat_messages(body: dict) -> list[dict]:
    """
    Converte i messaggi Anthropic in messaggi chat/completions.
    Anthropic messaggi hanno content che puo' essere stringa o array di content blocks:
      - {type:"text", text:"..."}
      - {type:"image", source:{...}}   (ignorato, NVIDIA non supporta immagini)
      - {type:"tool_use", id:"...", name:"...", input:{...}}  -> assistant tool_calls
      - {type:"tool_result", tool_use_id:"...", content:"..."}  -> role:tool
    """
    messages: list[dict] = []

    # system prompt (Anthropic lo mette separatamente, non nei messages)
    system = body.get("system")
    if system:
        if isinstance(system, list):
            # Anthropic system puo' essere array di {type:"text", text:"..."}
            system = "\n".join(
                b.get("text", "") for b in system if b.get("type") == "text"
            )
        if system:
            messages.append({"role": "system", "content": system})

    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # content stringa semplice
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        # content array di content blocks
        if not isinstance(content, list):
            messages.append({"role": role, "content": str(content)})
            continue

        # Parsa i content blocks
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        tool_results: list[dict] = []

        for block in content:
            btype = block.get("type", "text")

            if btype == "text":
                text_parts.append(block.get("text", ""))

            elif btype == "tool_use":
                # Assistant ha chiamato un tool -> diventa tool_calls nel messaggio assistant
                tool_calls.append({
                    "id": block.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })

            elif btype == "tool_result":
                # Risultato di un tool -> diventa messaggio role:tool
                tr_content = block.get("content", "")
                if isinstance(tr_content, list):
                    # array di content blocks (text)
                    tr_content = "\n".join(
                        b.get("text", "") for b in tr_content if b.get("type") == "text"
                    )
                elif isinstance(tr_content, dict):
                    tr_content = json.dumps(tr_content)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": str(tr_content),
                })

            # image blocks: ignorati (NVIDIA NIM non supporta vision)

        # Costruisci il messaggio finale
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
            # User con tool_result -> messaggi tool separati
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
    Anthropic tools -> chat/completions tools.
    Anthropic usa: {name, description, input_schema: {...}}
    OpenAI usa:    {type:"function", function:{name, description, parameters:{...}}}
    """
    chat_tools = []
    for tool in tools:
        # Salta tools non function (es. computer_use, bash, text_editor)
        # Anthropic ha type per alcuni, ma molti sono solo name+input_schema
        if tool.get("type") and tool.get("type") != "custom":
            # type="computer_20241022" etc -> non supportato, skip
            continue

        name = tool.get("name", "")
        if not name:
            continue

        chat_tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return chat_tools


def _build_chat_payload(body: dict, model_override: str | None) -> dict:
    """Costruisce il payload chat/completions dal body Anthropic Messages."""
    messages = _anthropic_to_chat_messages(body)

    # model_override (da state.active_model) ha precedenza
    effective_model = model_override or "deepseek-ai/deepseek-v4-pro"

    payload: dict[str, Any] = {
        "model": effective_model,
        "messages": messages,
    }

    # Tools
    tools = body.get("tools", [])
    if tools:
        payload["tools"] = _anthropic_tools_to_chat_tools(tools)
        # Anthropic tool_choice -> OpenAI tool_choice
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

    # Parametri: Anthropic usa max_tokens (obbligatorio), temperature, top_p
    # OpenAI usa max_tokens o max_completion_tokens
    max_tokens = body.get("max_tokens")
    if max_tokens:
        payload["max_tokens"] = max_tokens

    for key in ("temperature", "top_p", "stream"):
        val = body.get(key)
        if val is not None:
            payload[key] = val

    # stop_sequences -> stop
    stop = body.get("stop_sequences")
    if stop:
        payload["stop"] = stop

    return payload


# ── Response: chat/completions -> Anthropic Messages ─────────────────


def _chat_to_anthropic_response(chat_data: dict, model: str) -> dict:
    """Traduce una chat/completions response in formato Anthropic Messages."""
    choice = chat_data.get("choices", [{}])[0]
    msg = choice.get("message", {})

    content_blocks: list[dict] = []

    # Tool calls -> tool_use blocks
    tool_calls = msg.get("tool_calls", [])
    for tc in tool_calls:
        fn = tc.get("function", {})
        try:
            tool_input = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            tool_input = {}
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
            "name": fn.get("name", ""),
            "input": tool_input,
        })

    # Text content -> text block
    text = msg.get("content")
    if text:
        content_blocks.append({"type": "text", "text": text})

    # Anthropic richiede stop_reason
    finish = choice.get("finish_reason", "stop")
    stop_reason_map = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "end_turn",
    }
    stop_reason = stop_reason_map.get(finish, "end_turn")

    # Usage
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


# ── Streaming: SSE chat chunks -> SSE Anthropic events ───────────────


def _sse_event(event_type: str, data: dict) -> bytes:
    """Anthropic SSE: event: type\ndata: json\n\n"""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()


async def _stream_anthropic(
    state: ProxyState,
    chat_payload: dict,
    model: str,
    client,
    keys: list[str],
    request: Request,
) -> AsyncGenerator[bytes, None]:
    """Invia chat/completions con stream:true, ritraduce in SSE Anthropic."""
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    # message_start
    yield _sse_event("message_start", {
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
    })

    # Key rotation (stesso pattern della Responses shim)
    resp = None
    used_key = None
    for k in keys:
        if not state.is_key_healthy(k) or not state.key_can_send_rpm(k):
            continue
        hdrs = {
            "Authorization": f"Bearer {k}",
            "Content-Type": "application/json",
            "User-Agent": "openvidia/2.0",
        }
        try:
            req = client.build_request("POST", UPSTREAM, json=chat_payload, headers=hdrs)
            resp = await client.send(req, stream=True)
        except httpx.HTTPError:
            continue

        if resp.status_code == 200:
            used_key = k
            break

        err_status = resp.status_code
        await resp.aread()
        await resp.aclose()
        resp = None
        state.log_cb(f"  anthropic shim: key HTTP {err_status}")
        state.mark_key_failed(k, status=err_status)

    if resp is None or used_key is None:
        yield _sse_event("error", {"type": "error", "message": "all keys failed"})
        return

    state.stats.success += 1
    state.stats.record_key_usage(used_key, ok=True)
    state.record_request(used_key)

    # Stati per accumulare content blocks
    text_block_started = False
    text_block_index = 0
    text_full = ""

    # Tool calls: index -> state
    tool_map: dict[int, dict] = {}
    next_block_index = 1  # 0 = text, poi tool blocks

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

        choice = chunk.get("choices", [{}])[0]
        delta = choice.get("delta", {})

        # Text delta -> content_block_delta (text_delta)
        text_content = delta.get("content")
        if text_content:
            if not text_block_started:
                text_block_started = True
                text_block_index = 0
                yield _sse_event("content_block_start", {
                    "type": "content_block_start",
                    "index": text_block_index,
                    "content_block": {"type": "text", "text": ""},
                })
            yield _sse_event("content_block_delta", {
                "type": "content_block_delta",
                "index": text_block_index,
                "delta": {"type": "text_delta", "text": text_content},
            })
            text_full += text_content

        # Tool call deltas
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
                yield _sse_event("content_block_start", {
                    "type": "content_block_start",
                    "index": block_idx,
                    "content_block": {
                        "type": "tool_use",
                        "id": toolu_id,
                        "name": tc.get("function", {}).get("name", ""),
                        "input": {},
                    },
                })
            tcm = tool_map[idx]
            args_delta = tc.get("function", {}).get("arguments", "")
            if args_delta:
                tcm["arguments"] += args_delta
                yield _sse_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": tcm["block_idx"],
                    "delta": {"type": "input_json_delta", "partial_json": args_delta},
                })

        # Finish
        finish = choice.get("finish_reason")
        if finish:
            # Chiudi text block
            if text_block_started:
                yield _sse_event("content_block_stop", {
                    "type": "content_block_stop",
                    "index": text_block_index,
                })
            # Chiudi tool blocks
            for idx, tcm in tool_map.items():
                yield _sse_event("content_block_stop", {
                    "type": "content_block_stop",
                    "index": tcm["block_idx"],
                })

            # stop_reason mapping
            stop_map = {
                "stop": "end_turn",
                "length": "max_tokens",
                "tool_calls": "tool_use",
            }
            stop_reason = stop_map.get(finish, "end_turn")

            # message_delta con stop_reason + usage
            yield _sse_event("message_delta", {
                "type": "message_delta",
                "delta": {
                    "stop_reason": stop_reason,
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": len(text_full) // 4},  # stimato
            })

            # message_stop
            yield _sse_event("message_stop", {
                "type": "message_stop",
            })
            break

    # Se non ricevuto finish, chiudi comunque
    if text_block_started:
        yield _sse_event("content_block_stop", {
            "type": "content_block_stop",
            "index": text_block_index,
        })
    for idx, tcm in tool_map.items():
        yield _sse_event("content_block_stop", {
            "type": "content_block_stop",
            "index": tcm["block_idx"],
        })
        yield _sse_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 0},
        })
        yield _sse_event("message_stop", {"type": "message_stop"})

    await resp.aclose()


# ── Handler principale ─────────────────────────────────────────────────


async def handle_anthropic_messages(
    request: Request,
    state: ProxyState,
    client,
) -> JSONResponse | StreamingResponse:
    """
    Punto d'ingresso per POST /v1/messages.
    Traduce Anthropic Messages API in chat/completions e inoltra a NVIDIA NIM.
    Endpoint SEPARATO — non interferisce con Claude Code default.
    """
    raw = await request.body()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return JSONResponse(
            {"type": "error", "error": {"type": "invalid_request_error", "message": "invalid JSON"}},
            status_code=400,
        )

    state.log_cb(
        f"  anthropic: model={body.get('model', '?')} stream={body.get('stream', False)} "
        f"tools={len(body.get('tools', []))}"
    )

    model_override = state.active_model
    chat_payload = _build_chat_payload(body, model_override)

    want_stream = body.get("stream", False)

    # Key snapshot
    async with state.lock:
        keys = list(state.keys)
    if not keys:
        return JSONResponse(
            {"type": "error", "error": {"type": "api_error", "message": "no keys available"}},
            status_code=503,
        )

    if want_stream:
        chat_payload["stream"] = True
        return StreamingResponse(
            _stream_anthropic(
                state, chat_payload, model_override or chat_payload["model"], client, keys, request
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming: key rotation
    used_key = None
    resp = None
    for k in keys:
        if not state.is_key_healthy(k) or not state.key_can_send_rpm(k):
            continue
        hdrs = {
            "Authorization": f"Bearer {k}",
            "Content-Type": "application/json",
            "User-Agent": "openvidia/2.0",
        }
        try:
            req = client.build_request("POST", UPSTREAM, json=chat_payload, headers=hdrs)
            resp = await client.send(req)
        except httpx.HTTPError:
            continue
        if resp.status_code == 200:
            used_key = k
            break
        err_status = resp.status_code
        await resp.aclose()
        resp = None
        state.mark_key_failed(k, status=err_status)
        continue

    if resp is None or used_key is None:
        return JSONResponse(
            {"type": "error", "error": {"type": "api_error", "message": "all keys failed"}},
            status_code=503,
        )

    state.stats.success += 1
    state.stats.record_key_usage(used_key, ok=True)
    state.record_request(used_key)

    chat_data = resp.json()
    await resp.aclose()

    anthropic_data = _chat_to_anthropic_response(chat_data, model_override or chat_payload["model"])
    return JSONResponse(anthropic_data)
