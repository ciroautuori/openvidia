"""
Thread-safe shared state for the running proxy.

Single source of truth for keys, cooldowns, RPM tracking and usage stats.
``ProxyState`` is accessed concurrently by the asyncio event loop and by OS
threads spawned by ``account_manager`` (key regeneration), so a plain
``asyncio.Lock`` is not enough. Critical sections that touch the key list are
guarded by a real ``threading.Lock``; everything else relies on the async lock
and is safe within a single-threaded event loop.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from .config import atomic_write


# ── Cooldown / RPM constants ──────────────────────────────────────────

MAX_RPM = 28  # safe margin under NVIDIA's 40 RPM limit
RPM_WINDOW = 60.0  # sliding window in seconds

# Per-status cooldown durations (seconds).
# 400/404 are deterministic errors, not key faults — short cooldown, no rotation.
# 401/403 mean the key is dead — long cooldown, permanent invalidation.
# 429 respects Retry-After when provided.
# Adaptive: cooldowns scale with consecutive failures for repeated offenders.
COOLDOWN_DURATIONS: Dict[int, float] = {
    400: 60.0,   # Reduced from 120s - faster recovery for transient client errors
    401: 3600.0,
    403: 3600.0,
    404: 60.0,   # Reduced from 120s - endpoint issues may resolve quickly
    429: 120.0,  # Reduced from 180s - adaptive backoff handles repeat offenders
}
DEFAULT_COOLDOWN = 30.0

# Adaptive cooldown multiplier: increases cooldown for repeated failures
ADAPTIVE_COOLDOWN_MULTIPLIER = 1.5  # Each consecutive failure multiplies cooldown by this
ADAPTIVE_COOLDOWN_MAX = 5.0         # Cap multiplier at this value (max 5x base cooldown)

# Adaptive RPM: per-key ceiling is halved on a 429 (jittered backoff) and
# restored to MAX_RPM on the next success. This prevents the post-cooldown
# spike that re-triggers 429 the instant a key is revived.
ADAPTIVE_429_FACTOR = 0.5  # multiply per-key ceiling by this on each 429
ADAPTIVE_FLOOR_RPM = 8     # never go below this per-key (keeps SOME flow)
ADAPTIVE_REHAB_STEP = 4    # per-key ceiling growth on each success (until MAX_RPM)


# ── Per-key state ──────────────────────────────────────────────────────


class KeyState:
    """Per-key validity + cooldown + weighted-load tracking.

    Weighted-load tracking lets ``get_candidate_keys`` prefer the least busy
    key (lowest in-flight + lowest recent RPM) instead of naive round-robin,
    spreading burst traffic across the whole pool so no single key hits its
    RPM ceiling while others idle.
    """

    __slots__ = (
        "key",
        "is_valid",
        "cooldown_until",
        "last_error",
        "in_flight",
        "last_success_at",
        "last_failure_at",
        "consecutive_failures",
    )

    def __init__(self, key: str):
        self.key = key
        self.is_valid = True
        self.cooldown_until = 0.0
        self.last_error = ""
        self.in_flight = 0
        self.last_success_at = 0.0
        self.last_failure_at = 0.0
        self.consecutive_failures = 0


@dataclass
class KeyCooldown:
    """Active cooldown for a key with remaining-time helpers."""

    until: float = 0.0
    reason: str = ""

    @property
    def remaining(self) -> float:
        r = self.until - time.time()
        return r if r > 0 else 0.0

    @property
    def active(self) -> bool:
        return self.remaining > 0


# ── RPM tracker ────────────────────────────────────────────────────────


class RpmTracker:
    """Sliding-window requests-per-minute counter for a single key.

    Per-key adaptive ceiling: when NVIDIA's 429 response or Retry-After hint
    a lower effective RPM than MAX_RPM, the key lowers its own ceiling so the
    scheduler throttles it before a 429 actually occurs instead of after.
    """

    __slots__ = ("timestamps", "window", "max_rpm")

    def __init__(self, window: float = RPM_WINDOW, max_rpm: int = 0):
        self.timestamps: deque[float] = deque()
        self.window = window
        # 0 = inherit global MAX_RPM (legacy default).
        self.max_rpm = max_rpm

    def record(self) -> None:
        now = time.time()
        self.timestamps.append(now)
        self._prune(now)

    def count(self) -> int:
        self._prune()
        return len(self.timestamps)

    def can_send(self, max_rpm: int = MAX_RPM) -> bool:
        ceiling = self.max_rpm if self.max_rpm and self.max_rpm < max_rpm else max_rpm
        return self.count() < ceiling

    def _prune(self, now: Optional[float] = None) -> None:
        if now is None:
            now = time.time()
        cutoff = now - self.window
        while self.timestamps and self.timestamps[0] < cutoff:
            self.timestamps.popleft()


# ── Usage stats ────────────────────────────────────────────────────────


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


# ── ProxyState ────────────────────────────────────────────────────────


class ProxyState:
    def __init__(
        self,
        keys: List[str],
        stats: ProxyStats,
        index_path: Path,
        log_cb: Callable[[str], None],
        port: int = 3940,
    ):
        self._keys: List[str] = list(keys)
        self._key_states: Dict[str, KeyState] = {k: KeyState(k) for k in keys}
        self._keys_write_lock = threading.Lock()
        self.stats = stats
        self.index_path = index_path
        self.port = port
        self.lock = asyncio.Lock()
        self.save_lock = asyncio.Lock()
        self.log_buffer: deque = deque(maxlen=500)
        self._log_cb = log_cb
        self.on_key_failed: Optional[Callable[[str], None]] = None
        self.active_model: Optional[str] = None
        self.running: bool = True
        self.health_task: Optional[asyncio.Task] = None
        self.warm_task: Optional[asyncio.Task] = None

        self.cooldowns: Dict[str, KeyCooldown] = {}
        self.rpm: Dict[str, RpmTracker] = {}

        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.get_event_loop()
        self.listeners: Set[asyncio.Queue] = set()

    @property
    def keys(self) -> List[str]:
        return self._keys

    @keys.setter
    def keys(self, new_keys: List[str]) -> None:
        with self._keys_write_lock:
            updated_states = {}
            for k in new_keys:
                if k in self._key_states:
                    updated_states[k] = self._key_states[k]
                else:
                    updated_states[k] = KeyState(k)
            self._keys = list(new_keys)
            self._key_states = updated_states

    @property
    def key_states(self) -> Dict[str, KeyState]:
        with self._keys_write_lock:
            return dict(self._key_states)

    # ── Logging / SSE push ──────────────────────────────────────────

    def log_cb(self, msg: str) -> None:
        self._log_cb(msg)
        self.log_buffer.append(msg)
        if self.loop and self.loop.is_running():
            for q in list(self.listeners):
                self.loop.call_soon_threadsafe(q.put_nowait, msg)

    # ── Cooldown API ─────────────────────────────────────────────────

    def is_key_on_cooldown(self, key: str) -> bool:
        cd = self.cooldowns.get(key)
        return cd is not None and cd.active

    def cooldown_remaining(self, key: str) -> float:
        cd = self.cooldowns.get(key)
        return cd.remaining if cd is not None else 0.0

    def cooldown_reason(self, key: str) -> str:
        cd = self.cooldowns.get(key)
        return cd.reason if cd is not None else ""

    def set_cooldown(
        self, key: str, reason: str = "", duration: float = DEFAULT_COOLDOWN
    ) -> None:
        self.cooldowns[key] = KeyCooldown(until=time.time() + duration, reason=reason)

    def clear_cooldown(self, key: str) -> None:
        self.cooldowns.pop(key, None)

    def mark_key_failed(
        self, key: str, status: int = 0, retry_after: Optional[str] = None, 
                        error_body: Optional[str] = None
    ) -> None:
        """Record a failed attempt, set cooldown, and (on 429) tighten RPM.

        Adaptive RPM: NVIDIA's 429 / Retry-After is a signal about *future*
        throughput, not just the past. If we keep the ceiling at MAX_RPM we
        will re-429 the instant the cooldown expires. We step the per-key
        ceiling down and slowly restore it on the next success — exactly how
        a well-behaved client backs off without surrendering throughput
        forever.
        
        Adaptive Cooldown: Consecutive failures increase cooldown duration
        using exponential backoff with a cap, preventing hammering of failing
        endpoints while allowing faster recovery for transient errors.
        
        Detailed Error Logging: HTTP status codes and error bodies are logged
        for debugging 400/404/500 errors.
        """
        ks = self._key_states.get(key)
        if ks is not None:
            ks.last_failure_at = time.time()
            ks.consecutive_failures = (ks.consecutive_failures or 0) + 1

        # Build detailed error message for logging
        error_details = f"HTTP {status}" if status else "connection error"
        if error_body:
            error_details += f" (body: {error_body[:100]})"  # Truncate long bodies

        if status == 429:
            if retry_after:
                try:
                    base_duration = float(retry_after)
                except (ValueError, TypeError):
                    base_duration = COOLDOWN_DURATIONS[429]
            else:
                # Jittered backoff: avoids thundering herd when several keys
                # 429 simultaneously and then all wake up in unison. Base
                # 120s plus up to 30s jitter, seeded by key so the distribution
                # is stable per-key across runs.
                import random

                _r = random.Random(
                    int(time.time()) ^ (hash(key) & 0xFFFFFFFF)
                )
                base_duration = COOLDOWN_DURATIONS[429] + _r.uniform(0.0, 30.0)
            
            # Apply adaptive multiplier based on consecutive failures
            multiplier = min(ADAPTIVE_COOLDOWN_MULTIPLIER ** (ks.consecutive_failures - 1), 
                           ADAPTIVE_COOLDOWN_MAX)
            duration = base_duration * multiplier
            
            reason = f"429 rate-limited (cooldown {duration:.0f}s, attempt {ks.consecutive_failures})"
            if error_body:
                reason += f" - {error_body[:50]}"
            
            # Log detailed 429 info
            self.log_cb(f"⚠ key[{self._keys.index(key) if key in self._keys else '?'}] {reason}")
            
            # Tighten the per-key RPM ceiling: if current was MAX_RPM, drop
            # it by ADAPTIVE_429_FACTOR (default 0.5) but never below
            # ADAPTIVE_FLOOR_RPM. This prevents the post-cooldown spike that
            # would otherwise re-trigger 429 immediately.
            if ks is not None:
                tracker = self.rpm.setdefault(key, RpmTracker())
                ceil = tracker.max_rpm or MAX_RPM
                tracker.max_rpm = max(
                    ADAPTIVE_FLOOR_RPM,
                    int(ceil * ADAPTIVE_429_FACTOR),
                )
        elif status in COOLDOWN_DURATIONS:
            base_duration = COOLDOWN_DURATIONS[status]
            # Apply adaptive multiplier for repeated failures (except auth errors)
            if status not in (401, 403) and ks is not None and ks.consecutive_failures > 1:
                multiplier = min(ADAPTIVE_COOLDOWN_MULTIPLIER ** (ks.consecutive_failures - 1), 
                               ADAPTIVE_COOLDOWN_MAX)
                duration = base_duration * multiplier
                reason = f"{error_details} (cooldown {duration:.0f}s, attempt {ks.consecutive_failures})"
            else:
                duration = base_duration
                reason = f"{error_details} (cooldown {duration:.0f}s)"
        else:
            duration = DEFAULT_COOLDOWN
            reason = error_details

        self.set_cooldown(key, reason=reason, duration=duration)

        # Log detailed error information for debugging
        if status in (400, 404, 500, 502, 503):
            log_prefix = "⧉ error" if status >= 500 else "⚠ error"
            key_idx = self._keys.index(key) if key in self._keys else "?"
            self.log_cb(f"{log_prefix}: key[{key_idx}] {reason}")
            if error_body:
                self.log_cb(f"  └─ Response body: {error_body[:200]}")

        if status in (401, 403):
            if ks is not None:
                ks.is_valid = False
                ks.last_error = error_details
            self.log_cb(f"⚠ key marked INVALID ({error_details})")
        elif status in (400, 404, 429):
            if ks is not None:
                ks.cooldown_until = time.time() + duration
                ks.last_error = reason
        else:
            if ks is not None:
                ks.cooldown_until = time.time() + duration
                ks.last_error = reason

        if self.on_key_failed is not None:
            self.on_key_failed(key)

    def restore_key(self, key: str) -> None:
        self.clear_cooldown(key)
        ks = self._key_states.get(key)
        if ks:
            ks.cooldown_until = 0.0
            ks.is_valid = True
            ks.consecutive_failures = 0
            ks.last_success_at = time.time()
        # Graceful RPM rehab: bump the per-key ceiling back up by
        # ADAPTIVE_REHAB_STEP (default +4) capped at MAX_RPM. A key that was
        # stepped down on 429 earns throughput back one successful window at
        # a time, instead of snapping straight to MAX_RPM (which would risk
        # re-429). Snap straight to MAX_RPM only if already close.
        tracker = self.rpm.get(key)
        if tracker is None:
            return
        if tracker.max_rpm and tracker.max_rpm < MAX_RPM:
            new_ceiling = min(MAX_RPM, tracker.max_rpm + ADAPTIVE_REHAB_STEP)
            tracker.max_rpm = new_ceiling
            if new_ceiling >= MAX_RPM:
                tracker.max_rpm = 0  # fully rehabbed → inherit global ceiling

    # ── RPM API ─────────────────────────────────────────────────────

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

    def begin_in_flight(self, key: str) -> None:
        """Mark a key as serving a request; weighted by get_candidate_keys.

        Decrement happens in the caller's finally block via end_in_flight.
        Negative counts are clamped defensively in case of double-finally bugs.
        """
        ks = self._key_states.get(key)
        if ks is not None:
            ks.in_flight = max(0, ks.in_flight + 1)

    def end_in_flight(self, key: str) -> None:
        ks = self._key_states.get(key)
        if ks is not None:
            ks.in_flight = max(0, ks.in_flight - 1)

    # ── Composite helpers ───────────────────────────────────────────

    def is_key_healthy(self, key: str) -> bool:
        ks = self._key_states.get(key)
        if ks and not ks.is_valid:
            return False
        return not self.is_key_on_cooldown(key)

    def best_key_index(self) -> int:
        """Index of the least-loaded healthy, RPM-eligible key.

        Cost = in_flight_count + recent_rpm + small random tiebreaker. This
        keeps bursts spreading across the pool (max-min fairness) instead of
        slamming key[current_index] while the rest idle. When N concurrent
        requests hit us simultaneously, they each grab a different key instead
        of all queuing on the same one.
        """
        best_idx = -1
        best_cost = float("inf")
        for key in self._keys:
            ks = self._key_states.get(key)
            if not ks or not ks.is_valid:
                continue
            if self.is_key_on_cooldown(key):
                continue
            if not self.key_can_send_rpm(key):
                continue
            cost = (
                (ks.in_flight * 4)   # in-flight dominates: drains fast on completion
                + self.key_rpm(key)  # then recent rpm
                + ks.consecutive_failures * 8  # deprioritize flaky keys
            )
            if cost < best_cost:
                best_cost = cost
                best_idx = self._keys.index(key)
        return best_idx

    def get_candidate_keys(self) -> List[tuple[int, str]]:
        """
        Return ``(index, key)`` candidates for the current request, ordered by
        **least-loaded-first** (in-flight + recent RPM) instead of naive
        round-robin. Round-robin forced concurrent bursts onto the same
        ``current_index``; with the weighted cost below, N simultaneous
        requests each take a different key (max-min fairness across the pool).

        The first entry is the best key; the rest is the healthy pool sorted
        by cost, with cooldown keys appended last as a degraded fallback.
        """
        scored: list[tuple[float, int, str]] = []
        cooldown: List[tuple[int, str, float]] = []

        for idx, key in enumerate(self._keys):
            ks = self._key_states.get(key)
            if not ks or not ks.is_valid:
                continue
            if self.is_key_on_cooldown(key):
                cooldown.append((idx, key, self.cooldown_remaining(key)))
                continue
            cost = (
                (ks.in_flight * 4)
                + self.key_rpm(key)
                + ks.consecutive_failures * 8
            )
            scored.append((cost, idx, key))

        scored.sort(key=lambda x: (x[0], x[1]))
        available = [(idx, key) for _, idx, key in scored]

        if not available and cooldown:
            cooldown.sort(key=lambda x: x[2])
            available = [(idx, key) for idx, key, _ in cooldown]
            self.log_cb("⚠ No active keys outside cooldown, reusing cooldown keys")

        if not available:
            return []

        next_candidate_idx = available[0][0]
        self.stats.current_index = (next_candidate_idx + 1) % len(self._keys)
        self.stats.active_key_index = next_candidate_idx

        return available

    def clear_cooldown_and_restore(self, key: str) -> None:
        self.restore_key(key)

    # ── Stats for UI ────────────────────────────────────────────────

    def count_live_candidates(self) -> tuple[int, int]:
        """Return ``(live_rpm_eligible, total_valid)`` for saturation gating.

        ``live_rpm_eligible`` counts keys that are NOT on cooldown AND have
        RPM headroom right now (the realistic set a rotation loop could
        actually succeed on). ``total_valid`` counts keys not permanently
        invalidated. Callers use this to fast-fail rotation when the pool is
        saturated instead of serially hammering all ``len(keys)`` candidates
        with a 120s timeout each (the historical Codex CLI block).
        """
        live = 0
        valid = 0
        for key in self._keys:
            ks = self._key_states.get(key)
            if not ks or not ks.is_valid:
                continue
            valid += 1
            if self.is_key_on_cooldown(key):
                continue
            if not self.key_can_send_rpm(key):
                continue
            live += 1
        return live, valid

    def key_cooldown_info(self, key: str) -> tuple[float, str]:
        """Return ``(remaining_seconds, reason)`` for the dashboard."""
        if self.is_key_on_cooldown(key):
            return self.cooldown_remaining(key), self.cooldown_reason(key)
        return 0.0, ""


# ── Async index persistence ───────────────────────────────────────────


async def _async_write_index(path: Path, i: int, lock: asyncio.Lock) -> None:
    async with lock:
        try:
            await asyncio.to_thread(atomic_write, path, str(i))
        except OSError:
            pass


def persist_index(state: ProxyState, i: int) -> None:
    previous = state.stats.current_index
    state.stats.current_index = i
    state.stats.active_key_index = i
    if previous != i:
        asyncio.create_task(_async_write_index(state.index_path, i, state.save_lock))
