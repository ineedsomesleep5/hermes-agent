"""Tests for /api/studio/spaces/* routes (canvas storage + SSE)."""

from __future__ import annotations

import importlib
import json
import sys
import time

import pytest


@pytest.fixture
def studio(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sys.modules.pop("tools.studio_widgets", None)
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("starlette not installed")

    import tools.studio_widgets as sw_mod
    importlib.reload(sw_mod)

    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return client, sw_mod


# ---------------------------------------------------------------------------
# Spaces
# ---------------------------------------------------------------------------

def test_get_spaces_includes_default(studio):
    client, _ = studio
    resp = client.get("/api/studio/spaces")
    assert resp.status_code == 200
    ids = {s["id"] for s in resp.json()}
    assert "default" in ids


def test_create_space_then_fetch(studio):
    client, _ = studio
    resp = client.post(
        "/api/studio/spaces", json={"id": "alpha", "title": "Alpha"}
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "alpha"

    resp = client.get("/api/studio/spaces/alpha")
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Alpha"
    assert body["widgets"] == []


def test_create_duplicate_returns_400(studio):
    client, _ = studio
    client.post("/api/studio/spaces", json={"id": "dup"})
    resp = client.post("/api/studio/spaces", json={"id": "dup"})
    assert resp.status_code == 400


def test_delete_space(studio):
    client, _ = studio
    client.post("/api/studio/spaces", json={"id": "trash"})
    resp = client.delete("/api/studio/spaces/trash")
    assert resp.status_code == 200
    assert client.get("/api/studio/spaces/trash").status_code == 404


def test_cannot_delete_default(studio):
    client, _ = studio
    resp = client.delete("/api/studio/spaces/default")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

def test_upsert_then_get_widget(studio):
    client, _ = studio
    body = {
        "title": "Hello",
        "renderer": "async (p) => { p.textContent = 'hi' }",
        "position": {"x": 1, "y": 2},
        "size": {"w": 4, "h": 3},
    }
    resp = client.put("/api/studio/spaces/default/widgets/hello", json=body)
    assert resp.status_code == 200
    rec = resp.json()
    assert rec["title"] == "Hello"
    assert rec["position"] == {"x": 1, "y": 2}

    resp = client.get("/api/studio/spaces/default/widgets/hello")
    assert resp.status_code == 200
    assert resp.json()["renderer"].startswith("async")


def test_upsert_invalid_id_returns_400(studio):
    client, _ = studio
    body = {"title": "x", "renderer": "async()=>{}"}
    resp = client.put("/api/studio/spaces/default/widgets/..bad", json=body)
    assert resp.status_code == 400


def test_upsert_in_unknown_space_returns_400(studio):
    client, _ = studio
    body = {"title": "x", "renderer": "async()=>{}"}
    resp = client.put("/api/studio/spaces/missing/widgets/w", json=body)
    assert resp.status_code == 400


def test_delete_widget(studio):
    client, _ = studio
    body = {"title": "x", "renderer": "async()=>{}"}
    client.put("/api/studio/spaces/default/widgets/d", json=body)
    resp = client.delete("/api/studio/spaces/default/widgets/d")
    assert resp.status_code == 200
    resp = client.get("/api/studio/spaces/default/widgets/d")
    assert resp.status_code == 404


def test_patch_layout(studio):
    client, _ = studio
    body = {"title": "x", "renderer": "async()=>{}"}
    client.put("/api/studio/spaces/default/widgets/m", json=body)

    resp = client.patch(
        "/api/studio/spaces/default/widgets/m/layout",
        json={"position": {"x": 7, "y": 8}, "size": {"w": 5, "h": 5}},
    )
    assert resp.status_code == 200
    rec = resp.json()
    assert rec["position"] == {"x": 7, "y": 8}
    assert rec["size"] == {"w": 5, "h": 5}


def test_patch_layout_empty_returns_400(studio):
    client, _ = studio
    body = {"title": "x", "renderer": "async()=>{}"}
    client.put("/api/studio/spaces/default/widgets/m", json=body)
    resp = client.patch(
        "/api/studio/spaces/default/widgets/m/layout", json={}
    )
    assert resp.status_code == 400


def test_patch_layout_unknown_widget_returns_404(studio):
    client, _ = studio
    resp = client.patch(
        "/api/studio/spaces/default/widgets/ghost/layout",
        json={"position": {"x": 0, "y": 0}},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------

def test_events_stream_replays_existing_widgets(studio):
    client, sw = studio
    sw.upsert_widget("default", "pre1", title="A", renderer="async()=>{}")
    sw.upsert_widget("default", "pre2", title="B", renderer="async()=>{}")

    with client.stream(
        "GET", "/api/studio/spaces/default/events?timeout=1&replay=1"
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        events = []
        # Read until the timeout closes the stream.
        for raw in resp.iter_lines():
            line = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

    upserts = [e for e in events if e["type"] == "widget.upserted"]
    ids = {e["widget"]["id"] for e in upserts}
    assert {"pre1", "pre2"}.issubset(ids)


def test_events_stream_unknown_space_returns_404(studio):
    client, _ = studio
    resp = client.get("/api/studio/spaces/missing/events")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_widget_routes_require_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sys.modules.pop("tools.studio_widgets", None)
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("starlette not installed")
    import tools.studio_widgets as sw_mod
    importlib.reload(sw_mod)
    from hermes_cli.web_server import app

    unauth = TestClient(app)

    cases = [
        ("get", "/api/studio/spaces", None),
        ("post", "/api/studio/spaces", {"id": "x"}),
        ("get", "/api/studio/spaces/default", None),
        ("delete", "/api/studio/spaces/default", None),
        ("put", "/api/studio/spaces/default/widgets/x", {"title": "x", "renderer": "async()=>{}"}),
        ("get", "/api/studio/spaces/default/widgets/x", None),
        ("delete", "/api/studio/spaces/default/widgets/x", None),
        ("patch", "/api/studio/spaces/default/widgets/x/layout", {"position": {"x": 0, "y": 0}}),
        ("get", "/api/studio/spaces/default/events", None),
        ("get", "/api/studio/obsidian-graph", None),
    ]
    for method, path, body in cases:
        kwargs = {"json": body} if body is not None else {}
        resp = getattr(unauth, method)(path, **kwargs)
        assert resp.status_code == 401, f"{method.upper()} {path} -> {resp.status_code}"


def test_obsidian_graph_indexes_vault_wikilinks(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    vault = tmp_path / "profiles" / "supervisor" / "home" / "wiki"
    (vault / "entities").mkdir(parents=True)
    (vault / "concepts").mkdir(parents=True)
    (vault / "index.md").write_text(
        "---\ntitle: Index\ntags: [hermes]\n---\n# Index\n[[hermes-agent]]\n",
        encoding="utf-8",
    )
    (vault / "entities" / "hermes-agent.md").write_text(
        "---\ntitle: Hermes Agent\ntags: [hermes, agent]\n---\n# Hermes Agent\n[[studio-canvas]]\n",
        encoding="utf-8",
    )
    (vault / "concepts" / "studio-canvas.md").write_text(
        "---\ntitle: Studio Canvas\ntags: [project]\n---\n# Studio Canvas\n",
        encoding="utf-8",
    )

    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("starlette not installed")

    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    resp = client.get("/api/studio/obsidian-graph?limit=40")

    assert resp.status_code == 200
    body = resp.json()
    assert body["node_count"] == 3
    assert body["edge_count"] == 2
    assert {node["id"] for node in body["nodes"]} == {
        "index",
        "entities/hermes-agent",
        "concepts/studio-canvas",
    }


def test_log_endpoint_accepts_widget_friendly_aliases(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "agent.log").write_text("INFO hello from agent\n", encoding="utf-8")
    (logs / "gateway.log").write_text("INFO hello from gateway\n", encoding="utf-8")

    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("starlette not installed")

    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    resp = client.get("/api/logs?file=agent.log&lines=5")
    assert resp.status_code == 200
    assert resp.json()["file"] == "agent"
    assert resp.json()["lines"] == ["INFO hello from agent\n"]

    resp = client.get("/api/logs?file=gateway-supervisor.log&lines=5")
    assert resp.status_code == 200
    assert resp.json()["file"] == "gateway"
    assert resp.json()["lines"] == ["INFO hello from gateway\n"]
