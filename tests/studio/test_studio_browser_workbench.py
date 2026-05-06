"""Tests for the first-class embedded browser workbench."""

from __future__ import annotations

import importlib
import json
import sys

import pytest


@pytest.fixture
def browser_modules(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sys.modules.pop("tools.studio_widgets", None)
    sys.modules.pop("tools.studio_embedded_browser", None)
    sys.modules.pop("tools.studio_browser_workbench", None)
    import tools.studio_widgets as sw_mod
    importlib.reload(sw_mod)
    import tools.studio_embedded_browser as seb_mod
    importlib.reload(seb_mod)
    import tools.studio_browser_workbench as sbw_mod
    importlib.reload(sbw_mod)
    yield sw_mod, seb_mod, sbw_mod


def test_embedded_browser_renderer_includes_tab_and_record_controls(browser_modules):
    _, seb, _ = browser_modules
    renderer = seb.EMBEDDED_BROWSER_RENDERER
    assert "/api/studio/browser/state" in renderer
    assert "/api/studio/browser/tab/new" in renderer
    assert "/api/studio/browser/tab/select" in renderer
    assert "/api/studio/browser/record/start" in renderer
    assert "Recording" in renderer


def test_studio_install_embedded_browser_registers_tool(browser_modules):
    _, _, _ = browser_modules
    from tools.registry import registry

    assert "studio_install_embedded_browser" in registry._tools
    entry = registry._tools["studio_install_embedded_browser"]
    assert entry.toolset == "studio"


def test_studio_install_embedded_browser_creates_widget(browser_modules):
    sw, _, sbw = browser_modules
    out = json.loads(sbw.studio_install_embedded_browser({"space_id": "default"}))
    assert out["ok"] is True
    assert out["id"] == "embedded-browser"
    widget = sw.get_widget("default", "embedded-browser")
    assert widget is not None
    assert widget["title"] == "Embedded Browser"
    assert "/api/studio/browser/tab/new" in widget["renderer"]
    assert "/api/studio/browser/state" in widget["renderer"]


def test_studio_install_embedded_browser_allows_custom_layout(browser_modules):
    sw, _, sbw = browser_modules
    sbw.studio_install_embedded_browser(
        {
            "space_id": "default",
            "id": "browser-lab",
            "title": "Browser Lab",
            "position": {"x": 4, "y": 5},
            "size": {"w": 10, "h": 7},
        }
    )
    widget = sw.get_widget("default", "browser-lab")
    assert widget["position"] == {"x": 4, "y": 5}
    assert widget["size"] == {"w": 10, "h": 7}
    assert widget["title"] == "Browser Lab"


def test_registry_handler_returns_json(browser_modules):
    _, _, _ = browser_modules
    from tools.registry import registry

    handler = registry._tools["studio_install_embedded_browser"].handler
    payload = json.loads(handler({"space_id": "default", "id": "browser-one"}))
    assert payload["ok"] is True
    assert payload["id"] == "browser-one"
    assert payload["space_id"] == "default"
