"""Test del graph engine: loop agent, orchestrazione worker, generator-verifier."""

from __future__ import annotations

import asyncio
import json

import pytest

from openvidia.graph_engine import (
    AgentCtx,
    Hub,
    Provider,
    ToolSpec,
    WorkerSpec,
    generate_and_verify,
    run_agent,
)

FINAL = "final answer"


def text_msg(content: str) -> dict:
    return {"role": "assistant", "content": content}


def tool_call_msg(name: str, arguments: dict | None = None, call_id: str = "call_1") -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments or {}),
                },
            }
        ],
    }


def with_usage(resp: dict, prompt: int = 10, completion: int = 5) -> dict:
    resp["usage"] = {"prompt_tokens": prompt, "completion_tokens": completion}
    return resp


class ScriptedProvider(Provider):
    """Provider fake: ogni chiamata pop la prossima risposta dalla script."""

    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.calls = 0
        self.last_messages: list[dict] | None = None

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        client=None,
    ) -> dict:
        self.calls += 1
        self.last_messages = messages
        item = dict(self.script.pop(0))
        latency = item.pop("latency", 0.0)
        if latency:
            await asyncio.sleep(latency)
        return item


def script(*responses: dict) -> list[dict]:
    return list(responses)


@pytest.mark.asyncio
async def test_single_tool_loop_with_sleep() -> None:
    called = []

    async def sleeper(args: dict, ctx: AgentCtx) -> str:
        await asyncio.sleep(0)
        called.append(args)
        return "slept"

    hub = Hub()
    try:
        provider = ScriptedProvider(
            script(
                {"choices": [{"message": tool_call_msg("sleeper", {"secs": 1})}]},
                {"choices": [{"message": text_msg(FINAL)}]},
            )
        )
        result = await run_agent(
            hub,
            "lead",
            system="sys",
            first_turn="go",
            tools=(ToolSpec("sleeper", "sleep", sleeper),),
            provider=provider,
        )
    finally:
        await hub.close()
    assert result == FINAL
    assert called == [{"secs": 1}]
    assert provider.calls == 2
    assert hub.snapshot()["agents"]["lead"] == "done"


@pytest.mark.asyncio
async def test_tool_error_recovers_with_message_in_payload() -> None:
    async def boom(args: dict, ctx: AgentCtx) -> str:
        raise RuntimeError("kaboom")

    hub = Hub()
    try:
        provider = ScriptedProvider(
            script(
                {"choices": [{"message": tool_call_msg("boom")}]},
                {"choices": [{"message": text_msg(FINAL)}]},
            )
        )
        result = await run_agent(
            hub,
            "lead",
            system="sys",
            first_turn="go",
            tools=(ToolSpec("boom", "explodes", boom),),
            provider=provider,
        )
    finally:
        await hub.close()
    assert result == FINAL
    tool_messages = [m for m in provider.last_messages if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["content"].startswith("tool error: kaboom")
    assert tool_messages[0]["tool_call_id"] == "call_1"


@pytest.mark.asyncio
async def test_spawn_wait_and_kill_worker() -> None:
    hub = Hub()

    def worker_factory(name: str) -> WorkerSpec:
        return WorkerSpec(
            system="You are {name}",
            provider=lambda n: ScriptedProvider(
                script(
                    {
                        "choices": [
                            {
                                "message": tool_call_msg(
                                    "send_message",
                                    {"recipients": ["lead"], "message": "ping"},
                                )
                            }
                        ],
                        "latency": 1.0,
                    },
                    {"choices": [{"message": text_msg("worker done")}]},
                )
            ),
        )

    lead_provider = ScriptedProvider(
        script(
            {"choices": [{"message": tool_call_msg("create_subagents", {"names": ["helper1"]})}]},
            {"choices": [{"message": tool_call_msg("kill_subagents", {"names": ["helper1"]})}]},
            {"choices": [{"message": text_msg(FINAL)}]},
        )
    )
    try:
        result = await run_agent(
            hub,
            "lead",
            system="sys",
            first_turn="go",
            provider=lead_provider,
            worker_factory=worker_factory,
        )
    finally:
        await hub.close()
    assert result == FINAL
    statuses = hub.snapshot()["agents"]
    assert statuses["lead"] == "done"
    assert statuses["helper1"] == "killed"


@pytest.mark.asyncio
async def test_two_workers_parallel_then_cleanup() -> None:
    hub = Hub()

    def worker_factory(name: str) -> WorkerSpec:
        return WorkerSpec(
            system="You are {name}",
            provider=lambda n: ScriptedProvider(
                script(
                    {
                        "choices": [
                            {
                                "message": tool_call_msg(
                                    "send_message",
                                    {"recipients": ["lead"], "message": "hi"},
                                )
                            }
                        ]
                    },
                    {"choices": [{"message": text_msg(f"{n} done")}]},
                )
            ),
        )

    lead_provider = ScriptedProvider(
        script(
            {
                "choices": [
                    {
                        "message": tool_call_msg(
                            "create_subagents", {"names": ["helper1", "helper2"]}
                        )
                    }
                ]
            },
            {"choices": [{"message": tool_call_msg("wait_for_message")}]},
            {"choices": [{"message": tool_call_msg("wait_for_message")}]},
            {"choices": [{"message": text_msg(FINAL)}]},
        )
    )
    try:
        result = await run_agent(
            hub,
            "lead",
            system="sys",
            first_turn="go",
            provider=lead_provider,
            worker_factory=worker_factory,
        )
    finally:
        await hub.close()
    assert result == FINAL
    statuses = hub.snapshot()["agents"]
    assert statuses["helper1"] == "done"
    assert statuses["helper2"] == "done"


@pytest.mark.asyncio
async def test_max_iterations_cap() -> None:
    hub = Hub()
    try:
        provider = ScriptedProvider(
            script(
                {"choices": [{"message": tool_call_msg("loop")}]},
                {"choices": [{"message": tool_call_msg("loop")}]},
                {"choices": [{"message": tool_call_msg("loop")}]},
            )
        )
        result = await run_agent(
            hub,
            "lead",
            system="sys",
            first_turn="go",
            provider=provider,
            max_iterations=2,
        )
    finally:
        await hub.close()
    assert result == "<max iterations reached>"


@pytest.mark.asyncio
async def test_budget_tokens_exhaustion() -> None:
    hub = Hub()
    try:
        provider = ScriptedProvider(
            script(
                with_usage(
                    {"choices": [{"message": tool_call_msg("loop")}]},
                    prompt=100,
                    completion=100,
                )
            )
        )
        result = await run_agent(
            hub,
            "lead",
            system="sys",
            first_turn="go",
            provider=provider,
            budget_tokens=150,
        )
    finally:
        await hub.close()
    assert result == "<budget exhausted>"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_agent_timeout() -> None:
    hub = Hub()
    try:
        provider = ScriptedProvider(
            script(
                {"choices": [{"message": text_msg("late")}], "latency": 1.0},
            )
        )
        result = await run_agent(
            hub,
            "lead",
            system="sys",
            first_turn="go",
            provider=provider,
            timeout_s=0.05,
        )
    finally:
        await hub.close()
    assert result == "<timeout>"
    assert hub.snapshot()["agents"]["lead"] == "timeout"


@pytest.mark.asyncio
async def test_empty_response_crashes() -> None:
    hub = Hub()
    try:
        provider = ScriptedProvider(script({"choices": []}))
        with pytest.raises(RuntimeError, match="empty response"):
            await run_agent(
                hub,
                "lead",
                system="sys",
                first_turn="go",
                provider=provider,
            )
    finally:
        await hub.close()
    assert hub.snapshot()["agents"]["lead"] == "crashed"


@pytest.mark.asyncio
async def test_generate_and_verify_rejected_then_approved() -> None:
    hub = Hub()
    try:
        generator = ScriptedProvider(
            script(
                {"choices": [{"message": text_msg("draft one")}]},
                {"choices": [{"message": text_msg("draft two")}]},
            )
        )
        verifier = ScriptedProvider(
            script(
                {"choices": [{"message": text_msg("REJECTED: troppo corto")}]},
                {"choices": [{"message": text_msg("APPROVED")}]},
            )
        )
        artifact, rounds, approved = await generate_and_verify(
            hub,
            name="plan",
            system="sys",
            base_first_turn="write the plan",
            generator_tools=(),
            criteria=["clear", "complete"],
            provider=generator,
            verifier_provider=verifier,
            max_rounds=3,
        )
    finally:
        await hub.close()
    assert approved is True
    assert rounds == 2
    assert artifact == "draft two"
    # Il generator del round 2 riceve il feedback del round 1 nel primo turno utente.
    assert "Feedback from verifier (round 1)" in generator.last_messages[1]["content"]


@pytest.mark.asyncio
async def test_generate_and_verify_no_convergence() -> None:
    hub = Hub()
    try:
        generator = ScriptedProvider(
            script(
                {"choices": [{"message": text_msg("draft")}]},
                {"choices": [{"message": text_msg("draft again")}]},
            )
        )
        verifier = ScriptedProvider(
            script(
                {"choices": [{"message": text_msg("REJECTED: no")}]},
                {"choices": [{"message": text_msg("REJECTED: still no")}]},
            )
        )
        artifact, rounds, approved = await generate_and_verify(
            hub,
            name="plan",
            system="sys",
            base_first_turn="write the plan",
            generator_tools=(),
            criteria=["clear"],
            provider=generator,
            verifier_provider=verifier,
            max_rounds=2,
        )
    finally:
        await hub.close()
    assert approved is False
    assert rounds == 2
    assert artifact == "draft again"
