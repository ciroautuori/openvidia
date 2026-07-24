"""Reverse proxy core: catch-all route, key rotation, streaming passthrough."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from . import config
from .anthropic_shim import handle_anthropic_messages
from .proxy_state import ProxyState, persist_index
from .responses_shim import handle_responses

UPSTREAM_BASE = "https://integrate.api.nvidia.com/v1/"
MAX_BODY_BYTES = 64 * 1024 * 1024

# 400/404 are deterministic content errors — rotating keys won't help and
# just burns cooldown budget. 401/403/429 are key-specific → rotate + cooldown.
ROTATE_STATUSES = {401, 403, 429}

from ._upstream_utils import get_upstream_sem, is_resource_exhausted  # noqa: E402


def default_model(state: ProxyState | None = None) -> str:
    """The model a request runs on when the client sends the ``openvidia`` alias.

    Resolved live, never hardcoded: a pinned model name is a liability the day
    the provider retires it or ships something better, and it silently
    overrides what the user picked in the dashboard. Order: the active
    selection, then the first starred preset. Empty means the user has not
    chosen a model yet, and the caller must say so rather than invent one.
    """
    if state is not None and state.active_model:
        return state.active_model
    try:
        presets = config.load_saved_presets()
    except Exception:  # noqa: BLE001 — model choice must never break a request
        presets = []
    return presets[0] if presets else ""


# Bounded rotation: cap the number of upstream sends per rotation phase and
# give each send a bounded connect+read+write+pool timeout. The catch-all
# historically iterated ALL candidates with the client default read=120s, so
# 25 saturated keys could block a Codex request for up to 25×120s = 50min.
_MAX_ROTATE_ATTEMPTS = 5
_ROTATE_SEND_TIMEOUT = httpx.Timeout(**config.httpx_timeout_kwargs())
# Fast probe timeout: used only to detect dead/overloaded models quickly.
# If NVIDIA doesn't reply in 30s on ANY key → model is down → circuit opens.
# Full streaming answers still use _ROTATE_SEND_TIMEOUT (180s).
_MODEL_PROBE_TIMEOUT = httpx.Timeout(connect=5.0, read=90.0, write=10.0, pool=95.0)
_MIN_LIVE_FRACTION = 0.05  # <5% live keys → skip rotation, go to fallback/503

STRIPPED_RESPONSE_HEADERS = {
    "content-encoding",
    "transfer-encoding",
    "content-length",
    "connection",
}


def should_rotate(status: int) -> bool:
    return status in ROTATE_STATUSES or status >= 500


async def _check_key_health(
    client: httpx.AsyncClient, key: str, sem: asyncio.Semaphore | None = None
) -> bool:
    headers = {"Authorization": f"Bearer {key}", "User-Agent": "openvidia/2.0"}
    try:
        if sem is not None:
            async with sem:
                req = client.build_request("GET", UPSTREAM_BASE + "models", headers=headers)
                resp = await client.send(req)
        else:
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
    """Probe cooldown-expired keys in parallel with concurrency bounds.

    Serial probing was fine for <5 keys but stalls pre-warm beyond ~2s when
    many keys are dead. We batch them with asyncio.gather bounded by a
    Semaphore to prevent blasting dozens of requests at once.
    """
    targets: list[str] = []
    for key in state.keys:
        if not force and not state.is_key_on_cooldown(key):
            continue
        # Skip keys with most of their cooldown left — probe only when nearing expiry.
        if not force and state.cooldown_remaining(key) > 30:
            continue
        targets.append(key)

    if not targets:
        return

    sem = asyncio.Semaphore(5)
    results = await asyncio.gather(
        *(_check_key_health(client, k, sem) for k in targets),
        return_exceptions=True,
    )

    revived = 0
    for key, healthy in zip(targets, results, strict=True):
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


async def _background_health_check(state: ProxyState, client: httpx.AsyncClient) -> None:
    try:
        while True:
            await asyncio.sleep(60)
            await _health_check_all(state, client)
    except asyncio.CancelledError:
        pass


async def _warm_keepalive_task(state: ProxyState, client: httpx.AsyncClient) -> None:
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


def create_app(state: ProxyState, web_dir: Path | None = None) -> FastAPI:
    limits = httpx.Limits(max_keepalive_connections=100, max_connections=200, keepalive_expiry=30.0)
    proxy_url = config.outbound_proxy()
    client = httpx.AsyncClient(
        http2=True,
        proxy=proxy_url if proxy_url else None,
        limits=limits,
        timeout=httpx.Timeout(**config.httpx_timeout_kwargs()),
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
                req = client.build_request("GET", UPSTREAM_BASE + "models", headers=headers)
                resp = await client.send(req)
                if resp.is_success:
                    data = resp.json()
                    await resp.aclose()
                    # Mantieni entrambe le chiavi: "data" (standard OpenAI,
                    # usata da Codex) e "models" (usata da altri client).
                    if "data" in data and "models" not in data:
                        models = list(data["data"])
                        for m in models:
                            m["slug"] = m.get("id", "")
                            m["display_name"] = m.get("id", "")
                        data["models"] = models
                    # Inietta l'alias "openvidia" in cima a entrambe le liste
                    # così i picker dei CLI (Codex, opencode) lo mostrano come
                    # opzione selezionabile. Il proxy lo risolve a runtime nel
                    # modello selezionato nella dashboard.
                    alias = {
                        "id": "openvidia",
                        "object": "model",
                        "slug": "openvidia",
                        "display_name": "OpenVidia (dashboard auto-select)",
                    }
                    if isinstance(data.get("models"), list):
                        data["models"].insert(0, alias)
                    if isinstance(data.get("data"), list):
                        data["data"].insert(0, dict(alias))
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

        # Model alias: "openvidia" resolves to the user's active model.
        if isinstance(payload, dict):
            req_model = payload.get("model")
            if req_model == "openvidia" or not req_model:
                resolved = default_model(state)
                if not resolved:
                    return JSONResponse(
                        {"error": "no model selected — pick one in the dashboard"},
                        status_code=400,
                    )
                payload["model"] = resolved
                body = json.dumps(payload).encode()

        # Thinking toggle (dashboard setting; never overrides the client).
        if isinstance(payload, dict) and payload.get("model"):
            before = json.dumps(payload, sort_keys=True)
            config.apply_model_options(payload)
            if json.dumps(payload, sort_keys=True) != before:
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

        # ── Circuit breaker: skip model if too many consecutive failures ──────
        # When glm-5.2 or laguna-xs are down on NVIDIA, ALL keys will timeout.
        # Instead of spending 30s×5 attempts = 2.5min on a known-dead model,
        # check the circuit and auto-failover to the next healthy preset.
        requested_model = payload.get("model", "") if isinstance(payload, dict) else ""
        if requested_model and state.is_model_circuit_open(requested_model):
            # Try to failover to the next working preset
            presets = config.load_saved_presets()
            fallback = next(
                (m for m in presets if m != requested_model and not state.is_model_circuit_open(m)),
                None,
            )
            if fallback and isinstance(payload, dict):
                state.log_cb(f"🔴 {requested_model} circuit OPEN → auto-failover to {fallback}")
                payload["model"] = fallback
                body = json.dumps(payload).encode()
                requested_model = fallback
            else:
                state.log_cb(f"🔴 {requested_model} circuit OPEN, no healthy fallback")
                return JSONResponse(
                    {"error": f"{requested_model} is down (circuit open), no fallback available"},
                    status_code=503,
                )

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
            1
            for _, k in candidates
            if state.key_can_send_rpm(k) and not state.is_key_on_cooldown(k)
        )
        _total_pool = len(state.keys)
        _pool_saturated = _total_pool > 0 and _live_candidates < max(
            1, int(_total_pool * _MIN_LIVE_FRACTION)
        )
        if _pool_saturated:
            state.log_cb(
                f"⚠ pool saturated ({_live_candidates}/{_total_pool} live) → "
                f"skip rotation, try model fallback"
            )
            last_status = 429

        _rotate_attempts = 0
        for _pass in range(3):
            if _pass > 0:
                await asyncio.sleep(1.0)
            for orig_idx, key in candidates:
                if _rotate_attempts >= _MAX_ROTATE_ATTEMPTS:
                    state.log_cb(
                        f"  rotation cap reached ({_MAX_ROTATE_ATTEMPTS} attempts) → stop "
                        f"(fallback/503)"
                    )
                    break
                if not state.key_can_send_rpm(key) or state.is_key_on_cooldown(key):
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

                state.begin_in_flight(key)
                _rotate_attempts += 1
                _model = payload.get("model", "") if isinstance(payload, dict) else ""
                _t0 = time.monotonic()
                # First attempt uses the fast probe timeout to detect dead models
                # immediately. Subsequent attempts (the model is probably just slow)
                # use the full streaming timeout.
                _timeout = _MODEL_PROBE_TIMEOUT if _rotate_attempts == 1 else _ROTATE_SEND_TIMEOUT
                try:
                    req = client.build_request(
                        request.method,
                        url,
                        content=body,
                        headers=headers,
                        timeout=_timeout,
                    )
                    # Global concurrency semaphore: never exceed 28 concurrent
                    # upstream sends (NVIDIA worker limit is 32/32).
                    async with get_upstream_sem():
                        resp = await client.send(req, stream=True)
                except httpx.ReadTimeout:
                    # Slow model or dead model.
                    state.end_in_flight(key)
                    _ttft_wait = _timeout.read
                    state.log_cb(
                        f"key[{orig_idx}] no first byte in {_ttft_wait:.0f}s — model too slow/down"
                    )
                    state.stats.record_key_usage(key, ok=False, error="ReadTimeout")
                    state.record_model_result(_model, too_slow=True)
                    # On first attempt with probe timeout: continue rotating to confirm
                    # it's a model issue not a single-key fluke.
                    if _rotate_attempts == 1:
                        continue
                    # After 2nd timeout: model is confirmed dead, break immediately.
                    break
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
                    _ttft = time.monotonic() - _t0
                    state.record_model_result(_model, ok=True, ttft=_ttft)
                    if nv_path != "models":
                        state.log_cb(f"✔ key[{orig_idx}] OK ({_ttft:.1f}s TTFT)")

                    out_headers = {
                        k: v
                        for k, v in resp.headers.items()
                        if k.lower() not in STRIPPED_RESPONSE_HEADERS
                    }
                    out_headers["access-control-allow-origin"] = "*"
                    out_headers["access-control-allow-headers"] = "Content-Type, Authorization"
                    out_headers["access-control-allow-methods"] = "GET, POST, OPTIONS"

                    # Bind the loop variables explicitly. The return below leaves
                    # the loop immediately, so late binding could not bite here —
                    # but a reader (and the linter) should not have to prove that.
                    async def body_iter(resp=resp, key=key, orig_idx=orig_idx):
                        try:
                            async for chunk in resp.aiter_raw():
                                if await request.is_disconnected():
                                    break
                                yield chunk
                        except (httpx.ReadTimeout, httpx.StreamError, httpx.HTTPError) as e:
                            state.log_cb(f"key[{orig_idx}] stream timeout/error: {e}")
                        finally:
                            state.end_in_flight(key)
                            await resp.aclose()

                    return StreamingResponse(body_iter(), status_code=status, headers=out_headers)

                state.end_in_flight(key)
                state.log_cb(f"key[{orig_idx}] HTTP {status}")
                last_status = status

                # Gateway timeouts (502/503/504): upstream overload, not the
                # key's fault. Short cooldown prevents hammering the same
                # broken upstream instance while still letting the next key
                # try — if the entire pool is hitting the same wall, all keys
                # will briefly cooldown and the saturation gate will kick in.
                if status in {502, 503, 504}:
                    state.stats.record_key_usage(key, ok=False, error=f"HTTP {status}")
                    state.mark_key_failed(key, status=status, retry_after=10)
                    state.record_model_result(_model, status=status)
                    state.stats.rotations += 1
                    await resp.aclose()
                    continue

                if should_rotate(status):
                    retry_after = resp.headers.get("retry-after")
                    # Check if this 429 is a ResourceExhausted (worker concurrency
                    # limit) vs. a real RPM rate-limit. Concurrency errors are
                    # transient: the worker frees a slot as soon as any in-flight
                    # request finishes, so burning the key with a 45s+ cooldown
                    # just depletes the pool. Treat it as a brief skip instead.
                    _resp_body = None
                    try:
                        _resp_body = await resp.aread()
                    except Exception:
                        pass
                    await resp.aclose()
                    if status == 429 and is_resource_exhausted(_resp_body):
                        _rotate_attempts -= (
                            1  # Don't burn attempt budget on worker-level transient peak
                        )
                        state.log_cb(
                            f"key[{orig_idx}] ResourceExhausted (worker full) — "
                            f"pausing 0.8s for worker slot to free"
                        )
                        state.stats.record_key_usage(key, ok=False, error="ResourceExhausted")
                        state.stats.rotations += 1
                        await asyncio.sleep(0.8)
                        continue
                    state.stats.record_key_usage(key, ok=False, error=f"HTTP {status}")
                    state.mark_key_failed(key, status=status, retry_after=retry_after)
                    state.stats.rotations += 1
                    persist_index(state, (orig_idx + 1) % total_keys)
                    continue

            resp_bytes = await resp.aread()
            await resp.aclose()
            return Response(
                content=resp_bytes,
                status_code=status,
                headers={"access-control-allow-origin": "*"},
            )

        # All rotation attempts exhausted. Check if model circuit should auto-open
        # so the next request gets failover immediately instead of repeating all this.
        model_name = payload.get("model", "") if isinstance(payload, dict) else ""
        if model_name:
            state.record_model_result(model_name, status=last_status)
        msg = "all keys exhausted"
        if model_name:
            msg += f" for {model_name}"
        if model_name and state.is_model_circuit_open(model_name):
            msg += " (circuit open — will auto-failover on next request)"
        return JSONResponse(
            {"error": msg, "last_upstream_status": last_status},
            status_code=last_status,
        )

    # ── /ops/health — live model & pool health dashboard ──────────────
    @app.get("/ops/health")
    async def _ops_health() -> JSONResponse:
        """Structured health report: model circuit states, pool stats, recent logs."""
        import time as _time

        now = _time.time()
        models_out = []
        for model, h in state.model_health.items():
            models_out.append(
                {
                    "model": model,
                    "requests": h.requests,
                    "success": h.success,
                    "failure_rate": round(h.failure_rate, 2),
                    "too_slow": h.too_slow,
                    "gateway_timeouts": h.gateway_timeouts,
                    "rate_limited": h.rate_limited,
                    "median_ttft_s": round(h.median_ttft, 1),
                    "circuit_open": h.is_circuit_open,
                    "consecutive_failures": h.consecutive_failures,
                    "circuit_reset_in_s": max(
                        0, round(h.CIRCUIT_RESET_AFTER - (now - h.circuit_opened_at), 1)
                    )
                    if h.is_circuit_open
                    else 0,
                }
            )
        live_keys, valid_keys = state.count_live_candidates()
        recent_logs = list(state.log_buffer)[-50:]
        return JSONResponse(
            {
                "pool": {
                    "n_keys": len(state.keys),
                    "n_healthy": valid_keys,
                    "n_live_rpm": live_keys,
                    "n_on_cooldown": sum(1 for k in state.keys if state.is_key_on_cooldown(k)),
                    "aggregate_rpm": sum(state.key_rpm(k) for k in state.keys),
                    "rpm_ceiling": len(state.keys) * 28,
                },
                "models": models_out,
                "presets": config.load_saved_presets(),
                "active_model": state.active_model,
                "total_requests": state.stats.requests,
                "total_success": state.stats.success,
                "total_rotations": state.stats.rotations,
                "recent_logs": recent_logs,
            }
        )

    return app
