"""Regression tests for Studio as a dashboard plugin."""

from __future__ import annotations

import json
from pathlib import Path


def test_studio_toolset_resolves_all_registered_tools():
    from toolsets import _HERMES_CORE_TOOLS, resolve_toolset

    expected = {
        "studio_widget_create",
        "studio_widget_update",
        "studio_widget_delete",
        "studio_widget_list",
        "studio_widget_read",
        "studio_install_preset",
        "studio_list_presets",
    }
    assert expected.issubset(set(resolve_toolset("studio")))
    assert expected.issubset(set(_HERMES_CORE_TOOLS))
    assert expected.issubset(set(resolve_toolset("hermes-api-server")))


def test_studio_dashboard_plugin_manifest_and_assets_are_present():
    root = Path(__file__).resolve().parents[2]
    plugin_dir = root / "plugins" / "studio" / "dashboard"
    manifest = json.loads((plugin_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "studio"
    assert manifest["tab"]["path"] == "/studio"
    assert manifest["entry"] == "dist/index.js"
    assert manifest["css"] == "dist/style.css"
    assert manifest["api"] == "plugin_api.py"

    bundle = (plugin_dir / manifest["entry"]).read_text(encoding="utf-8")
    assert '__HERMES_PLUGINS__.register("studio"' in bundle
    assert 'src: "/studio-app"' in bundle
    assert (plugin_dir / manifest["css"]).exists()
    assert (plugin_dir / manifest["api"]).exists()
