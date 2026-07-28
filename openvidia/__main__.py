"""
OpenVidia — minimal multi-key NVIDIA API proxy with desktop app.

Install:
    pip install -e .

Usage:
    openvidia              # start proxy + desktop window
    openvidia foreground    # foreground mode (logs stdout)
    openvidia setup        # auto-configure ALL detected CLIs (opencode, Codex, Claude Code, Grok)

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

# ---------------------------------------------------------------------------
# Configuration — entrypoint constants
# ---------------------------------------------------------------------------
PORT = 1919
ENV_VAR = "OPENVIDIA_API_KEY"
ENV_VAL = "ignored"
_tray_ref = None  # Global tray reference (anti-GC)
_tray_hide = None  # Global hide-function reference for close-to-tray


def _port_listeners(port: int) -> list:
    """Processes currently LISTENing on ``port`` (never includes ourselves)."""
    try:
        import psutil
    except ImportError:
        return []
    me = os.getpid()
    out = {}
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError):
        return []
    for conn in conns:
        try:
            if (
                conn.laddr
                and conn.laddr.port == port
                and conn.status == "LISTEN"
                and conn.pid
                and conn.pid != me
            ):
                out[conn.pid] = psutil.Process(conn.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            continue
    return list(out.values())


def _kill_stale_port(port: int, *, grace: float = 6.0, hard: float = 4.0) -> bool:
    """Free ``port``, escalating SIGTERM → SIGKILL. Returns True if it is free.

    SIGTERM alone is not enough: uvicorn shuts down gracefully, and an agent
    CLI holding an open SSE stream keeps it alive indefinitely. The previous
    version sent SIGTERM, waited 3s, then returned silently either way — so
    the new instance started next to a survivor that still owned the port,
    and every request kept being served by the OLD code.
    """
    import time as _time

    try:
        import psutil
    except ImportError:
        return True

    procs = _port_listeners(port)
    if not procs:
        return True

    names = ", ".join(f"{p.name()}({p.pid})" for p in procs)
    for p in procs:
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _gone, alive = psutil.wait_procs(procs, timeout=grace)
    if alive:
        for p in alive:
            try:
                p.kill()  # SIGKILL — it ignored the polite request
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        psutil.wait_procs(alive, timeout=hard)

    # The socket can outlive the process briefly; wait for it to be released.
    deadline = _time.monotonic() + hard
    while _time.monotonic() < deadline:
        if not _port_listeners(port):
            print(f"● Freed port {port} (was: {names})", flush=True)
            return True
        _time.sleep(0.1)

    print(
        f"✗ Port {port} is STILL held by {', '.join(f'{p.name()}({p.pid})' for p in _port_listeners(port))} "
        f"— refusing to start a second instance on top of it",
        flush=True,
    )
    return False


def _setup_opencode():
    """Configure the opencode CLI (~/.config/opencode/opencode.json) to use OpenVidia."""
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

    # Remove the orphan nvidia provider if it points to localhost
    nv = providers.get("nvidia", {})
    if isinstance(nv, dict) and nv.get("options", {}).get("baseURL", "").startswith(
        "http://localhost"
    ):
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

    # Auto-compaction for NVIDIA models (smaller context than Claude)
    comp = cfg.get("compaction")
    if not isinstance(comp, dict) or not comp.get("auto") or not comp.get("prune"):
        cfg["compaction"] = {"auto": True, "prune": True, "reserved": 8000}
        changed = True
        print("✓ Enabled auto-compaction (prune=true, reserved=8000)")

    # Default model → openvidia/openvidia (provider/model_id)
    if cfg.get("model") != "openvidia/openvidia":
        cfg["model"] = "openvidia/openvidia"
        changed = True
        print("✓ Default model set to openvidia/openvidia")

    # Small model for lightweight tasks (titles, etc.) — same provider
    if not cfg.get("small_model"):
        cfg["small_model"] = "openvidia/openvidia"
        changed = True
        print("✓ Small model set to openvidia/openvidia")

    # Instructions: point to AGENTS.md if it exists in the project
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


def _ensure_env_var():
    """Ensure OPENVIDIA_API_KEY=ignored is in the shell rc file."""
    shell = os.environ.get("SHELL", "")
    home = Path.home()
    rc = home / ".zshrc"
    if "bash" in shell:
        rc = home / ".bashrc"
    elif "fish" in shell:
        rc = home / ".config" / "fish" / "config.fish"
    else:
        rc = home / ".zshrc"

    rc.parent.mkdir(parents=True, exist_ok=True)

    line = f"export {ENV_VAR}={ENV_VAL}"
    try:
        content = rc.read_text() if rc.exists() else ""
    except OSError:
        content = ""

    if ENV_VAR in content:
        return False

    with open(rc, "a") as f:
        if content and not content.endswith("\n"):
            f.write("\n")
        f.write(f"\n# OpenVidia proxy (NVIDIA NIM multi-key)\n{line}\n")
    print(f"✓ Added {ENV_VAR}={ENV_VAL} to {rc}")
    return True


def _setup_codex():
    """Configure the Codex CLI (~/.codex/config.toml) to use OpenVidia.

    Usa tomlkit per parse→modify→serialize: preserva commenti e sezioni
    esistenti, elimina il rischio di duplicati TOML che rompevano il parse.
    """
    import tomlkit

    codex_dir = Path.home() / ".codex"
    if not codex_dir.exists():
        print("ℹ Codex CLI not found — skipping")
        return False

    cfg_path = codex_dir / "config.toml"
    try:
        doc = tomlkit.parse(cfg_path.read_text()) if cfg_path.exists() else tomlkit.document()
    except Exception as e:
        print(f"✗ Cannot parse {cfg_path}: {e}")
        print("  Backing up and starting fresh")
        if cfg_path.exists():
            cfg_path.rename(cfg_path.with_suffix(".toml.bak-eros"))
        doc = tomlkit.document()

    changed = False

    # Top-level model + model_provider
    if doc.get("model") != "openvidia":
        doc["model"] = "openvidia"
        changed = True
    if doc.get("model_provider") != "openvidia":
        doc["model_provider"] = "openvidia"
        changed = True

    # Provider block: openvidia
    providers = doc.setdefault("model_providers", {})
    if "openvidia" not in providers:
        providers["openvidia"] = {
            "name": "OpenVidia",
            "base_url": f"http://localhost:{PORT}/v1",
            "env_key": ENV_VAR,
            "wire_api": "responses",
        }
        # Model metadata — senza questo Codex mostra "Model metadata not found"
        providers["openvidia"]["models"] = {
            "openvidia": {
                "name": "OpenVidia (NVIDIA NIM via proxy)",
                "context_window": 128000,
                "supports_tool_use": True,
                "supports_parallel_tool_use": True,
                "cost_input_tokens": 0,
                "cost_output_tokens": 0,
            }
        }
        changed = True
    else:
        # Assicurati che i campi critici siano corretti anche se il blocco esiste
        ov = providers["openvidia"]
        if ov.get("base_url") != f"http://localhost:{PORT}/v1":
            ov["base_url"] = f"http://localhost:{PORT}/v1"
            changed = True
        if ov.get("wire_api") != "responses":
            ov["wire_api"] = "responses"
            changed = True
        # Assicurati che il sub-blocco models.openvidia esista
        models = ov.get("models", {})
        if "openvidia" not in models:
            models["openvidia"] = {
                "name": "OpenVidia (NVIDIA NIM via proxy)",
                "context_window": 128000,
                "supports_tool_use": True,
                "supports_parallel_tool_use": True,
                "cost_input_tokens": 0,
                "cost_output_tokens": 0,
            }
            ov["models"] = models
            changed = True

    # Provider block: openai-direct (GPT/Codex — richiede OPENAI_API_KEY sk-...)
    # Non possiamo usare "openai" perché Codex lo riserva come built-in.
    if "openai-direct" not in providers:
        providers["openai-direct"] = {
            "name": "OpenAI Direct",
            "base_url": "https://api.openai.com/v1",
            "env_key": "OPENAI_API_KEY",
            "wire_api": "responses",
        }
        changed = True

    if changed:
        cfg_path.write_text(tomlkit.dumps(doc))
        print("✓ Configured Codex CLI → ~/.codex/config.toml")
    else:
        print("✓ Codex CLI already configured")

    _ensure_env_var()
    print("✓ Codex CLI ready — run: codex --model openvidia")
    return True


def _setup_claude_code():
    """Skip mutating shell rc files for Claude Code to ensure standard native operation is never affected."""
    print("ℹ Claude Code auto-config disabled (use native Claude Code).")
    return True


def _setup_grok():
    """Configure the Grok CLI (~/.grok/config.toml) to use OpenVidia."""
    grok_dir = Path.home() / ".grok"
    if not grok_dir.exists():
        print("ℹ Grok CLI not found — skipping")
        return False

    import tomlkit

    cfg_path = grok_dir / "config.toml"
    try:
        doc = tomlkit.parse(cfg_path.read_text()) if cfg_path.exists() else tomlkit.document()
    except Exception:
        backup = cfg_path.with_suffix(".toml.bak-eros")
        cfg_path.rename(backup)
        print(f"⚠ Grok config parse error — backed up to {backup.name}, starting fresh")
        doc = tomlkit.document()

    # Model provider block
    if "model" not in doc:
        doc["model"] = tomlkit.table()
    model_tbl = doc["model"]
    if "openvidia" not in model_tbl:
        model_tbl["openvidia"] = tomlkit.table()
    ov = model_tbl["openvidia"]
    ov["api_key"] = "ignored"
    ov["base_url"] = f"http://localhost:{PORT}/v1"
    ov["api_backend"] = "chat_completions"
    ov["context_window"] = 128000

    # Default model selection
    if "models" not in doc:
        doc["models"] = tomlkit.table()
    doc["models"]["default"] = "openvidia"

    cfg_path.write_text(tomlkit.dumps(doc))
    print("✓ Configured Grok CLI → ~/.grok/config.toml")
    _ensure_env_var()
    print("✓ Grok CLI ready — run: grok -m openvidia")
    return True


def _setup_proxy_config():
    """Ensure ~/.config/openvidia/proxy_config.json exists with default settings."""
    cfg_dir = config.config_dir()
    p = cfg_dir / "proxy_config.json"
    if not p.exists():
        default_cfg = {
            "outbound_proxy": "",
            "comment": "Set outbound_proxy (e.g. http://user:pass@proxy.example.com:8080) for IP rotation across large key pools",
        }
        config.atomic_write(p, json.dumps(default_cfg, indent=2))
        print(f"✓ Created proxy config template → {p}")
    else:
        print(f"✓ Proxy config ready → {p}")


def _setup_cmd():
    """Full setup: configure every detected CLI (opencode, Codex, Claude Code, Grok)."""
    print("╔════════════════════════════════════════════╗")
    print("║   OpenVidia — Setup CLI auto-config        ║")
    print("╚════════════════════════════════════════════╝")
    print()

    _ensure_env_var()
    print()

    _setup_proxy_config()
    print()

    _setup_opencode()
    print()

    _setup_codex()
    print()

    _setup_claude_code()
    print()

    _setup_grok()
    print()

    print("╔════════════════════════════════════════════╗")
    print("║         Setup complete!                    ║")
    print("╠════════════════════════════════════════════╣")
    print(f"║   Proxy:      http://localhost:{PORT}/v1      ║")
    print(f"║   Dashboard:  http://localhost:{PORT}         ║")
    print("║                                            ║")
    print("║   opencode → /model openvidia              ║")
    print("║   codex    → codex --model openvidia       ║")
    print("║   claude   → claude --model openvidia      ║")
    print("║   grok     → grok -m openvidia             ║")
    print("╚════════════════════════════════════════════╝")

    sys.exit(0)


async def main_async():
    """Foreground entrypoint: start the proxy on the event loop."""
    if not _kill_stale_port(PORT):
        # Starting anyway would leave the OLD process serving every request
        # while this one silently fails to bind — the hardest class of bug to
        # diagnose, because the code on disk is not the code answering.
        print(f"  Free it manually:  fuser -k {PORT}/tcp", flush=True)
        sys.exit(1)
    _setup_opencode()
    _setup_codex()
    _setup_claude_code()
    _setup_grok()
    repaired = config.harden_config_permissions()
    if repaired:
        print(f"  ✓ tightened permissions on {', '.join(repaired)} (was world-readable)")
    keys = config.load_saved_keys_file()
    if not keys:
        print("✗ No keys found. Add keys to ~/.config/openvidia/keys.json")
        print("  Or run: openvidia setup")
        sys.exit(1)

    stats = ProxyStats(current_index=config.load_saved_index())
    saved_model = config.load_active_model()

    def log(msg: str):
        print(msg, flush=True)

    web_dir = Path(__file__).resolve().parent.parent / "web"
    srv = await start(
        PORT,
        keys,
        log,
        stats,
        config.index_path(),
        web_dir=web_dir,
        initial_model=saved_model,
    )
    srv.state.log_cb(f"● OpenVidia running on :{PORT} ({len(keys)} keys)")

    # foreground = logs only, no UI
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        if srv:
            await srv.shutdown()


def _wait_until_serving(port: int, child=None, timeout: float = 20.0) -> bool:
    """Block until the proxy answers on ``port``; report why if it never does."""
    import socket
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if child is not None and child.poll() is not None:
            print(
                f"✗ Proxy exited during startup (code {child.returncode}). "
                f"Run `openvidia foreground` to see the error.",
                flush=True,
            )
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            _time.sleep(0.2)
    print(f"✗ Proxy did not start listening on :{port} within {timeout:.0f}s", flush=True)
    return False


def _kill_proxy_by_port(port: int) -> None:
    """Kill the process listening on the port — used by the tray Quit action.

    Shares the SIGTERM→SIGKILL escalation with startup: a polite terminate
    alone leaves the proxy alive whenever a client is holding a stream open,
    so "Quit" looked like it worked while the port stayed busy.
    """
    try:
        _kill_stale_port(port)
    except Exception:  # noqa: BLE001 — Quit must never raise
        pass


def _make_signaller(icon_path, window, port):
    """Create a QObject in the main thread with a 'create' signal.

    The cross-thread emit is dispatched onto the Qt main loop via
    QueuedConnection, so _create_tray runs in the right thread.
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
    """Build the function passed to webview.start(func=...).

    1) Wait for the QCoreApplication (pywebview creates it)
    2) Wait for the window shown event
    3) Emit the cross-thread signal -> tray on the Qt main loop
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
    """Create a QSystemTrayIcon (executed on the Qt main loop).

    Uses window.native (BrowserView/QMainWindow) for direct show/hide,
    bypassing the pywebview decorators.
    """

    from PyQt6.QtCore import QCoreApplication
    from PyQt6.QtGui import QAction, QIcon
    from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

    app = QCoreApplication.instance()
    if app is None:
        print("⚠ Tray: no QApplication", flush=True)
        return

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("⚠ Tray: not available on this desktop", flush=True)
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
    print("● Tray icon active", flush=True)

    def _on_activated(reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            _show_window()

    tray.activated.connect(_on_activated)

    # Global reference to prevent garbage collection of tray/menu/actions
    global _tray_ref, _tray_hide
    _tray_ref = (tray, menu, show_action, quit_action, _show_window, _on_activated)
    _tray_hide = _hide_window


def open_desk(port: int) -> None:
    """Open the dashboard in a native pywebview window with a system tray."""
    try:
        import webview
    except ImportError:
        print("⚠ pywebview not installed — opening in browser", flush=True)
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
        """Close-to-tray: hide the window, do NOT kill the proxy."""
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
    # NOTE: no killer on `closed` here. With a tray icon the window is a view
    # onto a background service — closing it hides the view, and the proxy
    # must keep serving the CLIs. Only the tray's Quit stops it. (This line
    # used to kill the proxy and appeared harmless purely because the kill was
    # SIGTERM-only and uvicorn ignored it while streams were open; once the
    # kill was made to actually work, closing the window took the proxy down
    # with it.) The no-tray fallback below registers its own handler, because
    # without a tray there is nothing left to stop it from.

    if signaller is not None:
        webview.start(
            func=_tray_waiter_factory(signaller, window),
            debug=False,
            icon=icon_path,
        )
    else:
        # Fallback: no tray, close = kill proxy
        def kill_proxy():
            _kill_proxy_by_port(port)
            print("● Desk closed — proxy terminated", flush=True)

        window.events.closing -= on_closing
        window.events.closed += kill_proxy
        webview.start(debug=False, icon=icon_path)


def main():
    """CLI entrypoint: dispatch on argv[1]."""
    if len(sys.argv) > 1:
        if sys.argv[1] == "setup":
            _setup_cmd()
            return
        if sys.argv[1] == "foreground":
            asyncio.run(main_async())
            return

    import subprocess as _sp

    if not _kill_stale_port(PORT):
        print(f"  Free it manually:  fuser -k {PORT}/tcp", flush=True)
        sys.exit(1)

    child = _sp.Popen(
        [sys.executable, "-m", "openvidia", "foreground"],
        stdout=_sp.DEVNULL,
        stderr=_sp.DEVNULL,
        stdin=_sp.DEVNULL,
    )

    # Wait for the server to actually answer before opening the window.
    # A fixed sleep(3) hid every startup failure: the window opened onto a
    # dead port, or onto a survivor still running the previous build.
    if not _wait_until_serving(PORT, child, timeout=20.0):
        sys.exit(1)

    # Desk app — compact native window
    open_desk(PORT)


if __name__ == "__main__":
    main()
