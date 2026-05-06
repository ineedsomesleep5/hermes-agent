"""Tests for /api/studio/* routes in hermes_cli.web_server.

Exercises the HTTP surface of tools/background_jobs.py through the FastAPI
app, with auth + isolated HERMES_HOME, the way real dashboard clients hit it.
"""

from __future__ import annotations

import json
import time

import pytest


@pytest.fixture
def studio_client(monkeypatch, tmp_path):
    """Return a TestClient with auth wired up and a freshly-imported
    background_jobs module rooted at a tmp HERMES_HOME.

    Order matters: HERMES_HOME must be set BEFORE background_jobs is
    imported so its module-level paths land in the tmp dir.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    import sys
    sys.modules.pop("tools.background_jobs", None)

    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    import importlib
    import tools.background_jobs as bg_mod
    importlib.reload(bg_mod)

    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    yield client, bg_mod

    try:
        bg_mod.shutdown(wait=False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# enqueue / list / get
# ---------------------------------------------------------------------------

def test_post_enqueues_and_returns_pending(studio_client):
    client, bg = studio_client

    @bg.register_runner("echo")
    def _echo(job_id, *, message="", **_):
        return f"echo:{message}"

    resp = client.post(
        "/api/studio/jobs",
        json={"runner": "echo", "kwargs": {"message": "hi"}},
    )
    assert resp.status_code == 200, resp.text
    rec = resp.json()
    assert rec["runner"] == "echo"
    assert rec["status"] in ("pending", "running", "done")
    assert rec["id"]


def test_post_unknown_runner_returns_400(studio_client):
    client, _bg = studio_client
    resp = client.post(
        "/api/studio/jobs", json={"runner": "no-such-runner", "kwargs": {}}
    )
    assert resp.status_code == 400
    assert "unknown runner" in resp.json()["detail"].lower()


def test_get_list_returns_enqueued_jobs(studio_client):
    client, bg = studio_client

    @bg.register_runner("noop")
    def _noop(job_id, **_):
        return "ok"

    a = client.post("/api/studio/jobs", json={"runner": "noop", "kwargs": {}}).json()
    b = client.post("/api/studio/jobs", json={"runner": "noop", "kwargs": {}}).json()

    resp = client.get("/api/studio/jobs")
    assert resp.status_code == 200
    ids = {j["id"] for j in resp.json()}
    assert a["id"] in ids
    assert b["id"] in ids


def test_get_list_filtered_by_status(studio_client):
    client, bg = studio_client

    @bg.register_runner("quick")
    def _quick(job_id, **_):
        return "done"

    rec = client.post("/api/studio/jobs", json={"runner": "quick", "kwargs": {}}).json()
    bg.wait_for(rec["id"], timeout=5)

    resp = client.get("/api/studio/jobs?status=done")
    assert resp.status_code == 200
    statuses = {j["status"] for j in resp.json()}
    assert statuses == {"done"} or statuses == set()  # only done jobs


def test_get_single_job(studio_client):
    client, bg = studio_client

    @bg.register_runner("ping")
    def _ping(job_id, **_):
        return "pong"

    rec = client.post("/api/studio/jobs", json={"runner": "ping", "kwargs": {}}).json()
    bg.wait_for(rec["id"], timeout=5)

    resp = client.get(f"/api/studio/jobs/{rec['id']}")
    assert resp.status_code == 200
    assert resp.json()["result"] == "pong"


def test_get_unknown_job_returns_404(studio_client):
    client, _bg = studio_client
    resp = client.get("/api/studio/jobs/does-not-exist")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------

def test_delete_cancels_running_job(studio_client):
    client, bg = studio_client

    @bg.register_runner("watchful")
    def _watchful(job_id, *, cancelled, **_):
        for _ in range(200):
            if cancelled():
                return "stopped"
            time.sleep(0.02)
        return "ran-to-completion"

    rec = client.post("/api/studio/jobs", json={"runner": "watchful", "kwargs": {}}).json()
    # Wait until running.
    for _ in range(50):
        if client.get(f"/api/studio/jobs/{rec['id']}").json()["status"] == "running":
            break
        time.sleep(0.02)

    resp = client.delete(f"/api/studio/jobs/{rec['id']}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    final = bg.wait_for(rec["id"], timeout=5)
    assert final["status"] == "cancelled"


def test_delete_unknown_returns_404(studio_client):
    client, _bg = studio_client
    resp = client.delete("/api/studio/jobs/missing")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

def test_routes_require_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    import sys
    sys.modules.pop("tools.background_jobs", None)
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    import importlib
    import tools.background_jobs as bg_mod
    importlib.reload(bg_mod)
    from hermes_cli.web_server import app

    unauth = TestClient(app)  # no session header
    cases = [
        ("get", "/api/studio/jobs", None),
        ("post", "/api/studio/jobs", {"runner": "x"}),
        ("get", "/api/studio/jobs/x", None),
        ("delete", "/api/studio/jobs/x", None),
        ("get", "/api/studio/jobs/x/stream", None),
    ]
    for method, path, body in cases:
        kwargs = {"json": body} if body is not None else {}
        resp = getattr(unauth, method)(path, **kwargs)
        assert resp.status_code == 401, f"{method.upper()} {path} returned {resp.status_code}"


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------

def test_stream_emits_progress_then_terminal(studio_client):
    client, bg = studio_client

    @bg.register_runner("steps")
    def _steps(job_id, *, progress, **_):
        for i in range(3):
            progress(f"line-{i}")
            time.sleep(0.05)
        return "fin"

    rec = client.post("/api/studio/jobs", json={"runner": "steps", "kwargs": {}}).json()

    with client.stream("GET", f"/api/studio/jobs/{rec['id']}/stream?timeout=5") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

        events = []
        for raw in resp.iter_lines():
            line = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
                last = events[-1]
                if last.get("type") == "status" and last.get("status") in (
                    "done", "error", "cancelled"
                ):
                    break

    progress_lines = [e["line"] for e in events if e["type"] == "progress"]
    statuses = [e["status"] for e in events if e["type"] == "status"]
    assert progress_lines == ["line-0", "line-1", "line-2"]
    assert statuses[-1] == "done"


def test_stream_unknown_job_returns_404(studio_client):
    client, _bg = studio_client
    resp = client.get("/api/studio/jobs/missing/stream")
    assert resp.status_code == 404
