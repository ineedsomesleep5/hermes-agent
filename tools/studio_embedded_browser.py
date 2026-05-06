"""Embedded browser renderer for Hermes Studio.

This is the Space Agent-style browser surface: a screenshot-driven browser
with explicit back/forward/reload controls and a live URL bar.
"""

EMBEDDED_BROWSER_RENDERER = r"""async (parent, spaceRef) => {
  parent.style.cssText = "position:relative;height:100%;display:flex;flex-direction:column;background:#1e1e1e;border-radius:8px;overflow:hidden;font-family:-apple-system, Inter, system-ui, sans-serif;";
  
  const header = document.createElement('div');
  header.style.cssText = "display:flex;padding:12px;background:linear-gradient(180deg, rgba(20,20,24,0.98), rgba(10,10,12,0.98));border-bottom:1px solid #3d3d3d;gap:8px;align-items:center;";
  
  const backBtn = document.createElement('button');
  backBtn.innerHTML = '&#8592;';
  backBtn.style.cssText = "padding:6px 10px;border-radius:4px;border:1px solid #444;background:#2d2d2d;color:white;cursor:pointer;font-size:14px;";
  backBtn.onmouseenter = () => backBtn.style.background = '#3d3d3d';
  backBtn.onmouseleave = () => backBtn.style.background = '#2d2d2d';
  
  const fwdBtn = document.createElement('button');
  fwdBtn.innerHTML = '&#8594;';
  fwdBtn.style.cssText = "padding:6px 10px;border-radius:4px;border:1px solid #444;background:#2d2d2d;color:white;cursor:pointer;font-size:14px;";
  fwdBtn.onmouseenter = () => fwdBtn.style.background = '#3d3d3d';
  fwdBtn.onmouseleave = () => fwdBtn.style.background = '#2d2d2d';
  
  const reloadBtn = document.createElement('button');
  reloadBtn.innerHTML = '&#8635;';
  reloadBtn.style.cssText = "padding:6px 10px;border-radius:4px;border:1px solid #444;background:#2d2d2d;color:white;cursor:pointer;font-size:14px;";
  reloadBtn.onmouseenter = () => reloadBtn.style.background = '#3d3d3d';
  reloadBtn.onmouseleave = () => reloadBtn.style.background = '#2d2d2d';
  
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
  
  const iframe = document.createElement('iframe');
  iframe.style.cssText = "flex:1;border:none;width:100%;background:white;";
  
  function getUrl(url) {
      if (!url) return 'about:blank';
      if (!url.startsWith('http://') && !url.startsWith('https://')) {
          url = 'https://' + url;
      }
      return url;
  }
  
  iframe.src = getUrl(input.value);
  iframe.sandbox = "allow-same-origin allow-scripts allow-popups allow-forms allow-modals";
  
  let history = [input.value];
  let historyIndex = 0;
  
  function navigate(rawUrl) {
      if (!rawUrl) return;
      let url = getUrl(rawUrl);
      input.value = url;
      iframe.src = url;
      
      if (history[historyIndex] !== url) {
        history = history.slice(0, historyIndex + 1);
        history.push(url);
        historyIndex++;
      }
  }
  
  goBtn.addEventListener('click', () => navigate(input.value.trim()));
  input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') navigate(input.value.trim());
  });
  
  backBtn.addEventListener('click', () => {
    try {
        iframe.contentWindow.history.back();
    } catch (e) {
        if (historyIndex > 0) {
            historyIndex--;
            input.value = history[historyIndex];
            iframe.src = history[historyIndex];
        }
    }
  });
  
  fwdBtn.addEventListener('click', () => {
    try {
        iframe.contentWindow.history.forward();
    } catch (e) {
        if (historyIndex < history.length - 1) {
            historyIndex++;
            input.value = history[historyIndex];
            iframe.src = history[historyIndex];
        }
    }
  });
  
  reloadBtn.addEventListener('click', () => {
    try {
        iframe.contentWindow.location.reload();
    } catch (e) {
        const currentUrl = iframe.src;
        iframe.src = 'about:blank';
        setTimeout(() => { iframe.src = currentUrl; }, 10);
    }
  });
  
  header.appendChild(backBtn);
  header.appendChild(fwdBtn);
  header.appendChild(reloadBtn);
  header.appendChild(input);
  header.appendChild(goBtn);
  parent.appendChild(header);
  parent.appendChild(iframe);
}"""
