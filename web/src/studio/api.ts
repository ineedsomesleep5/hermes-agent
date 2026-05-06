/** REST helpers for the Studio canvas (auth-aware). */

import type { Position, Size, Space, StudioPreset, Widget } from "./types";
import { authHeaders, studioFetch } from "./auth";

async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  const res = await studioFetch(url, { ...init, headers });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

export const studioApi = {
  listSpaces: () => fetchJSON<Space[]>("/api/studio/spaces"),

  getSpace: (id: string) =>
    fetchJSON<Space>(`/api/studio/spaces/${encodeURIComponent(id)}`),

  createSpace: (id: string, title?: string) =>
    fetchJSON<Space>("/api/studio/spaces", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, title }),
    }),

  deleteSpace: (id: string) =>
    fetchJSON<{ ok: boolean }>(
      `/api/studio/spaces/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    ),

  listPresets: () =>
    fetchJSON<{ presets: StudioPreset[] }>("/api/studio/presets"),

  installPreset: (
    name: string,
    body: {
      id?: string;
      title?: string;
      space_id?: string;
      position?: Position;
      size?: Size;
    } = {},
  ) =>
    fetchJSON<{
      ok: boolean;
      preset: string;
      kind?: string;
      id?: string;
      space_id: string;
      position?: Position;
      size?: Size;
      widgets?: Array<{
        preset: string;
        id: string;
        position: Position;
        size: Size;
      }>;
      bundle?: string[];
    }>("/api/studio/presets/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, ...body }),
    }),

  upsertWidget: (
    spaceId: string,
    widgetId: string,
    body: {
      title: string;
      renderer: string;
      position?: Position;
      size?: Size;
    },
  ) =>
    fetchJSON<Widget>(
      `/api/studio/spaces/${encodeURIComponent(spaceId)}/widgets/${encodeURIComponent(widgetId)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),

  deleteWidget: (spaceId: string, widgetId: string) =>
    fetchJSON<{ ok: boolean }>(
      `/api/studio/spaces/${encodeURIComponent(spaceId)}/widgets/${encodeURIComponent(widgetId)}`,
      { method: "DELETE" },
    ),

  patchLayout: (
    spaceId: string,
    widgetId: string,
    body: { position?: Position; size?: Size },
  ) =>
    fetchJSON<Widget>(
      `/api/studio/spaces/${encodeURIComponent(spaceId)}/widgets/${encodeURIComponent(widgetId)}/layout`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),

  /** Build the events URL — caller pipes to the SSE helper. */
  spaceEventsUrl: (spaceId: string, opts: { replay?: boolean; timeout?: number } = {}) => {
    const qs = new URLSearchParams();
    qs.set("replay", opts.replay === false ? "0" : "1");
    if (opts.timeout) qs.set("timeout", String(opts.timeout));
    return `/api/studio/spaces/${encodeURIComponent(spaceId)}/events?${qs.toString()}`;
  },

  authHeaders,
};
