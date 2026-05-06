#!/usr/bin/env python3
"""Tests for background delegation jobs.

These tests isolate HERMES_HOME so job persistence stays inside tmp_path.
"""

from __future__ import annotations

import importlib
import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch


def _fresh_module(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    for mod in ["tools.delegate_background"]:
        sys.modules.pop(mod, None)

    import tools.delegate_background as mod

    importlib.reload(mod)
    return mod


def _make_parent():
    parent = MagicMock()
    parent.session_id = "sess-123"
    parent.platform = "cli"
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "***"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.enabled_toolsets = ["terminal", "file"]
    parent.valid_tool_names = ["read_file", "terminal"]
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    return parent


def _wait_until(predicate, timeout=3.0, interval=0.02):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s; last={last!r}")


class TestBackgroundDelegationJobs:
    def test_enqueue_runs_asynchronously_and_persists_result(self, tmp_path, monkeypatch):
        mod = _fresh_module(tmp_path, monkeypatch)
        parent = _make_parent()

        def fake_delegate_task(*args, **kwargs):
            return json.dumps(
                {
                    "results": [
                        {
                            "task_index": 0,
                            "status": "completed",
                            "summary": "Background done",
                            "api_calls": 2,
                            "duration_seconds": 0.1,
                        }
                    ],
                    "total_duration_seconds": 0.1,
                }
            )

        with patch.object(mod, "delegate_task", side_effect=fake_delegate_task):
            enqueue = json.loads(mod.delegate_task_background(goal="Do background work", parent_agent=parent))
            assert enqueue["status"] == "queued"
            assert enqueue["job_id"]

            job_id = enqueue["job_id"]
            status = _wait_until(lambda: json.loads(mod.delegate_task_job_status(job_id)).get("status") in {"completed", "failed", "cancelled"} and json.loads(mod.delegate_task_job_status(job_id)))
            assert status["status"] == "completed"
            assert status["result"]["results"][0]["summary"] == "Background done"
            assert Path(status["result_path"]).exists()

            jobs = json.loads(mod.delegate_task_job_list())
            assert jobs[0]["job_id"] == job_id
            assert jobs[0]["status"] == "completed"

            # Re-import the module to prove persistence survives a reload.
            sys.modules.pop("tools.delegate_background", None)
            mod2 = importlib.import_module("tools.delegate_background")
            reloaded = json.loads(mod2.delegate_task_job_status(job_id))
            assert reloaded["status"] == "completed"
            assert reloaded["result"]["results"][0]["summary"] == "Background done"

    def test_cancel_running_job_interrupts_controller(self, tmp_path, monkeypatch):
        mod = _fresh_module(tmp_path, monkeypatch)
        parent = _make_parent()
        started = threading.Event()
        finished = threading.Event()

        def fake_delegate_task(*args, **kwargs):
            parent_agent = kwargs["parent_agent"]
            started.set()
            while not getattr(parent_agent, "_interrupt_requested", False):
                time.sleep(0.01)
            finished.set()
            return json.dumps(
                {
                    "results": [
                        {
                            "task_index": 0,
                            "status": "interrupted",
                            "summary": None,
                            "error": "cancelled",
                            "api_calls": 0,
                            "duration_seconds": 0.2,
                        }
                    ],
                    "total_duration_seconds": 0.2,
                }
            )

        with patch.object(mod, "delegate_task", side_effect=fake_delegate_task):
            enqueue = json.loads(mod.delegate_task_background(goal="Slow work", parent_agent=parent))
            job_id = enqueue["job_id"]

            _wait_until(lambda: started.is_set())
            cancel = json.loads(mod.delegate_task_job_cancel(job_id, reason="user request"))
            assert cancel["status"] in {"cancelling", "cancelled"}

            _wait_until(lambda: finished.is_set())
            status = _wait_until(lambda: json.loads(mod.delegate_task_job_status(job_id)).get("status") in {"cancelled", "completed", "failed"} and json.loads(mod.delegate_task_job_status(job_id)))
            assert status["status"] == "cancelled"
            assert status["cancel_requested"] is True
            assert status["cancel_reason"] == "user request"

    def test_status_and_list_include_request_metadata(self, tmp_path, monkeypatch):
        mod = _fresh_module(tmp_path, monkeypatch)
        parent = _make_parent()

        def fake_delegate_task(*args, **kwargs):
            return json.dumps({"results": [], "final_response": "done", "total_duration_seconds": 0.0})

        with patch.object(mod, "delegate_task", side_effect=fake_delegate_task):
            enqueue = json.loads(mod.delegate_task_background(goal="Inspect me", toolsets=["terminal", "file"], parent_agent=parent))
            job_id = enqueue["job_id"]
            status = _wait_until(lambda: json.loads(mod.delegate_task_job_status(job_id)).get("status") == "completed" and json.loads(mod.delegate_task_job_status(job_id)))
            assert status["request"]["goal"] == "Inspect me"
            assert status["request"]["toolsets"] == ["terminal", "file"]
            listing = json.loads(mod.delegate_task_job_list())
            assert listing[0]["job_id"] == job_id
            assert listing[0]["request"]["goal"] == "Inspect me"
