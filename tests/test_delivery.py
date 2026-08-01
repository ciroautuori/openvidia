"""Tests for the full-delivery features.

Covers the adaptive RPM ceiling learned from Retry-After, the per-key x
per-model scheduling score, the multi-endpoint router, the embedding cache,
the optional Redis sync and the free-tier provider fallback.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from openvidia.embedding_cache import EmbeddingCache
from openvidia.provider_fallback import (
    ProviderConfig,
    load_provider_configs,
    rewrite_payload,
    try_fallback,
)
from openvidia.proxy_state import ProxyState, ProxyStats
from openvidia.redis_sync import RedisSync
from openvidia.upstream_router import EndpointRouter


def _state(keys: list[str]) -> ProxyState:
    return ProxyState(keys, ProxyStats(), log_cb=lambda _m: None)


# ── 5. Embedding cache ────────────────────────────────────────────────


def test_embedding_cache_hit_miss_and_shape_sensitivity() -> None:
    cache = EmbeddingCache(max_entries=2, ttl_s=300.0)
    assert cache.get("m", "hi") is None
    assert cache.misses == 1
    cache.set("m", "hi", b"body")
    assert cache.get("m", "hi") == b"body"
    assert cache.hits == 1
    # L'ordine e la forma dell'input contano: una stringa non è una lista.
    assert cache.get("m", ["hi"]) is None


def test_embedding_cache_ttl_and_prune() -> None:
    expired = EmbeddingCache(ttl_s=-1.0)
    expired.set("m", "hi", b"x")
    assert expired.get("m", "hi") is None  # già scaduto al primo accesso

    small = EmbeddingCache(max_entries=2, ttl_s=300.0)
    for i in range(3):
        small.set("m", str(i), b"x")
    assert small.as_dict()["entries"] <= 2


# ── 3. Multi-endpoint router ──────────────────────────────────────────


def test_endpoint_router_blacklists_recovers_and_normalizes() -> None:
    router = EndpointRouter(["https://a.example/v1", "https://b.example/v1/"])
    assert router.endpoints == ["https://a.example/v1/", "https://b.example/v1/"]
    assert len(router.healthy_endpoints()) == 2
    router.mark_failure("https://a.example/v1/models")
    assert router.healthy_endpoints() == ["https://b.example/v1/"]
    info = router.as_dict()["endpoints"][0]
    assert info["healthy"] is False
    assert info["retry_in_s"] > 0
    router.mark_success("https://a.example/v1/chat/completions")
    assert len(router.healthy_endpoints()) == 2


# ── 2. Adaptive RPM ceiling ───────────────────────────────────────────


def test_rpm_tracker_learns_ceiling_from_retry_after() -> None:
    from openvidia.proxy_state import RpmTracker

    tracker = RpmTracker()
    tracker.learn_from_retry_after(30.0)
    assert tracker.observed_ceiling == 2
    tracker.record()
    tracker.record()
    assert not tracker.can_send()  # 2 RPM serviti, tetto raggiunto
    tracker.learn_from_retry_after(10.0)  # meno restrittivo: non peggiora
    assert tracker.observed_ceiling == 2


def test_mark_key_failed_429_learns_observed_ceiling() -> None:
    state = _state(["k1"])
    state.mark_key_failed("k1", status=429, retry_after="30")
    tracker = state.rpm["k1"]
    assert tracker.observed_ceiling == 2
    assert tracker.max_rpm == 14  # 28 * ADAPTIVE_429_FACTOR, floor incluso
    assert state.is_key_on_cooldown("k1")


# ── 1. Per-key x per-model score ──────────────────────────────────────


def test_key_model_score_reorders_candidates_by_model_record() -> None:
    state = _state(["k1", "k2"])
    state.begin_in_flight("k1")
    state.begin_in_flight("k2")
    for _ in range(3):
        state.record_key_model_result("k1", "m", ok=False)
    state.record_key_model_result("k2", "m", ok=True, ttft=0.1)
    assert state.key_model_score("k2", "m") > state.key_model_score("k1", "m")
    # A parità di carico, senza modello vince l'indice; con il modello la
    # chiave con record migliore su quel modello passa avanti.
    assert state.get_candidate_keys()[0][1] == "k1"
    assert state.get_candidate_keys("m")[0][1] == "k2"


def test_record_key_model_result_guards_empty_input() -> None:
    state = _state([])
    state.record_key_model_result("", "m", ok=True)
    state.record_key_model_result("k", "", ok=True)
    assert state.key_model_health == {}


# ── 6. Provider fallback ──────────────────────────────────────────────


def test_load_provider_configs_skips_unresolved_keys(isolated_config_dir, monkeypatch) -> None:
    (isolated_config_dir / "providers.json").write_text(
        json.dumps(
            [
                {
                    "name": "deepseek",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key_env": "DEEPSEEK_KEY",
                    "models": {"nvidia/x": "deepseek-chat"},
                },
                {
                    "name": "noenv",
                    "base_url": "https://x.example/v1",
                    "api_key_env": "MISSING_KEY",
                    "models": {},
                },
                {"name": 1, "base_url": "https://y.example/v1", "api_key_env": "K", "models": {}},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("DEEPSEEK_KEY", raising=False)
    assert load_provider_configs() == []
    monkeypatch.setenv("DEEPSEEK_KEY", "sk-123")
    providers = load_provider_configs()
    assert len(providers) == 1
    assert providers[0].name == "deepseek"
    assert providers[0].api_key == "sk-123"


def test_load_provider_configs_tolerates_garbage(isolated_config_dir) -> None:
    (isolated_config_dir / "providers.json").write_text("{not json", encoding="utf-8")
    assert load_provider_configs() == []
    (isolated_config_dir / "providers.json").write_text('{"not": "a list"}', encoding="utf-8")
    assert load_provider_configs() == []


@pytest.mark.asyncio
async def test_try_fallback_rewrites_model_and_fails_quietly() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["payload"] = json.loads(request.content)
        if request.url.host == "dead.example":
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    provider = ProviderConfig("p", "https://api.example/v1", "sk", {"nvidia/x": "glm-5"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        resp = await try_fallback(client, {"model": "nvidia/x", "messages": []}, provider)
        assert resp is not None and resp.status_code == 200
        assert seen["payload"]["model"] == "glm-5"
        assert seen["url"] == "https://api.example/v1/chat/completions"

        dead = ProviderConfig("d", "https://dead.example/v1", "sk", {})
        assert await try_fallback(client, {"model": "x"}, dead) is None
        # Modello non mappato: si usa il primo della mappa.
        assert rewrite_payload({"model": "other"}, provider)["model"] == "glm-5"
        # Nessuna mappa: payload invariato.
        assert rewrite_payload({"model": "x"}, dead)["model"] == "x"
    finally:
        await client.aclose()


# ── 4. Redis sync ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redis_sync_disabled_without_extra() -> None:
    # Il venv di test non ha redis installato: tutto deve degradare senza errori.
    rs = RedisSync("redis://localhost:6379")
    try:
        assert not rs.enabled
        rs.broadcast({"kind": "cooldown"})  # no-op, non deve sollevare
        await rs.start()  # no-op
    finally:
        await rs.close()
    assert RedisSync("").enabled is False


def test_apply_remote_event_applies_without_reemit() -> None:
    state = _state(["k1"])
    state.apply_remote_event(
        {
            "kind": "cooldown",
            "key": "k1",
            "until": time.time() + 60,
            "reason": "r",
            "status": 429,
        }
    )
    assert state.is_key_on_cooldown("k1")
    # Un cooldown già scaduto non deve sovrascrivere quello attivo.
    state.apply_remote_event(
        {"kind": "cooldown", "key": "k1", "until": time.time() - 5, "reason": "r"}
    )
    assert state.is_key_on_cooldown("k1")

    state.apply_remote_event({"kind": "key_invalid", "key": "k1"})
    assert not state.key_states["k1"].is_valid

    state.apply_remote_event({"kind": "pool_throttle", "until": time.time() + 30})
    assert state.is_pool_throttled()

    state.apply_remote_event({"kind": "model_circuit", "model": "m", "opened": True})
    assert state.is_model_circuit_open("m")
    state.apply_remote_event({"kind": "model_circuit", "model": "m", "opened": False})
    assert not state.is_model_circuit_open("m")

    state.apply_remote_event({"kind": "weird"})  # ignorato senza eccezioni
