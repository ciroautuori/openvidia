"""Cross-platform config paths and atomic file helpers."""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path


def config_dir() -> Path:
    """Platform-specific config directory, private to the user.

    The directory holds API keys in cleartext, so it is chmod'd 0700 on every
    call — cheap, and it repairs a directory created by an older version that
    left it world-readable.
    """
    if sys.platform == "win32":
        d = Path(os.environ.get("APPDATA", Path.home())) / "openvidia"
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "openvidia"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", "")
        d = Path(xdg) / "openvidia" if xdg else Path.home() / ".config" / "openvidia"
    d.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        try:
            d.chmod(0o700)
        except OSError:
            pass
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


# ── Upstream endpoints (single source of truth) ────────────────────────
# Base URL per NVIDIA NIM; la variante /chat/completions è usata da
# responses_shim, anthropic_shim e compaction.
UPSTREAM_BASE = "https://integrate.api.nvidia.com/v1/"
UPSTREAM_CHAT = UPSTREAM_BASE + "chat/completions"


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
    # ── Fallback / failover control ───────────────────────────────────────
    # "auto" = failover when circuit is open (current behavior)
    # "off"  = never failover, return 503 if model circuit is open
    # "on"   = always failover to next healthy preset (future use)
    "fallback": "off",
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


def lock_path() -> Path:
    return config_dir() / "singleton.lock"


# Model ids are vendor/name slugs. Anything else is either a typo or an
# attempt to smuggle markup into state the dashboard later renders.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


def is_valid_model_id(model: str) -> bool:
    return bool(_MODEL_ID_RE.match(model))


def mask_key(key: str) -> str:
    """Redact a key for display. Enough prefix to tell keys apart, never enough to use."""
    if not isinstance(key, str):
        return "?"
    return key if len(key) <= 12 else f"{key[:5]}…{key[-4:]}"


def load_saved_keys_file() -> list[str]:
    """Load the key pool, rejecting anything that is not a list of non-empty strings.

    A hand-edited ``keys.json`` that holds a dict or a number used to flow
    straight into ``Bearer {k}`` and produce an unexplainable 401 loop, so the
    shape is checked here rather than at the point of use.
    """
    p = config_path()
    try:
        data = json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return [k for k in data if isinstance(k, str) and k.strip()]


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    """Write to a unique temp file, fsync, then replace — crash-safe and private.

    Three things the obvious version gets wrong. The temp file is created with
    an explicit mode, because the default would be 0644 and this function's
    main caller writes API keys. It is fsynced (file *and* directory) before
    the rename, since rename is atomic against readers but not against power
    loss. And the temp name is unique, so two processes — which happens during
    the ~1s restart overlap — cannot clobber each other's staging file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        # os.replace, not Path.rename: rename() raises if the destination
        # exists on Windows, which config_dir() explicitly supports.
        os.replace(tmp, path)
        # Directory fsync makes the rename itself durable. Not possible on
        # Windows (a directory cannot be opened), where it is also not needed.
        if sys.platform != "win32":
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_keys_file(keys: list[str], create_backup: bool = True) -> None:
    """Save the key pool 0600, backing up the previous file first.

    Returns nothing but raises on write failure: losing the key file silently
    is worse than a traceback, because the user has no way to notice until the
    next start.
    """
    clean = [k for k in keys if isinstance(k, str) and k.strip()]
    content = json.dumps(clean, indent=2)
    cfg_path = config_path()

    if create_backup and cfg_path.exists():
        try:
            from .safe_file import create_backup as make_backup

            make_backup(cfg_path)
        except OSError as exc:
            # A failed backup is survivable, an unexplained one is not: if the
            # disk is full the write below will fail too, and the user needs
            # both facts to understand what happened.
            print(f"⚠ keys.json backup failed: {exc}", flush=True)

    atomic_write(cfg_path, content)


def control_token_path() -> Path:
    return config_dir() / "control_token"


_control_token: str | None = None


def control_token() -> str:
    """The shared secret that authenticates the control plane.

    Checking Origin and Host is not authentication. Those headers are
    unforgeable *by a browser*, which is why they stop a malicious page — but
    any script can set them to whatever it likes, so anything that can open a
    TCP connection to the port could reach /api/* by sending
    ``Host: localhost:1919``. Loopback binding does not help either: a
    forwarder (``tailscale serve``, an SSH tunnel, a container port map)
    connects from 127.0.0.1 on the client's behalf, so the proxy cannot tell
    the traffic apart by source address.

    A secret in a 0600 file can: possession of it means the caller could read
    a file only this UID can read. It is generated once and reused, so the
    dashboard keeps working across restarts.
    """
    global _control_token
    if _control_token:
        return _control_token
    p = control_token_path()
    try:
        existing = p.read_text().strip()
        if len(existing) >= 32:
            _control_token = existing
            return _control_token
    except (FileNotFoundError, OSError):
        pass
    _control_token = secrets.token_urlsafe(32)
    atomic_write(p, _control_token + "\n")
    return _control_token


def harden_config_permissions() -> list[str]:
    """Tighten anything an older version left world-readable. Returns what changed.

    Versions before this one wrote keys.json and its backups 0644, so upgrading
    is not enough — the files already on disk stay readable by every account on
    the machine until something repairs them. This runs at startup.
    """
    if sys.platform == "win32":
        return []
    fixed: list[str] = []
    d = config_dir()
    for p in d.glob("keys*.json"):
        try:
            if p.stat().st_mode & 0o077:
                p.chmod(0o600)
                fixed.append(p.name)
        except OSError:
            continue
    return fixed


def presets_path() -> Path:
    return config_dir() / "presets.json"


def load_saved_presets() -> list:
    p = presets_path()
    try:
        data = json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return [m for m in data if isinstance(m, str) and is_valid_model_id(m)]


def save_presets_file(presets: list) -> None:
    atomic_write(presets_path(), json.dumps(presets, indent=2))


# The stop flag used to be persisted here (stop_flag_path / save_stop_flag /
# check_stop_flag / clear_stop_flag). Nothing ever read it: ProxyState.running
# starts True unconditionally. Wiring it up would have been worse than deleting
# it — a user who paused the proxy from the dashboard would find it silently
# refusing every request after a reboot, with no indication why. Stop is a
# runtime toggle and stays one.


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
