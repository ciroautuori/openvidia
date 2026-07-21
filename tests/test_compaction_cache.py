"""Regression tests for the compaction rolling cache and latency budget.

These lock the three properties that were broken and caused the observed
"summarize failed (ReadTimeout) → trim fallback" loop:
  1. the conversation key survives the conversation growing,
  2. a cached summary is reusable as a prefix (incremental summarization),
  3. a slow summarize never blocks the client past the inline deadline, and
     lands in the cache for the next turn.
"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from openvidia import compaction
from openvidia import proxy_app  # noqa: F401 — warm the lazy import maybe_compact does
from openvidia.compaction import (
    _assemble,
    _cache_get,
    _cache_put,
    _conv_key,
    _fingerprint,
    _render_for_summary,
    estimate_tokens,
    maybe_compact,
)
from openvidia.proxy_state import ProxyState, ProxyStats


SYS = [{"role": "system", "content": "You are helpful"}]


def _msgs(n, size=40, tag="m"):
    return [{"role": "user", "content": f"{tag}{i}: " + "x" * size} for i in range(n)]


@pytest.fixture(autouse=True)
def _clean_caches():
    compaction._rolling.clear()
    compaction._inflight.clear()
    compaction._settings_cache = None
    yield
    compaction._rolling.clear()
    compaction._inflight.clear()
    compaction._settings_cache = None


@pytest.fixture
def state():
    keys = [f"nvapi-key{i}234567890abcdef1234567890abcd" for i in range(4)]
    return ProxyState(
        keys=keys,
        stats=ProxyStats(current_index=0),
        index_path=Path("/tmp/test_compaction_index.json"),
        log_cb=MagicMock(),
        port=3940,
    )


class TestConvKeyStability:
    def test_key_survives_conversation_growth(self):
        """The cache key MUST NOT change when new turns are appended.

        Including len(rest) in the hash minted a new key every turn, so the
        rolling cache never hit and every turn re-summarized from scratch.
        """
        rest = _msgs(10)
        assert _conv_key(SYS, rest) == _conv_key(SYS, rest + _msgs(5, tag="new"))

    def test_key_differs_on_divergent_prefix(self):
        assert _conv_key(SYS, _msgs(6, tag="a")) != _conv_key(SYS, _msgs(6, tag="b"))


class TestRollingCache:
    def test_hit_is_incremental(self):
        """A summary covering the first N messages stays valid as history grows."""
        old = _msgs(30)
        _cache_put("ck", 20, "SUMMARY", _fingerprint(old[:20]))
        assert _cache_get("ck", old) == (20, "SUMMARY")
        # ... and still valid five turns later
        assert _cache_get("ck", old + _msgs(5, tag="z")) == (20, "SUMMARY")

    def test_miss_when_covered_prefix_changed(self):
        old = _msgs(30)
        _cache_put("ck", 20, "SUMMARY", _fingerprint(old[:20]))
        mutated = list(old)
        mutated[3] = {"role": "user", "content": "rewritten"}
        assert _cache_get("ck", mutated) is None

    def test_miss_when_history_shrank_below_coverage(self):
        old = _msgs(30)
        _cache_put("ck", 20, "SUMMARY", _fingerprint(old[:20]))
        assert _cache_get("ck", old[:10]) is None

    def test_eviction_is_fifo_not_wipe(self):
        for i in range(compaction._ROLLING_CAP + 5):
            _cache_put(f"ck{i}", 0, "s", _fingerprint([]))
        assert len(compaction._rolling) == compaction._ROLLING_CAP
        assert "ck0" not in compaction._rolling
        assert f"ck{compaction._ROLLING_CAP + 4}" in compaction._rolling


class TestAssemble:
    def test_respects_budget_by_dropping_oldest_remainder(self):
        out = _assemble(SYS, "S", _msgs(200), _msgs(8, tag="tail"), 400)
        assert estimate_tokens(out) <= 400
        assert out[len(SYS)]["content"].startswith("Previous conversation summary:")
        # the recent tail survives the drop
        assert out[-1]["content"].startswith("tail7")

    def test_keeps_everything_when_it_fits(self):
        rem = _msgs(3, tag="r")
        out = _assemble(SYS, "S", rem, _msgs(2, tag="t"), 100_000)
        assert len(out) == len(SYS) + 1 + 3 + 2


class TestSummaryInputBounds:
    def test_per_message_clipping(self):
        out = _render_for_summary([{"role": "user", "content": "y" * 50_000}], 1_000, 0)
        assert len(out) < 1_200
        assert "chars omitted" in out

    def test_total_cap_keeps_most_recent(self):
        msgs = [{"role": "user", "content": f"n{i} " + "y" * 900} for i in range(50)]
        out = _render_for_summary(msgs, 4_000, 5_000)
        assert len(out) <= 5_200
        assert "n49" in out and "n0 " not in out


class TestInlineDeadline:
    """The client must never wait on a slow upstream summarize."""

    @pytest.mark.asyncio
    async def test_slow_summarize_falls_back_then_warms_cache(self, state, monkeypatch):
        monkeypatch.setattr(
            compaction,
            "_settings",
            lambda: {**compaction._DEFAULTS, "inline_deadline": 0.2},
        )
        started = asyncio.Event()
        release = asyncio.Event()  # the test decides when the upstream answers

        async def slow(*a, **kw):
            started.set()
            await release.wait()
            return "BACKGROUND SUMMARY"

        monkeypatch.setattr(compaction, "_summarize", slow)

        big = SYS + [{"role": "user", "content": "q " + "x" * 4_000} for _ in range(120)]
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        first = await maybe_compact(big, state=state, client=None, log=lambda m: None)
        elapsed = loop.time() - t0

        # The summarize has NOT returned yet, and the request was served anyway.
        assert started.is_set() and not release.is_set()
        assert elapsed < 2.0, "request blocked past the inline deadline"
        assert first is not big and estimate_tokens(first) < estimate_tokens(big)

        # The detached task completes and populates the cache for the next turn.
        release.set()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not compaction._inflight:
                break
        second = await maybe_compact(big, state=state, client=None, log=lambda m: None)
        assert any(
            isinstance(m.get("content"), str) and "BACKGROUND SUMMARY" in m["content"]
            for m in second
        )

    @pytest.mark.asyncio
    async def test_concurrent_requests_share_one_summarize(self, state, monkeypatch):
        monkeypatch.setattr(
            compaction,
            "_settings",
            lambda: {**compaction._DEFAULTS, "inline_deadline": 5.0},
        )
        calls = []

        async def counted(*a, **kw):
            calls.append(1)
            await asyncio.sleep(0.15)
            return "ONE SUMMARY"

        monkeypatch.setattr(compaction, "_summarize", counted)
        big = SYS + [{"role": "user", "content": "q " + "x" * 4_000} for _ in range(120)]

        outs = await asyncio.gather(
            *[
                maybe_compact(big, state=state, client=None, log=lambda m: None)
                for _ in range(4)
            ]
        )
        assert len(calls) == 1, "duplicate summarize for the same conversation"
        for o in outs:
            assert any("ONE SUMMARY" in str(m.get("content", "")) for m in o)

    @pytest.mark.asyncio
    async def test_failure_falls_back_to_trim_without_raising(self, state, monkeypatch):
        monkeypatch.setattr(
            compaction,
            "_settings",
            lambda: {**compaction._DEFAULTS, "inline_deadline": 5.0},
        )

        async def boom(*a, **kw):
            raise RuntimeError("all keys failed")

        monkeypatch.setattr(compaction, "_summarize", boom)
        big = SYS + [{"role": "user", "content": "q " + "x" * 4_000} for _ in range(120)]
        out = await maybe_compact(big, state=state, client=None, log=lambda m: None)
        assert estimate_tokens(out) <= 80_000
        assert any("omitted to fit context" in str(m.get("content", "")) for m in out)


class TestBoundaryAdvance:
    """The summary boundary advances in chunks, not once per turn."""

    @staticmethod
    def _big(n):
        return SYS + [
            {"role": "user", "content": f"t{i} " + "x" * 4_000} for i in range(n)
        ]

    @pytest.mark.asyncio
    async def test_steady_state_costs_no_upstream_call(self, state, monkeypatch):
        monkeypatch.setattr(
            compaction,
            "_settings",
            lambda: {**compaction._DEFAULTS, "inline_deadline": 5.0},
        )
        calls = []

        async def counted(*a, **kw):
            calls.append(1)
            return "SUMMARY OF EARLY WORK"

        monkeypatch.setattr(compaction, "_summarize", counted)

        h = self._big(120)
        await maybe_compact(h, state=state, client=None, log=lambda m: None)
        assert len(calls) == 1

        # Several more turns append messages; while the result still fits the
        # budget nothing more is sent upstream.
        for extra in range(1, 6):
            h = h + [{"role": "user", "content": f"new{extra} " + "y" * 2_000}]
            out = await maybe_compact(h, state=state, client=None, log=lambda m: None)
            assert estimate_tokens(out) <= 72_000
        assert len(calls) == 1, f"{len(calls)} summarize calls — boundary advancing per turn"

    @pytest.mark.asyncio
    async def test_keeps_far_more_than_keep_recent_verbatim(self, state, monkeypatch):
        """A big budget must not be collapsed into summary + 8 messages."""
        monkeypatch.setattr(
            compaction,
            "_settings",
            lambda: {**compaction._DEFAULTS, "inline_deadline": 5.0},
        )

        async def ok(*a, **kw):
            return "S"

        monkeypatch.setattr(compaction, "_summarize", ok)
        out = await maybe_compact(
            self._big(200), state=state, client=None, log=lambda m: None
        )
        verbatim = len(out) - len(SYS) - 1
        assert verbatim > compaction._DEFAULTS["keep_recent"] * 2, (
            f"only {verbatim} verbatim messages kept out of a 43k-token target"
        )
        assert estimate_tokens(out) <= 72_000


class TestCompactionTarget:
    @pytest.mark.asyncio
    async def test_result_sits_well_below_the_trigger(self, state, monkeypatch):
        """Compacting exactly to the budget re-triggers on the next turn."""
        monkeypatch.setattr(
            compaction,
            "_settings",
            lambda: {**compaction._DEFAULTS, "inline_deadline": 5.0},
        )

        async def boom(*a, **kw):
            raise RuntimeError("no keys")

        monkeypatch.setattr(compaction, "_summarize", boom)
        big = SYS + [{"role": "user", "content": "q " + "x" * 4_000} for _ in range(200)]
        out = await maybe_compact(big, state=state, client=None, log=lambda m: None)
        budget = 80_000 - 8_000
        assert estimate_tokens(out) <= budget * 0.6 + 1
