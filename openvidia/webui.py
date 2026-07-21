"""Embedded control panel served by the proxy itself.

A thin FastAPI router mounted on the same process that runs the proxy:
serves the static dashboard bundle plus a small REST/SSE surface for
key management, stats, presets and live logs. All mutators persist via
``config`` so state survives restarts; reads mirror ``ProxyState``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import webbrowser
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from . import config
from .proxy_state import ProxyState

UPSTREAM_BASE = "https://integrate.api.nvidia.com/v1/"


# --------------------------------------------------------------------------- #
# Static dashboard bundle
# --------------------------------------------------------------------------- #


def attach_webui(app: FastAPI, state: ProxyState, web_dir: Path) -> None:
    """Register the dashboard + management API on ``app``.

    Routes are closures over ``state`` and ``web_dir`` so the single
    ``ProxyState`` instance stays the source of truth for both the proxy
    and the UI.
    """

    @app.get("/")
    async def index() -> HTMLResponse:
        p = web_dir / "index.html"
        if not p.exists():
            return HTMLResponse(
                "<h1>OpenVidia</h1><p>UI not found</p>", status_code=404
            )
        return HTMLResponse(p.read_text())

    @app.get("/styles.css")
    async def styles() -> Response:
        p = web_dir / "styles.css"
        return (
            Response(content=p.read_bytes(), media_type="text/css")
            if p.exists()
            else Response("", status_code=404)
        )

    @app.get("/main.js")
    async def main_js() -> Response:
        p = web_dir / "main.js"
        return (
            Response(content=p.read_bytes(), media_type="application/javascript")
            if p.exists()
            else Response("", status_code=404)
        )

    @app.get("/logo.png")
    async def logo() -> Response:
        for p in [web_dir / "logo.png", web_dir / "assets" / "logo.png"]:
            if p.exists():
                return Response(content=p.read_bytes(), media_type="image/png")
        return Response("", status_code=404)

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        for p in [
            web_dir / "favicon.ico",
            web_dir / "assets" / "favicon.ico",
            web_dir / "assets" / "logo.png",
        ]:
            if p.exists():
                return Response(content=p.read_bytes(), media_type="image/x-icon")
        return Response("", status_code=404)

    # ----------------------------------------------------------------------- #
    # Health & status
    # ----------------------------------------------------------------------- #

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "keys": len(state.keys), "port": state.port}

    @app.get("/api/status")
    async def api_status() -> dict:
        async with state.lock:
            on_cooldown = sum(1 for k in state.keys if state.is_key_on_cooldown(k))
        return {
            "running": state.running,
            "port": state.port,
            "keys": len(state.keys),
            "cooldowns": on_cooldown,
        }

    @app.get("/api/stats")
    async def api_stats() -> dict:
        async with state.lock:
            on_cooldown = sum(1 for k in state.keys if state.is_key_on_cooldown(k))
            total_rpm = sum(state.key_rpm(k) for k in state.keys)
            n_healthy = sum(1 for k in state.keys if state.is_key_healthy(k))
            in_flight = sum(
                (state._key_states.get(k).in_flight if state._key_states.get(k) else 0)
                for k in state.keys
            )
            agg_ceiling = sum(
                (state.rpm.get(k).max_rpm if state.rpm.get(k) and state.rpm.get(k).max_rpm else 28)
                for k in state.keys
            )
        return {
            "requests": state.stats.requests,
            "rotations": state.stats.rotations,
            "success": state.stats.success,
            "active_index": state.stats.active_key_index,
            "cooldowns": on_cooldown,
            "total_rpm": total_rpm,
            "healthy": n_healthy,
            "in_flight": in_flight,
            "aggregate_rpm_ceiling": agg_ceiling,
        }

    # ----------------------------------------------------------------------- #
    # Accounts (legacy single-bucket surface kept for UI compat)
    # ----------------------------------------------------------------------- #

    @app.get("/api/accounts")
    async def api_get_accounts() -> dict:
        keys = list(state.keys)
        accounts = [{"name": "Default", "keys": keys}]
        return {"accounts": accounts, "active_account": ""}

    @app.post("/api/accounts")
    async def api_save_accounts(request: Request) -> dict:
        body = await request.json()
        accounts = body.get("accounts", [])
        keys: list[str] = []
        for acct in accounts:
            keys.extend(acct.get("keys", []))
        async with state.lock:
            state.keys = list(keys)
        config.save_keys_file(keys)
        return {"ok": True}

    @app.post("/api/accounts/active")
    async def api_set_active_account(request: Request) -> dict:
        await request.json()
        return {"ok": True}

    # ----------------------------------------------------------------------- #
    # Keys CRUD
    # ----------------------------------------------------------------------- #

    @app.get("/api/keys")
    async def api_get_keys() -> dict:
        async with state.lock:
            return {"keys": list(state.keys)}

    @app.post("/api/keys")
    async def api_save_keys(request: Request) -> dict:
        body = await request.json()
        keys = body.get("keys", [])
        async with state.lock:
            state.keys = list(keys)
        config.save_keys_file(keys)
        return {"ok": True}

    @app.post("/api/keys/add")
    async def api_add_key(request: Request) -> dict:
        body = await request.json()
        key = body.get("key", "")
        if not key:
            return {"ok": False, "error": "key required"}
        async with state.lock:
            state.keys = list(state.keys) + [key]
            keys = list(state.keys)
        config.save_keys_file(keys)
        return {"ok": True, "keys": keys}

    @app.post("/api/keys/remove")
    async def api_remove_key(request: Request) -> dict:
        body = await request.json()
        idx = body.get("index")
        key = body.get("key", "")
        async with state.lock:
            # Allow removal by index or by value; index wins when both are
            # provided and in range, matching what the dashboard sends.
            if idx is not None and 0 <= idx < len(state.keys):
                state.keys = [k for i, k in enumerate(state.keys) if i != idx]
            elif key:
                state.keys = [k for k in state.keys if k != key]
            keys = list(state.keys)
        config.save_keys_file(keys)
        return {"ok": True, "keys": keys}

    # ----------------------------------------------------------------------- #
    # Per-key live stats (cooldown, RPM, validity, freshness)
    # ----------------------------------------------------------------------- #

    @app.get("/api/keys/stats")
    async def api_key_stats() -> dict:
        async with state.lock:
            stats: dict[str, dict] = {}
            now = time.time()
            for i, k in enumerate(state.keys):
                u = state.stats.key_usage.get(k)
                entry: dict = {}
                if u:
                    entry.update(
                        {
                            "requests": u.requests,
                            "success": u.success,
                            "failed": u.failed,
                            "last_used": u.last_used,
                            "last_error": u.last_error,
                            "freshness": "fresh"
                            if (now - u.last_used) < 120
                            else "stale"
                            if u.last_used > 0
                            else "unused",
                        }
                    )
                # Cooldown remaining, current RPM, last probe validity,
                # adaptive RPM ceiling, in-flight count and consecutive failures
                # (used by the dashboard to show adaptive backoff state).
                cd_rem = state.cooldown_remaining(k)
                entry["cooldown"] = round(cd_rem, 1) if cd_rem > 0 else 0
                entry["cooldown_reason"] = (
                    state.cooldown_reason(k) if cd_rem > 0 else ""
                )
                entry["rpm"] = state.key_rpm(k)
                tracker = state.rpm.get(k)
                entry["rpm_ceiling"] = (
                    tracker.max_rpm if tracker and tracker.max_rpm else 28
                )
                ks = state.key_states.get(k)
                entry["is_valid"] = ks.is_valid if ks else True
                entry["in_flight"] = ks.in_flight if ks else 0
                entry["consecutive_failures"] = ks.consecutive_failures if ks else 0
                stats[str(i)] = entry
            return {
                "active_index": state.stats.active_key_index,
                "key_stats": stats,
            }

    # ----------------------------------------------------------------------- #
    # Active model
    # ----------------------------------------------------------------------- #

    @app.get("/api/model")
    async def api_get_model() -> dict:
        return {"model": state.active_model or ""}

    @app.post("/api/model")
    async def api_set_model(request: Request) -> dict:
        body = await request.json()
        m = body.get("model", "") or None
        state.active_model = m
        config.save_active_model(m or "")
        return {"ok": True, "model": m or ""}

    @app.get("/api/thinking")
    async def api_get_thinking() -> dict:
        opts = config.model_options()
        model = state.active_model or ""
        per = (opts.get("per_model") or {}).get(model, {})
        return {
            "model": model,
            "mode": per.get("thinking") or opts.get("thinking", "auto"),
            "inherited": "thinking" not in per,
        }

    @app.post("/api/thinking")
    async def api_set_thinking(request: Request) -> dict:
        """Set the reasoning mode for the active model (or globally).

        A hybrid reasoning model emits nothing until it stops thinking, which
        is the difference between a 2s and a 160s first token — worth a switch
        rather than a config file edit.
        """
        body = await request.json()
        mode = body.get("mode", "auto")
        if mode not in ("auto", "on", "off"):
            return {"ok": False, "error": "mode must be auto, on or off"}
        opts = config.model_options()
        model = body.get("model", state.active_model or "")
        if model:
            per = dict(opts.get("per_model") or {})
            entry = dict(per.get(model) or {})
            if mode == "auto":
                entry.pop("thinking", None)
            else:
                entry["thinking"] = mode
            if entry:
                per[model] = entry
            else:
                per.pop(model, None)
            opts["per_model"] = per
        else:
            opts["thinking"] = mode
        config.save_model_options(opts)
        state.log_cb(f"◆ thinking={mode} for {model or 'all models'}")
        return {"ok": True, "model": model, "mode": mode}

    @app.get("/api/model-health")
    async def api_model_health() -> dict:
        """What the proxy has learned about each model from real traffic."""
        return {
            m: h.as_dict() for m, h in sorted(state.model_health.items())
        }

    # ----------------------------------------------------------------------- #
    # Lifecycle: stop / start / restart
    # ----------------------------------------------------------------------- #

    @app.post("/api/stop")
    async def api_stop() -> dict:
        state.running = False
        config.save_stop_flag()
        return {"ok": True, "status": "stopped"}

    @app.post("/api/start")
    async def api_start() -> dict:
        state.running = True
        config.clear_stop_flag()
        return {"ok": True, "status": "running"}

    @app.post("/api/restart")
    async def api_restart() -> dict:
        import os as _os
        import signal as _signal
        import subprocess as _sp

        # Spawn the replacement process first, then kill the current one so
        # there is no window where no proxy is listening. SIGKILL is POSIX
        # only: on Windows os.kill maps SIGTERM to TerminateProcess anyway.
        _kill_sig = _signal.SIGKILL if sys.platform != "win32" else _signal.SIGTERM

        def _do_restart() -> None:
            import time as _time

            _time.sleep(0.3)
            _sp.Popen([sys.executable, "-m", "openvidia"] + sys.argv[1:])
            _time.sleep(0.5)
            _os.kill(_os.getpid(), _kill_sig)

        threading.Thread(target=_do_restart, daemon=True).start()
        return {"ok": True, "restarting": True}

    # ----------------------------------------------------------------------- #
    # Model probe — fire a 5-token chat completion against upstream
    # ----------------------------------------------------------------------- #

    @app.post("/api/test-model")
    async def api_test_model(request: Request) -> dict:
        body = await request.json()
        model_id = body.get("model", "")
        if not model_id:
            return {"ok": False, "error": "no model specified"}

        async with state.lock:
            keys = list(state.keys)
        if not keys:
            return {"ok": False, "error": "no keys"}

        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": "ok"}],
            "max_completion_tokens": 5,
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(15)) as client:
            for key in keys:
                try:
                    r = await client.post(
                        UPSTREAM_BASE + "chat/completions",
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                    if r.is_success:
                        d = r.json()
                        txt = (
                            d.get("choices", [{}])[0]
                            .get("message", {})
                            .get("content", "")
                        )
                        return {"ok": True, "model": model_id, "response": txt[:100]}
                    # 400/429 are key/model-level rejections worth surfacing;
                    # everything else is treated as transient and skipped.
                    if r.status_code != 400 and r.status_code != 429:
                        continue
                except httpx.HTTPError:
                    continue
                return {
                    "ok": False,
                    "model": model_id,
                    "error": "unavailable",
                    "detail": r.text[:200],
                }

        return {"ok": False, "model": model_id, "error": "all keys failed"}

    # ----------------------------------------------------------------------- #
    # Presets (saved key sets)
    # ----------------------------------------------------------------------- #

    @app.get("/api/presets")
    async def api_get_presets() -> dict:
        return {"presets": config.load_saved_presets()}

    @app.post("/api/presets")
    async def api_save_presets(request: Request) -> dict:
        body = await request.json()
        presets = body.get("presets", [])
        config.save_presets_file(presets)
        return {"ok": True}

    # ----------------------------------------------------------------------- #
    # Live log stream (SSE)
    # ----------------------------------------------------------------------- #

    @app.get("/api/logs/stream")
    async def log_stream(request: Request) -> StreamingResponse:
        async def event_generator():
            q: asyncio.Queue[str] = asyncio.Queue()

            # Replay the current buffer first so a reconnect doesn't lose
            # the recent history visible before the connection opened.
            buf = list(state.log_buffer)
            for msg in buf:
                yield f"data: {json.dumps({'msg': msg})}\n\n"

            # Register for live push (no polling) and stream until the
            # client disconnects; heartbeats keep the socket warm during
            # quiet periods without violating the SSE contract.
            state.listeners.add(q)
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=2.0)
                        yield f"data: {json.dumps({'msg': msg})}\n\n"
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
            finally:
                state.listeners.discard(q)

        return StreamingResponse(event_generator(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# Convenience: open the dashboard in the user's default browser
# --------------------------------------------------------------------------- #


def auto_open(port: int = 3940) -> None:
    """Best-effort browser launch 1.5 s after call.

    The delay gives the proxy a head start so the browser doesn't race
    the listener and show a connection-refused page on slow machines.
    """
    threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
