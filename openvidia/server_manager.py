"""
Owns the actual uvicorn server instance(s) for one proxy run.

Binds loopback on BOTH IP stacks so a client reaching us as "localhost"
works whether its resolver picks IPv4 (127.0.0.1) or IPv6 (::1) — the
latter is what Node/Bun try first. Loopback-only, never all-interfaces:
the proxy serves unauthenticated API keys. IPv6 loopback is best-effort —
don't fail proxy startup on hosts with IPv6 off.
"""

import asyncio
import socket as socket_mod
from pathlib import Path
from typing import Callable, List, Optional

import uvicorn

from .proxy_app import create_app
from .proxy_state import ProxyState, ProxyStats


class ProxyServer:
    def __init__(
        self,
        servers: List[uvicorn.Server],
        tasks: List[asyncio.Task],
        state: ProxyState,
    ):
        self._servers = servers
        self._tasks = tasks
        self.state = state

    async def shutdown(self):
        for s in self._servers:
            s.should_exit = True
        for t in self._tasks:
            try:
                await asyncio.wait_for(t, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                t.cancel()


def _bind(family: int, host: str, port: int) -> Optional[socket_mod.socket]:
    try:
        sock = socket_mod.socket(family, socket_mod.SOCK_STREAM)
    except OSError:
        return None
    sock.setsockopt(socket_mod.SOL_SOCKET, socket_mod.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
        sock.listen(128)
        sock.setblocking(False)
        return sock
    except OSError:
        sock.close()
        return None


async def start(
    port: int,
    keys: List[str],
    log_cb: Callable[[str], None],
    stats: ProxyStats,
    index_path: Path,
    web_dir: Optional[Path] = None,
    initial_model: str = "",
) -> ProxyServer:
    state = ProxyState(
        keys=keys, stats=stats, index_path=index_path, log_cb=log_cb, port=port
    )
    if initial_model:
        state.active_model = initial_model
    app = create_app(state, web_dir=web_dir)

    # Bind explicitly first (like Rust's TcpListener::bind) so a host with
    # IPv6 off just skips that stack instead of taking the whole proxy down.
    v4_sock = _bind(socket_mod.AF_INET, "127.0.0.1", port)
    if v4_sock is None:
        raise RuntimeError(f"proxy failed to bind :{port} (IPv4 loopback)")

    v6_sock = _bind(socket_mod.AF_INET6, "::1", port)

    servers: List[uvicorn.Server] = []
    tasks: List[asyncio.Task] = []
    socks = [v4_sock]
    if v6_sock is not None:
        socks.append(v6_sock)

    v4_config = uvicorn.Config(app, log_level="warning", lifespan="on")
    v4_server = uvicorn.Server(v4_config)
    servers.append(v4_server)
    tasks.append(asyncio.create_task(v4_server.serve(sockets=[v4_sock])))

    if v6_sock is not None:
        v6_config = uvicorn.Config(app, log_level="warning", lifespan="off")
        v6_server = uvicorn.Server(v6_config)
        servers.append(v6_server)
        tasks.append(asyncio.create_task(v6_server.serve(sockets=[v6_sock])))

    for _ in range(100):
        if v4_server.started:
            break
        await asyncio.sleep(0.02)
    else:
        for s in socks:
            s.close()
        raise RuntimeError(f"proxy failed to start on :{port}")

    return ProxyServer(servers, tasks, state)
