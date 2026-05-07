"""Embedded browser renderer for Hermes Studio.

Two modes:
- iframe (default, fast) — direct page embed, fails on sites that block frame-ancestors.
- snapshot — Playwright-driven Chrome streamed over WebSocket via CDP screencast.
  Real-time JPEG frames at the browser's natural paint rate (~30fps), bidirectional
  input over the same socket. Falls back to HTTP polling if WebSocket fails.

The widget auto-suggests snapshot mode when the user navigates to a known
iframe-blocking site.
"""

EMBEDDED_BROWSER_RENDERER = r"""async (parent, spaceRef) => {
  parent.style.cssText = "position:relative;height:100%;display:flex;flex-direction:column;background:#1e1e1e;border-radius:8px;overflow:hidden;font-family:-apple-system, Inter, system-ui, sans-serif;";

  const SESSION_HEADER = 'X-Hermes-Session-Token';
  const BLOCKED_HOSTS = ['x.com','twitter.com','www.x.com','www.twitter.com','facebook.com','www.facebook.com','instagram.com','www.instagram.com','linkedin.com','www.linkedin.com','reddit.com','www.reddit.com'];
  const VIEWPORT = { width: 1280, height: 720 };  // updated from actual frame on first decode

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
  input.placeholder = "Enter URL (e.g., https://google.com) or search terms";
  input.style.cssText = "flex:1;padding:8px 12px;border-radius:6px;border:1px solid rgba(255,255,255,0.12);background:rgba(255,255,255,0.04);color:#f4f4f5;outline:none;font-size:13px;";
  input.value = "https://www.google.com";

  const goBtn = document.createElement('button');
  goBtn.textContent = "Go";
  goBtn.style.cssText = "padding:8px 16px;border-radius:6px;border:none;background:#3b82f6;color:white;cursor:pointer;font-weight:bold;font-size:13px;";

  const modeBtn = makeBtn('&#127760; Iframe');
  modeBtn.title = "Switch between iframe (fast) and snapshot (works on X, etc.)";

  header.appendChild(backBtn);
  header.appendChild(fwdBtn);
  header.appendChild(reloadBtn);
  header.appendChild(input);
  header.appendChild(goBtn);
  header.appendChild(modeBtn);
  parent.appendChild(header);

  // ---------- viewports ----------
  const stage = document.createElement('div');
  stage.style.cssText = "flex:1;position:relative;background:#000;overflow:hidden;";
  parent.appendChild(stage);

  const iframe = document.createElement('iframe');
  iframe.style.cssText = "position:absolute;inset:0;width:100%;height:100%;border:none;background:white;";
  iframe.sandbox = "allow-same-origin allow-scripts allow-popups allow-forms allow-modals";
  stage.appendChild(iframe);

  // Snapshot stage uses canvas (faster than img src= for streaming)
  const shotWrap = document.createElement('div');
  shotWrap.style.cssText = "display:none;position:absolute;inset:0;background:#0a0a0a;overflow:hidden;";
  shotWrap.tabIndex = 0; // make it focusable for keyboard events
  shotWrap.style.outline = 'none';
  const shotCanvas = document.createElement('canvas');
  shotCanvas.width = VIEWPORT.width;
  shotCanvas.height = VIEWPORT.height;
  shotCanvas.style.cssText = "display:block;width:100%;height:100%;object-fit:fill;cursor:crosshair;outline:none;";
  const shotCtx = shotCanvas.getContext('2d');
  shotWrap.appendChild(shotCanvas);

  // Hidden input absorbs keystrokes when snapshot is focused (lets us
  // capture every key including IME composition). Positioned off-screen.
  const hiddenInput = document.createElement('textarea');
  hiddenInput.style.cssText = "position:absolute;left:-9999px;top:0;width:1px;height:1px;opacity:0;";
  hiddenInput.autocapitalize = 'off';
  hiddenInput.autocomplete = 'off';
  hiddenInput.spellcheck = false;
  shotWrap.appendChild(hiddenInput);
  stage.appendChild(shotWrap);

  const status = document.createElement('div');
  status.style.cssText = "position:absolute;left:10px;bottom:8px;padding:4px 10px;border-radius:12px;background:rgba(0,0,0,0.55);color:#a3a3a3;font-size:11px;pointer-events:none;font-family:ui-monospace,monospace;max-width:60%;text-overflow:ellipsis;overflow:hidden;white-space:nowrap;";
  stage.appendChild(status);

  const focusHint = document.createElement('div');
  focusHint.style.cssText = "display:none;position:absolute;right:10px;bottom:8px;padding:4px 10px;border-radius:12px;background:rgba(59,130,246,0.85);color:white;font-size:11px;pointer-events:none;";
  focusHint.textContent = 'click to type — Esc to release';
  stage.appendChild(focusHint);

  // ---------- state ----------
  let mode = 'iframe';
  let history = [input.value];
  let historyIndex = 0;
  let ws = null;
  let wsBlobUrl = null;
  let pollTimer = null;
  let stateTimer = null; // for url/title polling (not screen frames)
  let lastFrameAt = 0;

  function setStatus(text) { status.textContent = text || ''; }

  function setMode(next) {
    const prevUrl = getUrl(input.value);
    mode = next;
    if (mode === 'iframe') {
      modeBtn.innerHTML = '&#127760; Iframe';
      iframe.style.display = '';
      shotWrap.style.display = 'none';
      focusHint.style.display = 'none';
      stopStreaming();
      setStatus('');
      // Snapshot → iframe: load the same URL in the iframe (if it doesn't
      // block frame embedding). isBlocked check happens on next nav.
      if (prevUrl && prevUrl !== 'about:blank' && iframe.src !== prevUrl) {
        iframe.src = prevUrl;
      }
    } else {
      modeBtn.innerHTML = '&#128247; Snapshot';
      iframe.style.display = 'none';
      shotWrap.style.display = 'block';
      startStreaming();
      // Iframe → snapshot: navigate the headless browser to the same URL
      // so the user lands on the same page in the new mode.
      api('/api/studio/browser/navigate', { url: prevUrl })
        .then(s => updateMeta(s))
        .catch(e => setStatus('nav error: ' + e.message));
    }
  }

  modeBtn.addEventListener('click', () => setMode(mode === 'iframe' ? 'snapshot' : 'iframe'));

  // ---------- streaming ----------
  function startStreaming() {
    if (ws && ws.readyState === WebSocket.OPEN) return;
    const tok = encodeURIComponent(getSessionToken());
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${proto}//${location.host}/api/studio/browser/stream?token=${tok}`;
    try {
      ws = new WebSocket(wsUrl);
      ws.binaryType = 'arraybuffer';
    } catch (e) {
      setStatus('ws connect failed: ' + e.message);
      return startPolling();
    }
    ws.addEventListener('open', () => {
      setStatus('streaming');
      startStatePolling(); // for url/title (not frames)
    });
    ws.addEventListener('message', (ev) => {
      if (ev.data instanceof ArrayBuffer) {
        // JPEG frame — decode + draw to canvas
        lastFrameAt = Date.now();
        const blob = new Blob([ev.data], { type: 'image/jpeg' });
        const url = URL.createObjectURL(blob);
        const img = new Image();
        img.onload = () => {
          // Sync viewport + canvas internal dims to actual frame size, so click
          // coords map 1:1 against the image (and stay valid if page resizes).
          if (img.naturalWidth && img.naturalWidth !== VIEWPORT.width) VIEWPORT.width = img.naturalWidth;
          if (img.naturalHeight && img.naturalHeight !== VIEWPORT.height) VIEWPORT.height = img.naturalHeight;
          if (shotCanvas.width !== VIEWPORT.width) shotCanvas.width = VIEWPORT.width;
          if (shotCanvas.height !== VIEWPORT.height) shotCanvas.height = VIEWPORT.height;
          shotCtx.drawImage(img, 0, 0, shotCanvas.width, shotCanvas.height);
          URL.revokeObjectURL(url);
        };
        img.onerror = () => URL.revokeObjectURL(url);
        img.src = url;
      } else {
        // Text message — likely error
        try {
          const obj = JSON.parse(ev.data);
          if (obj.error) setStatus('err: ' + obj.error);
        } catch (_) {}
      }
    });
    ws.addEventListener('close', () => {
      setStatus('stream closed');
      // Auto-fall-back to polling if we lose WS but mode is still snapshot
      if (mode === 'snapshot' && !pollTimer) startPolling();
    });
    ws.addEventListener('error', () => {
      setStatus('ws error — falling back to polling');
      startPolling();
    });
  }

  function stopStreaming() {
    if (ws) { try { ws.close(); } catch (_) {} ws = null; }
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    if (stateTimer) { clearInterval(stateTimer); stateTimer = null; }
  }

  function wsSend(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify(obj)); return true; } catch (_) {}
    }
    return false;
  }

  // Fallback polling (only used if WS fails)
  async function pollOnce() {
    try {
      const s = await api('/api/studio/browser/state');
      updateMeta(s);
      if (s.image) {
        const img = new Image();
        img.onload = () => shotCtx.drawImage(img, 0, 0, shotCanvas.width, shotCanvas.height);
        img.src = s.image;
      }
    } catch (e) { setStatus('poll error: ' + e.message); }
  }
  function startPolling() {
    if (pollTimer) return;
    pollOnce();
    pollTimer = setInterval(pollOnce, 1000);
  }

  // Periodic state poll (for URL/title — frames come over WS)
  async function pollStateOnly() {
    try {
      const s = await api('/api/studio/browser/state');
      // Only update meta, not image (WS handles frames)
      input.value = s.url || input.value;
      setStatus(s.title ? `${s.title}  —  ${s.url}` : (s.url || 'streaming'));
    } catch (_) {}
  }
  function startStatePolling() {
    if (stateTimer) return;
    pollStateOnly();
    stateTimer = setInterval(pollStateOnly, 4000);
  }

  function updateMeta(s) {
    if (!s) return;
    if (s.url) input.value = s.url;
    setStatus(s.title ? `${s.title}  —  ${s.url}` : (s.url || ""));
    const js = s.jpegSize || s.viewport;
    if (js && js.width && js.height) {
      if (js.width !== VIEWPORT.width || js.height !== VIEWPORT.height) {
        VIEWPORT.width = js.width;
        VIEWPORT.height = js.height;
        if (shotCanvas.width !== js.width) shotCanvas.width = js.width;
        if (shotCanvas.height !== js.height) shotCanvas.height = js.height;
      }
    }
  }

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
      // Use WS message if connected, else HTTP
      if (!wsSend({ type: 'navigate', url })) {
        api('/api/studio/browser/navigate', { url }).catch(e => setStatus('nav: ' + e.message));
      }
    } else if (isBlocked(url)) {
      setStatus('auto-snapshot for ' + hostOf(url));
      setMode('snapshot');
    } else {
      iframe.src = url;
    }
  }

  iframe.src = getUrl(input.value);

  goBtn.addEventListener('click', () => navigate(input.value.trim()));
  // When user focuses the URL bar, blur the hidden snapshot textarea so it
  // doesn't intercept keys.
  input.addEventListener('focus', () => {
    if (typeof hiddenInput !== 'undefined') {
      hiddenInput.blur();
      if (typeof focusHint !== 'undefined') focusHint.style.display = 'none';
    }
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') navigate(input.value.trim());
    // Stop propagation so nothing else captures the key
    e.stopPropagation();
  });

  backBtn.addEventListener('click', () => {
    if (mode === 'snapshot') {
      api('/api/studio/browser/back', {}).catch(() => {});
      return;
    }
    try { iframe.contentWindow.history.back(); }
    catch (e) {
      if (historyIndex > 0) { historyIndex--; input.value = history[historyIndex]; iframe.src = history[historyIndex]; }
    }
  });

  fwdBtn.addEventListener('click', () => {
    if (mode === 'snapshot') {
      api('/api/studio/browser/forward', {}).catch(() => {});
      return;
    }
    try { iframe.contentWindow.history.forward(); }
    catch (e) {
      if (historyIndex < history.length - 1) { historyIndex++; input.value = history[historyIndex]; iframe.src = history[historyIndex]; }
    }
  });

  reloadBtn.addEventListener('click', () => {
    if (mode === 'snapshot') {
      api('/api/studio/browser/reload', {}).catch(() => {});
      return;
    }
    try { iframe.contentWindow.location.reload(); }
    catch (e) {
      const cur = iframe.src;
      iframe.src = 'about:blank';
      setTimeout(() => { iframe.src = cur; }, 10);
    }
  });

  // ---------- snapshot input ----------
  function pointFromEvent(ev) {
    // Canvas is object-fit:fill — rendered image == canvas rect. Map cursor
    // to viewport coords; displayWidth/Height come from live VIEWPORT (synced
    // from actual frame in img.onload) so backend scalePoint produces the
    // same coord regardless of widget size.
    const rect = shotCanvas.getBoundingClientRect();
    const vw = VIEWPORT.width, vh = VIEWPORT.height;
    const lx = (ev.clientX - rect.left) * (vw / Math.max(1, rect.width));
    const ly = (ev.clientY - rect.top)  * (vh / Math.max(1, rect.height));
    return {
      x: Math.max(0, Math.min(vw - 1, lx)),
      y: Math.max(0, Math.min(vh - 1, ly)),
      displayWidth: vw,
      displayHeight: vh,
    };
  }

  // Clicking the canvas (a) sends click to remote (b) focuses hidden input for keystrokes
  shotCanvas.addEventListener('mousedown', (ev) => {
    ev.preventDefault();
    const pt = pointFromEvent(ev);
    if (!wsSend({ type: 'click', ...pt, button: ev.button === 2 ? 'right' : 'left' })) {
      api('/api/studio/browser/click', pt).catch(e => setStatus('click: ' + e.message));
    }
    // Take focus so subsequent keystrokes go to the remote browser
    hiddenInput.focus();
    focusHint.style.display = 'block';
    // Visual indicator the canvas is "active"
    shotWrap.style.boxShadow = 'inset 0 0 0 2px #3b82f6';
  });

  shotCanvas.addEventListener('contextmenu', (ev) => ev.preventDefault());

  // Mouse move (throttled)
  let lastMoveAt = 0;
  shotCanvas.addEventListener('mousemove', (ev) => {
    const now = Date.now();
    if (now - lastMoveAt < 50) return;
    lastMoveAt = now;
    const pt = pointFromEvent(ev);
    wsSend({ type: 'mousemove', ...pt });
  });

  // Wheel / scroll
  shotCanvas.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    const pt = pointFromEvent(ev);
    pt.deltaX = ev.deltaX;
    pt.deltaY = ev.deltaY;
    if (!wsSend({ type: 'wheel', ...pt })) {
      api('/api/studio/browser/scroll', pt).catch(() => {});
    }
  }, { passive: false });

  // Keyboard — listen on hidden input to capture all keys including IME
  hiddenInput.addEventListener('keydown', (ev) => {
    // Special keys go through /key
    const named = {
      'Enter':'Enter','Backspace':'Backspace','Tab':'Tab','Escape':'Escape',
      'ArrowLeft':'ArrowLeft','ArrowRight':'ArrowRight','ArrowUp':'ArrowUp','ArrowDown':'ArrowDown',
      'Delete':'Delete','Home':'Home','End':'End','PageUp':'PageUp','PageDown':'PageDown',
    };
    if (ev.key === 'Escape') {
      hiddenInput.blur();
      focusHint.style.display = 'none';
      ev.preventDefault();
      return;
    }
    if (named[ev.key]) {
      ev.preventDefault();
      if (!wsSend({ type: 'key', key: named[ev.key] })) {
        api('/api/studio/browser/key', { key: named[ev.key] }).catch(() => {});
      }
      return;
    }
    // Ctrl/Cmd combos go through /key too
    if ((ev.ctrlKey || ev.metaKey) && ev.key.length === 1) {
      ev.preventDefault();
      const combo = (ev.metaKey ? 'Meta+' : '') + (ev.ctrlKey ? 'Control+' : '') + (ev.shiftKey ? 'Shift+' : '') + (ev.altKey ? 'Alt+' : '') + ev.key.toUpperCase();
      if (!wsSend({ type: 'key', key: combo })) {
        api('/api/studio/browser/key', { key: combo }).catch(() => {});
      }
      return;
    }
    // Otherwise let it through to the textarea — `input` event handles it
  });

  hiddenInput.addEventListener('input', (ev) => {
    const text = hiddenInput.value;
    if (!text) return;
    hiddenInput.value = '';
    if (!wsSend({ type: 'type', text })) {
      api('/api/studio/browser/type', { text }).catch(() => {});
    }
  });

  hiddenInput.addEventListener('blur', () => {
    focusHint.style.display = 'none';
    shotWrap.style.boxShadow = '';
  });

  // initial: auto-switch if user starts on a known-blocked URL
  if (isBlocked(input.value)) {
    setStatus('auto-snapshot for ' + hostOf(input.value));
    setMode('snapshot');
  }
}"""
