"""Shared test isolation.

Several code paths persist state to ``config.config_dir()`` as a side effect of
doing their job — learned context windows, the key file, presets. Without this
fixture a test run writes into the developer's real ~/.config/openvidia, which
is both a dirty test and a way to corrupt a working install.
"""

from __future__ import annotations

import pytest

from openvidia import config


@pytest.fixture(autouse=True)
def isolated_config_dir(tmp_path, monkeypatch):
    """Point every config read/write at a per-test directory."""
    d = tmp_path / "openvidia-config"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "config_dir", lambda: d)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    # compaction caches learned limits in module globals, so a value written by
    # one test would otherwise be visible to the next.
    from openvidia import compaction

    monkeypatch.setattr(compaction, "_learned_limits", {})
    monkeypatch.setattr(compaction, "_learned_loaded", False)
    monkeypatch.setattr(compaction, "_settings_cache", None)
    return d
