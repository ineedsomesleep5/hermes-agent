"""Iframe-first embedded browser renderer for Hermes Studio.

The failed VPS snapshot/streaming-browser experiment is intentionally not part
of this widget. Sites that allow frames render directly in Studio. Sites that
block frames, need a real Google/browser session, or are known to be hostile to
embedded browsers open in the user's own device browser instead.
"""

EMBEDDED_BROWSER_RENDERER = r"""async (parent, spaceRef) => {
  parent.style.cssText = "position:relative;height:100%;display:flex;flex-direction:column;background:#151518;border-radius:8px;overflow:hidden;font-family:-apple-system, Inter, system-ui, sans-serif;";

  const BLANK_URL = 'about:blank';
  const GOOGLE_HOME_URL = 'https://google.com/';
  const DEFAULT_URL = GOOGLE_HOME_URL;
  const SEARCH_URL = 'https://www.google.com/search?q=';
  const EXTERNAL_HOSTS = [
    'accounts.google.com',
    'bitbucket.org',
    'facebook.com',
    'github.com',
    'gitlab.com',
    'instagram.com',
    'linkedin.com',
    'notion.so',
    'reddit.com',
    'slack.com',
    'twitter.com',
    'x.com',
    'youtube.com'
  ];

  let currentUrl = DEFAULT_URL;
  let history = [];
  let historyIndex = -1;
  let suppressNextLoadHistory = false;

  function googleIframeUrl(url) {
    try {
      const parsed = new URL(url);
      const host = parsed.hostname.toLowerCase().replace(/^www\./, '');
      if (host !== 'google.com') return parsed.toString();
      if (parsed.pathname === '/' || parsed.pathname === '/webhp') return GOOGLE_HOME_URL;
      if (parsed.pathname === '/search') {
        parsed.hostname = 'www.google.com';
        return parsed.toString();
      }
      return parsed.toString();
    } catch (_) {
      return url;
    }
  }

  function normalize(rawValue) {
    const raw = String(rawValue || '').trim();
    if (!raw) return DEFAULT_URL;
    if (raw === BLANK_URL) return raw;
    if (/^https?:\/\//i.test(raw)) return googleIframeUrl(raw);
    if (/^localhost(:\d+)?(\/|$)/i.test(raw) || /^127\.0\.0\.1(:\d+)?(\/|$)/.test(raw)) return 'http://' + raw;
    if (/^[a-z0-9.-]+\.[a-z]{2,}(\/|$|\?|#)/i.test(raw)) return googleIframeUrl('https://' + raw);
    return SEARCH_URL + encodeURIComponent(raw);
  }

  function hostOf(url) {
    try { return new URL(url).hostname.toLowerCase().replace(/^www\./, ''); }
    catch (_) { return ''; }
  }

  function shouldOpenExternally(url, rawValue) {
    const host = hostOf(url);
    return EXTERNAL_HOSTS.some(item => host === item || host.endsWith('.' + item));
  }

  function setStatus(message) {
    status.textContent = message || '';
    status.style.opacity = message ? '1' : '0';
    if (message) {
      clearTimeout(setStatus._timer);
      setStatus._timer = setTimeout(() => { status.style.opacity = '0'; }, 3200);
    }
  }

  function setAddress(url) {
    currentUrl = url || DEFAULT_URL;
    address.value = currentUrl === BLANK_URL ? '' : currentUrl;
    openBtn.disabled = currentUrl === BLANK_URL;
    reloadBtn.disabled = currentUrl === BLANK_URL;
    backBtn.disabled = historyIndex <= 0;
    forwardBtn.disabled = historyIndex < 0 || historyIndex >= history.length - 1;
  }

  function pushHistory(url) {
    if (!url || url === BLANK_URL) return;
    if (history[historyIndex] === url) return;
    history = history.slice(0, historyIndex + 1);
    history.push(url);
    historyIndex = history.length - 1;
    setAddress(url);
  }

  function showExternalFallback(url, autoOpen = false) {
    if (!url || url === BLANK_URL) return;
    setAddress(url);
    iframe.removeAttribute('src');
    fallbackTitle.textContent = `${hostOf(url) || 'This site'} blocks iframe browsing`;
    fallbackText.textContent = 'Open it in your device browser. The Studio address and history stay in sync here.';
    fallbackUrl.textContent = url;
    fallback.style.display = 'flex';
    modePill.textContent = 'external';
    modePill.style.borderColor = 'rgba(255,190,80,.72)';
    modePill.style.background = 'rgba(255,190,80,.14)';
    modePill.style.color = '#ffd48a';
    let opened = null;
    if (autoOpen) opened = window.open(url, '_blank', 'noopener,noreferrer');
    setStatus(opened ? 'opened in this device browser' : 'use Open to continue');
  }

  function clearExternalFallback() {
    fallback.style.display = 'none';
    modePill.textContent = 'iframe';
    modePill.style.borderColor = 'rgba(66,133,244,.65)';
    modePill.style.background = 'rgba(66,133,244,.12)';
    modePill.style.color = '#9ec1ff';
  }

  function loadIframe(rawValue, options = {}) {
    const url = normalize(rawValue);
    if (url === BLANK_URL) {
      iframe.removeAttribute('src');
      clearExternalFallback();
      setAddress(BLANK_URL);
      setStatus('');
      return;
    }
    if (!options.forceIframe && shouldOpenExternally(url, rawValue)) {
      if (!options.skipHistory) pushHistory(url);
      showExternalFallback(url, options.autoOpen !== false);
      return;
    }
    clearExternalFallback();
    setAddress(url);
    if (!options.skipHistory) pushHistory(url);
    suppressNextLoadHistory = !!options.skipHistory;
    iframe.src = url;
    setStatus('loading iframe');
  }

  const header = document.createElement('div');
  header.style.cssText = "display:flex;padding:10px;background:linear-gradient(180deg, rgba(19,19,22,0.98), rgba(9,9,11,0.98));border-bottom:1px solid #303039;gap:8px;align-items:center;min-width:0;";
  parent.appendChild(header);

  function makeBtn(label, title) {
    const b = document.createElement('button');
    b.innerHTML = label;
    b.title = title || '';
    b.style.cssText = "height:34px;min-width:38px;padding:0 10px;border-radius:4px;border:1px solid #444;background:#2d2d2d;color:white;cursor:pointer;font-size:14px;font-weight:700;display:inline-flex;align-items:center;justify-content:center;white-space:nowrap;";
    b.onmouseenter = () => { if (!b.disabled) b.style.background = '#3a3a3a'; };
    b.onmouseleave = () => { b.style.background = b.disabled ? '#242428' : '#2d2d2d'; };
    b.onfocus = () => b.style.outline = '2px solid rgba(66,133,244,0.65)';
    b.onblur = () => b.style.outline = 'none';
    return b;
  }

  const backBtn = makeBtn('&larr;', 'Back');
  const forwardBtn = makeBtn('&rarr;', 'Forward');
  const reloadBtn = makeBtn('&#8635;', 'Reload');
  header.append(backBtn, forwardBtn, reloadBtn);

  const address = document.createElement('input');
  address.type = 'text';
  address.placeholder = 'Search Google or enter a URL';
  address.autocomplete = 'off';
  address.spellcheck = false;
  address.style.cssText = "flex:1;min-width:120px;padding:0 12px;height:34px;border-radius:5px;border:1px solid #3b3b44;background:#17171b;color:white;font-size:14px;font-weight:600;outline:none;";
  header.appendChild(address);

  const goBtn = makeBtn('Go', 'Go');
  goBtn.style.background = '#4285f4';
  goBtn.style.borderColor = '#4285f4';
  goBtn.onmouseleave = () => { goBtn.style.background = '#4285f4'; };

  const openBtn = makeBtn('&#8599;', 'Open in this device browser');
  const modePill = document.createElement('span');
  modePill.textContent = 'iframe';
  modePill.style.cssText = "height:28px;padding:0 12px;border-radius:999px;border:1px solid rgba(66,133,244,.65);background:rgba(66,133,244,.12);color:#9ec1ff;font-size:12px;font-weight:800;display:inline-flex;align-items:center;letter-spacing:.02em;";
  header.append(goBtn, openBtn, modePill);

  const frameWrap = document.createElement('div');
  frameWrap.style.cssText = "position:relative;flex:1;min-height:0;background:white;overflow:hidden;";
  parent.appendChild(frameWrap);

  const iframe = document.createElement('iframe');
  iframe.title = 'Embedded browser iframe';
  iframe.style.cssText = "position:absolute;inset:0;width:100%;height:100%;border:none;background:white;";
  iframe.referrerPolicy = "strict-origin-when-cross-origin";
  frameWrap.appendChild(iframe);

  const fallback = document.createElement('div');
  fallback.style.cssText = "position:absolute;inset:0;display:none;flex-direction:column;align-items:center;justify-content:center;gap:12px;padding:28px;box-sizing:border-box;background:linear-gradient(145deg,#f8fafc,#e8edf5);color:#1f2937;text-align:center;";
  frameWrap.appendChild(fallback);

  const fallbackTitle = document.createElement('div');
  fallbackTitle.style.cssText = "font-size:18px;font-weight:800;line-height:1.2;";
  const fallbackText = document.createElement('div');
  fallbackText.style.cssText = "max-width:520px;font-size:14px;line-height:1.45;color:#4b5563;";
  const fallbackUrl = document.createElement('div');
  fallbackUrl.style.cssText = "max-width:min(620px,90%);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:#64748b;background:rgba(255,255,255,.72);border:1px solid rgba(148,163,184,.34);border-radius:999px;padding:7px 11px;";
  const fallbackOpen = document.createElement('button');
  fallbackOpen.type = 'button';
  fallbackOpen.textContent = 'Open in this device browser';
  fallbackOpen.style.cssText = "height:40px;padding:0 16px;border:0;border-radius:10px;background:#2563eb;color:white;font-size:14px;font-weight:800;cursor:pointer;box-shadow:0 10px 24px rgba(37,99,235,.24);";
  fallbackOpen.onclick = () => {
    const opened = window.open(currentUrl, '_blank', 'noopener,noreferrer');
    setStatus(opened ? 'opened in this device browser' : 'popup blocked');
  };
  fallback.append(fallbackTitle, fallbackText, fallbackUrl, fallbackOpen);

  const status = document.createElement('div');
  status.style.cssText = "position:absolute;left:10px;bottom:10px;max-width:70%;padding:7px 10px;border-radius:999px;background:rgba(0,0,0,.48);color:white;font-size:12px;font-weight:700;pointer-events:none;opacity:0;transition:opacity .18s ease;";
  frameWrap.appendChild(status);

  iframe.addEventListener('load', () => {
    setStatus('');
    try {
      const href = iframe.contentWindow.location.href;
      if (href && href !== BLANK_URL) {
        setAddress(href);
        if (suppressNextLoadHistory) suppressNextLoadHistory = false;
        else pushHistory(href);
      }
    } catch (_) {
      suppressNextLoadHistory = false;
      if (iframe.src) setAddress(iframe.src);
    }
  });

  address.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      loadIframe(address.value);
    }
  });

  goBtn.onclick = () => loadIframe(address.value);
  openBtn.onclick = () => showExternalFallback(normalize(address.value || currentUrl), true);
  reloadBtn.onclick = () => {
    if (!currentUrl || currentUrl === BLANK_URL) return;
    if (fallback.style.display !== 'none') {
      showExternalFallback(currentUrl, false);
      return;
    }
    try { iframe.contentWindow.location.reload(); }
    catch (_) { iframe.src = currentUrl; }
  };
  backBtn.onclick = () => {
    if (historyIndex <= 0) return;
    historyIndex -= 1;
    loadIframe(history[historyIndex], { skipHistory: true, autoOpen: false });
  };
  forwardBtn.onclick = () => {
    if (historyIndex >= history.length - 1) return;
    historyIndex += 1;
    loadIframe(history[historyIndex], { skipHistory: true, autoOpen: false });
  };

  loadIframe(DEFAULT_URL, { forceIframe: true });
}"""
