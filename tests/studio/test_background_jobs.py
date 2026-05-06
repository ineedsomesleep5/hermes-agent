"""Tests for tools/background_jobs.py — durable, cancellable, restart-safe job runner.

Hermes Studio sits on top of this. A "job" is a registered runner-name +
kwargs; the worker pool picks it up, the runner writes progress lines and
returns a result, and the record is persisted to ~/.hermes/studio/jobs.json
across the entire lifecycle.

Mirrors the JSON-on-disk + threading.Lock pattern from cron/jobs.py.
"""

from __future__ import annotations

import importlib
import json
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def bg(monkeypatch, tmp_path):
    """Fresh background_jobs module, isolated to a tmp HERMES_HOME.

    Each test gets a clean import so module-level state (worker pool,
    registry, in-memory cache) starts empty, and the on-disk store points
    at a per-test directory.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    # Force a clean import — clear cached module so its module-level
    # state (registry, executor) is rebuilt against the new HERMES_HOME.
    import sys
    sys.modules.pop("tools.background_jobs", None)

    import tools.background_jobs as bg_mod
    importlib.reload(bg_mod)

    yield bg_mod

    # Best-effort teardown so a hung runner can't leak a thread between tests.
    try:
        bg_mod.shutdown(wait=False)
    except Exception:
        pass


@pytest.fixture
def echo_runner(bg):
    """Register a deterministic 'echo' runner that returns its kwargs."""
    @bg.register_runner("echo")
    def _echo(job_id: str, *, message: str = "", progress=None, **_):
        if progress:
            progress(f"echo: {message}")
        return f"echoed:{message}"
    return _echo


# ---------------------------------------------------------------------------
# enqueue + status
# ---------------------------------------------------------------------------

class TestEnqueue:
    def test_enqueue_returns_pending_record(self, bg, echo_runner):
        rec = bg.enqueue("echo", {"message": "hi"})
        assert rec["id"]
        assert rec["status"] == "pending"
        assert rec["runner"] == "echo"
        assert rec["kwargs"] == {"message": "hi"}
        assert "created_at" in rec
        assert rec["result"] is None
        assert rec["error"] is None

    def test_unknown_runner_rejected(self, bg):
        with pytest.raises(ValueError, match="unknown runner"):
            bg.enqueue("does-not-exist", {})

    def test_enqueue_persists_to_disk(self, bg, echo_runner, tmp_path):
        rec = bg.enqueue("echo", {"message": "persist"})
        store = tmp_path / "studio" / "jobs.json"
        assert store.exists()
        data = json.loads(store.read_text())
        ids = [j["id"] for j in data["jobs"]]
        assert rec["id"] in ids


# ---------------------------------------------------------------------------
# Lifecycle: pending → running → done
# ---------------------------------------------------------------------------

class TestExecution:
    def test_runner_runs_and_record_becomes_done(self, bg, echo_runner):
        rec = bg.enqueue("echo", {"message": "hello"})
        final = bg.wait_for(rec["id"], timeout=5)
        assert final["status"] == "done"
        assert final["result"] == "echoed:hello"
        assert final["started_at"] is not None
        assert final["finished_at"] is not None
        assert final["error"] is None

    def test_runner_exception_is_captured_as_error(self, bg):
        @bg.register_runner("boom")
        def _boom(job_id, **_):
            raise RuntimeError("kaboom")

        rec = bg.enqueue("boom", {})
        final = bg.wait_for(rec["id"], timeout=5)
        assert final["status"] == "error"
        assert "kaboom" in (final["error"] or "")
        assert final["result"] is None

    def test_progress_lines_are_appended(self, bg):
        @bg.register_runner("chatty")
        def _chatty(job_id, *, progress, **_):
            progress("step 1")
            progress("step 2")
            progress("step 3")
            return "done"

        rec = bg.enqueue("chatty", {})
        final = bg.wait_for(rec["id"], timeout=5)
        assert final["progress"] == ["step 1", "step 2", "step 3"]


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

class TestList:
    def test_list_returns_all_jobs(self, bg, echo_runner):
        a = bg.enqueue("echo", {"message": "a"})
        b = bg.enqueue("echo", {"message": "b"})
        bg.wait_for(a["id"], timeout=5)
        bg.wait_for(b["id"], timeout=5)
        jobs = bg.list_jobs()
        ids = {j["id"] for j in jobs}
        assert a["id"] in ids
        assert b["id"] in ids

    def test_list_filtered_by_status(self, bg):
        @bg.register_runner("slow")
        def _slow(job_id, *, gate, **_):
            gate.wait(timeout=5)
            return "ok"

        gate = threading.Event()
        rec = bg.enqueue("slow", {"gate": gate})
        # Give the worker a moment to pick it up.
        for _ in range(50):
            j = bg.get_job(rec["id"])
            if j["status"] == "running":
                break
            time.sleep(0.02)
        running = bg.list_jobs(status="running")
        assert any(j["id"] == rec["id"] for j in running)
        gate.set()
        bg.wait_for(rec["id"], timeout=5)
        done = bg.list_jobs(status="done")
        assert any(j["id"] == rec["id"] for j in done)


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

class TestCancel:
    def test_cancel_pending_job_marks_cancelled(self, bg):
        # Block the worker pool with a long-running job so a follow-up stays pending.
        @bg.register_runner("blocker")
        def _blocker(job_id, *, gate, **_):
            gate.wait(timeout=5)
            return "ok"

        @bg.register_runner("victim")
        def _victim(job_id, **_):
            return "should not run"

        # Saturate workers (default size of 4 — enqueue 8 to be sure).
        gate = threading.Event()
        for _ in range(8):
            bg.enqueue("blocker", {"gate": gate})
        victim = bg.enqueue("victim", {})
        # Should still be pending.
        assert bg.get_job(victim["id"])["status"] == "pending"

        ok = bg.cancel(victim["id"])
        assert ok is True
        assert bg.get_job(victim["id"])["status"] == "cancelled"

        gate.set()  # let blockers finish for cleanup

    def test_cancel_running_job_signals_runner(self, bg):
        @bg.register_runner("watchful")
        def _watchful(job_id, *, cancelled, **_):
            for _ in range(200):
                if cancelled():
                    return "interrupted"
                time.sleep(0.02)
            return "completed"

        rec = bg.enqueue("watchful", {})
        # Wait until running.
        for _ in range(50):
            if bg.get_job(rec["id"])["status"] == "running":
                break
            time.sleep(0.02)
        ok = bg.cancel(rec["id"])
        assert ok is True
        final = bg.wait_for(rec["id"], timeout=5)
        assert final["status"] == "cancelled"

    def test_cancel_unknown_returns_false(self, bg):
        assert bg.cancel("does-not-exist") is False


# ---------------------------------------------------------------------------
# Persistence + restart safety
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_done_jobs_survive_module_reload(self, bg, echo_runner, tmp_path, monkeypatch):
        rec = bg.enqueue("echo", {"message": "remember-me"})
        bg.wait_for(rec["id"], timeout=5)

        # Simulate process restart: drop the module, re-import against the
        # same HERMES_HOME, the persisted record should reload.
        bg.shutdown(wait=True)
        import sys
        sys.modules.pop("tools.background_jobs", None)
        import tools.background_jobs as bg2
        importlib.reload(bg2)

        recovered = bg2.get_job(rec["id"])
        assert recovered is not None
        assert recovered["status"] == "done"
        assert recovered["result"] == "echoed:remember-me"
        bg2.shutdown(wait=False)

    def test_pending_jobs_become_orphaned_on_restart(self, bg, tmp_path, monkeypatch):
        """If the process dies while a job was running, the next boot must
        not silently leave it in 'running' state — it should be marked
        'orphaned' so the UI knows the result is lost.
        """
        # Manually inject a 'running' record on disk (no live worker).
        store = tmp_path / "studio"
        store.mkdir(parents=True, exist_ok=True)
        (store / "jobs.json").write_text(json.dumps({
            "jobs": [{
                "id": "ghost",
                "runner": "echo",
                "kwargs": {"message": "x"},
                "status": "running",
                "created_at": "2026-04-30T00:00:00",
                "started_at": "2026-04-30T00:00:01",
                "finished_at": None,
                "progress": [],
                "result": None,
                "error": None,
            }]
        }))

        bg.shutdown(wait=True)
        import sys
        sys.modules.pop("tools.background_jobs", None)
        import tools.background_jobs as bg2
        importlib.reload(bg2)

        rec = bg2.get_job("ghost")
        assert rec is not None
        assert rec["status"] == "orphaned"
        bg2.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Subscription / streaming
# ---------------------------------------------------------------------------

class TestSubscribe:
    def test_subscribe_yields_progress_then_terminal(self, bg):
        @bg.register_runner("steps")
        def _steps(job_id, *, progress, **_):
            for i in range(3):
                progress(f"step {i}")
                time.sleep(0.05)
            return "all-done"

        rec = bg.enqueue("steps", {})
        seen_progress = []
        terminal = None
        for event in bg.subscribe(rec["id"], timeout=5):
            if event["type"] == "progress":
                seen_progress.append(event["line"])
            elif event["type"] == "status":
                terminal = event["status"]
                if event["status"] in ("done", "error", "cancelled"):
                    break
        assert seen_progress == ["step 0", "step 1", "step 2"]
        assert terminal == "done"

    def test_subscribe_to_already_finished_job_replays(self, bg, echo_runner):
        rec = bg.enqueue("echo", {"message": "history"})
        bg.wait_for(rec["id"], timeout=5)
        events = list(bg.subscribe(rec["id"], timeout=2))
        # Should at least emit the final terminal status.
        statuses = [e for e in events if e["type"] == "status"]
        assert any(e["status"] == "done" for e in statuses)
