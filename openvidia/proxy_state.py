"""
Shared state for the running proxy.

Single source of truth for keys, cooldowns, RPM tracking and usage stats.

Concurrency, accurately: everything here runs on the asyncio event loop. The
health check and the pre-warm are coroutines, not OS threads — the only
threading.Thread in the project is the restart helper in webui.py, and it does
not touch this object. ``self.lock`` (asyncio) serialises the compound
read-modify-write sequences that span an await; single statements are already
atomic under the loop.

The key list additionally takes a threading.Lock when replaced, because the
list is swapped wholesale while readers may be iterating it. That is the only
thing it protects, and it is not a claim that the rest of the class is
thread-safe: an earlier version of this docstring said it was, which is worse
than saying nothing, because the next person to add a thread would believe it.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

# ── Cooldown / RPM constants ──────────────────────────────────────────

MAX_RPM = 28  # safe margin under NVIDIA's 40 RPM limit
RPM_WINDOW = 60.0  # sliding window in seconds

# Per-status cooldown durations (seconds).
# 400/404 are deterministic errors, not key faults — short cooldown, no rotation.
# 401/403 mean the key is dead — long cooldown, permanent invalidation.
# 429 respects Retry-After when provided.
# Adaptive: cooldowns scale with consecutive failures for repeated offenders.
COOLDOWN_DURATIONS: dict[int, float] = {
    400: 60.0,  # Reduced from 120s - faster recovery for transient client errors
    401: 3600.0,
    403: 3600.0,
    404: 60.0,  # Reduced from 120s - endpoint issues may resolve quickly
    429: 45.0,  # Reduced to 45s for faster key recovery under high pool load
}
DEFAULT_COOLDOWN = 30.0

# Adaptive cooldown: repeated failures get up to ADAPTIVE_COOLDOWN_MAX
# times the base duration. The exponent is capped at MAX so a key that
# fails many times in a row is not locked out indefinitely.
ADAPTIVE_COOLDOWN_MAX = 1.5

# Adaptive RPM: per-key ceiling is halved on a 429 (jittered backoff) and
# restored to MAX_RPM on the next success.
ADAPTIVE_429_FACTOR = 0.5  # multiply per-key ceiling by this on each 429
ADAPTIVE_FLOOR_RPM = 14  # minimum floor per-key to maintain throughput
ADAPTIVE_REHAB_STEP = 10  # fast per-key ceiling growth (+10 RPM) on success

# ── Aggregate (account/IP) throttle detection ──────────────────────────
# NVIDIA documents the free tier as ~40 RPM bound to the API key. Observed
# behaviour says that is not the only limiter: several distinct keys can take
# a 429 inside the same second, and the rejection arrives in ~0.16s — far too
# fast to have reached a model. Independent per-key budgets do not expire in
# lockstep, so that signature means something upstream is counting across the
# whole pool (same account, same source address, or an anti-abuse heuristic).
#
# It matters because the pool's entire strategy — rotate to another key — is
# useless against an aggregate limit, and actively harmful: each rotation adds
# another cooled-down key, so one throttling event takes the whole pool out of
# service. When the signature appears, stop rotating and wait instead.
POOL_429_WINDOW = 10.0  # look-back for correlating 429s
POOL_429_DISTINCT_KEYS = 3  # distinct keys inside the window → aggregate limit
POOL_THROTTLE_PAUSE = 15.0  # how long to stop spending keys on it


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
    # The upstream status that caused it. A rate limit and a dead socket both
    # park a key, but only one of them can be disproved by a cheap probe.
    status: int = 0

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

    def _prune(self, now: float | None = None) -> None:
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


class ModelHealth:
    """Per-model health, measured from real traffic.

    Provider capacity for one model collapses without warning while the rest
    stay fast, and the proxy is the only component that sees it happen. Every
    request already carries the evidence — how long the first byte took, and
    what status came back — so nothing needs to be probed: we just keep score
    and say so out loud when a model stops being usable.
    """

    __slots__ = (
        "requests",
        "success",
        "gateway_timeouts",
        "rate_limited",
        "too_slow",
        "ttfts",
        "warned_at",
        "consecutive_failures",
        "circuit_opened_at",
    )

    # Circuit breaker: open after this many consecutive failures
    CIRCUIT_OPEN_AFTER = 3
    # Auto-reset after this many seconds (allow model to recover)
    CIRCUIT_RESET_AFTER = 120.0

    def __init__(self):
        self.requests = 0
        self.success = 0
        self.gateway_timeouts = 0  # 502/503/504: provider gave up on the model
        self.rate_limited = 0  # 429
        self.too_slow = 0  # no first byte before our own read timeout
        self.ttfts: deque[float] = deque(maxlen=20)
        self.warned_at = 0.0
        self.consecutive_failures = 0
        self.circuit_opened_at = 0.0

    @property
    def is_circuit_open(self) -> bool:
        """True when this model should be skipped (too many consecutive failures)."""
        if self.consecutive_failures < self.CIRCUIT_OPEN_AFTER:
            return False
        if time.time() - self.circuit_opened_at > self.CIRCUIT_RESET_AFTER:
            # Auto-reset: give model a chance to recover
            self.consecutive_failures = 0
            self.circuit_opened_at = 0.0
            return False
        return True

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures == self.CIRCUIT_OPEN_AFTER:
            self.circuit_opened_at = time.time()

    def record_success_reset(self) -> None:
        self.consecutive_failures = 0
        self.circuit_opened_at = 0.0

    @property
    def median_ttft(self) -> float:
        if not self.ttfts:
            return 0.0
        s = sorted(self.ttfts)
        return s[len(s) // 2]

    @property
    def failure_rate(self) -> float:
        if not self.requests:
            return 0.0
        return 1.0 - (self.success / self.requests)

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "success": self.success,
            "gateway_timeouts": self.gateway_timeouts,
            "rate_limited": self.rate_limited,
            "too_slow": self.too_slow,
            "median_ttft": round(self.median_ttft, 1),
            "failure_rate": round(self.failure_rate, 2),
            "circuit_open": self.is_circuit_open,
            "consecutive_failures": self.consecutive_failures,
        }


# A model is called out once its recent record is this bad. Small samples lie,
# so require a handful of attempts before saying anything.
_MODEL_WARN_MIN_REQUESTS = 4
_MODEL_WARN_FAILURE_RATE = 0.5
_MODEL_WARN_INTERVAL = 120.0  # seconds between repeats, per model


class ProxyStats:
    def __init__(self, current_index: int = 0):
        self.requests = 0
        self.rotations = 0
        self.success = 0
        self.current_index = current_index
        self.active_key_index: int = current_index
        self.key_usage: dict[str, KeyUsage] = {}

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
        keys: list[str],
        stats: ProxyStats,
        log_cb: Callable[[str], None],
        port: int = 1919,
    ):
        self._keys: list[str] = list(keys)
        self._key_states: dict[str, KeyState] = {k: KeyState(k) for k in keys}
        self._keys_write_lock = threading.Lock()
        self.stats = stats
        self.port = port
        self.lock = asyncio.Lock()
        self.log_buffer: deque = deque(maxlen=500)
        self._log_cb = log_cb

        self.active_model: str | None = None
        self.running: bool = True
        self.health_task: asyncio.Task | None = None

        self.cooldowns: dict[str, KeyCooldown] = {}
        self.rpm: dict[str, RpmTracker] = {}
        # (timestamp, key) for recent 429s, used to tell a per-key rate limit
        # apart from an account-wide one. See POOL_429_WINDOW.
        self._recent_429: deque[tuple[float, str]] = deque()
        self.pool_throttled_until: float = 0.0
        # Per-model health, learned from real traffic (see ModelHealth).
        self.model_health: dict[str, ModelHealth] = {}

        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            # Python 3.14 removed the implicit "create if missing"
            # behavior of get_event_loop; fall back to a new short-lived
            # loop so ProxyState can be constructed outside a running loop
            # (tests, ad-hoc scripts). In a FastAPI worker the running
            # loop branch above already handles the normal path.
            self.loop = asyncio.new_event_loop()
        self.listeners: set[asyncio.Queue] = set()

    @property
    def keys(self) -> list[str]:
        return self._keys

    @keys.setter
    def keys(self, new_keys: list[str]) -> None:
        """Replace the pool, carrying over state for keys that remain.

        Everything keyed by the API string is pruned together. Only
        ``_key_states`` used to be rebuilt, so ``cooldowns``, ``rpm`` and
        ``stats.key_usage`` grew for the life of the process and — worse —
        outlived the key itself. Since a key is identified by its own string,
        removing one that a 401 had parked for an hour and pasting it back
        produced a fresh KeyState with is_valid=True that is_key_healthy()
        still rejected, from a cooldown attached to a key the pool no longer
        believed it had.
        """
        with self._keys_write_lock:
            updated_states = {}
            for k in new_keys:
                if k in self._key_states:
                    updated_states[k] = self._key_states[k]
                else:
                    updated_states[k] = KeyState(k)
            self._keys = list(new_keys)
            self._key_states = updated_states

            live = set(new_keys)
            for gone in [k for k in self.cooldowns if k not in live]:
                del self.cooldowns[gone]
            for gone in [k for k in self.rpm if k not in live]:
                del self.rpm[gone]
            for gone in [k for k in self.stats.key_usage if k not in live]:
                del self.stats.key_usage[gone]

    @property
    def key_states(self) -> dict[str, KeyState]:
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
        self,
        key: str,
        reason: str = "",
        duration: float = DEFAULT_COOLDOWN,
        status: int = 0,
    ) -> None:
        self.cooldowns[key] = KeyCooldown(
            until=time.time() + duration, reason=reason, status=status
        )

    def cooldown_status(self, key: str) -> int:
        cd = self.cooldowns.get(key)
        return cd.status if cd is not None else 0

    # ── Aggregate throttle ──────────────────────────────────────────

    def note_rate_limited(self, key: str) -> bool:
        """Record a 429 and report whether it looks account-wide rather than per-key.

        Returns True when the pool has just been put on an aggregate pause.
        """
        now = time.time()
        self._recent_429.append((now, key))
        cutoff = now - POOL_429_WINDOW
        while self._recent_429 and self._recent_429[0][0] < cutoff:
            self._recent_429.popleft()

        if now < self.pool_throttled_until:
            return False  # already paused; don't re-log on every straggler
        distinct = {k for _ts, k in self._recent_429}
        if len(distinct) < POOL_429_DISTINCT_KEYS:
            return False

        self.pool_throttled_until = now + POOL_THROTTLE_PAUSE
        self.log_cb(
            f"⏸ {len(distinct)} keys rate-limited within {POOL_429_WINDOW:.0f}s — this is an "
            f"account-wide limit, not per-key. Pausing {POOL_THROTTLE_PAUSE:.0f}s instead of "
            f"rotating (rotation cannot beat a shared quota)."
        )
        return True

    def pool_throttle_remaining(self) -> float:
        r = self.pool_throttled_until - time.time()
        return r if r > 0 else 0.0

    def is_pool_throttled(self) -> bool:
        return self.pool_throttle_remaining() > 0

    def clear_cooldown(self, key: str) -> None:
        self.cooldowns.pop(key, None)

    def mark_key_failed(
        self,
        key: str,
        status: int = 0,
        retry_after: str | None = None,
        error_body: str | None = None,
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
            multiplier = None  # set below; None means "compute from adaptive formula"
            if retry_after:
                try:
                    # Trust NVIDIA's Retry-After exactly — do NOT apply the
                    # adaptive multiplier. NVIDIA's value already encodes how
                    # long to wait; multiplying it just makes recovery slower
                    # than the server actually requires, extending the doom
                    # loop where all keys sit locked out longer than needed.
                    base_duration = float(retry_after)
                    multiplier = 1.0  # honour upstream's explicit window
                except (ValueError, TypeError):
                    base_duration = COOLDOWN_DURATIONS[429]
                    multiplier = None  # compute adaptively below
            # Jitter up to 10s (was 30s): smaller spread reduces thundering
            # herd while still staggering simultaneous 429s.
            if multiplier is None:
                _r = random.Random(int(time.time()) ^ (hash(key) & 0xFFFFFFFF))
                base_duration = COOLDOWN_DURATIONS[429] + _r.uniform(0.0, 10.0)
            # Apply adaptive multiplier only when we chose the base (no
            # Retry-After was provided). With MULTIPLIER=1.5 and MAX=1.5
            # the exponent never exceeds the cap, so a simple threshold
            # is equivalent and clearer than min(pow(...), cap).
            failures = ks.consecutive_failures if ks is not None else 1
            if multiplier is None:
                multiplier = 1.0 if failures <= 1 else ADAPTIVE_COOLDOWN_MAX
            duration = base_duration * multiplier

            reason = f"429 rate-limited (cooldown {duration:.0f}s, attempt {failures})"
            if error_body:
                reason += f" - {error_body[:50]}"

            self.note_rate_limited(key)

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
        elif retry_after:
            # An explicit retry_after is the caller stating how long this key
            # should sit out, and it used to be honoured for 429 only: a 503
            # asking for 10s silently became the 30s default, so the log said
            # one thing ("10s cooldown") and the pool did another. A provider
            # outage then drained the pool three times faster than the code
            # claimed it would.
            try:
                duration = float(retry_after)
            except (ValueError, TypeError):
                duration = COOLDOWN_DURATIONS.get(status, DEFAULT_COOLDOWN)
            reason = f"{error_details} (cooldown {duration:.0f}s)"
        elif status in COOLDOWN_DURATIONS:
            base_duration = COOLDOWN_DURATIONS[status]
            # Apply adaptive multiplier for repeated failures (except auth errors)
            if status not in (401, 403) and ks is not None and ks.consecutive_failures > 1:
                multiplier = ADAPTIVE_COOLDOWN_MAX
                duration = base_duration * multiplier
                reason = (
                    f"{error_details} (cooldown {duration:.0f}s, attempt {ks.consecutive_failures})"
                )
            else:
                duration = base_duration
                reason = f"{error_details} (cooldown {duration:.0f}s)"
        else:
            duration = DEFAULT_COOLDOWN
            reason = error_details

        self.set_cooldown(key, reason=reason, duration=duration, status=status)

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

    def restore_key(self, key: str, *, rehab_rpm: bool = True) -> None:
        """Return a key to the pool.

        ``rehab_rpm=False`` clears the cooldown without giving throughput back.
        The health check needs that distinction: it proves a key is alive with
        GET /v1/models, which is a metadata endpoint that answers 200 even
        while chat/completions is rate-limited for that same key. Treating that
        as evidence of recovered throughput undid the backoff the pool had just
        learned, and the key went straight back into 429 — visible in the logs
        as "revived" followed within a minute by the same key rate-limited.
        """
        self.clear_cooldown(key)
        ks = self._key_states.get(key)
        if ks:
            ks.cooldown_until = 0.0
            ks.is_valid = True
            ks.consecutive_failures = 0
            ks.last_success_at = time.time()
        if not rehab_rpm:
            return
        # Graceful RPM rehab: bump the per-key ceiling back up by
        # ADAPTIVE_REHAB_STEP capped at MAX_RPM. A key that was stepped down on
        # 429 earns throughput back one successful window at a time, instead of
        # snapping straight to MAX_RPM (which would risk re-429).
        tracker = self.rpm.get(key)
        if tracker is None:
            return
        if tracker.max_rpm and tracker.max_rpm < MAX_RPM:
            new_ceiling = min(MAX_RPM, tracker.max_rpm + ADAPTIVE_REHAB_STEP)
            tracker.max_rpm = new_ceiling
            if new_ceiling >= MAX_RPM:
                tracker.max_rpm = 0  # fully rehabbed → inherit global ceiling

    # ── RPM API ─────────────────────────────────────────────────────

    def record_model_result(
        self,
        model: str,
        *,
        ok: bool = False,
        status: int = 0,
        ttft: float | None = None,
        too_slow: bool = False,
    ) -> None:
        """Score one attempt against a model and warn when it stops working."""
        if not model:
            return
        h = self.model_health.get(model)
        if h is None:
            h = ModelHealth()
            self.model_health[model] = h
        h.requests += 1
        if ok:
            h.success += 1
            h.record_success_reset()
            if ttft is not None:
                h.ttfts.append(ttft)
            return
        # Record failure for circuit breaker
        h.record_failure()
        if too_slow:
            h.too_slow += 1
        elif status in (502, 503, 504):
            h.gateway_timeouts += 1
        elif status == 429:
            h.rate_limited += 1

        if (
            h.requests >= _MODEL_WARN_MIN_REQUESTS
            and h.failure_rate >= _MODEL_WARN_FAILURE_RATE
            and time.time() - h.warned_at > _MODEL_WARN_INTERVAL
        ):
            h.warned_at = time.time()
            detail = []
            if h.gateway_timeouts:
                detail.append(f"{h.gateway_timeouts}× 503/504 provider down")
            if h.too_slow:
                detail.append(f"{h.too_slow}× timeout")
            if h.rate_limited:
                detail.append(f"{h.rate_limited}× 429")
            if h.median_ttft:
                detail.append(f"TTFT ~{h.median_ttft:.0f}s")
            circuit = " 🔴 CIRCUIT OPEN" if h.is_circuit_open else ""
            self.log_cb(
                f"⚠ {model}: {h.success}/{h.requests} OK"
                + (f" ({', '.join(detail)})" if detail else "")
                + circuit
            )

    def is_model_circuit_open(self, model: str) -> bool:
        """True if circuit breaker has tripped for this model."""
        h = self.model_health.get(model)
        return h is not None and h.is_circuit_open

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

        Every caller must pair this with end_in_flight in a finally block: the
        scheduling cost weighs in_flight at 4, so one leak per stream is enough
        to walk a key out of the pool over an afternoon.
        """
        ks = self._key_states.get(key)
        if ks is not None:
            # No clamp on the way up — the previous max(0, x + 1) suggested the
            # count could be negative here, which it cannot.
            ks.in_flight += 1

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

    def get_candidate_keys(self) -> list[tuple[int, str]]:
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
        cooldown: list[tuple[int, str, float]] = []

        for idx, key in enumerate(self._keys):
            ks = self._key_states.get(key)
            if not ks or not ks.is_valid:
                continue
            if self.is_key_on_cooldown(key):
                cooldown.append((idx, key, self.cooldown_remaining(key)))
                continue
            cost = (ks.in_flight * 4) + self.key_rpm(key) + ks.consecutive_failures * 8
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

    def decay_stale_failures(self, older_than: float = 180.0) -> int:
        """Forget consecutive failures a key has since outlived.

        Without this a key that had two transient errors five minutes ago
        keeps paying for them: the scheduling cost weighs failures at 8 each,
        so it stays at the back of the queue indefinitely.
        """
        now = time.time()
        decayed = 0
        for ks in self._key_states.values():
            if ks.consecutive_failures and now - ks.last_failure_at > older_than:
                ks.consecutive_failures -= 1
                decayed += 1
        return decayed

    def key_cooldown_info(self, key: str) -> tuple[float, str]:
        """Return ``(remaining_seconds, reason)`` for the dashboard."""
        if self.is_key_on_cooldown(key):
            return self.cooldown_remaining(key), self.cooldown_reason(key)
        return 0.0, ""
