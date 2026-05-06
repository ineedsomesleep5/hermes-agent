/** A draggable, resizable glass card that hosts one widget.
 *
 * Drag is initiated on the header; resize on the bottom-right corner.
 * Visual updates use direct DOM mutations (translate3d / width / height)
 * during the drag for buttery 60fps; React state is only touched on
 * mouseup/touchend so the layout PATCH fires once, not per frame.
 *
 * Touch support mirrors mouse — on iOS we use Pointer Events when
 * available (most reliable across input modes), falling back to native
 * touch events. `touch-action: none` on the drag handle prevents the
 * browser from hijacking the gesture for scroll/zoom.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { Position, Size, Widget } from "./types";
import { WidgetHost } from "./WidgetHost";

const GRID = 80; // px per logical grid unit; mirrors --studio-grid-unit

function snap(px: number): number {
  return Math.round(px / GRID);
}

interface Props {
  widget: Widget;
  spaceId: string;
  /** Highlight this card with a red bloom (e.g. just-created via SSE). */
  flash?: boolean;
  /** Live ref to the canvas viewport scale, so drag deltas adjust on zoom. */
  scaleRef?: { current: number };
  annotationMode?: boolean;
  annotationCount?: number;
  selectedForAnnotation?: boolean;
  onSelectForAnnotation?: (id: string) => void;
  onLayoutCommit: (id: string, position: Position, size: Size) => void;
  onDelete: (id: string) => void;
}

interface PointerStart {
  x: number;
  y: number;
}

function pointerOf(e: PointerEvent | MouseEvent | TouchEvent): PointerStart {
  if ("touches" in e && e.touches.length > 0) {
    return { x: e.touches[0].clientX, y: e.touches[0].clientY };
  }
  if ("changedTouches" in e && e.changedTouches.length > 0) {
    return { x: e.changedTouches[0].clientX, y: e.changedTouches[0].clientY };
  }
  return {
    x: (e as PointerEvent | MouseEvent).clientX,
    y: (e as PointerEvent | MouseEvent).clientY,
  };
}

export function WidgetCard({
  widget,
  spaceId,
  flash = false,
  scaleRef,
  annotationMode = false,
  annotationCount = 0,
  selectedForAnnotation = false,
  onSelectForAnnotation,
  onLayoutCommit,
  onDelete,
}: Props) {
  const getScale = () => {
    const s = scaleRef?.current ?? 1;
    return s > 0.01 ? s : 1;
  };
  const cardRef = useRef<HTMLDivElement | null>(null);
  // True only while the user is actively dragging or resizing this widget.
  const [interacting, setInteracting] = useState(false);
  const [minimized, setMinimized] = useState(false);
  const [renderKey, setRenderKey] = useState(0);

  // Snap layout state (logical grid units).
  const [layout, setLayout] = useState<{ position: Position; size: Size }>({
    position: widget.position,
    size: widget.size,
  });

  // Sync external updates (e.g. another tab patches layout) into local state
  // unless we're mid-drag.
  const draggingRef = useRef(false);
  useEffect(() => {
    if (!draggingRef.current) {
      setLayout({ position: widget.position, size: widget.size });
    }
  }, [widget.position, widget.size]);

  const beginDrag = useCallback(
    (start: PointerStart) => {
      const startPosX = layout.position.x * GRID;
      const startPosY = layout.position.y * GRID;
      const card = cardRef.current;
      if (!card) return null;
      draggingRef.current = true;
      setInteracting(true);

      const onMove = (clientX: number, clientY: number) => {
        const k = getScale();
        const dx = (clientX - start.x) / k;
        const dy = (clientY - start.y) / k;
        card.style.transform = `translate3d(${startPosX + dx}px, ${startPosY + dy}px, 0)`;
      };

      const onEnd = (clientX: number, clientY: number) => {
        const k = getScale();
        const dx = (clientX - start.x) / k;
        const dy = (clientY - start.y) / k;
        const nextPx: Position = {
          x: snap(startPosX + dx),
          y: snap(startPosY + dy),
        };
        draggingRef.current = false;
        setInteracting(false);
        setLayout((prev) => ({ ...prev, position: nextPx }));
        onLayoutCommit(widget.id, nextPx, layout.size);
      };

      return { onMove, onEnd };
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [layout.position, layout.size, onLayoutCommit, widget.id],
  );

  const beginResize = useCallback(
    (start: PointerStart) => {
      const startW = layout.size.w * GRID;
      const startH = layout.size.h * GRID;
      const card = cardRef.current;
      if (!card) return null;
      draggingRef.current = true;
      setInteracting(true);

      const onMove = (clientX: number, clientY: number) => {
        const k = getScale();
        const w = Math.max(GRID, startW + (clientX - start.x) / k);
        const h = Math.max(GRID, startH + (clientY - start.y) / k);
        card.style.width = `${w}px`;
        card.style.height = `${h}px`;
      };

      const onEnd = (clientX: number, clientY: number) => {
        const k = getScale();
        const wPx = Math.max(GRID, startW + (clientX - start.x) / k);
        const hPx = Math.max(GRID, startH + (clientY - start.y) / k);
        const nextSize: Size = {
          w: Math.max(1, snap(wPx)),
          h: Math.max(1, snap(hPx)),
        };
        draggingRef.current = false;
        setInteracting(false);
        setLayout((prev) => ({ ...prev, size: nextSize }));
        onLayoutCommit(widget.id, layout.position, nextSize);
      };

      return { onMove, onEnd };
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [layout.position, layout.size, onLayoutCommit, widget.id],
  );

  // Mouse drag handler (header).
  const onHeaderMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      const handlers = beginDrag({ x: e.clientX, y: e.clientY });
      if (!handlers) return;
      const onMove = (mv: MouseEvent) => handlers.onMove(mv.clientX, mv.clientY);
      const onUp = (mv: MouseEvent) => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        handlers.onEnd(mv.clientX, mv.clientY);
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    },
    [beginDrag],
  );

  const onResizeMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      const handlers = beginResize({ x: e.clientX, y: e.clientY });
      if (!handlers) return;
      const onMove = (mv: MouseEvent) => handlers.onMove(mv.clientX, mv.clientY);
      const onUp = (mv: MouseEvent) => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        handlers.onEnd(mv.clientX, mv.clientY);
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    },
    [beginResize],
  );

  // Touch drag handler (header) — passive: false so we can preventDefault.
  const onHeaderTouchStart = useCallback(
    (e: React.TouchEvent) => {
      if (e.touches.length !== 1) return;
      e.stopPropagation();
      const t = e.touches[0];
      const handlers = beginDrag({ x: t.clientX, y: t.clientY });
      if (!handlers) return;
      const onMove = (tev: TouchEvent) => {
        if (tev.touches.length === 0) return;
        tev.preventDefault();
        const tt = tev.touches[0];
        handlers.onMove(tt.clientX, tt.clientY);
      };
      const onEnd = (tev: TouchEvent) => {
        document.removeEventListener("touchmove", onMove);
        document.removeEventListener("touchend", onEnd);
        document.removeEventListener("touchcancel", onEnd);
        const p = pointerOf(tev);
        handlers.onEnd(p.x, p.y);
      };
      document.addEventListener("touchmove", onMove, { passive: false });
      document.addEventListener("touchend", onEnd);
      document.addEventListener("touchcancel", onEnd);
    },
    [beginDrag],
  );

  const onResizeTouchStart = useCallback(
    (e: React.TouchEvent) => {
      if (e.touches.length !== 1) return;
      e.stopPropagation();
      const t = e.touches[0];
      const handlers = beginResize({ x: t.clientX, y: t.clientY });
      if (!handlers) return;
      const onMove = (tev: TouchEvent) => {
        if (tev.touches.length === 0) return;
        tev.preventDefault();
        const tt = tev.touches[0];
        handlers.onMove(tt.clientX, tt.clientY);
      };
      const onEnd = (tev: TouchEvent) => {
        document.removeEventListener("touchmove", onMove);
        document.removeEventListener("touchend", onEnd);
        document.removeEventListener("touchcancel", onEnd);
        const p = pointerOf(tev);
        handlers.onEnd(p.x, p.y);
      };
      document.addEventListener("touchmove", onMove, { passive: false });
      document.addEventListener("touchend", onEnd);
      document.addEventListener("touchcancel", onEnd);
    },
    [beginResize],
  );

  const x = layout.position.x * GRID;
  const y = layout.position.y * GRID;
  const w = layout.size.w * GRID;
  const h = layout.size.h * GRID;

  return (
    <div
      ref={cardRef}
      className={[
        "studio-widget",
        "studio-glass",
        interacting ? "is-active" : "",
        flash ? "is-flash" : "",
        minimized ? "is-minimized" : "",
        annotationMode ? "is-annotation-mode" : "",
        selectedForAnnotation ? "is-annotation-selected" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      style={{
        transform: `translate3d(${x}px, ${y}px, 0)`,
        width: `${w}px`,
        height: `${h}px`,
      }}
      data-widget-id={widget.id}
      data-space-id={spaceId}
      onClick={() => {
        if (annotationMode) onSelectForAnnotation?.(widget.id);
      }}
    >
      {annotationCount > 0 && (
        <button
          type="button"
          className="studio-widget-note-badge"
          onClick={(e) => {
            e.stopPropagation();
            onSelectForAnnotation?.(widget.id);
          }}
          title={`${annotationCount} note${annotationCount === 1 ? "" : "s"}`}
          aria-label={`${annotationCount} annotation notes`}
        >
          {annotationCount}
        </button>
      )}
      <div
        className="studio-widget-header"
        onMouseDown={onHeaderMouseDown}
        onTouchStart={onHeaderTouchStart}
      >
        <span className="studio-widget-title">{widget.title}</span>
        <div className="studio-widget-actions">
          <button
            type="button"
            className="studio-widget-action"
            onClick={(e) => {
              e.stopPropagation();
              onSelectForAnnotation?.(widget.id);
            }}
            aria-label="Annotate widget"
            title="Annotate"
          >
            #
          </button>
          <button
            type="button"
            className="studio-widget-action"
            onClick={(e) => {
              e.stopPropagation();
              setRenderKey((key) => key + 1);
            }}
            aria-label="Reload widget"
            title="Reload"
          >
            R
          </button>
          <button
            type="button"
            className="studio-widget-action"
            onClick={(e) => {
              e.stopPropagation();
              setMinimized((value) => !value);
            }}
            aria-label={minimized ? "Expand widget" : "Minimize widget"}
            title={minimized ? "Expand" : "Minimize"}
          >
            {minimized ? "+" : "_"}
          </button>
          <button
            type="button"
            className="studio-widget-action is-destructive"
            onClick={(e) => {
              e.stopPropagation();
              onDelete(widget.id);
            }}
            aria-label="Delete widget"
            title="Delete"
          >
            x
          </button>
        </div>
      </div>
      {!minimized && (
        <>
          <WidgetHost key={renderKey} widget={widget} space={{ id: spaceId }} />
          <div
            className="studio-widget-resize"
            onMouseDown={onResizeMouseDown}
            onTouchStart={onResizeTouchStart}
            aria-label="Resize"
          />
        </>
      )}
    </div>
  );
}
