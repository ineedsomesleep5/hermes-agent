"""Tests for tools/studio_presets.py — pre-baked widgets + install tool."""

from __future__ import annotations

import importlib
import json
import sys

import pytest


@pytest.fixture
def presets(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sys.modules.pop("tools.studio_widgets", None)
    sys.modules.pop("tools.studio_presets", None)
    import tools.studio_widgets as sw_mod
    importlib.reload(sw_mod)
    import tools.studio_presets as sp_mod
    importlib.reload(sp_mod)
    yield sp_mod, sw_mod


def test_subagent_monitor_preset_exists(presets):
    sp, _ = presets
    assert "subagent_monitor" in sp.PRESETS
    p = sp.PRESETS["subagent_monitor"]
    assert "renderer" in p and isinstance(p["renderer"], str)
    assert "/api/studio/delegations" in p["renderer"]
    assert "X-Hermes-Session-Token" in p["renderer"]


def test_install_preset_creates_widget(presets):
    sp, sw = presets
    out = json.loads(sp.studio_install_preset({"name": "subagent_monitor"}))
    assert out["ok"] is True
    assert out["preset"] == "subagent_monitor"
    rec = sw.get_widget("default", out["id"])
    assert rec is not None
    assert "/api/studio/delegations" in rec["renderer"]


def test_install_preset_uses_default_size(presets):
    sp, sw = presets
    sp.studio_install_preset({"name": "subagent_monitor"})
    rec = sw.get_widget("default", "subagent-monitor")
    assert rec["size"] == sp.PRESETS["subagent_monitor"]["size"]


def test_install_preset_with_custom_id_and_position(presets):
    sp, sw = presets
    sp.studio_install_preset(
        {
            "name": "subagent_monitor",
            "id": "ops",
            "position": {"x": 7, "y": 7},
        }
    )
    rec = sw.get_widget("default", "ops")
    assert rec["position"] == {"x": 7, "y": 7}


def test_install_unknown_preset_returns_error(presets):
    sp, _ = presets
    out = json.loads(sp.studio_install_preset({"name": "nope"}))
    assert "error" in out
    assert "subagent_monitor" in out["error"]  # lists available presets


def test_install_missing_name_returns_error(presets):
    sp, _ = presets
    out = json.loads(sp.studio_install_preset({}))
    assert "error" in out


def test_list_presets(presets):
    sp, _ = presets
    out = json.loads(sp.studio_list_presets({}))
    names = [p["name"] for p in out["presets"]]
    assert "subagent_monitor" in names
    monitor = next(p for p in out["presets"] if p["name"] == "subagent_monitor")
    assert monitor["title"]
    assert monitor["description"]


def test_install_preset_registered_in_registry(presets):
    sp, _ = presets
    from tools.registry import registry

    assert "studio_install_preset" in registry._tools
    assert "studio_list_presets" in registry._tools
    entry = registry._tools["studio_install_preset"]
    assert entry.toolset == "studio"

    out = json.loads(
        entry.handler({"name": "subagent_monitor", "id": "registry-installed"})
    )
    assert out["ok"] is True


def test_studio_install_preset_in_HERMES_CORE_TOOLS():
    from toolsets import _HERMES_CORE_TOOLS, resolve_toolset

    assert "studio_install_preset" in _HERMES_CORE_TOOLS
    assert "studio_list_presets" in _HERMES_CORE_TOOLS

    api_tools = resolve_toolset("hermes-api-server")
    assert "studio_install_preset" in api_tools
    assert "studio_list_presets" in api_tools
