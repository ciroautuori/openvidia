"""Cross-platform config paths and atomic file helpers."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import List


def config_dir() -> Path:
    """Platform-specific config directory."""
    if sys.platform == "win32":
        d = Path(os.environ.get("APPDATA", Path.home())) / "openvidia"
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "openvidia"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", "")
        d = Path(xdg) / "openvidia" if xdg else Path.home() / ".config" / "openvidia"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return config_dir() / "keys.json"


def accounts_path() -> Path:
    return config_dir() / "accounts.json"


def index_path() -> Path:
    return config_dir() / "index"


def lock_path() -> Path:
    return config_dir() / "singleton.lock"


def load_saved_keys_file() -> List[str]:
    p = config_path()
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def atomic_write(path: Path, content: str) -> None:
    """Write to a temp file then rename — crash-safe, atomic on POSIX."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content)
    tmp.rename(path)


def save_keys_file(keys: List[str]) -> None:
    atomic_write(config_path(), json.dumps(keys, indent=2))


def load_saved_index() -> int:
    p = index_path()
    try:
        return int(p.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return 0


def presets_path() -> Path:
    return config_dir() / "presets.json"


def load_saved_presets() -> list:
    p = presets_path()
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def save_presets_file(presets: list) -> None:
    atomic_write(presets_path(), json.dumps(presets, indent=2))


def stop_flag_path() -> Path:
    return config_dir() / "stop"


def save_stop_flag() -> None:
    atomic_write(stop_flag_path(), "1")


def check_stop_flag() -> bool:
    p = stop_flag_path()
    if p.exists():
        try:
            return p.read_text().strip() == "1"
        except OSError:
            return False
    return False


def clear_stop_flag() -> None:
    p = stop_flag_path()
    if p.exists():
        p.unlink()


def active_model_path() -> Path:
    return config_dir() / "active_model"


def save_active_model(model: str) -> None:
    atomic_write(active_model_path(), model)


def load_active_model() -> str:
    p = active_model_path()
    try:
        return p.read_text().strip()
    except (FileNotFoundError, OSError):
        return ""


def opencode_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg:
        return Path(xdg) / "opencode" / "opencode.json"
    return Path.home() / ".config" / "opencode" / "opencode.json"
