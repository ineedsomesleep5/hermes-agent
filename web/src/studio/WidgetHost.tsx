/** Mounts an agent-authored widget renderer into a DOM node.
 *
 * The renderer is a JavaScript expression that evaluates to an async
 * function, e.g.:
 *
 *   async (parent, space, ctx) => {
 *     parent.textContent = 'hello';
 *     return { cleanup: () => { ... } };
 *   }
 *
 * It runs unsandboxed in the same JS realm as the page — the security
 * model mirrors the rest of Hermes Studio (only authenticated users
 * can author widgets via the API). Errors are caught and rendered
 * inside the widget body, so a broken renderer can't take the page down.
 */

import { useEffect, useRef } from "react";
import { studioFetch } from "./auth";
import type { Widget, Space } from "./types";

interface RendererCleanup {
  cleanup?: () => void | Promise<void>;
}

export function WidgetHost({
  widget,
  space,
}: {
  widget: Widget;
  space: { id: string; title?: string };
}) {
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const errorRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const parent = bodyRef.current;
    const errorEl = errorRef.current;
    if (!parent) return;

    let disposed = false;
    let result: RendererCleanup | void;

    parent.innerHTML = "";
    if (errorEl) errorEl.textContent = "";

    const run = async () => {
      try {
        // eslint-disable-next-line @typescript-eslint/no-implied-eval, no-new-func
        const factory = new Function(`return (${widget.renderer});`);
        const fn = factory();
        if (typeof fn !== "function") {
          throw new Error("renderer must evaluate to a function");
        }
        const storagePrefix = `__hermes_studio_app_file__:${space.id}:`;
        const normalizePath = (path: string) => String(path || "").replace(/^~\/?/, "");
        const fileKey = (path: string) => storagePrefix + normalizePath(path);
        const appFiles = {
          async write(path: string, content = "") {
            const normalized = normalizePath(path);
            if (!normalized) return { ok: true, path: normalized };
            localStorage.setItem(fileKey(normalized), String(content ?? ""));
            return { ok: true, path: normalized };
          },
          async read(path: string) {
            const normalized = normalizePath(path);
            const content = localStorage.getItem(fileKey(normalized));
            if (content === null) throw new Error(`404 file not found: ${normalized}`);
            return { path: normalized, content };
          },
          async delete(path: string) {
            const normalized = normalizePath(path);
            localStorage.removeItem(fileKey(normalized));
            return { ok: true, path: normalized };
          },
          async list(path = "") {
            const normalizedDir = normalizePath(path).replace(/\/?$/, "/");
            const paths: string[] = [];
            for (let i = 0; i < localStorage.length; i += 1) {
              const key = localStorage.key(i);
              if (!key?.startsWith(storagePrefix)) continue;
              const storedPath = key.slice(storagePrefix.length);
              if (!normalizedDir || storedPath.startsWith(normalizedDir)) paths.push(storedPath);
            }
            return { paths: paths.sort() };
          },
        };
        const fetchExternal = async (url: string, options: RequestInit = {}) => {
          const res = await studioFetch("/api/studio/fetch-external", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
            },
            body: JSON.stringify({
              url,
              method: options.method || "GET",
              headers: options.headers || {},
              body: typeof options.body === "string" ? options.body : undefined,
            }),
          });
          if (!res.ok) {
            const detail = await res.text().catch(() => res.statusText);
            throw new Error(`fetchExternal failed (${res.status}): ${detail}`);
          }
          const payload = (await res.json()) as {
            status?: number;
            body?: string;
            headers?: Record<string, string>;
          };
          return new Response(payload.body || "", {
            status: payload.status || 200,
            headers: payload.headers || {},
          });
        };
        const removeWidget = async ({
          spaceId,
          widgetId,
        }: {
          spaceId?: string;
          widgetId?: string;
        }) => {
          const targetSpace = encodeURIComponent(spaceId || space.id);
          const targetWidget = encodeURIComponent(widgetId || widget.id);
          const res = await studioFetch(`/api/studio/spaces/${targetSpace}/widgets/${targetWidget}`, {
            method: "DELETE",
          });
          if (!res.ok) throw new Error(`removeWidget failed (${res.status})`);
          return res.json();
        };
        const runtimeSpace = {
          ...space,
          fetchExternal,
          api: appFiles,
        };
        const ctx = {
          widget,
          space: runtimeSpace,
          appFiles,
          spaces: { removeWidget },
          studio: (window as unknown as { __HERMES_STUDIO__?: unknown }).__HERMES_STUDIO__,
        };
        const ret = await fn(parent, runtimeSpace, ctx);
        if (disposed) {
          // Component unmounted while renderer was awaiting — try cleanup.
          if (ret && typeof (ret as RendererCleanup).cleanup === "function") {
            try {
              await (ret as RendererCleanup).cleanup!();
            } catch {
              /* ignore */
            }
          }
          return;
        }
        result = ret as RendererCleanup;
      } catch (err) {
        if (disposed) return;
        if (errorEl) {
          errorEl.textContent = `Widget error:\n${
            err instanceof Error ? `${err.name}: ${err.message}` : String(err)
          }`;
        }
      }
    };

    void run();

    return () => {
      disposed = true;
      if (result && typeof result.cleanup === "function") {
        try {
          void result.cleanup();
        } catch {
          /* ignore */
        }
      }
    };
    // Re-run ONLY when the renderer source or widget id actually change.
    // Layout updates (position/size) bump widget.updated_at on the
    // backend — we deliberately ignore that here so dragging/resizing
    // doesn't tear down the DOM and reset internal timers/polling.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [widget.id, widget.renderer]);

  return (
    <div className="studio-widget-body">
      <div ref={bodyRef} style={{ position: "absolute", inset: 0 }} />
      <div ref={errorRef} className="studio-widget-error" />
    </div>
  );
}

export type { Widget, Space };
