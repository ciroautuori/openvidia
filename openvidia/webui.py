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


def attach_webui(app: FastAPI, state: ProxyState, web_dir: Path) -> None:

    @app.get("/")
    async def index():
        p = web_dir / "index.html"
        if not p.exists():
            return HTMLResponse("<h1>OpenVidia</h1><p>UI not found</p>", status_code=404)
        return HTMLResponse(p.read_text())

    @app.get("/styles.css")
    async def styles():
        p = web_dir / "styles.css"
        return Response(content=p.read_bytes(), media_type="text/css") if p.exists() else Response("", status_code=404)

    @app.get("/main.js")
    async def main_js():
        p = web_dir / "main.js"
        return Response(content=p.read_bytes(), media_type="application/javascript") if p.exists() else Response("", status_code=404)

    @app.get("/logo.png")
    async def logo():
        for p in [web_dir / "logo.png", web_dir / "assets" / "logo.png"]:
            if p.exists():
                return Response(content=p.read_bytes(), media_type="image/png")
        return Response("", status_code=404)

    @app.get("/favicon.ico")
    async def favicon():
        for p in [web_dir / "favicon.ico", web_dir / "assets" / "favicon.ico", web_dir / "assets" / "logo.png"]:
            if p.exists():
                return Response(content=p.read_bytes(), media_type="image/x-icon")
        return Response("", status_code=404)

    @app.get("/health")
    async def health():
        return {"status": "ok", "keys": len(state.keys), "port": state.port}

    @app.get("/api/status")
    async def api_status():
        async with state.lock:
            on_cooldown = sum(1 for k in state.keys if state.is_key_on_cooldown(k))
        return {
            "running": state.running,
            "port": state.port,
            "keys": len(state.keys),
            "cooldowns": on_cooldown,
        }

    @app.get("/api/stats")
    async def api_stats():
        async with state.lock:
            on_cooldown = sum(1 for k in state.keys if state.is_key_on_cooldown(k))
            total_rpm = sum(state.key_rpm(k) for k in state.keys)
        return {
            "requests": state.stats.requests,
            "rotations": state.stats.rotations,
            "success": state.stats.success,
            "active_index": state.stats.active_key_index,
            "cooldowns": on_cooldown,
            "total_rpm": total_rpm,
        }

    @app.get("/api/accounts")
    async def api_get_accounts():
        keys = list(state.keys)
        accounts = [{"name": "Default", "keys": keys}]
        return {"accounts": accounts, "active_account": ""}

    @app.post("/api/accounts")
    async def api_save_accounts(request: Request):
        body = await request.json()
        accounts = body.get("accounts", [])
        keys = []
        for acct in accounts:
            keys.extend(acct.get("keys", []))
        async with state.lock:
            state.keys = list(keys)
        config.save_keys_file(keys)
        return {"ok": True}

    @app.post("/api/accounts/active")
    async def api_set_active_account(request: Request):
        body = await request.json()
        return {"ok": True}

    @app.get("/api/keys")
    async def api_get_keys():
        async with state.lock:
            return {"keys": list(state.keys)}

    @app.post("/api/keys")
    async def api_save_keys(request: Request):
        body = await request.json()
        keys = body.get("keys", [])
        async with state.lock:
            state.keys = list(keys)
        config.save_keys_file(keys)
        return {"ok": True}

    @app.post("/api/keys/add")
    async def api_add_key(request: Request):
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
    async def api_remove_key(request: Request):
        body = await request.json()
        idx = body.get("index")
        key = body.get("key", "")
        async with state.lock:
            if idx is not None and 0 <= idx < len(state.keys):
                state.keys = [k for i, k in enumerate(state.keys) if i != idx]
            elif key:
                state.keys = [k for k in state.keys if k != key]
            keys = list(state.keys)
        config.save_keys_file(keys)
        return {"ok": True, "keys": keys}

    @app.get("/api/keys/stats")
    async def api_key_stats():
        async with state.lock:
            stats = {}
            now = time.time()
            for i, k in enumerate(state.keys):
                u = state.stats.key_usage.get(k)
                entry = {}
                if u:
                    entry.update({
                        "requests": u.requests,
                        "success": u.success,
                        "failed": u.failed,
                        "last_used": u.last_used,
                        "last_error": u.last_error,
                        "freshness": "fresh" if (now - u.last_used) < 120 else
                                     "stale" if u.last_used > 0 else "unused",
                    })
                # Cooldown / RPM / validità
                cd_rem = state.cooldown_remaining(k)
                entry["cooldown"] = round(cd_rem, 1) if cd_rem > 0 else 0
                entry["cooldown_reason"] = state.cooldown_reason(k) if cd_rem > 0 else ""
                entry["rpm"] = state.key_rpm(k)
                ks = state.key_states.get(k)
                entry["is_valid"] = ks.is_valid if ks else True
                stats[str(i)] = entry
            return {
                "active_index": state.stats.active_key_index,
                "key_stats": stats,
            }

    @app.get("/api/model")
    async def api_get_model():
        return {"model": state.active_model or ""}

    @app.post("/api/model")
    async def api_set_model(request: Request):
        body = await request.json()
        m = body.get("model", "") or None
        state.active_model = m
        config.save_active_model(m or "")
        return {"ok": True, "model": m or ""}

    @app.post("/api/stop")
    async def api_stop():
        state.running = False
        config.save_stop_flag()
        return {"ok": True, "status": "stopped"}

    @app.post("/api/start")
    async def api_start():
        state.running = True
        config.clear_stop_flag()
        return {"ok": True, "status": "running"}

    @app.post("/api/restart")
    async def api_restart():
        import os as _os
        import signal as _signal
        import subprocess as _sp
        # Spawn the new process first, then kill the old one.
        # SIGKILL non esiste su Windows (solo POSIX): li' os.kill con
        # SIGTERM termina il processo (Windows non ha una vera distinzione
        # SIGTERM/SIGKILL — os.kill lo mappa a TerminateProcess).
        _kill_sig = _signal.SIGKILL if sys.platform != "win32" else _signal.SIGTERM

        def _do_restart():
            import time as _time
            _time.sleep(0.3)
            _sp.Popen([sys.executable, "-m", "openvidia"] + sys.argv[1:])
            _time.sleep(0.5)
            _os.kill(_os.getpid(), _kill_sig)
        threading.Thread(target=_do_restart, daemon=True).start()
        return {"ok": True, "restarting": True}

    @app.post("/api/test-model")
    async def api_test_model(request: Request):
        body = await request.json()
        model_id = body.get("model", "")
        if not model_id:
            return {"ok": False, "error": "no model specified"}

        async with state.lock:
            keys = list(state.keys)
        if not keys:
            return {"ok": False, "error": "no keys"}

        payload = {"model": model_id, "messages": [{"role": "user", "content": "ok"}], "max_completion_tokens": 5}
        async with httpx.AsyncClient(timeout=httpx.Timeout(15)) as client:
            for key in keys:
                try:
                    r = await client.post(
                        UPSTREAM_BASE + "chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json=payload,
                    )
                    if r.is_success:
                        d = r.json()
                        txt = d.get("choices", [{}])[0].get("message", {}).get("content", "")
                        return {"ok": True, "model": model_id, "response": txt[:100]}
                    if r.status_code != 400 and r.status_code != 429:
                        continue
                except httpx.HTTPError:
                    continue
                return {"ok": False, "model": model_id, "error": "unavailable", "detail": r.text[:200]}

        return {"ok": False, "model": model_id, "error": "all keys failed"}

    @app.get("/api/presets")
    async def api_get_presets():
        return {"presets": config.load_saved_presets()}

    @app.post("/api/presets")
    async def api_save_presets(request: Request):
        body = await request.json()
        presets = body.get("presets", [])
        config.save_presets_file(presets)
        return {"ok": True}

    @app.get("/api/logs/stream")
    async def log_stream(request: Request):
        async def event_generator():
            q = asyncio.Queue()

            # Invia lo storico attuale
            buf = list(state.log_buffer)
            for msg in buf:
                yield f"data: {json.dumps({'msg': msg})}\n\n"

            # Registra la coda per i nuovi log live (push, non polling)
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


def auto_open(port: int = 3940) -> None:
    threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
