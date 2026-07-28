"""The installed package must contain everything the proxy serves.

The dashboard bundle lived outside the package for two releases. Editable
installs hid it — they run from the source tree — so the failure only appeared
in the Arch package and in `pip install openvidia`, where the proxy came up
with no UI and, because /health is registered inside attach_webui, no health
endpoint either.
"""

from __future__ import annotations

from pathlib import Path

import openvidia
from openvidia import __main__ as m

PKG_ROOT = Path(openvidia.__file__).resolve().parent
WEB = PKG_ROOT / "web"

REQUIRED_WEB_FILES = ("index.html", "main.js", "styles.css")


def test_web_bundle_lives_inside_the_package():
    assert WEB.is_dir(), f"{WEB} missing — the dashboard will not ship in a wheel"


def test_the_files_the_dashboard_routes_serve_are_present():
    for name in REQUIRED_WEB_FILES:
        assert (WEB / name).is_file(), f"web/{name} missing"


def test_the_icon_the_installers_reference_is_present():
    """install.sh and the PKGBUILD both copy this path."""
    assert (WEB / "assets" / "logo.png").is_file()


def test_main_resolves_web_dir_inside_the_package():
    """__main__ computes web_dir from its own location; keep them in step."""
    import inspect

    src = inspect.getsource(m.main_async)
    assert 'parent / "web"' in src
    assert 'parent.parent / "web"' not in src, "web_dir still points outside the package"


def test_package_data_declares_the_bundle():
    """A wheel only carries these files if pyproject says so."""
    pyproject = (PKG_ROOT.parent / "pyproject.toml").read_text()
    assert "[tool.setuptools.package-data]" in pyproject
    assert 'openvidia = ["web/*", "web/assets/*"]' in pyproject


def test_health_is_only_available_when_the_bundle_is():
    """Documents the coupling that made the missing bundle so hard to see:
    /health is registered by attach_webui, which is skipped when web_dir does
    not exist — so the packaged proxy failed its own health check."""
    import inspect

    from openvidia import proxy_app, webui

    assert "/health" in inspect.getsource(webui.attach_webui)
    assert "web_dir.exists()" in inspect.getsource(proxy_app.create_app)
