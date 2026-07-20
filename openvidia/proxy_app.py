"""Reverse proxy core: catch-all route, key rotation, streaming passthrough."""

from __future__ import annotations

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

# 400/404 are deterministic content errors — rotating keys won't help and
# just burns cooldown budget. 401/403/429 are key-specific → rotate + cooldown.
ROTATE_STATUSES = {401, 403, 429}

DEFAULT_MODEL = "deepseek-ai/deepseek-v4-pro"

# Bounded rotation: cap the number of upstream sends per rotation phase and
# give each send a bounded connect+read+write+pool timeout. The catch-all
# historically iterated ALL candidates with the client default read=120s, so
# 25 saturated keys could block a Codex request for up to 25×120s = 50min.
_MAX_ROTATE_ATTEMPTS = 5
_ROTATE_SEND_TIMEOUT = httpx.Timeout(connect=4.0, read=30.0, write=10.0, pool=30.0)
_MIN_LIVE_FRACTION = 0.2  # <20% live keys → skip rotation, go to fallback/503

STRIPPED_RESPONSE_HEADERS = {
    "content-encoding",
    "transfer-encoding",
    "content-length",
    "connection",
}


def should_rotate(status: int) -> bool:
    return status in ROTATE_STATUSES or status >= 500


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


async def _health_check_all(
    state: ProxyState, client: httpx.AsyncClient, force: bool = False
) -> None:
    """Probe cooldown-expired keys in parallel (pre-warm touches all keys).

    Serial probing was fine for <5 keys but stalls pre-warm beyond ~2s when
    many keys are dead and each probe takes the full ReadTimeout. We batch
    them with asyncio.gather so the whole pass completes in ~one round-trip.
    """
    targets: list[str] = []
    for key in state.keys:
        if not force and not state.is_key_on_cooldown(key):
            continue
        # Skip keys with most of their cooldown left — probing too early wastes quota.
        if not force and state.cooldown_remaining(key) > 90:
            continue
        targets.append(key)

    if not targets:
        return

    results = await asyncio.gather(
        *(_check_key_health(client, k) for k in targets),
        return_exceptions=True,
    )

    revived = 0
    for key, healthy in zip(targets, results):
        if isinstance(healthy, Exception):
            healthy = False
        if healthy:
            state.clear_cooldown_and_restore(key)
            revived += 1
        elif force:
            state.mark_key_failed(key)
    n_unhealthy = sum(1 for k in state.keys if state.is_key_on_cooldown(k))
    all_ok = len(state.keys) - n_unhealthy
    state.log_cb(
        f"⚕ health: {all_ok}/{len(state.keys)} OK"
        + (f", {n_unhealthy} on cooldown" if n_unhealthy else "")
        + (f", {revived} revived" if revived else "")
    )


async def _background_health_check(
    state: ProxyState, client: httpx.AsyncClient
) -> None:
    try:
        while True:
            await asyncio.sleep(30)
            await _health_check_all(state, client)
    except asyncio.CancelledError:
        pass


async def _warm_keepalive_task(
    state: ProxyState, client: httpx.AsyncClient
) -> None:
    """Decay-only passive helper.

    We DO NOT actively ping all healthy keys on a timer — that would burn
    ~25 GET /v1/models every 45s across the pool, silently inflating the RPM
    sliding window of every key and risking accidental self-induced 429 when
    real user traffic arrives on top. Instead this task just ages out stale
    consecutive-failure counters once per minute so a key that had a couple
    of transient errors three minutes ago stops being deprioritized forever.
    """
    try:
        while True:
            await asyncio.sleep(60)
            now = time.time()
            for key in state.keys:
                ks = state._key_states.get(key)
                if ks and ks.consecutive_failures and now - ks.last_failure_at > 180:
                    ks.consecutive_failures = max(0, ks.consecutive_failures - 1)
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
    # Tuned for many concurrent streaming completions: generous keepalive pool,
    # long read timeout for slow LLM generation, HTTP/2 for connection reuse.
    limits = httpx.Limits(
        max_keepalive_connections=100, max_connections=200, keepalive_expiry=30.0
    )
    client = httpx.AsyncClient(
        http2=True,
        limits=limits,
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=120.0),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def _pre_warm():
            state.log_cb("⚕ pre-warm: checking all keys...")
            await _health_check_all(state, client, force=True)
            state.log_cb(
                f"⚕ pre-warm done ({sum(1 for k in state.keys if state.is_key_healthy(k))}/{len(state.keys)} healthy)"
            )

        asyncio.create_task(_pre_warm())
        state.health_task = asyncio.create_task(_background_health_check(state, client))
        state.warm_task = asyncio.create_task(_warm_keepalive_task(state, client))
        yield
        if state.warm_task is not None:
            state.warm_task.cancel()
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

    # ── Responses API shim → chat/completions (Codex) ──────────────────
    @app.post("/v1/responses")
    async def responses_handler(request: Request):
        if not state.running:
            return JSONResponse({"error": "proxy stopped"}, status_code=503)
        state.stats.requests += 1
        return await handle_responses(request, state, client)

    # ── Anthropic Messages API shim (Claude Code) ───────────────────────
    # Separate endpoint — only active if the user points ANTHROPIC_BASE_URL
    # at localhost:1919. Zero impact on the default Claude Code flow.
    @app.post("/v1/messages")
    async def anthropic_messages_handler(request: Request):
        if not state.running:
            return JSONResponse(
                {
                    "type": "error",
                    "error": {"type": "api_error", "message": "proxy stopped"},
                },
                status_code=503,
            )
        state.stats.requests += 1
        return await handle_anthropic_messages(request, state, client)

    # ── /v1/models in OpenAI format (Codex compatibility) ──────────────
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
                req = client.build_request(
                    "GET", UPSTREAM_BASE + "models", headers=headers
                )
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

    # ── Internal ops endpoint: not proxied, dashboard-facing ──────────
    # Exposes live per-key health/RPM/in-flight/consecutive-failures so the
    # dashboard can render the whole pool, not just an aggregate count.
    @app.get("/ops/keys")
    async def _ops_keys_inner() -> JSONResponse:
        if not state.running:
            return JSONResponse({"error": "proxy stopped"}, status_code=503)
        async with state.lock:
            out: list[dict] = []
            for idx, key in enumerate(state.keys):
                ks = state._key_states.get(key)
                redacted = key[:5] + "…" + key[-4:] if len(key) > 12 else "***"
                tracker = state.rpm.get(key)
                ku = state.stats.key_usage.get(key)
                out.append(
                    {
                        "index": idx,
                        "key": redacted,
                        "valid": bool(ks and ks.is_valid),
                        "healthy": state.is_key_healthy(key),
                        "cooldown_remaining": round(state.cooldown_remaining(key), 1),
                        "cooldown_reason": state.cooldown_reason(key),
                        "rpm": state.key_rpm(key),
                        "rpm_ceiling": tracker.max_rpm if tracker and tracker.max_rpm else None,
                        "in_flight": ks.in_flight if ks else 0,
                        "consecutive_failures": ks.consecutive_failures if ks else 0,
                        "requests": ku.requests if ku else 0,
                        "success": ku.success if ku else 0,
                        "failed": ku.failed if ku else 0,
                    }
                )
        return JSONResponse(
            {
                "keys": out,
                "n_keys": len(state.keys),
                "n_healthy": sum(1 for k in state.keys if state.is_key_healthy(k)),
                "n_on_cooldown": sum(1 for k in state.keys if state.is_key_on_cooldown(k)),
                "aggregate_rpm": sum(state.key_rpm(k) for k in state.keys),
                "aggregate_rpm_ceiling": len(state.keys) * 28,
                "active_index": state.stats.active_key_index,
            }
        )

    # ── Catch-all proxy → NVIDIA NIM ──────────────────────────────────
    @app.api_route(
        "/v1/{full_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
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

        # Model alias: "openvidia" resolves to the user's active model or the default.
        if isinstance(payload, dict):
            m = state.active_model or payload.get("model")
            if isinstance(m, str) and m != "openvidia":
                payload["model"] = m
                body = json.dumps(payload).encode()
            elif m == "openvidia":
                payload["model"] = DEFAULT_MODEL
                body = json.dumps(payload).encode()

        # Auto-compaction: if conversation history exceeds the token budget,
        # summarize older turns transparently so the request stays under limits.
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

        # ── Key rotation ──────────────────────────────────────────────
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

        # Saturation gate: weigh live (cooldown-free, RPM-eligible) candidates
        # against the FULL pool size, not just len(candidates). The proxy's
        # get_candidate_keys() drops invalid keys and sorts cooldown ones to the
        # tail, so len(candidates) can be small even when the pool is healthy.
        # Using the full pool as the denominator makes the gate fire correctly
        # when most of the 25 keys are on cooldown (the historical Codex block).
        _live_candidates = sum(
            1 for _, k in candidates
            if state.key_can_send_rpm(k) and not state.is_key_on_cooldown(k)
        )
        _total_pool = len(state.keys)
        _pool_saturated = (
            _total_pool > 0
            and _live_candidates < max(1, int(_total_pool * _MIN_LIVE_FRACTION))
        )
        if _pool_saturated:
            state.log_cb(
                f"⚠ pool saturated ({_live_candidates}/{_total_pool} live) → "
                f"skip rotation, try model fallback"
            )
            last_status = 429

        _rotate_attempts = 0
        for orig_idx, key in candidates:
            if _rotate_attempts >= _MAX_ROTATE_ATTEMPTS:
                state.log_cb(
                    f"  rotation cap reached ({_MAX_ROTATE_ATTEMPTS} attempts) → stop "
                    f"(fallback/503)"
                )
                break
            if not state.key_can_send_rpm(key):
                state.log_cb(
                    f"  key[{orig_idx}] RPM saturated ({state.key_rpm(key)}/min), skip"
                )
                continue

            headers = {
                "Authorization": f"Bearer {key}",
                "User-Agent": "openvidia/2.0",
            }
            for k, v in request.headers.items():
                if k.lower() in CLIENT_FWD_HEADERS:
                    headers[k] = v
            if isinstance(payload, dict) and "content-type" not in {
                k.lower() for k in headers
            }:
                headers["Content-Type"] = "application/json"

            state.begin_in_flight(key)
            _rotate_attempts += 1
            try:
                req = client.build_request(
                    request.method, url, content=body, headers=headers,
                    timeout=_ROTATE_SEND_TIMEOUT,
                )
                resp = await client.send(req, stream=True)
            except httpx.ReadTimeout:
                state.end_in_flight(key)
                state.log_cb(f"key[{orig_idx}] ReadTimeout (rotating, cooldown 30s)")
                state.stats.record_key_usage(key, ok=False, error="ReadTimeout")
                state.mark_key_failed(key)
                state.stats.rotations += 1
                persist_index(state, (orig_idx + 1) % total_keys)
                continue
            except httpx.HTTPError as e:
                state.end_in_flight(key)
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
                state.restore_key(key)
                if nv_path != "models":
                    state.log_cb(f"✔ key[{orig_idx}] OK")

                out_headers = {
                    k: v
                    for k, v in resp.headers.items()
                    if k.lower() not in STRIPPED_RESPONSE_HEADERS
                }
                out_headers["access-control-allow-origin"] = "*"
                out_headers["access-control-allow-headers"] = (
                    "Content-Type, Authorization"
                )
                out_headers["access-control-allow-methods"] = "GET, POST, OPTIONS"

                async def body_iter():
                    try:
                        async for chunk in resp.aiter_raw():
                            if await request.is_disconnected():
                                break
                            yield chunk
                    finally:
                        state.end_in_flight(key)
                        await resp.aclose()

                return StreamingResponse(
                    body_iter(), status_code=status, headers=out_headers
                )

            state.end_in_flight(key)
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

        # ── Preset-based model fallback ────────────────────────────────
        model_name = payload.get("model", "") if isinstance(payload, dict) else ""
        fallback_model = _get_fallback_model(state, model_name)

        if fallback_model:
            state.log_cb(
                f"↻ {model_name} failed on all keys — retry with preset fallback: {fallback_model}"
            )
            fb_body = json.dumps({**payload, "model": fallback_model}).encode()
            for fb_idx, fb_key in enumerate(state.keys):
                if not state.is_key_healthy(fb_key) or not state.key_can_send_rpm(fb_key):
                    continue
                hdrs = {
                    "Authorization": f"Bearer {fb_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "openvidia/2.0",
                }
                try:
                    fb_req = client.build_request(
                        "POST", url, content=fb_body, headers=hdrs
                    )
                    fb_resp = await client.send(fb_req)
                    if fb_resp.is_success:
                        state.log_cb(f"✔ fallback key[{fb_idx}] → {fallback_model}")
                        state.record_request(fb_key)
                        state.stats.record_key_usage(fb_key, ok=True)
                        fb_body_raw = await fb_resp.aread()
                        persist_index(state, (fb_idx + 1) % len(state.keys))
                        await fb_resp.aclose()
                        fb_data = json.loads(fb_body_raw)
                        fb_data["model"] = fallback_model
                        return JSONResponse(
                            content=fb_data,
                            headers={"access-control-allow-origin": "*"},
                        )
                    fb_status = fb_resp.status_code
                    await fb_resp.aclose()
                    state.stats.record_key_usage(fb_key, ok=False, error=f"HTTP {fb_status}")
                    if should_rotate(fb_status):
                        state.mark_key_failed(fb_key, status=fb_status)
                        continue
                    state.log_cb(f"✗ fallback HTTP {fb_status}")
                    break
                except httpx.HTTPError as e:
                    err_msg = str(e) or type(e).__name__
                    state.log_cb(f"  ⏳ fallback key error: {err_msg}")
                    state.mark_key_failed(fb_key)
                    continue

        msg = "all keys exhausted"
        if fallback_model:
            msg += (
                f" — {model_name} failed, preset fallback {fallback_model} also failed"
            )
        return JSONResponse(
            {"error": msg, "last_upstream_status": last_status},
            status_code=last_status,
        )

    return app


def _get_fallback_model(state: ProxyState, failed_model: str) -> Optional[str]:
    """Find the next preset model after the one that just failed all keys."""
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
