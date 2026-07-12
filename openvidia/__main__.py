"""
OpenVidia — minimal multi-key NVIDIA API proxy with desktop app.

Install:
    pip install -e .

Usage:
    openvidia              # start proxy + desktop window
    openvidia foreground    # foreground mode (logs stdout)
    openvidia setup        # configure opencode provider

Dashboard + API at http://localhost:1919
Edit keys via ~/.config/openvidia/keys.json or dashboard Keys tab.
Keys auto-extracted from accounts.json if keys.json is empty.
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from . import config
from .proxy_state import ProxyStats
from .server_manager import start

PORT = 1919


def _kill_stale_port(port: int):
    """Termina qualsiasi processo in ascolto sulla porta (multipiattaforma via psutil)."""
    import time as _time
    try:
        import psutil
    except ImportError:
        return

    killed = []
    for conn in psutil.net_connections():
        try:
            if conn.laddr.port == port and conn.status == "LISTEN" and conn.pid:
                proc = psutil.Process(conn.pid)
                proc.terminate()
                killed.append(f"{proc.name()}({proc.pid})")
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            continue

    if killed:
        print(f"● Killed stale process on port {port}: {', '.join(killed)}", flush=True)

    # Aspetta che la porta si liberi
    for _ in range(30):
        conns = []
        try:
            conns = psutil.net_connections()
        except (psutil.AccessDenied, OSError):
            return
        still = any(c.laddr.port == port and c.status == "LISTEN" for c in conns)
        if not still:
            return
        _time.sleep(0.1)


def _extract_keys_from_accounts() -> list:
    try:
        p = config.accounts_path()
        if not p.exists():
            return []
        accounts = json.loads(p.read_text())
        keys = []
        for acct in accounts:
            keys.extend(acct.get("keys", []))
        if keys:
            config.save_keys_file(keys)
            print(f"● Extracted {len(keys)} keys from accounts.json")
        return keys
    except Exception:
        return []


def _setup_opencode():
    oc_path = config.opencode_config_path()
    if not oc_path.exists():
        print(f"ℹ opencode not found at {oc_path} — skipping")
        return False
    try:
        cfg = json.loads(oc_path.read_text())
    except (json.JSONDecodeError, OSError):
        print(f"✗ Invalid opencode config at {oc_path}")
        return False

    changed = False
    providers = cfg.setdefault("provider", {})

    # Remove orphan nvidia provider if it points to localhost
    nv = providers.get("nvidia", {})
    if isinstance(nv, dict) and nv.get("options", {}).get("baseURL", "").startswith("http://localhost"):
        del providers["nvidia"]
        changed = True

    if "openvidia" not in providers:
        providers["openvidia"] = {
            "models": {"openvidia": {"name": "OpenVidia", "tools": True}},
            "npm": "@ai-sdk/openai-compatible",
            "options": {
                "apiKey": "ignored",
                "baseURL": f"http://localhost:{PORT}/v1",
            },
        }
        changed = True
        print(f"✓ Added OpenVidia provider to opencode")
    else:
        ov = providers["openvidia"]
        m = ov.setdefault("models", {})
        if "openvidia" not in m:
            m["openvidia"] = {"name": "OpenVidia", "tools": True}
            changed = True
            print(f"✓ Added OpenVidia model to opencode provider")

    # Compaction auto per modelli NVIDIA (contesto più piccolo di Claude)
    comp = cfg.get("compaction")
    if not isinstance(comp, dict) or not comp.get("auto") or not comp.get("prune"):
        cfg["compaction"] = {"auto": True, "prune": True, "reserved": 8000}
        changed = True
        print(f"✓ Enabled auto-compaction (prune=true, reserved=8000)")

    # Modello predefinito → openvidia/openvidia (provider/model_id)
    if cfg.get("model") != "openvidia/openvidia":
        cfg["model"] = "openvidia/openvidia"
        changed = True
        print(f"✓ Default model set to openvidia/openvidia")

    # Small model per task leggeri (titoli, etc.) — stesso provider
    if not cfg.get("small_model"):
        cfg["small_model"] = "openvidia/openvidia"
        changed = True
        print(f"✓ Small model set to openvidia/openvidia")

    # Instructions: punta ad AGENTS.md se esiste nel progetto
    agents_md = Path.cwd() / "AGENTS.md"
    if agents_md.exists():
        instr = cfg.get("instructions", [])
        if "AGENTS.md" not in instr:
            cfg["instructions"] = ["AGENTS.md"] + instr
            changed = True
            print(f"✓ Instructions → AGENTS.md")

    if changed:
        tmp = oc_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2))
        tmp.rename(oc_path)

    print(f"✓ OpenVidia provider ready → http://localhost:{PORT}/v1")
    print(f"✓ Dashboard at http://localhost:{PORT}")
    return True


def _setup_cmd():
    ok = _setup_opencode()
    if ok:
        print("● Run 'opencode' to use the OpenVidia models.")
    sys.exit(0)


async def main_async():
    _kill_stale_port(PORT)
    _setup_opencode()
    keys = config.load_saved_keys_file()
    if not keys:
        keys = _extract_keys_from_accounts()
    if not keys:
        print("✗ No keys found. Add keys to ~/.config/openvidia/keys.json")
        print("  Or run: openvidia setup")
        sys.exit(1)

    stats = ProxyStats(current_index=config.load_saved_index())
    saved_model = config.load_active_model()

    def log(msg: str):
        print(msg, flush=True)

    web_dir = Path(__file__).resolve().parent.parent / "web"
    srv = await start(PORT, keys, log, stats, config.index_path(), web_dir=web_dir, initial_model=saved_model)
    srv.state.log_cb(f"● OpenVidia running on :{PORT} ({len(keys)} keys)")

    # AccountManager: auto-rigenerazione chiavi quando muoiono (dal VECCHIO)
    try:
        from .account_manager import AccountManager
        am = AccountManager(srv.state, config.accounts_path())
        am.set_log_cb(log)
        am.load()
        srv.state.on_key_failed = am.on_key_failed
        asyncio.create_task(am.health_check_loop())
        srv.state.log_cb(f"● AccountManager loaded ({len(am.accounts)} accounts)")
    except ImportError:
        pass  # playwright/websockets non installati — auto-rigenerazione disabilitata
    except Exception as e:
        srv.state.log_cb(f"⚠ AccountManager init failed: {e}")

    # foreground = solo log, niente UI
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        if srv:
            await srv.shutdown()


def open_desk(port: int) -> None:
    """Apre la dashboard in una finestra nativa pywebview."""
    try:
        import webview
    except ImportError:
        print("⚠ pywebview non installato — apertura browser", flush=True)
        from .webui import auto_open
        auto_open(port)
        return

    url = f"http://localhost:{port}"
    assets = Path(__file__).resolve().parent.parent / "web" / "assets"
    icon_path = str(assets / "logo.png")
    # pywebview auto-detect: Qt (KDE Wayland nativo) se disponibile, altrimenti GTK
    print(f"● Desktop window → {url}", flush=True)

    window = webview.create_window(
        "OpenVidia",
        url=url,
        width=310,
        height=570,
        min_size=(260, 300),
        text_select=True,
        easy_drag=True,
    )

    def kill_proxy():
        try:
            import psutil
            for conn in psutil.net_connections():
                if conn.laddr.port == port and conn.status == "LISTEN" and conn.pid:
                    psutil.Process(conn.pid).terminate()
        except Exception:
            pass
        print("● Desk closed — proxy terminated", flush=True)

    window.events.closed += kill_proxy

    webview.start(debug=False, icon=icon_path)


def main():
    if len(sys.argv) > 1:
        if sys.argv[1] == "setup":
            _setup_cmd()
            return
        if sys.argv[1] == "foreground":
            asyncio.run(main_async())
            return

    import subprocess as _sp
    import time as _time
    _kill_stale_port(PORT)

    _sp.Popen(
        [sys.executable, "-m", "openvidia", "foreground"],
        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        stdin=_sp.DEVNULL,
    )

    # Desk app — finestra nativa compatta
    _time.sleep(3)
    open_desk(PORT)


if __name__ == "__main__":
    main()
