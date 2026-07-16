"""
OpenVidia — minimal multi-key NVIDIA API proxy with desktop app.

Install:
    pip install -e .

Usage:
    openvidia              # start proxy + desktop window
    openvidia foreground    # foreground mode (logs stdout)
    openvidia setup        # auto-configure ALL detected CLIs (opencode, Codex, Grok)

Dashboard + API at http://localhost:1919
Edit keys via ~/.config/openvidia/keys.json or dashboard Keys tab.
Keys auto-extracted from accounts.json if keys.json is empty.
"""

import asyncio
import json
import os
import re
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


def _kill_stale_port(port: int):
    """Kill any process listening on the given port (cross-platform via psutil)."""
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

    # Wait for the port to become free
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
    """Configure the Codex CLI (~/.codex/config.toml) to use OpenVidia."""
    codex_dir = Path.home() / ".codex"
    if not codex_dir.exists():
        print("ℹ Codex CLI not found — skipping")
        return False

    cfg_path = codex_dir / "config.toml"
    content = cfg_path.read_text() if cfg_path.exists() else ""

    changed = False
    needs_model = not re.search(r'^model\s*=\s*"openvidia"', content, re.MULTILINE)
    needs_provider = not re.search(
        r'^model_provider\s*=\s*"openvidia"', content, re.MULTILINE
    )
    needs_block = "[model_providers.openvidia]" not in content

    if needs_model or needs_provider or needs_block:
        lines = content.splitlines()
        new_lines = []
        in_openvidia_block = False
        model_set = False
        provider_set = False

        for line in lines:
            stripped = line.strip()

            # Skip old model= / model_provider= lines so they get overwritten
            if stripped.startswith("model ") or stripped.startswith("model="):
                if not model_set:
                    new_lines.append('model = "openvidia"')
                    model_set = True
                    if needs_model:
                        changed = True
                    continue
            if stripped.startswith("model_provider ") or stripped.startswith(
                "model_provider="
            ):
                if not provider_set:
                    new_lines.append('model_provider = "openvidia"')
                    provider_set = True
                    if needs_provider:
                        changed = True
                    continue

            # Skip the old [model_providers.openvidia] block if present
            if stripped == "[model_providers.openvidia]":
                in_openvidia_block = True
                continue
            if (
                in_openvidia_block
                and stripped.startswith("[")
                and stripped != "[model_providers.openvidia]"
            ):
                in_openvidia_block = False
            if in_openvidia_block:
                continue

            new_lines.append(line)

        # Prepend model/model_provider if not yet set
        if not model_set:
            new_lines.insert(0, 'model = "openvidia"')
        if not provider_set:
            new_lines.insert(1, 'model_provider = "openvidia"')

        # Append provider block at the end
        new_lines.append("")
        new_lines.append("# Provider custom: openvidia (NVIDIA NIM multi-key proxy)")
        new_lines.append("[model_providers.openvidia]")
        new_lines.append('name = "OpenVidia"')
        new_lines.append(f'base_url = "http://localhost:{PORT}/v1"')
        new_lines.append(f'env_key = "{ENV_VAR}"')
        new_lines.append('wire_api = "responses"')
        new_lines.append("")
        changed = True

        if changed:
            cfg_path.write_text("\n".join(new_lines))
            print("✓ Configured Codex CLI → ~/.codex/config.toml")
    else:
        print("✓ Codex CLI already configured")

    # Also set env var in auth.json if Codex needs it
    _ensure_env_var()
    print("✓ Codex CLI ready — run: codex --model openvidia")
    return True


def _setup_grok():
    """Configure the Grok CLI (~/.grok/config.toml) to use OpenVidia."""
    grok_dir = Path.home() / ".grok"
    if not grok_dir.exists():
        print("ℹ Grok CLI not found — skipping")
        return False

    cfg_path = grok_dir / "config.toml"
    content = cfg_path.read_text() if cfg_path.exists() else ""

    has_model = re.search(r"^\[model\.openvidia\]", content, re.MULTILINE)
    has_default = re.search(r'^default\s*=\s*"openvidia"', content, re.MULTILINE)

    if has_model and has_default:
        print("✓ Grok CLI already configured")
        return True

    block = """
# Provider custom: openvidia (NVIDIA NIM multi-key proxy)
[model.openvidia]
api_key = "ignored"
base_url = "http://localhost:{PORT}/v1"
api_backend = "chat_completions"
context_window = 128000
""".format(PORT=PORT)

    lines = content.splitlines()
    new_lines = []
    default_set = False
    in_models_section = False
    skip_old_openvidia = False

    for line in lines:
        stripped = line.strip()

        # Track the [models] section
        if stripped == "[models]":
            in_models_section = True
            new_lines.append(line)
            continue
        elif stripped.startswith("[") and stripped != "[models]":
            in_models_section = False

        # Replace the default model inside the [models] section
        if in_models_section and (
            stripped.startswith("default ") or stripped.startswith("default=")
        ):
            if not default_set:
                new_lines.append('default = "openvidia"')
                default_set = True
                continue

        # Skip the old [model.openvidia] block
        if stripped == "[model.openvidia]":
            skip_old_openvidia = True
            continue
        if (
            skip_old_openvidia
            and stripped.startswith("[")
            and stripped != "[model.openvidia]"
        ):
            skip_old_openvidia = False
        if skip_old_openvidia:
            continue

        new_lines.append(line)

    if not default_set:
        # Ensure a [models] section exists with the default
        if "[models]" not in "\n".join(new_lines):
            new_lines.insert(0, "[models]")
            new_lines.insert(1, 'default = "openvidia"')
            new_lines.insert(2, "")
        else:
            # Insert right after the [models] header
            for i, line in enumerate(new_lines):
                if line.strip() == "[models]":
                    new_lines.insert(i + 1, 'default = "openvidia"')
                    break
        default_set = True

    new_lines.append(block)

    cfg_path.write_text("\n".join(new_lines))
    print("✓ Configured Grok CLI → ~/.grok/config.toml")
    _ensure_env_var()
    print("✓ Grok CLI ready — run: grok -m openvidia")
    return True


def _setup_cmd():
    """Full setup: configure every detected CLI (opencode, Codex, Grok)."""
    print("╔════════════════════════════════════════════╗")
    print("║   OpenVidia — Setup CLI auto-config        ║")
    print("╚════════════════════════════════════════════╝")
    print()

    _ensure_env_var()
    print()

    _setup_opencode()
    print()

    _setup_codex()
    print()

    _setup_grok()
    print()

    print("╔════════════════════════════════════════════╗")
    print("║   Setup complete!                         ║")
    print("╠════════════════════════════════════════════╣")
    print(f"║   Proxy:       http://localhost:{PORT}/v1")
    print(f"║   Dashboard:   http://localhost:{PORT}")
    print("║                                            ║")
    print("║   opencode → /model openvidia              ║")
    print("║   codex    → codex --model openvidia       ║")
    print("║   grok     → grok -model openvidia         ║")
    print("╚════════════════════════════════════════════╝")

    sys.exit(0)


async def main_async():
    """Foreground entrypoint: start the proxy on the event loop."""
    _kill_stale_port(PORT)
    _setup_opencode()
    _setup_codex()
    _setup_grok()
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

    # AccountManager: auto-regenerate keys when they die
    try:
        from .account_manager import AccountManager

        am = AccountManager(srv.state, config.accounts_path())
        am.set_log_cb(log)
        am.load()
        srv.state.on_key_failed = am.on_key_failed
        asyncio.create_task(am.health_check_loop())
        srv.state.log_cb(f"● AccountManager loaded ({len(am.accounts)} accounts)")
    except ImportError:
        pass  # playwright/websockets not installed — auto-regen disabled
    except Exception as e:
        srv.state.log_cb(f"⚠ AccountManager init failed: {e}")

    # foreground = logs only, no UI
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        if srv:
            await srv.shutdown()


def _kill_proxy_by_port(port: int) -> None:
    """Kill the process listening on the port — used by the tray Quit action."""
    try:
        import psutil

        for conn in psutil.net_connections():
            if conn.laddr.port == port and conn.status == "LISTEN" and conn.pid:
                psutil.Process(conn.pid).terminate()
    except Exception:
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
    from PyQt6.QtWidgets import QSystemTrayIcon, QMenu

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
    window.events.closed += lambda: _kill_proxy_by_port(port)

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
    import time as _time

    _kill_stale_port(PORT)

    _sp.Popen(
        [sys.executable, "-m", "openvidia", "foreground"],
        stdout=_sp.DEVNULL,
        stderr=_sp.DEVNULL,
        stdin=_sp.DEVNULL,
    )

    # Desk app — compact native window
    _time.sleep(3)
    open_desk(PORT)


if __name__ == "__main__":
    main()
