"""Tests for tools/studio_widget_tools.py — registry-side widget tools.

These tools are what the *main* Hermes agent uses to mutate the Studio
canvas (Telegram, dashboard /chat, web, etc.). The handlers wrap
``tools.studio_widgets`` with JSON-string in / JSON-string out, mirroring
``delegate_background.py``'s pattern.
"""

from __future__ import annotations

import importlib
import json
import sys

import pytest


@pytest.fixture
def tools_mod(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Reset the studio_widgets module so STUDIO_DIR points at the tmp HOME.
    sys.modules.pop("tools.studio_widgets", None)
    sys.modules.pop("tools.studio_widget_tools", None)
    import tools.studio_widgets as sw_mod
    importlib.reload(sw_mod)
    import tools.studio_widget_tools as swt_mod
    importlib.reload(swt_mod)
    yield swt_mod, sw_mod


# ---------------------------------------------------------------------------
# Handler smoke tests (call functions directly, not via registry)
# ---------------------------------------------------------------------------

def test_create_handler_writes_widget(tools_mod):
    swt, sw = tools_mod
    out = json.loads(
        swt.studio_widget_create(
            {
                "id": "hello",
                "title": "Hello",
                "renderer": "async (p) => { p.textContent = 'hi' }",
            }
        )
    )
    assert out["ok"] is True
    assert out["id"] == "hello"
    rec = sw.get_widget("default", "hello")
    assert rec["title"] == "Hello"


def test_create_handler_missing_field_returns_error(tools_mod):
    swt, _ = tools_mod
    out = json.loads(swt.studio_widget_create({"id": "x"}))
    assert "error" in out


def test_create_handler_passes_layout(tools_mod):
    swt, sw = tools_mod
    swt.studio_widget_create(
        {
            "id": "p",
            "title": "P",
            "renderer": "async()=>{}",
            "position": {"x": 3, "y": 4},
            "size": {"w": 5, "h": 2},
        }
    )
    rec = sw.get_widget("default", "p")
    assert rec["position"] == {"x": 3, "y": 4}
    assert rec["size"] == {"w": 5, "h": 2}


def test_update_handler_preserves_position(tools_mod):
    swt, sw = tools_mod
    swt.studio_widget_create(
        {
            "id": "u",
            "title": "u",
            "renderer": "async()=>{1}",
            "position": {"x": 7, "y": 7},
        }
    )
    swt.studio_widget_update({"id": "u", "renderer": "async()=>{2}"})
    rec = sw.get_widget("default", "u")
    assert "2" in rec["renderer"]
    assert rec["position"] == {"x": 7, "y": 7}


def test_update_unknown_widget_returns_error(tools_mod):
    swt, _ = tools_mod
    out = json.loads(swt.studio_widget_update({"id": "ghost", "title": "x"}))
    assert "error" in out


def test_list_handler_returns_compact_view(tools_mod):
    swt, _ = tools_mod
    swt.studio_widget_create(
        {"id": "a", "title": "A", "renderer": "async()=>{}"}
    )
    swt.studio_widget_create(
        {"id": "b", "title": "B", "renderer": "async()=>{}"}
    )
    out = json.loads(swt.studio_widget_list({}))
    assert out["space_id"] == "default"
    ids = {w["id"] for w in out["widgets"]}
    assert ids == {"a", "b"}
    # Compact view — no renderer source.
    assert all("renderer" not in w for w in out["widgets"])


def test_read_handler_returns_full_record(tools_mod):
    swt, _ = tools_mod
    swt.studio_widget_create(
        {
            "id": "r",
            "title": "R",
            "renderer": "async()=>{42}",
        }
    )
    out = json.loads(swt.studio_widget_read({"id": "r"}))
    assert out["id"] == "r"
    assert "42" in out["renderer"]


def test_read_unknown_returns_error(tools_mod):
    swt, _ = tools_mod
    out = json.loads(swt.studio_widget_read({"id": "ghost"}))
    assert "error" in out


def test_delete_handler_removes_widget(tools_mod):
    swt, sw = tools_mod
    swt.studio_widget_create(
        {"id": "d", "title": "d", "renderer": "async()=>{}"}
    )
    out = json.loads(swt.studio_widget_delete({"id": "d"}))
    assert out["ok"] is True
    assert sw.get_widget("default", "d") is None


def test_delete_unknown_returns_ok_false(tools_mod):
    swt, _ = tools_mod
    out = json.loads(swt.studio_widget_delete({"id": "ghost"}))
    assert out["ok"] is False


# ---------------------------------------------------------------------------
# Registry integration — discoverable + handler matches
# ---------------------------------------------------------------------------

def test_tools_are_registered(tools_mod):
    _swt, _sw = tools_mod
    from tools.registry import registry

    for name in (
        "studio_widget_create",
        "studio_widget_update",
        "studio_widget_delete",
        "studio_widget_list",
        "studio_widget_read",
    ):
        assert name in registry._tools, f"{name} not registered"
        entry = registry._tools[name]
        assert entry.toolset == "studio"
        assert entry.schema["name"] == name


def test_registry_handler_creates_widget(tools_mod):
    _swt, sw = tools_mod
    from tools.registry import registry

    entry = registry._tools["studio_widget_create"]
    out = json.loads(
        entry.handler(
            {
                "id": "viaregistry",
                "title": "RG",
                "renderer": "async()=>{}",
            }
        )
    )
    assert out["ok"] is True
    assert sw.get_widget("default", "viaregistry") is not None


def test_studio_toolset_in_TOOLSETS():
    """The 'studio' toolset is defined and contains all studio_* tool names."""
    from toolsets import TOOLSETS

    assert "studio" in TOOLSETS
    studio_tools = set(TOOLSETS["studio"]["tools"])
    # The 5 widget CRUD tools must be present.
    assert {
        "studio_widget_create",
        "studio_widget_update",
        "studio_widget_delete",
        "studio_widget_list",
        "studio_widget_read",
    }.issubset(studio_tools)
