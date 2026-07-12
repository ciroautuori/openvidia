"""
Shim Responses API → chat/completions.

Traduce /v1/responses (l'API usata da Codex CLI) in /v1/chat/completions
(che openvidia gia' sa inoltrare a NVIDIA NIM). Bidirezionale:
  - request:  input (string|items[]) → messages[]
  - response: chat completion → output items (text, function_call)
  - streaming: SSE chat chunks → SSE Responses events
  - tools:    function definitions → chat tools, e ritorno

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

# ── Request: Responses input → chat/completions messages ─────────────


def _input_to_messages(input_data: Any) -> list[dict]:
    """
    Responses API accetta `input` come:
      - stringa singola → un messaggio user
      - array di InputItems (message, function_call, function_call_output)
    Codex CLI invia messaggi con role="developer" (→ system) e
    content parts con type="input_text" (→ text).
    """
    if isinstance(input_data, str):
        return [{"role": "user", "content": input_data}]

    messages: list[dict] = []
    for item in input_data:
        typ = item.get("type", "message")

        if typ == "message":
            role = item.get("role", "user")
            # Codex usa "developer" → mappa a "system" per chat/completions
            if role == "developer":
                role = "system"
            content = item.get("content", "")
            # content puo' essere stringa o array di content_part
            if isinstance(content, list):
                # Codex usa type="input_text", OpenAI standard usa type="text"
                text_parts = [
                    p.get("text", "")
                    for p in content
                    if p.get("type") in ("text", "input_text")
                ]
                content = "\n".join(text_parts)
            messages.append({"role": role, "content": content})

        elif typ == "function_call_output":
            # Risultato di una tool call precedente — va come messaggio tool
            call_id = item.get("call_id", "")
            output = item.get("output", "")
            if isinstance(output, dict):
                output = json.dumps(output)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": str(output)})

        elif typ == "function_call":
            # Una function call gia' fatta nel turno precedente — la ricostruiamo
            # come assistant message con tool_calls
            name = item.get("name", "")
            arguments = item.get("arguments", "")
            call_id = item.get("call_id", "")
            messages.append({
                "role": "assistant",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }],
            })
        # item type sconosciuto → skip

    return messages


def _tools_to_chat_tools(tools: list[dict]) -> list[dict]:
    """
    Responses tools → chat/completions tools[].
    Codex CLI invia due formati:
      - flat: {type:"function", name:"x", description:"...", parameters:{...}}
      - nested: {type:"function", function:{name:"x", ...}}
    Filtra anche tipi non-function (namespace, web_search, image_generation)
    che NVIDIA non supporta.
    """
    chat_tools = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        # Formato flat (Codex) — nome e parametri al top level
        if "name" in tool:
            chat_tools.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {}),
                },
            })
        # Formato nested (standard OpenAI)
        elif "function" in tool:
            fn = tool["function"]
            chat_tools.append({
                "type": "function",
                "function": {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                },
            })
    return chat_tools


def _build_chat_payload(body: dict, model_override: str | None) -> dict:
    """Costruisce il payload chat/completions dal body Responses."""
    messages = []

    # Instructions (system prompt) → system message
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})

    messages.extend(_input_to_messages(body.get("input", "")))

    # model_override (da state.active_model) ha precedenza; se assente,
    # usa il default NVIDIA (mai passare "openvidia/openvidia" a NVIDIA)
    effective_model = model_override or "deepseek-ai/deepseek-v4-pro"

    payload: dict[str, Any] = {
        "model": effective_model,
        "messages": messages,
    }

    # Tools
    tools = body.get("tools", [])
    if tools:
        payload["tools"] = _tools_to_chat_tools(tools)

    # Parametri opzionali pass-through (solo quelli compatibili con chat/completions)
    for key in ("temperature", "top_p", "max_tokens", "max_completion_tokens", "stream"):
        val = body.get(key)
        if val is not None:
            payload[key] = val

    return payload


# ── Response: chat/completions → Responses output ────────────────────


def _chat_response_to_responses(chat_data: dict, model: str) -> dict:
    """Traduce una chat/completions response (non-streaming) in formato Responses."""
    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    created = chat_data.get("created", int(time.time()))

    output: list[dict] = []
    choice = chat_data.get("choices", [{}])[0]
    msg = choice.get("message", {})

    # Tool calls → function_call items
    tool_calls = msg.get("tool_calls", [])
    if tool_calls:
        for tc in tool_calls:
            fn = tc.get("function", {})
            output.append({
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex[:24]}",
                "call_id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "arguments": fn.get("arguments", ""),
            })

    # Text content → message item con content array (output_text)
    text = msg.get("content")
    if text:
        output.append({
            "type": "message",
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text}],
        })

    # Status
    finish = choice.get("finish_reason", "stop")
    status = "completed" if finish == "stop" else "incomplete"

    # Usage
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
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()


async def _stream_responses(
    state: ProxyState,
    chat_payload: dict,
    model: str,
    client,
    keys: list[str],
    request: Request,
) -> AsyncGenerator[bytes, None]:
    """
    Invia chat/completions con stream:true a NVIDIA, ritraduce i chunk SSE
    in eventi Responses SSE.
    """
    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    created_ts = int(time.time())

    # Evento iniziale: response.created
    yield _sse_event("response.created", {
        "type": "response.created",
        "response": {
            "id": resp_id,
            "object": "response",
            "created_at": created_ts,
            "model": model,
            "output": [],
            "status": "in_progress",
        },
    })
    # response.in_progress — Codex lo aspetta
    yield _sse_event("response.in_progress", {
        "type": "response.in_progress",
        "response": {
            "id": resp_id,
            "object": "response",
            "created_at": created_ts,
            "model": model,
            "output": [],
            "status": "in_progress",
        },
    })

    upstream = "https://integrate.api.nvidia.com/v1/chat/completions"

    # Prova tutte le chiavi in rotazione (come il catch-all)
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
            req = client.build_request("POST", upstream, json=chat_payload, headers=hdrs)
            resp = await client.send(req, stream=True)
        except httpx.HTTPError:
            continue

        if resp.status_code == 200:
            used_key = k
            break

        # Error: rotate to next key
        err_status = resp.status_code
        await resp.aread()
        await resp.aclose()
        resp = None
        state.log_cb(f"  responses shim: key HTTP {err_status}")
        state.mark_key_failed(k, status=err_status)

    if resp is None or used_key is None:
        yield _sse_event("error", {"type": "error", "message": "all keys failed"})
        yield _sse_event("response.failed", {
            "type": "response.failed",
            "response": {
                "id": resp_id,
                "object": "response",
                "created_at": created_ts,
                "model": model,
                "output": [],
                "status": "failed",
            },
        })
        return

    state.stats.success += 1
    state.stats.record_key_usage(used_key, ok=True)
    state.record_request(used_key)

    # Stati per accumulare tool calls durante lo streaming
    # Codex (Responses API) usa item type "message" con content array di
    # content_part type "output_text" — NON item type "text".
    text_item_id = f"msg_{uuid.uuid4().hex[:24]}"
    text_started = False
    text_full = ""
    # output_index: 0 = text (se presente), poi tool_calls
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

        choice = chunk.get("choices", [{}])[0]
        delta = choice.get("delta", {})

        # Text delta
        text_content = delta.get("content")
        if text_content:
            if not text_started:
                text_started = True
                text_output_index = next_output_index
                next_output_index += 1
                yield _sse_event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": text_output_index,
                    "item": {
                        "type": "message",
                        "id": text_item_id,
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    },
                })
            yield _sse_event("response.output_text.delta", {
                "type": "response.output_text.delta",
                "item_id": text_item_id,
                "output_index": text_output_index,
                "delta": text_content,
            })
            text_full += text_content

        # Tool call deltas
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
                yield _sse_event("response.output_item.added", {
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
                })
            tcm = tool_calls_map[idx]
            args_delta = tc.get("function", {}).get("arguments", "")
            if args_delta:
                tcm["arguments"] += args_delta
                yield _sse_event("response.function_call_arguments.delta", {
                    "type": "response.function_call_arguments.delta",
                    "item_id": tcm["fc_id"],
                    "output_index": tcm["output_index"],
                    "delta": args_delta,
                })

        # Finish
        finish = choice.get("finish_reason")
        if finish:
            if text_started:
                yield _sse_event("response.output_text.done", {
                    "type": "response.output_text.done",
                    "item_id": text_item_id,
                    "output_index": text_output_index,
                    "text": text_full,
                })
                yield _sse_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": text_output_index,
                    "item": {
                        "type": "message",
                        "id": text_item_id,
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": text_full}],
                    },
                })
            for idx, tcm in tool_calls_map.items():
                yield _sse_event("response.function_call_arguments.done", {
                    "type": "response.function_call_arguments.done",
                    "item_id": tcm["fc_id"],
                    "output_index": tcm["output_index"],
                    "arguments": tcm["arguments"],
                })
                yield _sse_event("response.output_item.done", {
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
                })
            break

    # response.completed finale
    yield _sse_event("response.completed", {
        "type": "response.completed",
        "response": {
            "id": resp_id,
            "object": "response",
            "created_at": created_ts,
            "model": model,
            "output": [],
            "status": "completed",
        },
    })

    await resp.aclose()


# ── Handler principale ─────────────────────────────────────────────────

async def handle_responses(
    request: Request,
    state: ProxyState,
    client,
) -> JSONResponse | StreamingResponse:
    """
    Punto d'ingresso per POST /v1/responses.
    Fa l'effettivo forward a NVIDIA chat/completions (interno, NON passa dal catch-all).
    """
    raw = await request.body()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    # Log per debug
    state.log_cb(f"  responses: model={body.get('model','?')} stream={body.get('stream',False)} tools={len(body.get('tools',[]))}")

    model_override = state.active_model
    chat_payload = _build_chat_payload(body, model_override)

    want_stream = body.get("stream", False)

    # Keysnapshot
    async with state.lock:
        keys = list(state.keys)
    if not keys:
        return JSONResponse({"error": "no keys available"}, status_code=503)

    if want_stream:
        chat_payload["stream"] = True
        return StreamingResponse(
            _stream_responses(state, chat_payload, model_override or chat_payload["model"], client, keys, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming: prova tutte le chiavi in rotazione
    upstream = "https://integrate.api.nvidia.com/v1/chat/completions"
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
            req = client.build_request("POST", upstream, json=chat_payload, headers=hdrs)
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
        return JSONResponse({"error": "all keys failed"}, status_code=503)

    state.stats.success += 1
    state.stats.record_key_usage(used_key, ok=True)
    state.record_request(used_key)

    chat_data = resp.json()
    await resp.aclose()

    responses_data = _chat_response_to_responses(chat_data, model_override or chat_payload["model"])
    return JSONResponse(responses_data)
