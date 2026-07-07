"""
Shared state for the running proxy.
"""
import asyncio
import time
from collections import deque
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .config import atomic_write


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
        self.on_key_failed: Optional[Callable[[str], None]] = None
        self.active_model: Optional[str] = None

    def log_cb(self, msg: str) -> None:
        self._log_cb(msg)
        self.log_buffer.append(msg)


def persist_index(state: ProxyState, i: int) -> None:
    previous = state.stats.current_index
    state.stats.current_index = i
    state.stats.active_key_index = i
    if previous != i:
        try:
            atomic_write(state.index_path, str(i))
        except OSError:
            pass
