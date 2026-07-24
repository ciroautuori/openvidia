"""Shared upstream utilities: concurrency semaphore and error detection.

Kept in a separate module to avoid circular imports between proxy_app
(which imports responses_shim) and responses_shim (which needs these
helpers from proxy_app).
"""

from __future__ import annotations

import asyncio
import json

# ── Global upstream concurrency cap ─────────────────────────────────────
# NVIDIA free-tier workers reject with:
#   "ResourceExhausted: Worker local total request limit reached (32/32)"
# when >32 concurrent requests hit the same worker worldwide. Stay under the ceiling
# by capping total in-flight upstream sends to 14.
_UPSTREAM_CONCURRENCY_LIMIT = 14
_upstream_sem: asyncio.Semaphore | None = None


def get_upstream_sem() -> asyncio.Semaphore:
    """Return (creating if needed) the shared upstream concurrency semaphore."""
    global _upstream_sem
    if _upstream_sem is None:
        _upstream_sem = asyncio.Semaphore(_UPSTREAM_CONCURRENCY_LIMIT)
    return _upstream_sem


def is_resource_exhausted(body: bytes | None) -> bool:
    """True when NVIDIA says the worker is full (concurrent limit), not RPM.

    429 with ResourceExhausted body = transient concurrency limit.
    429 without it = real RPM rate-limit → burn the key cooldown.
    """
    if not body:
        return False
    try:
        data = json.loads(body)
        msg = str(data.get("message", "") or data.get("detail", "") or "")
        return "ResourceExhausted" in msg or "request limit reached" in msg.lower()
    except Exception:
        return "ResourceExhausted" in body.decode("utf-8", errors="replace")
