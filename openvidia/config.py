"""Cross-platform config paths and atomic file helpers."""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path


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


# ── Upstream timeouts ──────────────────────────────────────────────────
# `read` is the wait for the FIRST byte of a streamed answer, and a
# reasoning model emits nothing at all while it thinks. Measured on the
# NVIDIA free tier: z-ai/glm-5.2 takes ~117s to first byte on a 2k-token
# prompt and ~143s on a 20k one — latency driven by the model, not by the
# prompt size. The previous 30s ceiling therefore made every request to a
# slow model time out on every key in the pool, and the proxy blamed the
# keys for it. Single source of truth: all three request paths (chat
# completions, /v1/responses, /v1/messages) import this.
_TIMEOUT_DEFAULTS = {
    "connect": 5.0,
    "read": 180.0,
    "write": 30.0,
    "pool": 240.0,
}

_HTTPX_TIMEOUT_KEYS = ("connect", "read", "write", "pool")


def httpx_timeout_kwargs() -> dict[str, float]:
    """Return configured upstream timeouts as kwargs for `httpx.Timeout`."""
    out = dict(_TIMEOUT_DEFAULTS)
    try:
        p = config_dir() / "timeouts.json"
        if p.exists():
            user = json.loads(p.read_text())
            if isinstance(user, dict):
                for k in _HTTPX_TIMEOUT_KEYS:
                    if k in user and isinstance(user[k], int | float):
                        out[k] = float(user[k])
    except (json.JSONDecodeError, OSError):
        pass
    return out


def upstream_timeouts() -> dict:
    """All upstream timeout settings, overridable via ``timeouts.json``.

    Read once at import time — this is startup configuration, not a hot path.
    """
    try:
        p = config_dir() / "timeouts.json"
        if p.exists():
            loaded = json.loads(p.read_text())
            return {**_TIMEOUT_DEFAULTS, **{k: float(v) for k, v in loaded.items()}}
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        pass
    return dict(_TIMEOUT_DEFAULTS)


def outbound_proxy() -> str | None:
    """Return outbound HTTP/SOCKS5 proxy URL for upstream requests.

    Overridable via OPENVIDIA_OUTBOUND_PROXY or proxy_config.json.
    """
    env_proxy = (
        os.environ.get("OPENVIDIA_OUTBOUND_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("http_proxy")
    )
    if env_proxy:
        return env_proxy
    try:
        p = config_dir() / "proxy_config.json"
        if p.exists():
            data = json.loads(p.read_text())
            if isinstance(data, dict) and data.get("outbound_proxy"):
                return str(data["outbound_proxy"])
    except (json.JSONDecodeError, OSError):
        pass
    return None



# ── Thinking / reasoning toggle ────────────────────────────────────────
# Hybrid reasoning models emit nothing until they finish thinking, which is
# the difference between a 2s and a 160s first token. Providers expose the
# switch under different names and it changes with every model generation, so
# the PAYLOAD is configuration, not code: when the next model uses a
# different flag, edit model_options.json instead of shipping a release.
_MODEL_OPTIONS_DEFAULTS = {
    "thinking": "auto",  # "auto" (send nothing) | "on" | "off"
    # NVIDIA NIM 2026: new models use enable_thinking, older use chat_template_kwargs.thinking
    "thinking_off_payload": {"chat_template_kwargs": {"enable_thinking": False}},
    "thinking_on_payload": {"chat_template_kwargs": {"enable_thinking": True}},
    # ── Reasoning effort: granularità oltre on/off ──
    # low   → thinking off + temperature alta (fast, zero reasoning)
    # medium → thinking on  + temperature media (balanced)
    # high  → thinking on  + temperature bassa (focused, deep reasoning)
    "reasoning_effort": "auto",  # "auto" | "low" | "medium" | "high"
    "effort_payloads": {
        "low": {"chat_template_kwargs": {"enable_thinking": False}, "temperature": 0.7},
        "medium": {"chat_template_kwargs": {"enable_thinking": True}, "temperature": 0.5},
        "high": {"chat_template_kwargs": {"enable_thinking": True}, "temperature": 0.2},
    },
    # ── Per-model hardcoded optimizations ─────────────────────────────────
    # These are defaults from NVIDIA docs — the dashboard can still override.
    # Key insight:
    #   DeepSeek V4 Pro: enable_thinking=False → TTFT 60s→3s for coding
    #   Nemotron Ultra:  enable_thinking=False mandatory for tool calling;
    #                    temperature=1.0, top_p=0.95 per NVIDIA best practice
    #   GLM 5.2:         thinking=False → stops the 180s block
    "per_model": {
        "deepseek-ai/deepseek-v4-pro": {
            "thinking": "off",
            # temperature 0.0 = deterministic, best for coding accuracy
            "extra_payload": {
                "chat_template_kwargs": {"enable_thinking": False},
                "temperature": 0.0,
            },
        },
        "nvidia/nemotron-3-ultra-550b-a55b": {
            "thinking": "off",
            # temperature=1.0, top_p=0.95: NVIDIA recommended for Nemotron reasoning modes
            # enable_thinking MUST be False for tool calling (otherwise hangs)
            "extra_payload": {
                "chat_template_kwargs": {"enable_thinking": False},
                "temperature": 1.0,
                "top_p": 0.95,
            },
        },
        "z-ai/glm-5.2": {
            "thinking": "off",
            "extra_payload": {
                "chat_template_kwargs": {"thinking": False},
            },
        },
        "poolside/laguna-xs-2.1": {
            "thinking": "off",
            "extra_payload": {
                "chat_template_kwargs": {"enable_thinking": False},
            },
        },
    },
}


def model_options_path() -> Path:
    return config_dir() / "model_options.json"


def model_options() -> dict:
    opts = copy.deepcopy(_MODEL_OPTIONS_DEFAULTS)
    try:
        p = model_options_path()
        if p.exists():
            saved = json.loads(p.read_text())
            if isinstance(saved, dict):
                _fill_missing(saved, opts)
                return saved
    except (json.JSONDecodeError, OSError):
        pass
    return opts


def save_model_options(opts: dict) -> None:
    atomic_write(model_options_path(), json.dumps(opts, indent=2))


def _fill_missing(dst: dict, src: dict) -> dict:
    """Recursively add keys from ``src`` that ``dst`` does not already have.

    Fill, never overwrite: the dashboard sets a default, but a CLI that spells
    the parameter out in its own request has made an explicit choice and must
    win at every level of nesting.
    """
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _fill_missing(dst[k], v)
        elif k not in dst:
            dst[k] = copy.deepcopy(v)
    return dst


def apply_model_options(payload: dict) -> dict:
    """Merge the configured thinking/reasoning payload into an outgoing chat request.

    Priority (high → low):
    1. per_model.extra_payload  — model-specific optimal params (enable_thinking, temperature…)
    2. reasoning_effort         — low/medium/high slider
    3. thinking on/off          — simple binary toggle
    Never overwrites something the client already set explicitly.
    """
    if not isinstance(payload, dict):
        return payload
    opts = model_options()
    model = payload.get("model") or ""
    per = (opts.get("per_model") or {}).get(model, {})

    # 1. Per-model extra_payload (model-specific optimal params like enable_thinking: False, temp, etc.)
    extra_payload = per.get("extra_payload")
    if isinstance(extra_payload, dict):
        _fill_missing(payload, extra_payload)

    # 2. Reasoning effort override (if specified for model or globally and not auto)
    effort = per.get("reasoning_effort") or opts.get("reasoning_effort", "auto")
    if effort != "auto":
        extra = (opts.get("effort_payloads") or {}).get(effort)
        if isinstance(extra, dict):
            _fill_missing(payload, extra)
            return payload

    # 3. Fallback: toggle thinking binario auto/on/off
    mode = per.get("thinking") or opts.get("thinking", "auto")
    if mode == "off":
        extra = opts.get("thinking_off_payload") or {}
        if isinstance(extra, dict):
            _fill_missing(payload, extra)
    elif mode == "on":
        extra = opts.get("thinking_on_payload") or {}
        if isinstance(extra, dict):
            _fill_missing(payload, extra)
    return payload


def config_path() -> Path:
    return config_dir() / "keys.json"


def accounts_path() -> Path:
    return config_dir() / "accounts.json"


def index_path() -> Path:
    return config_dir() / "index"


def lock_path() -> Path:
    return config_dir() / "singleton.lock"


def load_saved_keys_file() -> list[str]:
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


def save_keys_file(keys: list[str], create_backup: bool = True) -> None:
    """Save keys with optional automatic backup.

    Args:
        keys: List of API keys to save
        create_backup: Whether to create a backup before writing
    """

    content = json.dumps(keys, indent=2)
    cfg_path = config_path()

    if create_backup and cfg_path.exists():
        # Create backup before writing
        try:
            from .safe_file import create_backup as make_backup

            make_backup(cfg_path)
        except Exception:
            pass  # Backup is optional, continue with write

    atomic_write(cfg_path, content)


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
