"""Round-robin router over multiple NVIDIA-compatible upstream endpoints.

A single integrate.api.nvidia.com can 429 the whole account: keeping a
secondary endpoint (same key family, different host) and blacklisting a host
for a short window after a 5xx/timeout spreads load and survives partial
outages.
"""

from __future__ import annotations

import time

ENDPOINT_RETRY_AFTER = 60.0


class EndpointRouter:
    """Tracks per-endpoint health and hands out the healthy ones in order."""

    def __init__(self, endpoints: list[str]) -> None:
        self.endpoints = [e.rstrip("/") + "/" for e in endpoints]
        self._blacklisted_until: dict[str, float] = {}

    def healthy_endpoints(self) -> list[str]:
        now = time.monotonic()
        return [e for e in self.endpoints if self._blacklisted_until.get(e, 0.0) <= now]

    def mark_failure(self, url: str) -> None:
        for endpoint in self.endpoints:
            if url.startswith(endpoint):
                self._blacklisted_until[endpoint] = time.monotonic() + ENDPOINT_RETRY_AFTER
                return

    def mark_success(self, url: str) -> None:
        for endpoint in self.endpoints:
            if url.startswith(endpoint):
                self._blacklisted_until.pop(endpoint, None)
                return

    def as_dict(self) -> dict[str, object]:
        now = time.monotonic()
        return {
            "endpoints": [
                {
                    "url": e,
                    "healthy": self._blacklisted_until.get(e, 0.0) <= now,
                    "retry_in_s": max(0.0, self._blacklisted_until.get(e, 0.0) - now),
                }
                for e in self.endpoints
            ]
        }
