"""
Shared state for the running proxy.

Counters are plain ints, not real atomics: uvicorn runs a single asyncio
event loop with no preemption between awaits, so a bare `+= 1` inside a
handler is already race-free here — the Rust AtomicU64 was for a genuinely
multi-threaded tokio runtime, which we don't have with one asyncio loop.
If you ever run this with multiple uvicorn *workers* (separate processes),
these counters stop being shared and this assumption breaks.
"""
import asyncio
from collections import deque
from pathlib import Path
from typing import Callable, List

from .config import atomic_write


class ProxyStats:
    def __init__(self, current_index: int = 0):
        self.requests = 0
        self.rotations = 0
        self.success = 0
        self.current_index = current_index


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
        # Optional callback fired when a key returns 401/403/404 in the proxy.
        # Receives the failed key value. Used by account_manager to replenish.
        self.on_key_failed: Optional[Callable[[str], None]] = None

    def log_cb(self, msg: str) -> None:
        self._log_cb(msg)
        self.log_buffer.append(msg)


def persist_index(state: ProxyState, i: int) -> None:
    """
    Store the working-key pointer and mirror it to disk, but only when it
    actually moves — the common case (same key keeps working) re-stores the
    same value and skips the write.

    A hard kill between the change and the write loses the last index;
    acceptable — a stale index just costs one extra rotation after restart.
    """
    previous = state.stats.current_index
    state.stats.current_index = i
    if previous != i:
        try:
            atomic_write(state.index_path, str(i))
        except OSError:
            pass
