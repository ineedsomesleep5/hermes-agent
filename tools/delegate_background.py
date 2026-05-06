#!/usr/bin/env python3
"""Persistent background delegation jobs.

This module provides a small durable job layer on top of tools.delegate_tool:
- enqueue a delegate_task to run in a background thread
- inspect status
- list jobs
- cancel running or queued jobs

Jobs and results are persisted under HERMES_HOME/delegation/.
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home
from tools.delegate_tool import delegate_task
from tools.registry import registry

logger = logging.getLogger(__name__)

HERMES_HOME = get_hermes_home().resolve()
BACKGROUND_DIR = HERMES_HOME / "delegation"
RESULTS_DIR = BACKGROUND_DIR / "results"
JOBS_FILE = BACKGROUND_DIR / "jobs.json"

_LOCK = threading.RLock()
_STARTED = False
_START_LOCK = threading.Lock()
_THREADS: Dict[str, threading.Thread] = {}
_CONTROLLERS: Dict[str, "BackgroundDelegateController"] = {}
_JOBS: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _ensure_dirs() -> None:
    BACKGROUND_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        BACKGROUND_DIR.chmod(0o700)
        RESULTS_DIR.chmod(0o700)
    except Exception:
        pass


def _atomic_write_json(path: Path, payload: dict) -> None:
    _ensure_dirs()
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)
    try:
        path.chmod(0o600)
    except Exception:
        pass


def _load_jobs_from_disk() -> Dict[str, Dict[str, Any]]:
    if not JOBS_FILE.exists():
        return {}
    try:
        data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not load background delegation jobs: %s", exc)
        return {}
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    out: Dict[str, Dict[str, Any]] = {}
    for job in jobs:
        if isinstance(job, dict) and job.get("job_id"):
            out[str(job["job_id"])] = job
    return out


def _save_jobs_to_disk() -> None:
    with _LOCK:
        jobs = sorted(_JOBS.values(), key=lambda j: (j.get("created_at") or "", j.get("job_id") or ""), reverse=True)
        _atomic_write_json(JOBS_FILE, {"jobs": jobs, "updated_at": _now()})


def _persist_job(job: Dict[str, Any]) -> None:
    with _LOCK:
        _JOBS[job["job_id"]] = dict(job)
        _save_jobs_to_disk()


def _update_job(job_id: str, **updates: Any) -> Optional[Dict[str, Any]]:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None
        job = {**job, **updates}
        _JOBS[job_id] = job
        _save_jobs_to_disk()
        return dict(job)


def _get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def _job_result_path(job_id: str) -> Path:
    return RESULTS_DIR / f"{job_id}.json"


def _write_result_file(job_id: str, result: Any) -> str:
    path = _job_result_path(job_id)
    payload = {
        "job_id": job_id,
        "saved_at": _now(),
        "result": result,
    }
    _atomic_write_json(path, payload)
    return str(path)


def _read_result_file(result_path: str) -> Optional[Any]:
    try:
        payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return payload


def _extract_result_summary(result: Any) -> str:
    if isinstance(result, dict):
        if isinstance(result.get("results"), list) and result["results"]:
            first = result["results"][0]
            if isinstance(first, dict):
                summary = first.get("summary") or first.get("error")
                if summary:
                    return str(summary)
        summary = result.get("summary") or result.get("final_response") or result.get("error")
        if summary:
            return str(summary)
    if result is None:
        return ""
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return text[:500]


def _result_indicates_success(result: Any) -> bool:
    if isinstance(result, dict):
        if result.get("error"):
            return False
        items = result.get("results")
        if isinstance(items, list) and items:
            ok_states = {"completed", "success", "ok"}
            states = [str(item.get("status") or "").lower() for item in items if isinstance(item, dict)]
            if states and all(state in ok_states for state in states):
                return True
            return any(state in ok_states for state in states)
        if result.get("final_response") or result.get("summary"):
            return True
    return not isinstance(result, dict)


def _result_indicates_cancelled(result: Any) -> bool:
    if isinstance(result, dict):
        if str(result.get("status") or "").lower() in {"cancelled", "canceled", "interrupted"}:
            return True
        items = result.get("results")
        if isinstance(items, list):
            states = [str(item.get("status") or "").lower() for item in items if isinstance(item, dict)]
            return any(state in {"cancelled", "canceled", "interrupted"} for state in states)
    return False


# ---------------------------------------------------------------------------
# Parent-agent proxy used by delegate_task
# ---------------------------------------------------------------------------

class BackgroundDelegateController:
    """Minimal delegate_task parent agent with interrupt propagation.

    delegate_task expects a parent_agent with a number of attributes, plus an
    interrupt() method and an _active_children list.  This proxy mirrors the
    important pieces of a real AIAgent while keeping the background job layer
    independent from the chat loop.
    """

    def __init__(self, request: Dict[str, Any], job_id: str, parent_agent: Any = None):
        self.job_id = job_id
        self.request = dict(request)
        self._interrupt_requested = False
        self._active_children: List[Any] = []
        self._active_children_lock = threading.Lock()
        self._session_db = getattr(parent_agent, "_session_db", None)
        self.session_id = getattr(parent_agent, "session_id", None)
        self.base_url = getattr(parent_agent, "base_url", None)
        self.api_key = getattr(parent_agent, "api_key", None)
        self.provider = getattr(parent_agent, "provider", None)
        self.api_mode = getattr(parent_agent, "api_mode", None)
        self.model = getattr(parent_agent, "model", None)
        self.platform = getattr(parent_agent, "platform", "cli")
        self.enabled_toolsets = getattr(parent_agent, "enabled_toolsets", None)
        self.valid_tool_names = getattr(parent_agent, "valid_tool_names", None)
        self.providers_allowed = getattr(parent_agent, "providers_allowed", None)
        self.providers_ignored = getattr(parent_agent, "providers_ignored", None)
        self.providers_order = getattr(parent_agent, "providers_order", None)
        self.provider_sort = getattr(parent_agent, "provider_sort", None)
        self.reasoning_config = getattr(parent_agent, "reasoning_config", None)
        self.prefill_messages = getattr(parent_agent, "prefill_messages", None)
        self.max_tokens = getattr(parent_agent, "max_tokens", None)
        self.acp_command = getattr(parent_agent, "acp_command", None)
        self.acp_args = getattr(parent_agent, "acp_args", None)
        self.cwd = getattr(parent_agent, "cwd", None)
        self.terminal_cwd = getattr(parent_agent, "terminal_cwd", None)
        self._subdirectory_hints = getattr(parent_agent, "_subdirectory_hints", None)
        self._memory_manager = getattr(parent_agent, "_memory_manager", None)
        self._print_fn = getattr(parent_agent, "_print_fn", None)
        self.tool_progress_callback = getattr(parent_agent, "tool_progress_callback", None)
        self.thinking_callback = getattr(parent_agent, "thinking_callback", None)
        self._credential_pool = getattr(parent_agent, "_credential_pool", None)
        self._delegate_depth = getattr(parent_agent, "_delegate_depth", 0)
        self._delegate_role = getattr(parent_agent, "_delegate_role", "leaf")
        self._client_kwargs = getattr(parent_agent, "_client_kwargs", {"api_key": self.api_key} if self.api_key else {})

    def interrupt(self, reason: str | None = None):
        self._interrupt_requested = True
        with self._active_children_lock:
            children = list(self._active_children)
        for child in children:
            try:
                if hasattr(child, "interrupt"):
                    child.interrupt(reason or f"Cancelled background job {self.job_id}")
                elif hasattr(child, "_interrupt_requested"):
                    child._interrupt_requested = True
            except Exception:
                logger.debug("Background job %s child interrupt failed", self.job_id, exc_info=True)
        return True

    def close(self):
        return None


# ---------------------------------------------------------------------------
# Core job execution
# ---------------------------------------------------------------------------

def _build_delegate_kwargs(request: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "goal": request.get("goal"),
        "context": request.get("context"),
        "toolsets": request.get("toolsets"),
        "tasks": request.get("tasks"),
        "max_iterations": request.get("max_iterations"),
        "acp_command": request.get("acp_command"),
        "acp_args": request.get("acp_args"),
        "role": request.get("role"),
    }


def _run_background_job(job_id: str) -> None:
    job = _get_job(job_id)
    if not job:
        return

    controller = _CONTROLLERS.get(job_id)
    if controller is None:
        return

    _update_job(job_id, status="running", started_at=_now(), worker_thread=threading.current_thread().name)

    result_obj: Any = None
    final_status = "failed"
    error_text = None

    try:
        delegate_kwargs = _build_delegate_kwargs(job["request"])
        delegate_kwargs["parent_agent"] = controller
        raw = delegate_task(**delegate_kwargs)
        try:
            result_obj = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            result_obj = {"raw": raw}

        if controller._interrupt_requested or _result_indicates_cancelled(result_obj):
            final_status = "cancelled"
        elif _result_indicates_success(result_obj):
            final_status = "completed"
        else:
            final_status = "failed"
    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}"
        result_obj = {
            "error": error_text,
            "traceback": traceback.format_exc(),
        }
        final_status = "cancelled" if controller._interrupt_requested else "failed"
    finally:
        try:
            result_path = _write_result_file(job_id, result_obj)
        except Exception as exc:
            logger.warning("Failed to persist background delegation result %s: %s", job_id, exc)
            result_path = ""

        summary = _extract_result_summary(result_obj)
        latest_job = _get_job(job_id) or job
        if final_status == "cancelled" and controller._interrupt_requested:
            cancel_reason = latest_job.get("cancel_reason") or "cancel requested"
        else:
            cancel_reason = latest_job.get("cancel_reason")

        _update_job(
            job_id,
            status=final_status,
            finished_at=_now(),
            result_path=result_path,
            result_summary=summary,
            error=error_text,
            cancel_requested=bool(controller._interrupt_requested),
            cancel_reason=cancel_reason,
        )
        with _LOCK:
            _CONTROLLERS.pop(job_id, None)
            _THREADS.pop(job_id, None)
        try:
            controller.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _ensure_started() -> None:
    global _STARTED
    if _STARTED:
        return
    with _START_LOCK:
        if _STARTED:
            return
        _ensure_dirs()
        with _LOCK:
            _JOBS.update(_load_jobs_from_disk())
        _STARTED = True


def _job_view(job: Dict[str, Any], *, include_result: bool = True) -> Dict[str, Any]:
    out = dict(job)
    result_path = out.get("result_path")
    if include_result and result_path:
        out["result"] = _read_result_file(result_path)
    return out


def delegate_task_background(
    goal: Optional[str] = None,
    context: Optional[str] = None,
    toolsets: Optional[List[str]] = None,
    tasks: Optional[List[Dict[str, Any]]] = None,
    max_iterations: Optional[int] = None,
    acp_command: Optional[str] = None,
    acp_args: Optional[List[str]] = None,
    role: Optional[str] = None,
    parent_agent=None,
) -> str:
    """Enqueue a delegate_task to run in the background."""
    if parent_agent is None:
        return json.dumps({"error": "delegate_task_background requires a parent agent context."}, ensure_ascii=False)

    if tasks and isinstance(tasks, list):
        request_tasks = tasks
        request_goal = None
    elif isinstance(goal, str) and goal.strip():
        request_tasks = None
        request_goal = goal.strip()
    else:
        return json.dumps({"error": "Provide either 'goal' or 'tasks'."}, ensure_ascii=False)

    _ensure_started()

    job_id = uuid.uuid4().hex[:12]
    request = {
        "goal": request_goal,
        "context": context,
        "toolsets": list(toolsets) if isinstance(toolsets, list) else toolsets,
        "tasks": request_tasks,
        "max_iterations": max_iterations,
        "acp_command": acp_command,
        "acp_args": list(acp_args) if isinstance(acp_args, list) else acp_args,
        "role": role,
    }

    job = {
        "job_id": job_id,
        "type": "delegate_task_background",
        "status": "queued",
        "cancel_requested": False,
        "cancel_reason": None,
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
        "worker_thread": None,
        "request": request,
        "result_path": None,
        "result_summary": None,
        "error": None,
        "parent_session_id": getattr(parent_agent, "session_id", None),
        "parent_platform": getattr(parent_agent, "platform", None),
    }

    controller = BackgroundDelegateController(request=request, job_id=job_id, parent_agent=parent_agent)
    with _LOCK:
        _JOBS[job_id] = job
        _CONTROLLERS[job_id] = controller
        _save_jobs_to_disk()

    thread = threading.Thread(target=_run_background_job, name=f"delegate-bg-{job_id}", args=(job_id,), daemon=True)
    with _LOCK:
        _THREADS[job_id] = thread
    thread.start()

    return json.dumps({"job_id": job_id, "status": "queued", "request": request}, ensure_ascii=False)


def delegate_task_job_status(job_id: str) -> str:
    _ensure_started()
    job = _get_job(str(job_id).strip())
    if not job:
        return json.dumps({"error": f"Unknown background delegation job: {job_id}"}, ensure_ascii=False)
    return json.dumps(_job_view(job, include_result=True), ensure_ascii=False)


def delegate_task_job_list(state: Optional[str] = None, limit: Optional[int] = None) -> str:
    _ensure_started()
    jobs = list(_JOBS.values())
    if state:
        state = str(state).strip().lower()
        jobs = [j for j in jobs if str(j.get("status") or "").lower() == state]
    jobs.sort(key=lambda j: (j.get("created_at") or "", j.get("job_id") or ""), reverse=True)
    if isinstance(limit, int) and limit > 0:
        jobs = jobs[:limit]
    return json.dumps([_job_view(j, include_result=False) for j in jobs], ensure_ascii=False)


def delegate_task_job_cancel(job_id: str, reason: Optional[str] = None) -> str:
    _ensure_started()
    job_id = str(job_id).strip()
    if not job_id:
        return json.dumps({"error": "job_id is required"}, ensure_ascii=False)

    with _LOCK:
        job = _JOBS.get(job_id)
        controller = _CONTROLLERS.get(job_id)
    if not job:
        return json.dumps({"error": f"Unknown background delegation job: {job_id}"}, ensure_ascii=False)

    current_status = str(job.get("status") or "")
    reason_text = reason or job.get("cancel_reason") or "cancelled by user"

    if current_status in {"completed", "failed", "cancelled"}:
        return json.dumps(_job_view(job, include_result=True), ensure_ascii=False)

    if current_status == "queued":
        _update_job(
            job_id,
            status="cancelled",
            cancel_requested=True,
            cancel_reason=reason_text,
            finished_at=_now(),
        )
        return json.dumps(_job_view(_get_job(job_id) or job, include_result=True), ensure_ascii=False)

    if controller is not None:
        controller._interrupt_requested = True
        controller.interrupt(reason_text)

    _update_job(job_id, status="cancelling", cancel_requested=True, cancel_reason=reason_text)
    return json.dumps(_job_view(_get_job(job_id) or job, include_result=False), ensure_ascii=False)


def shutdown_background_delegate_jobs(timeout: float = 1.0) -> None:
    """Best-effort shutdown helper for tests and process exit."""
    with _LOCK:
        controllers = list(_CONTROLLERS.items())
    for job_id, controller in controllers:
        try:
            controller.interrupt("shutdown")
        except Exception:
            pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _LOCK:
            if not _THREADS:
                break
        time.sleep(0.01)


atexit.register(shutdown_background_delegate_jobs)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

BACKGROUND_SCHEMA = {
    "name": "delegate_task_background",
    "description": (
        "Queue a delegate_task to run in the background and return immediately. "
        "Use this when the user wants to keep chatting while long-running delegated work continues."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "What the subagent should accomplish. Required unless tasks is provided.",
            },
            "context": {
                "type": "string",
                "description": "Background information the subagent needs.",
            },
            "toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Toolsets to enable for the subagent.",
            },
            "tasks": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Batch task list. When provided, goal is ignored.",
            },
            "max_iterations": {
                "type": "integer",
                "description": "Optional override forwarded to the delegated work.",
            },
            "acp_command": {
                "type": "string",
                "description": "Optional ACP command override forwarded to the delegated work.",
            },
            "acp_args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional ACP args override forwarded to the delegated work.",
            },
            "role": {
                "type": "string",
                "description": "leaf or orchestrator.",
            },
        },
        "additionalProperties": False,
    },
}

STATUS_SCHEMA = {
    "name": "delegate_task_job_status",
    "description": "Inspect the status and result of a background delegation job.",
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "Background delegation job id."},
        },
        "required": ["job_id"],
        "additionalProperties": False,
    },
}

LIST_SCHEMA = {
    "name": "delegate_task_job_list",
    "description": "List background delegation jobs.",
    "parameters": {
        "type": "object",
        "properties": {
            "state": {
                "type": "string",
                "description": "Optional filter by job status (queued, running, cancelling, completed, failed, cancelled).",
            },
            "limit": {
                "type": "integer",
                "description": "Optional maximum number of jobs to return.",
            },
        },
        "additionalProperties": False,
    },
}

CANCEL_SCHEMA = {
    "name": "delegate_task_job_cancel",
    "description": "Cancel a queued or running background delegation job.",
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "Background delegation job id."},
            "reason": {"type": "string", "description": "Optional human-readable cancellation reason."},
        },
        "required": ["job_id"],
        "additionalProperties": False,
    },
}


def check_background_delegate_requirements() -> bool:
    return True


registry.register(
    name="delegate_task_background",
    toolset="delegation",
    schema=BACKGROUND_SCHEMA,
    handler=lambda args, **kw: delegate_task_background(
        goal=args.get("goal"),
        context=args.get("context"),
        toolsets=args.get("toolsets"),
        tasks=args.get("tasks"),
        max_iterations=args.get("max_iterations"),
        acp_command=args.get("acp_command"),
        acp_args=args.get("acp_args"),
        role=args.get("role"),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=check_background_delegate_requirements,
    description=BACKGROUND_SCHEMA["description"],
)

registry.register(
    name="delegate_task_job_status",
    toolset="delegation",
    schema=STATUS_SCHEMA,
    handler=lambda args, **kw: delegate_task_job_status(job_id=args.get("job_id")),
    check_fn=check_background_delegate_requirements,
    description=STATUS_SCHEMA["description"],
)

registry.register(
    name="delegate_task_job_list",
    toolset="delegation",
    schema=LIST_SCHEMA,
    handler=lambda args, **kw: delegate_task_job_list(
        state=args.get("state"),
        limit=args.get("limit"),
    ),
    check_fn=check_background_delegate_requirements,
    description=LIST_SCHEMA["description"],
)

registry.register(
    name="delegate_task_job_cancel",
    toolset="delegation",
    schema=CANCEL_SCHEMA,
    handler=lambda args, **kw: delegate_task_job_cancel(
        job_id=args.get("job_id"),
        reason=args.get("reason"),
    ),
    check_fn=check_background_delegate_requirements,
    description=CANCEL_SCHEMA["description"],
)
