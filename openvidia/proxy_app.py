"""Reverse proxy core: catch-all route, key rotation, streaming passthrough."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from . import config
from .anthropic_shim import handle_anthropic_messages
from .config import UPSTREAM_BASE
from .embedding_cache import EmbeddingCache
from .provider_fallback import load_provider_configs, try_fallback
from .proxy_state import ProxyState
from .redis_sync import RedisSync
from .responses_shim import (
    _MAX_ROTATE_ATTEMPTS,
    _MIN_LIVE_FRACTION,
    _MODEL_PROBE_TIMEOUT,
    _ROTATE_SEND_TIMEOUT,
    _live_pool_snapshot,
    _rotation_phase,
    _upstream_error_message,
    handle_responses,
)
from .upstream_router import EndpointRouter

MAX_BODY_BYTES = 64 * 1024 * 1024


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


def _key_model_score(state: ProxyState, key: str) -> float | None:
    """Media degli score per-modello osservati per questa chiave."""
    scores = [h.score() for (k, _model), h in state.key_model_health.items() if k == key]
    if not scores:
        return None
    return round(sum(scores) / len(scores), 2)


STRIPPED_RESPONSE_HEADERS = {
    "content-encoding",
    "transfer-encoding",
    "content-length",
    "connection",
}


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
        # A rate limit is not something GET /v1/models can disprove. That
        # endpoint serves metadata and answers 200 for a key whose
        # chat/completions quota is spent, so probing a 429 cooldown only ever
        # ends it early — and the key goes straight back to being rate-limited.
        # Let 429 cooldowns expire on their own schedule.
        if not force and state.cooldown_status(key) == 429:
            continue
        # A 403/401 means the key is dead (invalid auth). GET /v1/models may
        # still return 200 for some dead keys (NVIDIA's auth check is
        # inconsistent across endpoints), so probing would revive a key that
        # will just 403 again on chat/completions. Let the 3600s cooldown
        # expire naturally.
        if not force and state.cooldown_status(key) in (401, 403):
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
            # Reachability restored, throughput unproven: clear the cooldown but
            # leave the adaptive RPM ceiling where the pool learned to put it.
            state.restore_key(key, rehab_rpm=False)
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
    """Once a minute: age out stale failures, then probe expiring cooldowns.

    The failure decay used to be a second task on its own 60s timer, reaching
    into state._key_states from outside the class to decrement a counter. It
    runs on the same schedule as the health check and belongs with it.

    Note what neither of them does: ping healthy keys. That would spend ~25
    requests a minute across the pool, inflate every key's RPM window, and
    manufacture the 429s this whole module exists to avoid.
    """
    try:
        while True:
            await asyncio.sleep(60)
            state.decay_stale_failures()
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


# Origins the dashboard itself can be served from. Anything else asking for
# /api/* or /ops/* is a foreign page using the user's browser as a deputy.
LOCAL_ORIGINS = frozenset(f"http://{host}:1919" for host in ("localhost", "127.0.0.1", "[::1]"))

# Host values that legitimately reach a loopback-bound server. A request whose
# Host is a public name resolving to 127.0.0.1 is DNS rebinding, not a user.
LOCAL_HOSTS = frozenset(
    ["localhost", "127.0.0.1", "::1", "[::1]"]
    + [f"{h}:1919" for h in ("localhost", "127.0.0.1", "[::1]")]
)

GUARDED_PREFIXES = ("/api", "/ops")


TOKEN_HEADER = "x-openvidia-token"


class LocalOnlyMiddleware(BaseHTTPMiddleware):
    """Authenticate the control plane, and stop browsers from riding along.

    Two independent checks, because they defend against different attackers:

    * **A token** (``config.control_token``) — the actual authentication.
      Holding it proves the caller could read a 0600 file owned by this user.
      This is what stops a script: header checks alone did not, because any
      non-browser client can send ``Host: localhost:1919`` and walk straight
      in. That was reachable in practice — a ``tailscale serve`` forward on
      this port let every node on the tailnet reveal keys in cleartext and
      stop the proxy. Loopback binding is no defence there either: the
      forwarder connects from 127.0.0.1 on the peer's behalf, so the source
      address says nothing.

    * **Origin / Host** — kept as defence in depth against the browser. A page
      cannot forge either, so even a token that leaked into a URL the user
      pasted somewhere cannot be replayed cross-origin.

    /v1/* is deliberately unauthenticated: it is the proxy's whole purpose,
    every local client would need the token, and it hands out no secrets.
    """

    def __init__(self, app, token: str):
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith(GUARDED_PREFIXES):
            origin = request.headers.get("origin")
            if origin is not None and origin not in LOCAL_ORIGINS:
                return JSONResponse({"error": "cross-origin request denied"}, status_code=403)
            host = (request.headers.get("host") or "").lower()
            if host and host not in LOCAL_HOSTS:
                return JSONResponse({"error": "invalid host header"}, status_code=403)

            supplied = request.headers.get(TOKEN_HEADER) or request.query_params.get("token") or ""
            # compare_digest, not ==: string comparison returns early on the
            # first differing byte, which leaks the prefix to a timing attack.
            if not secrets.compare_digest(supplied, self._token):
                return JSONResponse(
                    {
                        "error": "control plane requires a token",
                        "hint": f"send it as {TOKEN_HEADER} or ?token=; "
                        f"it lives in {config.control_token_path()}",
                    },
                    status_code=401,
                )
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
    router = EndpointRouter(config.upstream_endpoints())
    emb_cache = EmbeddingCache()
    _redis_url = config.redis_url()
    rs = RedisSync(_redis_url) if _redis_url else None
    if rs is not None:
        state.redis_sync = rs

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
        if rs is not None:
            rs.on_remote = state.apply_remote_event
            await rs.start()
        yield
        if state.health_task is not None:
            state.health_task.cancel()
        if rs is not None:
            await rs.close()
        await client.aclose()

    app = FastAPI(lifespan=lifespan)

    # Not a wildcard. The dashboard is same-origin with the proxy, so it never
    # needs CORS at all; the only reason to answer any origin is to let a
    # foreign page read the response — which is exactly the thing to prevent
    # when one of the responses is the user's API keys.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(LOCAL_ORIGINS),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(LocalOnlyMiddleware, token=config.control_token())
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

        # Gli endpoint alternativi sono fallback: si prova in ordine fino al
        # primo che risponde con la lista modelli.
        for endpoint in router.healthy_endpoints():
            for key in keys:
                if not state.is_key_healthy(key) or not state.key_can_send_rpm(key):
                    continue
                headers = {"Authorization": f"Bearer {key}", "User-Agent": "openvidia/2.0"}
                try:
                    req = client.build_request("GET", endpoint + "models", headers=headers)
                    resp = await client.send(req)
                    if resp.is_success:
                        router.mark_success(endpoint + "models")
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
                        "observed_ceiling": tracker.observed_ceiling
                        if tracker and tracker.observed_ceiling
                        else None,
                        "model_score": _key_model_score(state, key),
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
                "aggregate_observed_ceiling": sum(
                    t.observed_ceiling for t in state.rpm.values() if t.observed_ceiling
                ),
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
                payload["messages"],
                state=state,
                client=client,
                log=state.log_cb,
                model=payload.get("model", "") or "",
            )
            if new_messages is not payload["messages"]:
                payload["messages"] = new_messages
                body = json.dumps(payload).encode()

        # ── Embedding cache (deterministic, RPM-heavy) ────────────────
        # I vettori sono deterministici per (modello, input): la cache evita
        # di bruciare il budget RPM del free tier su richieste identiche
        # ripetute (i client li ricalcolano a ogni restart).
        is_embeddings = (
            isinstance(payload, dict)
            and isinstance(payload.get("input"), (str, list))
            and full_path.endswith("embeddings")
        )
        if is_embeddings:
            cached = emb_cache.get(str(payload.get("model", "")), payload["input"])
            if cached is not None:
                return Response(content=cached, media_type="application/json")

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
            # Respect user's fallback preference: "off" = never failover, return 503
            _opts = config.model_options()
            _fallback_mode = _opts.get("fallback", "off")
            _per = (_opts.get("per_model") or {}).get(requested_model, {})
            _fallback_mode = _per.get("fallback", _fallback_mode)
            if _fallback_mode == "off":
                state.log_cb(f"🔴 {requested_model} circuit OPEN, fallback=off → 503")
                return JSONResponse(
                    {"error": f"{requested_model} is down (circuit open), fallback disabled"},
                    status_code=503,
                )
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
        endpoint_urls = router.healthy_endpoints() or [UPSTREAM_BASE]

        CLIENT_FWD_HEADERS = {"content-type", "accept", "x-request-id", "x-trace-id"}

        # Saturation gate: weigh live (cooldown-free, RPM-eligible) candidates
        # against the FULL pool size, not just len(candidates). The proxy's
        # get_candidate_keys() drops invalid keys and sorts cooldown ones to the
        # tail, so len(candidates) can be small even when the pool is healthy.
        # Using the full pool as the denominator makes the gate fire correctly
        # when most of the 25 keys are on cooldown (the historical Codex block).
        _live, _total_pool = _live_pool_snapshot(state, candidates)
        _pool_saturated = _total_pool > 0 and _live < max(1, int(_total_pool * _MIN_LIVE_FRACTION))
        last_status = 429 if _pool_saturated else 503
        if _pool_saturated:
            state.log_cb(
                f"⚠ pool saturated ({_live}/{_total_pool} live) → skip rotation, try model fallback"
            )

        # Bounded rotation via shared _rotation_phase (same logic as shims).
        # The catch-all passes method + raw content so it supports GET, POST,
        # PUT, etc. — the shims use the default POST + json=payload.
        _payload_for_rotation = payload if isinstance(payload, dict) else {"model": ""}
        _outcome: dict = {}

        def _hdr(k, idx):
            h = {
                "Authorization": f"Bearer {k}",
                "User-Agent": "openvidia/2.0",
            }
            for hk, hv in request.headers.items():
                if hk.lower() in CLIENT_FWD_HEADERS:
                    h[hk] = hv
            if "content-type" not in {hk.lower() for hk in h}:
                h["Content-Type"] = "application/json"
            return h

        # Multi-endpoint failover: si prova il primo endpoint sano; su 5xx o
        # timeout l'host è probabilmente in down parziale, si blacklista per
        # ENDPOINT_RETRY_AFTER e si passa al successivo. Un errore
        # deterministico (400/401/404) non si ripete diversamente su un altro
        # host: ci si ferma subito. Un "exhausted" (budget esaurito) NON è un
        # down dell'host: si prova il prossimo senza blacklist.
        resp = None
        used_key = ""
        used_idx = -1
        for endpoint in endpoint_urls:
            url = endpoint + nv_path
            resp, used_key, used_idx = await _rotation_phase(
                client,
                url,
                _payload_for_rotation,
                _hdr,
                state,
                candidates,
                max_attempts=_MAX_ROTATE_ATTEMPTS,
                timeout=_ROTATE_SEND_TIMEOUT,
                stream=not is_embeddings,
                log_tag="catch-all",
                outcome_box=_outcome,
                method=request.method,
                content=body if body else None,
                probe_timeout=_MODEL_PROBE_TIMEOUT,
            )
            if resp is not None and resp.status_code == 200:
                router.mark_success(url)
                break
            _endpoint_dead = (resp is not None and resp.status_code >= 500) or (
                resp is None and (_outcome.get("status") or 0) >= 500
            )
            if _endpoint_dead and not _outcome.get("exhausted"):
                router.mark_failure(url)
            if _outcome.get("deterministic"):
                break
            resp = None

        if resp is not None and resp.status_code == 200:
            # CORS is the middleware's job. Setting the headers by hand here
            # re-introduced the wildcard the middleware no longer sends, on the
            # one route that carries model output.
            out_headers = {
                k: v for k, v in resp.headers.items() if k.lower() not in STRIPPED_RESPONSE_HEADERS
            }
            state.stats.success += 1
            state.stats.record_key_usage(used_key, ok=True)
            _sent_model = _payload_for_rotation.get("model", "")
            if used_key and _sent_model:
                state.record_key_model_result(used_key, _sent_model, ok=True)

            if is_embeddings:
                emb_body = await resp.aread()
                await resp.aclose()
                state.end_in_flight(used_key)
                if _sent_model:
                    emb_cache.set(_sent_model, payload["input"], emb_body)
                return Response(content=emb_body, media_type="application/json")

            async def body_iter(resp=resp, key=used_key, orig_idx=used_idx):
                try:
                    # aiter_bytes, not aiter_raw: httpx asks for gzip by
                    # default, and content-encoding is stripped from the
                    # forwarded headers above. Passing the raw stream through
                    # would hand the client compressed bytes labelled identity.
                    async for chunk in resp.aiter_bytes():
                        if await request.is_disconnected():
                            break
                        yield chunk
                except httpx.HTTPError as e:
                    # The client already has a 200 and some bytes, so there is
                    # no status left to change. Say so in the stream instead of
                    # closing cleanly, which reads as a complete answer.
                    state.log_cb(f"key[{orig_idx}] stream error: {e}")
                    yield b'\ndata: {"error":"upstream stream interrupted"}\n\n'
                finally:
                    state.end_in_flight(key)
                    await resp.aclose()

            return StreamingResponse(body_iter(), status_code=resp.status_code, headers=out_headers)

        # Rotation failed. _rotation_phase already closed the response and
        # captured the upstream status and body in _outcome.
        if _outcome.get("status"):
            last_status = _outcome["status"]

        model_name = payload.get("model", "") if isinstance(payload, dict) else ""
        # Same exemption the rotation loop applies: a saturated upstream worker
        # pool says nothing about this model, and scoring it here would reopen
        # the circuit the loop just declined to open.
        if model_name and not _outcome.get("exhausted"):
            state.record_model_result(model_name, status=last_status)
            if used_key:
                state.record_key_model_result(used_key, model_name, ok=False)

        # A request the provider rejected outright keeps its own status: a 400
        # reported as 503 tells the client to retry something that cannot work.
        if _outcome.get("deterministic"):
            return JSONResponse(
                {
                    "error": _upstream_error_message(_outcome),
                    "last_upstream_status": last_status,
                },
                status_code=last_status or 400,
            )

        if state.is_pool_throttled():
            retry_in = int(state.pool_throttle_remaining()) + 1
            return JSONResponse(
                {
                    "error": (
                        "upstream rate limit applies to the whole account, not to individual "
                        "keys — rotating cannot help. Retry shortly."
                    ),
                    "last_upstream_status": 429,
                },
                status_code=429,
                headers={"Retry-After": str(retry_in)},
            )

        # ── Provider free-tier fallback (opt-in via providers.json) ──
        # Non tocca le chiavi NVIDIA (niente record_key_usage: il successo
        # non è merito del pool), ma conta come richiesta servita.
        if isinstance(payload, dict) and full_path.endswith("chat/completions"):
            for provider in load_provider_configs():
                fallback_resp = await try_fallback(client, payload, provider)
                if fallback_resp is None:
                    continue
                state.log_cb(f"⇄ fallback → {provider.name}")
                state.stats.success += 1
                f_body = await fallback_resp.aread()
                await fallback_resp.aclose()
                return Response(content=f_body, media_type="application/json")

        msg = "all keys exhausted"
        if model_name:
            msg += f" for {model_name}"
        if model_name and state.is_model_circuit_open(model_name):
            msg += " (circuit open — will auto-failover on next request)"
        return JSONResponse(
            {
                "error": _upstream_error_message(_outcome, msg),
                "last_upstream_status": last_status,
            },
            status_code=last_status,
        )

    # ── /ops/health — live model & pool health dashboard ──────────────
    @app.get("/ops/health")
    async def _ops_health() -> JSONResponse:
        """Structured health report: model circuit states, pool stats, recent logs."""
        now = time.time()
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
                    "aggregate_observed_ceiling": sum(
                        t.observed_ceiling for t in state.rpm.values() if t.observed_ceiling
                    ),
                },
                "endpoints": router.as_dict(),
                "embedding_cache": emb_cache.as_dict(),
                "redis": bool(rs.enabled) if rs is not None else False,
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
