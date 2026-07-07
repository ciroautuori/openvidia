"""
The actual reverse proxy: catch-all route, key rotation, streaming passthrough.

Direct port of proxy.rs's proxy_handler. Upstream is NVIDIA NIM only
(https://integrate.api.nvidia.com) — no Z.ai routing, by design (see chat).
"""
import json
from pathlib import Path

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

    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=8.0, read=120.0, write=120.0, pool=120.0))
    app.state.http_client = client

    @app.on_event("shutdown")
    async def _close_client():
        await client.aclose()

    @app.api_route("/v1/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def proxy_handler(full_path: str, request: Request):
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
            if isinstance(m, str):
                payload["model"] = m
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
            except httpx.HTTPError as e:
                state.log_cb(f"key[{i}] error: {e}")
                state.stats.record_key_usage(key, ok=False, error=str(e))
                state.stats.rotations += 1
                persist_index(state, (i + 1) % len(keys))
                continue

            status = resp.status_code

            if 200 <= status < 300:
                state.stats.success += 1
                state.stats.record_key_usage(key, ok=True)
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
                if state.on_key_failed is not None:
                    state.on_key_failed(key)
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

        # All keys tried — return the real upstream status so the caller
        # can distinguish a bad request (400) from rate-limiting (429) from
        # credentials exhausted (401/403). No 503 masking: every rotatable
        # status is a real signal, not a blanket "retry me".
        return JSONResponse(
            {"error": "all keys exhausted", "last_upstream_status": last_status},
            status_code=last_status,
        )

    return app
