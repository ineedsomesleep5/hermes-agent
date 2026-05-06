/** SSE client built on fetch + ReadableStream so we can send auth headers.
 *
 * The browser's native EventSource API does not support custom request
 * headers, but our auth middleware requires the X-Hermes-Session-Token
 * header on every /api/* request. We parse the SSE wire format manually
 * (data-only — we do not need event:/id:/retry: support).
 *
 * Auto-reconnects with exponential backoff if the stream ends or fails:
 * the dashboard process can restart, Cloudflare can drop idle
 * connections, and the user's network can flap. Without auto-reconnect
 * the browser silently shows stale state and the user has to refresh.
 */

import { resolveStudioSessionToken, SESSION_HEADER } from "./auth";

type Listener<T> = (event: T) => void;

export interface SSEHandle {
  close: () => void;
}

const RECONNECT_BASE_MS = 1000; // first retry after 1s
const RECONNECT_MAX_MS = 15_000; // cap backoff at 15s

export function subscribeSSE<T = unknown>(
  url: string,
  onEvent: Listener<T>,
  opts: {
    signal?: AbortSignal;
    headers?: Record<string, string>;
    onStatus?: (status: "connecting" | "open" | "closed") => void;
  } = {},
): SSEHandle {
  const ctrl = new AbortController();
  const userSignal = opts.signal;
  if (userSignal) {
    if (userSignal.aborted) ctrl.abort();
    else userSignal.addEventListener("abort", () => ctrl.abort(), { once: true });
  }

  let attempt = 0;

  const status = (s: "connecting" | "open" | "closed") => {
    try {
      opts.onStatus?.(s);
    } catch {
      /* never let user callbacks break the loop */
    }
  };

  const headersWithToken = async (forceToken = false) => {
    const headers = new Headers(opts.headers ?? {});
    headers.set("Accept", "text/event-stream");
    const token = await resolveStudioSessionToken(forceToken);
    if (token && !headers.has(SESSION_HEADER)) headers.set(SESSION_HEADER, token);
    return headers;
  };

  const connectOnce = async (): Promise<"ended" | "errored"> => {
    let buffer = "";
    try {
      status("connecting");
      let res = await fetch(url, { headers: await headersWithToken(), signal: ctrl.signal });
      if (res.status === 401) {
        res = await fetch(url, { headers: await headersWithToken(true), signal: ctrl.signal });
      }
      if (!res.ok || !res.body) {
        if (!ctrl.signal.aborted) {
          console.warn(`[studio sse] ${url} -> ${res.status}`);
        }
        return "errored";
      }
      status("open");
      attempt = 0; // successful connection — reset backoff

      const reader = res.body.getReader();
      const decoder = new TextDecoder();

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // SSE messages are separated by a blank line.
        let sep: number;
        while ((sep = buffer.indexOf("\n\n")) !== -1) {
          const raw = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);

          // Concatenate all `data:` lines in this message.
          let data = "";
          for (const line of raw.split("\n")) {
            if (line.startsWith("data: ")) {
              data += (data ? "\n" : "") + line.slice(6);
            } else if (line.startsWith("data:")) {
              data += (data ? "\n" : "") + line.slice(5);
            }
          }
          if (!data) continue;
          try {
            onEvent(JSON.parse(data) as T);
          } catch (err) {
            console.warn("[studio sse] bad JSON", err, data);
          }
        }
      }
      return "ended";
    } catch (err) {
      if (!ctrl.signal.aborted) {
        console.warn("[studio sse] error", err);
      }
      return "errored";
    }
  };

  void (async () => {
    while (!ctrl.signal.aborted) {
      const reason = await connectOnce();
      if (ctrl.signal.aborted) break;

      // Backoff: first retry near-instant (250ms) since "ended" cleanly
      // usually means the server intentionally closed (e.g. dashboard
      // restart) and is already coming back. "errored" gets the full
      // exponential climb.
      const delay =
        reason === "ended" && attempt === 0
          ? 250
          : Math.min(
              RECONNECT_BASE_MS * Math.pow(2, attempt),
              RECONNECT_MAX_MS,
            );
      attempt++;
      status("closed");
      await new Promise<void>((resolve) => {
        const t = window.setTimeout(resolve, delay);
        ctrl.signal.addEventListener(
          "abort",
          () => {
            window.clearTimeout(t);
            resolve();
          },
          { once: true },
        );
      });
    }
    status("closed");
  })();

  return { close: () => ctrl.abort() };
}
