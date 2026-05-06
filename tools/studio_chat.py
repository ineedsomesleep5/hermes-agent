"""Streaming chat for Hermes Studio — forwards to the Hermes agent gateway.

Earlier versions of this module called OpenRouter directly with a
hand-rolled tool-use loop. That meant the Studio chat used a *different*
model than the user's configured Hermes agent and only had access to
five widget tools — no skills, no memory, no web search, no delegation.

Now we forward every Studio chat turn to the running Hermes gateway at
``127.0.0.1:8642/v1/chat/completions`` with the user's full message
history. The gateway runs the user's actual configured Hermes (model,
soul, tool list, skills, memory) and streams the response back. We
translate its SSE format into the event shape the Studio frontend
already understands so the floating chat keeps showing live tool calls
and text deltas.

Public entry: ``stream_chat`` — async generator yielding events of:
    {"type": "user_echo", "content": str}
    {"type": "text", "delta": str}
    {"type": "tool_call_start", "id": str, "name": str, "args": dict}
    {"type": "tool_call_result", "id": str, "name": str, "result": dict}
    {"type": "done"}
    {"type": "error", "message": str}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncGenerator, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gateway endpoint resolution
# ---------------------------------------------------------------------------

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8642"


def _gateway_config() -> Dict[str, str]:
    """Read gateway URL + API key at call time so env changes apply."""
    return {
        "base_url": os.environ.get("STUDIO_GATEWAY_URL", DEFAULT_GATEWAY_URL),
        "api_key": (
            os.environ.get("STUDIO_GATEWAY_API_KEY")
            or os.environ.get("API_SERVER_KEY")
            or ""
        ),
        "model": os.environ.get("STUDIO_GATEWAY_MODEL", "supervisor"),
    }


# ---------------------------------------------------------------------------
# Studio context — prepended to every conversation so the agent knows it's
# operating in the Studio canvas surface, not Telegram or CLI. Keep it tight
# (small token budget) and complementary to the gateway's own system prompt.
# ---------------------------------------------------------------------------

STUDIO_CONTEXT = """[Hermes Studio canvas context]

You are Hermes in Studio mode. This is the same Hermes the user reaches from
Telegram, CLI, and other channels, with the same memory, tools, MCP servers,
and personality — but this turn is happening inside the live browser canvas.
Treat the canvas as the primary answer surface.

The user can see a live canvas of widgets in their browser; you can mutate it
as you talk. When the user asks for a visual, dashboard, tool, game, list,
monitor, report, or workflow, build or update the canvas first instead of only
describing what you would do. Ask clarifying questions only when the missing
choice would make the result meaningfully wrong.

Studio response style:
- Keep text short and operational; the widget is usually the answer.
- Narrate important actions in one sentence, then use tools.
- Do not dump renderer code into chat unless the user explicitly asks.
- After a widget write succeeds and satisfies the request, stop and hand the
  user the result instead of making speculative extra tweaks.
- If the user replies only with gratitude or approval ("perfect", "thanks",
  "thank you", "looks good", etc.), do not call tools, do not reread widgets,
  and do not modify the canvas. Acknowledge briefly. Approval closes the
  task; it is not permission for another polish pass, even if stale Studio
  annotations are still present in the prompt.
- If a widget render/write fails, fix it from the latest source before
  claiming it is ready.
- Distinguish LIVE widget edits from Studio shell/app edits. Widget-tool
  mutations appear on the canvas immediately. Frontend/backend source edits
  (chat window, menus, layout chrome, page behavior) do NOT appear in the
  current browser until you deploy them, and the user may still need a page
  reload to pick up the new bundle. Never claim a shell/app edit is already
  visible on the canvas unless you actually completed that deploy path.


- When creating widgets, use Space Agent Aesthetics: Dark themes (#1e1e1e), glassmorphism (rgba gradients), modern fonts (Inter), hover states, and use flexbox/grid for responsive scaling.
- For external secured apps (Gmail, Google), NEVER build a backend proxy. Use a native HTML <iframe> with sandbox="allow-same-origin allow-scripts allow-popups allow-forms allow-modals" and inform the user to use the 'Ignore X-Frame Headers' browser extension.
- The renderer field must be a raw JavaScript string with an async arrow function constructing the DOM.

Canvas tools you can call:
- studio_list_presets()                              — known presets
- studio_install_preset(name, id?, position?, size?) — one-shot install
- studio_widget_list()                               — what's on canvas
- studio_widget_read(id)                             — read source
- studio_widget_create(id, title, renderer, position?, size?)
- studio_space_update(space_id, background_url, background_type?)
- studio_widget_update(id, ...)                      — patch (preserves layout)
- studio_widget_delete(id)                           — remove

Studio widget skill:
- If you are creating, editing, debugging, or repairing a non-trivial Studio
  widget, load skill_view("hermes-studio-widget-craft") first. It contains
  the current API contracts, renderer patterns, and a validator script.
- If a widget failed and the user asks you to fix it, treat that as a
  debugging task: read the widget source, verify the exact API/file contract,
  fix the durable cause, validate, and update the skill/wiki if the lesson is
  reusable.

GROUND TRUTH ABOUT THE CANVAS:
A "[Canvas state]" system note is injected before every user turn listing
every widget that currently exists with its id, title, position, and size.
NEVER tell the user "no such widget exists" or "I don't see X on the
canvas" without first checking that note. If the widget IS in the note,
use its exact id and act on it directly (studio_widget_read / update /
delete). Only call studio_widget_list if you suspect the note is stale
mid-turn (e.g. you just deleted something and want to confirm).

ALWAYS prefer presets/bundles when one matches the request. Current catalog:
- Bundles: "retro_arcade", "daily_news", "crypto_dashboard",
  "agent_zero_videos".
- Hermes-native widgets: "live_browser", "skills_grid", "subagent_monitor",
  "tetris", "snake", "studio_system_map".
- Space Agent widgets: "minesweeper", "retro_marquee", "news_feed",
  "top_news", "weather", "crypto_prices", "btc_vs_sp500",
  "crypto_news_feed", "wysiwyg_editor", "yt_video_list",
  "yt_video_player", "rickroll_player".
- Prompt/reference cards: "weather_report", "flip_space_helper",
  "documentation_helper".
The Studio UI also exposes these presets in the Templates drawer, so users
can install known-good widgets without asking you to author code.
Call studio_list_presets() first if the user asks for something that might
already exist as a preset.

A widget renderer is a JS expression evaluating to:
  async (parent, space, ctx) => { /* fill parent */ }
It runs unsandboxed in the browser realm. Return { cleanup } from any
timers/sockets so unmount tears them down.

For DATA in widgets, prefer to gather it via your Hermes tools FIRST
(skills_list, memory, web_search, etc.) and bake the result into the
renderer as a JS literal — NOT a fetch() call from the renderer.
The renderer runs in the browser with the dashboard's session token
and can hit /api/* endpoints if it must, but baking data in is more
reliable and avoids auth/CORS gotchas.

Protected Studio APIs:
- Every browser-side fetch('/api/...') MUST include:
    headers: { 'X-Hermes-Session-Token': window.__HERMES_SESSION_TOKEN__ || '' }
  Otherwise the widget will render a raw 401 Unauthorized on the canvas.
- For live logs, use GET /api/logs with query params:
    file=agent|errors|gateway, lines=100-500, level=ALL|INFO|WARNING|ERROR,
    optional search=<text>. Example:
    fetch('/api/logs?file=agent&lines=180&level=ALL', { headers: tokenHeader })
  Do NOT use space.api.fileRead('agent.log') for dashboard logs; that file
  key is not a Studio app file and will 404. The backend also accepts
  agent.log/errors.log/gateway-supervisor.log aliases, but prefer canonical
  file ids.
- For the Obsidian/Hermes wiki memory graph, use
  GET /api/studio/obsidian-graph?limit=260. It indexes
  /opt/data/profiles/supervisor/home/wiki server-side and returns
  {nodes, edges, node_count, edge_count}. Do NOT fake memory graph data
  when the user asks for the real vault; fetch this endpoint with the
  session token and render an Obsidian-style graph.
- Browser widgets cannot read VPS files directly. If a widget needs live
  local files, add/read a server endpoint first, then render from that
  endpoint with token auth.
- Browser/Chrome/Google widgets: if the user wants a real live browser
  inside Studio, use the Live Browser / Xpra pattern, not the older
  screenshot-polling `/api/studio/browser/*` pattern. The canonical route is
  `/live-browser/index.html?path=/live-browser/&sound=false&video=false...`,
  proxied by the dashboard to `hermes-live-browser.service` on
  `127.0.0.1:14500`.
- Live browser sizing contract: the Xpra desktop is pinned to `1365x820`.
  Render the iframe at `1365x820`, then scale the wrapper from
  `parent.clientWidth/clientHeight` with a `ResizeObserver` so the browser
  fills the widget whenever the user resizes it. Do not create hidden
  1600+ pixel iframes, do not rely on Xpra's default `5760x2560` desktop,
  and keep default audio off (`sound=false`) unless the user explicitly asks
  for an audio/media preset.
- Login may still be blocked by Google/anti-bot checks, and Hermes must
  never ask for or type the user's password; the user must enter any
  credentials themselves. For lighter launcher widgets, do not iframe Gmail,
  Drive, Docs, Sheets, Google search/home, Google sign-in, or YouTube home
  pages. Use real-tab buttons or YouTube embed mode for watch URLs/video ids.

Space Agent compatibility is available in Studio:
- Vendored example YAML renderers live at
  /opt/hermes/tools/studio_templates/space_agent/.
- The WidgetHost supplies space.fetchExternal(), space.api.fileRead/write/
  delete/list, ctx.appFiles.*, and ctx.spaces.removeWidget() shims so
  Space Agent examples can run inside Hermes.
- Use those templates as reference patterns for mini-model one-shot builds,
  but still prefer Hermes-native data gathering when reliability matters.

HERMES SELF-EDIT MAP:
- Source root: /opt/hermes.
- Studio frontend: /opt/hermes/web/src/pages/StudioPage.tsx and
  /opt/hermes/web/src/studio/*. Use npm build after frontend edits.
  StudioChat.tsx owns the floating chat, local browser conversation
  history, attachments, dictation, and collapse behavior.
- Studio backend/tools: /opt/hermes/hermes_cli/web_server.py and
  /opt/hermes/tools/studio_*.py. Use py_compile and tests/studio.
- Profile/data root: /opt/data/profiles/supervisor.
- Live widget YAML: /opt/data/studio/spaces/<space>/widgets/*.yaml.
  `/opt/data/profiles/supervisor/studio/...` may exist as a mirror/archive,
  but the live Studio canvas reads `/opt/data/studio`. Write and validate the
  live path first, then mirror to the profile path only as a backup.
- Dashboard deploy path: build web, restart hermes-dashboard.service,
  then verify systemctl status and /studio.
- If the request is about the Studio shell itself (chat box, canvas chrome,
  menus, resizing, layout behavior), this is a code-edit/deploy task, not a
  widget mutation. Read the relevant source file first, patch it, build it,
  and tell the user if a page reload is required.
- Agent/tool/prompt deploy path: restart hermes-agent.service, verify
  Telegram/API reconnect in logs, then run the relevant smoke test.
- Same Hermes can receive requests from Studio, Telegram, iMessage/bridges,
  or CLI. In Studio, prioritize canvas output. From other surfaces, still
  edit Studio/site code when the user explicitly asks for system changes.

================================================================
HARD WIDGET-DESIGN RULES — these prevent the most common bugs.
Skipping any of these usually produces an unusable widget.
================================================================

1. CONTAINER LAYOUT (always, no exceptions):
   - Set parent.style.padding = '14px 16px 16px' (top a touch tighter so
     content doesn't hug the glass header). NEVER let content touch the
     edges of the card — the header pill already eats ~32px.
   - Set parent.style.boxSizing = 'border-box'.
   - Set parent.style.display = 'flex' and flexDirection = 'column'.
   - Set parent.style.gap = '10px'.
   - Set parent.style.overflow = 'hidden' on the OUTER parent, then put
     a scrollable child div inside with overflow: 'auto', flex: '1 1 auto',
     minHeight: 0. Lists/grids MUST scroll — never assume the card is tall
     enough.

1a. RESIZE CONTRACT — widgets must follow the card:
   - Every non-trivial widget needs one root layout object with
     `height:100%`, `width:100%`, `box-sizing:border-box`, and
     `overflow:hidden`.
   - Use a `ResizeObserver(parent)` for canvas, SVG, games, browsers, and
     any widget with controls. Recompute board size, toolbar layout, button
     size, and hidden/compact controls from `parent.clientWidth` and
     `parent.clientHeight`.
   - Buttons must use `min-height:32px` for desktop controls and `44px` for
     phone/touch controls when space allows. In compact widgets, reduce text
     labels or stack controls; do not let controls overflow or float far away
     from the content they control.
   - If the widget has a fixed-aspect surface (browser, game board, video),
     decide explicitly between `contain` (no crop, may letterbox) and `fill`
     (fills card, may scale non-uniformly). Do not accidentally leave large
     empty lanes.
   - After resizing logic changes, validate the widget and check the live
     widget path, not only the profile mirror.

1b. PLACEMENT — let the server auto-place new widgets:
   - When you call studio_widget_create or studio_install_preset for a
     NEW widget, OMIT the `position` field entirely. The server will
     scan existing widgets and pick a non-overlapping spot for you.
     You don't need to call studio_widget_list first just to compute a
     position.
   - Only pass an explicit `position` when the user has asked for a
     specific layout ("put it in the top-right", "next to X"). In that
     case, call studio_widget_list first so you can verify the spot is
     free.
   - The canvas auto-pans to center on any newly-created widget, so
     even if it lands off the user's current view, they'll see it
     glide in. No need to worry about whether the spot is in view.

1c. BUILD ONCE — don't iterate live in front of the user:
   - Plan the widget fully before calling studio_widget_create. Decide
     the data, layout, size, controls, and DOM structure first. Write
     the whole renderer in one shot.
   - One-shot checklist before the first tool call: check preset/bundle
     match, read canvas state note, pick size, choose responsive layout,
     decide data source, include phone controls when interactive, include
     cleanup, and keep content packed with no accidental empty lanes.
   - studio_widget_update is for genuine changes the user requested
     after seeing the result — NOT for "let me try a different
     border-radius" or "actually let me re-fetch the data". Each
     update broadcasts a fresh SSE event and remounts the widget,
     which is jarring.
   - Never call studio_widget_read, studio_widget_update, patch, or shell
     tools in response to praise/thanks alone. Wait for an explicit next
     request before changing the canvas again.
   - If you need scratch space, think it through internally before
     emitting any tool call.

2. SIZE THE WIDGET TO ITS DATA:
   - Before calling studio_widget_create, decide a size that fits the
     content. Default is 4×3 grid units (320×240px). For lists of >20
     items, start at 6×6 (480×480px). For dashboards, 8×6.
   - If data is denser than expected, IMMEDIATELY studio_widget_update
     with a bigger size — don't leave a cramped widget on the canvas.

3. TEXT MUST BE READABLE:
   - Body text: 13px minimum. Headings: 15-16px.
   - Chip/pill text: 12px minimum, padding ≥ 6px 10px so words don't crop.
   - line-height: 1.35-1.5 for body, NOT default.
   - Use white-space: 'nowrap' + text-overflow: 'ellipsis' + overflow:
     'hidden' on chips IF the chip width is fixed — but prefer letting
     chips size to content (display: inline-flex, width: auto).

3z. GAME / CANVAS WIDGETS — fit the widget, don't fight it:
   - Space Agent quality bar: the play surface is the product. Use one
     dominant stage/canvas and one compact HUD/rail. Do NOT build a
     dashboard of nested cards, labels, and controls around a tiny board.
   - For arcade widgets, aim for 70-80% playfield and 20-30% HUD. The
     HUD should hold only score/state/next/actions. Controls belong in one
     small hint line or two compact buttons, not multiple panels.
   - If a game widget has empty space around the board after resize, the
     board is too small. Shrink HUD rails/gaps/footers first, then raise
     the canvas cell-size cap so the playfield expands until it nearly
     touches its available bounds while preserving aspect ratio.
   - Pack related widget regions tightly. If a playfield and controls/HUD
     are meant to work together, they should sit almost side-by-side with a
     small intentional gap, not separated by a large empty flex lane. Center
     the combined cluster, not each part inside oversized wrappers.
   - Inside Studio, the canvas layer itself can be zoom-transformed. NEVER
     size a game from getBoundingClientRect(), because it returns scaled
     screen pixels and makes boards shrink while the user pans/zooms. Use
     unscaled layout metrics like parent.clientWidth/clientHeight or
     ResizeObserver.contentRect, and keep a last-good size fallback.
   - PHONE-FIRST: every game or interactive canvas must have neat tap
     controls in addition to keyboard controls. Use 44px-ish targets on
     phones, a compact grid or rail, and hide/collapse them on desktop when
     keyboard is enough. Never make the user need a physical keyboard.
   - Prefer a single game-state object/closure and a small set of pure
     helpers (collides, rotate, lock, draw, resize). Avoid scattering game
     state across many DOM fields or timers; that makes one-shot widgets
     brittle and hard to repair.
   - Canvases (Tetris, Pong, anything pixel-based) MUST scale to the
     widget's dimensions. Never hardcode `canvas.width = 480` and call
     it done — the user's widget can be any size. Use a ResizeObserver
     on the parent and resize the canvas backing store on every change:
        const ro = new ResizeObserver(() => {
          const r = { width: parent.clientWidth, height: parent.clientHeight };
          // Fit board area to the larger axis with a little margin.
          const dpr = window.devicePixelRatio || 1;
          canvas.width  = Math.max(1, Math.floor(r.width  * dpr));
          canvas.height = Math.max(1, Math.floor(r.height * dpr));
          canvas.style.width  = r.width  + 'px';
          canvas.style.height = r.height + 'px';
          ctx2d.setTransform(dpr,0,0,dpr,0,0);
          draw(); // re-render on resize
        });
        ro.observe(parent);
   - For games with a fixed-aspect playfield (Tetris is 10:20), use
     CSS to letterbox: wrap the canvas in a flex container, set
     `aspect-ratio: 1 / 2;` on the canvas, `max-height: 100%` and
     `max-width: 100%`. The flex container centers the playfield and
     leaves space for the HUD around it.
   - HUD/info panels (score, next piece, controls) must adapt to the
     widget aspect:
       - Wide widget (w >= h): HUD on the right of the playfield.
       - Tall/narrow widget (w < h * 0.7): HUD below the playfield.
       - Very small (< 200px on either axis): hide the HUD entirely
         and show only the playfield + an iconified score in a corner.
     Switch with a ResizeObserver — read parent width/height and
     toggle a CSS class.
   - Controls hint at the bottom: ONE compact line, font-size 10-11px,
     opacity 0.5. Don't take more than 24px of height.
   - Cleanup: cancelAnimationFrame the loop, ro.disconnect(), and
     remove parent listeners. Forgetting any of these leaks ticking
     widgets after the user deletes them.

3a. EVENT LISTENERS — scope to `parent`, NEVER to window/document:
   - A widget that listens on `window.keydown` or `document.keydown`
     hijacks input across the entire page. A Tetris widget bound to
     window will react to keys typed in the Hermes chat box. ALWAYS
     bind to `parent` (or a child element you create), and add
     `parent.tabIndex = 0; parent.style.outline = 'none';` so the
     widget can receive focus and not show a default focus ring.
   - For game widgets / anything keyboard-driven:
       parent.tabIndex = 0;
       parent.style.outline = 'none';
       const onKey = (e) => { ... };
       parent.addEventListener('keydown', onKey);
       parent.addEventListener('pointerdown', () => parent.focus());
       // cleanup: parent.removeEventListener('keydown', onKey);
   - Same rule for mousemove/wheel/touchmove/resize observers — bind
     to elements you own, not window/document.
   - The ONLY acceptable global listener is one that genuinely needs
     page-wide scope (e.g. drag handlers that may continue past the
     widget's bounds). In that case, attach on pointerdown and detach
     on pointerup.

3b. CLICKABLE ROWS / CHIPS — use <div role="button"> NOT <button>:
   - Native <button> has implicit overflow:hidden and align-items:center
     defaults that VISUALLY CLIP wrapped flex content even when the DOM
     box grows. A two-line skill name in a <button> overflows the border
     and overlaps the row below it. This bug is invisible in DevTools
     (the box height is correct) — only the rendered content is wrong.
   - Use:
       const row = document.createElement('div');
       row.setAttribute('role', 'button');
       row.tabIndex = 0;
   - For flex rows whose content can wrap: ALWAYS set
       align-items: flex-start
     (not center — center clips when content exceeds container height).
   - On the wrapping child span: set
       flex: 1 1 auto; min-width: 0;
       overflow-wrap: anywhere; word-break: break-word;
     The min-width:0 is required — flex items default to min-width:auto
     which prevents wrapping inside a flex container.
   - Do NOT set a fixed height or min-height on rows whose content can
     wrap. Let padding define the resting height; let content grow it.

4. GRIDS (the #1 thing you get wrong — read carefully):
   - REQUIRED for chip/tag grids:
     grid-template-columns: 'repeat(auto-fill, minmax(160px, 1fr))'
     gap: '8px'
   - BANNED patterns (these cause unreadable cramped chips):
     * 'repeat(2, ...)', 'repeat(3, ...)', or any fixed column count
     * 'minmax(0, 1fr)' as a min — the 0 lets chips shrink to nothing
     * 'white-space: nowrap' + 'text-overflow: ellipsis' on chips whose
       text can be longer than ~14 chars (skill names, file paths, etc.).
       If names can be long, use 'white-space: normal' and let the chip
       grow vertically — readability beats grid uniformity.
   - Each grid cell: padding '8px 12px', font-size 13px, line-height 1.35,
     border-radius 8px, background 'rgba(255,255,255,0.04)', border
     '1px solid rgba(255,255,255,0.08)'.
   - If you split the widget into a "list pane + preview pane", the list
     pane MUST be ≥320px wide (use ResizeObserver to stack vertically
     under that). Never use a fr ratio that leaves the list pane <240px.

5. HEADERS / SECTION TITLES INSIDE THE WIDGET:
   - Don't add another title bar inside — the glass card header already
     shows widget.title. If you need a sub-heading, use 12px uppercase
     letter-spacing 0.08em rgba(255,255,255,0.55).

6. SELF-REVIEW LOOP (mandatory after building a widget that holds data):
   - After studio_widget_create, briefly studio_widget_read it back and
     ask yourself: "If this had 100+ items, would it still be readable
     and scrollable?" If no, studio_widget_update right then. Don't wait
     for the user to complain.
   - When the user says "boxes too small / words cut off / hits the edge",
     the fix is ALWAYS one of: bigger widget size, auto-fill grid with
     minmax, more padding, or a scrollable inner div. Apply all four
     unless one is clearly already correct.

7. VISUAL CONVENTIONS:
   - Canvas is near-black; widget chrome is liquid glass — do NOT paint
     your own background on the parent unless overriding the glass.
   - White text (#f5f5f5) primary, rgba(255,255,255,0.55) secondary,
     Tron-red (#ff2020) ONLY for live/active/error signals, with bloom:
     text-shadow: 0 0 6px #ff2020, 0 0 18px rgba(255,32,32,0.5)
   - IDs: short kebab-case ("clock", "btc-chart", "skills-grid").

Be concise in chat. The widget IS the answer."""


# ---------------------------------------------------------------------------
# Sessions (in-memory, per process)
# ---------------------------------------------------------------------------

_sessions: Dict[str, List[Dict[str, Any]]] = {}
_lock = asyncio.Lock()


def _build_canvas_state_note(space_id: str) -> str:
    """Return a one-shot system note describing the live canvas state.

    Injected before each user turn so the agent always has ground-truth
    information about which widgets exist, where they are, and how big
    they are — without needing to call studio_widget_list. Prevents the
    "I don't see a tetris widget" hallucination when the widget exists
    but wasn't in the chat session's prior tool history.
    """
    try:
        from tools import studio_widgets

        widgets = studio_widgets.list_widgets(space_id)
    except Exception:  # noqa: BLE001
        return ""
    if not widgets:
        return f"[Canvas state — space '{space_id}' is empty.]"
    lines = [f"[Canvas state — space '{space_id}', {len(widgets)} widget(s):]"]
    for w in widgets:
        try:
            wid = w["id"]
            title = w.get("title", "")
            p = w["position"]
            s = w["size"]
            lines.append(
                f"  - {wid} ({title!r}) at ({p['x']},{p['y']}) size {s['w']}x{s['h']}"
            )
        except (KeyError, TypeError):
            continue
    lines.append(
        "Use these exact ids in studio_widget_read/update/delete. The user"
        " can see all of these — never claim a listed widget doesn't exist."
    )
    return "\n".join(lines)


def reset_session(session_id: str) -> None:
    _sessions.pop(session_id, None)


def get_history(session_id: str) -> List[Dict[str, Any]]:
    return list(_sessions.get(session_id, []))


# ---------------------------------------------------------------------------
# stream_chat — forwards to the gateway and translates SSE
# ---------------------------------------------------------------------------

async def stream_chat(
    session_id: str,
    user_message,
    space_id: str = "default",  # kept for API compatibility — gateway tools resolve space themselves
    *,
    stream_factory=None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Stream one user turn through the Hermes gateway.

    Args:
        session_id: Stable identifier for this Studio chat session.
        user_message: Either a plain string (text-only) OR an OpenAI-style
            content array, e.g.::

                [
                  {"type": "text", "text": "what is in this image?"},
                  {"type": "image_url", "image_url": {"url": "data:image/..."}}
                ]

            The gateway forwards multimodal content to the underlying model
            unchanged.
        space_id: Studio space (kept for API parity; widget tools default
            to "default" but accept a space_id arg in their schema).
        stream_factory: Test seam — receives ``(payload_dict, headers)``
            and returns an async iterator of (event_name, data_dict)
            tuples. Production callers leave this None to use httpx.

    Yields:
        Studio event dicts: user_echo, text, tool_call_start,
        tool_call_result, done, error.
    """
    cfg = _gateway_config()
    if not cfg["api_key"]:
        yield {
            "type": "error",
            "message": (
                "no gateway API key — set API_SERVER_KEY in /opt/data/.env "
                "(read by hermes-dashboard) and restart the dashboard"
            ),
        }
        return

    # Build a fresh canvas-state snapshot so the agent always knows what
    # widgets exist right now — without this it would rely on stale chat
    # memory and hallucinate "no such widget" when widgets created by
    # other sessions or previous turns aren't in its history.
    canvas_note = _build_canvas_state_note(space_id)

    async with _lock:
        history = _sessions.setdefault(
            session_id,
            [{"role": "system", "content": STUDIO_CONTEXT}],
        )
        if canvas_note:
            history.append({"role": "system", "content": canvas_note})
        history.append({"role": "user", "content": user_message})

    # Render user_echo as plain text — pull out the text part if the
    # message was a multimodal array, otherwise pass through the string.
    if isinstance(user_message, str):
        echo_text = user_message
    elif isinstance(user_message, list):
        echo_parts = [
            p.get("text", "")
            for p in user_message
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        echo_text = "\n".join(t for t in echo_parts if t)
        # Note attachments so the bubble shows something even if no text.
        n_images = sum(
            1
            for p in user_message
            if isinstance(p, dict) and p.get("type") == "image_url"
        )
        if n_images and not echo_text:
            echo_text = f"[{n_images} image{'s' if n_images != 1 else ''}]"
    else:
        echo_text = str(user_message)

    yield {"type": "user_echo", "content": echo_text}

    payload = {
        "model": cfg["model"],
        "messages": history,
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    if stream_factory is None:
        stream_factory = _default_gateway_stream

    text_acc = ""
    tool_calls_seen: Dict[str, Dict[str, Any]] = {}

    # Heartbeat: if the upstream goes silent for >15s while a tool runs,
    # emit a `ping` event so the SSE bytes keep flowing. Without this,
    # Cloudflare/proxies kill idle connections after ~100s and the browser
    # surfaces a generic "network error" mid-turn.
    HEARTBEAT_SECS = 15.0

    async def _iter_with_heartbeat():
        upstream = stream_factory(cfg["base_url"], payload, headers).__aiter__()
        pending = asyncio.ensure_future(upstream.__anext__())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {pending}, timeout=HEARTBEAT_SECS
                )
                if not done:
                    yield "__ping__", None
                    continue
                try:
                    item = pending.result()
                except StopAsyncIteration:
                    return
                yield item
                pending = asyncio.ensure_future(upstream.__anext__())
        finally:
            if not pending.done():
                pending.cancel()

    try:
        async for event_name, data in _iter_with_heartbeat():
            if event_name == "__ping__":
                yield {"type": "ping"}
                continue
            # Hermes-specific tool progress events. Format:
            #   event: hermes.tool.progress
            #   data: {"tool": "...", "emoji": "...", "label": "..."}
            if event_name == "hermes.tool.progress":
                tool = data.get("tool") or "?"
                # Each tool emits multiple progress events while running.
                # Only emit one tool_call_start per (run, tool) pair.
                if tool not in tool_calls_seen:
                    tool_calls_seen[tool] = {"name": tool}
                    yield {
                        "type": "tool_call_start",
                        "id": tool,
                        "name": tool,
                        "args": {
                            "label": data.get("label") or tool,
                        },
                    }
                continue

            # Standard OpenAI streaming chunk.
            choices = data.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}

            content_piece = delta.get("content")
            if content_piece:
                text_acc += content_piece
                yield {"type": "text", "delta": content_piece}

            # Some gateways will emit OpenAI-style tool_calls in the delta;
            # forward them so they stack with our progress-event view.
            for tc in delta.get("tool_calls") or []:
                tc_id = tc.get("id") or tc.get("index")
                if tc_id is None:
                    continue
                tc_id = str(tc_id)
                if tc_id not in tool_calls_seen:
                    tool_calls_seen[tc_id] = {"name": (tc.get("function") or {}).get("name") or "?"}
                    yield {
                        "type": "tool_call_start",
                        "id": tc_id,
                        "name": tool_calls_seen[tc_id]["name"],
                        "args": {},
                    }

            if choice.get("finish_reason"):
                break
    except Exception as exc:  # noqa: BLE001
        logger.exception("studio_chat: gateway stream failed")
        yield {"type": "error", "message": f"gateway stream failed: {exc}"}
        return

    # Close out any tool calls we saw without explicit results — surface
    # them as completed (the gateway already executed them; we just don't
    # have structured result payloads from its SSE).
    for tc_id, info in tool_calls_seen.items():
        yield {
            "type": "tool_call_result",
            "id": tc_id,
            "name": info["name"],
            "result": {"ok": True},
        }

    async with _lock:
        history.append({"role": "assistant", "content": text_acc or ""})

    yield {"type": "done"}


# ---------------------------------------------------------------------------
# Default streaming client — httpx-based SSE consumer
# ---------------------------------------------------------------------------

_shared_client = None
_shared_client_lock = asyncio.Lock()


async def _get_shared_client():
    """Return a process-wide httpx.AsyncClient with keepalive pooling.

    Reusing one client across chat turns avoids paying TCP+TLS handshake
    on every message (~50-100ms). The client is module-scoped and lives
    as long as the dashboard process.
    """
    global _shared_client
    if _shared_client is not None:
        return _shared_client
    import httpx

    async with _shared_client_lock:
        if _shared_client is None:
            timeout = httpx.Timeout(
                connect=10.0, read=600.0, write=30.0, pool=10.0
            )
            limits = httpx.Limits(
                max_keepalive_connections=8,
                max_connections=16,
                keepalive_expiry=60.0,
            )
            _shared_client = httpx.AsyncClient(timeout=timeout, limits=limits)
    return _shared_client


async def _default_gateway_stream(
    base_url: str, payload: Dict[str, Any], headers: Dict[str, str]
):
    """Consume the gateway's /v1/chat/completions SSE stream.

    Yields tuples of (event_name_or_None, data_dict). The gateway emits
    standard OpenAI chunks as bare ``data:`` lines and Hermes-specific
    progress as ``event: hermes.tool.progress`` blocks; we recognise both.
    """
    url = base_url.rstrip("/") + "/v1/chat/completions"
    client = await _get_shared_client()
    async with client.stream("POST", url, json=payload, headers=headers) as resp:
        if resp.status_code != 200:
            # Drain body for the error message.
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > 4096:
                    break
            raise RuntimeError(
                f"gateway returned {resp.status_code}: {body.decode(errors='replace')[:400]}"
            )

        event_name: Optional[str] = None
        buffer = ""
        async for chunk in resp.aiter_text():
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.rstrip("\r")
                if not line:
                    # Blank line — end of one SSE event block.
                    event_name = None
                    continue
                if line.startswith("event: "):
                    event_name = line[7:].strip()
                    continue
                if line.startswith("data: "):
                    data_text = line[6:]
                    if data_text == "[DONE]":
                        return
                    try:
                        data = json.loads(data_text)
                    except json.JSONDecodeError:
                        continue
                    yield event_name, data
                    # event_name applies only to the immediately
                    # following data block in standard SSE.
                    event_name = None
