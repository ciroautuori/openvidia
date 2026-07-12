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
import sys
from pathlib import Path

from . import config
from .proxy_state import ProxyStats
from .server_manager import start

PORT = 1919
_tray_ref = None  # Riferimento globale al tray (anti-GC)
_tray_hide = None  # Riferimento alla funzione hide per close-to-tray


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
        print("✓ Added OpenVidia provider to opencode")
    else:
        ov = providers["openvidia"]
        m = ov.setdefault("models", {})
        if "openvidia" not in m:
            m["openvidia"] = {"name": "OpenVidia", "tools": True}
            changed = True
            print("✓ Added OpenVidia model to opencode provider")

    # Compaction auto per modelli NVIDIA (contesto più piccolo di Claude)
    comp = cfg.get("compaction")
    if not isinstance(comp, dict) or not comp.get("auto") or not comp.get("prune"):
        cfg["compaction"] = {"auto": True, "prune": True, "reserved": 8000}
        changed = True
        print("✓ Enabled auto-compaction (prune=true, reserved=8000)")

    # Modello predefinito → openvidia/openvidia (provider/model_id)
    if cfg.get("model") != "openvidia/openvidia":
        cfg["model"] = "openvidia/openvidia"
        changed = True
        print("✓ Default model set to openvidia/openvidia")

    # Small model per task leggeri (titoli, etc.) — stesso provider
    if not cfg.get("small_model"):
        cfg["small_model"] = "openvidia/openvidia"
        changed = True
        print("✓ Small model set to openvidia/openvidia")

    # Instructions: punta ad AGENTS.md se esiste nel progetto
    agents_md = Path.cwd() / "AGENTS.md"
    if agents_md.exists():
        instr = cfg.get("instructions", [])
        if "AGENTS.md" not in instr:
            cfg["instructions"] = ["AGENTS.md"] + instr
            changed = True
            print("✓ Instructions → AGENTS.md")

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


def _kill_proxy_by_port(port: int) -> None:
    """Termina il processo in ascolto sulla porta — usato da Quit del tray."""
    try:
        import psutil
        for conn in psutil.net_connections():
            if conn.laddr.port == port and conn.status == "LISTEN" and conn.pid:
                psutil.Process(conn.pid).terminate()
    except Exception:
        pass


def _make_signaller(icon_path, window, port):
    """Crea un QObject nel main thread con segnale 'create'.

    L'emit cross-thread atterra sul Qt main loop via QueuedConnection,
    eseguendo _create_tray nel thread giusto.
    """
    try:
        from PyQt6.QtCore import QObject, pyqtSignal
    except ImportError:
        return None

    class _Sig(QObject):
        create = pyqtSignal()

    s = _Sig()
    s.create.connect(lambda: _create_tray(icon_path, window, port))
    return s


def _tray_waiter_factory(signaller, window):
    """Crea la funzione per webview.start(func=...).

    1) Aspetta QCoreApplication (pywebview l'ha creato)
    2) Aspetta la finestra mostrata
    3) Emette segnale cross-thread -> tray sul Qt main loop
    """
    import time as _time

    def _waiter():
        from PyQt6.QtCore import QCoreApplication

        for _ in range(100):
            if QCoreApplication.instance() is not None:
                break
            _time.sleep(0.2)
        else:
            return

        if not window.events.shown.wait(20):
            return

        signaller.create.emit()

    return _waiter


def _create_tray(icon_path: str, window, port: int):
    """Crea QSystemTrayIcon (eseguito sul Qt main loop).

    Usa window.native (BrowserView/QMainWindow) per show/hide diretto,
    bypassando i decorator pywebview."""

    from PyQt6.QtCore import QCoreApplication
    from PyQt6.QtGui import QAction, QIcon
    from PyQt6.QtWidgets import QSystemTrayIcon, QMenu

    app = QCoreApplication.instance()
    if app is None:
        print("⚠ Tray: nessun QApplication", flush=True)
        return

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("⚠ Tray: non disponibile su questo desktop", flush=True)
        return

    def _show_window():
        w = window.native
        if w is None:
            return
        w.show()
        w.raise_()
        w.activateWindow()

    def _hide_window():
        w = window.native
        if w is None:
            return
        w.hide()

    icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
    tray = QSystemTrayIcon(icon)
    tray.setToolTip("OpenVidia")

    menu = QMenu()

    show_action = QAction("Show")
    show_action.triggered.connect(_show_window)
    menu.addAction(show_action)

    menu.addSeparator()

    quit_action = QAction("Quit")
    quit_action.triggered.connect(lambda: (_kill_proxy_by_port(port), app.quit()))
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.show()
    print("● Tray icon attiva", flush=True)

    def _on_activated(reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            _show_window()

    tray.activated.connect(_on_activated)

    # Riferimento globale per evitare garbage collection di tray/menu/azioni
    global _tray_ref, _tray_hide
    _tray_ref = (tray, menu, show_action, quit_action, _show_window, _on_activated)
    _tray_hide = _hide_window


def open_desk(port: int) -> None:
    """Apre la dashboard in una finestra nativa pywebview con system tray."""
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

    signaller = _make_signaller(icon_path, window, port)

    def on_closing():
        """Close-to-tray: nasconde la finestra, NON uccide il proxy."""
        try:
            h = _tray_hide
            if h is not None:
                h()
            else:
                window.hide()
        except Exception:
            pass
        return False

    window.events.closing += on_closing
    window.events.closed += lambda: _kill_proxy_by_port(port)

    if signaller is not None:
        webview.start(
            func=_tray_waiter_factory(signaller, window),
            debug=False,
            icon=icon_path,
        )
    else:
        # Fallback: niente tray, close = kill proxy
        def kill_proxy():
            _kill_proxy_by_port(port)
            print("● Desk closed — proxy terminated", flush=True)

        window.events.closing -= on_closing
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
