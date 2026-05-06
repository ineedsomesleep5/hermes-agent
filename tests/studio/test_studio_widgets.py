"""Tests for tools/studio_widgets.py — space/widget storage + live pub/sub.

The Studio canvas is the source of truth for what's on screen at any moment.
Widgets are stored as YAML files at:

    $HERMES_HOME/studio/spaces/<space_id>/widgets/<widget_id>.yaml

with the renderer (an async JS function body, as plain text) inline. A
``space.yaml`` sibling holds the per-space metadata + layout. Mutations
publish events to a per-space subscriber registry so the SSE route can
stream changes to the browser canvas.
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sw(monkeypatch, tmp_path):
    """Clean, isolated studio_widgets module per test."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sys.modules.pop("tools.studio_widgets", None)
    import tools.studio_widgets as mod
    importlib.reload(mod)
    yield mod


# ---------------------------------------------------------------------------
# Spaces
# ---------------------------------------------------------------------------

class TestSpaces:
    def test_default_space_is_auto_created(self, sw):
        spaces = sw.list_spaces()
        assert any(s["id"] == "default" for s in spaces)

    def test_create_space_persists_and_returns_record(self, sw, tmp_path):
        s = sw.create_space("crypto", title="Crypto Dashboard")
        assert s["id"] == "crypto"
        assert s["title"] == "Crypto Dashboard"
        assert s["widget_order"] == []
        # Persisted to disk?
        space_yaml = tmp_path / "studio" / "spaces" / "crypto" / "space.yaml"
        assert space_yaml.exists()

    def test_create_duplicate_space_raises(self, sw):
        sw.create_space("twin")
        with pytest.raises(ValueError, match="exists"):
            sw.create_space("twin")

    def test_invalid_space_id_rejected(self, sw):
        for bad in ["", "../etc", "with spaces", "name/with/slashes", "."]:
            with pytest.raises(ValueError):
                sw.create_space(bad)

    def test_get_space_returns_widget_list(self, sw):
        sw.create_space("with-widgets")
        sw.upsert_widget("with-widgets", "w1", title="A", renderer="async()=>{}")
        sw.upsert_widget("with-widgets", "w2", title="B", renderer="async()=>{}")
        s = sw.get_space("with-widgets")
        assert s["id"] == "with-widgets"
        ids = {w["id"] for w in s["widgets"]}
        assert ids == {"w1", "w2"}

    def test_get_unknown_space_returns_none(self, sw):
        assert sw.get_space("nope") is None

    def test_delete_space_removes_directory(self, sw, tmp_path):
        sw.create_space("trash")
        sw.upsert_widget("trash", "w", title="x", renderer="async()=>{}")
        assert sw.delete_space("trash") is True
        assert not (tmp_path / "studio" / "spaces" / "trash").exists()
        assert sw.get_space("trash") is None

    def test_cannot_delete_default_space(self, sw):
        with pytest.raises(ValueError, match="default"):
            sw.delete_space("default")


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class TestWidgets:
    def test_upsert_creates_widget_with_defaults(self, sw):
        w = sw.upsert_widget(
            "default", "hello",
            title="Hello", renderer="async (p) => { p.textContent = 'hi' }"
        )
        assert w["id"] == "hello"
        assert w["title"] == "Hello"
        assert w["renderer"].startswith("async")
        assert w["position"] == {"x": 0, "y": 0}
        assert w["size"] == {"w": 6, "h": 4}
        assert "created_at" in w
        assert "updated_at" in w

    def test_upsert_with_explicit_position_size(self, sw):
        w = sw.upsert_widget(
            "default", "p",
            title="Positioned", renderer="async()=>{}",
            position={"x": 4, "y": 2},
            size={"w": 8, "h": 6},
        )
        assert w["position"] == {"x": 4, "y": 2}
        assert w["size"] == {"w": 8, "h": 6}

    def test_upsert_existing_widget_preserves_created_at(self, sw):
        a = sw.upsert_widget("default", "k", title="v1", renderer="async()=>{1}")
        time.sleep(0.01)
        b = sw.upsert_widget("default", "k", title="v2", renderer="async()=>{2}")
        assert a["created_at"] == b["created_at"]
        assert b["updated_at"] >= a["updated_at"]
        assert b["title"] == "v2"
        assert "2" in b["renderer"]

    def test_upsert_appends_to_widget_order(self, sw):
        sw.upsert_widget("default", "a", title="A", renderer="async()=>{}")
        sw.upsert_widget("default", "b", title="B", renderer="async()=>{}")
        sw.upsert_widget("default", "c", title="C", renderer="async()=>{}")
        s = sw.get_space("default")
        assert s["widget_order"] == ["a", "b", "c"]
        # Re-upserting an existing one must NOT duplicate it in the order
        sw.upsert_widget("default", "b", title="B prime", renderer="async()=>{}")
        s = sw.get_space("default")
        assert s["widget_order"] == ["a", "b", "c"]

    def test_get_space_prunes_stale_widget_order_ids(self, sw, tmp_path):
        sw.upsert_widget("default", "live-a", title="A", renderer="async()=>{}")
        sw.upsert_widget("default", "live-b", title="B", renderer="async()=>{}")
        space_yaml = tmp_path / "studio" / "spaces" / "default" / "space.yaml"
        data = yaml.safe_load(space_yaml.read_text(encoding="utf-8"))
        data["widget_order"] = ["ghost", "live-b", "old-test", "live-a"]
        space_yaml.write_text(yaml.safe_dump(data), encoding="utf-8")

        s = sw.get_space("default")

        assert s["widget_order"] == ["live-b", "live-a"]
        saved = yaml.safe_load(space_yaml.read_text(encoding="utf-8"))
        assert saved["widget_order"] == ["live-b", "live-a"]

    def test_invalid_widget_id_rejected(self, sw):
        for bad in ["", "../escape", "a/b", "."]:
            with pytest.raises(ValueError):
                sw.upsert_widget("default", bad, title="x", renderer="async()=>{}")

    def test_get_widget(self, sw):
        sw.upsert_widget("default", "g", title="Get", renderer="async()=>{}")
        w = sw.get_widget("default", "g")
        assert w is not None
        assert w["title"] == "Get"
        assert sw.get_widget("default", "missing") is None
        assert sw.get_widget("missing-space", "g") is None

    def test_list_widgets(self, sw):
        sw.upsert_widget("default", "a", title="A", renderer="async()=>{}")
        sw.upsert_widget("default", "b", title="B", renderer="async()=>{}")
        widgets = sw.list_widgets("default")
        assert {w["id"] for w in widgets} == {"a", "b"}

    def test_delete_widget(self, sw, tmp_path):
        sw.upsert_widget("default", "doomed", title="x", renderer="async()=>{}")
        ok = sw.delete_widget("default", "doomed")
        assert ok is True
        assert sw.get_widget("default", "doomed") is None
        # Order also cleaned
        s = sw.get_space("default")
        assert "doomed" not in s["widget_order"]
        # File gone
        assert not (
            tmp_path / "studio" / "spaces" / "default" / "widgets" / "doomed.yaml"
        ).exists()

    def test_delete_unknown_widget_returns_false(self, sw):
        assert sw.delete_widget("default", "ghost") is False

    def test_set_layout_updates_position_and_size(self, sw):
        sw.upsert_widget("default", "l", title="x", renderer="async()=>{}")
        w = sw.set_layout(
            "default", "l", position={"x": 3, "y": 5}, size={"w": 10, "h": 8}
        )
        assert w["position"] == {"x": 3, "y": 5}
        assert w["size"] == {"w": 10, "h": 8}
        # Re-read confirms persistence
        again = sw.get_widget("default", "l")
        assert again["position"] == {"x": 3, "y": 5}

    def test_set_layout_unknown_widget_returns_none(self, sw):
        assert sw.set_layout("default", "ghost", position={"x": 0, "y": 0}) is None


# ---------------------------------------------------------------------------
# Pub/sub for SSE
# ---------------------------------------------------------------------------

class TestSubscribe:
    def test_subscribe_yields_upsert_then_delete(self, sw):
        events = []
        sub = sw.subscribe("default", timeout=2)

        # Drain the optional snapshot prelude (replay of existing widgets).
        # Then start mutating in a parallel thread so the live stream
        # picks them up.
        import threading
        def mutate():
            time.sleep(0.05)
            sw.upsert_widget("default", "live", title="t", renderer="async()=>{}")
            sw.delete_widget("default", "live")

        threading.Thread(target=mutate, daemon=True).start()

        for ev in sub:
            events.append(ev)
            if len(events) >= 6:
                break
            if ev.get("type") == "widget.deleted" and ev.get("id") == "live":
                break

        types = [e["type"] for e in events]
        assert "widget.upserted" in types
        assert "widget.deleted" in types

    def test_subscribe_replays_current_widgets(self, sw):
        sw.upsert_widget("default", "pre1", title="A", renderer="async()=>{}")
        sw.upsert_widget("default", "pre2", title="B", renderer="async()=>{}")
        events = list(sw.subscribe("default", timeout=0.2, replay=True, follow=False))
        upserts = [e for e in events if e["type"] == "widget.upserted"]
        ids = {e["widget"]["id"] for e in upserts}
        assert ids == {"pre1", "pre2"}

    def test_subscribe_unknown_space_yields_nothing(self, sw):
        events = list(sw.subscribe("ghost-space", timeout=0.1, replay=True, follow=False))
        assert events == []

    def test_layout_change_emits_position_changed(self, sw):
        sw.upsert_widget("default", "m", title="x", renderer="async()=>{}")
        events_seen = []

        import threading
        ready = threading.Event()
        def consume():
            for ev in sw.subscribe("default", timeout=2):
                if ev["type"] == "widget.position_changed":
                    events_seen.append(ev)
                    return
                if not ready.is_set():
                    ready.set()  # any event means subscription is live

        t = threading.Thread(target=consume, daemon=True)
        t.start()
        # Wait for subscriber to be live (gets snapshot or first event).
        time.sleep(0.05)
        sw.set_layout("default", "m", position={"x": 9, "y": 9})
        t.join(timeout=2)

        assert len(events_seen) == 1
        assert events_seen[0]["id"] == "m"
        assert events_seen[0]["position"] == {"x": 9, "y": 9}


# ---------------------------------------------------------------------------
# Disk format sanity (Hermes operators read these by hand)
# ---------------------------------------------------------------------------

class TestDiskFormat:
    def test_widget_yaml_is_human_readable(self, sw, tmp_path):
        renderer_src = """async (parent, space, ctx) => {
  parent.textContent = 'live';
}"""
        sw.upsert_widget(
            "default", "rd",
            title="Readable",
            renderer=renderer_src,
        )
        text = (
            tmp_path / "studio" / "spaces" / "default" / "widgets" / "rd.yaml"
        ).read_text()
        # The renderer should be written in literal-block form so newlines
        # are preserved rather than escaped — operators must be able to
        # diff a widget.yaml without YAML decoding.
        assert "parent.textContent = 'live';" in text
        assert "renderer:" in text
        # No JSON-style escaped newlines.
        assert "\\n" not in text
