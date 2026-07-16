"""
Shared state for the running proxy.

Merges il meglio di entrambe le versioni:
- Dal VECCHIO: KeyState con is_valid permanente, SSE listener push, persist asincrona,
  on_key_failed callback, key setter con preservazione stati
- Dal NUOVO: KeyCooldown dataclass, RpmTracker sliding window, COOLDOWN_DURATIONS,
  mark_key_failed con Retry-After parsing, API cooldown completa
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

MAX_RPM = 28                     # safe margin under NVIDIA's 40 RPM limit
RPM_WINDOW = 60.0                # sliding window in seconds

# Default cooldown durations by HTTP status
COOLDOWN_DURATIONS: Dict[int, float] = {
    400: 120.0,   # bad request — maybe model access issue
    401: 3600.0,  # unauthorized — dead key
    403: 3600.0,  # forbidden — dead key
    404: 120.0,   # not found — model not on this key
    429: 180.0,   # rate limited (Retry-After overrides this)
}
DEFAULT_COOLDOWN = 30.0


# ── KeyState (dal VECCHIO — traccia is_valid permanente) ──────────────

class KeyState:
    """Per-key state: validità permanente + cooldown temporaneo."""
    __slots__ = ("key", "is_valid", "cooldown_until", "last_error")

    def __init__(self, key: str):
        self.key = key
        self.is_valid = True       # False = permanentemente morta (401/403)
        self.cooldown_until = 0.0  # 0 = non in cooldown
        self.last_error = ""


# ── KeyCooldown (dal NUOVO — dataclass pulita per APICooldown) ─────────

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


# ── RPM Tracker (dal NUOVO — sliding window 60s) ─────────────────────

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


# ── Usage / Stats (identici in entrambe le versioni) ──────────────────

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


# ── ProxyState (merge) ────────────────────────────────────────────────

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
        # Protegge la coppia (_keys, _key_states) da scritture concorrenti
        # provenienti da thread OS reali (es. account_manager.py, che
        # rigenera le chiavi da threading.Thread) E dall'event loop asyncio.
        # asyncio.Lock da solo NON basta: non serializza rispetto a thread
        # esterni all'event loop. threading.Lock è invece cross-thread e,
        # per operazioni brevi come questa, sicuro da usare anche dentro
        # codice sync chiamato dall'event loop (nessun await al suo interno).
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

        # Cooldown / RPM per key (dal NUOVO)
        self.cooldowns: Dict[str, KeyCooldown] = {}
        self.rpm: Dict[str, RpmTracker] = {}

        # SSE listener push (dal VECCHIO)
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.get_event_loop()
        self.listeners: Set[asyncio.Queue] = set()

    # ── Keys (dal VECCHIO — preserva stati al update) ──────────────────

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
            # Riassegna insieme, sotto lock: chi legge keys/key_states da
            # un altro thread mentre questo blocco gira aspetta il lock
            # invece di vedere uno stato a metà aggiornamento.
            self._keys = list(new_keys)
            self._key_states = updated_states

    @property
    def key_states(self) -> Dict[str, KeyState]:
        with self._keys_write_lock:
            return dict(self._key_states)

    # ── Logging (dal VECCHIO — push ai listener SSE) ───────────────────

    def log_cb(self, msg: str) -> None:
        self._log_cb(msg)
        self.log_buffer.append(msg)
        if self.loop and self.loop.is_running():
            for q in list(self.listeners):
                self.loop.call_soon_threadsafe(q.put_nowait, msg)

    # ── Cooldown API (dal NUOVO — strutturata + Retry-After) ───────────

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

        # Dal VECCHIO: marca permanentemente invalida per 401/403
        if status in (401, 403):
            ks = self._key_states.get(key)
            if ks:
                ks.is_valid = False
                ks.last_error = f"HTTP {status}"
            self.log_cb(f"⚠ key marked INVALID (HTTP {status})")
        elif status in (400, 404, 429):
            # Cooldown temporaneo — non invalida permanentemente
            ks = self._key_states.get(key)
            if ks:
                ks.cooldown_until = time.time() + duration
                ks.last_error = reason
        else:
            ks = self._key_states.get(key)
            if ks:
                ks.cooldown_until = time.time() + duration
                ks.last_error = reason

        # Callback per auto-rigenerazione (dal VECCHIO)
        if self.on_key_failed is not None:
            self.on_key_failed(key)

    def restore_key(self, key: str) -> None:
        """Ripristina una chiave dopo successo (dal VECCHIO)."""
        self.clear_cooldown(key)
        ks = self._key_states.get(key)
        if ks:
            ks.cooldown_until = 0.0
            ks.is_valid = True

    # ── RPM API (dal NUOVO) ────────────────────────────────────────────

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

    # ── Composite helpers ──────────────────────────────────────────────

    def is_key_healthy(self, key: str) -> bool:
        """Una chiave è healthy quando valida AND non in cooldown."""
        ks = self._key_states.get(key)
        if ks and not ks.is_valid:
            return False
        return not self.is_key_on_cooldown(key)

    def get_candidate_keys(self) -> List[tuple[int, str]]:
        """
        Ritorna la lista di tuple (original_index, key) nell'ordine in cui
        devono essere provate per la richiesta corrente, ruotata rispetto a current_index.

        Pre-claim: avanza current_index subito, così richieste concorrenti partono
        da indici diversi e non collidono tutte sulla stessa chiave.
        """
        available: List[tuple[int, str]] = []
        cooldown: List[tuple[int, str, float]] = []

        for idx, key in enumerate(self._keys):
            ks = self._key_states.get(key)
            if not ks or not ks.is_valid:
                continue
            if self.is_key_on_cooldown(key):
                cooldown.append((idx, key, self.cooldown_remaining(key)))
            else:
                available.append((idx, key))

        if not available and cooldown:
            cooldown.sort(key=lambda x: x[2])
            available = [(idx, key) for idx, key, _ in cooldown]
            self.log_cb("⚠ No active keys outside cooldown, reusing cooldown keys")

        if not available:
            return []

        curr_idx = self.stats.current_index % len(self._keys) if self._keys else 0
        after = [x for x in available if x[0] >= curr_idx]
        before = [x for x in available if x[0] < curr_idx]
        ordered = after + before

        # Pre-claim: avanza subito l'indice al prossimo candidato,
        # così la prossima chiamata concorrente non parte dallo stesso indice.
        if ordered:
            next_candidate_idx = ordered[0][0]
            self.stats.current_index = (next_candidate_idx + 1) % len(self._keys)
            self.stats.active_key_index = next_candidate_idx

        return ordered

    def clear_cooldown_and_restore(self, key: str) -> None:
        """Usato dal health check per rivitalizzare una chiave."""
        self.restore_key(key)

    # ── Stats per key (dal NUOVO — include cooldown + RPM info) ─────────

    def key_cooldown_info(self, key: str) -> tuple[float, str]:
        """Ritorna (remaining_seconds, reason) per la UI."""
        if self.is_key_on_cooldown(key):
            return self.cooldown_remaining(key), self.cooldown_reason(key)
        return 0.0, ""


# ── Persist index asincrona (dal VECCHIO — non blocca il loop) ─────────

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
