"""Hermes Studio widget tools — registry-side wrappers.

Promotes the five widget CRUD operations from being studio-chat-only
plumbing to first-class Hermes agent tools. Once registered + allowlisted
in ``toolsets.py``, *any* Hermes session — Telegram, dashboard /chat,
web, CLI — can mutate the Studio canvas.

The dashboard's internal ``studio_chat.py`` keeps its own dispatch so the
Studio /studio page has a streaming UX without going through the gateway,
but the underlying canvas storage (``tools.studio_widgets``) is shared, so
both paths produce identical SSE events on the canvas.

Tool names mirror the rest of the codebase's verb-last convention (cf.
``delegate_task_background``):

    studio_widget_create
    studio_widget_update
    studio_widget_delete
    studio_widget_list
    studio_widget_read
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from tools import studio_widgets
from tools.registry import registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handlers — small, predictable JSON in / JSON out
# ---------------------------------------------------------------------------

def _ok(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _err(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


def _coerce_layout(value: Optional[Any], keys: tuple[str, ...]) -> Optional[Dict[str, int]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"expected object, got {type(value).__name__}")
    out: Dict[str, int] = {}
    for k in keys:
        if k not in value:
            raise ValueError(f"missing {k!r}")
        v = value[k]
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError(f"{k} must be an integer")
        out[k] = v
    return out


def studio_widget_create(args: Dict[str, Any], **_) -> str:
    """Create or replace a widget on a Studio space."""
    try:
        space_id = args.get("space_id") or "default"
        widget_id = args.get("id")
        title = args.get("title")
        renderer = args.get("renderer")
        if not widget_id or not title or not renderer:
            return _err("id, title, and renderer are required")
        position = _coerce_layout(args.get("position"), ("x", "y"))
        size = _coerce_layout(args.get("size"), ("w", "h"))
        rec = studio_widgets.upsert_widget(
            space_id,
            widget_id,
            title=title,
            renderer=renderer,
            position=position,
            size=size,
        )
        return _ok(
            {
                "ok": True,
                "id": rec["id"],
                "space_id": space_id,
                "position": rec["position"],
                "size": rec["size"],
            }
        )
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("studio_widget_create failed")
        return _err(str(exc))


def studio_widget_update(args: Dict[str, Any], **_) -> str:
    """Patch an existing widget's title and/or renderer (preserves layout)."""
    try:
        space_id = args.get("space_id") or "default"
        widget_id = args.get("id")
        if not widget_id:
            return _err("id is required")
        existing = studio_widgets.get_widget(space_id, widget_id)
        if existing is None:
            return _err(f"widget not found: {widget_id}")
        rec = studio_widgets.upsert_widget(
            space_id,
            widget_id,
            title=args.get("title", existing["title"]),
            renderer=args.get("renderer", existing["renderer"]),
        )
        return _ok({"ok": True, "id": rec["id"], "space_id": space_id})
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("studio_widget_update failed")
        return _err(str(exc))


def studio_widget_delete(args: Dict[str, Any], **_) -> str:
    """Remove a widget from the canvas."""
    try:
        space_id = args.get("space_id") or "default"
        widget_id = args.get("id")
        if not widget_id:
            return _err("id is required")
        ok = studio_widgets.delete_widget(space_id, widget_id)
        return _ok({"ok": bool(ok), "id": widget_id, "space_id": space_id})
    except Exception as exc:  # noqa: BLE001
        logger.exception("studio_widget_delete failed")
        return _err(str(exc))


def studio_widget_list(args: Dict[str, Any], **_) -> str:
    """List widgets on a Studio space (compact view — no renderer source)."""
    try:
        space_id = args.get("space_id") or "default"
        widgets = studio_widgets.list_widgets(space_id)
        return _ok(
            {
                "space_id": space_id,
                "widgets": [
                    {
                        "id": w["id"],
                        "title": w["title"],
                        "position": w["position"],
                        "size": w["size"],
                        "updated_at": w.get("updated_at"),
                    }
                    for w in widgets
                ],
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("studio_widget_list failed")
        return _err(str(exc))


def studio_widget_read(args: Dict[str, Any], **_) -> str:
    """Return the full widget record including renderer source."""
    try:
        space_id = args.get("space_id") or "default"
        widget_id = args.get("id")
        if not widget_id:
            return _err("id is required")
        rec = studio_widgets.get_widget(space_id, widget_id)
        if rec is None:
            return _err(f"widget not found: {widget_id}")
        return _ok(rec)
    except Exception as exc:  # noqa: BLE001
        logger.exception("studio_widget_read failed")
        return _err(str(exc))


# ---------------------------------------------------------------------------
# OpenAI-style schemas — fed to the model on every turn
# ---------------------------------------------------------------------------

_RENDERER_DESC = (
    "JavaScript expression that evaluates to: "
    "async (parent, space, ctx) => { ... }. The function fills `parent` "
    "(a DOM node) and may return { cleanup: () => ... } for any timers."
)

CREATE_SCHEMA = {
    "name": "studio_widget_create",
    "description": (
        "Create or replace a widget on a Hermes Studio canvas. "
        "The browser canvas updates live via SSE."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "space_id": {
                "type": "string",
                "description": "Target space (default: 'default').",
            },
            "id": {
                "type": "string",
                "description": "Short kebab-case widget id (e.g. 'clock').",
            },
            "title": {"type": "string"},
            "renderer": {"type": "string", "description": _RENDERER_DESC},
            "position": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
            },
            "size": {
                "type": "object",
                "properties": {
                    "w": {"type": "integer", "minimum": 1},
                    "h": {"type": "integer", "minimum": 1},
                },
            },
        },
        "required": ["id", "title", "renderer"],
        "additionalProperties": False,
    },
}

UPDATE_SCHEMA = {
    "name": "studio_widget_update",
    "description": "Patch an existing widget's title and/or renderer (layout preserved).",
    "parameters": {
        "type": "object",
        "properties": {
            "space_id": {"type": "string"},
            "id": {"type": "string"},
            "title": {"type": "string"},
            "renderer": {"type": "string", "description": _RENDERER_DESC},
        },
        "required": ["id"],
        "additionalProperties": False,
    },
}

DELETE_SCHEMA = {
    "name": "studio_widget_delete",
    "description": "Remove a widget from the Studio canvas.",
    "parameters": {
        "type": "object",
        "properties": {
            "space_id": {"type": "string"},
            "id": {"type": "string"},
        },
        "required": ["id"],
        "additionalProperties": False,
    },
}

LIST_SCHEMA = {
    "name": "studio_widget_list",
    "description": "List widgets on a Studio space (compact view, no renderer).",
    "parameters": {
        "type": "object",
        "properties": {"space_id": {"type": "string"}},
        "additionalProperties": False,
    },
}

READ_SCHEMA = {
    "name": "studio_widget_read",
    "description": "Read a widget's full record (including renderer source).",
    "parameters": {
        "type": "object",
        "properties": {
            "space_id": {"type": "string"},
            "id": {"type": "string"},
        },
        "required": ["id"],
        "additionalProperties": False,
    },
}


def check_studio_requirements() -> bool:
    return True


# ---------------------------------------------------------------------------
# Registry — top-level calls so tools.registry.discover_builtin_tools picks
# this module up automatically (the AST scanner only finds module-level
# registry.register(...) statements).
# ---------------------------------------------------------------------------

registry.register(
    name="studio_widget_create",
    toolset="studio",
    schema=CREATE_SCHEMA,
    handler=lambda args, **kw: studio_widget_create(args, **kw),
    check_fn=check_studio_requirements,
    description=CREATE_SCHEMA["description"],
)

registry.register(
    name="studio_widget_update",
    toolset="studio",
    schema=UPDATE_SCHEMA,
    handler=lambda args, **kw: studio_widget_update(args, **kw),
    check_fn=check_studio_requirements,
    description=UPDATE_SCHEMA["description"],
)

registry.register(
    name="studio_widget_delete",
    toolset="studio",
    schema=DELETE_SCHEMA,
    handler=lambda args, **kw: studio_widget_delete(args, **kw),
    check_fn=check_studio_requirements,
    description=DELETE_SCHEMA["description"],
)

registry.register(
    name="studio_widget_list",
    toolset="studio",
    schema=LIST_SCHEMA,
    handler=lambda args, **kw: studio_widget_list(args, **kw),
    check_fn=check_studio_requirements,
    description=LIST_SCHEMA["description"],
)

registry.register(
    name="studio_widget_read",
    toolset="studio",
    schema=READ_SCHEMA,
    handler=lambda args, **kw: studio_widget_read(args, **kw),
    check_fn=check_studio_requirements,
    description=READ_SCHEMA["description"],
)

@registry.register(
    "Update the Studio canvas background to an image, video, or color.",
    space_id="Space ID (usually 'default')",
    background_url="URL of the video/image, or a CSS color string (#000000, rgb(0,0,0)).",
    background_type="Optional. Set to 'video', 'image', or 'color'. Auto-detects if omitted.",
)
def studio_space_update(
    space_id: str, background_url: str, background_type: Optional[str] = None
) -> str:
    try:
        s = studio_widgets.update_space_background(space_id, background_url, background_type)
        return _ok({"id": s["id"], "background_url": s.get("background_url")})
    except ValueError as e:
        return _error(str(e))
