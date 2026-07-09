"""
Shared state for the running proxy.

Cooldown + sliding-window RPM per key.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .config import atomic_write


# ── Cooldown / RPM constants ──────────────────────────────────────────

MAX_RPM = 28                     # safe margin under NVIDIA's 40 RPM limit
RPM_WINDOW = 60.0                # sliding window in seconds

# Default cooldown durations by HTTP status
COOLDOWN_DURATIONS: Dict[int, float] = {
    400: 120.0,   # bad request — maybe model access issue
    401: 3600.0,  # unauthorized — dead key
    403: 3600.0,  # forbidden — dead key
    404: 120.0,   # not found — model not on this key
    429: 60.0,    # rate limited (Retry-After overrides this)
}
DEFAULT_COOLDOWN = 30.0


# ── Models ─────────────────────────────────────────────────────────────

@dataclass
class KeyCooldown:
    until: float = 0.0
    reason: str = ""

    @property
    def remaining(self) -> float:
        r = self.until - time.time()
        return r if r > 0 else 0.0

    @property
    def active(self) -> bool:
        return self.remaining > 0


class RpmTracker:
    """Sliding-window requests-per-minute counter per key."""

    __slots__ = ("timestamps", "window")

    def __init__(self, window: float = RPM_WINDOW):
        self.timestamps: deque[float] = deque()
        self.window = window

    def record(self) -> None:
        now = time.time()
        self.timestamps.append(now)
        self._prune(now)

    def count(self) -> int:
        self._prune()
        return len(self.timestamps)

    def can_send(self, max_rpm: int = MAX_RPM) -> bool:
        return self.count() < max_rpm

    def _prune(self, now: Optional[float] = None) -> None:
        if now is None:
            now = time.time()
        cutoff = now - self.window
        while self.timestamps and self.timestamps[0] < cutoff:
            self.timestamps.popleft()


class KeyUsage:
    __slots__ = ("requests", "success", "failed", "last_used", "last_error")

    def __init__(self):
        self.requests = 0
        self.success = 0
        self.failed = 0
        self.last_used = 0.0
        self.last_error = ""


class ProxyStats:
    def __init__(self, current_index: int = 0):
        self.requests = 0
        self.rotations = 0
        self.success = 0
        self.current_index = current_index
        self.active_key_index: int = current_index
        self.key_usage: Dict[str, KeyUsage] = {}

    def record_key_usage(self, key: str, ok: bool = True, error: str = "") -> None:
        u = self.key_usage.get(key)
        if u is None:
            u = KeyUsage()
            self.key_usage[key] = u
        u.requests += 1
        u.last_used = time.time()
        if ok:
            u.success += 1
        else:
            u.failed += 1
            u.last_error = error


class ProxyState:
    def __init__(
        self,
        keys: List[str],
        stats: ProxyStats,
        index_path: Path,
        log_cb: Callable[[str], None],
        port: int = 3940,
    ):
        self.keys: List[str] = list(keys)
        self.stats = stats
        self.index_path = index_path
        self.port = port
        self.lock = asyncio.Lock()
        self.log_buffer: deque = deque(maxlen=500)
        self._log_cb = log_cb
        self.active_model: Optional[str] = None
        self.running: bool = True
        self.health_task: Optional[asyncio.Task] = None

        # Cooldown / RPM per key (replaces old unhealthy_keys set)
        self.cooldowns: Dict[str, KeyCooldown] = {}
        self.rpm: Dict[str, RpmTracker] = {}

    # ── Logging ───────────────────────────────────────────────────────

    def log_cb(self, msg: str) -> None:
        self._log_cb(msg)
        self.log_buffer.append(msg)

    # ── Cooldown API ───────────────────────────────────────────────────

    def is_key_on_cooldown(self, key: str) -> bool:
        cd = self.cooldowns.get(key)
        return cd is not None and cd.active

    def cooldown_remaining(self, key: str) -> float:
        cd = self.cooldowns.get(key)
        return cd.remaining if cd is not None else 0.0

    def cooldown_reason(self, key: str) -> str:
        cd = self.cooldowns.get(key)
        return cd.reason if cd is not None else ""

    def set_cooldown(self, key: str, reason: str = "", duration: float = DEFAULT_COOLDOWN) -> None:
        self.cooldowns[key] = KeyCooldown(until=time.time() + duration, reason=reason)

    def clear_cooldown(self, key: str) -> None:
        self.cooldowns.pop(key, None)

    def mark_key_failed(self, key: str, status: int = 0, retry_after: Optional[str] = None) -> None:
        """Record a failed attempt and set cooldown appropriate to the error type."""
        if status == 429:
            if retry_after:
                try:
                    duration = float(retry_after)
                except (ValueError, TypeError):
                    duration = COOLDOWN_DURATIONS[429]
            else:
                duration = COOLDOWN_DURATIONS[429]
            reason = f"429 rate-limited (cooldown {duration:.0f}s)"
        elif status in COOLDOWN_DURATIONS:
            duration = COOLDOWN_DURATIONS[status]
            reason = f"HTTP {status} (cooldown {duration:.0f}s)"
        else:
            duration = DEFAULT_COOLDOWN
            reason = f"HTTP {status}" if status else "connection error"

        self.set_cooldown(key, reason=reason, duration=duration)

    # ── RPM API ────────────────────────────────────────────────────────

    def record_request(self, key: str) -> None:
        t = self.rpm.get(key)
        if t is None:
            t = RpmTracker()
            self.rpm[key] = t
        t.record()

    def key_rpm(self, key: str) -> int:
        t = self.rpm.get(key)
        return t.count() if t is not None else 0

    def key_can_send_rpm(self, key: str) -> bool:
        t = self.rpm.get(key)
        return t is None or t.can_send()

    # ── Composite helpers (used by proxy loop) ────────────────────────

    def is_key_healthy(self, key: str) -> bool:
        """A key is 'healthy' when not on cooldown (RPM is checked separately)."""
        return not self.is_key_on_cooldown(key)

    def mark_key_healthy(self, key: str, healthy: bool) -> None:
        """Legacy compat for health-check code — clears cooldown."""
        if healthy:
            self.clear_cooldown(key)
        else:
            self.mark_key_failed(key)  # mark with default cooldown


def persist_index(state: ProxyState, i: int) -> None:
    previous = state.stats.current_index
    state.stats.current_index = i
    state.stats.active_key_index = i
    if previous != i:
        try:
            atomic_write(state.index_path, str(i))
        except OSError:
            pass
