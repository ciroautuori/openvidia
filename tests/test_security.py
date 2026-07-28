"""Security regressions: key exposure, origin guard, input validation, file modes.

Every test here maps to a specific way the proxy used to hand out — or fail to
protect — the user's NVIDIA keys. They are written against the real ASGI app
rather than the helpers, because the bugs lived in how the pieces were wired
together, not in any single function.
"""

from __future__ import annotations

import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

from openvidia import config
from openvidia.proxy_app import create_app
from openvidia.proxy_state import ProxyState, ProxyStats

KEY_A = "nvapi-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
KEY_B = "nvapi-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A live app with a two-key pool and an isolated config directory."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # config_dir() is resolved per call, so the env var above is enough on
    # Linux; on other platforms pin the whole directory.
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)

    state = ProxyState(
        keys=[KEY_A, KEY_B],
        stats=ProxyStats(),
        index_path=tmp_path / "index",
        log_cb=lambda _m: None,
    )
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("<h1>ok</h1>")
    app = create_app(state, web_dir=web_dir)
    # base_url drives the Host header. The default ("testserver") is exactly
    # what the rebinding guard is there to reject, so the happy-path client
    # has to look like a real loopback caller.
    with TestClient(app, base_url="http://localhost:1919") as c:
        c.state = state
        yield c


# --------------------------------------------------------------------------- #
# Key exposure
# --------------------------------------------------------------------------- #


def test_api_keys_never_returns_cleartext(client):
    """GET /api/keys used to hand back the full pool to anything that asked."""
    r = client.get("/api/keys")
    assert r.status_code == 200
    body = r.text
    assert KEY_A not in body
    assert KEY_B not in body
    assert r.json()["keys"] == [config.mask_key(KEY_A), config.mask_key(KEY_B)]


def test_mask_key_keeps_enough_to_disambiguate_and_no_more():
    masked = config.mask_key(KEY_A)
    assert masked.startswith(KEY_A[:5])
    assert masked.endswith(KEY_A[-4:])
    assert len(masked) < len(KEY_A)


def test_reveal_returns_one_key_by_index(client):
    r = client.post("/api/keys/reveal", json={"index": 1})
    assert r.json() == {"ok": True, "key": KEY_B}


@pytest.mark.parametrize("payload", [{}, {"index": "1"}, {"index": 99}, {"index": -1}])
def test_reveal_rejects_bad_index(client, payload):
    assert client.post("/api/keys/reveal", json=payload).json()["ok"] is False


def test_full_overwrite_route_is_gone(client):
    """POST /api/keys replaced the entire pool from an unauthenticated body."""
    r = client.post("/api/keys", json={"keys": ["attacker-key"]})
    assert r.status_code == 405
    assert client.state.keys == [KEY_A, KEY_B]


# --------------------------------------------------------------------------- #
# Origin / Host guard
# --------------------------------------------------------------------------- #


def test_control_routes_reject_foreign_origin(client):
    r = client.get("/api/keys", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_control_routes_allow_the_dashboards_own_origin(client):
    r = client.get("/api/keys", headers={"Origin": "http://localhost:1919"})
    assert r.status_code == 200


def test_control_routes_allow_clients_that_send_no_origin(client):
    """curl and the CLIs send no Origin; only browsers do."""
    assert client.get("/api/keys").status_code == 200


def test_destructive_routes_reject_foreign_origin(client):
    for path, payload in (
        ("/api/keys/remove", {"index": 0}),
        ("/api/keys/add", {"key": "nvapi-c" * 5}),
        ("/api/keys/reveal", {"index": 0}),
        ("/api/restart", {}),
    ):
        r = client.post(path, json=payload, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403, path
    assert client.state.keys == [KEY_A, KEY_B]


def test_rebound_host_is_rejected(client):
    """A public name resolving to 127.0.0.1 must not reach the control plane."""
    r = client.get("/api/keys", headers={"Host": "evil.example"})
    assert r.status_code == 403


def test_cors_does_not_answer_wildcard(client):
    r = client.get("/api/keys", headers={"Origin": "http://localhost:1919"})
    assert r.headers.get("access-control-allow-origin") != "*"


# --------------------------------------------------------------------------- #
# Stored-XSS input validation
# --------------------------------------------------------------------------- #


def test_active_model_rejects_markup(client):
    payload = {"model": "</code><img src=x onerror=alert(1)>"}
    r = client.post("/api/model", json=payload)
    assert r.json()["ok"] is False
    assert client.state.active_model in (None, "")


def test_active_model_accepts_a_real_id(client):
    r = client.post("/api/model", json={"model": "deepseek-ai/deepseek-v4-flash"})
    assert r.json()["ok"] is True
    assert client.state.active_model == "deepseek-ai/deepseek-v4-flash"


def test_presets_reject_markup(client):
    r = client.post("/api/presets", json={"presets": ["ok/model", "<script>"]})
    assert r.json()["ok"] is False


@pytest.mark.parametrize(
    "model,valid",
    [
        ("deepseek-ai/deepseek-v4-flash", True),
        ("meta/llama-3.1-8b-instruct", True),
        ("z-ai/glm-5.2", True),
        ("<script>alert(1)</script>", False),
        ("", False),
        ("a b", False),
        ("/leading-slash", False),
        ("x" * 200, False),
    ],
)
def test_model_id_validation(model, valid):
    assert config.is_valid_model_id(model) is valid


# --------------------------------------------------------------------------- #
# Key add/remove semantics
# --------------------------------------------------------------------------- #


def test_add_rejects_duplicate(client):
    r = client.post("/api/keys/add", json={"key": KEY_A})
    assert r.json()["ok"] is False
    assert client.state.keys == [KEY_A, KEY_B]


def test_add_then_remove_by_index(client):
    added = client.post("/api/keys/add", json={"key": "nvapi-ccccccccccccccccc"})
    assert added.json()["ok"] is True
    assert len(client.state.keys) == 3
    removed = client.post("/api/keys/remove", json={"index": 0})
    assert removed.json()["ok"] is True
    assert client.state.keys == [KEY_B, "nvapi-ccccccccccccccccc"]
    # The response describes the pool without exposing it.
    assert KEY_B not in removed.text


@pytest.mark.parametrize("payload", [{}, {"key": 42}, {"key": "   "}])
def test_add_rejects_junk(client, payload):
    assert client.post("/api/keys/add", json=payload).json()["ok"] is False


# --------------------------------------------------------------------------- #
# On-disk permissions
# --------------------------------------------------------------------------- #

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")


@posix_only
def test_saved_keys_are_not_world_readable(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    config.save_keys_file([KEY_A], create_backup=False)
    mode = (tmp_path / "keys.json").stat().st_mode
    assert mode & 0o077 == 0, oct(mode)


@posix_only
def test_atomic_write_leaves_no_temp_file_behind(tmp_path):
    d = tmp_path / "writes"
    d.mkdir()
    target = d / "out.json"
    config.atomic_write(target, json.dumps({"a": 1}))
    assert target.read_text() == '{"a": 1}'
    assert [p.name for p in d.iterdir()] == ["out.json"]


@posix_only
def test_atomic_write_overwrites_existing(tmp_path):
    """Path.rename raises on Windows when the target exists; os.replace does not."""
    target = tmp_path / "out.json"
    config.atomic_write(target, "first")
    config.atomic_write(target, "second")
    assert target.read_text() == "second"


@posix_only
def test_harden_repairs_files_an_older_version_left_open(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps([KEY_A]))
    os.chmod(keys, 0o644)
    backup = tmp_path / "keys_backup_20260101_000000.json"
    backup.write_text(json.dumps([KEY_A]))
    os.chmod(backup, 0o644)

    fixed = config.harden_config_permissions()

    assert set(fixed) == {"keys.json", "keys_backup_20260101_000000.json"}
    assert keys.stat().st_mode & 0o077 == 0
    assert backup.stat().st_mode & 0o077 == 0


# --------------------------------------------------------------------------- #
# Config loading is defensive about shape
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "content,expected",
    [
        ('["nvapi-one", "nvapi-two"]', ["nvapi-one", "nvapi-two"]),
        ('{"keys": ["nvapi-one"]}', []),  # dict would have iterated its keys
        ("[1, 2, 3]", []),  # ints became "Bearer 1" and 401-looped
        ('["nvapi-one", "", 7]', ["nvapi-one"]),
        ("not json", []),
    ],
)
def test_load_keys_rejects_wrong_shapes(tmp_path, monkeypatch, content, expected):
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    (tmp_path / "keys.json").write_text(content)
    assert config.load_saved_keys_file() == expected
