import asyncio
import json
import webbrowser
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from . import config
from .proxy_state import ProxyState

# Optional — imported lazily so the app works without account_manager
_account_manager = None


def set_account_manager(am):
    global _account_manager
    _account_manager = am


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

    @app.get("/api/status")
    async def api_status():
        return {
            "running": True,
            "port": state.port,
        }

    @app.get("/api/stats")
    async def api_stats():
        return {
            "requests": state.stats.requests,
            "rotations": state.stats.rotations,
            "success": state.stats.success,
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

    # ── Account management endpoints ──────────────────────────────────

    @app.get("/api/accounts")
    async def api_get_accounts():
        am = _account_manager
        if am is None:
            return {"accounts": []}
        return {"accounts": am.get_accounts_info()}

    @app.post("/api/accounts")
    async def api_add_account(request: Request):
        am = _account_manager
        if am is None:
            return {"ok": False, "error": "account manager not loaded"}
        body = await request.json()
        name = body.get("name", "").strip()
        email = body.get("email", "").strip()
        password = body.get("password", "").strip()
        cookie_json = body.get("cookies", "").strip()
        if not name:
            return {"ok": False, "error": "name required"}
        if not email and not cookie_json:
            return {"ok": False, "error": "email+password or cookies required"}
        try:
            am.add_account(name, email=email, password=password, cookie_json=cookie_json)
            return {"ok": True}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    @app.delete("/api/accounts/{name}")
    async def api_remove_account(name: str):
        am = _account_manager
        if am is None:
            return {"ok": False, "error": "account manager not loaded"}
        try:
            am.remove_account(name)
            return {"ok": True}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    @app.put("/api/accounts/{name}")
    async def api_update_account(name: str, request: Request):
        am = _account_manager
        if am is None:
            return {"ok": False, "error": "account manager not loaded"}
        body = await request.json()
        try:
            am.update_account(
                name,
                email=body.get("email", ""),
                password=body.get("password", ""),
                cookie_json=body.get("cookies", ""),
            )
            return {"ok": True}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    @app.post("/api/accounts/{name}/replenish")
    async def api_trigger_replenish(name: str):
        """Manually trigger key replenishment for an account (for testing)."""
        am = _account_manager
        if am is None:
            return {"ok": False, "error": "account manager not loaded"}
        acct = next((a for a in am.accounts if a.name == name), None)
        if acct is None:
            return {"ok": False, "error": f"account {name!r} not found"}
        if not acct.keys:
            return {"ok": False, "error": "account has no keys to replenish"}
        old_key = acct.keys[0]
        am.on_key_failed(old_key)
        return {"ok": True, "message": f"replenish triggered for {old_key[:12]}…"}

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


def open_browser(port: int = 3940) -> None:
    import threading
    threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{port}")).start()


def auto_open(port: int = 3940) -> None:
    """Open browser when proxy starts, with a small delay."""
    import threading
    threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
