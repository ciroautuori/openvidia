import json
import os
from pathlib import Path
from typing import List


def config_dir() -> Path:
    d = Path.home() / ".config" / "openvidia"
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
