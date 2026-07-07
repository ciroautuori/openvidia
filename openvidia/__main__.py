"""
OpenVidia — minimal multi-key NVIDIA API proxy with web UI.

Install:
    pip install -e .

Usage:
    openvidia              # start proxy
    openvidia setup        # configure opencode provider

Edit keys via web UI at http://localhost:3940
Or edit ~/.config/openvidia/keys.json and restart.
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

PORT = 3940


def _kill_stale_port(port: int):
    try:
        out = subprocess.check_output(
            ["fuser", str(port) + "/tcp"], stderr=subprocess.DEVNULL, timeout=3
        )
        for pid in out.decode().strip().split():
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (OSError, ValueError):
                pass
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass


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

    providers = cfg.setdefault("provider", {})

    # Remove orphan nvidia provider if it points to localhost
    nv = providers.get("nvidia", {})
    if isinstance(nv, dict) and nv.get("options", {}).get("baseURL", "").startswith("http://localhost"):
        del providers["nvidia"]

    if "openvidia" not in providers:
        providers["openvidia"] = {
            "models": {"openvidia": {"name": "OpenVidia", "tools": True}},
            "npm": "@ai-sdk/openai-compatible",
            "options": {
                "apiKey": "ignored",
                "baseURL": f"http://localhost:{PORT}/v1",
            },
        }
        tmp = oc_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2))
        tmp.rename(oc_path)
        print(f"✓ Added OpenVidia provider to opencode")
    else:
        ov = providers["openvidia"]
        m = ov.setdefault("models", {})
        if "openvidia" not in m:
            m["openvidia"] = {"name": "OpenVidia", "tools": True}
            tmp = oc_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(cfg, indent=2))
            tmp.rename(oc_path)
            print(f"✓ Added OpenVidia model to opencode provider")

    print(f"✓ OpenVidia provider ready → http://localhost:{PORT}/v1")
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

    def log(msg: str):
        print(msg)

    web_dir = Path(__file__).resolve().parent.parent / "web"
    srv = await start(PORT, keys, log, stats, config.index_path(), web_dir=web_dir)
    print(f"● OpenVidia running on :{PORT} ({len(keys)} keys)")
    from .webui import auto_open
    auto_open(PORT)

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await srv.shutdown()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        _setup_cmd()
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
