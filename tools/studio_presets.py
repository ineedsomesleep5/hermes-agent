"""Pre-baked widget renderers for Hermes Studio.

These are reference widgets the agent (or a user) can install with one
tool call — typically the agent will author one-off widgets from
scratch, but for things like the subagent-job monitor it's better to ship
a known-good renderer than to ask the model to re-derive it every time.

Add new presets by appending an entry to ``PRESETS``. Each entry is a
dict with ``title``, ``renderer`` (JS source as a string), and optional
``size``/``position`` defaults. The agent calls
``studio_install_preset(name=..., id=..., space_id=...)`` to install one.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from tools import studio_widgets
from tools.registry import registry

logger = logging.getLogger(__name__)

SPACE_AGENT_TEMPLATE_ROOT = Path(__file__).resolve().parent / "studio_templates" / "space_agent"
CANONICAL_STUDIO_WIDGET_ROOT = Path(os.environ.get("HERMES_HOME", "/opt/data")) / "studio" / "spaces"


def _template_card_renderer(
    title: str,
    subtitle: str,
    items: list[str],
    accent: str = "#ff2020",
) -> str:
    """Small resilient card used for prompt-style templates and fallbacks."""
    return f"""async (parent, space, ctx) => {{
  const title = {json.dumps(title)};
  const subtitle = {json.dumps(subtitle)};
  const items = {json.dumps(items)};
  const accent = {json.dumps(accent)};
  parent.style.cssText = `
    display:flex;flex-direction:column;height:100%;box-sizing:border-box;
    padding:16px;color:#f5f5f5;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
    overflow:hidden;background:radial-gradient(circle at top left, ${{accent}}22, transparent 42%);
  `;
  const head = document.createElement('div');
  head.style.cssText = 'display:flex;flex-direction:column;gap:7px;margin-bottom:12px;';
  const eyebrow = document.createElement('div');
  eyebrow.textContent = 'Hermes Studio Template';
  eyebrow.style.cssText = `font-size:10px;letter-spacing:0.18em;text-transform:uppercase;color:${{accent}};`;
  const h = document.createElement('div');
  h.textContent = title;
  h.style.cssText = 'font-size:20px;font-weight:800;letter-spacing:-0.04em;line-height:1.05;';
  const sub = document.createElement('div');
  sub.textContent = subtitle;
  sub.style.cssText = 'font-size:12px;line-height:1.45;color:rgba(255,255,255,0.68);';
  head.append(eyebrow, h, sub);
  const list = document.createElement('div');
  list.style.cssText = 'flex:1 1 auto;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:8px;';
  for (const item of items) {{
    const row = document.createElement('div');
    row.style.cssText = 'border:1px solid rgba(255,255,255,0.1);background:rgba(255,255,255,0.045);border-radius:11px;padding:10px 11px;font-size:12px;line-height:1.42;color:rgba(255,255,255,0.86);';
    row.textContent = item;
    list.append(row);
  }}
  parent.append(head, list);
}}"""


def _load_space_agent_renderer(relative_path: str, fallback: str) -> str:
    """Load a vendored Space Agent onboarding renderer, or use fallback."""
    path = SPACE_AGENT_TEMPLATE_ROOT / relative_path
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        renderer = data.get("renderer")
        if isinstance(renderer, str) and renderer.strip():
            return renderer
    except Exception:  # noqa: BLE001
        logger.warning("failed to load Space Agent template %s", path, exc_info=True)
    return fallback


def _space_agent_widget(
    relative_path: str,
    title: str,
    subtitle: str,
    items: list[str],
    accent: str,
) -> str:
    return _load_space_agent_renderer(
        relative_path,
        _template_card_renderer(title, subtitle, items, accent),
    )


def _canonical_widget_renderer(space_id: str, widget_id: str, fallback: str) -> str:
    """Load a known-good live Studio widget renderer for use as a preset."""
    path = CANONICAL_STUDIO_WIDGET_ROOT / space_id / "widgets" / f"{widget_id}.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        renderer = data.get("renderer")
        if isinstance(renderer, str) and renderer.strip():
            return renderer
    except Exception:  # noqa: BLE001
        logger.warning("failed to load canonical Studio widget %s", path, exc_info=True)
    return fallback


# ---------------------------------------------------------------------------
# Preset renderers
# ---------------------------------------------------------------------------

# Sub-agent monitor — polls /api/studio/delegations every 2s, draws a list.
SUBAGENT_MONITOR_RENDERER = r"""async (parent, space, ctx) => {
  parent.style.cssText = `
    display: flex;
    flex-direction: column;
    height: 100%;
    padding: 14px 16px;
    box-sizing: border-box;
    color: #f5f5f5;
    font-family: -apple-system, "Inter", sans-serif;
    font-size: 0.78rem;
    overflow: hidden;
  `;
  const head = document.createElement('div');
  head.style.cssText = 'display:flex;align-items:center;gap:8px;font-size:0.65rem;letter-spacing:0.12em;text-transform:uppercase;color:rgba(255,255,255,0.55);margin-bottom:10px;flex:0 0 auto;';
  const dot = document.createElement('span');
  dot.style.cssText = 'width:8px;height:8px;border-radius:50%;background:#ff2020;box-shadow:0 0 6px #ff2020,0 0 18px rgba(255,32,32,0.5);animation:studio-pulse 1.6s ease-in-out infinite;';
  const label = document.createElement('span');
  label.textContent = 'Subagents';
  const count = document.createElement('span');
  count.style.cssText = 'margin-left:auto;color:rgba(255,255,255,0.4);';
  count.textContent = '—';
  head.append(dot, label, count);

  const list = document.createElement('div');
  list.style.cssText = 'flex:1 1 auto;min-height:0;overflow-y:auto;display:flex;flex-direction:column;gap:6px;';
  parent.append(head, list);

  const fmtTime = (iso) => {
    if (!iso) return '';
    try {
      const t = new Date(iso);
      return t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    } catch (_) { return ''; }
  };

  const colorFor = (status) => {
    const s = (status || '').toLowerCase();
    if (s === 'running') return '#ff2020';
    if (s === 'queued' || s === 'cancelling') return 'rgba(255,255,255,0.5)';
    if (s === 'completed') return 'rgba(255,255,255,0.85)';
    if (s === 'failed') return '#ff2020';
    if (s === 'cancelled') return 'rgba(255,255,255,0.4)';
    return 'rgba(255,255,255,0.55)';
  };

  const render = (jobs) => {
    count.textContent = `${jobs.length} job${jobs.length === 1 ? '' : 's'}`;
    list.innerHTML = '';
    if (jobs.length === 0) {
      const empty = document.createElement('div');
      empty.style.cssText = 'color:rgba(255,255,255,0.35);font-size:0.72rem;letter-spacing:0.06em;padding:8px 0;';
      empty.textContent = 'no background subagents';
      list.append(empty);
      return;
    }
    for (const j of jobs) {
      const row = document.createElement('div');
      row.style.cssText = 'padding:8px 10px;border-radius:8px;background:rgba(255,255,255,0.04);display:flex;flex-direction:column;gap:4px;';
      const top = document.createElement('div');
      top.style.cssText = 'display:flex;align-items:center;gap:8px;';
      const status = document.createElement('span');
      status.textContent = (j.status || '?').toUpperCase();
      status.style.cssText = `font-size:0.6rem;letter-spacing:0.1em;color:${colorFor(j.status)};font-family:ui-monospace,SF Mono,Menlo,monospace;`;
      if ((j.status || '').toLowerCase() === 'running') {
        status.style.textShadow = '0 0 6px rgba(255,32,32,0.6)';
      }
      const id = document.createElement('span');
      id.textContent = (j.job_id || '').slice(0, 8);
      id.style.cssText = 'font-size:0.65rem;color:rgba(255,255,255,0.45);font-family:ui-monospace,SF Mono,Menlo,monospace;';
      const time = document.createElement('span');
      time.style.cssText = 'margin-left:auto;font-size:0.65rem;color:rgba(255,255,255,0.35);';
      time.textContent = fmtTime(j.created_at);
      top.append(status, id, time);

      const goal = document.createElement('div');
      goal.style.cssText = 'font-size:0.78rem;color:rgba(255,255,255,0.85);line-height:1.35;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;';
      const goalText = (j.goal || j.tasks?.[0]?.goal || j.task_count || '').toString();
      goal.textContent = goalText.slice(0, 240) || '(no goal)';

      row.append(top, goal);
      list.append(row);
    }
  };

  let alive = true;
  const tick = async () => {
    if (!alive) return;
    try {
      const res = await fetch('/api/studio/delegations?limit=20', {
        headers: { 'X-Hermes-Session-Token': window.__HERMES_SESSION_TOKEN__ || '' },
      });
      if (res.ok) {
        const jobs = await res.json();
        render(Array.isArray(jobs) ? jobs : []);
      }
    } catch (_) { /* ignore — keep ticking */ }
  };
  tick();
  const interval = setInterval(tick, 2500);
  return { cleanup: () => { alive = false; clearInterval(interval); } };
}"""

# Skills grid — auto-fill chips with hover preview pane. The renderer
# fetches /api/skills via the dashboard session token so it stays in sync
# without the agent re-baking data on every install.
SKILLS_GRID_RENDERER = r"""async (parent, space, ctx) => {
  parent.style.cssText = `
    display: flex;
    flex-direction: column;
    height: 100%;
    padding: 14px 18px 16px;
    box-sizing: border-box;
    color: #f5f5f5;
    font-family: -apple-system, "Inter", sans-serif;
    overflow: hidden;
    gap: 10px;
  `;

  const head = document.createElement('div');
  head.style.cssText = 'display:flex;align-items:baseline;gap:10px;flex:0 0 auto;';
  const title = document.createElement('div');
  title.style.cssText = 'font-size:0.95rem;font-weight:600;letter-spacing:0.01em;';
  title.textContent = 'Hermes Skills';
  const sub = document.createElement('div');
  sub.style.cssText = 'font-size:0.7rem;color:rgba(255,255,255,0.5);';
  sub.textContent = 'hover a chip to preview';
  const count = document.createElement('div');
  count.style.cssText = 'margin-left:auto;font-size:0.65rem;letter-spacing:0.1em;color:rgba(255,255,255,0.45);font-family:ui-monospace,monospace;';
  count.textContent = '— skills';
  head.append(title, sub, count);

  const body = document.createElement('div');
  body.style.cssText = 'flex:1 1 auto;min-height:0;display:grid;grid-template-columns:minmax(220px, 1fr) minmax(240px, 1fr);gap:12px;overflow:hidden;';

  // Vertical scrollable list — one skill per row. Capped visible count
  // via the parent's height; everything else scrolls. No grid means no
  // chip-overlap regardless of widget size.
  const grid = document.createElement('div');
  grid.style.cssText = 'overflow-y:auto;display:flex;flex-direction:column;gap:4px;padding-right:6px;';

  const preview = document.createElement('div');
  preview.style.cssText = 'overflow-y:auto;padding:12px 14px;border-radius:10px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);font-size:0.78rem;line-height:1.5;color:rgba(255,255,255,0.85);white-space:pre-wrap;';
  preview.textContent = 'Hover a skill →';

  body.append(grid, preview);
  parent.append(head, body);

  // Responsive: when the widget is narrow, stack preview under the grid.
  const ro = new ResizeObserver((entries) => {
    for (const e of entries) {
      if (e.contentRect.width < 380) {
        body.style.gridTemplateColumns = '1fr';
      } else {
        body.style.gridTemplateColumns = '1fr 240px';
      }
    }
  });
  ro.observe(parent);

  let alive = true;

  const renderSkills = (skills) => {
    count.textContent = `${skills.length} skill${skills.length === 1 ? '' : 's'}`;
    grid.innerHTML = '';
    for (const s of skills) {
      // Use a div (not <button>) — button has implicit overflow:hidden
      // and align-items:center that visually clip wrapped content even
      // though the DOM box grows. div + role/tabindex behaves predictably.
      const chip = document.createElement('div');
      chip.setAttribute('role', 'button');
      chip.tabIndex = 0;
      // Single full-width row: name on left, dim category on right. Long
      // names wrap naturally — align-items:flex-start so the row grows
      // downward with the text instead of overflowing the box.
      chip.style.cssText = 'cursor:pointer;display:flex;align-items:flex-start;gap:10px;padding:8px 12px;border-radius:8px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);font-size:0.8rem;line-height:1.4;color:rgba(255,255,255,0.92);transition:background 120ms ease,border-color 120ms ease;box-sizing:border-box;height:auto;';
      const nameEl = document.createElement('span');
      nameEl.textContent = s.name || s.id || '?';
      nameEl.style.cssText = 'flex:1 1 auto;min-width:0;overflow-wrap:anywhere;word-break:break-word;';
      const metaEl = document.createElement('span');
      metaEl.textContent = s.category || '';
      metaEl.style.cssText = 'flex:0 0 auto;font-size:0.65rem;letter-spacing:0.08em;text-transform:uppercase;color:rgba(255,255,255,0.4);font-family:ui-monospace,monospace;padding-top:2px;';
      chip.append(nameEl, metaEl);
      chip.title = s.name || '';
      const onHover = () => {
        chip.style.background = 'rgba(255,32,32,0.08)';
        chip.style.borderColor = 'rgba(255,32,32,0.35)';
        const desc = s.description ? `**Description:** ${s.description}\n` : '';
        const cat = s.category ? `**Category:** ${s.category}\n` : '';
        const usage = s.usage ? `**Usage:** ${s.usage}\n` : '';
        const body = s.body || s.snippet || '';
        preview.textContent = `# ${s.name || s.id}\n\n${desc}${cat}${usage}${body ? '\n' + body.slice(0, 1200) : ''}`.trim();
      };
      const onLeave = () => {
        chip.style.background = 'rgba(255,255,255,0.04)';
        chip.style.borderColor = 'rgba(255,255,255,0.08)';
      };
      chip.addEventListener('mouseenter', onHover);
      chip.addEventListener('focus', onHover);
      chip.addEventListener('mouseleave', onLeave);
      chip.addEventListener('blur', onLeave);
      grid.append(chip);
    }
  };

  try {
    const res = await fetch('/api/skills', {
      headers: { 'X-Hermes-Session-Token': window.__HERMES_SESSION_TOKEN__ || '' },
    });
    if (res.ok) {
      const data = await res.json();
      const list = Array.isArray(data) ? data : (data.skills || data.items || []);
      if (alive) renderSkills(list);
    } else {
      preview.textContent = `Failed to load skills (HTTP ${res.status})`;
    }
  } catch (e) {
    preview.textContent = `Failed to load skills: ${e.message}`;
  }

  return { cleanup: () => { alive = false; ro.disconnect(); } };
}"""

TETRIS_RENDERER = r"""async (parent, space, ctx) => {
  parent.style.cssText = `
    position: absolute; inset: 0;
    display: flex; flex-direction: column;
    padding: 12px 14px 10px;
    box-sizing: border-box;
    color: #f5f5f5;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    overflow: hidden;
    outline: none;
  `;
  parent.tabIndex = 0;
  parent.style.touchAction = 'none';
  parent.addEventListener('pointerdown', () => parent.focus());
  setTimeout(() => parent.focus(), 0);

  // Layout: [board | hud] when wide, [board / hud] when tall, [board only] when small
  const stage = document.createElement('div');
  stage.style.cssText = 'flex:1 1 auto; min-height:0; display:flex; gap:8px; overflow:hidden;';
  parent.appendChild(stage);

  const boardWrap = document.createElement('div');
  boardWrap.style.cssText = 'flex:1 1 auto; min-width:0; min-height:0; display:flex; align-items:center; justify-content:center; position:relative;';
  stage.appendChild(boardWrap);

  const canvas = document.createElement('canvas');
  canvas.style.cssText = 'background:rgba(255,255,255,0.02); border:1px solid rgba(255,255,255,0.08); border-radius:6px; image-rendering:pixelated;';
  boardWrap.appendChild(canvas);
  const cx = canvas.getContext('2d');

  // Floating HUD pieces — re-parented based on layout class.
  const hud = document.createElement('div');
  hud.style.cssText = 'flex:0 0 auto; display:flex; flex-direction:column; gap:10px; min-width:0;';
  const makeStat = (label) => {
    const wrap = document.createElement('div');
    wrap.style.cssText = 'background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.08); border-radius:8px; padding:8px 10px; display:flex; flex-direction:column; gap:2px;';
    const l = document.createElement('div');
    l.style.cssText = 'font-size:9px; letter-spacing:0.14em; text-transform:uppercase; color:rgba(255,255,255,0.45);';
    l.textContent = label;
    const v = document.createElement('div');
    v.style.cssText = 'font-size:18px; font-weight:600; color:#f5f5f5; line-height:1;';
    v.textContent = '0';
    wrap.appendChild(l); wrap.appendChild(v);
    return { wrap, v };
  };
  const scoreEl = makeStat('Score');
  const linesEl = makeStat('Lines');
  const levelEl = makeStat('Level');
  const nextWrap = document.createElement('div');
  nextWrap.style.cssText = 'background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.08); border-radius:8px; padding:8px 10px;';
  const nextLabel = document.createElement('div');
  nextLabel.style.cssText = 'font-size:9px; letter-spacing:0.14em; text-transform:uppercase; color:rgba(255,255,255,0.45); margin-bottom:6px;';
  nextLabel.textContent = 'Next';
  const nextCanvas = document.createElement('canvas');
  nextCanvas.style.cssText = 'display:block; width:100%; max-width:80px; aspect-ratio:1/1; image-rendering:pixelated;';
  nextWrap.appendChild(nextLabel); nextWrap.appendChild(nextCanvas);
  const nx = nextCanvas.getContext('2d');
  hud.appendChild(scoreEl.wrap);
  hud.appendChild(linesEl.wrap);
  hud.appendChild(levelEl.wrap);
  hud.appendChild(nextWrap);

  // Tiny score corner for small mode.
  const corner = document.createElement('div');
  corner.style.cssText = 'position:absolute; top:6px; right:8px; font-size:11px; font-weight:600; color:#f5f5f5; text-shadow:0 1px 2px rgba(0,0,0,0.6); pointer-events:none; display:none;';
  boardWrap.appendChild(corner);

  const footer = document.createElement('div');
  footer.style.cssText = 'flex:0 0 auto; height:14px; font-size:9px; letter-spacing:0.04em; color:rgba(255,255,255,0.36); text-align:center; padding-top:3px;';
  footer.textContent = 'A/D move - W rotate - S soft drop - Space hard drop - P pause - R restart';
  parent.appendChild(footer);

  const touchControls = document.createElement('div');
  touchControls.style.cssText = 'flex:0 0 auto; display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:7px; padding-top:8px;';
  const makeTouchButton = (label, actionName, accent = false) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = label;
    btn.setAttribute('aria-label', label);
    btn.style.cssText = `min-height:44px;border-radius:11px;border:1px solid ${accent ? 'rgba(255,32,32,0.35)' : 'rgba(255,255,255,0.12)'};background:${accent ? 'rgba(255,32,32,0.14)' : 'rgba(255,255,255,0.055)'};color:#f5f5f5;font:700 12px/1 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:0.04em;touch-action:manipulation;-webkit-tap-highlight-color:transparent;`;
    const fire = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      parent.focus();
      gameAction(actionName);
    };
    btn.addEventListener('pointerdown', fire);
    touchControls.appendChild(btn);
    return btn;
  };
  makeTouchButton('LEFT', 'left');
  makeTouchButton('ROTATE', 'rotate', true);
  makeTouchButton('RIGHT', 'right');
  makeTouchButton('DROP', 'hard', true);
  makeTouchButton('PAUSE', 'pause');
  makeTouchButton('DOWN', 'down');
  makeTouchButton('RESTART', 'restart');
  makeTouchButton('FOCUS', 'focus');

  // ── Game state ────────────────────────────────────────────────────────
  const COLS = 10, ROWS = 20;
  const COLORS = {
    I: '#5ec5ff', O: '#ffd95e', T: '#c46bff', S: '#5eff8a',
    Z: '#ff6b6b', J: '#7a8cff', L: '#ffa15e',
  };
  const SHAPES = {
    I: [[0,0,0,0],[1,1,1,1],[0,0,0,0],[0,0,0,0]],
    O: [[1,1],[1,1]],
    T: [[0,1,0],[1,1,1],[0,0,0]],
    S: [[0,1,1],[1,1,0],[0,0,0]],
    Z: [[1,1,0],[0,1,1],[0,0,0]],
    J: [[1,0,0],[1,1,1],[0,0,0]],
    L: [[0,0,1],[1,1,1],[0,0,0]],
  };
  const TYPES = Object.keys(SHAPES);

  let grid, current, nextType, score, lines, level, dropAccum, dropInterval, paused, gameOver, animation;
  function blank() { return Array.from({length: ROWS}, () => Array(COLS).fill(null)); }
  function newPiece(type) {
    const m = SHAPES[type].map(row => row.slice());
    return { type, m, x: Math.floor(COLS/2 - m[0].length/2), y: type === 'I' ? -1 : 0 };
  }
  function rotate(m, dir) {
    const N = m.length, out = Array.from({length:N}, () => Array(N).fill(0));
    for (let y=0;y<N;y++) for (let x=0;x<N;x++) {
      if (dir === 1) out[x][N-1-y] = m[y][x];
      else out[N-1-x][y] = m[y][x];
    }
    return out;
  }
  function collides(p) {
    for (let y=0;y<p.m.length;y++) for (let x=0;x<p.m[0].length;x++) {
      if (!p.m[y][x]) continue;
      const gx = p.x + x, gy = p.y + y;
      if (gx < 0 || gx >= COLS || gy >= ROWS) return true;
      if (gy >= 0 && grid[gy][gx]) return true;
    }
    return false;
  }
  function lock() {
    for (let y=0;y<current.m.length;y++) for (let x=0;x<current.m[0].length;x++) {
      if (!current.m[y][x]) continue;
      const gy = current.y + y;
      if (gy < 0) { gameOver = true; return; }
      grid[gy][current.x + x] = current.type;
    }
    let cleared = 0;
    for (let y=ROWS-1;y>=0;y--) {
      if (grid[y].every(c => c)) {
        grid.splice(y, 1); grid.unshift(Array(COLS).fill(null)); cleared++; y++;
      }
    }
    if (cleared) {
      const points = [0, 100, 300, 500, 800][cleared] * (level);
      score += points; lines += cleared;
      level = 1 + Math.floor(lines / 10);
      dropInterval = Math.max(80, 800 - (level-1) * 70);
    }
    current = newPiece(nextType);
    nextType = TYPES[Math.floor(Math.random()*TYPES.length)];
    if (collides(current)) gameOver = true;
  }
  function softDrop() {
    current.y++;
    if (collides(current)) { current.y--; lock(); }
    dropAccum = 0;
  }
  function hardDrop() {
    while (!collides(current)) current.y++;
    current.y--; lock(); dropAccum = 0;
  }
  function tryRotate(dir) {
    const m = rotate(current.m, dir);
    const test = { ...current, m };
    for (const dx of [0, -1, 1, -2, 2]) {
      test.x = current.x + dx;
      if (!collides(test)) { current.m = m; current.x = test.x; return; }
    }
  }
  function move(dx) {
    current.x += dx;
    if (collides(current)) current.x -= dx;
  }
  function restart() {
    grid = blank();
    nextType = TYPES[Math.floor(Math.random()*TYPES.length)];
    current = newPiece(TYPES[Math.floor(Math.random()*TYPES.length)]);
    nextType = TYPES[Math.floor(Math.random()*TYPES.length)];
    score = 0; lines = 0; level = 1;
    dropAccum = 0; dropInterval = 800;
    paused = false; gameOver = false;
    updateHUD();
  }
  function updateHUD() {
    scoreEl.v.textContent = score;
    linesEl.v.textContent = lines;
    levelEl.v.textContent = level;
    corner.textContent = score;
  }

  function gameAction(kind) {
    if (kind === 'focus') { draw(); return; }
    if (kind === 'restart' || (gameOver && kind === 'restart')) {
      restart(); draw(); return;
    }
    if (gameOver) return;
    if (kind === 'left') move(-1);
    else if (kind === 'right') move(1);
    else if (kind === 'down') softDrop();
    else if (kind === 'rotate') tryRotate(1);
    else if (kind === 'rotateLeft') tryRotate(-1);
    else if (kind === 'hard') hardDrop();
    else if (kind === 'pause') paused = !paused;
    else return;
    draw();
  }

  // ── Drawing ───────────────────────────────────────────────────────────
  let cellSize = 16;
  let lastGoodCellSize = 16;
  function draw() {
    if (!cellSize) return;
    const w = canvas.width, h = canvas.height;
    cx.fillStyle = '#000'; cx.fillRect(0,0,w,h);
    // Grid lines
    cx.strokeStyle = 'rgba(255,255,255,0.04)';
    cx.lineWidth = 1;
    for (let x=0;x<=COLS;x++) {
      cx.beginPath(); cx.moveTo(x*cellSize+0.5, 0); cx.lineTo(x*cellSize+0.5, h); cx.stroke();
    }
    for (let y=0;y<=ROWS;y++) {
      cx.beginPath(); cx.moveTo(0, y*cellSize+0.5); cx.lineTo(w, y*cellSize+0.5); cx.stroke();
    }
    // Locked blocks
    for (let y=0;y<ROWS;y++) for (let x=0;x<COLS;x++) {
      if (grid[y][x]) drawCell(cx, x, y, COLORS[grid[y][x]]);
    }
    // Current piece
    if (current) {
      for (let y=0;y<current.m.length;y++) for (let x=0;x<current.m[0].length;x++) {
        if (current.m[y][x]) drawCell(cx, current.x + x, current.y + y, COLORS[current.type]);
      }
    }
    // Game over overlay
    if (gameOver) {
      cx.fillStyle = 'rgba(0,0,0,0.7)';
      cx.fillRect(0,0,w,h);
      cx.fillStyle = '#ff2020';
      cx.shadowColor = '#ff2020'; cx.shadowBlur = 12;
      cx.font = `bold ${Math.floor(cellSize*1.4)}px ui-monospace`;
      cx.textAlign = 'center'; cx.textBaseline = 'middle';
      cx.fillText('GAME OVER', w/2, h/2 - cellSize);
      cx.shadowBlur = 0;
      cx.fillStyle = 'rgba(255,255,255,0.7)';
      cx.font = `${Math.floor(cellSize*0.8)}px ui-monospace`;
      cx.fillText('press R to restart', w/2, h/2 + cellSize);
    } else if (paused) {
      cx.fillStyle = 'rgba(0,0,0,0.6)';
      cx.fillRect(0,0,w,h);
      cx.fillStyle = '#f5f5f5';
      cx.font = `bold ${Math.floor(cellSize*1.4)}px ui-monospace`;
      cx.textAlign = 'center'; cx.textBaseline = 'middle';
      cx.fillText('PAUSED', w/2, h/2);
    }
    drawNext();
  }
  function drawCell(c, x, y, color) {
    if (y < 0) return;
    const px = x * cellSize, py = y * cellSize;
    c.fillStyle = color;
    c.fillRect(px+1, py+1, cellSize-2, cellSize-2);
    c.fillStyle = 'rgba(255,255,255,0.18)';
    c.fillRect(px+1, py+1, cellSize-2, 2);
    c.fillStyle = 'rgba(0,0,0,0.25)';
    c.fillRect(px+1, py+cellSize-3, cellSize-2, 2);
  }
  function drawNext() {
    const w = nextCanvas.width, h = nextCanvas.height;
    nx.fillStyle = '#000'; nx.fillRect(0,0,w,h);
    if (!nextType) return;
    const m = SHAPES[nextType];
    const cs = Math.floor(Math.min(w, h) / (m.length + 1));
    const offX = (w - cs * m[0].length) / 2;
    const offY = (h - cs * m.length) / 2;
    nx.fillStyle = COLORS[nextType];
    for (let y=0;y<m.length;y++) for (let x=0;x<m[0].length;x++) {
      if (m[y][x]) nx.fillRect(offX + x*cs + 1, offY + y*cs + 1, cs-2, cs-2);
    }
  }

  // ── Layout / responsive ───────────────────────────────────────────────
  let mode = ''; // 'wide' | 'tall' | 'small'
  function applyLayout(rect) {
    const w = rect.width, h = rect.height;
    let next;
    if (w < 220 || h < 260) next = 'small';
    else if (w >= h * 1.05) next = 'wide';
    else next = 'tall';

    if (next !== mode) {
      mode = next;
      if (mode === 'wide') {
        stage.style.flexDirection = 'row';
        stage.style.justifyContent = 'center';
        stage.style.alignItems = 'stretch';
        if (!hud.parentNode) stage.appendChild(hud);
        hud.style.display = 'flex';
        hud.style.width = w > 680 ? '154px' : '136px';
        hud.style.flexDirection = 'column';
        hud.style.gap = '7px';
        hud.style.maxHeight = '100%';
        hud.style.overflowY = 'auto';
        boardWrap.style.alignItems = 'center';
        boardWrap.style.justifyContent = 'center';
        if (touchControls.parentNode !== hud) hud.appendChild(touchControls);
        corner.style.display = 'none';
      } else if (mode === 'tall') {
        stage.style.flexDirection = 'column';
        stage.style.justifyContent = 'flex-start';
        stage.style.alignItems = 'stretch';
        if (!hud.parentNode) stage.appendChild(hud);
        hud.style.display = 'flex';
        hud.style.width = 'auto';
        hud.style.flexDirection = 'row';
        hud.style.gap = '8px';
        hud.style.flex = '0 0 auto';
        hud.style.maxHeight = 'none';
        hud.style.overflowY = 'visible';
        boardWrap.style.alignItems = 'center';
        boardWrap.style.justifyContent = 'center';
        if (touchControls.parentNode !== parent) parent.appendChild(touchControls);
        corner.style.display = 'none';
      } else {
        stage.style.justifyContent = 'center';
        stage.style.alignItems = 'stretch';
        if (hud.parentNode) hud.parentNode.removeChild(hud);
        if (touchControls.parentNode !== parent) parent.appendChild(touchControls);
        corner.style.display = 'block';
      }
      // Hide footer hint in small mode (no room).
      footer.style.display = (mode === 'small') ? 'none' : 'block';
    }
    const coarse = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;
    touchControls.style.display = 'grid';
    touchControls.style.gridTemplateColumns = mode === 'wide'
      ? 'repeat(2,minmax(0,1fr))'
      : 'repeat(4,minmax(0,1fr))';
    touchControls.style.paddingTop = mode === 'wide' ? '0' : '8px';
    touchControls.style.marginTop = mode === 'wide' ? '2px' : '0';
    if (mode === 'wide') hud.style.width = w > 680 ? '154px' : '136px';

    // Compute cell size to fit board in available area.
    const hudW = mode === 'wide' ? hud.offsetWidth : 0;
    const availW = mode === 'wide'
      ? Math.max(0, stage.clientWidth - hudW - 10)
      : boardWrap.clientWidth - 4;
    const availH = boardWrap.clientHeight - 4;
    const byW = Math.floor(availW / COLS);
    const byH = Math.floor(availH / ROWS);
    const computed = Math.min(byW, byH, mode === 'wide' ? 64 : 44);
    if (Number.isFinite(computed) && computed >= 8) {
      cellSize = Math.max(8, computed);
      lastGoodCellSize = cellSize;
    } else {
      cellSize = lastGoodCellSize;
    }
    const dpr = window.devicePixelRatio || 1;
    canvas.width  = COLS * cellSize;
    canvas.height = ROWS * cellSize;
    canvas.style.width  = canvas.width  + 'px';
    canvas.style.height = canvas.height + 'px';
    if (mode === 'wide') {
      boardWrap.style.flex = `0 0 ${canvas.width}px`;
      boardWrap.style.width = canvas.width + 'px';
    } else {
      boardWrap.style.flex = '1 1 auto';
      boardWrap.style.width = 'auto';
    }
    nextCanvas.width  = 64; nextCanvas.height = 64;
    draw();
  }

  const ro = new ResizeObserver(entries => {
    for (const e of entries) applyLayout(e.contentRect);
  });
  ro.observe(parent);

  // ── Controls ──────────────────────────────────────────────────────────
  const onKey = (e) => {
    const k = e.key.toLowerCase();
    const before = JSON.stringify({ score, lines, level, paused, gameOver, x: current?.x, y: current?.y });
    if (k === 'arrowleft' || k === 'a') gameAction('left');
    else if (k === 'arrowright' || k === 'd') gameAction('right');
    else if (k === 'arrowdown' || k === 's') gameAction('down');
    else if (k === 'arrowup' || k === 'x') gameAction('rotate');
    else if (k === 'z') gameAction('rotateLeft');
    else if (e.key === ' ') gameAction('hard');
    else if (k === 'p') gameAction('pause');
    else if (k === 'r') gameAction('restart');
    else return;
    const after = JSON.stringify({ score, lines, level, paused, gameOver, x: current?.x, y: current?.y });
    const used = before !== after || ['p', 'r', ' ', 'arrowup', 'arrowdown', 'arrowleft', 'arrowright'].includes(k) || e.key === ' ';
    if (used) { e.preventDefault(); draw(); }
  };
  parent.addEventListener('keydown', onKey, { passive: false });

  // ── Game loop ─────────────────────────────────────────────────────────
  let last = performance.now();
  function loop(now) {
    const delta = now - last; last = now;
    if (!paused && !gameOver) {
      dropAccum += delta;
      if (dropAccum > dropInterval) softDrop();
    }
    updateHUD();
    draw();
    animation = requestAnimationFrame(loop);
  }
  restart();
  animation = requestAnimationFrame(loop);

  return {
    cleanup: () => {
      cancelAnimationFrame(animation);
      ro.disconnect();
      parent.removeEventListener('keydown', onKey);
    }
  };
}"""

SNAKE_RENDERER = r"""async (parent, space, ctx) => {
  parent.style.cssText = `
    position:absolute; inset:0; box-sizing:border-box;
    display:flex; flex-direction:column; gap:8px; padding:12px;
    overflow:hidden; outline:none; color:#f5f5f5;
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
    touch-action:none;
  `;
  parent.tabIndex = 0;
  parent.addEventListener('pointerdown', () => parent.focus());

  const stage = document.createElement('div');
  stage.style.cssText = 'flex:1 1 auto;min-height:0;display:flex;gap:8px;overflow:hidden;';
  const boardWrap = document.createElement('div');
  boardWrap.style.cssText = 'flex:1 1 auto;min-width:0;min-height:0;display:flex;align-items:center;justify-content:center;position:relative;';
  const canvas = document.createElement('canvas');
  canvas.style.cssText = 'background:#020202;border:1px solid rgba(255,255,255,0.12);border-radius:8px;image-rendering:pixelated;';
  boardWrap.appendChild(canvas);
  const cx = canvas.getContext('2d');

  const hud = document.createElement('div');
  hud.style.cssText = 'flex:0 0 132px;display:flex;flex-direction:column;gap:8px;min-width:0;';
  const stat = (label) => {
    const box = document.createElement('div');
    box.style.cssText = 'padding:8px 10px;border-radius:10px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);';
    const l = document.createElement('div');
    l.style.cssText = 'font-size:9px;letter-spacing:0.14em;text-transform:uppercase;color:rgba(255,255,255,0.42);';
    l.textContent = label;
    const v = document.createElement('div');
    v.style.cssText = 'font-size:18px;font-weight:700;line-height:1.2;';
    v.textContent = '0';
    box.append(l, v);
    hud.appendChild(box);
    return v;
  };
  const scoreEl = stat('Score');
  const bestEl = stat('Best');
  stage.append(boardWrap, hud);
  parent.appendChild(stage);

  const controls = document.createElement('div');
  controls.style.cssText = 'display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px;flex:0 0 auto;';
  parent.appendChild(controls);
  const button = (label, action, accent=false) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.style.cssText = `min-height:44px;border-radius:11px;border:1px solid ${accent ? 'rgba(255,32,32,0.35)' : 'rgba(255,255,255,0.12)'};background:${accent ? 'rgba(255,32,32,0.14)' : 'rgba(255,255,255,0.055)'};color:#f5f5f5;font:700 12px/1 ui-monospace,SFMono-Regular,Menlo,monospace;touch-action:manipulation;`;
    b.addEventListener('pointerdown', (ev) => {
      ev.preventDefault(); ev.stopPropagation(); parent.focus(); act(action);
    });
    controls.appendChild(b);
  };
  button('LEFT', 'left');
  button('UP', 'up', true);
  button('RIGHT', 'right');
  button('RESTART', 'restart');
  button('DOWN', 'down', true);
  button('PAUSE', 'pause');

  const N = 20;
  let snake, dir, nextDir, food, score, best = 0, dead, paused, tickMs, acc, last, raf, cell = 12, lastGood = 12;
  const rand = () => ({ x: Math.floor(Math.random() * N), y: Math.floor(Math.random() * N) });
  const same = (a, b) => a.x === b.x && a.y === b.y;
  function placeFood() {
    do food = rand(); while (snake.some((p) => same(p, food)));
  }
  function restart() {
    snake = [{x:10,y:10},{x:9,y:10},{x:8,y:10}];
    dir = {x:1,y:0}; nextDir = dir; score = 0; tickMs = 130; acc = 0; dead = false; paused = false;
    placeFood(); updateHud(); draw();
  }
  function updateHud() { scoreEl.textContent = score; bestEl.textContent = best; }
  function setDir(x, y) {
    if (dir.x + x === 0 && dir.y + y === 0) return;
    nextDir = {x, y};
  }
  function act(action) {
    if (action === 'restart') return restart();
    if (action === 'pause') { paused = !paused; draw(); return; }
    if (dead) return;
    if (action === 'left') setDir(-1, 0);
    if (action === 'right') setDir(1, 0);
    if (action === 'up') setDir(0, -1);
    if (action === 'down') setDir(0, 1);
    draw();
  }
  function step() {
    dir = nextDir;
    const head = { x: snake[0].x + dir.x, y: snake[0].y + dir.y };
    if (head.x < 0 || head.x >= N || head.y < 0 || head.y >= N || snake.some((p) => same(p, head))) {
      dead = true; best = Math.max(best, score); updateHud(); return;
    }
    snake.unshift(head);
    if (same(head, food)) {
      score += 10; tickMs = Math.max(70, tickMs - 2); placeFood();
    } else {
      snake.pop();
    }
    updateHud();
  }
  function resize() {
    const wide = parent.clientWidth >= parent.clientHeight * 1.08;
    stage.style.flexDirection = wide ? 'row' : 'column';
    hud.style.flexDirection = wide ? 'column' : 'row';
    hud.style.flexBasis = wide ? '132px' : 'auto';
    controls.style.gridTemplateColumns = wide ? 'repeat(3,minmax(0,1fr))' : 'repeat(3,minmax(0,1fr))';
    const computed = Math.floor((Math.min(boardWrap.clientWidth, boardWrap.clientHeight) - 4) / N);
    if (Number.isFinite(computed) && computed >= 6) { cell = computed; lastGood = cell; }
    else cell = lastGood;
    canvas.width = N * cell; canvas.height = N * cell;
    canvas.style.width = canvas.width + 'px'; canvas.style.height = canvas.height + 'px';
    draw();
  }
  function draw() {
    cx.fillStyle = '#020202'; cx.fillRect(0,0,canvas.width,canvas.height);
    cx.strokeStyle = 'rgba(255,255,255,0.04)';
    for (let i=0;i<=N;i++) {
      cx.beginPath(); cx.moveTo(i*cell+0.5,0); cx.lineTo(i*cell+0.5,canvas.height); cx.stroke();
      cx.beginPath(); cx.moveTo(0,i*cell+0.5); cx.lineTo(canvas.width,i*cell+0.5); cx.stroke();
    }
    cx.fillStyle = '#ff2020'; cx.fillRect(food.x*cell+2, food.y*cell+2, cell-4, cell-4);
    snake.forEach((p, i) => {
      cx.fillStyle = i === 0 ? '#5eff8a' : '#32d875';
      cx.fillRect(p.x*cell+1, p.y*cell+1, cell-2, cell-2);
    });
    if (dead || paused) {
      cx.fillStyle = 'rgba(0,0,0,0.68)'; cx.fillRect(0,0,canvas.width,canvas.height);
      cx.fillStyle = dead ? '#ff2020' : '#f5f5f5';
      cx.font = `bold ${Math.max(14, Math.floor(cell*1.3))}px ui-monospace`;
      cx.textAlign = 'center'; cx.textBaseline = 'middle';
      cx.fillText(dead ? 'GAME OVER' : 'PAUSED', canvas.width/2, canvas.height/2);
    }
  }
  const onKey = (e) => {
    const k = e.key.toLowerCase();
    if (k === 'arrowleft' || k === 'a') act('left');
    else if (k === 'arrowright' || k === 'd') act('right');
    else if (k === 'arrowup' || k === 'w') act('up');
    else if (k === 'arrowdown' || k === 's') act('down');
    else if (k === 'r') act('restart');
    else if (k === 'p' || e.key === ' ') act('pause');
    else return;
    e.preventDefault();
  };
  parent.addEventListener('keydown', onKey, { passive:false });
  const ro = new ResizeObserver(resize);
  ro.observe(parent);
  function loop(now) {
    const dt = now - last; last = now;
    if (!paused && !dead) {
      acc += dt;
      while (acc >= tickMs) { step(); acc -= tickMs; }
    }
    draw();
    raf = requestAnimationFrame(loop);
  }
  restart(); resize(); last = performance.now(); raf = requestAnimationFrame(loop);
  return { cleanup: () => { cancelAnimationFrame(raf); ro.disconnect(); parent.removeEventListener('keydown', onKey); } };
}"""


def _get_embedded_browser_renderer():
    """Import the embedded browser renderer from the dedicated module."""
    from tools.studio_embedded_browser import EMBEDDED_BROWSER_RENDERER
    return EMBEDDED_BROWSER_RENDERER


PRESETS: Dict[str, Dict[str, Any]] = {
    "retro_arcade": {
        "title": "Retro Arcade",
        "kind": "bundle",
        "category": "Space Agent Packs",
        "bundle": ["tetris", "snake", "minesweeper", "retro_marquee"],
        "description": (
            "Installs the arcade starter pack: Hermes Tetris, Snake, "
            "Minesweeper, and the Space Agent retro marquee."
        ),
    },
    "daily_news": {
        "title": "Daily News",
        "kind": "bundle",
        "category": "Space Agent Packs",
        "bundle": ["news_feed", "top_news", "weather"],
        "description": (
            "Installs the Space Agent news-feed, top-news, and weather "
            "widgets with Hermes compatibility shims."
        ),
    },
    "crypto_dashboard": {
        "title": "Crypto Dashboard",
        "kind": "bundle",
        "category": "Space Agent Packs",
        "bundle": ["crypto_prices", "btc_vs_sp500", "crypto_news_feed"],
        "description": (
            "Installs crypto prices, BTC vs S&P 500, and crypto news "
            "widgets from the Space Agent dashboard examples."
        ),
    },
    "agent_zero_videos": {
        "title": "Agent Zero Videos",
        "kind": "bundle",
        "category": "Space Agent Packs",
        "bundle": ["yt_video_list", "yt_video_player"],
        "description": (
            "Installs the YouTube/RSS video list and player pair from "
            "Space Agent's onboarding templates."
        ),
    },
    "subagent_monitor": {
        "title": "Subagent Monitor",
        "kind": "widget",
        "category": "Hermes Native",
        "renderer": SUBAGENT_MONITOR_RENDERER,
        "size": {"w": 5, "h": 5},
        "description": (
            "Live list of background delegate_task subagents — polls every "
            "~2.5s and shows status, id, goal, and start time."
        ),
    },
    "embedded_browser": {
        "title": "Embedded Browser",
        "kind": "widget",
        "category": "Space Agent",
        "renderer": _get_embedded_browser_renderer(),
        "size": {"w": 8, "h": 8},
        "description": (
            "Lightweight Space Agent-style embedded browser with URL bar, "
            "tabs, back/forward/reload, screenshot view, and recording. "
            "Uses the Hermes browser backend API instead of heavy Xpra/Chrome."
        ),
    },
    "skills_grid": {
        "title": "Hermes Skills",
        "kind": "widget",
        "category": "Hermes Native",
        "renderer": SKILLS_GRID_RENDERER,
        "size": {"w": 7, "h": 7},
        "description": (
            "Auto-fill grid of all available Hermes skills with a hover "
            "preview pane. Scrollable, responsive (stacks when narrow). Use "
            "this instead of re-authoring a skills widget from scratch."
        ),
    },
    "tetris": {
        "title": "Tetris",
        "kind": "widget",
        "category": "Retro Arcade",
        "renderer": TETRIS_RENDERER,
        "size": {"w": 6, "h": 9},
        "description": (
            "Playable Tetris with responsive HUD. Adapts to widget size "
            "(side-HUD when wide, stacked when tall, score-only when small). "
            "Keyboard controls are scoped to the widget and mobile/touch "
            "buttons are built in for phone play."
        ),
    },
    "snake": {
        "title": "Snake",
        "kind": "widget",
        "category": "Retro Arcade",
        "renderer": SNAKE_RENDERER,
        "size": {"w": 5, "h": 5},
        "description": (
            "Retro Arcade starter preset: playable Snake with responsive "
            "canvas sizing, scoped keyboard controls, and phone-friendly "
            "tap controls."
        ),
    },
    "minesweeper": {
        "title": "Minesweeper",
        "kind": "widget",
        "category": "Retro Arcade",
        "renderer": _space_agent_widget(
            "retro-arcade/minesweeper-game.yaml",
            "Minesweeper",
            "Space Agent arcade example, vendored into Hermes Studio.",
            ["Resizable minefield", "Mouse/tap reveal and flag actions", "Timer and mine counter"],
            "#00ffd5",
        ),
        "size": {"w": 5, "h": 5},
        "source": "space-agent/retro-arcade/minesweeper-game.yaml",
        "description": "Space Agent Minesweeper, installed as a Hermes Studio preset.",
    },
    "retro_marquee": {
        "title": "Retro Marquee",
        "kind": "widget",
        "category": "Retro Arcade",
        "renderer": _space_agent_widget(
            "retro-arcade/retro-marquee.yaml",
            "Retro Marquee",
            "A compact animated arcade sign from Space Agent.",
            ["Use as a title strip for arcade spaces", "Good reference for motion without layout bloat"],
            "#00ffd5",
        ),
        "size": {"w": 7, "h": 2},
        "source": "space-agent/retro-arcade/retro-marquee.yaml",
        "description": "Animated Space Agent arcade marquee.",
    },
    "news_feed": {
        "title": "News Feed",
        "kind": "widget",
        "category": "Daily News",
        "renderer": _space_agent_widget(
            "daily-news/news-feed.yaml",
            "News Feed",
            "Space Agent RSS reader example adapted through Hermes compatibility APIs.",
            ["Feed picker", "Readable article cards", "Stored feed preference shim"],
            "#93c5ff",
        ),
        "size": {"w": 6, "h": 6},
        "source": "space-agent/daily-news/news-feed.yaml",
        "description": "RSS news feed template from Space Agent.",
    },
    "top_news": {
        "title": "Top News",
        "kind": "widget",
        "category": "Daily News",
        "renderer": _space_agent_widget(
            "daily-news/top-news.yaml",
            "Top News",
            "Space Agent top-news panel adapted through Hermes compatibility APIs.",
            ["Multiple source feeds", "Story summaries", "Image-aware cards"],
            "#93c5ff",
        ),
        "size": {"w": 6, "h": 5},
        "source": "space-agent/daily-news/top-news.yaml",
        "description": "Top-news dashboard widget from Space Agent.",
    },
    "weather": {
        "title": "Weather",
        "kind": "widget",
        "category": "Daily News",
        "renderer": _space_agent_widget(
            "daily-news/weather.yaml",
            "Weather",
            "Space Agent weather widget with local browser preference shim.",
            ["Approximate location mode", "Forecast cards", "Compact responsive surface"],
            "#7dc6ff",
        ),
        "size": {"w": 4, "h": 4},
        "source": "space-agent/daily-news/weather.yaml",
        "description": "Weather widget template from Space Agent.",
    },
    "crypto_prices": {
        "title": "Crypto Prices",
        "kind": "widget",
        "category": "Crypto Dashboard",
        "renderer": _space_agent_widget(
            "crypto-dashboard/crypto-prices.yaml",
            "Crypto Prices",
            "Space Agent crypto ticker adapted through Hermes external-fetch bridge.",
            ["CoinGecko price data", "24h change indicators", "Ticker-style layout"],
            "#f6be63",
        ),
        "size": {"w": 7, "h": 3},
        "source": "space-agent/crypto-dashboard/crypto-prices.yaml",
        "description": "Crypto price ticker from Space Agent.",
    },
    "btc_vs_sp500": {
        "title": "BTC vs S&P 500",
        "kind": "widget",
        "category": "Crypto Dashboard",
        "renderer": _space_agent_widget(
            "crypto-dashboard/btc-vs-sp500.yaml",
            "BTC vs S&P 500",
            "Space Agent comparison chart adapted through Hermes fetch bridge.",
            ["BTC trend", "S&P/FRED comparison", "Chart-heavy dashboard reference"],
            "#f6be63",
        ),
        "size": {"w": 7, "h": 5},
        "source": "space-agent/crypto-dashboard/btc-vs-sp500.yaml",
        "description": "BTC versus S&P 500 chart widget from Space Agent.",
    },
    "crypto_news_feed": {
        "title": "Crypto News Feed",
        "kind": "widget",
        "category": "Crypto Dashboard",
        "renderer": _space_agent_widget(
            "crypto-dashboard/crypto-news-feed.yaml",
            "Crypto News Feed",
            "Space Agent crypto RSS feed adapted through Hermes compatibility APIs.",
            ["Crypto RSS sources", "Preference storage shim", "Scrollable story cards"],
            "#f6be63",
        ),
        "size": {"w": 6, "h": 5},
        "source": "space-agent/crypto-dashboard/crypto-news-feed.yaml",
        "description": "Crypto RSS/news widget from Space Agent.",
    },
    "wysiwyg_editor": {
        "title": "WYSIWYG Editor",
        "kind": "widget",
        "category": "Productivity",
        "renderer": _space_agent_widget(
            "wysiwyg-editor.yaml",
            "WYSIWYG Editor",
            "Space Agent editor template using Hermes' local appFiles shim.",
            ["Toolbar editing", "Stored document list", "A strong reference for complex tool widgets"],
            "#89baff",
        ),
        "size": {"w": 8, "h": 6},
        "source": "space-agent/wysiwyg-editor.yaml",
        "description": "Rich text editor template from Space Agent.",
    },
    "yt_video_list": {
        "title": "YouTube Video List",
        "kind": "widget",
        "category": "Agent Zero Videos",
        "renderer": _space_agent_widget(
            "agent-zero-videos/yt-video-list.yaml",
            "YouTube Video List",
            "Space Agent RSS video list adapted through Hermes external-fetch bridge.",
            ["RSS-backed video list", "Broadcasts selected video to the player", "Pairs with yt_video_player"],
            "#ff5a4f",
        ),
        "size": {"w": 5, "h": 6},
        "source": "space-agent/agent-zero-videos/yt-video-list.yaml",
        "description": "Agent Zero video list widget from Space Agent.",
    },
    "yt_video_player": {
        "title": "YouTube Video Player",
        "kind": "widget",
        "category": "Agent Zero Videos",
        "renderer": _space_agent_widget(
            "agent-zero-videos/yt-video-player.yaml",
            "YouTube Video Player",
            "Space Agent player widget that listens for video selections.",
            ["Embeds YouTube", "Pairs with yt_video_list", "Good media-player reference"],
            "#ff5a4f",
        ),
        "size": {"w": 7, "h": 5},
        "source": "space-agent/agent-zero-videos/yt-video-player.yaml",
        "description": "Agent Zero video player widget from Space Agent.",
    },
    "rickroll_player": {
        "title": "Rickroll Player",
        "kind": "widget",
        "category": "Media",
        "renderer": _space_agent_widget(
            "rickroll-player.yaml",
            "Rickroll Player",
            "Space Agent media widget, including self-remove compatibility support.",
            ["YouTube player", "Autoplay behavior", "Widget self-remove API reference"],
            "#ff7566",
        ),
        "size": {"w": 6, "h": 5},
        "source": "space-agent/rickroll-player.yaml",
        "description": "Rickroll media-player template from Space Agent.",
    },
    "weather_report": {
        "title": "Weather Report Prompt",
        "kind": "prompt",
        "category": "Prompt Starters",
        "renderer": _template_card_renderer(
            "Weather Report Prompt",
            "A Space Agent chat starter Hermes should translate into a report/dashboard.",
            [
                "Use approximate IP location only; do not request exact geolocation unless the user asks.",
                "Fetch weather server-side with Hermes tools when possible, then bake data into the widget/report.",
                "If the user asks for a document, use the PDF/report skill and summarize briefly in chat.",
            ],
            "#7dc6ff",
        ),
        "size": {"w": 5, "h": 4},
        "description": "Prompt-style Space Agent weather report starter converted into a Hermes guidance card.",
    },
    "flip_space_helper": {
        "title": "Flip Space Helper",
        "kind": "prompt",
        "category": "Prompt Starters",
        "renderer": _template_card_renderer(
            "Flip Space Helper",
            "A Space Agent prompt pattern for whole-space visual effects.",
            [
                "Preserve current transform state; add the requested transform instead of resetting it.",
                "Use a timed CSS transition so the space visibly changes, then leave the new state stable.",
                "Keep effects reversible and explain the escape/revert action in one short sentence.",
            ],
            "#ff9bc9",
        ),
        "size": {"w": 5, "h": 4},
        "description": "Prompt-style Space Agent starter for whole-space effects.",
    },
    "documentation_helper": {
        "title": "Documentation Helper",
        "kind": "prompt",
        "category": "Prompt Starters",
        "renderer": _template_card_renderer(
            "Documentation Helper",
            "A Space Agent documentation starter tuned for Hermes.",
            [
                "Load the relevant docs/skill first, then ask the one exact missing question if needed.",
                "Prefer official docs or repo-local source before generic web answers.",
                "Turn useful docs into compact Studio reference cards when the user is building.",
            ],
            "#f3c46c",
        ),
        "size": {"w": 5, "h": 4},
        "description": "Documentation prompt starter converted into a Hermes Studio reference card.",
    },
    "studio_system_map": {
        "title": "Hermes Studio System Map",
        "kind": "guide",
        "category": "Hermes Native",
        "renderer": _template_card_renderer(
            "Hermes Studio System Map",
            "Operational memory for editing Studio, the dashboard, and live Hermes services.",
            [
                "Source root: /opt/hermes. Studio frontend: /opt/hermes/web/src/pages/StudioPage.tsx and /opt/hermes/web/src/studio/*. Backend: /opt/hermes/hermes_cli/web_server.py and /opt/hermes/tools/studio_*.py.",
                "Profile/data root: /opt/data/profiles/supervisor. Live widgets live under /opt/data/studio/spaces/<space>/widgets/*.yaml; profile studio paths are mirror/archive only.",
                "Dashboard changes: build in /opt/hermes/web, restart hermes-dashboard.service, then verify systemctl status and the public Studio URL.",
                "Agent/tool/prompt changes: restart hermes-agent.service and verify gateway logs, Telegram/API reconnect, and relevant tests.",
                "Studio requests should prioritize canvas widgets. Telegram/iMessage requests can still edit the same codebase if the user explicitly asks for system/site changes.",
            ],
            "#ff2020",
        ),
        "size": {"w": 7, "h": 5},
        "description": "Hermes-specific codepath and restart map so the agent knows how to edit Studio itself.",
    },
}


# ---------------------------------------------------------------------------
# Tool — install preset by name
# ---------------------------------------------------------------------------

def _ok(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _err(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


def studio_install_preset(args: Dict[str, Any], **_) -> str:
    """Install a named preset widget or preset bundle into a Studio space."""
    try:
        name = args.get("name")
        if not name or name not in PRESETS:
            return _err(
                f"unknown preset {name!r}. Available: {sorted(PRESETS)}"
            )
        preset = PRESETS[name]
        space_id = args.get("space_id") or "default"

        if preset.get("kind") == "bundle":
            installed = []
            for child_name in preset.get("bundle", []):
                child = PRESETS.get(child_name)
                if not child or "renderer" not in child:
                    raise ValueError(f"bundle {name!r} references missing widget {child_name!r}")
                rec = studio_widgets.upsert_widget(
                    space_id,
                    child_name.replace("_", "-"),
                    title=child["title"],
                    renderer=child["renderer"],
                    position=None,
                    size=child.get("size"),
                )
                installed.append(
                    {
                        "preset": child_name,
                        "id": rec["id"],
                        "position": rec["position"],
                        "size": rec["size"],
                    }
                )
            return _ok(
                {
                    "ok": True,
                    "preset": name,
                    "kind": "bundle",
                    "space_id": space_id,
                    "widgets": installed,
                    "bundle": preset.get("bundle", []),
                }
            )

        widget_id = args.get("id") or name.replace("_", "-")
        position = args.get("position")
        size = args.get("size") or preset.get("size")

        rec = studio_widgets.upsert_widget(
            space_id,
            widget_id,
            title=args.get("title") or preset["title"],
            renderer=preset["renderer"],
            position=position,
            size=size,
        )
        return _ok(
                {
                    "ok": True,
                    "preset": name,
                    "kind": preset.get("kind", "widget"),
                    "id": rec["id"],
                    "space_id": space_id,
                    "position": rec["position"],
                "size": rec["size"],
            }
        )
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("studio_install_preset failed")
        return _err(str(exc))


def studio_list_presets(_args: Dict[str, Any] = None, **_) -> str:
    """Return the catalog of installable presets."""
    return _ok(
        {
            "presets": [
                {
                    "name": k,
                    "title": v["title"],
                    "description": v.get("description", ""),
                    "size": v.get("size"),
                    "kind": v.get("kind", "widget"),
                    "category": v.get("category", ""),
                    "bundle": v.get("bundle"),
                    "source": v.get("source", ""),
                }
                for k, v in PRESETS.items()
            ]
        }
    )


# ---------------------------------------------------------------------------
# Schemas + registry
# ---------------------------------------------------------------------------

INSTALL_PRESET_SCHEMA = {
    "name": "studio_install_preset",
    "description": (
        "Install a pre-baked Studio widget or bundle by preset name "
        "(one-shot). Use this for known panels/games/packs like "
        "retro_arcade, daily_news, crypto_dashboard, live_browser, tetris, skills_grid, "
        "or the subagent monitor instead of re-authoring the renderer. "
        "List names with studio_list_presets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Preset name (e.g. 'retro_arcade', 'live_browser', 'tetris', or 'subagent_monitor').",
            },
            "id": {
                "type": "string",
                "description": "Widget id (default = preset name with hyphens).",
            },
            "space_id": {"type": "string"},
            "title": {"type": "string"},
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
        "required": ["name"],
        "additionalProperties": False,
    },
}

LIST_PRESETS_SCHEMA = {
    "name": "studio_list_presets",
    "description": "List the available pre-baked Studio widget presets.",
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}


def check_studio_requirements() -> bool:
    return True


registry.register(
    name="studio_install_preset",
    toolset="studio",
    schema=INSTALL_PRESET_SCHEMA,
    handler=lambda args, **kw: studio_install_preset(args, **kw),
    check_fn=check_studio_requirements,
    description=INSTALL_PRESET_SCHEMA["description"],
)

registry.register(
    name="studio_list_presets",
    toolset="studio",
    schema=LIST_PRESETS_SCHEMA,
    handler=lambda args, **kw: studio_list_presets(args, **kw),
    check_fn=check_studio_requirements,
    description=LIST_PRESETS_SCHEMA["description"],
)


