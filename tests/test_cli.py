"""CLI surface: what it writes, what it refuses to write, and what it runs.

__main__.py had no tests, and it is the code that edits other tools' config
files. The regressions here are all things it used to do to the user's machine
on every single proxy start.
"""

from __future__ import annotations

import json

import pytest

from openvidia import __main__ as m


@pytest.fixture
def opencode_cfg(tmp_path, monkeypatch):
    """An opencode config the setup path will find."""
    p = tmp_path / "opencode.json"
    monkeypatch.setattr(m.config, "opencode_config_path", lambda: p)
    return p


def write(p, data):
    p.write_text(json.dumps(data, indent=2))


# --------------------------------------------------------------------------- #
# Starting the proxy must not reconfigure anything
# --------------------------------------------------------------------------- #


def test_startup_no_longer_calls_the_setup_helpers():
    """_setup_* ran on every main_async(), not just on `openvidia setup`."""
    import inspect

    src = inspect.getsource(m.main_async)
    for name in ("_setup_opencode", "_setup_codex", "_setup_claude_code", "_setup_grok"):
        assert name not in src, f"{name} is still called at startup"


def test_the_config_rewriting_helpers_are_gone():
    """Codex/Grok/shell-rc writers are replaced by `openvidia run`."""
    for name in ("_setup_codex", "_setup_grok", "_setup_claude_code", "_ensure_env_var"):
        assert not hasattr(m, name), f"{name} still exists"


# --------------------------------------------------------------------------- #
# opencode setup: additive, backed up, no clobber
# --------------------------------------------------------------------------- #


def test_setup_adds_the_provider_to_an_empty_config(opencode_cfg):
    write(opencode_cfg, {})
    assert m._setup_opencode() is True
    cfg = json.loads(opencode_cfg.read_text())
    assert cfg["provider"]["openvidia"]["options"]["baseURL"] == m.BASE_URL
    assert cfg["model"] == "openvidia/openvidia"


def test_setup_does_not_steal_a_model_the_user_chose(opencode_cfg):
    """This reset the user's provider on every proxy launch."""
    write(opencode_cfg, {"model": "anthropic/claude-sonnet-4"})

    m._setup_opencode()

    cfg = json.loads(opencode_cfg.read_text())
    assert cfg["model"] == "anthropic/claude-sonnet-4"


def test_setup_does_not_re_enable_compaction_the_user_turned_off(opencode_cfg):
    write(opencode_cfg, {"compaction": {"auto": False, "prune": False}})

    m._setup_opencode()

    cfg = json.loads(opencode_cfg.read_text())
    assert cfg["compaction"]["auto"] is False
    assert cfg["compaction"]["prune"] is False


def test_setup_does_not_inject_the_cwd_agents_md(opencode_cfg, tmp_path, monkeypatch):
    """Launched from the repo, this pushed OpenVidia's AGENTS.md into the
    GLOBAL opencode instructions, for every project the user had."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("# OpenVidia internal instructions")
    write(opencode_cfg, {})

    m._setup_opencode()

    cfg = json.loads(opencode_cfg.read_text())
    assert "AGENTS.md" not in cfg.get("instructions", [])


def test_setup_backs_up_before_writing(opencode_cfg):
    write(opencode_cfg, {"model": "keep/me"})

    m._setup_opencode()

    backups = list(opencode_cfg.parent.glob("opencode_backup_*.json"))
    assert backups, "no backup was taken before editing a third-party config"
    assert json.loads(backups[0].read_text())["model"] == "keep/me"


def test_setup_is_idempotent(opencode_cfg):
    write(opencode_cfg, {})
    m._setup_opencode()
    first = opencode_cfg.read_text()
    m._setup_opencode()
    assert opencode_cfg.read_text() == first


def test_setup_reports_a_missing_opencode(opencode_cfg, capsys):
    assert m._setup_opencode() is False
    assert "not found" in capsys.readouterr().out


def test_setup_refuses_an_unparseable_config(opencode_cfg):
    opencode_cfg.write_text("{ this is not json")
    assert m._setup_opencode() is False
    assert opencode_cfg.read_text() == "{ this is not json"


def test_setup_drops_a_stale_localhost_nvidia_provider(opencode_cfg):
    write(opencode_cfg, {"provider": {"nvidia": {"options": {"baseURL": "http://localhost:3940"}}}})
    m._setup_opencode()
    cfg = json.loads(opencode_cfg.read_text())
    assert "nvidia" not in cfg["provider"]


def test_setup_keeps_a_real_nvidia_provider(opencode_cfg):
    write(
        opencode_cfg,
        {"provider": {"nvidia": {"options": {"baseURL": "https://integrate.api.nvidia.com/v1"}}}},
    )
    m._setup_opencode()
    cfg = json.loads(opencode_cfg.read_text())
    assert "nvidia" in cfg["provider"]


# --------------------------------------------------------------------------- #
# `openvidia run`
# --------------------------------------------------------------------------- #


def test_known_targets_cover_the_documented_clis():
    assert set(m._cli_targets()) >= {"opencode", "codex", "claude", "grok", "jcode"}


def test_claude_target_uses_the_anthropic_variables():
    env = m._cli_targets()["claude"]["env"]
    assert env["ANTHROPIC_BASE_URL"] == m.ROOT_URL
    assert "ANTHROPIC_API_KEY" in env


def test_openai_compatible_targets_point_at_the_v1_base():
    for name in ("opencode", "codex", "grok", "jcode"):
        assert m._cli_targets()[name]["env"]["OPENAI_BASE_URL"] == m.BASE_URL


def test_targets_are_user_overridable(tmp_path, monkeypatch):
    monkeypatch.setattr(m.config, "config_dir", lambda: tmp_path)
    (tmp_path / "cli_targets.json").write_text(
        json.dumps({"claude": {"env": {"ANTHROPIC_BASE_URL": "http://elsewhere"}}})
    )
    assert m._cli_targets()["claude"]["env"]["ANTHROPIC_BASE_URL"] == "http://elsewhere"


def test_malformed_target_overrides_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(m.config, "config_dir", lambda: tmp_path)
    (tmp_path / "cli_targets.json").write_text("not json")
    assert "claude" in m._cli_targets()


def test_run_without_a_cli_name_explains_itself(capsys):
    assert m._run_cmd([]) == 2
    assert "Usage: openvidia run" in capsys.readouterr().out


def test_run_reports_a_missing_binary(capsys):
    assert m._run_cmd(["definitely-not-installed-xyz"]) == 127
    assert "not on PATH" in capsys.readouterr().out


def test_run_execs_with_the_environment_set(monkeypatch, capsys):
    """The whole point: variables are set for the child, nothing is written."""
    captured = {}

    monkeypatch.setattr(m.shutil, "which", lambda c: f"/usr/bin/{c}")

    def fake_exec(exe, argv, env):
        captured["exe"] = exe
        captured["argv"] = argv
        captured["env"] = env
        raise SystemExit(0)

    monkeypatch.setattr(m.os, "execvpe", fake_exec)

    with pytest.raises(SystemExit):
        m._run_cmd(["claude", "-p", "hello"])

    assert captured["argv"] == ["claude", "-p", "hello"]
    assert captured["env"]["ANTHROPIC_BASE_URL"] == m.ROOT_URL
    # Inherited environment is preserved, not replaced.
    assert "PATH" in captured["env"]


def test_run_passes_unknown_clis_through_with_openai_vars(monkeypatch):
    captured = {}
    monkeypatch.setattr(m.shutil, "which", lambda c: f"/usr/bin/{c}")

    def fake_exec(exe, argv, env):
        captured["argv"] = argv
        captured["env"] = env
        raise SystemExit(0)

    monkeypatch.setattr(m.os, "execvpe", fake_exec)
    with pytest.raises(SystemExit):
        m._run_cmd(["aider", "--model", "x"])

    assert captured["argv"] == ["aider", "--model", "x"]
    assert captured["env"]["OPENAI_BASE_URL"] == m.BASE_URL


def test_run_writes_nothing_to_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(m.config, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(m.shutil, "which", lambda c: f"/usr/bin/{c}")
    monkeypatch.setattr(m.os, "execvpe", lambda *_a: (_ for _ in ()).throw(SystemExit(0)))
    before = sorted(p.name for p in tmp_path.iterdir())

    with pytest.raises(SystemExit):
        m._run_cmd(["codex"])

    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_codex_target_explains_its_provider_block_instead_of_writing_it():
    note = m._cli_targets()["codex"].get("note", "")
    assert "config.toml" in note
    assert "model_providers.openvidia" in note


# --------------------------------------------------------------------------- #
# Jcode setup: additive, backed up, no clobber
# --------------------------------------------------------------------------- #


@pytest.fixture
def jcode_cfg(tmp_path, monkeypatch):
    """A Jcode config dir + path the setup will find."""
    jcode_dir = tmp_path / ".jcode"
    jcode_dir.mkdir()
    p = jcode_dir / "config.toml"
    monkeypatch.setattr(m.config, "jcode_config_path", lambda: p)
    return p


def test_jcode_setup_adds_provider_to_empty_config(jcode_cfg):
    jcode_cfg.write_text("")
    assert m._setup_jcode() is True
    import tomlkit

    doc = tomlkit.parse(jcode_cfg.read_text())
    assert doc["providers"]["openvidia"]["base_url"] == m.BASE_URL
    assert doc["provider"]["default_provider"] == "openvidia"


def test_jcode_setup_does_not_steal_a_provider_the_user_chose(jcode_cfg):
    jcode_cfg.write_text(
        '[provider]\ndefault_provider = "anthropic"\ndefault_model = "claude-sonnet-4"\n'
    )
    m._setup_jcode()
    import tomlkit

    doc = tomlkit.parse(jcode_cfg.read_text())
    assert doc["provider"]["default_provider"] == "anthropic"


def test_jcode_setup_backs_up_before_writing(jcode_cfg):
    jcode_cfg.write_text('[provider]\ndefault_provider = "keep-me"\n')
    m._setup_jcode()
    backups = list(jcode_cfg.parent.glob("config_backup_*.toml"))
    assert backups, "no backup was taken before editing Jcode config"


def test_jcode_setup_is_idempotent(jcode_cfg):
    jcode_cfg.write_text("")
    m._setup_jcode()
    first = jcode_cfg.read_text()
    m._setup_jcode()
    assert jcode_cfg.read_text() == first


def test_jcode_setup_reports_missing_jcode(tmp_path, monkeypatch, capsys):
    """When ~/.jcode doesn't exist, skip gracefully."""
    missing = tmp_path / "no-jcode" / "config.toml"
    monkeypatch.setattr(m.config, "jcode_config_path", lambda: missing)
    assert m._setup_jcode() is False
    assert "not found" in capsys.readouterr().out


def test_jcode_setup_preserves_existing_comments(jcode_cfg):
    jcode_cfg.write_text("# My Jcode config\n[display]\nemoji = true\n")
    m._setup_jcode()
    content = jcode_cfg.read_text()
    assert "# My Jcode config" in content
    assert "emoji = true" in content


def test_jcode_target_uses_openai_compatible_vars():
    env = m._cli_targets()["jcode"]["env"]
    assert env["OPENAI_BASE_URL"] == m.BASE_URL
    assert "OPENAI_API_KEY" in env


def test_known_targets_now_include_jcode():
    assert "jcode" in m._cli_targets()
