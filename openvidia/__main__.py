"""
OpenVidia — minimal multi-key NVIDIA API proxy with web UI.

Install:
    cd ~/Scrivania/envidia && uv pip install -e .

Usage:
    openvidia
    # or: uv run python3 -m openvidia

Edit keys via the web UI at http://localhost:3940
Or edit ~/.config/openvidia/keys.json and restart.
Keys are auto-extracted from accounts.json if keys.json is empty.
"""
import asyncio
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
    """Auto-extract keys from accounts.json if keys.json is empty."""
    try:
        import json
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


async def main_async():
    _kill_stale_port(PORT)
    keys = config.load_saved_keys_file()
    if not keys:
        keys = _extract_keys_from_accounts()
    if not keys:
        print("✗ No keys found. Add keys to ~/.config/openvidia/keys.json")
        print("  Or run: python -c 'import json; json.dump([\"nvapi-...\"], open(\"$HOME/.config/openvidia/keys.json\",\"w\"))'")
        print("  Accounts with keys in accounts.json are auto-extracted.")
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
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
