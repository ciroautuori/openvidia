"""State that outlives what it describes, and translation gaps for Codex."""

from __future__ import annotations

import time

import pytest

from openvidia import proxy_state as ps
from openvidia.responses_shim import (
    _build_chat_payload,
    _input_to_messages,
    json_headers,
)


def make_state(keys):
    return ps.ProxyState(keys=list(keys), stats=ps.ProxyStats(), log_cb=lambda _m: None)


# --------------------------------------------------------------------------- #
# Per-key maps must not outlive the key
# --------------------------------------------------------------------------- #


def test_removing_a_key_drops_everything_keyed_on_it():
    """A 1h 401 cooldown used to survive removal and re-adding the same key."""
    state = make_state(["a", "b"])
    state.mark_key_failed("a", status=401)
    state.record_request("a")
    state.stats.record_key_usage("a", ok=True)

    assert "a" in state.cooldowns and "a" in state.rpm

    state.keys = ["b"]

    assert "a" not in state.cooldowns
    assert "a" not in state.rpm
    assert "a" not in state.stats.key_usage


def test_a_re_added_key_starts_clean():
    state = make_state(["a", "b"])
    state.mark_key_failed("a", status=401)  # 1 hour
    assert state.is_key_on_cooldown("a")

    state.keys = ["b"]
    state.keys = ["b", "a"]

    assert not state.is_key_on_cooldown("a")
    assert state.is_key_healthy("a")


def test_surviving_keys_keep_their_state():
    state = make_state(["a", "b"])
    state.mark_key_failed("b", status=429)

    state.keys = ["b", "c"]

    assert state.is_key_on_cooldown("b"), "state for a key that stayed was discarded"


# --------------------------------------------------------------------------- #
# Failure decay
# --------------------------------------------------------------------------- #


def test_stale_failures_decay():
    """Cost weighs failures at 8, so a key never forgiven never gets traffic."""
    state = make_state(["a"])
    ks = state.key_states["a"]
    ks.consecutive_failures = 3
    ks.last_failure_at = time.time() - 600

    assert state.decay_stale_failures() == 1
    assert state._key_states["a"].consecutive_failures == 2


def test_recent_failures_do_not_decay():
    state = make_state(["a"])
    ks = state._key_states["a"]
    ks.consecutive_failures = 3
    ks.last_failure_at = time.time()

    assert state.decay_stale_failures() == 0
    assert ks.consecutive_failures == 3


def test_in_flight_never_goes_negative():
    state = make_state(["a"])
    state.end_in_flight("a")
    assert state.key_states["a"].in_flight == 0


# --------------------------------------------------------------------------- #
# Shared header factory
# --------------------------------------------------------------------------- #


def test_json_headers_carry_the_key():
    h = json_headers("nvapi-x", 0)
    assert h["Authorization"] == "Bearer nvapi-x"
    assert h["Content-Type"] == "application/json"


# --------------------------------------------------------------------------- #
# Responses API fields that were silently dropped
# --------------------------------------------------------------------------- #


def test_assistant_history_text_is_not_lost():
    """Assistant turns replayed as output_text became empty messages, so the
    model could not see its own previous replies."""
    msgs = _input_to_messages(
        [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "what is 2+2"}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "it is 4"}],
            },
        ]
    )
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == "it is 4"


def test_reasoning_effort_is_read_from_the_responses_shape():
    """Codex sends reasoning:{effort}; the shim read reasoning_effort."""
    payload = _build_chat_payload(
        {"input": "hi", "model": "m", "reasoning": {"effort": "low"}}, None
    )
    assert payload["chat_template_kwargs"]["enable_thinking"] is False


def test_high_effort_enables_thinking():
    payload = _build_chat_payload(
        {"input": "hi", "model": "m", "reasoning": {"effort": "high"}}, None
    )
    assert payload["chat_template_kwargs"]["enable_thinking"] is True


def test_max_output_tokens_is_translated():
    payload = _build_chat_payload({"input": "hi", "model": "m", "max_output_tokens": 256}, None)
    assert payload["max_tokens"] == 256


def test_an_explicit_max_tokens_wins():
    payload = _build_chat_payload(
        {"input": "hi", "model": "m", "max_tokens": 10, "max_output_tokens": 999}, None
    )
    assert payload["max_tokens"] == 10


def test_structured_output_is_translated_from_text_format():
    schema = {"type": "json_schema", "json_schema": {"name": "r", "schema": {"type": "object"}}}
    payload = _build_chat_payload({"input": "hi", "model": "m", "text": {"format": schema}}, None)
    assert payload["response_format"] == schema


def test_response_format_still_wins_when_sent_directly():
    payload = _build_chat_payload(
        {
            "input": "hi",
            "model": "m",
            "response_format": {"type": "json_object"},
            "text": {"format": {"type": "text"}},
        },
        None,
    )
    assert payload["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(
    "finish,expected",
    [
        ("stop", "completed"),
        ("tool_calls", "completed"),
        ("length", "incomplete"),
        ("content_filter", "incomplete"),
    ],
)
def test_tool_calls_is_a_completed_response(finish, expected):
    """finish_reason "tool_calls" was reported as incomplete, which tells a
    conforming client the answer was truncated."""
    from openvidia.responses_shim import _chat_response_to_responses

    out = _chat_response_to_responses(
        {
            "id": "x",
            "created": 0,
            "choices": [
                {"message": {"role": "assistant", "content": "hi"}, "finish_reason": finish}
            ],
        },
        "m",
    )
    assert out["status"] == expected
