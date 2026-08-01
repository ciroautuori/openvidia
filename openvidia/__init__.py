"""OpenVIDIA — multi-key NVIDIA NIM reverse proxy with desktop dashboard.

Exposes the high-level entry points used by the CLI launcher and the
webview UI. Importing this package triggers no side effects; the proxy
server and GUI are started explicitly via ``openvidia.__main__``.
"""

from __future__ import annotations

from openvidia.graph_engine import (
    AgentCtx,
    Hub,
    Provider,
    ToolSpec,
    WorkerSpec,
    generate_and_verify,
    graph_tools,
    run_agent,
)

__all__ = [
    "AgentCtx",
    "Hub",
    "Provider",
    "ToolSpec",
    "WorkerSpec",
    "generate_and_verify",
    "graph_tools",
    "run_agent",
]
