// studio_browser_server.js
//
// Thin shim around `agent-browser` (Vercel Labs CLI). Replaces our
// previous Playwright + stealth stack. agent-browser owns Chrome
// lifecycle + stealth + profile management; we just bridge:
//
//   [browser tab]  <-WS->  this server (port 9322)
//                              |
//                              +--> agent-browser stream WS (frames, read-only)
//                              |
//                              +--> agent-browser CDP URL (input dispatch + navigation)
//                              |
//                              +--> agent-browser CLI (snapshot, get url/title, etc.)
//
// HTTP endpoints kept compatible with the dashboard's existing
// /api/studio/browser/* routes so nothing else has to change.

const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn, execFileSync } = require('child_process');
const { WebSocketServer, WebSocket } = require('ws');

const PORT = Number(process.env.STUDIO_BROWSER_PORT || 9322);
const AB_BIN = process.env.AGENT_BROWSER_BIN || '/usr/bin/agent-browser';
const SEARCH_HOME = process.env.STUDIO_BROWSER_SEARCH_HOME || 'https://example.com/';
const SEARCH_URL  = process.env.STUDIO_BROWSER_SEARCH_URL  || 'https://www.google.com/search?q=';
const VIEWPORT = { width: 1280, height: 820 };
const STREAM_QUALITY_ACTIVE = Number(process.env.STUDIO_BROWSER_STREAM_QUALITY_ACTIVE || 55);
const STREAM_QUALITY_IDLE = Number(process.env.STUDIO_BROWSER_STREAM_QUALITY_IDLE || 35);
const RECORDING_DIR = process.env.STUDIO_BROWSER_RECORDING_DIR || '/opt/data/studio/browser-recordings';
const AB_SESSION = process.env.AGENT_BROWSER_SESSION || 'studio';
const AB_SESSION_NAME = process.env.AGENT_BROWSER_SESSION_NAME || AB_SESSION;
const AB_PROFILE = process.env.AGENT_BROWSER_PROFILE || '/opt/data/profiles/supervisor/home/.studio-browser-profile';

function abArgs(args, launchOptions = false) {
  const out = [...args, '--session', AB_SESSION, '--session-name', AB_SESSION_NAME];
  if (launchOptions) {
    out.push('--profile', AB_PROFILE);
    if (process.env.AGENT_BROWSER_USER_AGENT) out.push('--user-agent', process.env.AGENT_BROWSER_USER_AGENT);
    if (process.env.AGENT_BROWSER_ARGS) out.push('--args', process.env.AGENT_BROWSER_ARGS);
  }
  return out;
}

function abEnv(launchOptions = false) {
  const env = { ...process.env };
  if (!launchOptions) {
    delete env.AGENT_BROWSER_PROFILE;
    delete env.AGENT_BROWSER_USER_AGENT;
    delete env.AGENT_BROWSER_ARGS;
  }
  return env;
}

// ------------------------------------------------------------------
// agent-browser CLI helpers
// ------------------------------------------------------------------

function abJSON(args) {
  // Run `agent-browser ...args --json` synchronously, return parsed obj or null
  try {
    const out = execFileSync(AB_BIN, [...abArgs(args), '--json'], { timeout: 15000, encoding: 'utf8', env: abEnv() });
    return JSON.parse(out);
  } catch (err) {
    return null;
  }
}

function abRun(args) {
  // Run async, fire-and-forget (used for click/type/press where we don't need stdout)
  return new Promise((resolve, reject) => {
    const child = spawn(AB_BIN, abArgs(args), { stdio: ['ignore', 'pipe', 'pipe'], env: abEnv() });
    let stdout = '', stderr = '';
    child.stdout.on('data', d => stdout += d);
    child.stderr.on('data', d => stderr += d);
    child.on('error', reject);
    child.on('close', code => {
      if (code === 0) resolve(stdout);
      else reject(new Error(`agent-browser exit ${code}: ${stderr || stdout}`));
    });
    setTimeout(() => { try { child.kill(); } catch (_) {} reject(new Error('timeout')); }, 30000);
  });
}

function getStreamPort() {
  const r = abJSON(['stream', 'status']);
  return r && r.success && r.data && r.data.port ? r.data.port : null;
}

function getCdpUrl() {
  try {
    return execFileSync(AB_BIN, abArgs(['get', 'cdp-url']), { timeout: 5000, encoding: 'utf8', env: abEnv() }).trim();
  } catch (_) { return null; }
}

function getCurrentUrl() {
  try {
    return execFileSync(AB_BIN, abArgs(['get', 'url']), { timeout: 5000, encoding: 'utf8', env: abEnv() }).trim();
  } catch (_) { return ''; }
}

function getCurrentTitle() {
  try {
    return execFileSync(AB_BIN, abArgs(['get', 'title']), { timeout: 5000, encoding: 'utf8', env: abEnv() }).trim();
  } catch (_) { return ''; }
}

function getTabs() {
  const r = abJSON(['tab', 'list']);
  if (!r || !r.success || !r.data || !Array.isArray(r.data.tabs)) return [];
  return r.data.tabs.map((tab, index) => ({
    index,
    active: !!tab.active,
    tabId: tab.tabId || `t${index + 1}`,
    title: tab.title || '',
    url: tab.url || '',
    type: tab.type || 'page',
  }));
}

function tabRefFromIndex(index) {
  currentTabs = getTabs();
  const numeric = Number(index || 0);
  const tab = currentTabs[Math.max(0, numeric)];
  return tab && tab.tabId ? tab.tabId : `t${numeric + 1}`;
}

function ensureBrowser() {
  // Make sure a daemon + page is running. `agent-browser open` is idempotent —
  // if a session is already on a URL, it just updates it; if not, it spins up.
  const url = getCurrentUrl();
  if (!url || url === 'about:blank' || url === '') {
    try {
      execFileSync(AB_BIN, abArgs(['open', SEARCH_HOME], true), { timeout: 30000, stdio: 'ignore', env: abEnv(true) });
    } catch (_) {}
  }
}

function normalizeUrl(value) {
  const raw = String(value || '').trim();
  if (!raw) return SEARCH_HOME;
  if (/^https?:\/\//i.test(raw)) return raw;
  if (/^localhost(:\d+)?(\/|$|\?|#)/i.test(raw) || /^127\.0\.0\.1(:\d+)?(\/|$|\?|#)/.test(raw)) {
    return `http://${raw}`;
  }
  if (/^[a-z0-9.-]+\.[a-z]{2,}(\/|$|\?|#)/i.test(raw)) {
    return `https://${raw}`;
  }
  return `${SEARCH_URL}${encodeURIComponent(raw)}`;
}

// ------------------------------------------------------------------
// CDP client (one shared connection for input dispatch)
// ------------------------------------------------------------------

let cdp = null;
let cdpId = 0;
const cdpPending = new Map();
let cdpSessionId = null;

function resetCdp() {
  try { if (cdp) cdp.close(); } catch (_) {}
  cdp = null;
  cdpSessionId = null;
  cdpPending.clear();
}

function cdpSend(method, params = {}) {
  return new Promise((resolve, reject) => {
    if (!cdp || cdp.readyState !== WebSocket.OPEN) {
      return reject(new Error('CDP not connected'));
    }
    const id = ++cdpId;
    cdpPending.set(id, { resolve, reject });
    const msg = { id, method, params };
    if (cdpSessionId) msg.sessionId = cdpSessionId;
    cdp.send(JSON.stringify(msg));
    setTimeout(() => {
      if (cdpPending.has(id)) {
        cdpPending.delete(id);
        reject(new Error(`CDP timeout: ${method}`));
      }
    }, 10000);
  });
}

async function ensureCdp() {
  if (cdp && cdp.readyState === WebSocket.OPEN && cdpSessionId) return;

  const browserUrl = getCdpUrl();
  if (!browserUrl) throw new Error('no CDP URL — is agent-browser running?');

  await new Promise((resolve, reject) => {
    cdp = new WebSocket(browserUrl);
    cdp.on('open', resolve);
    cdp.on('error', reject);
    cdp.on('message', (raw) => {
      try {
        const msg = JSON.parse(raw.toString());
        if (msg.id && cdpPending.has(msg.id)) {
          const { resolve, reject } = cdpPending.get(msg.id);
          cdpPending.delete(msg.id);
          if (msg.error) reject(new Error(msg.error.message || JSON.stringify(msg.error)));
          else resolve(msg.result);
        }
      } catch (_) {}
    });
    cdp.on('close', () => { cdp = null; cdpSessionId = null; });
    setTimeout(() => reject(new Error('CDP connect timeout')), 5000);
  });

  // Need to attach to the active page target
  const targets = await cdpSend('Target.getTargets');
  const pages = (targets.targetInfos || []).filter(t => t.type === 'page');
  const activeUrl = lastUrl || getCurrentUrl();
  const page =
    pages.find(t => activeUrl && t.url === activeUrl) ||
    pages.find(t => activeUrl && t.url && activeUrl.startsWith(t.url)) ||
    pages.find(t => t.attached !== false) ||
    pages[0];
  if (!page) throw new Error('no page target found');

  const attached = await cdpSend('Target.attachToTarget', { targetId: page.targetId, flatten: true });
  cdpSessionId = attached.sessionId;

  // Enable the domains we need for nav events + UA override
  try { await cdpSend('Page.enable'); } catch (_) {}
  try { await cdpSend('Network.enable'); } catch (_) {}

  // Override the HeadlessChrome UA — Google etc. CAPTCHA on it instantly.
  const STEALTH_UA = process.env.AGENT_BROWSER_USER_AGENT ||
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.7727.137 Safari/537.36';
  try {
    await cdpSend('Network.setUserAgentOverride', {
      userAgent: STEALTH_UA,
      acceptLanguage: 'en-US,en;q=0.9',
      platform: 'MacIntel',
    });
    await cdpSend('Emulation.setUserAgentOverride', {
      userAgent: STEALTH_UA,
      acceptLanguage: 'en-US,en;q=0.9',
      platform: 'MacIntel',
      userAgentMetadata: {
        platform: 'macOS',
        platformVersion: '10.15.7',
        architecture: 'x86',
        model: '',
        mobile: false,
        brands: [
          { brand: 'Chromium', version: '147' },
          { brand: 'Google Chrome', version: '147' },
          { brand: 'Not?A_Brand', version: '24' },
        ],
      },
    });
  } catch (e) {
    console.error('UA override failed:', e.message);
  }

  // Stealth JS — masks navigator.webdriver and other automation tells.
  // Injected into every page (including subframes) BEFORE site code runs.
  const STEALTH_JS = `
    (function() {
      try {
        Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => false, configurable: true });
        // window.chrome shim
        if (!window.chrome) window.chrome = {};
        if (!window.chrome.runtime) window.chrome.runtime = {};
        // Spoof plugin length (real Chrome has plugins; headless doesn't)
        Object.defineProperty(Navigator.prototype, 'plugins', {
          get: () => [
            { name: 'PDF Viewer', filename: 'internal-pdf-viewer', length: 1 },
            { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', length: 1 },
            { name: 'Native Client', filename: 'internal-nacl-plugin', length: 1 },
          ],
        });
        Object.defineProperty(Navigator.prototype, 'languages', {
          get: () => ['en-US', 'en'],
          configurable: true,
        });
        // Spoof permissions API quirk (headless returns 'denied' for everything)
        const origQuery = window.navigator.permissions && window.navigator.permissions.query;
        if (origQuery) {
          window.navigator.permissions.query = (params) =>
            params && params.name === 'notifications'
              ? Promise.resolve({ state: Notification.permission })
              : origQuery.call(window.navigator.permissions, params);
        }
      } catch (e) {}
    })();
  `;
  try {
    await cdpSend('Page.addScriptToEvaluateOnNewDocument', { source: STEALTH_JS });
    try { await cdpSend('Runtime.evaluate', { expression: STEALTH_JS }); } catch (_) {}

    // Track CSS viewport — what CDP click events consume. Refresh after each
    // navigation since some pages do orientation/zoom tricks that change it.
    await refreshCssViewport();
    cdp.on('message', (raw) => {
      try {
        const m = JSON.parse(raw.toString());
        if (m.method === 'Page.frameNavigated' && m.params && m.params.frame && !m.params.frame.parentId) {
          // top-level navigation — refresh viewport on next tick
          setTimeout(() => { refreshCssViewport().catch(() => {}); }, 200);
        }
      } catch (_) {}
    });
  } catch (_) {}
}

async function refreshCssViewport() {
  try {
    const r = await cdpSend('Runtime.evaluate', {
      expression: 'JSON.stringify({w:window.innerWidth,h:window.innerHeight,dpr:window.devicePixelRatio})',
      returnByValue: true,
    });
    const v = r && r.result && r.result.value ? JSON.parse(r.result.value) : null;
    if (v && v.w > 0 && v.h > 0) {
      cssViewport = { width: v.w, height: v.h };
    }
  } catch (_) { /* CDP not ready yet — try again later */ }
}

async function cdpClick(x, y, button = 'left', clickCount = 1) {
  await ensureCdp();
  await cdpSend('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button, clickCount });
  await cdpSend('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button, clickCount });
}

async function cdpMouseMove(x, y) {
  await ensureCdp();
  await cdpSend('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none' });
}

async function cdpWheel(x, y, deltaX, deltaY) {
  await ensureCdp();
  await cdpSend('Input.dispatchMouseEvent', {
    type: 'mouseWheel', x, y, button: 'none', deltaX, deltaY
  });
}

async function cdpInsertText(text) {
  await ensureCdp();
  try {
    await cdpSend('Input.insertText', { text });
    return true;
  } catch (e) {
    return false;
  }
}

async function cdpKey(key) {
  await ensureCdp();
  const raw = String(key || '');
  const parts = raw.split('+').filter(Boolean);
  const base = parts.length ? parts[parts.length - 1] : raw;
  const modifiers =
    (parts.includes('Alt') ? 1 : 0) |
    (parts.includes('Control') ? 2 : 0) |
    (parts.includes('Meta') ? 4 : 0) |
    (parts.includes('Shift') ? 8 : 0);
  // Map common keys to CDP
  const keyMap = {
    Enter: { key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 },
    Backspace: { key: 'Backspace', code: 'Backspace', windowsVirtualKeyCode: 8 },
    Tab: { key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 },
    Escape: { key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 },
    ArrowLeft: { key: 'ArrowLeft', code: 'ArrowLeft', windowsVirtualKeyCode: 37 },
    ArrowUp: { key: 'ArrowUp', code: 'ArrowUp', windowsVirtualKeyCode: 38 },
    ArrowRight: { key: 'ArrowRight', code: 'ArrowRight', windowsVirtualKeyCode: 39 },
    ArrowDown: { key: 'ArrowDown', code: 'ArrowDown', windowsVirtualKeyCode: 40 },
    Delete: { key: 'Delete', code: 'Delete', windowsVirtualKeyCode: 46 },
    Home: { key: 'Home', code: 'Home', windowsVirtualKeyCode: 36 },
    End: { key: 'End', code: 'End', windowsVirtualKeyCode: 35 },
    PageUp: { key: 'PageUp', code: 'PageUp', windowsVirtualKeyCode: 33 },
    PageDown: { key: 'PageDown', code: 'PageDown', windowsVirtualKeyCode: 34 },
  };
  const upper = base.length === 1 ? base.toUpperCase() : base;
  const k = keyMap[base] || {
    key: base.length === 1 ? base : raw,
    code: base.length === 1 ? `Key${upper}` : base,
    windowsVirtualKeyCode: base.length === 1 ? upper.charCodeAt(0) : 0,
  };
  await cdpSend('Input.dispatchKeyEvent', { type: 'keyDown', modifiers, ...k });
  await cdpSend('Input.dispatchKeyEvent', { type: 'keyUp', modifiers, ...k });
}

async function cdpNavigate(url) {
  await ensureCdp();
  await cdpSend('Page.navigate', { url });
}

// ------------------------------------------------------------------
// Frame fanout from agent-browser stream WS to our clients
// ------------------------------------------------------------------

let upstreamWs = null;
const frameClients = new Set();
let lastFrame = null; // base64 string for /state
let lastViewport = VIEWPORT;       // JPEG image dimensions (what user sees + clicks against)
let cssViewport = { width: 1280, height: 720 };  // CSS pixel viewport (what CDP click events consume)
let lastUrl = '';
let lastTitle = '';
let currentTabs = [];
let lastError = '';
let lastFrameAt = 0;
let lastStreamQuality = 0;
let lastRecordingPath = '';
let lastMetadataRefreshAt = 0;

function setLastError(err) {
  lastError = err ? String(err.message || err) : '';
  if (lastError) console.error('[studio-browser]', lastError);
}

function sendScreencastStart() {
  if (!upstreamWs || upstreamWs.readyState !== WebSocket.OPEN) return;
  const quality = frameClients.size > 0 ? STREAM_QUALITY_ACTIVE : STREAM_QUALITY_IDLE;
  if (quality === lastStreamQuality) return;
  lastStreamQuality = quality;
  try { upstreamWs.send(JSON.stringify({ type: 'screencast_start', quality })); } catch (_) {}
}

function sendScreencastStop() {
  if (!upstreamWs || upstreamWs.readyState !== WebSocket.OPEN) return;
  lastStreamQuality = 0;
  try { upstreamWs.send(JSON.stringify({ type: 'screencast_stop' })); } catch (_) {}
}

function refreshActiveMetadata(force = false) {
  const now = Date.now();
  if (!force && now - lastMetadataRefreshAt < 1000) return;
  lastMetadataRefreshAt = now;
  const tabs = getTabs();
  if (tabs.length) {
    currentTabs = tabs;
    const active = currentTabs.find(t => t.active);
    if (active) {
      lastUrl = active.url || lastUrl;
      lastTitle = active.title || lastTitle;
    }
  }
  const cliUrl = getCurrentUrl();
  const cliTitle = getCurrentTitle();
  if (cliUrl) lastUrl = cliUrl;
  if (cliTitle) lastTitle = cliTitle;
}

function ensureUpstream(force = false) {
  if (!force && frameClients.size === 0) return;
  if (upstreamWs && upstreamWs.readyState === WebSocket.OPEN) return;
  const port = getStreamPort();
  if (!port) return; // no daemon yet — caller will retry

  upstreamWs = new WebSocket(`ws://127.0.0.1:${port}`);
  upstreamWs.binaryType = 'arraybuffer';

  upstreamWs.on('open', () => {
    sendScreencastStart();
  });

  upstreamWs.on('message', (raw) => {
    let msg;
    try { msg = JSON.parse(raw.toString()); } catch (_) { return; }
    if (!msg) return;

    if (msg.type === 'frame' && msg.data) {
      lastFrame = msg.data;
      lastFrameAt = Date.now();
      if (msg.metadata) {
        lastViewport = {
          width: msg.metadata.deviceWidth || lastViewport.width,
          height: msg.metadata.deviceHeight || lastViewport.height,
        };
      }
      // Broadcast as binary buffer to clients (no JSON wrap → cheaper)
      const buf = Buffer.from(msg.data, 'base64');
      for (const ws of frameClients) {
        if (ws.readyState === WebSocket.OPEN) {
          try { ws.send(buf); } catch (_) {}
        }
      }
    } else if (msg.type === 'tabs') {
      currentTabs = (msg.tabs || []).map((tab, index) => ({
        index,
        active: !!tab.active,
        tabId: tab.tabId || `t${index + 1}`,
        title: tab.title || '',
        url: tab.url || '',
        type: tab.type || 'page',
      }));
      const active = currentTabs.find(t => t.active);
      if (active) {
        lastUrl = active.url || lastUrl;
        lastTitle = active.title || lastTitle;
      }
    } else if (msg.type === 'status') {
      if (msg.viewportWidth) lastViewport.width = msg.viewportWidth;
      if (msg.viewportHeight) lastViewport.height = msg.viewportHeight;
    }
  });

  upstreamWs.on('close', () => {
    upstreamWs = null;
    lastStreamQuality = 0;
    if (frameClients.size > 0) setTimeout(() => ensureUpstream(), 1000);
  });
  upstreamWs.on('error', (err) => setLastError(err));
}

// Re-attempt upstream connect periodically until it sticks
setInterval(() => {
  if (frameClients.size > 0 && (!upstreamWs || upstreamWs.readyState !== WebSocket.OPEN)) ensureUpstream();
  if (frameClients.size === 0 && upstreamWs && upstreamWs.readyState === WebSocket.OPEN) {
    sendScreencastStop();
    try { upstreamWs.close(); } catch (_) {}
  }
}, 3000);

// Periodically refresh CSS viewport so click scaling stays accurate even if
// the page mutates window dimensions (e.g. mobile-emulating sites).
setInterval(() => { refreshCssViewport().catch(() => {}); }, 5000);

// ------------------------------------------------------------------
// HTTP API (drop-in for old endpoints)
// ------------------------------------------------------------------

function send(res, status, data) {
  const body = JSON.stringify(data);
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

async function readBody(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  if (!chunks.length) return {};
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch (_) { return {}; }
}

function scalePoint(body) {
  // body.x / body.y are in the source coord space of body.displayWidth/Height
  // (normally the streamed JPEG frame). CDP input events consume CSS viewport
  // pixels, which can differ from the JPEG dimensions on pages with browser
  // UI/zoom/device metrics, so frame coords map to cssViewport by default.
  const target =
    body.targetSpace === 'frame'
      ? lastViewport
      : cssViewport;
  const dW = Math.max(1, Number(body.displayWidth || lastViewport.width));
  const dH = Math.max(1, Number(body.displayHeight || lastViewport.height));
  return {
    x: Math.max(0, Math.min(target.width  - 1, Number(body.x || 0) * target.width  / dW)),
    y: Math.max(0, Math.min(target.height - 1, Number(body.y || 0) * target.height / dH)),
  };
}

function snapshot() {
  refreshActiveMetadata();
  // Cheap snapshot: use cached frame + url/title from stream events
  return {
    url: lastUrl || getCurrentUrl(),
    title: lastTitle || getCurrentTitle(),
    viewport: lastViewport,         // JPEG dims (legacy callers)
    jpegSize: lastViewport,         // explicit: image pixel dims user clicks against
    cssViewport: cssViewport,       // CSS pixel viewport CDP uses
    tabs: currentTabs,
    activeTabIndex: currentTabs.findIndex(t => t.active),
    stream: {
      connected: !!(upstreamWs && upstreamWs.readyState === WebSocket.OPEN),
      clients: frameClients.size,
      quality: lastStreamQuality,
      lastFrameAt,
    },
    recordingPath: lastRecordingPath || '',
    error: lastError,
    image: lastFrame ? `data:image/jpeg;base64,${lastFrame}` : '',
    ts: Date.now(),
  };
}

function resolveRecordingPath(requestPath) {
  fs.mkdirSync(RECORDING_DIR, { recursive: true });
  const raw = String(requestPath || '').trim();
  if (!raw) return path.join(RECORDING_DIR, `studio-${Date.now()}.webm`);
  const resolved = path.resolve(raw.startsWith('/') ? raw : path.join(RECORDING_DIR, raw));
  const allowedRoot = path.resolve('/opt/data');
  if (!resolved.startsWith(`${allowedRoot}/`)) {
    throw new Error('recording path must be under /opt/data');
  }
  return resolved.endsWith('.webm') ? resolved : `${resolved}.webm`;
}

const server = http.createServer(async (req, res) => {
  if (req.method === 'OPTIONS') {
    res.writeHead(204, { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*' });
    return res.end();
  }
  try {
    const url = new URL(req.url, `http://127.0.0.1:${PORT}`);

    if (req.method === 'GET' && url.pathname === '/health') {
      return send(res, 200, {
        ok: true,
        streamClients: frameClients.size,
        streamConnected: !!(upstreamWs && upstreamWs.readyState === WebSocket.OPEN),
        lastError,
      });
    }

    if (req.method === 'GET' && url.pathname === '/state') {
      ensureBrowser();
      if (frameClients.size > 0 || !lastFrame) ensureUpstream(true);
      return send(res, 200, snapshot());
    }

    if (req.method === 'GET' && url.pathname === '/snapshot') {
      ensureBrowser();
      const interactive = url.searchParams.get('interactive');
      const compact = url.searchParams.get('compact');
      const args = ['snapshot'];
      if (interactive === '1' || interactive === 'true') args.push('--interactive');
      if (compact !== '0' && compact !== 'false') args.push('--compact');
      const snap = abJSON(args);
      if (!snap || snap.success === false) {
        return send(res, 500, { error: (snap && snap.error) || 'snapshot failed' });
      }
      return send(res, 200, { ...snap.data, state: snapshot() });
    }

    const body = await readBody(req);

    if (req.method === 'POST' && url.pathname === '/navigate') {
      ensureBrowser();
      ensureUpstream(true);
      const target = normalizeUrl(body.url);
      const oldUrl = lastUrl;
      try {
        await abRun(['open', target]);
        resetCdp();
      } catch (e) {
        setLastError(e);
        try { await cdpNavigate(target); } catch (_) {}
      }
      // Wait up to 5s for lastUrl to update via the streaming tabs event,
      // then refresh from agent-browser CLI as a fallback. This way the
      // response carries the actual post-nav URL/title.
      const waitUntil = Date.now() + 5000;
      while (lastUrl === oldUrl && Date.now() < waitUntil) {
        await new Promise(r => setTimeout(r, 100));
      }
      if (lastUrl === oldUrl) {
        // Streaming tabs event hasn't fired — query CLI directly
        const cliUrl = getCurrentUrl();
        const cliTitle = getCurrentTitle();
        if (cliUrl) lastUrl = cliUrl;
        if (cliTitle) lastTitle = cliTitle;
      }
      currentTabs = getTabs();
      return send(res, 200, snapshot());
    }

    if (req.method === 'POST' && url.pathname === '/reload') {
      try { await abRun(['reload']); } catch (_) {}
      return send(res, 200, snapshot());
    }
    if (req.method === 'POST' && url.pathname === '/back') {
      try { await abRun(['back']); } catch (_) {}
      return send(res, 200, snapshot());
    }
    if (req.method === 'POST' && url.pathname === '/forward') {
      try { await abRun(['forward']); } catch (_) {}
      return send(res, 200, snapshot());
    }
    if (req.method === 'POST' && url.pathname === '/click') {
      const pt = scalePoint(body);
      try {
        await abRun(['mouse', 'move', String(Math.round(pt.x)), String(Math.round(pt.y))]);
        await abRun(['mouse', 'down', body.button === 'right' ? 'right' : 'left']);
        await abRun(['mouse', 'up', body.button === 'right' ? 'right' : 'left']);
      } catch (e) {
        try { await cdpClick(pt.x, pt.y, body.button === 'right' ? 'right' : 'left', Number(body.clickCount || 1)); }
        catch (err) { return send(res, 500, { error: err.message }); }
      }
      return send(res, 200, { ok: true });
    }
    if (req.method === 'POST' && url.pathname === '/scroll') {
      const pt = scalePoint(body);
      try {
        await abRun(['mouse', 'move', String(Math.round(pt.x)), String(Math.round(pt.y))]);
        await abRun(['mouse', 'wheel', String(Math.round(Number(body.deltaY || 0))), String(Math.round(Number(body.deltaX || 0)))]);
      } catch (e) {
        try {
          await cdpMouseMove(pt.x, pt.y);
          await cdpWheel(pt.x, pt.y, Number(body.deltaX || 0), Number(body.deltaY || 0));
        } catch (err) { return send(res, 500, { error: err.message }); }
      }
      return send(res, 200, { ok: true });
    }
    if (req.method === 'POST' && url.pathname === '/type') {
      const text = String(body.text || '');
      if (text) {
        try { await abRun(['keyboard', 'inserttext', text]); }
        catch (_) {
          const ok = await cdpInsertText(text);
          if (!ok) { try { await abRun(['keyboard', 'type', text]); } catch (_) {} }
        }
      }
      return send(res, 200, { ok: true });
    }
    if (req.method === 'POST' && url.pathname === '/key') {
      try { await abRun(['press', String(body.key || 'Enter')]); }
      catch (e) {
        try { await cdpKey(String(body.key || 'Enter')); }
        catch (err) { return send(res, 500, { error: err.message }); }
      }
      return send(res, 200, { ok: true });
    }
    if (req.method === 'POST' && url.pathname === '/tab/new') {
      const target = body.url ? normalizeUrl(body.url) : '';
      try {
        await abRun(['tab', 'new']);
        if (target) {
          await abRun(['open', target]);
        }
        resetCdp();
        await new Promise(r => setTimeout(r, 500));
        currentTabs = getTabs();
        return send(res, 200, snapshot());
      } catch (e) {
        setLastError(e);
        return send(res, 500, { error: e.message });
      }
    }
    if (req.method === 'POST' && url.pathname === '/tab/select') {
      try {
        await abRun(['tab', tabRefFromIndex(body.index)]);
        resetCdp();
        await new Promise(r => setTimeout(r, 300));
        currentTabs = getTabs();
        return send(res, 200, snapshot());
      } catch (e) {
        setLastError(e);
        return send(res, 500, { error: e.message });
      }
    }
    if (req.method === 'POST' && url.pathname === '/tab/close') {
      try {
        await abRun(['tab', 'close', tabRefFromIndex(body.index)]);
        resetCdp();
        await new Promise(r => setTimeout(r, 300));
        currentTabs = getTabs();
        return send(res, 200, snapshot());
      } catch (e) {
        setLastError(e);
        return send(res, 500, { error: e.message });
      }
    }
    if (req.method === 'POST' && url.pathname === '/record/start') {
      try {
        const recordPath = resolveRecordingPath(body.path);
        await abRun(['record', 'start', recordPath]);
        lastRecordingPath = recordPath;
        return send(res, 200, { ok: true, path: recordPath, ...snapshot() });
      } catch (e) {
        setLastError(e);
        return send(res, 500, { error: e.message });
      }
    }
    if (req.method === 'POST' && url.pathname === '/record/stop') {
      try {
        await abRun(['record', 'stop']);
        const recordPath = lastRecordingPath;
        lastRecordingPath = '';
        return send(res, 200, { ok: true, path: recordPath, ...snapshot() });
      } catch (e) {
        setLastError(e);
        return send(res, 500, { error: e.message });
      }
    }
    if (req.method === 'POST' && url.pathname === '/cookies/import') {
      // Use CDP Network.setCookies — agent-browser doesn't expose this directly
      await ensureCdp();
      const cookies = Array.isArray(body.cookies) ? body.cookies : [];
      try {
        const normalized = cookies.map(c => ({
          name: String(c.name),
          value: String(c.value),
          domain: c.domain || '',
          path: c.path || '/',
          expires: typeof c.expires === 'number' ? c.expires : -1,
          httpOnly: !!c.httpOnly,
          secure: c.secure !== false,
          sameSite: c.sameSite || 'Lax',
        })).filter(c => c.name && c.value && c.domain);
        await cdpSend('Network.setCookies', { cookies: normalized });
        if (body.verifyUrl) {
          await cdpNavigate(String(body.verifyUrl));
          await new Promise(r => setTimeout(r, 1000));
        }
        return send(res, 200, { imported: normalized.length, ...snapshot() });
      } catch (e) {
        return send(res, 500, { error: e.message });
      }
    }
    if (req.method === 'GET' && url.pathname === '/cookies/list') {
      await ensureCdp();
      const filter = url.searchParams.get('domain') || '';
      try {
        const r = await cdpSend('Network.getAllCookies');
        const all = r.cookies || [];
        const filtered = filter ? all.filter(c => (c.domain || '').includes(filter)) : all;
        return send(res, 200, { cookies: filtered.map(c => ({ name: c.name, domain: c.domain, expires: c.expires })) });
      } catch (e) {
        return send(res, 500, { error: e.message });
      }
    }

    return send(res, 404, { error: 'not found' });
  } catch (err) {
    return send(res, 500, { error: String(err && err.stack || err) });
  }
});

// ------------------------------------------------------------------
// Our public WS endpoint: bridge to clients
// ------------------------------------------------------------------

const wss = new WebSocketServer({ server, path: '/ws/stream' });

wss.on('connection', (ws) => {
  frameClients.add(ws);
  ensureBrowser();
  ensureUpstream(true);
  sendScreencastStart();

  // Send the most recent frame immediately (avoid black on first connect)
  if (lastFrame) {
    try { ws.send(Buffer.from(lastFrame, 'base64')); } catch (_) {}
  }

  ws.on('message', async (raw) => {
    let msg;
    try { msg = JSON.parse(raw.toString()); } catch (_) { return; }
    if (!msg || !msg.type) return;
    try {
      if (msg.type === 'click') {
        const pt = scalePoint(msg);
        try {
          await abRun(['mouse', 'move', String(Math.round(pt.x)), String(Math.round(pt.y))]);
          await abRun(['mouse', 'down', msg.button === 'right' ? 'right' : 'left']);
          await abRun(['mouse', 'up', msg.button === 'right' ? 'right' : 'left']);
        } catch (_) {
          await cdpClick(pt.x, pt.y, msg.button === 'right' ? 'right' : 'left', Number(msg.clickCount || 1));
        }
      } else if (msg.type === 'mousemove') {
        const pt = scalePoint(msg);
        await cdpMouseMove(pt.x, pt.y);
      } else if (msg.type === 'wheel') {
        const pt = scalePoint(msg);
        try {
          await abRun(['mouse', 'move', String(Math.round(pt.x)), String(Math.round(pt.y))]);
          await abRun(['mouse', 'wheel', String(Math.round(Number(msg.deltaY || 0))), String(Math.round(Number(msg.deltaX || 0)))]);
        } catch (_) {
          await cdpWheel(pt.x, pt.y, Number(msg.deltaX || 0), Number(msg.deltaY || 0));
        }
      } else if (msg.type === 'type') {
        const text = String(msg.text || '');
        if (text) {
          try { await abRun(['keyboard', 'inserttext', text]); }
          catch (_) {
            const ok = await cdpInsertText(text);
            if (!ok) { try { await abRun(['keyboard', 'type', text]); } catch (_) {} }
          }
        }
      } else if (msg.type === 'key') {
        try { await abRun(['press', String(msg.key || 'Enter')]); }
        catch (_) { await cdpKey(String(msg.key || 'Enter')); }
      } else if (msg.type === 'navigate') {
        try {
          await abRun(['open', normalizeUrl(msg.url)]);
          resetCdp();
        } catch (_) { try { await cdpNavigate(normalizeUrl(msg.url)); } catch (_) {} }
      }
    } catch (err) {
      try { ws.send(JSON.stringify({ type: 'error', error: String(err.message || err) })); } catch (_) {}
    }
  });

  ws.on('close', () => {
    frameClients.delete(ws);
    if (frameClients.size === 0) sendScreencastStop();
  });
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`studio-browser-server (agent-browser shim) listening on ${PORT}`);
  ensureBrowser();
});

async function shutdown() {
  try { if (cdp) cdp.close(); } catch (_) {}
  try { if (upstreamWs) upstreamWs.close(); } catch (_) {}
  process.exit(0);
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
