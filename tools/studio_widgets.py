"""Hermes Studio canvas storage — spaces and widgets, on disk as YAML.

A *space* is a named workspace; a *widget* lives in exactly one space and
carries a JS renderer (as plain text) that the browser canvas evaluates
to mount the widget body. Operators can hand-edit the YAML files; that's
why we write them with literal-block scalars so renderers stay readable.

Every mutation publishes an event to per-space subscriber queues so the
SSE route can stream live changes to the browser while the agent is
authoring widgets.

On-disk layout::

    $HERMES_HOME/studio/spaces/<space_id>/space.yaml
    $HERMES_HOME/studio/spaces/<space_id>/widgets/<widget_id>.yaml
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Dict, Iterator, List, Optional

import yaml

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STUDIO_DIR = get_hermes_home().resolve() / "studio"
SPACES_DIR = STUDIO_DIR / "spaces"
DEFAULT_SPACE_ID = "default"

WIDGET_SCHEMA = "studio/widget/v1"
SPACE_SCHEMA = "studio/space/v1"

DEFAULT_WIDGET_POSITION = {"x": 0, "y": 0}
DEFAULT_WIDGET_SIZE = {"w": 6, "h": 4}

# IDs are used as path components, so we restrict them to a safe charset.
_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$")

_lock = threading.Lock()
_subscribers: Dict[str, List[Queue]] = {}


# ---------------------------------------------------------------------------
# YAML helpers — literal-block scalars for multi-line strings so renderer
# source stays human-diff-able on disk.
# ---------------------------------------------------------------------------

def _str_representer(dumper: yaml.Dumper, data: str):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


class _StudioDumper(yaml.SafeDumper):
    pass


_StudioDumper.add_representer(str, _str_representer)


def _dump_yaml(data: Any) -> str:
    return yaml.dump(
        data,
        Dumper=_StudioDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


def _load_yaml(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.error("studio_widgets: failed to read %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def _atomic_write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".sw_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(contents)
            f.flush()
            os.fsync(f.fileno())
        # Make readable by both the dashboard (root) and the agent
        # (hermes) regardless of which user wrote the file. mkstemp
        # defaults to 0600 which silently breaks cross-user reads.
        try:
            os.chmod(tmp, 0o664)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_id(value: str, kind: str) -> None:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValueError(
            f"invalid {kind} id: {value!r} (must match {_ID_RE.pattern})"
        )


def _validate_layout(
    position: Optional[Dict[str, Any]], size: Optional[Dict[str, Any]]
) -> tuple[Dict[str, int], Dict[str, int]]:
    pos = dict(position or DEFAULT_WIDGET_POSITION)
    siz = dict(size or DEFAULT_WIDGET_SIZE)
    for key in ("x", "y"):
        if not isinstance(pos.get(key), int):
            raise ValueError(f"position.{key} must be int, got {pos.get(key)!r}")
    for key in ("w", "h"):
        if not isinstance(siz.get(key), int) or siz[key] < 1:
            raise ValueError(f"size.{key} must be a positive int")
    return {"x": pos["x"], "y": pos["y"]}, {"w": siz["w"], "h": siz["h"]}


# ---------------------------------------------------------------------------
# Pub/sub
# ---------------------------------------------------------------------------

def _emit_locked(space_id: str, event: Dict[str, Any]) -> None:
    """Push to live subscribers of `space_id`. Caller must hold _lock."""
    for q in _subscribers.get(space_id, []):
        try:
            q.put_nowait(event)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _space_dir(space_id: str) -> Path:
    return SPACES_DIR / space_id


def _space_yaml_path(space_id: str) -> Path:
    return _space_dir(space_id) / "space.yaml"


def _widget_yaml_path(space_id: str, widget_id: str) -> Path:
    return _space_dir(space_id) / "widgets" / f"{widget_id}.yaml"


def _ensure_default_space() -> None:
    """Idempotently create the default space + dirs."""
    SPACES_DIR.mkdir(parents=True, exist_ok=True)
    if not _space_yaml_path(DEFAULT_SPACE_ID).exists():
        record = {
            "id": DEFAULT_SPACE_ID,
            "title": "Hermes Studio",
            "schema": SPACE_SCHEMA,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "widget_order": [],
        }
        _atomic_write(_space_yaml_path(DEFAULT_SPACE_ID), _dump_yaml(record))


# ---------------------------------------------------------------------------
# Spaces — public API
# ---------------------------------------------------------------------------

def list_spaces() -> List[Dict[str, Any]]:
    with _lock:
        _ensure_default_space()
        out: List[Dict[str, Any]] = []
        if not SPACES_DIR.exists():
            return out
        for child in sorted(SPACES_DIR.iterdir()):
            if not child.is_dir():
                continue
            record = _load_yaml(child / "space.yaml")
            if record:
                out.append(record)
        return out


def create_space(space_id: str, *, title: Optional[str] = None) -> Dict[str, Any]:
    _validate_id(space_id, "space")
    with _lock:
        path = _space_yaml_path(space_id)
        if path.exists():
            raise ValueError(f"space already exists: {space_id}")
        record = {
            "id": space_id,
            "title": title or space_id,
            "schema": SPACE_SCHEMA,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "widget_order": [],
        }
        _atomic_write(path, _dump_yaml(record))
        _emit_locked(space_id, {"type": "space.updated", "space": dict(record)})
        return record


def get_space(space_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        record = _load_yaml(_space_yaml_path(space_id))
        if record is None:
            return None
        widgets = _read_widgets_locked(space_id)
        order = _sync_widget_order_locked(space_id, record, widgets, persist=True)
        index = {wid: i for i, wid in enumerate(order)}
        widgets.sort(key=lambda w: (index.get(w["id"], len(order)), w["id"]))
        return {**record, "widgets": widgets}


def delete_space(space_id: str) -> bool:
    if space_id == DEFAULT_SPACE_ID:
        raise ValueError("cannot delete the default space")
    with _lock:
        d = _space_dir(space_id)
        if not d.exists():
            return False
        shutil.rmtree(d, ignore_errors=False)
        _emit_locked(space_id, {"type": "space.deleted", "id": space_id})
        return True


# ---------------------------------------------------------------------------
# Widgets — public API
# ---------------------------------------------------------------------------

def _read_widgets_locked(space_id: str) -> List[Dict[str, Any]]:
    """Return widgets for a space. Caller must hold _lock."""
    widgets_dir = _space_dir(space_id) / "widgets"
    if not widgets_dir.exists():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(widgets_dir.glob("*.yaml")):
        record = _load_yaml(path)
        if record:
            out.append(record)
    return out


def _sync_widget_order_locked(
    space_id: str,
    space: Dict[str, Any],
    widgets: List[Dict[str, Any]],
    *,
    persist: bool = False,
) -> List[str]:
    """Return a widget_order that only contains existing widget ids.

    Older agent experiments left deleted ids in space.yaml. Keeping the
    manifest tidy makes API snapshots match what the browser can render.
    Caller must hold _lock.
    """
    existing_ids = {str(w.get("id")) for w in widgets if w.get("id")}
    ordered = [
        str(widget_id)
        for widget_id in (space.get("widget_order") or [])
        if str(widget_id) in existing_ids
    ]
    for widget_id in sorted(existing_ids):
        if widget_id not in ordered:
            ordered.append(widget_id)

    if persist and ordered != list(space.get("widget_order") or []):
        space["widget_order"] = ordered
        space["updated_at"] = _now_iso()
        _atomic_write(_space_yaml_path(space_id), _dump_yaml(space))

    return ordered


def list_widgets(space_id: str) -> List[Dict[str, Any]]:
    with _lock:
        widgets = _read_widgets_locked(space_id)
        space = _load_yaml(_space_yaml_path(space_id)) or {}
        order = _sync_widget_order_locked(space_id, space, widgets)
        index = {wid: i for i, wid in enumerate(order)}
        widgets.sort(key=lambda w: (index.get(w["id"], len(order)), w["id"]))
        return widgets



def update_space_background(space_id: str, background_url: str, background_type: Optional[str] = None) -> Dict[str, Any]:
    with _lock:
        s = _get_space_locked(space_id)
        if s is None:
            raise ValueError(f"Space {space_id} not found")
        
        s["background_url"] = background_url
        if background_type:
            s["background_type"] = background_type
        s["updated_at"] = datetime.now(timezone.utc).isoformat()
        
        space_path = SPACES_DIR / space_id / "space.yaml"
        temp_path = space_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(_dump_yaml(s))
        os.replace(temp_path, space_path)
        
        # Publish space.updated event
        _publish(space_id, {"type": "space.updated", "space": s})
        return s


def get_widget(space_id: str, widget_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        return _load_yaml(_widget_yaml_path(space_id, widget_id))


def _position_collides(
    space_id: str, pos: Dict[str, int], size: Dict[str, int],
    exclude_id: Optional[str] = None,
) -> bool:
    """Return True if a widget at `pos`/`size` would overlap any existing
    widget in `space_id`. Caller must hold _lock.
    """
    px, py = int(pos["x"]), int(pos["y"])
    pw, ph = int(size["w"]), int(size["h"])
    for w in _read_widgets_locked(space_id):
        if exclude_id and w.get("id") == exclude_id:
            continue
        try:
            wp, ws = w["position"], w["size"]
            wx, wy = int(wp["x"]), int(wp["y"])
            ww, wh = int(ws["w"]), int(ws["h"])
        except (KeyError, TypeError, ValueError):
            continue
        if px < wx + ww and px + pw > wx and py < wy + wh and py + ph > wy:
            return True
    return False


def _find_free_position(
    space_id: str, size: Dict[str, int], exclude_id: Optional[str] = None
) -> Dict[str, int]:
    """Pick a position where a widget of `size` won\'t overlap any existing
    widget in `space_id`. Sweeps row-major from origin out to a 30x30
    canvas; falls back to (0, 0) if nothing fits (very crowded canvas).
    Caller must hold _lock.
    """
    widgets = _read_widgets_locked(space_id)
    rects = []
    for w in widgets:
        if exclude_id and w.get("id") == exclude_id:
            continue
        try:
            p = w["position"]
            s = w["size"]
            rects.append((int(p["x"]), int(p["y"]), int(s["w"]), int(s["h"])))
        except (KeyError, TypeError, ValueError):
            continue

    sw, sh = int(size["w"]), int(size["h"])
    for y in range(0, 40):
        for x in range(0, 40):
            collides = False
            for (rx, ry, rw, rh) in rects:
                if x < rx + rw and x + sw > rx and y < ry + rh and y + sh > ry:
                    collides = True
                    break
            if not collides:
                return {"x": x, "y": y}
    return {"x": 0, "y": 0}


def upsert_widget(
    space_id: str,
    widget_id: str,
    *,
    title: str,
    renderer: str,
    position: Optional[Dict[str, int]] = None,
    size: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    _validate_id(space_id, "space")
    _validate_id(widget_id, "widget")
    if not isinstance(title, str):
        raise ValueError("title must be a string")
    if not isinstance(renderer, str):
        raise ValueError("renderer must be a string (JS source)")
    auto_place = position is None
    pos, siz = _validate_layout(position, size)

    with _lock:
        # Make sure the space exists. The default space is auto-created;
        # other spaces require an explicit create_space() first.
        if space_id == DEFAULT_SPACE_ID:
            _ensure_default_space()
        elif not _space_yaml_path(space_id).exists():
            raise ValueError(f"space does not exist: {space_id}")

        path = _widget_yaml_path(space_id, widget_id)
        existing = _load_yaml(path)
        if not existing:
            # New widget: ALWAYS verify the requested position
            # doesn't overlap. If it does (or no position given),
            # auto-relocate to a free spot. Saves the agent from
            # itself when it forgets to omit position or hardcodes
            # (0,0). Existing widgets keep their position.
            if auto_place or _position_collides(space_id, pos, siz):
                pos = _find_free_position(space_id, siz)
        now = _now_iso()
        record = {
            "id": widget_id,
            "title": title,
            "schema": WIDGET_SCHEMA,
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
            "position": pos,
            "size": siz,
            "renderer": renderer,
        }
        # Preserve a custom position/size if the caller did not pass one
        # but the existing record had non-default values. This is what
        # makes "patch the renderer" calls leave layout alone.
        if existing:
            if position is None:
                record["position"] = existing.get("position", record["position"])
            if size is None:
                record["size"] = existing.get("size", record["size"])

        _atomic_write(path, _dump_yaml(record))

        # Update space.widget_order — append if new.
        space = _load_yaml(_space_yaml_path(space_id)) or {}
        order = _sync_widget_order_locked(
            space_id, space, _read_widgets_locked(space_id), persist=False
        )
        if widget_id not in order:
            order.append(widget_id)
            space["widget_order"] = order
            space["updated_at"] = now
            _atomic_write(_space_yaml_path(space_id), _dump_yaml(space))

        _emit_locked(
            space_id,
            {"type": "widget.upserted", "space_id": space_id, "widget": dict(record)},
        )
        return record


def delete_widget(space_id: str, widget_id: str) -> bool:
    with _lock:
        path = _widget_yaml_path(space_id, widget_id)
        if not path.exists():
            return False
        try:
            path.unlink()
        except OSError as exc:
            logger.error("studio_widgets: unlink %s failed: %s", path, exc)
            return False

        # Remove from order.
        space = _load_yaml(_space_yaml_path(space_id))
        if space:
            order = [w for w in (space.get("widget_order") or []) if w != widget_id]
            space["widget_order"] = order
            space["updated_at"] = _now_iso()
            _atomic_write(_space_yaml_path(space_id), _dump_yaml(space))

        _emit_locked(
            space_id,
            {"type": "widget.deleted", "space_id": space_id, "id": widget_id},
        )
        return True


def set_layout(
    space_id: str,
    widget_id: str,
    *,
    position: Optional[Dict[str, int]] = None,
    size: Optional[Dict[str, int]] = None,
) -> Optional[Dict[str, Any]]:
    """Update only the position and/or size of a widget. Returns the
    updated record, or None if the widget does not exist.
    """
    if position is None and size is None:
        raise ValueError("set_layout requires position and/or size")

    with _lock:
        path = _widget_yaml_path(space_id, widget_id)
        existing = _load_yaml(path)
        if existing is None:
            return None
        if position is not None:
            pos, _ = _validate_layout(position, existing.get("size"))
            existing["position"] = pos
        if size is not None:
            _, siz = _validate_layout(existing.get("position"), size)
            existing["size"] = siz
        existing["updated_at"] = _now_iso()
        _atomic_write(path, _dump_yaml(existing))
        _emit_locked(
            space_id,
            {
                "type": "widget.position_changed",
                "space_id": space_id,
                "id": widget_id,
                "position": existing["position"],
                "size": existing["size"],
            },
        )
        return dict(existing)


# ---------------------------------------------------------------------------
# Subscribe — generator that yields events for a space.
# ---------------------------------------------------------------------------

def subscribe(
    space_id: str,
    *,
    timeout: Optional[float] = 30,
    replay: bool = True,
    follow: bool = True,
) -> Iterator[Dict[str, Any]]:
    """Yield events for `space_id` until terminal/timeout.

    Args:
        timeout: stop yielding after this many seconds idle (None = forever).
        replay: emit a synthetic "widget.upserted" event for each existing
            widget at subscribe time, so a fresh client can paint state.
        follow: keep yielding live events after the replay; if False, only
            the snapshot is returned.

    Subscribing to a non-existent space yields nothing.
    """
    q: Queue = Queue()
    with _lock:
        if not _space_yaml_path(space_id).exists():
            return
        snapshot: List[Dict[str, Any]] = []
        if replay:
            snapshot = _read_widgets_locked(space_id)
            space = _load_yaml(_space_yaml_path(space_id)) or {}
            order = _sync_widget_order_locked(space_id, space, snapshot)
            index = {wid: i for i, wid in enumerate(order)}
            snapshot.sort(key=lambda w: (index.get(w["id"], len(order)), w["id"]))
        if follow:
            _subscribers.setdefault(space_id, []).append(q)

    try:
        if replay:
            for w in snapshot:
                yield {
                    "type": "widget.upserted",
                    "space_id": space_id,
                    "widget": w,
                }

        if not follow:
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
    finally:
        if follow:
            with _lock:
                try:
                    _subscribers.get(space_id, []).remove(q)
                except (ValueError, KeyError):
                    pass


# ---------------------------------------------------------------------------
# Module init
# ---------------------------------------------------------------------------

def _init() -> None:
    STUDIO_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(STUDIO_DIR, 0o700)
    except (OSError, NotImplementedError):
        pass
    SPACES_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_default_space()


_init()
