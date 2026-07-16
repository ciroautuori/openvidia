"""
The actual reverse proxy: catch-all route, key rotation, streaming passthrough.

Merge il meglio di entrambe le versioni:
- Dal VECCHIO: connection pooling tuned, key rotation con bucket available+cooldown,
  reuse cooldown se tutte giù, ripristino validità su successo
- Dal NUOVO: background health check, RPM rate limiting, Responses shim per Codex,
  Retry-After parsing, client disconnect detection, HTTP/2, fallback basato sui preset

Fallback: niente DEGRADED_MODELS hardcoded. Se il modello attivo fallisce su tutte
le chiavi, prova il modello successivo nei preset dell'utente.
"""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from .proxy_state import ProxyState, persist_index
from .responses_shim import handle_responses
from .anthropic_shim import handle_anthropic_messages

UPSTREAM_BASE = "https://integrate.api.nvidia.com/v1/"
MAX_BODY_BYTES = 64 * 1024 * 1024

# 400 (payload malformato) e 404 (modello inesistente) sono deterministici sul
# contenuto: ruotare non aiuta (stesso errore su ogni chiave) e sprecherebbe
# chiavi in cooldown. Vengono ritornati direttamente al client.
# 401/403 (auth) e 429 (rate) sono invece colpa della chiave → rotazione+cooldown.
ROTATE_STATUSES = {401, 403, 429}


def should_rotate(status: int) -> bool:
    return status in ROTATE_STATUSES or status >= 500


STRIPPED_RESPONSE_HEADERS = {"content-encoding", "transfer-encoding", "content-length", "connection"}

# Sinonimi di modello — se l'utente chiede "openvidia" mappa al modello attivo
DEFAULT_MODEL = "deepseek-ai/deepseek-v4-pro"


async def _check_key_health(client: httpx.AsyncClient, key: str) -> bool:
    headers = {"Authorization": f"Bearer {key}", "User-Agent": "openvidia/2.0"}
    try:
        req = client.build_request("GET", UPSTREAM_BASE + "models", headers=headers)
        resp = await client.send(req)
        ok = resp.is_success
        await resp.aclose()
        return ok
    except httpx.HTTPError:
        return False


async def _health_check_all(state: ProxyState, client: httpx.AsyncClient, force: bool = False) -> None:
    revived = 0
    for key in state.keys:
        if not force and not state.is_key_on_cooldown(key):
            continue
        if not force and state.cooldown_remaining(key) > 90:
            continue
        healthy = await _check_key_health(client, key)
        if healthy:
            state.clear_cooldown_and_restore(key)
            revived += 1
        elif not force:
            pass
        else:
            state.mark_key_failed(key)
    n_unhealthy = sum(1 for k in state.keys if state.is_key_on_cooldown(k))
    all_ok = len(state.keys) - n_unhealthy
    state.log_cb(
        f"⚕ health: {all_ok}/{len(state.keys)} OK"
        + (f", {n_unhealthy} on cooldown" if n_unhealthy else "")
        + (f", {revived} revived" if revived else "")
    )


async def _background_health_check(state: ProxyState, client: httpx.AsyncClient) -> None:
    try:
        while True:
            await asyncio.sleep(30)
            await _health_check_all(state, client)
    except asyncio.CancelledError:
        pass


class BodyLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > MAX_BODY_BYTES:
                    return JSONResponse({"error": "payload too large"}, status_code=413)
            except ValueError:
                pass
        return await call_next(request)


def create_app(state: ProxyState, web_dir: Optional[Path] = None) -> FastAPI:
    # Connection pooling tuned (dal VECCHIO) + HTTP/2 (dal NUOVO)
    limits = httpx.Limits(max_keepalive_connections=100, max_connections=200, keepalive_expiry=30.0)
    client = httpx.AsyncClient(
        http2=True,
        limits=limits,
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=120.0),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # startup: pre-warm chiavi + health check in background
        async def _pre_warm():
            state.log_cb("⚕ pre-warm: checking all keys...")
            await _health_check_all(state, client, force=True)
            state.log_cb(
                f"⚕ pre-warm done ({sum(1 for k in state.keys if state.is_key_healthy(k))}/{len(state.keys)} healthy)"
            )
        asyncio.create_task(_pre_warm())
        state.health_task = asyncio.create_task(_background_health_check(state, client))
        yield
        # shutdown: ferma il task e chiudi il client
        if state.health_task is not None:
            state.health_task.cancel()
        await client.aclose()

    app = FastAPI(lifespan=lifespan)
    app.state.http_client = client

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(BodyLimitMiddleware)

    if web_dir and web_dir.exists():
        from .webui import attach_webui
        attach_webui(app, state, web_dir)

    # ── Shim Responses API → chat/completions (Codex) ────────────────
    @app.post("/v1/responses")
    async def responses_handler(request: Request):
        if not state.running:
            return JSONResponse({"error": "proxy stopped"}, status_code=503)
        state.stats.requests += 1
        return await handle_responses(request, state, client)

    # ── Shim Anthropic Messages API → chat/completions (Claude Code) ──
    # Endpoint SEPARATO — attivo solo se l'utente punta ANTHROPIC_BASE_URL
    # a localhost:1919. Zero impatto su Claude Code default (api.anthropic.com).
    @app.post("/v1/messages")
    async def anthropic_messages_handler(request: Request):
        if not state.running:
            return JSONResponse(
                {"type": "error", "error": {"type": "api_error", "message": "proxy stopped"}},
                status_code=503,
            )
        state.stats.requests += 1
        return await handle_anthropic_messages(request, state, client)

    # ── /v1/models con formato OpenAI per Codex ───────────────────────
    @app.get("/v1/models")
    async def models_handler():
        if not state.running:
            return JSONResponse({"error": "proxy stopped"}, status_code=503)

        async with state.lock:
            keys = list(state.keys)
        if not keys:
            return JSONResponse({"error": "no keys"}, status_code=503)

        for key in keys:
            if not state.is_key_healthy(key) or not state.key_can_send_rpm(key):
                continue
            headers = {"Authorization": f"Bearer {key}", "User-Agent": "openvidia/2.0"}
            try:
                req = client.build_request("GET", UPSTREAM_BASE + "models", headers=headers)
                resp = await client.send(req)
                if resp.is_success:
                    data = resp.json()
                    await resp.aclose()
                    if "data" in data and "models" not in data:
                        models = data.pop("data")
                        for m in models:
                            m["slug"] = m.get("id", "")
                            m["display_name"] = m.get("id", "")
                        data["models"] = models
                    return JSONResponse(data)
                await resp.aclose()
            except httpx.HTTPError:
                continue
        return JSONResponse({"error": "all keys failed"}, status_code=503)

    # ── Catch-all proxy → NVIDIA NIM ──────────────────────────────────
    @app.api_route("/v1/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def proxy_handler(full_path: str, request: Request):
        if not state.running:
            return JSONResponse({"error": "proxy stopped"}, status_code=503)

        state.stats.requests += 1

        body = await request.body()
        payload = None
        if body:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = None

        # Model rewrite: "openvidia" → active_model o default
        if isinstance(payload, dict):
            m = state.active_model or payload.get("model")
            if isinstance(m, str) and m != "openvidia":
                payload["model"] = m
                body = json.dumps(payload).encode()
            elif m == "openvidia":
                payload["model"] = DEFAULT_MODEL
                body = json.dumps(payload).encode()

        # ── Auto-compaction: history troppo lunga → riassunto (no block) ──
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("messages"), list)
            and full_path.endswith("chat/completions")
        ):
            from .compaction import maybe_compact
            new_messages = await maybe_compact(
                payload["messages"], state=state, client=client, log=state.log_cb
            )
            if new_messages is not payload["messages"]:
                payload["messages"] = new_messages
                body = json.dumps(payload).encode()

        # ── Key rotation con bucket (dal VECCHIO meglio del NUOVO) ─────
        async with state.lock:
            candidates = state.get_candidate_keys()

        if not candidates:
            state.log_cb("✗ No valid keys available")
            return JSONResponse({"error": "no valid keys available"}, status_code=503)

        nv_path = full_path[3:] if full_path.startswith("v1/") else full_path
        url = UPSTREAM_BASE + nv_path

        total_keys = len(state.keys)
        last_status = 503
        CLIENT_FWD_HEADERS = {"content-type", "accept", "x-request-id", "x-trace-id"}

        for orig_idx, key in candidates:
            # Skip se RPM satura
            if not state.key_can_send_rpm(key):
                state.log_cb(f"  key[{orig_idx}] RPM saturated ({state.key_rpm(key)}/min), skip")
                continue

            headers = {
                "Authorization": f"Bearer {key}",
                "User-Agent": "openvidia/2.0",
            }
            for k, v in request.headers.items():
                if k.lower() in CLIENT_FWD_HEADERS:
                    headers[k] = v
            if isinstance(payload, dict) and "content-type" not in {k.lower() for k in headers}:
                headers["Content-Type"] = "application/json"

            try:
                req = client.build_request(request.method, url, content=body, headers=headers)
                resp = await client.send(req, stream=True)
            except httpx.ReadTimeout:
                state.log_cb(f"key[{orig_idx}] ReadTimeout (rotating, cooldown 30s)")
                state.stats.record_key_usage(key, ok=False, error="ReadTimeout")
                state.mark_key_failed(key)
                state.stats.rotations += 1
                persist_index(state, (orig_idx + 1) % total_keys)
                continue
            except httpx.HTTPError as e:
                err_msg = str(e) or type(e).__name__
                state.log_cb(f"key[{orig_idx}] {err_msg}")
                state.stats.record_key_usage(key, ok=False, error=err_msg)
                state.mark_key_failed(key)
                state.stats.rotations += 1
                persist_index(state, (orig_idx + 1) % total_keys)
                continue

            status = resp.status_code

            if 200 <= status < 300:
                state.stats.success += 1
                state.stats.record_key_usage(key, ok=True)
                state.record_request(key)
                state.restore_key(key)  # Dal VECCHIO: ripristina su successo
                if nv_path != "models":
                    state.log_cb(f"✔ key[{orig_idx}] OK")

                out_headers = {
                    k: v for k, v in resp.headers.items() if k.lower() not in STRIPPED_RESPONSE_HEADERS
                }
                out_headers["access-control-allow-origin"] = "*"
                out_headers["access-control-allow-headers"] = "Content-Type, Authorization"
                out_headers["access-control-allow-methods"] = "GET, POST, OPTIONS"

                async def body_iter():
                    try:
                        async for chunk in resp.aiter_raw():
                            if await request.is_disconnected():
                                break
                            yield chunk
                    finally:
                        await resp.aclose()

                return StreamingResponse(body_iter(), status_code=status, headers=out_headers)

            state.log_cb(f"key[{orig_idx}] HTTP {status}")
            last_status = status

            if should_rotate(status):
                retry_after = resp.headers.get("retry-after")
                state.stats.record_key_usage(key, ok=False, error=f"HTTP {status}")
                state.mark_key_failed(key, status=status, retry_after=retry_after)
                state.stats.rotations += 1
                persist_index(state, (orig_idx + 1) % total_keys)
                await resp.aclose()
                continue

            resp_bytes = await resp.aread()
            await resp.aclose()
            return Response(
                content=resp_bytes,
                status_code=status,
                headers={"access-control-allow-origin": "*"},
            )

        # ── Fallback basato sui preset utente (no DEGRADED_MODELS) ──────
        model_name = payload.get("model", "") if isinstance(payload, dict) else ""
        fallback_model = _get_fallback_model(state, model_name)

        if fallback_model:
            state.log_cb(f"↻ {model_name} failed on all keys — retry with preset fallback: {fallback_model}")
            fb_body = json.dumps({**payload, "model": fallback_model}).encode()
            for fb_key in state.keys:
                if not state.is_key_healthy(fb_key):
                    continue
                hdrs = {
                    "Authorization": f"Bearer {fb_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "openvidia/2.0",
                }
                try:
                    fb_req = client.build_request("POST", url, content=fb_body, headers=hdrs)
                    fb_resp = await client.send(fb_req)
                    if fb_resp.is_success:
                        state.log_cb(f"✔ fallback → {fallback_model}")
                        state.record_request(fb_key)
                        fb_body_raw = await fb_resp.aread()
                        persist_index(state, (state.keys.index(fb_key) + 1) % len(state.keys))
                        await fb_resp.aclose()
                        fb_data = json.loads(fb_body_raw)
                        fb_data["model"] = fallback_model
                        return JSONResponse(content=fb_data, headers={"access-control-allow-origin": "*"})
                    await fb_resp.aclose()
                    if fb_resp.status_code == 429:
                        continue
                    state.log_cb(f"✗ fallback HTTP {fb_resp.status_code}")
                    break
                except Exception as e:
                    state.log_cb(f"  ⏳ fallback key error: {e}, trying next...")
                    continue

        msg = "all keys exhausted"
        if fallback_model:
            msg += f" — {model_name} failed, preset fallback {fallback_model} also failed"
        return JSONResponse(
            {"error": msg, "last_upstream_status": last_status},
            status_code=last_status,
        )

    return app


def _get_fallback_model(state: ProxyState, failed_model: str) -> Optional[str]:
    """
    Fallback basato sui preset utente: trova il modello successivo nei preset
    rispetto al modello attivo. Niente mapping hardcoded.
    """
    from . import config

    try:
        presets = config.load_saved_presets()
    except Exception:
        return None

    if not presets or failed_model not in presets:
        return None

    idx = presets.index(failed_model)
    if idx + 1 < len(presets):
        return presets[idx + 1]

    return None
