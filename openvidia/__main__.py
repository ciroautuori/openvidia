"""
OpenVidia — minimal multi-key NVIDIA API proxy with desktop app.

Install:
    pip install -e .

Usage:
    openvidia                    # start proxy + desktop window
    openvidia foreground         # foreground mode (logs stdout)
    openvidia setup              # configure opencode (the only persistent edit)
    openvidia run <cli> [args]   # run any CLI against the proxy, writing nothing

Dashboard + API at http://localhost:1919
Edit keys via ~/.config/openvidia/keys.json or dashboard Keys tab.
"""

import asyncio
import json
import os
import shutil
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

    # Auto-compaction, but only as a default. Forcing it back on every run
    # overrode users who had deliberately turned it off.
    if "compaction" not in cfg:
        cfg["compaction"] = {"auto": True, "prune": True, "reserved": 8000}
        changed = True
        print("✓ Enabled auto-compaction (prune=true, reserved=8000)")

    # Select the provider only when nothing is selected. This used to be
    # unconditional and ran on every proxy start, so a user who picked another
    # provider in opencode found it reset the next time OpenVidia launched.
    if not cfg.get("model"):
        cfg["model"] = "openvidia/openvidia"
        changed = True
        print("✓ Default model set to openvidia/openvidia")
    elif cfg.get("model") != "openvidia/openvidia":
        print(f"ℹ Leaving your opencode model as {cfg['model']} — switch with /model openvidia")

    if not cfg.get("small_model"):
        cfg["small_model"] = "openvidia/openvidia"
        changed = True
        print("✓ Small model set to openvidia/openvidia")

    # NOTE: the previous version also prepended Path.cwd()/AGENTS.md to
    # opencode's GLOBAL instructions. Launched from this repo — which the
    # desktop launcher does — that meant every opencode project everywhere
    # started loading OpenVidia's own AGENTS.md. A proxy has no business
    # deciding what instructions another tool reads.

    if changed:
        # Someone else's config file: back it up before touching it. This is
        # the protection save_keys_file already gave OpenVidia's own data.
        try:
            from .safe_file import create_backup

            create_backup(oc_path)
        except OSError as e:
            print(f"✗ could not back up {oc_path}: {e} — not writing")
            return False
        config.atomic_write(oc_path, json.dumps(cfg, indent=2), mode=0o644)

    print(f"✓ OpenVidia provider ready → http://localhost:{PORT}/v1")
    print(f"✓ Dashboard at http://localhost:{PORT}")
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


# ---------------------------------------------------------------------------
# Running another CLI against the proxy
# ---------------------------------------------------------------------------
# The proxy used to reconfigure opencode, Codex, Claude Code and Grok on every
# single start — not just on `setup`. That reset the user's chosen opencode
# model to openvidia/openvidia on each launch, re-enabled compaction settings
# they had turned off, rewrote ~/.codex/config.toml and ~/.grok/config.toml
# without a backup, appended to shell rc files (with bash syntax, even under
# fish, so every new shell printed an error), and injected whatever AGENTS.md
# happened to be in the launch directory into opencode's GLOBAL instructions.
#
# `openvidia run <cli>` replaces all of that with the ollama model: nothing on
# disk changes, ever. The environment is set for one child process and the CLI
# is exec'd into it.
BASE_URL = f"http://localhost:{PORT}/v1"
ROOT_URL = f"http://localhost:{PORT}"

# Defaults ship for the variables these tools document. The table is
# user-editable at ~/.config/openvidia/cli_targets.json so a CLI that renames
# its variables tomorrow is a config edit rather than a release — the same
# choice model_options.json already makes for payload flags.
_DEFAULT_CLI_TARGETS: dict[str, dict] = {
    "opencode": {
        "argv": ["opencode"],
        "env": {"OPENAI_BASE_URL": BASE_URL, "OPENAI_API_KEY": ENV_VAL, ENV_VAR: ENV_VAL},
    },
    "claude": {
        "argv": ["claude"],
        "env": {"ANTHROPIC_BASE_URL": ROOT_URL, "ANTHROPIC_API_KEY": ENV_VAL},
    },
    "codex": {
        "argv": ["codex"],
        "env": {"OPENAI_BASE_URL": BASE_URL, "OPENAI_API_KEY": ENV_VAL, ENV_VAR: ENV_VAL},
        # Codex resolves providers from ~/.codex/config.toml, so the base URL
        # alone may not be enough. We refuse to write that file behind the
        # user's back; print it instead and let them decide.
        "note": (
            "Codex may also need a provider block in ~/.codex/config.toml.\n"
            "  If it does not pick up the proxy, add once:\n"
            "\n"
            '    model = "openvidia"\n'
            '    model_provider = "openvidia"\n'
            "\n"
            "    [model_providers.openvidia]\n"
            '    name = "OpenVidia"\n'
            f'    base_url = "{BASE_URL}"\n'
            f'    env_key = "{ENV_VAR}"\n'
            '    wire_api = "responses"'
        ),
    },
    "grok": {
        "argv": ["grok"],
        "env": {"OPENAI_BASE_URL": BASE_URL, "OPENAI_API_KEY": ENV_VAL, ENV_VAR: ENV_VAL},
    },
}


def _cli_targets() -> dict[str, dict]:
    """Ship defaults, let the user override them without a release."""
    targets = {k: dict(v) for k, v in _DEFAULT_CLI_TARGETS.items()}
    try:
        p = config.config_dir() / "cli_targets.json"
        if p.exists():
            user = json.loads(p.read_text())
            if isinstance(user, dict):
                for name, spec in user.items():
                    if isinstance(spec, dict):
                        targets[str(name)] = {**targets.get(str(name), {}), **spec}
    except (json.JSONDecodeError, OSError):
        pass
    return targets


def _run_cmd(argv: list[str]) -> int:
    """``openvidia run <cli> [args...]`` — point one process at the proxy.

    Nothing is written. The variables live in this process's child and die
    with it, so a CLI configured against the proxy today is unmodified
    tomorrow, and the user's own config keeps whatever they put in it.
    """
    targets = _cli_targets()
    if not argv:
        print("Usage: openvidia run <cli> [args...]")
        print(f"Known: {', '.join(sorted(targets))}")
        print("Anything else is executed as-is with the OpenAI-compatible vars set.")
        return 2

    name, rest = argv[0], argv[1:]
    spec = targets.get(name)
    if spec is None:
        # Not in the table: still useful — most tools read OPENAI_BASE_URL.
        spec = {
            "argv": [name],
            "env": {"OPENAI_BASE_URL": BASE_URL, "OPENAI_API_KEY": ENV_VAL, ENV_VAR: ENV_VAL},
        }

    env = {**os.environ, **{str(k): str(v) for k, v in (spec.get("env") or {}).items()}}
    cmd = list(spec.get("argv") or [name]) + rest

    exe = shutil.which(cmd[0])
    if exe is None:
        print(f"✗ {cmd[0]} is not on PATH")
        return 127

    if not _proxy_is_up(PORT):
        print(f"⚠ Nothing is serving {ROOT_URL} — start it with `openvidia` first.")

    for k in sorted(spec.get("env") or {}):
        print(f"  {k}={spec['env'][k]}")
    if spec.get("note"):
        print(f"\nℹ {spec['note']}\n")
    print(f"→ exec {' '.join(cmd)}\n", flush=True)

    try:
        os.execvpe(exe, cmd, env)
    except OSError as e:  # pragma: no cover — execvpe only returns on failure
        print(f"✗ could not exec {cmd[0]}: {e}")
        return 126


def _proxy_is_up(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _setup_cmd():
    """``openvidia setup`` — configure opencode, and only opencode.

    opencode is the one CLI this project treats as first-class, so it is the
    one place a persistent config edit is worth making. Everything else runs
    through `openvidia run`, which writes nothing.
    """
    print("OpenVidia setup")
    print()
    _setup_proxy_config()
    print()
    _setup_opencode()
    print()
    print(f"Proxy:      {BASE_URL}")
    print(f"Dashboard:  {ROOT_URL}")
    print()
    print("Other CLIs need no setup — run them against the proxy on demand:")
    for name in sorted(n for n in _cli_targets() if n != "opencode"):
        print(f"  openvidia run {name}")
    sys.exit(0)


async def main_async():
    """Foreground entrypoint: start the proxy on the event loop."""
    if not _kill_stale_port(PORT):
        # Starting anyway would leave the OLD process serving every request
        # while this one silently fails to bind — the hardest class of bug to
        # diagnose, because the code on disk is not the code answering.
        print(f"  Free it manually:  fuser -k {PORT}/tcp", flush=True)
        sys.exit(1)
    # Deliberately no _setup_*() here. Starting a proxy is not consent to
    # rewrite four other tools' configuration files.
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


USAGE = f"""OpenVidia — multi-key proxy for NVIDIA NIM

  openvidia                    start the proxy and open the dashboard
  openvidia foreground         start the proxy in this terminal (logs to stdout)
  openvidia setup              configure opencode to use the proxy (backs up first)
  openvidia run <cli> [args]   run another CLI against the proxy, changing no files
  openvidia --help             this message

Dashboard  {ROOT_URL}
API base   {BASE_URL}
Keys       {{keys_path}}

`run` sets the CLI's base-URL variables for that one process only. Override the
variables per CLI in {{targets_path}}."""


def _print_usage() -> None:
    print(
        USAGE.format(
            keys_path=config.config_path(),
            targets_path=config.config_dir() / "cli_targets.json",
        )
    )


def main():
    """CLI entrypoint: dispatch on argv[1]."""
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd in ("-h", "--help", "help"):
            _print_usage()
            return
        if cmd == "setup":
            _setup_cmd()
            return
        if cmd == "run":
            sys.exit(_run_cmd(sys.argv[2:]) or 0)
        if cmd == "foreground":
            asyncio.run(main_async())
            return
        # Unknown arguments used to be ignored silently, which made a typo
        # look like a successful launch.
        print(f"✗ unknown command: {cmd}\n")
        _print_usage()
        sys.exit(2)

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
