"""Rotation engine behaviour: what burns a key, what must not, and what leaks.

_rotation_phase is the single path every upstream send goes through — shims and
catch-all alike — and it had no tests at all. Each case here corresponds to a
way the pool used to lose keys to something that was not the key's fault.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from openvidia import proxy_state as ps
from openvidia.responses_shim import (
    _MAX_ROTATE_ATTEMPTS,
    _rotation_phase,
    _upstream_error_message,
)

pytestmark = pytest.mark.asyncio

KEYS = [f"nvapi-key{i:02d}" for i in range(6)]
FAST = httpx.Timeout(connect=1.0, read=1.0, write=1.0, pool=1.0)


def make_state(keys=None, log=None):
    return ps.ProxyState(
        keys=list(keys if keys is not None else KEYS),
        stats=ps.ProxyStats(),
        log_cb=log or (lambda _m: None),
    )


class FakeClient:
    """Replays a scripted sequence of upstream outcomes.

    Each script entry is either an int status, an Exception to raise, or a
    (status, body) pair. Records every send so tests can assert how many keys
    were actually spent.
    """

    def __init__(self, script):
        self.script = list(script)
        self.sent: list[str] = []

    def build_request(self, method, url, **kw):
        req = httpx.Request(method, url, headers=kw.get("headers") or {})
        req._ov_auth = (kw.get("headers") or {}).get("Authorization", "")
        return req

    async def send(self, req, stream=False):
        self.sent.append(getattr(req, "_ov_auth", ""))
        outcome = self.script.pop(0) if self.script else 500
        if isinstance(outcome, Exception):
            raise outcome
        status, body = outcome if isinstance(outcome, tuple) else (outcome, b"")
        return httpx.Response(status, content=body, request=req)


def hdr(k, idx):
    return {"Authorization": f"Bearer {k}"}


async def run(state, client, **kw):
    async with state.lock:
        candidates = state.get_candidate_keys()
    params = {
        "max_attempts": _MAX_ROTATE_ATTEMPTS,
        "timeout": FAST,
        "stream": False,
        "log_tag": "test",
    }
    params.update(kw)
    return await _rotation_phase(
        client,
        "https://upstream.invalid/v1/chat/completions",
        {"model": "test/model"},
        hdr,
        state,
        candidates,
        **params,
    )


# --------------------------------------------------------------------------- #
# Deterministic errors must not cost keys
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [400, 404, 422])
async def test_request_errors_do_not_rotate_or_cool_keys(status):
    """A malformed request used to cool down five keys before giving up."""
    state = make_state()
    client = FakeClient([(status, b'{"detail":"bad request"}')] * 6)
    outcome: dict = {}

    resp, key, idx = await run(state, client, outcome_box=outcome)

    assert resp is None
    assert len(client.sent) == 1, "must stop after the first key"
    assert not any(state.is_key_on_cooldown(k) for k in state.keys)
    assert outcome["deterministic"] is True
    assert outcome["status"] == status


async def test_deterministic_error_body_reaches_the_caller():
    state = make_state()
    body = json.dumps({"detail": "maximum context length is 128000 tokens"}).encode()
    client = FakeClient([(400, body)])
    outcome: dict = {}

    await run(state, client, outcome_box=outcome)

    msg = _upstream_error_message(outcome)
    assert "maximum context length" in msg


async def test_server_errors_still_rotate():
    """500 is not deterministic — another key may well be routed elsewhere."""
    state = make_state()
    client = FakeClient([500, 500, 200])
    resp, key, idx = await run(state, client)
    assert resp is not None and resp.status_code == 200
    assert len(client.sent) == 3


# --------------------------------------------------------------------------- #
# Transport events are not key faults
# --------------------------------------------------------------------------- #


async def test_goaway_retries_the_same_key():
    """HTTP/2 connection recycling took four keys out of the pool in one second."""
    state = make_state()
    client = FakeClient([httpx.RemoteProtocolError("<ConnectionTerminated ...>"), 200])

    resp, key, idx = await run(state, client)

    assert resp is not None and resp.status_code == 200
    assert len(client.sent) == 2
    assert client.sent[0] == client.sent[1], "retry must reuse the same key"
    assert not any(state.is_key_on_cooldown(k) for k in state.keys)


async def test_persistent_goaway_gives_up_and_rotates():
    state = make_state()
    err = httpx.RemoteProtocolError("<ConnectionTerminated ...>")
    client = FakeClient([err, err, 200])
    resp, key, idx = await run(state, client)
    # First key: two GOAWAYs → treated as a real failure, rotate to the next.
    assert resp is not None and resp.status_code == 200
    assert client.sent[0] == client.sent[1] != client.sent[2]


# --------------------------------------------------------------------------- #
# In-flight accounting
# --------------------------------------------------------------------------- #


async def test_failed_attempts_release_their_claim():
    """A leaked in_flight permanently deprioritises a key: cost is in_flight * 4."""
    state = make_state()
    client = FakeClient([500, 500, 500, 500, 500])

    await run(state, client)

    for k in state.keys:
        assert state.key_states[k].in_flight == 0


async def test_successful_send_keeps_the_claim_for_the_caller():
    state = make_state()
    client = FakeClient([200])
    resp, key, idx = await run(state, client)
    assert state.key_states[key].in_flight == 1
    state.end_in_flight(key)
    assert state.key_states[key].in_flight == 0


# --------------------------------------------------------------------------- #
# RPM accounting — the reason opencode saw 429 storms
# --------------------------------------------------------------------------- #


async def test_every_send_is_counted_against_the_rpm_window():
    """The catch-all never called record_request, so the throttle was inert."""
    state = make_state()
    client = FakeClient([500, 500, 200])

    _resp, key, _idx = await run(state, client)

    assert sum(state.key_rpm(k) for k in state.keys) == 3
    assert state.key_rpm(key) >= 1


async def test_requests_are_counted_even_when_they_fail():
    state = make_state()
    client = FakeClient([httpx.ConnectError("boom")] * 5)
    await run(state, client)
    assert sum(state.key_rpm(k) for k in state.keys) == len(client.sent)


async def test_keys_over_their_rpm_ceiling_are_skipped():
    state = make_state(keys=KEYS[:2])
    hot = state.keys[0]
    tracker = state.rpm.setdefault(hot, ps.RpmTracker())
    tracker.max_rpm = 1
    tracker.record()

    client = FakeClient([200])
    _resp, key, _idx = await run(state, client)

    assert key != hot


# --------------------------------------------------------------------------- #
# Attempt budget
# --------------------------------------------------------------------------- #


async def test_attempt_budget_is_honoured():
    state = make_state(keys=[f"nvapi-k{i}" for i in range(20)])
    client = FakeClient([500] * 30)
    await run(state, client)
    assert len(client.sent) == _MAX_ROTATE_ATTEMPTS


async def test_spent_budget_does_not_pay_for_the_remaining_passes():
    """`break` left the inner loop only, so each exhausted phase slept 2s extra."""
    state = make_state(keys=[f"nvapi-k{i}" for i in range(20)])
    client = FakeClient([500] * 30)

    started = asyncio.get_running_loop().time()
    await run(state, client)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.5, f"took {elapsed:.2f}s — the inter-pass sleeps still run"


# --------------------------------------------------------------------------- #
# Aggregate (account-wide) rate limiting
# --------------------------------------------------------------------------- #


async def test_correlated_429s_are_treated_as_an_account_limit():
    """Independent per-key budgets do not expire in the same second."""
    logs: list[str] = []
    state = make_state(log=logs.append)

    for k in state.keys[: ps.POOL_429_DISTINCT_KEYS]:
        state.mark_key_failed(k, status=429)

    assert state.is_pool_throttled()
    assert any("account-wide" in m for m in logs)


async def test_a_single_key_hitting_429_is_not_an_account_limit():
    state = make_state()
    state.mark_key_failed(state.keys[0], status=429)
    assert not state.is_pool_throttled()


async def test_rotation_stops_once_the_pool_is_throttled():
    state = make_state()
    body = b'{"status":429,"title":"Too Many Requests"}'
    client = FakeClient([(429, body)] * 6)

    await run(state, client)

    # Three distinct keys trip the detector; the fourth send never happens.
    assert len(client.sent) == ps.POOL_429_DISTINCT_KEYS
    assert state.is_pool_throttled()


async def test_throttle_expires():
    state = make_state()
    state.pool_throttled_until = 0.0
    assert not state.is_pool_throttled()


# --------------------------------------------------------------------------- #
# Cooldown durations
# --------------------------------------------------------------------------- #


async def test_gateway_timeout_honours_the_short_retry_after():
    """The log said 10s while the pool actually sat out the 30s default."""
    state = make_state()
    key = state.keys[0]

    state.mark_key_failed(key, status=503, retry_after=10)

    assert 9 <= state.cooldown_remaining(key) <= 11


async def test_429_cooldown_records_its_status():
    state = make_state()
    key = state.keys[0]
    state.mark_key_failed(key, status=429)
    assert state.cooldown_status(key) == 429


async def test_health_probe_does_not_hand_back_rpm_headroom():
    """GET /v1/models answers 200 for a key whose chat quota is spent."""
    state = make_state()
    key = state.keys[0]
    tracker = state.rpm.setdefault(key, ps.RpmTracker())
    tracker.max_rpm = ps.ADAPTIVE_FLOOR_RPM

    state.restore_key(key, rehab_rpm=False)

    assert tracker.max_rpm == ps.ADAPTIVE_FLOOR_RPM
    assert not state.is_key_on_cooldown(key)


async def test_real_success_does_hand_rpm_headroom_back():
    state = make_state()
    key = state.keys[0]
    tracker = state.rpm.setdefault(key, ps.RpmTracker())
    tracker.max_rpm = ps.ADAPTIVE_FLOOR_RPM

    state.restore_key(key)

    assert tracker.max_rpm > ps.ADAPTIVE_FLOOR_RPM


# --------------------------------------------------------------------------- #
# Error message construction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "outcome,expected_fragment",
    [
        ({}, "all keys failed"),
        ({"status": 503, "body": ""}, "last upstream status 503"),
        ({"status": 400, "body": '{"detail":"no such model"}'}, "no such model"),
        ({"status": 400, "body": '{"error":{"message":"bad tool"}}'}, "bad tool"),
        ({"status": 429, "body": '{"title":"Too Many Requests"}'}, "Too Many Requests"),
        ({"status": 500, "body": "plain text failure"}, "plain text failure"),
    ],
)
async def test_upstream_error_message(outcome, expected_fragment):
    assert expected_fragment in _upstream_error_message(outcome)
