"""
The actual reverse proxy: catch-all route, key rotation, streaming passthrough.

Direct port of proxy.rs's proxy_handler. Upstream is NVIDIA NIM only
(https://integrate.api.nvidia.com) — no Z.ai routing, by design (see chat).
"""
import asyncio
import json
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from .proxy_state import ProxyState, persist_index

UPSTREAM_BASE = "https://integrate.api.nvidia.com/v1/"
MAX_BODY_BYTES = 64 * 1024 * 1024  # see DefaultBodyLimit note in original proxy.rs

# Status codes another key might survive: auth/quota (401/403/429), server
# errors (5xx), and bad-request/not-found (400/404) — on NVIDIA a key lacking
# access to the requested model returns 404/400 while another key with that
# model works. A truly malformed request 4xx's on every key and the loop
# then returns that real status (not an opaque 503).
ROTATE_STATUSES = {400, 401, 403, 404, 429}


def should_rotate(status: int) -> bool:
    return status in ROTATE_STATUSES or status >= 500


STRIPPED_RESPONSE_HEADERS = {"content-encoding", "transfer-encoding", "content-length", "connection"}
DEGRADED_MODELS = {
    "z-ai/glm-5.2": "deepseek-ai/deepseek-v4-pro",
    "moonshotai/kimi-k2.6": "deepseek-ai/deepseek-v4-flash",
}


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
            continue  # already healthy — skip
        healthy = await _check_key_health(client, key)
        if healthy:
            state.clear_cooldown(key)
            revived += 1
        elif not force:
            pass  # keep existing cooldown
        else:
            state.mark_key_failed(key)  # first-time check failed
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
    """
    The handler buffers the whole body to inject the model, so an unbounded
    body could OOM us. Cap at 64MB and let the real upstream limit decide;
    still bounded so a rogue localhost client can't hurt us. Mirrors
    DefaultBodyLimit::max(64MB) in the original proxy.rs.
    """

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
    app = FastAPI()

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

    client = httpx.AsyncClient(
        http2=True,
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=120.0),
    )
    app.state.http_client = client

    @app.on_event("startup")
    async def _start_background_tasks():
        async def _pre_warm():
            state.log_cb("⚕ pre-warm: checking all keys...")
            await _health_check_all(state, client, force=True)
            state.log_cb(f"⚕ pre-warm done ({sum(1 for k in state.keys if state.is_key_healthy(k))}/{len(state.keys)} healthy)")
        asyncio.create_task(_pre_warm())
        state.health_task = asyncio.create_task(_background_health_check(state, client))

    @app.on_event("shutdown")
    async def _close_client():
        if state.health_task is not None:
            state.health_task.cancel()
        await client.aclose()

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

        if isinstance(payload, dict):
            m = state.active_model or payload.get("model")
            if isinstance(m, str) and m != "openvidia":
                payload["model"] = m
                body = json.dumps(payload).encode()
            elif m == "openvidia":
                payload["model"] = "deepseek-ai/deepseek-v4-pro"
                body = json.dumps(payload).encode()

        async with state.lock:
            keys = list(state.keys)

        if not keys:
            state.log_cb("✗ No keys available")
            return JSONResponse({"error": "no keys available"}, status_code=503)

        nv_path = full_path[3:] if full_path.startswith("v1/") else full_path
        url = UPSTREAM_BASE + nv_path

        start = state.stats.current_index % len(keys)
        last_status = 503

        CLIENT_FWD_HEADERS = {"content-type", "accept", "x-request-id", "x-trace-id"}

        for offset in range(len(keys)):
            i = (start + offset) % len(keys)
            key = keys[i]
            if not state.is_key_healthy(key):
                continue
            if not state.key_can_send_rpm(key):
                state.log_cb(f"  key[{i}] RPM saturated ({state.key_rpm(key)}/min), skip")
                continue
            headers = {
                "Authorization": f"Bearer {key}",
                "User-Agent": "openvidia/2.0",
            }
            # Forward original client headers that aren't hop-by-hop
            for k, v in request.headers.items():
                if k.lower() in CLIENT_FWD_HEADERS:
                    headers[k] = v
            if isinstance(payload, dict) and "content-type" not in {k.lower() for k in headers}:
                headers["Content-Type"] = "application/json"

            try:
                req = client.build_request(request.method, url, content=body, headers=headers)
                resp = await client.send(req, stream=True)
            except httpx.ReadTimeout:
                state.log_cb(f"key[{i}] ReadTimeout (rotating, no cooldown)")
                state.stats.record_key_usage(key, ok=False, error="ReadTimeout")
                state.stats.rotations += 1
                persist_index(state, (i + 1) % len(keys))
                continue
            except httpx.HTTPError as e:
                err_msg = str(e) or type(e).__name__
                state.log_cb(f"key[{i}] {err_msg}")
                state.stats.record_key_usage(key, ok=False, error=err_msg)
                state.mark_key_failed(key)  # network error → cooldown
                state.stats.rotations += 1
                persist_index(state, (i + 1) % len(keys))
                continue

            status = resp.status_code

            if 200 <= status < 300:
                state.stats.success += 1
                state.stats.record_key_usage(key, ok=True)
                state.record_request(key)
                persist_index(state, i)
                if nv_path != "models":
                    state.log_cb(f"✔ key[{i}] OK")

                out_headers = {
                    k: v for k, v in resp.headers.items() if k.lower() not in STRIPPED_RESPONSE_HEADERS
                }
                out_headers["access-control-allow-origin"] = "*"
                out_headers["access-control-allow-headers"] = "Content-Type, Authorization"
                out_headers["access-control-allow-methods"] = "GET, POST, OPTIONS"

                # Stream the upstream body straight through — critical for SSE
                # (stream:true), so tokens reach the client as they arrive
                # instead of being buffered until the generation finishes.
                async def body_iter():
                    try:
                        async for chunk in resp.aiter_raw():
                            yield chunk
                    finally:
                        await resp.aclose()

                return StreamingResponse(body_iter(), status_code=status, headers=out_headers)

            state.log_cb(f"key[{i}] HTTP {status}")

            last_status = status

            if should_rotate(status):
                state.stats.record_key_usage(key, ok=False, error=f"HTTP {status}")
                retry_after = resp.headers.get("retry-after")
                state.mark_key_failed(key, status=status, retry_after=retry_after)
                state.stats.rotations += 1
                persist_index(state, (i + 1) % len(keys))
                await resp.aclose()
                continue

            resp_bytes = await resp.aread()
            await resp.aclose()
            return Response(
                content=resp_bytes,
                status_code=status,
                headers={"access-control-allow-origin": "*"},
            )

        # All keys tried — degraded model fallback
        model_name = payload.get("model", "") if isinstance(payload, dict) else ""
        fallback_model = DEGRADED_MODELS.get(model_name, "")

        if fallback_model:
            state.log_cb(f"↻ {model_name} failed on all keys — retry with {fallback_model}")
            fb_body = json.dumps({**payload, "model": fallback_model}).encode()
            for fb_key in keys:
                hdrs = {
                    "Authorization": f"Bearer {fb_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "openvidia/2.0",
                }
                try:
                    fb_client = httpx.AsyncClient(http2=True, timeout=httpx.Timeout(30.0))
                    req = fb_client.build_request("POST", url, content=fb_body, headers=hdrs)
                    fb_resp = await fb_client.send(req)
                    if fb_resp.is_success:
                        state.log_cb(f"✔ fallback → {fallback_model}")
                        state.record_request(fb_key)
                        fb_body_raw = await fb_resp.aread()
                        persist_index(state, (keys.index(fb_key) + 1) % len(keys))
                        await fb_resp.aclose()
                        await fb_client.aclose()
                        fb_data = json.loads(fb_body_raw)
                        fb_data["model"] = fallback_model
                        return JSONResponse(content=fb_data, headers={"access-control-allow-origin": "*"})
                    fb_body_raw = await fb_resp.aread()
                    await fb_resp.aclose()
                    await fb_client.aclose()
                    if fb_resp.status_code == 429:
                        continue
                    state.log_cb(f"✗ fallback HTTP {fb_resp.status_code}")
                    break
                except Exception as e:
                    err_msg = str(e) or type(e).__name__
                    state.log_cb(f"  ⏳ fallback key error: {err_msg}, trying next...")
                    continue

        msg = "all keys exhausted"
        if fallback_model:
            msg += f" — {model_name} failed, fallback to {fallback_model} also failed"
        return JSONResponse(
            {"error": msg, "last_upstream_status": last_status},
            status_code=last_status,
        )

    return app
