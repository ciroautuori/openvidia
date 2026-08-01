"""In-memory cache for /v1/embeddings responses.

Embedding vectors are deterministic per (model, input): caching the upstream
body avoids burning free-tier RPM budget on repeated identical requests.
"""

from __future__ import annotations

import hashlib
import json
import time

_CANONICAL_SEP = "\x1f"


class EmbeddingCache:
    """TTL cache keyed by sha256 of the canonical (model, inputs) tuple.

    The canonical form keeps the input ORDER (embedding order matters), so the
    digest covers the exact sequence the caller asked for.
    """

    def __init__(self, max_entries: int = 1000, ttl_s: float = 300.0) -> None:
        self.max_entries = max_entries
        self.ttl_s = ttl_s
        self._entries: dict[str, tuple[float, bytes]] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(model: str, inputs: str | list[str]) -> str:
        if isinstance(inputs, str):
            raw = model + _CANONICAL_SEP + inputs
        else:
            raw = model + _CANONICAL_SEP + json.dumps(inputs, ensure_ascii=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, model: str, inputs: str | list[str]) -> bytes | None:
        key = self._key(model, inputs)
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        inserted_at, body = entry
        if time.monotonic() - inserted_at > self.ttl_s:
            self._entries.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return body

    def set(self, model: str, inputs: str | list[str], body: bytes) -> None:
        # La prune è pigra: se la soglia è superata si butta il 10% più vecchio,
        # senza scansioni complete a ogni inserimento.
        if len(self._entries) >= self.max_entries:
            oldest = sorted(self._entries.items(), key=lambda kv: kv[1][0])[
                : max(1, self.max_entries // 10)
            ]
            for old_key, _ in oldest:
                self._entries.pop(old_key, None)
        self._entries[self._key(model, inputs)] = (time.monotonic(), body)

    def clear(self) -> None:
        self._entries.clear()
        self.hits = 0
        self.misses = 0

    def as_dict(self) -> dict[str, int]:
        return {"entries": len(self._entries), "hits": self.hits, "misses": self.misses}
