"""Durable, cancellable background job runner for Hermes Studio.

Public surface: ``register_runner``, ``enqueue``, ``get_job``, ``list_jobs``,
``cancel``, ``wait_for``, ``subscribe``, ``shutdown``.


A "runner" is a Python callable registered by name. ``enqueue(name, kwargs)``
schedules it on a ThreadPoolExecutor; the runner receives ``progress(line)``
and ``cancelled() -> bool`` callbacks alongside its own kwargs. Status
transitions and progress lines persist to ``$HERMES_HOME/studio/jobs.json``
atomically so a process restart never leaves the on-disk view inconsistent.

This is the substrate the Studio API + UI sit on top of: long delegations,
agent calls, widget-render jobs, anything that should survive the parent
chat tab and stream progress back to the browser.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from queue import Empty, Queue
from typing import Any, Callable, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STUDIO_DIR = get_hermes_home().resolve() / "studio"
JOBS_FILE = STUDIO_DIR / "jobs.json"
DEFAULT_WORKERS = 4

TERMINAL_STATUSES = frozenset({"done", "error", "cancelled", "orphaned"})

# Module-level state. Re-initialized when the module is reloaded so tests
# get a clean slate by clearing sys.modules and re-importing.
_jobs_lock = threading.Lock()
_runners: Dict[str, Callable[..., Any]] = {}
_cancel_flags: Dict[str, threading.Event] = {}
_done_events: Dict[str, threading.Event] = {}
_subscribers: Dict[str, List[Queue]] = {}
_executor: Optional[ThreadPoolExecutor] = None


# ---------------------------------------------------------------------------
# On-disk persistence (mirrors cron/jobs.py atomic-write pattern)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dirs() -> None:
    STUDIO_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(STUDIO_DIR, 0o700)
    except (OSError, NotImplementedError):
        pass


def _load_raw() -> List[Dict[str, Any]]:
    if not JOBS_FILE.exists():
        return []
    try:
        with open(JOBS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read studio jobs.json: %s", exc)
        return []
    return data.get("jobs", [])


def _save_raw(jobs: List[Dict[str, Any]]) -> None:
    _ensure_dirs()
    fd, tmp = tempfile.mkstemp(
        dir=str(JOBS_FILE.parent), prefix=".jobs_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                {"jobs": jobs, "updated_at": _now_iso()}, f, indent=2
            )
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, JOBS_FILE)
        try:
            os.chmod(JOBS_FILE, 0o600)
        except (OSError, NotImplementedError):
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _sanitize_kwargs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Return a JSON-safe view of kwargs for on-disk persistence.

    Non-serializable objects (Events, Locks, callables) are replaced with a
    type marker so the persisted record stays loadable. The original dict is
    still passed unchanged to the runner — only the on-disk copy is reduced.
    """
    out: Dict[str, Any] = {}
    for k, v in (kwargs or {}).items():
        try:
            json.dumps(v)
            out[k] = v
        except (TypeError, ValueError):
            out[k] = f"<{type(v).__name__}>"
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def register_runner(name: str):
    """Decorator that registers a runner under ``name``.

    The runner is called as ``fn(job_id, *, progress, cancelled, **kwargs)``.
    ``progress(line: str)`` appends a line to the job record (and pushes it
    to live subscribers); ``cancelled()`` returns True when the user has
    requested cancellation. Whatever the runner returns is stored as
    ``result``; raising sets status="error".
    """

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        _runners[name] = fn
        return fn

    return deco


def enqueue(runner: str, kwargs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if runner not in _runners:
        raise ValueError(f"unknown runner: {runner!r}")
    if _executor is None:
        raise RuntimeError("background_jobs: executor not initialized")

    kwargs = dict(kwargs or {})
    job_id = uuid.uuid4().hex[:12]
    record: Dict[str, Any] = {
        "id": job_id,
        "runner": runner,
        "kwargs": _sanitize_kwargs(kwargs),
        "status": "pending",
        "created_at": _now_iso(),
        "started_at": None,
        "finished_at": None,
        "progress": [],
        "result": None,
        "error": None,
    }
    with _jobs_lock:
        jobs = _load_raw()
        jobs.append(record)
        _save_raw(jobs)
    _cancel_flags[job_id] = threading.Event()
    _done_events[job_id] = threading.Event()
    _executor.submit(_run, job_id, runner, kwargs)
    return dict(record)


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _jobs_lock:
        for j in _load_raw():
            if j["id"] == job_id:
                return dict(j)
    return None


def list_jobs(status: Optional[str] = None) -> List[Dict[str, Any]]:
    with _jobs_lock:
        jobs = _load_raw()
    if status:
        jobs = [j for j in jobs if j["status"] == status]
    return jobs


def cancel(job_id: str) -> bool:
    rec = get_job(job_id)
    if rec is None:
        return False
    evt = _cancel_flags.get(job_id)
    if evt is not None:
        evt.set()
    if rec["status"] == "pending":
        # Worker has not picked it up yet. Flip directly so the UI sees
        # the change immediately; the deferred _run() will short-circuit.
        with _jobs_lock:
            jobs = _load_raw()
            for j in jobs:
                if j["id"] == job_id and j["status"] == "pending":
                    j["status"] = "cancelled"
                    j["finished_at"] = _now_iso()
                    _save_raw(jobs)
                    _emit_locked(job_id, {"type": "status", "status": "cancelled"})
                    break
        d = _done_events.get(job_id)
        if d is not None:
            d.set()
    # If running, the runner is responsible for noticing cancelled() and
    # exiting; the wrapper in _run() finalizes status afterward.
    return True


def wait_for(job_id: str, timeout: Optional[float] = None) -> Dict[str, Any]:
    """Block until the job reaches a terminal status, then return its record."""
    evt = _done_events.get(job_id)
    if evt is not None:
        evt.wait(timeout=timeout)
    rec = get_job(job_id)
    if rec is None:
        raise KeyError(job_id)
    return rec


def subscribe(job_id: str, timeout: Optional[float] = 30):
    """Yield {type, ...} events for a job until terminal status or timeout.

    Replays existing progress lines + current status from the persisted
    record, then streams live events. Locking ensures the snapshot we
    replay never overlaps with the live queue.
    """
    q: Queue = Queue()
    with _jobs_lock:
        snapshot: Optional[Dict[str, Any]] = None
        for j in _load_raw():
            if j["id"] == job_id:
                snapshot = dict(j)
                break
        if snapshot is None:
            return
        _subscribers.setdefault(job_id, []).append(q)

    try:
        for line in snapshot.get("progress", []):
            yield {"type": "progress", "line": line}
        yield {"type": "status", "status": snapshot["status"]}
        if snapshot["status"] in TERMINAL_STATUSES:
            return

        end = time.monotonic() + timeout if timeout else None
        while True:
            remaining: Optional[float] = None
            if end is not None:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return
            try:
                event = q.get(timeout=remaining)
            except Empty:
                return
            yield event
            if (
                event.get("type") == "status"
                and event.get("status") in TERMINAL_STATUSES
            ):
                return
    finally:
        with _jobs_lock:
            try:
                _subscribers.get(job_id, []).remove(q)
            except (ValueError, KeyError):
                pass


def shutdown(wait: bool = True) -> None:
    global _executor
    if _executor is not None:
        try:
            _executor.shutdown(wait=wait, cancel_futures=not wait)
        except Exception:
            pass
        _executor = None


# ---------------------------------------------------------------------------
# Internal: worker + emit helpers
# ---------------------------------------------------------------------------

def _emit_locked(job_id: str, event: Dict[str, Any]) -> None:
    """Push an event to live subscribers. Must hold _jobs_lock."""
    for q in _subscribers.get(job_id, []):
        try:
            q.put_nowait(event)
        except Exception:
            pass


def _append_progress(job_id: str, line: str) -> None:
    with _jobs_lock:
        jobs = _load_raw()
        for j in jobs:
            if j["id"] == job_id:
                j["progress"].append(line)
                _save_raw(jobs)
                break
        _emit_locked(job_id, {"type": "progress", "line": line})


def _set_status(job_id: str, **patch: Any) -> Optional[Dict[str, Any]]:
    with _jobs_lock:
        jobs = _load_raw()
        for j in jobs:
            if j["id"] == job_id:
                j.update(patch)
                _save_raw(jobs)
                rec = dict(j)
                if "status" in patch:
                    _emit_locked(
                        job_id, {"type": "status", "status": patch["status"]}
                    )
                return rec
    return None


def _run(job_id: str, runner_name: str, kwargs: Dict[str, Any]) -> None:
    cancel_evt = _cancel_flags.get(job_id)
    done_evt = _done_events.get(job_id)
    try:
        # Cancelled before pickup — nothing to do; cancel() already emitted.
        if cancel_evt is not None and cancel_evt.is_set():
            current = get_job(job_id)
            if current is None or current["status"] != "cancelled":
                _set_status(job_id, status="cancelled", finished_at=_now_iso())
            return

        _set_status(job_id, status="running", started_at=_now_iso())

        def progress(line: Any) -> None:
            _append_progress(job_id, str(line))

        def cancelled() -> bool:
            return bool(cancel_evt is not None and cancel_evt.is_set())

        runner_fn = _runners.get(runner_name)
        if runner_fn is None:
            _set_status(
                job_id,
                status="error",
                finished_at=_now_iso(),
                error=f"runner gone: {runner_name}",
            )
            return

        result = runner_fn(
            job_id, progress=progress, cancelled=cancelled, **kwargs
        )

        if cancel_evt is not None and cancel_evt.is_set():
            _set_status(
                job_id,
                status="cancelled",
                finished_at=_now_iso(),
                result=result,
            )
        else:
            _set_status(
                job_id, status="done", finished_at=_now_iso(), result=result
            )
    except Exception as exc:
        logger.exception("background job %s crashed", job_id)
        _set_status(
            job_id, status="error", finished_at=_now_iso(), error=str(exc)
        )
    finally:
        if done_evt is not None:
            done_evt.set()


# ---------------------------------------------------------------------------
# Module init: recover from prior process death
# ---------------------------------------------------------------------------

def _init() -> None:
    global _executor
    _ensure_dirs()
    _executor = ThreadPoolExecutor(
        max_workers=DEFAULT_WORKERS, thread_name_prefix="hermes-studio-job"
    )
    # Any job left in 'running' or 'pending' from the previous process is
    # orphaned — we lost its in-memory state (cancel events, runner kwargs
    # that may have included non-serializable objects). Mark it so the UI
    # surfaces the loss instead of waiting forever.
    with _jobs_lock:
        jobs = _load_raw()
        changed = False
        for j in jobs:
            if j["status"] in ("running", "pending"):
                j["status"] = "orphaned"
                j["finished_at"] = j.get("finished_at") or _now_iso()
                changed = True
        if changed:
            _save_raw(jobs)


_init()
