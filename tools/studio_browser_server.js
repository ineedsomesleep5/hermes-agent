const http = require('http');
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const PORT = Number(process.env.STUDIO_BROWSER_PORT || 9322);
const PROFILE = process.env.STUDIO_BROWSER_PROFILE || '/opt/data/profiles/supervisor/home/.studio-browser-profile';
const SEARCH_HOME = process.env.STUDIO_BROWSER_SEARCH_HOME || 'https://duckduckgo.com/';
const SEARCH_URL = process.env.STUDIO_BROWSER_SEARCH_URL || 'https://duckduckgo.com/?q=';
const VIEWPORT = { width: 1280, height: 820 };
const CANDIDATES = [
  process.env.STUDIO_BROWSER_CHROME,
  '/opt/data/profiles/supervisor/home/.cache/puppeteer/chrome/linux-146.0.7680.153/chrome-linux64/chrome',
  '/opt/data/profiles/supervisor/home/.cache/puppeteer/chrome-headless-shell/linux-146.0.7680.153/chrome-headless-shell-linux64/chrome-headless-shell',
  '/opt/data/profiles/supervisor/home/.cache/hyperframes/chrome/chrome-headless-shell/linux-131.0.6778.85/chrome-headless-shell-linux64/chrome-headless-shell',
].filter(Boolean);

let context;
let page;
let launching;

function chromePath() {
  for (const p of CANDIDATES) {
    try {
      if (fs.existsSync(p)) return p;
    } catch (_) {}
  }
  return undefined;
}

function normalizeUrl(value) {
  const raw = String(value || '').trim();
  if (!raw) return SEARCH_HOME;
  if (/^https?:\/\//i.test(raw)) return raw;
  if (/^[a-z0-9.-]+\.[a-z]{2,}(\/|$|\?|#)/i.test(raw) || /^localhost(:\d+)?(\/|$)/i.test(raw)) return `https://${raw}`;
  return `${SEARCH_URL}${encodeURIComponent(raw)}`;
}

async function ensurePage() {
  if (page && !page.isClosed()) return page;
  if (launching) return launching;
  launching = (async () => {
    fs.mkdirSync(PROFILE, { recursive: true });
    context = await chromium.launchPersistentContext(PROFILE, {
      executablePath: chromePath(),
      headless: true,
      viewport: VIEWPORT,
      ignoreHTTPSErrors: true,
      args: [
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
        '--disable-extensions',
        '--disable-background-networking',
        '--window-size=1280,820',
      ],
    });
    page = context.pages()[0] || await context.newPage();
    page.setDefaultTimeout(12000);
    if (page.url() === 'about:blank') {
      await page.goto(SEARCH_HOME, { waitUntil: 'domcontentloaded', timeout: 20000 }).catch(() => {});
    }
    launching = null;
    return page;
  })().catch((err) => {
    launching = null;
    throw err;
  });
  return launching;
}

async function readBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  if (!chunks.length) return {};
  const raw = Buffer.concat(chunks).toString('utf8');
  try { return JSON.parse(raw); } catch (_) { return {}; }
}

function send(res, status, data) {
  const body = JSON.stringify(data);
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

async function snapshot() {
  const p = await ensurePage();
  let image = '';
  let error = '';
  try {
    const png = await p.screenshot({
      type: 'png',
      fullPage: false,
      timeout: 30000,
      animations: 'disabled',
      caret: 'hide',
    });
    image = `data:image/png;base64,${png.toString('base64')}`;
  } catch (err) {
    error = String(err && err.message || err);
  }
  return {
    url: p.url(),
    title: await p.title().catch(() => ''),
    viewport: VIEWPORT,
    image,
    error,
    ts: Date.now(),
  };
}

function scalePoint(body) {
  const displayW = Math.max(1, Number(body.displayWidth || VIEWPORT.width));
  const displayH = Math.max(1, Number(body.displayHeight || VIEWPORT.height));
  return {
    x: Math.max(0, Math.min(VIEWPORT.width - 1, Number(body.x || 0) * VIEWPORT.width / displayW)),
    y: Math.max(0, Math.min(VIEWPORT.height - 1, Number(body.y || 0) * VIEWPORT.height / displayH)),
  };
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, `http://127.0.0.1:${PORT}`);
    if (req.method === 'GET' && url.pathname === '/health') return send(res, 200, { ok: true });
    if (req.method === 'GET' && url.pathname === '/state') return send(res, 200, await snapshot());

    const body = await readBody(req);
    const p = await ensurePage();

    if (req.method === 'POST' && url.pathname === '/navigate') {
      await p.goto(normalizeUrl(body.url), { waitUntil: 'domcontentloaded', timeout: 25000 }).catch(() => {});
      return send(res, 200, await snapshot());
    }
    if (req.method === 'POST' && url.pathname === '/reload') {
      await p.reload({ waitUntil: 'domcontentloaded', timeout: 20000 }).catch(() => {});
      return send(res, 200, await snapshot());
    }
    if (req.method === 'POST' && url.pathname === '/back') {
      await p.goBack({ waitUntil: 'domcontentloaded', timeout: 15000 }).catch(() => {});
      return send(res, 200, await snapshot());
    }
    if (req.method === 'POST' && url.pathname === '/forward') {
      await p.goForward({ waitUntil: 'domcontentloaded', timeout: 15000 }).catch(() => {});
      return send(res, 200, await snapshot());
    }
    if (req.method === 'POST' && url.pathname === '/click') {
      const point = scalePoint(body);
      await p.mouse.click(point.x, point.y);
      return send(res, 200, await snapshot());
    }
    if (req.method === 'POST' && url.pathname === '/scroll') {
      const point = scalePoint(body);
      await p.mouse.move(point.x, point.y);
      await p.mouse.wheel(Number(body.deltaX || 0), Number(body.deltaY || 0));
      return send(res, 200, await snapshot());
    }
    if (req.method === 'POST' && url.pathname === '/type') {
      await p.keyboard.type(String(body.text || ''), { delay: 5 });
      return send(res, 200, await snapshot());
    }
    if (req.method === 'POST' && url.pathname === '/key') {
      await p.keyboard.press(String(body.key || 'Enter'));
      return send(res, 200, await snapshot());
    }
    return send(res, 404, { error: 'not found' });
  } catch (err) {
    return send(res, 500, { error: String(err && err.stack || err) });
  }
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`studio-browser-server listening on ${PORT}`);
});

async function shutdown() {
  try { if (context) await context.close(); } catch (_) {}
  process.exit(0);
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
