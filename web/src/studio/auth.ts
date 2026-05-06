/** Shared Studio auth helpers.
 *
 * Studio tabs can outlive dashboard restarts. When that happens the old
 * window token starts returning 401s even though /studio can provide a fresh
 * token. Keep all Studio fetch paths able to recover once.
 */

export const SESSION_HEADER = "X-Hermes-Session-Token";

let cachedToken = "";

function tokenFromWindows(): string {
  const roots = [window, globalThis, window.parent, window.top].filter(Boolean);
  for (const root of roots) {
    try {
      const token = (root as { __HERMES_SESSION_TOKEN__?: string }).__HERMES_SESSION_TOKEN__;
      if (token) return token;
    } catch {
      // Cross-origin frames can throw when inspected.
    }
  }
  return "";
}

export function currentStudioSessionToken(): string {
  return cachedToken || tokenFromWindows();
}

export async function resolveStudioSessionToken(force = false): Promise<string> {
  if (cachedToken && !force) return cachedToken;

  const direct = tokenFromWindows();
  if (direct && !force) {
    cachedToken = direct;
    return cachedToken;
  }

  try {
    const html = await fetch("/studio-app", { cache: "no-store" }).then((res) => res.text());
    const match = html.match(/__HERMES_SESSION_TOKEN__\s*=\s*["']([^"']+)["']/);
    if (match?.[1]) {
      cachedToken = match[1];
      (window as { __HERMES_SESSION_TOKEN__?: string }).__HERMES_SESSION_TOKEN__ = cachedToken;
    }
  } catch {
    // Callers will surface the original API failure if token refresh fails.
  }

  return cachedToken || direct;
}

export function authHeaders(extra: Record<string, string> = {}): HeadersInit {
  const token = currentStudioSessionToken();
  const out: Record<string, string> = { ...extra };
  if (token) out[SESSION_HEADER] = token;
  return out;
}

export async function studioFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const withToken = async (force = false) => {
    const headers = new Headers(init?.headers);
    const token = await resolveStudioSessionToken(force);
    if (token && !headers.has(SESSION_HEADER)) headers.set(SESSION_HEADER, token);
    return fetch(input, { ...init, headers });
  };

  let res = await withToken(false);
  if (res.status === 401) {
    cachedToken = "";
    res = await withToken(true);
  }
  return res;
}
