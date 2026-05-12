"""First-class iframe browser workbench for Hermes Studio.

This installs the lightweight iframe-first browser surface as a Studio widget.
Sites that block iframe embedding open in the user's real device browser.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from tools import studio_widgets
from tools.registry import registry
from tools.studio_embedded_browser import EMBEDDED_BROWSER_RENDERER

logger = logging.getLogger(__name__)


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


def studio_install_embedded_browser(args: Dict[str, Any], **_) -> str:
    """Install the embedded browser widget into a Studio space."""
    try:
        space_id = str(args.get("space_id") or "default")
        widget_id = str(args.get("id") or "embedded-browser")
        title = str(args.get("title") or "Embedded Browser")
        position = _coerce_layout(args.get("position"), ("x", "y"))
        size = _coerce_layout(args.get("size"), ("w", "h"))
        rec = studio_widgets.upsert_widget(
            space_id,
            widget_id,
            title=title,
            renderer=EMBEDDED_BROWSER_RENDERER,
            position=position,
            size=size,
        )
        return _ok(
            {
                "ok": True,
                "space_id": space_id,
                "id": rec["id"],
                "title": rec["title"],
                "position": rec["position"],
                "size": rec["size"],
                "description": "Iframe-first embedded browser. Frame-blocked or login-heavy sites open in the user's real device browser.",
            }
        )
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("studio_install_embedded_browser failed")
        return _err(str(exc))


registry.register(
    name="studio_install_embedded_browser",
    toolset="studio",
    schema={
        "type": "object",
        "properties": {
            "space_id": {"type": "string", "description": "Studio space id", "default": "default"},
            "id": {"type": "string", "description": "Widget id", "default": "embedded-browser"},
            "title": {"type": "string", "description": "Widget title", "default": "Embedded Browser"},
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
                    "w": {"type": "integer"},
                    "h": {"type": "integer"},
                },
            },
        },
        "additionalProperties": False,
        "description": "Install the embedded browser workbench widget into a Studio space.",
    },
    handler=lambda args, **kw: studio_install_embedded_browser(args, **kw),
    check_fn=lambda: True,
    description="Install the iframe-first embedded browser workbench widget into a Studio space.",
)
