"""Graph engine for OpenVIDIA: Hub + run_agent + graph tools, DeepSeek local.

Implements the Anthropic graph-loop patterns (orchestrator-subagent,
generator-verifier, fresh context per worker, depth bound) on top of the
OpenVIDIA proxy at localhost:1919.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:1919/v1"
# L'alias "openvidia" è risolto dal proxy all'active model: robusto ai cambi
# di modello senza toccare il client (un ID upstream hardcoded andrebbe in 404).
DEFAULT_MODEL = "openvidia"
MAX_BUDGET_EXHAUSTED = "<budget exhausted>"
MAX_ITERATIONS_REACHED = "<max iterations reached>"
MAX_TIMEOUT = "<timeout>"


@dataclass
class Provider:
    """OpenAI-compatible chat provider pointing at the OpenVIDIA proxy."""

    base_url: str = DEFAULT_BASE_URL
    api_key: str = "ignored"
    model: str = DEFAULT_MODEL
    timeout_s: float = 120.0
    temperature: float | None = None

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        own_client = client is None
        # Il client si chiude solo se creato qui: chi lo passa (hub.client) lo gestisce.
        hc = client or httpx.AsyncClient(timeout=self.timeout_s)
        try:
            resp = await hc.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            resp.raise_for_status()
            return resp.json()
        finally:
            if own_client:
                await hc.aclose()


@dataclass
class ToolSpec:
    """A tool an agent can call: name, description, async handler, JSON schema."""

    name: str
    description: str
    handler: Callable[[dict[str, Any], AgentCtx], Awaitable[str]]
    parameters: dict[str, Any] | None = None

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }


@dataclass
class AgentCtx:
    """Context handed to tool handlers: the hub plus who is calling."""

    hub: Hub
    name: str


@dataclass
class WorkerSpec:
    """Spec for a spawnable worker: system prompt, tools, provider, limits."""

    system: str
    tools: tuple[ToolSpec, ...] = ()
    provider: Provider | Callable[[str], Provider] | None = None
    max_iterations: int = 30
    budget_tokens: int | None = None
    timeout_s: float = 300.0


class Hub:
    """Registry of agents: statuses, mailboxes, tasks, events."""

    def __init__(
        self,
        *,
        default_provider: Provider | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.default_provider = default_provider or Provider()
        self.client = client
        self._status: dict[str, str] = {}
        self._mailboxes: dict[str, asyncio.Queue] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._events: list[dict[str, Any]] = []
        self._counter = 0

    def register(self, name: str) -> None:
        if name in self._status:
            raise ValueError(f"agent already registered: {name}")
        self._status[name] = "active"
        self._mailboxes[name] = asyncio.Queue()
        self.log_event("register", name)

    def new_name(self, prefix: str = "helper") -> str:
        while True:
            self._counter += 1
            candidate = f"{prefix}{self._counter}"
            if candidate not in self._status:
                return candidate

    def mailbox(self, name: str) -> asyncio.Queue:
        try:
            return self._mailboxes[name]
        except KeyError:
            raise KeyError(f"unknown agent: {name}") from None

    def log_event(self, kind: str, agent: str, detail: str | None = None) -> None:
        self._events.append({"kind": kind, "agent": agent, "detail": detail})

    def snapshot(self) -> dict[str, Any]:
        return {
            "agents": dict(self._status),
            "events": list(self._events),
        }

    async def close(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)


def _extract_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


async def run_agent(
    hub: Hub,
    name: str,
    *,
    system: str,
    first_turn: str,
    tools: tuple[ToolSpec, ...] = (),
    extra_dispatch: dict[str, ToolSpec] | None = None,
    provider: Provider | Callable[[str], Provider] | None = None,
    worker_factory: Callable[[str], WorkerSpec] | None = None,
    max_iterations: int = 50,
    budget_tokens: int | None = None,
    timeout_s: float = 600.0,
) -> str:
    """Run one agent loop: chat completions until a plain-text reply or a limit."""
    agent_provider = provider or hub.default_provider
    if callable(agent_provider):
        agent_provider = agent_provider(name)
    # Idempotente: lo spawn registra già nome e mailbox prima di creare il task,
    # così un worker cancellato prima dello start risulta comunque registrato.
    if name not in hub._status:
        hub.register(name)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": first_turn},
    ]
    usage = {"prompt": 0, "completion": 0}

    dispatch: dict[str, ToolSpec] = dict(extra_dispatch or {})
    for spec in tools:
        dispatch[spec.name] = spec
    graph_specs = graph_tools(hub, worker_factory=worker_factory)
    for spec in graph_specs:
        dispatch[spec.name] = spec
    schemas = [spec.schema() for spec in dispatch.values()]

    hub._tasks[name] = asyncio.current_task()
    try:
        async with asyncio.timeout(timeout_s):
            for _ in range(max_iterations):
                if budget_tokens is not None and (
                    usage["prompt"] + usage["completion"] >= budget_tokens
                ):
                    hub.log_event("budget", name, str(budget_tokens))
                    return MAX_BUDGET_EXHAUSTED
                reply = await agent_provider.chat(messages, tools=schemas, client=hub.client)
                choices = reply.get("choices") or []
                if not choices:
                    # Risposta vuota: meglio crashare che far credere a un successo.
                    raise RuntimeError(f"empty response from {agent_provider.model}")
                choice = choices[0]
                message = choice.get("message", {})
                usage["prompt"] += reply.get("usage", {}).get("prompt_tokens", 0)
                usage["completion"] += reply.get("usage", {}).get("completion_tokens", 0)
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    return _extract_text(message)
                messages.append(message)
                for tool_call in tool_calls:
                    function = tool_call.get("function", {})
                    tool_name = function.get("name")
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                    tool = dispatch.get(tool_name)
                    if tool is None:
                        result = f"tool '{tool_name}' not found"
                    else:
                        try:
                            result = await tool.handler(arguments, AgentCtx(hub, name))
                        except Exception as exc:
                            result = f"tool error: {exc}"
                    if not isinstance(result, str):
                        result = json.dumps(result, ensure_ascii=False)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.get("id", ""),
                            "content": result,
                        }
                    )
        return MAX_ITERATIONS_REACHED
    except TimeoutError:
        hub.log_event("timeout", name, str(timeout_s))
        hub._status[name] = "timeout"
        return MAX_TIMEOUT
    except asyncio.CancelledError:
        hub._status[name] = "killed"
        raise
    except Exception as exc:
        hub.log_event("crash", name, str(exc))
        hub._status[name] = "crashed"
        raise
    finally:
        hub._tasks.pop(name, None)
        if hub._status.get(name) == "active":
            hub._status[name] = "done"


async def _get_status(args: dict[str, Any], ctx: AgentCtx) -> str:
    return json.dumps(ctx.hub.snapshot()["agents"], ensure_ascii=False)


async def _send_message(args: dict[str, Any], ctx: AgentCtx) -> str:
    recipients = args.get("recipients") or []
    message = args.get("message") or ""
    if not message:
        return "error: 'message' required"
    # Validare prima di consegnare: niente messaggi a metà strada.
    for recipient in recipients:
        if recipient not in ctx.hub._status:
            return f"error: unknown agent '{recipient}'"
    for recipient in recipients:
        ctx.hub._mailboxes[recipient].put_nowait({"from": ctx.name, "content": message})
    return f"sent to {len(recipients)} recipient(s)"


async def _wait_for_message(args: dict[str, Any], ctx: AgentCtx) -> str:
    timeout = float(args.get("timeout") or 30.0)
    try:
        msg = await asyncio.wait_for(ctx.hub.mailbox(ctx.name).get(), timeout)
        return json.dumps(msg, ensure_ascii=False)
    except TimeoutError:
        return "error: timeout waiting for message"


def _make_create_subagents(
    worker_factory: Callable[[str], WorkerSpec],
) -> Callable[[dict[str, Any], AgentCtx], Awaitable[str]]:
    # Closure sulla factory: il handler di un tool non riceve la factory dagli args.
    async def create_subagents(args: dict[str, Any], ctx: AgentCtx) -> str:
        return await _create_subagents(args, ctx, worker_factory)

    return create_subagents


async def _create_subagents(
    args: dict[str, Any],
    ctx: AgentCtx,
    worker_factory: Callable[[str], WorkerSpec],
) -> str:
    names = args.get("names") or []
    first_turns = args.get("tasks") or {}
    if not names:
        return "error: 'names' required"
    for name in names:
        if name in ctx.hub._status:
            return f"error: agent '{name}' already exists"
    spawned: list[str] = []
    for name in names:
        spec = worker_factory(name)
        spec_provider = spec.provider
        if callable(spec_provider):
            spec_provider = spec_provider(name)
        # Registrazione sincrona: lo status deve esistere anche se il task viene
        # cancellato prima di partire (il corpo di un task mai avviato non gira).
        ctx.hub.register(name)
        # Depth bound: i worker girano senza worker_factory, non possono spawnare.
        task = asyncio.create_task(
            run_agent(
                ctx.hub,
                name,
                system=spec.system.format(name=name),
                first_turn=first_turns.get(name, ""),
                tools=spec.tools,
                provider=spec_provider,
                max_iterations=spec.max_iterations,
                budget_tokens=spec.budget_tokens,
                timeout_s=spec.timeout_s,
                worker_factory=None,
            )
        )
        ctx.hub._tasks[name] = task
        spawned.append(name)
    return f"spawned: {', '.join(spawned)}"


async def _kill_subagents(args: dict[str, Any], ctx: AgentCtx) -> str:
    names = args.get("names") or []
    killed: list[str] = []
    for name in names:
        task = ctx.hub._tasks.get(name)
        if task is None:
            return f"error: agent '{name}' not running"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # Copre il caso task mai avviato: il corpo non esegue il finally di run_agent.
        ctx.hub._status[name] = "killed"
        killed.append(name)
    return f"killed: {', '.join(killed)}"


def graph_tools(
    hub: Hub, worker_factory: Callable[[str], WorkerSpec] | None = None
) -> list[ToolSpec]:
    """Tools di coordinamento: sempre status/messaggi; spawn/kill solo con factory."""
    tools: list[ToolSpec] = [
        ToolSpec(
            "get_status",
            "List all agents and their status (active/done/killed/timeout/crashed).",
            _get_status,
        ),
        ToolSpec(
            "send_message",
            "Send a message to one or more agents; they receive it via wait_for_message.",
            _send_message,
            {
                "type": "object",
                "properties": {
                    "recipients": {"type": "array", "items": {"type": "string"}},
                    "message": {"type": "string"},
                },
                "required": ["recipients", "message"],
            },
        ),
        ToolSpec(
            "wait_for_message",
            "Wait for a message from another agent. Returns the JSON message.",
            _wait_for_message,
            {
                "type": "object",
                "properties": {"timeout": {"type": "number", "default": 30}},
            },
        ),
    ]
    if worker_factory is not None:
        tools.extend(
            [
                ToolSpec(
                    "create_subagents",
                    "Spawn worker agents (names) with the configured factory; each gets a fresh context.",
                    _make_create_subagents(worker_factory),
                    {
                        "type": "object",
                        "properties": {
                            "names": {"type": "array", "items": {"type": "string"}},
                            "tasks": {"type": "object"},
                        },
                        "required": ["names"],
                    },
                ),
                ToolSpec(
                    "kill_subagents",
                    "Cancel running worker agents by name.",
                    _kill_subagents,
                    {
                        "type": "object",
                        "properties": {"names": {"type": "array", "items": {"type": "string"}}},
                        "required": ["names"],
                    },
                ),
            ]
        )
    return tools


async def generate_and_verify(
    hub: Hub,
    *,
    name: str,
    system: str,
    base_first_turn: str,
    generator_tools: tuple[ToolSpec, ...],
    criteria: list[str],
    provider: Provider,
    verifier_provider: Provider | None = None,
    max_rounds: int = 3,
) -> tuple[str, int, bool]:
    """Generator-verifier loop with explicit rubric; returns (artifact, rounds, approved)."""
    verifier = verifier_provider or provider
    first_turn = base_first_turn
    artifact = ""
    rubric = "\n".join(f"- {criterion}" for criterion in criteria)
    for round_no in range(1, max_rounds + 1):
        artifact = await run_agent(
            hub,
            f"{name}:gen:{round_no}",
            system=system,
            first_turn=first_turn,
            tools=generator_tools,
            provider=provider,
        )
        verdict = await run_agent(
            hub,
            f"{name}:ver:{round_no}",
            system=(
                "You are a strict verifier. Judge the artifact against the rubric. "
                "Reply ONLY with APPROVED or REJECTED: <motivo>"
            ),
            first_turn=f"Rubric:\n{rubric}\n\nArtifact:\n{artifact}\n\nVerdict:",
            provider=verifier,
            max_iterations=1,
        )
        verdict = verdict.strip()
        if verdict.startswith("APPROVED"):
            return artifact, round_no, True
        feedback = verdict[len("REJECTED") :].strip() if verdict.startswith("REJECTED") else verdict
        first_turn = f"{base_first_turn}\n\nFeedback from verifier (round {round_no}):\n{feedback}"
    return artifact, max_rounds, False
