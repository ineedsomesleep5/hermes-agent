/** The studio canvas — subscribes to space events and renders widgets.
 *
 * Wraps the widget layer in a transformable container so the user can
 * pan and zoom the entire grid:
 *   - Wheel: zoom in/out, anchored on cursor (Cmd/Ctrl+scroll on trackpads
 *     also works as the browser's native pinch maps to wheel+ctrlKey).
 *   - Drag empty canvas: pan.
 *   - Two-finger pinch on touch: zoom (anchored on midpoint).
 *   - One-finger drag on empty canvas: pan.
 *
 * Newly-upserted widgets briefly glow red ("flash") for ~2s so the user
 * sees what just appeared even when it's stacked behind other widgets.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type TouchEvent as ReactTouchEvent,
  type WheelEvent as ReactWheelEvent,
} from "react";
import { studioApi } from "./api";
import { subscribeSSE } from "./sse";
import { WidgetCard } from "./WidgetCard";
import type { Position, Size, Space, StudioAnnotation, StudioEvent, Widget } from "./types";

const FLASH_MS = 2400;
const MIN_SCALE = 0.2;
const MAX_SCALE = 3;
const ZOOM_STEP = 1.18;
// Mirrors the GRID constant in WidgetCard.tsx — kept local to avoid a
// cross-file import for one number.
const CANVAS_GRID = 80;

declare global {
  interface Window {
    __HERMES_STUDIO_INITIAL_SPACE__?: Space;
  }
}

interface Viewport {
  x: number;
  y: number;
  scale: number;
}

const IDENTITY_VIEWPORT: Viewport = { x: 0, y: 0, scale: 1 };

function clampScale(s: number): number {
  return Math.max(MIN_SCALE, Math.min(MAX_SCALE, s));
}

export function StudioCanvas({
  spaceId,
  annotations = [],
  annotationMode = false,
  selectedWidgetId = null,
  onSelectWidget,
  onWidgetsChange,
}: {
  spaceId: string;
  annotations?: StudioAnnotation[];
  annotationMode?: boolean;
  selectedWidgetId?: string | null;
  onSelectWidget?: (widgetId: string) => void;
  onWidgetsChange?: (widgets: Widget[]) => void;
}) {
  const initialWidgetsRef = useRef<Widget[] | null>(null);
  if (initialWidgetsRef.current === null) {
    const initialSpace = window.__HERMES_STUDIO_INITIAL_SPACE__;
    initialWidgetsRef.current =
      initialSpace && (!initialSpace.id || initialSpace.id === spaceId)
        ? (initialSpace.widgets ?? [])
        : [];
  }
  const [space, setSpace] = useState<Space | null>(window.__HERMES_STUDIO_INITIAL_SPACE__ ?? null);
  const [widgets, setWidgets] = useState<Map<string, Widget>>(
    () => new Map(initialWidgetsRef.current?.map((widget) => [widget.id, widget]) ?? []),
  );
  const hasInitialWidgets = (initialWidgetsRef.current?.length ?? 0) > 0;
  const [error, setError] = useState<string | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<
    "connecting" | "open" | "closed"
  >(hasInitialWidgets ? "open" : "connecting");
  const [viewport, setViewport] = useState<Viewport>(IDENTITY_VIEWPORT);
  const [flashing, setFlashing] = useState<Set<string>>(() => new Set());
  // Briefly true while an auto-pan is in flight, so the transform glides
  // smoothly. We never animate during user drag/zoom — only auto-centring.
  const [easing, setEasing] = useState(false);
  const easingTimerRef = useRef<number | null>(null);

  const canvasRef = useRef<HTMLDivElement | null>(null);
  // Live ref to the current scale so widget drag handlers can read the
  // up-to-date value without re-binding on every viewport change.
  const scaleRef = useRef(1);
  // Layout PATCH coalescing — see note below.
  const lastLayoutPatchRef = useRef<Map<string, string>>(new Map());
  // Have we seen this widget id before? Anything new gets a flash.
  const seenIdsRef = useRef<Set<string>>(
    new Set(initialWidgetsRef.current?.map((widget) => widget.id) ?? []),
  );
  const flashTimersRef = useRef<Map<string, number>>(new Map());
  const initialSnapshotLoadedRef = useRef(hasInitialWidgets);

  // Smoothly re-center the viewport on a widget. Used when a new widget
  // arrives via the chat so the user immediately sees what was created
  // even if it landed off-screen. Skips work if the widget is already
  // mostly within the visible area (no surprise jumps).
  const centerViewportOnWidget = useCallback((widget: Widget) => {
    const el = canvasRef.current;
    if (!el) return;
    const vp = el.getBoundingClientRect();
    if (vp.width < 40 || vp.height < 40) return;

    setViewport((cur) => {
      const cx = (widget.position.x + widget.size.w / 2) * CANVAS_GRID;
      const cy = (widget.position.y + widget.size.h / 2) * CANVAS_GRID;
      // Where does the widget center currently land on screen?
      const screenX = cur.x + cx * cur.scale;
      const screenY = cur.y + cy * cur.scale;
      const widgetWPx = widget.size.w * CANVAS_GRID * cur.scale;
      const widgetHPx = widget.size.h * CANVAS_GRID * cur.scale;
      const margin = 40;
      const fullyVisible =
        screenX - widgetWPx / 2 > margin &&
        screenY - widgetHPx / 2 > margin &&
        screenX + widgetWPx / 2 < vp.width - margin &&
        screenY + widgetHPx / 2 < vp.height - margin;
      if (fullyVisible) return cur;
      // Turn on the easing transition for the next ~450ms so the pan
      // glides instead of teleporting.
      setEasing(true);
      if (easingTimerRef.current)
        window.clearTimeout(easingTimerRef.current);
      easingTimerRef.current = window.setTimeout(() => {
        setEasing(false);
        easingTimerRef.current = null;
      }, 480);
      return {
        x: vp.width / 2 - cx * cur.scale,
        y: vp.height / 2 - cy * cur.scale,
        scale: cur.scale,
      };
    });
  }, []);

  const triggerFlash = useCallback((id: string) => {
    setFlashing((prev) => {
      const next = new Set(prev);
      next.add(id);
      return next;
    });
    const existing = flashTimersRef.current.get(id);
    if (existing) window.clearTimeout(existing);
    const handle = window.setTimeout(() => {
      setFlashing((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
      flashTimersRef.current.delete(id);
    }, FLASH_MS);
    flashTimersRef.current.set(id, handle);
  }, []);

  const handleEvent = useCallback(
    (evt: StudioEvent) => {
      setWidgets((prev) => {
        const next = new Map(prev);
        switch (evt.type) {
          case "widget.upserted": {
            const isNew = !seenIdsRef.current.has(evt.widget.id);
            const existing = next.get(evt.widget.id);
            const contentChanged =
              !!existing &&
              (existing.renderer !== evt.widget.renderer ||
                existing.title !== evt.widget.title);
            seenIdsRef.current.add(evt.widget.id);

            // Echo of our own layout PATCH? Drop the marker so future
            // server-side updates aren't ignored.
            const patchKey = lastLayoutPatchRef.current.get(evt.widget.id);
            const incomingKey = JSON.stringify({
              position: evt.widget.position,
              size: evt.widget.size,
              renderer: evt.widget.renderer,
              title: evt.widget.title,
            });
            if (patchKey === incomingKey) {
              lastLayoutPatchRef.current.delete(evt.widget.id);
            }
            next.set(evt.widget.id, evt.widget);

            // Flash newly-arriving widgets so the user sees what just
            // appeared even if it's stacked behind something else.
            // Don't flash the initial replay (handled by `isNew` since
            // the seenIds set was just populated for everything we
            // pre-loaded — see initial fetch below).
            if (isNew) {
              triggerFlash(evt.widget.id);
              // Defer to next frame so the widget is mounted (with its
              // current size) before we measure and pan to it.
              window.requestAnimationFrame(() =>
                centerViewportOnWidget(evt.widget),
              );
            } else if (contentChanged) {
              triggerFlash(evt.widget.id);
            }
            return next;
          }
          case "space.updated":
            if ((evt as any).space) {
              setSpace((evt as any).space);
            }
            return next;
          case "widget.deleted":
            next.delete(evt.id);
            seenIdsRef.current.delete(evt.id);
            return next;
          case "widget.position_changed": {
            const cur = next.get(evt.id);
            if (cur) {
              next.set(evt.id, {
                ...cur,
                position: evt.position,
                size: evt.size,
              });
            }
            return next;
          }
          default:
            return next;
        }
      });
    },
    [triggerFlash],
  );

  // Initial fetch + SSE subscription.
  useEffect(() => {
    let alive = true;

    // Pull the latest canvas state from the API and merge into local state.
    // Used both for initial load and as a recovery path on tab focus / SSE
    // disconnect — guarantees the user always sees ground truth even if a
    // few SSE events were missed.
    const refreshFromApi = async () => {
      try {
        const space = await studioApi.getSpace(spaceId);
        if (!alive) return;
        const m = new Map<string, Widget>();
        const newlyDiscovered: Widget[] = [];
        const hadInitialSnapshot = initialSnapshotLoadedRef.current;
        for (const w of space.widgets ?? []) {
          m.set(w.id, w);
          if (hadInitialSnapshot && !seenIdsRef.current.has(w.id)) {
            newlyDiscovered.push(w);
          }
          seenIdsRef.current.add(w.id);
        }
        initialSnapshotLoadedRef.current = true;
        setWidgets(m);
        setSpace(space);
        setError(null);
        setConnectionStatus((status) => (status === "connecting" ? "open" : status));
        if (newlyDiscovered.length > 0) {
          const newest = newlyDiscovered.reduce((latest, widget) =>
            String(widget.updated_at || "") > String(latest.updated_at || "") ? widget : latest,
          );
          triggerFlash(newest.id);
          window.requestAnimationFrame(() => centerViewportOnWidget(newest));
        }
      } catch (err) {
        if (alive) setError((err as Error).message);
      }
    };

    void refreshFromApi();

    let lastStatus: "connecting" | "open" | "closed" = "connecting";
    const sub = subscribeSSE<StudioEvent>(
      studioApi.spaceEventsUrl(spaceId, { replay: true, timeout: 86400 }),
      handleEvent,
      {
        onStatus: (s) => {
          setConnectionStatus(s);
          // On every successful reconnect (closed -> open), refresh the
          // canvas snapshot to catch any events that were emitted while
          // the stream was down. Belt-and-suspenders: replay=true on the
          // SSE URL also resends all widgets, but a fresh GET is the
          // ground-truth source.
          if (s === "open" && lastStatus === "closed") {
            void refreshFromApi();
          }
          lastStatus = s;
        },
      },
    );

    // Safety net: if the user comes back to the tab and the SSE stream
    // missed events while they were away, pull a fresh snapshot. Cheap
    // (one HTTP GET) and bounds the worst-case staleness to one focus.
    const onVisible = () => {
      if (document.visibilityState === "visible") void refreshFromApi();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);

    return () => {
      alive = false;
      sub.close();
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
      // Clear any pending flash timers.
      for (const handle of flashTimersRef.current.values()) {
        window.clearTimeout(handle);
      }
      flashTimersRef.current.clear();
    };
  }, [spaceId, handleEvent]);

  const onLayoutCommit = useCallback(
    (widgetId: string, position: Position, size: Size) => {
      const cur = widgets.get(widgetId);
      if (cur) {
        lastLayoutPatchRef.current.set(
          widgetId,
          JSON.stringify({
            position,
            size,
            renderer: cur.renderer,
            title: cur.title,
          }),
        );
      }
      void studioApi
        .patchLayout(spaceId, widgetId, { position, size })
        .catch((err: Error) => {
          console.warn("[studio] layout patch failed:", err.message);
        });
    },
    [spaceId, widgets],
  );

  const onDelete = useCallback(
    (widgetId: string) => {
      void studioApi.deleteWidget(spaceId, widgetId).catch((err: Error) => {
        console.warn("[studio] delete failed:", err.message);
      });
    },
    [spaceId],
  );

  // ---------------------------------------------------------------------
  // Pan / zoom — wheel + drag (mouse) + touch (1-finger pan, 2-finger pinch)
  // ---------------------------------------------------------------------

  const isEmptyCanvasTarget = useCallback(
    (target: EventTarget | null): boolean => {
      if (!(target instanceof Element)) return false;
      // Pan only when the user grabs empty canvas — not a widget body.
      return !target.closest(".studio-widget");
    },
    [],
  );

  const onWheel = useCallback((e: ReactWheelEvent<HTMLDivElement>) => {
    // Zoom around the cursor. ctrlKey is set by browsers for pinch on
    // trackpads (and Cmd-scroll on Mac), but we zoom on plain wheel too
    // because users may not have a trackpad.
    if (!isEmptyCanvasTarget(e.target) && !e.ctrlKey) return;
    e.preventDefault();
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return;
    const cx = e.clientX - rect.left;
    const cy = e.clientY - rect.top;
    setViewport((v) => {
      const factor = e.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP;
      const nextScale = clampScale(v.scale * factor);
      const k = nextScale / v.scale;
      // Keep the world point under the cursor stationary.
      const nx = cx - (cx - v.x) * k;
      const ny = cy - (cy - v.y) * k;
      return { x: nx, y: ny, scale: nextScale };
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onPointerDown = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      // Only pan when grabbing empty canvas with primary button.
      if (e.button !== 0) return;
      if (!isEmptyCanvasTarget(e.target)) return;
      // Don't start a pan from a touch — touch is handled separately to
      // distinguish 1-finger pan vs 2-finger pinch.
      if (e.pointerType === "touch") return;

      e.preventDefault();
      const start = { x: e.clientX, y: e.clientY };
      const startVp = viewport;
      const onMove = (mv: PointerEvent) => {
        const dx = mv.clientX - start.x;
        const dy = mv.clientY - start.y;
        setViewport({ x: startVp.x + dx, y: startVp.y + dy, scale: startVp.scale });
      };
      const onUp = () => {
        document.removeEventListener("pointermove", onMove);
        document.removeEventListener("pointerup", onUp);
        document.removeEventListener("pointercancel", onUp);
      };
      document.addEventListener("pointermove", onMove);
      document.addEventListener("pointerup", onUp);
      document.addEventListener("pointercancel", onUp);
    },
    [isEmptyCanvasTarget, viewport],
  );

  // Touch: 1-finger pan on empty canvas, 2-finger pinch zoom.
  const onTouchStart = useCallback(
    (e: ReactTouchEvent<HTMLDivElement>) => {
      const rect = canvasRef.current?.getBoundingClientRect();
      if (!rect) return;

      if (e.touches.length === 1) {
        if (!isEmptyCanvasTarget(e.target)) return;
        const t = e.touches[0];
        const start = { x: t.clientX, y: t.clientY };
        const startVp = viewport;
        const onMove = (tev: TouchEvent) => {
          if (tev.touches.length !== 1) return;
          tev.preventDefault();
          const tt = tev.touches[0];
          const dx = tt.clientX - start.x;
          const dy = tt.clientY - start.y;
          setViewport({
            x: startVp.x + dx,
            y: startVp.y + dy,
            scale: startVp.scale,
          });
        };
        const onEnd = () => {
          document.removeEventListener("touchmove", onMove);
          document.removeEventListener("touchend", onEnd);
          document.removeEventListener("touchcancel", onEnd);
        };
        document.addEventListener("touchmove", onMove, { passive: false });
        document.addEventListener("touchend", onEnd);
        document.addEventListener("touchcancel", onEnd);
        return;
      }

      if (e.touches.length === 2) {
        e.preventDefault();
        const a = e.touches[0];
        const b = e.touches[1];
        const startDist = Math.hypot(b.clientX - a.clientX, b.clientY - a.clientY) || 1;
        const startMidX = (a.clientX + b.clientX) / 2 - rect.left;
        const startMidY = (a.clientY + b.clientY) / 2 - rect.top;
        const startVp = viewport;
        const onMove = (tev: TouchEvent) => {
          if (tev.touches.length !== 2) return;
          tev.preventDefault();
          const aa = tev.touches[0];
          const bb = tev.touches[1];
          const dist =
            Math.hypot(bb.clientX - aa.clientX, bb.clientY - aa.clientY) || 1;
          const factor = dist / startDist;
          const nextScale = clampScale(startVp.scale * factor);
          const k = nextScale / startVp.scale;
          const midX = (aa.clientX + bb.clientX) / 2 - rect.left;
          const midY = (aa.clientY + bb.clientY) / 2 - rect.top;
          // Anchor pinch around the midpoint AND track midpoint translation
          // so users can pan-while-pinching naturally.
          const nx =
            midX -
            (startMidX - startVp.x) * k +
            (midX - startMidX) * 0; // already accounted for via midX
          const ny = midY - (startMidY - startVp.y) * k + (midY - startMidY) * 0;
          setViewport({ x: nx, y: ny, scale: nextScale });
        };
        const onEnd = () => {
          document.removeEventListener("touchmove", onMove);
          document.removeEventListener("touchend", onEnd);
          document.removeEventListener("touchcancel", onEnd);
        };
        document.addEventListener("touchmove", onMove, { passive: false });
        document.addEventListener("touchend", onEnd);
        document.addEventListener("touchcancel", onEnd);
      }
    },
    [isEmptyCanvasTarget, viewport],
  );

  // ---------------------------------------------------------------------
  // Zoom toolbar
  // ---------------------------------------------------------------------

  const zoomBy = useCallback((factor: number) => {
    setViewport((v) => {
      const rect = canvasRef.current?.getBoundingClientRect();
      const cx = rect ? rect.width / 2 : 0;
      const cy = rect ? rect.height / 2 : 0;
      const nextScale = clampScale(v.scale * factor);
      const k = nextScale / v.scale;
      return {
        x: cx - (cx - v.x) * k,
        y: cy - (cy - v.y) * k,
        scale: nextScale,
      };
    });
  }, []);

  const fitAll = useCallback(() => {
    setViewport(IDENTITY_VIEWPORT);
  }, []);

  const widgetList = useMemo(() => Array.from(widgets.values()), [widgets]);
  const annotationCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const annotation of annotations) {
      if (annotation.resolvedAt) continue;
      counts.set(annotation.widgetId, (counts.get(annotation.widgetId) ?? 0) + 1);
    }
    return counts;
  }, [annotations]);

  // Keep scaleRef in sync with the current viewport scale.
  useEffect(() => {
    scaleRef.current = viewport.scale;
  }, [viewport.scale]);

  useEffect(() => {
    onWidgetsChange?.(widgetList);
  }, [onWidgetsChange, widgetList]);

  return (
    <div
      ref={canvasRef}
      className="studio-canvas"
      onWheel={onWheel}
      onPointerDown={onPointerDown}
      onTouchStart={onTouchStart}
    >
      {space?.background_url && (
        <div style={{ position: "absolute", inset: 0, zIndex: 0, overflow: "hidden", pointerEvents: "none" }}>
          {space.background_type === "video" || space.background_url.match(/\.(mp4|webm|ogg)$/i) ? (
            <video
              src={space.background_url}
              autoPlay
              loop
              muted
              playsInline
              style={{ width: "100%", height: "100%", objectFit: "cover", opacity: 0.8 }}
            />
          ) : space.background_type === "color" || space.background_url.startsWith("#") || space.background_url.startsWith("rgb") ? (
            <div style={{ width: "100%", height: "100%", background: space.background_url, opacity: 0.8 }} />
          ) : (
            <img
              src={space.background_url}
              alt="Space background"
              style={{ width: "100%", height: "100%", objectFit: "cover", opacity: 0.8 }}
            />
          )}
        </div>
      )}
      <div
        className="studio-canvas-content"

        style={{
          transform: `translate3d(${viewport.x}px, ${viewport.y}px, 0) scale(${viewport.scale})`,
          transformOrigin: "0 0",
          transition: easing
            ? "transform 420ms cubic-bezier(0.22, 1, 0.36, 1)"
            : undefined,
        }}
      >
        {widgetList.map((w) => (
          <WidgetCard
            key={w.id}
            widget={w}
            spaceId={spaceId}
            flash={flashing.has(w.id)}
            scaleRef={scaleRef}
            annotationMode={annotationMode}
            annotationCount={annotationCounts.get(w.id) ?? 0}
            selectedForAnnotation={selectedWidgetId === w.id}
            onSelectForAnnotation={onSelectWidget}
            onLayoutCommit={onLayoutCommit}
            onDelete={onDelete}
          />
        ))}
      </div>

      <div
        className={`studio-live-pill studio-glass is-${connectionStatus}`}
        title={
          connectionStatus === "open"
            ? "Studio canvas is receiving live updates"
            : connectionStatus === "connecting"
              ? "Connecting to Studio updates"
              : "Reconnecting to Studio updates"
        }
      >
        <span className="studio-live-dot" aria-hidden />
        <span>
          {connectionStatus === "open"
            ? "Live"
            : connectionStatus === "connecting"
              ? "Connecting"
              : "Reconnecting"}
        </span>
      </div>

      {widgetList.length === 0 && !error && (
        <div className="studio-canvas-empty">
          empty space — talk to hermes to summon something
        </div>
      )}
      {error && (
        <div className="studio-canvas-empty" style={{ color: "var(--studio-signal)" }}>
          {error}
        </div>
      )}

      <div className="studio-zoom-toolbar studio-glass">
        <button
          type="button"
          className="studio-zoom-btn"
          onClick={() => zoomBy(1 / ZOOM_STEP)}
          aria-label="Zoom out"
          title="Zoom out"
        >
          −
        </button>
        <button
          type="button"
          className="studio-zoom-btn studio-zoom-reset"
          onClick={fitAll}
          aria-label="Reset view"
          title="Reset view"
        >
          {Math.round(viewport.scale * 100)}%
        </button>
        <button
          type="button"
          className="studio-zoom-btn"
          onClick={() => zoomBy(ZOOM_STEP)}
          aria-label="Zoom in"
          title="Zoom in"
        >
          +
        </button>
      </div>
    </div>
  );
}
