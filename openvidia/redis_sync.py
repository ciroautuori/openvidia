"""Optional cross-node state sync via Redis pub/sub.

Multi-node setups (several proxy instances behind the same pool of keys)
share cooldowns and circuit state so node A does not hammer a key that node
B just cooled down. Redis is an OPTIONAL extra: without it the proxy runs
standalone, in-memory, exactly as before.
"""

from __future__ import annotations

import asyncio
import json
import uuid

try:
    import redis.asyncio as aioredis
except ImportError:  # redis extra non installato
    aioredis = None

_CHANNEL = "openvidia:events"


class RedisSync:
    """Publish local state events and apply remote ones via on_remote."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.node_id = uuid.uuid4().hex[:8]
        self.on_remote = None
        self._client = aioredis.from_url(url) if aioredis else None
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._pubsub = None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def broadcast(self, payload: dict) -> None:
        """Fire-and-forget publish; the queue keeps it non-blocking."""
        if not self.enabled:
            return
        try:
            self._queue.put_nowait(payload)
        except Exception:
            pass

    async def start(self) -> None:
        if not self.enabled:
            return
        self._tasks.append(asyncio.create_task(self._publish_loop()))
        self._tasks.append(asyncio.create_task(self._listen_loop()))

    async def _publish_loop(self) -> None:
        while True:
            payload = await self._queue.get()
            try:
                await self._client.publish(_CHANNEL, json.dumps(payload))
            except Exception:
                pass

    async def _listen_loop(self) -> None:
        try:
            self._pubsub = self._client.pubsub()
            await self._pubsub.subscribe(_CHANNEL)
            async for message in self._pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    event = json.loads(message["data"])
                except (TypeError, ValueError, KeyError):
                    continue
                if event.get("node_id") == self.node_id:
                    # Il proprio messaggio arriva indietro dal canale: non va
                    # riapplicato (loop infinito altrimenti).
                    continue
                callback = self.on_remote
                if callback is not None:
                    await callback(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
