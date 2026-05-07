"""Embedded browser renderer for Hermes Studio.

Two modes:
- iframe (fast, default) — direct page embed, fails on sites that block frame-ancestors (X, FB, LinkedIn, ...).
- snapshot — Playwright-driven Chrome on port 9322 returns base64 PNGs through /api/studio/browser/state;
  clicks/scroll/keystrokes get forwarded back to it. Works on every site, slower.

The widget auto-suggests snapshot mode when the user navigates to a known-blocked domain.
"""

EMBEDDED_BROWSER_RENDERER = r"""async (parent, spaceRef) => {
  parent.style.cssText = "position:relative;height:100%;display:flex;flex-direction:column;background:#1e1e1e;border-radius:8px;overflow:hidden;font-family:-apple-system, Inter, system-ui, sans-serif;";

  // ---------- helpers ----------
  const SESSION_HEADER = 'X-Hermes-Session-Token';
  const BLOCKED_HOSTS = ['x.com','twitter.com','www.x.com','www.twitter.com','facebook.com','www.facebook.com','instagram.com','www.instagram.com','linkedin.com','www.linkedin.com','reddit.com','www.reddit.com'];

  function getSessionToken() {
    try {
      if (window.__HERMES_SESSION_TOKEN__) return window.__HERMES_SESSION_TOKEN__;
      const m = document.cookie.match(/(?:^|; )hermes_session=([^;]+)/);
      if (m) return decodeURIComponent(m[1]);
    } catch (_) {}
    return '';
  }

  async function api(path, body) {
    const headers = { 'Content-Type': 'application/json' };
    const tok = getSessionToken();
    if (tok) headers[SESSION_HEADER] = tok;
    const opts = body === undefined
      ? { method: 'GET', headers }
      : { method: 'POST', headers, body: JSON.stringify(body) };
    const res = await fetch(path, opts);
    if (!res.ok) throw new Error(path + ' -> ' + res.status);
    return res.json();
  }

  function getUrl(url) {
    if (!url) return 'about:blank';
    if (!url.startsWith('http://') && !url.startsWith('https://')) url = 'https://' + url;
    return url;
  }

  function hostOf(url) {
    try { return new URL(getUrl(url)).hostname.toLowerCase(); } catch (_) { return ''; }
  }

  function isBlocked(url) {
    const h = hostOf(url);
    return BLOCKED_HOSTS.some(b => h === b || h.endsWith('.' + b));
  }

  // ---------- header ----------
  const header = document.createElement('div');
  header.style.cssText = "display:flex;padding:12px;background:linear-gradient(180deg, rgba(20,20,24,0.98), rgba(10,10,12,0.98));border-bottom:1px solid #3d3d3d;gap:8px;align-items:center;";

  function makeBtn(label) {
    const b = document.createElement('button');
    b.innerHTML = label;
    b.style.cssText = "padding:6px 10px;border-radius:4px;border:1px solid #444;background:#2d2d2d;color:white;cursor:pointer;font-size:14px;";
    b.onmouseenter = () => b.style.background = '#3d3d3d';
    b.onmouseleave = () => b.style.background = '#2d2d2d';
    return b;
  }

  const backBtn = makeBtn('&#8592;');
  const fwdBtn = makeBtn('&#8594;');
  const reloadBtn = makeBtn('&#8635;');

  const input = document.createElement('input');
  input.type = "text";
  input.placeholder = "Enter URL (e.g., https://duckduckgo.com)";
  input.style.cssText = "flex:1;padding:8px 12px;border-radius:6px;border:1px solid rgba(255,255,255,0.12);background:rgba(255,255,255,0.04);color:#f4f4f5;outline:none;font-size:13px;";
  input.value = "https://duckduckgo.com";

  const goBtn = document.createElement('button');
  goBtn.textContent = "Go";
  goBtn.style.cssText = "padding:8px 16px;border-radius:6px;border:none;background:#3b82f6;color:white;cursor:pointer;font-weight:bold;font-size:13px;";
  goBtn.onmouseenter = () => goBtn.style.background = '#2563eb';
  goBtn.onmouseleave = () => goBtn.style.background = '#3b82f6';

  const modeBtn = makeBtn('&#127760; Iframe');
  modeBtn.title = "Switch between iframe (fast) and snapshot (works on X, etc.)";

  header.appendChild(backBtn);
  header.appendChild(fwdBtn);
  header.appendChild(reloadBtn);
  header.appendChild(input);
  header.appendChild(goBtn);
  header.appendChild(modeBtn);
  parent.appendChild(header);

  // ---------- banner (mode suggestion) ----------
  const banner = document.createElement('div');
  banner.style.cssText = "display:none;padding:8px 14px;background:#7c2d12;color:#fed7aa;font-size:12px;align-items:center;gap:10px;";
  const bannerMsg = document.createElement('span');
  bannerMsg.style.flex = '1';
  const bannerYes = document.createElement('button');
  bannerYes.textContent = 'Switch to Snapshot';
  bannerYes.style.cssText = "padding:4px 10px;border-radius:4px;border:none;background:#fed7aa;color:#7c2d12;cursor:pointer;font-weight:bold;";
  const bannerNo = document.createElement('button');
  bannerNo.textContent = 'Dismiss';
  bannerNo.style.cssText = "padding:4px 10px;border-radius:4px;border:1px solid #fed7aa;background:transparent;color:#fed7aa;cursor:pointer;";
  banner.appendChild(bannerMsg);
  banner.appendChild(bannerYes);
  banner.appendChild(bannerNo);
  parent.appendChild(banner);

  // ---------- viewports (iframe + snapshot) ----------
  const stage = document.createElement('div');
  stage.style.cssText = "flex:1;position:relative;background:#000;overflow:hidden;";
  parent.appendChild(stage);

  const iframe = document.createElement('iframe');
  iframe.style.cssText = "position:absolute;inset:0;width:100%;height:100%;border:none;background:white;";
  iframe.sandbox = "allow-same-origin allow-scripts allow-popups allow-forms allow-modals";
  stage.appendChild(iframe);

  const shotWrap = document.createElement('div');
  shotWrap.style.cssText = "display:none;position:absolute;inset:0;background:#0a0a0a;overflow:auto;";
  const shotImg = document.createElement('img');
  shotImg.style.cssText = "display:block;width:100%;height:auto;cursor:crosshair;user-select:none;";
  shotImg.draggable = false;
  shotWrap.appendChild(shotImg);
  stage.appendChild(shotWrap);

  const status = document.createElement('div');
  status.style.cssText = "position:absolute;left:10px;bottom:8px;padding:4px 10px;border-radius:12px;background:rgba(0,0,0,0.55);color:#a3a3a3;font-size:11px;pointer-events:none;font-family:ui-monospace,monospace;";
  status.textContent = '';
  stage.appendChild(status);

  // ---------- state ----------
  let mode = 'iframe';
  let history = [input.value];
  let historyIndex = 0;
  let pollTimer = null;
  let snapshotShape = { width: 1280, height: 820 };

  function setStatus(text) { status.textContent = text || ''; }

  function showBanner(message, onYes) {
    bannerMsg.textContent = message;
    banner.style.display = 'flex';
    bannerYes.onclick = () => { banner.style.display = 'none'; onYes(); };
    bannerNo.onclick = () => { banner.style.display = 'none'; };
  }

  function setMode(next) {
    mode = next;
    if (mode === 'iframe') {
      modeBtn.innerHTML = '&#127760; Iframe';
      iframe.style.display = '';
      shotWrap.style.display = 'none';
      stopPolling();
      setStatus('');
    } else {
      modeBtn.innerHTML = '&#128247; Snapshot';
      iframe.style.display = 'none';
      shotWrap.style.display = 'block';
      const url = getUrl(input.value);
      api('/api/studio/browser/navigate', { url })
        .then((state) => { applyState(state); startPolling(); })
        .catch((e) => setStatus('snapshot error: ' + e.message));
    }
  }

  modeBtn.addEventListener('click', () => setMode(mode === 'iframe' ? 'snapshot' : 'iframe'));

  // ---------- iframe navigation ----------
  function navigate(rawUrl) {
    if (!rawUrl) return;
    const url = getUrl(rawUrl);
    input.value = url;

    if (history[historyIndex] !== url) {
      history = history.slice(0, historyIndex + 1);
      history.push(url);
      historyIndex++;
    }

    if (mode === 'snapshot') {
      api('/api/studio/browser/navigate', { url }).catch((e) => setStatus('nav error: ' + e.message));
    } else {
      iframe.src = url;
      if (isBlocked(url)) {
        showBanner('This site blocks iframes — switch to Snapshot mode?', () => setMode('snapshot'));
      }
    }
  }

  iframe.src = getUrl(input.value);

  goBtn.addEventListener('click', () => navigate(input.value.trim()));
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') navigate(input.value.trim()); });

  backBtn.addEventListener('click', () => {
    if (mode === 'snapshot') { api('/api/studio/browser/back', {}).catch(() => {}); return; }
    try { iframe.contentWindow.history.back(); }
    catch (e) {
      if (historyIndex > 0) {
        historyIndex--;
        input.value = history[historyIndex];
        iframe.src = history[historyIndex];
      }
    }
  });

  fwdBtn.addEventListener('click', () => {
    if (mode === 'snapshot') { api('/api/studio/browser/forward', {}).catch(() => {}); return; }
    try { iframe.contentWindow.history.forward(); }
    catch (e) {
      if (historyIndex < history.length - 1) {
        historyIndex++;
        input.value = history[historyIndex];
        iframe.src = history[historyIndex];
      }
    }
  });

  reloadBtn.addEventListener('click', () => {
    if (mode === 'snapshot') { api('/api/studio/browser/reload', {}).catch(() => {}); return; }
    try { iframe.contentWindow.location.reload(); }
    catch (e) {
      const cur = iframe.src;
      iframe.src = 'about:blank';
      setTimeout(() => { iframe.src = cur; }, 10);
    }
  });

  // ---------- snapshot polling + input forwarding ----------
  function applyState(state) {
    if (!state) return;
    if (state.image) shotImg.src = state.image;
    if (state.viewport) {
      snapshotShape = { width: state.viewport.width || 1280, height: state.viewport.height || 820 };
    }
    if (state.url) {
      input.value = state.url;
      setStatus(state.title ? (state.title + '  —  ' + state.url) : state.url);
    }
  }

  async function pollOnce() {
    try { applyState(await api('/api/studio/browser/state')); }
    catch (e) { setStatus('poll error: ' + e.message); }
  }

  function startPolling() {
    stopPolling();
    pollOnce();
    pollTimer = setInterval(pollOnce, 1500);
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function pointFromEvent(ev) {
    const rect = shotImg.getBoundingClientRect();
    const x = (ev.clientX - rect.left) * (snapshotShape.width / rect.width);
    const y = (ev.clientY - rect.top) * (snapshotShape.height / rect.height);
    return {
      x, y,
      displayWidth: snapshotShape.width,
      displayHeight: snapshotShape.height,
    };
  }

  shotImg.addEventListener('click', async (ev) => {
    ev.preventDefault();
    const pt = pointFromEvent(ev);
    try { applyState(await api('/api/studio/browser/click', pt)); }
    catch (e) { setStatus('click error: ' + e.message); }
  });

  shotWrap.addEventListener('wheel', async (ev) => {
    ev.preventDefault();
    const pt = pointFromEvent(ev);
    pt.deltaX = ev.deltaX;
    pt.deltaY = ev.deltaY;
    try { await api('/api/studio/browser/scroll', pt); }
    catch (e) { setStatus('scroll error: ' + e.message); }
  }, { passive: false });

  parent.addEventListener('keydown', async (ev) => {
    if (mode !== 'snapshot') return;
    if (document.activeElement === input) return;
    const single = ev.key.length === 1;
    const named = ['Enter','Backspace','Tab','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Escape','Delete','Home','End','PageUp','PageDown'];
    if (!single && !named.includes(ev.key)) return;
    ev.preventDefault();
    try {
      if (single && !ev.ctrlKey && !ev.metaKey) {
        await api('/api/studio/browser/type', { text: ev.key });
      } else {
        await api('/api/studio/browser/key', { key: ev.key });
      }
    } catch (e) { setStatus('key error: ' + e.message); }
  });

  // initial banner if user starts on a blocked URL
  if (isBlocked(input.value)) {
    showBanner('This site blocks iframes — switch to Snapshot mode?', () => setMode('snapshot'));
  }
}"""
