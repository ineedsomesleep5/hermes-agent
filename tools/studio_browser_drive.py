"""Studio browser drive — agent tools that control the user's snapshot browser.

The snapshot widget in Studio renders a real Chrome instance running on this
VPS at port 9322 (managed by hermes-studio-browser systemd unit, backed by
agent-browser). This module exposes that SAME browser to the Hermes agent
via studio_browser_* tools.

When the agent calls one of these tools, the user *sees* the action happen
live in their snapshot widget. This is the Space Agent pattern: agent and
user share one browser.

Distinct from the agent's regular browser_* tools (tools/browser_tool.py),
which spawn task-scoped agent-browser sessions per agent task. Those are
isolated from what the user sees. studio_browser_* is the shared path.

Endpoints used (all proxied through the hermes-studio-browser shim at :9322):
  POST /navigate {url}          -> goto the URL, return new state
  POST /click  {x,y,...}        -> click at viewport coords
  POST /scroll {x,y,deltaX,deltaY}
  POST /type {text}             -> insert text via CDP Input.insertText
  POST /key {key}               -> press a key (Enter, Tab, etc.)
  GET  /state                   -> {url, title, viewport, image (base64 jpeg)}

Element-aware refs (@e1, @e2, ...) are NOT supported here — those would
require an ariaSnapshot endpoint on the shim. For ref-based interaction
the agent should use the regular browser_* tools instead. This module is
for "show me what the user sees + drive it via clicks/keys".
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from tools.registry import registry

SHIM_BASE = "http://127.0.0.1:9322"
TIMEOUT = 30.0


def _http(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(SHIM_BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"error": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"error": str(exc)}


def _ok(payload: Dict[str, Any]) -> str:
    """Return a compact summary the agent can act on. Strip the (huge) image
    field to keep token usage small — the user already sees the screen."""
    out = {k: v for k, v in payload.items() if k != "image"}
    if "image" in payload:
        out["image_bytes"] = len(payload["image"]) if isinstance(payload["image"], str) else 0
    return json.dumps(out)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def studio_browser_state(args: Dict[str, Any], **_kw) -> str:
    """Return current URL, title, viewport. Image is reported by size only."""
    return _ok(_http("GET", "/state"))


def studio_browser_navigate(args: Dict[str, Any], **_kw) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        return json.dumps({"error": "url required"})
    return _ok(_http("POST", "/navigate", {"url": url}))


def studio_browser_click(args: Dict[str, Any], **_kw) -> str:
    x = int(args.get("x", 0))
    y = int(args.get("y", 0))
    button = str(args.get("button", "left"))
    return _ok(_http("POST", "/click", {
        "x": x, "y": y, "displayWidth": 1280, "displayHeight": 820, "button": button,
    }))


def studio_browser_scroll(args: Dict[str, Any], **_kw) -> str:
    x = int(args.get("x", 640))
    y = int(args.get("y", 400))
    delta_y = int(args.get("delta_y", args.get("deltaY", 0)))
    delta_x = int(args.get("delta_x", args.get("deltaX", 0)))
    return _ok(_http("POST", "/scroll", {
        "x": x, "y": y, "deltaX": delta_x, "deltaY": delta_y,
        "displayWidth": 1280, "displayHeight": 820,
    }))


def studio_browser_type(args: Dict[str, Any], **_kw) -> str:
    text = str(args.get("text", ""))
    if not text:
        return json.dumps({"error": "text required"})
    return _ok(_http("POST", "/type", {"text": text}))


def studio_browser_key(args: Dict[str, Any], **_kw) -> str:
    key = str(args.get("key", ""))
    if not key:
        return json.dumps({"error": "key required (Enter, Tab, Backspace, etc.)"})
    return _ok(_http("POST", "/key", {"key": key}))


def studio_browser_back(args: Dict[str, Any], **_kw) -> str:
    return _ok(_http("POST", "/back", {}))


def studio_browser_forward(args: Dict[str, Any], **_kw) -> str:
    return _ok(_http("POST", "/forward", {}))


def studio_browser_reload(args: Dict[str, Any], **_kw) -> str:
    return _ok(_http("POST", "/reload", {}))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_SHARED_DESC_SUFFIX = (
    " Drives the SAME Chrome instance the user sees in their Studio "
    "embedded browser snapshot. The user watches every action live."
)

registry.register(
    name="studio_browser_state",
    toolset="studio",
    schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "description": "Get current URL, title, and viewport of the Studio shared browser." + _SHARED_DESC_SUFFIX,
    },
    handler=lambda args, **kw: studio_browser_state(args, **kw),
    check_fn=lambda: True,
    description="Read current state of the Studio shared browser.",
)

registry.register(
    name="studio_browser_navigate",
    toolset="studio",
    schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to navigate to (with or without https://)."},
        },
        "required": ["url"],
        "additionalProperties": False,
        "description": "Navigate the Studio shared browser to a URL." + _SHARED_DESC_SUFFIX,
    },
    handler=lambda args, **kw: studio_browser_navigate(args, **kw),
    check_fn=lambda: True,
    description="Navigate the Studio shared browser.",
)

registry.register(
    name="studio_browser_click",
    toolset="studio",
    schema={
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "Click x coord (0-1280)."},
            "y": {"type": "integer", "description": "Click y coord (0-820)."},
            "button": {"type": "string", "enum": ["left", "right"], "default": "left"},
        },
        "required": ["x", "y"],
        "additionalProperties": False,
        "description": "Click at viewport coords in the shared browser." + _SHARED_DESC_SUFFIX,
    },
    handler=lambda args, **kw: studio_browser_click(args, **kw),
    check_fn=lambda: True,
    description="Click in the Studio shared browser.",
)

registry.register(
    name="studio_browser_scroll",
    toolset="studio",
    schema={
        "type": "object",
        "properties": {
            "x": {"type": "integer", "default": 640},
            "y": {"type": "integer", "default": 400},
            "delta_y": {"type": "integer", "description": "Scroll amount in px (positive = down)."},
            "delta_x": {"type": "integer", "default": 0},
        },
        "required": ["delta_y"],
        "additionalProperties": False,
        "description": "Scroll the shared browser viewport." + _SHARED_DESC_SUFFIX,
    },
    handler=lambda args, **kw: studio_browser_scroll(args, **kw),
    check_fn=lambda: True,
    description="Scroll the Studio shared browser.",
)

registry.register(
    name="studio_browser_type",
    toolset="studio",
    schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to insert into the focused element."},
        },
        "required": ["text"],
        "additionalProperties": False,
        "description": "Insert text into the focused element of the shared browser. Click an input first to focus it." + _SHARED_DESC_SUFFIX,
    },
    handler=lambda args, **kw: studio_browser_type(args, **kw),
    check_fn=lambda: True,
    description="Type into the Studio shared browser.",
)

registry.register(
    name="studio_browser_key",
    toolset="studio",
    schema={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Key name: Enter, Tab, Backspace, Escape, Arrow*, etc."},
        },
        "required": ["key"],
        "additionalProperties": False,
        "description": "Press a single key in the shared browser." + _SHARED_DESC_SUFFIX,
    },
    handler=lambda args, **kw: studio_browser_key(args, **kw),
    check_fn=lambda: True,
    description="Press a key in the Studio shared browser.",
)

registry.register(
    name="studio_browser_back",
    toolset="studio",
    schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "description": "Go back one history entry in the shared browser." + _SHARED_DESC_SUFFIX,
    },
    handler=lambda args, **kw: studio_browser_back(args, **kw),
    check_fn=lambda: True,
    description="Browser back.",
)

registry.register(
    name="studio_browser_forward",
    toolset="studio",
    schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "description": "Go forward one history entry." + _SHARED_DESC_SUFFIX,
    },
    handler=lambda args, **kw: studio_browser_forward(args, **kw),
    check_fn=lambda: True,
    description="Browser forward.",
)

registry.register(
    name="studio_browser_reload",
    toolset="studio",
    schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "description": "Reload the current page." + _SHARED_DESC_SUFFIX,
    },
    handler=lambda args, **kw: studio_browser_reload(args, **kw),
    check_fn=lambda: True,
    description="Browser reload.",
)
