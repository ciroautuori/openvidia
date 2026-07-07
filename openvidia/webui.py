import asyncio
import json
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from . import config
from .proxy_state import ProxyState


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

    @app.get("/health")
    async def health():
        return {"status": "ok", "keys": len(state.keys), "port": state.port}

    @app.get("/api/status")
    async def api_status():
        return {"running": True, "port": state.port}

    @app.get("/api/stats")
    async def api_stats():
        return {
            "requests": state.stats.requests,
            "rotations": state.stats.rotations,
            "success": state.stats.success,
            "active_index": state.stats.active_key_index,
        }

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

    @app.get("/api/keys/stats")
    async def api_key_stats():
        async with state.lock:
            stats = {}
            now = time.time()
            for i, k in enumerate(state.keys):
                u = state.stats.key_usage.get(k)
                if u:
                    stats[str(i)] = {
                        "requests": u.requests,
                        "success": u.success,
                        "failed": u.failed,
                        "last_used": u.last_used,
                        "last_error": u.last_error,
                        "freshness": "fresh" if (now - u.last_used) < 120 else
                                     "stale" if u.last_used > 0 else "unused",
                    }
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
        state.active_model = body.get("model", "") or None
        return {"ok": True, "model": state.active_model or ""}

    @app.get("/api/logs/stream")
    async def log_stream(request: Request):
        async def event_generator():
            last_len = 0
            while True:
                if await request.is_disconnected():
                    break
                buf = state.log_buffer
                current_len = len(buf)
                if current_len > last_len:
                    for i in range(last_len, current_len):
                        yield f"data: {json.dumps({'msg': buf[i]})}\n\n"
                    last_len = current_len
                else:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.2)
        return StreamingResponse(event_generator(), media_type="text/event-stream")


def auto_open(port: int = 3940) -> None:
    threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
