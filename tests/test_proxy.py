"""Tests for OpenVidia proxy rotation, cooldown, and compaction."""

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from openvidia.proxy_state import (
    ProxyState,
    ProxyStats,
    KeyState,
    RpmTracker,
    KeyCooldown,
    MAX_RPM,
    COOLDOWN_DURATIONS,
    ADAPTIVE_COOLDOWN_MAX,
)
from openvidia.compaction import (
    estimate_tokens,
    _split,
    _render,
    _conv_key,
    _fingerprint,
    _trim,
    _settings,
)
from openvidia.compaction import _model_budgets
from openvidia.compaction import _DEFAULTS as _DEFAULTS_KEYS


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_keys():
    """Sample API keys for testing."""
    return [
        "nvapi-key1234567890abcdef1234567890abcd",
        "nvapi-key2234567890abcdef1234567890abcd",
        "nvapi-key3234567890abcdef1234567890abcd",
    ]


@pytest.fixture
def proxy_state(sample_keys):
    """Create a ProxyState instance with sample keys."""
    stats = ProxyStats(current_index=0)
    log_cb = MagicMock()
    index_path = Path("/tmp/test_index.json")
    return ProxyState(
        keys=sample_keys,
        stats=stats,
        index_path=index_path,
        log_cb=log_cb,
        port=3940,
    )


# ─────────────────────────────────────────────────────────────────────
# Test KeyState
# ─────────────────────────────────────────────────────────────────────


class TestKeyState:
    def test_key_state_init(self):
        """Test KeyState initialization."""
        key = "test-key"
        ks = KeyState(key)
        
        assert ks.key == key
        assert ks.is_valid is True
        assert ks.cooldown_until == 0.0
        assert ks.last_error == ""
        assert ks.in_flight == 0
        assert ks.last_success_at == 0.0
        assert ks.last_failure_at == 0.0
        assert ks.consecutive_failures == 0

    def test_key_state_slots(self):
        """Test that KeyState uses __slots__ for memory efficiency."""
        ks = KeyState("test")
        with pytest.raises(AttributeError):
            ks.non_existent_attr = "value"


# ─────────────────────────────────────────────────────────────────────
# Test RpmTracker
# ─────────────────────────────────────────────────────────────────────


class TestRpmTracker:
    def test_rpm_tracker_init(self):
        """Test RpmTracker initialization."""
        tracker = RpmTracker()
        assert len(tracker.timestamps) == 0
        assert tracker.window == 60.0
        assert tracker.max_rpm == 0

    def test_rpm_tracker_record_and_count(self):
        """Test recording requests and counting."""
        tracker = RpmTracker(window=60.0)
        
        assert tracker.count() == 0
        
        tracker.record()
        assert tracker.count() == 1
        
        tracker.record()
        tracker.record()
        assert tracker.count() == 3

    def test_rpm_tracker_prune_old(self):
        """Test that old timestamps are pruned."""
        tracker = RpmTracker(window=1.0)  # 1 second window
        
        tracker.record()
        tracker.record()
        assert tracker.count() == 2
        
        time.sleep(1.1)  # Wait for window to expire
        tracker.record()  # This will prune old entries
        assert tracker.count() == 1

    def test_rpm_tracker_can_send(self):
        """Test can_send respects max_rpm."""
        tracker = RpmTracker(max_rpm=5)
        
        for _ in range(4):
            tracker.record()
        assert tracker.can_send(max_rpm=10) is True
        
        tracker.record()
        assert tracker.can_send(max_rpm=10) is False
        
        # With per-key max_rpm lower than global
        tracker.max_rpm = 3
        assert tracker.can_send(max_rpm=10) is False

    def test_rpm_tracker_adaptive_ceiling(self):
        """Test adaptive RPM ceiling."""
        tracker = RpmTracker(max_rpm=MAX_RPM)
        
        # Initially should use MAX_RPM
        assert tracker.can_send(MAX_RPM) is True
        
        # Simulate 429 - lower the ceiling
        tracker.max_rpm = int(MAX_RPM * 0.5)
        assert tracker.max_rpm < MAX_RPM


# ─────────────────────────────────────────────────────────────────────
# Test KeyCooldown
# ─────────────────────────────────────────────────────────────────────


class TestKeyCooldown:
    def test_cooldown_inactive_by_default(self):
        """Test KeyCooldown is inactive by default."""
        cd = KeyCooldown()
        assert cd.active is False
        assert cd.remaining == 0.0
        assert cd.reason == ""

    def test_cooldown_active_when_set(self):
        """Test KeyCooldown becomes active when set."""
        cd = KeyCooldown(until=time.time() + 10.0, reason="rate-limited")
        
        assert cd.active is True
        assert cd.remaining > 0.0
        assert cd.remaining <= 10.0
        assert cd.reason == "rate-limited"

    def test_cooldown_remaining_property(self):
        """Test remaining time decreases."""
        cd = KeyCooldown(until=time.time() + 2.0)
        
        initial = cd.remaining
        time.sleep(0.5)
        later = cd.remaining
        
        assert later < initial
        assert later > 0.0


# ─────────────────────────────────────────────────────────────────────
# Test ProxyState - Cooldown Management
# ─────────────────────────────────────────────────────────────────────


class TestProxyStateCooldown:
    def test_is_key_on_cooldown_false_initially(self, proxy_state):
        """Test keys are not on cooldown initially."""
        for key in proxy_state.keys:
            assert proxy_state.is_key_on_cooldown(key) is False

    def test_mark_key_failed_sets_cooldown(self, proxy_state):
        """Test marking a key failed sets cooldown."""
        key = proxy_state.keys[0]
        
        proxy_state.mark_key_failed(key, status=429)
        
        assert proxy_state.is_key_on_cooldown(key) is True
        assert "429" in proxy_state.cooldown_reason(key)

    def test_cooldown_duration_by_status(self, proxy_state):
        """Different status codes get different cooldown durations.

        One key per status on purpose. Reusing a single key made this test
        measure the ADAPTIVE multiplier (which keys consecutive_failures, and
        which clear_cooldown does not reset) on top of the base duration, so
        the assertion bounds only held for part of the jitter range — it
        failed roughly one run in five.
        """
        k401, k429, k400 = proxy_state.keys[0], proxy_state.keys[1], proxy_state.keys[2]

        proxy_state.mark_key_failed(k401, status=401)
        assert proxy_state.cooldown_remaining(k401) > 3500  # ~3600s

        # 429: base + up to 30s of per-key jitter, no adaptive multiplier on
        # the first failure.
        proxy_state.mark_key_failed(k429, status=429)
        base_429 = COOLDOWN_DURATIONS[429]
        assert base_429 - 1 < proxy_state.cooldown_remaining(k429) <= base_429 + 30

        proxy_state.mark_key_failed(k400, status=400)
        base_400 = COOLDOWN_DURATIONS[400]
        assert base_400 - 1 < proxy_state.cooldown_remaining(k400) <= base_400

    def test_adaptive_cooldown_grows_with_consecutive_failures(self, proxy_state):
        """Repeat offenders back off harder, up to the cap."""
        first, second = proxy_state.keys[0], proxy_state.keys[1]

        proxy_state.mark_key_failed(first, status=400)
        one_failure = proxy_state.cooldown_remaining(first)

        for _ in range(3):
            proxy_state.mark_key_failed(second, status=400)
        three_failures = proxy_state.cooldown_remaining(second)

        assert three_failures > one_failure
        assert three_failures <= COOLDOWN_DURATIONS[400] * ADAPTIVE_COOLDOWN_MAX

    def test_clear_cooldown(self, proxy_state):
        """Test clearing a cooldown."""
        key = proxy_state.keys[0]
        
        proxy_state.mark_key_failed(key, status=429)
        assert proxy_state.is_key_on_cooldown(key) is True
        
        proxy_state.clear_cooldown(key)
        assert proxy_state.is_key_on_cooldown(key) is False
        assert proxy_state.cooldown_remaining(key) == 0.0

    def test_restore_key(self, proxy_state):
        """Test restoring a key clears cooldown and resets state."""
        key = proxy_state.keys[0]
        
        proxy_state.mark_key_failed(key, status=429)
        ks = proxy_state._key_states[key]
        ks.consecutive_failures = 5
        
        proxy_state.restore_key(key)
        
        assert proxy_state.is_key_on_cooldown(key) is False
        assert ks.consecutive_failures == 0
        assert ks.is_valid is True


# ─────────────────────────────────────────────────────────────────────
# Test ProxyState - RPM Tracking
# ─────────────────────────────────────────────────────────────────────


class TestProxyStateRpm:
    def test_key_rpm_initially_zero(self, proxy_state):
        """Test RPM is zero initially."""
        for key in proxy_state.keys:
            assert proxy_state.key_rpm(key) == 0

    def test_record_request_increments_rpm(self, proxy_state):
        """Test recording requests increments RPM counter."""
        key = proxy_state.keys[0]
        
        proxy_state.record_request(key)
        assert proxy_state.key_rpm(key) == 1
        
        proxy_state.record_request(key)
        proxy_state.record_request(key)
        assert proxy_state.key_rpm(key) == 3

    def test_key_can_send_rpm(self, proxy_state):
        """Test key_can_send_rpm respects limits."""
        key = proxy_state.keys[0]
        
        # Initially can send
        assert proxy_state.key_can_send_rpm(key) is True
        
        # Fill up to MAX_RPM
        for _ in range(MAX_RPM):
            proxy_state.record_request(key)
        
        assert proxy_state.key_can_send_rpm(key) is False

    def test_in_flight_tracking(self, proxy_state):
        """Test in-flight request tracking."""
        key = proxy_state.keys[0]
        
        assert proxy_state._key_states[key].in_flight == 0
        
        proxy_state.begin_in_flight(key)
        assert proxy_state._key_states[key].in_flight == 1
        
        proxy_state.begin_in_flight(key)
        assert proxy_state._key_states[key].in_flight == 2
        
        proxy_state.end_in_flight(key)
        assert proxy_state._key_states[key].in_flight == 1
        
        proxy_state.end_in_flight(key)
        assert proxy_state._key_states[key].in_flight == 0


# ─────────────────────────────────────────────────────────────────────
# Test ProxyState - Key Selection
# ─────────────────────────────────────────────────────────────────────


class TestProxyStateKeySelection:
    def test_get_candidate_keys_returns_all_healthy(self, proxy_state):
        """Test getting candidate keys returns all healthy keys."""
        candidates = proxy_state.get_candidate_keys()
        
        assert len(candidates) == len(proxy_state.keys)
        for idx, key in candidates:
            assert key in proxy_state.keys

    def test_get_candidate_keys_excludes_invalid(self, proxy_state):
        """Test candidate keys excludes invalid keys."""
        key = proxy_state.keys[0]
        proxy_state._key_states[key].is_valid = False
        
        candidates = proxy_state.get_candidate_keys()
        
        assert len(candidates) == len(proxy_state.keys) - 1
        assert key not in [k for _, k in candidates]

    def test_get_candidate_keys_excludes_cooldown(self, proxy_state):
        """Test candidate keys handles keys on cooldown as degraded fallback."""
        key = proxy_state.keys[0]
        proxy_state.mark_key_failed(key, status=429)
        
        candidates = proxy_state.get_candidate_keys()
        
        # get_candidate_keys excludes cooldown keys from main list
        # (they're only used as last-resort fallback in the proxy loop)
        assert len(candidates) == len(proxy_state.keys) - 1
        assert key not in [k for _, k in candidates]
        # But the key is still tracked and on cooldown
        assert proxy_state.is_key_on_cooldown(key) is True

    def test_best_key_index_least_loaded(self, proxy_state):
        """Test best_key_index returns least loaded key."""
        # Make first key busy
        key0 = proxy_state.keys[0]
        proxy_state.begin_in_flight(key0)
        proxy_state.begin_in_flight(key0)
        proxy_state.begin_in_flight(key0)
        
        # Second key is free
        
        best_idx = proxy_state.best_key_index()
        
        # Should prefer key1 (less loaded)
        assert best_idx != 0


# ─────────────────────────────────────────────────────────────────────
# Test ProxyState - Health Check
# ─────────────────────────────────────────────────────────────────────


class TestProxyStateHealth:
    def test_is_key_healthy(self, proxy_state):
        """Test key health status."""
        key = proxy_state.keys[0]
        
        # Initially healthy
        assert proxy_state.is_key_healthy(key) is True
        
        # Mark invalid
        proxy_state._key_states[key].is_valid = False
        assert proxy_state.is_key_healthy(key) is False
        
        # Restore validity but add cooldown
        proxy_state._key_states[key].is_valid = True
        proxy_state.mark_key_failed(key, status=429)
        assert proxy_state.is_key_healthy(key) is False

    def test_consecutive_failures_tracking(self, proxy_state):
        """Test consecutive failures increment."""
        key = proxy_state.keys[0]
        
        assert proxy_state._key_states[key].consecutive_failures == 0
        
        proxy_state.mark_key_failed(key, status=500)
        assert proxy_state._key_states[key].consecutive_failures == 1
        
        proxy_state.mark_key_failed(key, status=500)
        assert proxy_state._key_states[key].consecutive_failures == 2
        
        # Restore resets counter
        proxy_state.restore_key(key)
        assert proxy_state._key_states[key].consecutive_failures == 0


# ─────────────────────────────────────────────────────────────────────
# Test Compaction - Token Estimation
# ─────────────────────────────────────────────────────────────────────


class TestCompactionTokenEstimation:
    def test_estimate_tokens_basic(self):
        """Test basic token estimation."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        
        tokens = estimate_tokens(messages)
        assert tokens > 0
        assert isinstance(tokens, int)

    def test_estimate_tokens_empty(self):
        """Test token estimation with empty messages."""
        assert estimate_tokens([]) == 0

    def test_estimate_tokens_scales_with_size(self):
        """Test token estimation scales with message size."""
        small = [{"role": "user", "content": "Hi"}]
        large = [{"role": "user", "content": "A" * 1000}]
        
        assert estimate_tokens(large) > estimate_tokens(small)


# ─────────────────────────────────────────────────────────────────────
# Test Compaction - Message Splitting
# ─────────────────────────────────────────────────────────────────────


class TestCompactionSplit:
    def test_split_system_messages(self):
        """Test splitting system messages from conversation."""
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "system", "content": "Be concise"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        
        system_block, rest = _split(messages)
        
        assert len(system_block) == 2
        assert len(rest) == 2
        assert all(m["role"] == "system" for m in system_block)
        assert all(m["role"] != "system" for m in rest)

    def test_split_no_system(self):
        """Test splitting when no system messages exist."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        
        system_block, rest = _split(messages)
        
        assert len(system_block) == 0
        assert len(rest) == 2


# ─────────────────────────────────────────────────────────────────────
# Test Compaction - Message Rendering
# ─────────────────────────────────────────────────────────────────────


class TestCompactionRender:
    def test_render_simple_messages(self):
        """Test rendering simple messages."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        
        rendered = _render(messages)
        
        assert "user: Hello" in rendered
        assert "assistant: Hi there" in rendered

    def test_render_with_tool_calls(self):
        """Test rendering messages with tool calls."""
        messages = [
            {
                "role": "assistant",
                "content": "Let me check",
                "tool_calls": [
                    {"function": {"name": "get_weather"}}
                ],
            },
        ]
        
        rendered = _render(messages)
        
        assert "tool_calls: get_weather" in rendered

    def test_render_non_string_content(self):
        """Test rendering non-string content."""
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
        ]
        
        rendered = _render(messages)
        
        assert "user:" in rendered


# ─────────────────────────────────────────────────────────────────────
# Test Compaction - Conversation Key
# ─────────────────────────────────────────────────────────────────────


class TestCompactionConvKey:
    def test_conv_key_stable_for_same_messages(self):
        """Test conversation key is stable for same messages."""
        system = [{"role": "system", "content": "You are helpful"}]
        rest = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        
        key1 = _conv_key(system, rest)
        key2 = _conv_key(system, rest)
        
        assert key1 == key2
        assert len(key1) == 64  # SHA-256 hex digest

    def test_conv_key_different_for_different_messages(self):
        """Test conversation key differs for different messages."""
        system = [{"role": "system", "content": "You are helpful"}]
        rest1 = [{"role": "user", "content": "Hello"}]
        rest2 = [{"role": "user", "content": "Hi there"}]
        
        key1 = _conv_key(system, rest1)
        key2 = _conv_key(system, rest2)
        
        assert key1 != key2


# ─────────────────────────────────────────────────────────────────────
# Test Compaction - Fingerprint
# ─────────────────────────────────────────────────────────────────────


class TestCompactionFingerprint:
    def test_fingerprint_unique_for_different_messages(self):
        """Test fingerprints are unique for different messages."""
        msgs1 = [{"role": "user", "content": "Hello"}]
        msgs2 = [{"role": "user", "content": "Goodbye"}]
        
        fp1 = _fingerprint(msgs1)
        fp2 = _fingerprint(msgs2)
        
        assert fp1 != fp2
        assert len(fp1) == 64  # Full SHA-256

    def test_fingerprint_stable_for_same_messages(self):
        """Test fingerprint is stable for same messages."""
        msgs = [{"role": "user", "content": "Test"}]
        
        fp1 = _fingerprint(msgs)
        fp2 = _fingerprint(msgs)
        
        assert fp1 == fp2


# ─────────────────────────────────────────────────────────────────────
# Test Compaction - Deterministic Trim
# ─────────────────────────────────────────────────────────────────────


class TestCompactionTrim:
    def test_trim_keeps_system_and_recent(self):
        """Test trimming keeps system messages and recent conversation."""
        system = [{"role": "system", "content": "You are helpful"}]
        rest = [
            {"role": "user", "content": f"Message {i}"}
            for i in range(20)
        ]
        
        budget = 500  # Tight budget
        keep_recent = 5
        
        trimmed = _trim(system, rest, budget, keep_recent)
        
        # Should have system + some messages
        assert len(trimmed) > 0
        assert trimmed[0]["role"] == "system"

    def test_trim_fits_within_budget(self):
        """Test trimming respects token budget."""
        system = [{"role": "system", "content": "System prompt"}]
        rest = [
            {"role": "user", "content": "A" * 500}
            for _ in range(10)
        ]
        
        budget = 2000  # More realistic budget
        keep_recent = 2
        
        trimmed = _trim(system, rest, budget, keep_recent)
        
        tokens = estimate_tokens(trimmed)
        # MUST respect the budget strictly — the upstream depends on this
        # guarantee to avoid 400 context overflow.
        assert tokens <= budget

    def test_trim_never_exceeds_budget_pathological_head(self):
        """A single head message larger than budget must not overflow."""
        system = [{"role": "system", "content": "S" * 400_000}]
        rest = [{"role": "user", "content": "Y" * 100} for _ in range(5)]
        trimmed = _trim(system, rest, 92_000, 2)
        assert estimate_tokens(trimmed) <= 92_000

    def test_trim_never_exceeds_budget_pathological_messages(self):
        """Every message larger than budget must not overflow."""
        rest = [{"role": "user", "content": "Z" * 400_000} for _ in range(10)]
        trimmed = _trim([], rest, 92_000, 8)
        assert estimate_tokens(trimmed) <= 92_000

    def test_trim_steamed_state_under_budget(self):
        """Trim steady-state must sit below the trigger to avoid loops."""
        rest = [{"role": "user", "content": "x" * 900} for _ in range(430)]
        trimmed = _trim([], rest, 92_000, 8)
        assert estimate_tokens(trimmed) <= 92_000

    def test_no_hardcoded_model_budget(self):
        """Per-model budgets come from compaction.json, never hardcoded."""
        assert _model_budgets(_settings()) == {} or isinstance(
            _model_budgets(_settings()), dict
        )
        cfg = {**_settings(), "model_budgets": {"z-ai/glm-5.2": 120_000}}
        assert _model_budgets(cfg).get("z-ai/glm-5.2") == 120_000


# ─────────────────────────────────────────────────────────────────────
# Test Compaction - Settings
# ─────────────────────────────────────────────────────────────────────


class TestCompactionSettings:
    def test_settings_defaults(self):
        """Built-in defaults. Asserted on _DEFAULTS, not on _settings(): the
        latter merges the user's compaction.json and is not hermetic."""
        from openvidia.compaction import _DEFAULTS

        assert _DEFAULTS["enabled"] is True
        assert _DEFAULTS["budget_tokens"] == 80_000
        assert _DEFAULTS["keep_recent"] == 8
        assert _DEFAULTS["summary_max_tokens"] == 1024
        assert _DEFAULTS["summary_model"] == ""

    def test_settings_merges_user_overrides_over_defaults(self):
        settings = _settings()

        assert set(_DEFAULTS_KEYS).issubset(settings.keys())
        assert isinstance(settings["budget_tokens"], int)


# ─────────────────────────────────────────────────────────────────────
# Test ProxyState - Stats Tracking
# ─────────────────────────────────────────────────────────────────────


class TestProxyStateStats:
    def test_stats_initialization(self, proxy_state):
        """Test stats are initialized correctly."""
        assert proxy_state.stats.requests == 0
        assert proxy_state.stats.rotations == 0
        assert proxy_state.stats.success == 0

    def test_key_usage_tracking(self, proxy_state):
        """Test key usage statistics are tracked."""
        key = proxy_state.keys[0]
        
        proxy_state.stats.record_key_usage(key, ok=True)
        usage = proxy_state.stats.key_usage[key]
        
        assert usage.requests == 1
        assert usage.success == 1
        assert usage.failed == 0

    def test_key_usage_failure_tracking(self, proxy_state):
        """Test failure tracking in key usage."""
        key = proxy_state.keys[0]
        
        proxy_state.stats.record_key_usage(key, ok=False, error="429")
        usage = proxy_state.stats.key_usage[key]
        
        assert usage.requests == 1
        assert usage.success == 0
        assert usage.failed == 1
        assert usage.last_error == "429"


# ─────────────────────────────────────────────────────────────────────
# Test Edge Cases
# ─────────────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_key_list(self):
        """Test handling empty key list."""
        stats = ProxyStats()
        log_cb = MagicMock()
        index_path = Path("/tmp/test.json")
        
        state = ProxyState(
            keys=[],
            stats=stats,
            index_path=index_path,
            log_cb=log_cb,
        )
        
        candidates = state.get_candidate_keys()
        assert len(candidates) == 0

    def test_single_key(self):
        """Test handling single key."""
        stats = ProxyStats()
        log_cb = MagicMock()
        index_path = Path("/tmp/test.json")
        
        state = ProxyState(
            keys=["single-key"],
            stats=stats,
            index_path=index_path,
            log_cb=log_cb,
        )
        
        candidates = state.get_candidate_keys()
        assert len(candidates) == 1

    def test_rpm_tracker_window_edge(self):
        """Test RPM tracker at window boundary."""
        tracker = RpmTracker(window=0.1)  # Very short window
        
        tracker.record()
        assert tracker.count() == 1
        
        time.sleep(0.15)
        tracker.record()  # Should prune old entry
        
        assert tracker.count() == 1


class TestMarkKeyFailedUnknownKey:
    """mark_key_failed must survive a key that is no longer in _key_states.

    The account manager can swap keys while a request is in flight. An
    AttributeError raised from the error-handling path takes the request down
    with it — the one place that must never throw.
    """

    def test_429_for_unknown_key_does_not_raise(self, proxy_state):
        proxy_state.mark_key_failed("nvapi-not-in-the-pool", status=429)

    def test_other_statuses_for_unknown_key_do_not_raise(self, proxy_state):
        for status in (0, 400, 401, 404, 500):
            proxy_state.mark_key_failed("nvapi-not-in-the-pool", status=status)


class TestKeySpreadUnderConcurrency:
    """N requests arriving together must land on N different keys.

    get_candidate_keys() scores a key by in_flight + recent RPM. Neither is
    set until a request completes, so a caller that does not claim the key
    before sending makes every concurrent request score the whole pool at
    zero, tie-break on index, and pile onto key[0] — a 26-key pool producing
    429s while 25 keys idle.
    """

    def test_concurrent_selection_spreads_across_the_pool(self, sample_keys):
        state = ProxyState(
            keys=sample_keys,
            stats=ProxyStats(current_index=0),
            index_path=Path("/tmp/test_spread_index.json"),
            log_cb=MagicMock(),
            port=3940,
        )
        chosen = []
        for _ in range(len(sample_keys)):
            candidates = state.get_candidate_keys()
            _idx, key = candidates[0]
            state.begin_in_flight(key)  # what the request paths must do
            chosen.append(key)

        assert len(set(chosen)) == len(sample_keys), (
            f"concurrent requests collapsed onto {len(set(chosen))} of "
            f"{len(sample_keys)} keys: {chosen}"
        )

    def test_without_claiming_they_all_pick_the_same_key(self, sample_keys):
        """Documents the failure mode the claim exists to prevent."""
        state = ProxyState(
            keys=sample_keys,
            stats=ProxyStats(current_index=0),
            index_path=Path("/tmp/test_spread_index2.json"),
            log_cb=MagicMock(),
            port=3940,
        )
        chosen = [state.get_candidate_keys()[0][1] for _ in range(len(sample_keys))]
        assert len(set(chosen)) == 1

    def test_released_key_becomes_selectable_again(self, sample_keys):
        state = ProxyState(
            keys=sample_keys,
            stats=ProxyStats(current_index=0),
            index_path=Path("/tmp/test_spread_index3.json"),
            log_cb=MagicMock(),
            port=3940,
        )
        first = state.get_candidate_keys()[0][1]
        state.begin_in_flight(first)
        assert state.get_candidate_keys()[0][1] != first
        state.end_in_flight(first)
        assert state.get_candidate_keys()[0][1] == first


class TestThinkingToggle:
    """The reasoning switch must be config-driven and never override a client."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        from openvidia import config as cfg
        monkeypatch.setattr(cfg, "config_dir", lambda: tmp_path)
        yield

    def test_auto_sends_nothing(self):
        from openvidia import config as cfg
        payload = {"model": "vendor/m", "messages": []}
        assert cfg.apply_model_options(dict(payload)) == payload

    def test_off_injects_the_configured_payload(self):
        from openvidia import config as cfg
        cfg.save_model_options({**cfg._MODEL_OPTIONS_DEFAULTS, "thinking": "off"})
        out = cfg.apply_model_options({"model": "vendor/m", "messages": []})
        assert out["chat_template_kwargs"] == {"thinking": False}

    def test_per_model_beats_the_global_setting(self):
        from openvidia import config as cfg
        cfg.save_model_options({
            **cfg._MODEL_OPTIONS_DEFAULTS,
            "thinking": "off",
            "per_model": {"vendor/keeps-thinking": {"thinking": "on"}},
        })
        out = cfg.apply_model_options({"model": "vendor/keeps-thinking"})
        assert out["chat_template_kwargs"] == {"thinking": True}

    def test_client_choice_is_not_overridden(self):
        from openvidia import config as cfg
        cfg.save_model_options({**cfg._MODEL_OPTIONS_DEFAULTS, "thinking": "off"})
        out = cfg.apply_model_options({
            "model": "vendor/m",
            "chat_template_kwargs": {"thinking": True},
        })
        assert out["chat_template_kwargs"]["thinking"] is True

    def test_the_flag_name_is_configuration_not_code(self):
        """A future model using a different flag needs no release."""
        from openvidia import config as cfg
        cfg.save_model_options({
            **cfg._MODEL_OPTIONS_DEFAULTS,
            "thinking": "off",
            "thinking_off_payload": {"reasoning_effort": "none"},
        })
        out = cfg.apply_model_options({"model": "vendor/future"})
        assert out["reasoning_effort"] == "none"
