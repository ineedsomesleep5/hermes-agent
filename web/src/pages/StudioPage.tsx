/** Hermes Studio — full-screen workspace.
 *
 * Lives outside the dashboard chrome (App.tsx short-circuits when the
 * pathname starts with /studio). Renders the canvas + the floating chat
 * panel + a small brand strip for native Studio spaces.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronDown, Layers, Plus, Trash2 } from "lucide-react";
import { loadAnnotations, saveAnnotations } from "@/studio/annotations";
import { studioApi } from "@/studio/api";
import { StudioCanvas } from "@/studio/StudioCanvas";
import { StudioChat } from "@/studio/StudioChat";
import type { Space, StudioAnnotation, StudioPreset, Widget } from "@/studio/types";
import "@/studio/styles.css";

const DEFAULT_SPACE_ID = "default";
const SPACE_STORAGE_KEY = "hermes-studio-active-space";

function initialSpaceId(): string {
  const params = new URLSearchParams(window.location.search);
  const fromUrl = params.get("space")?.trim();
  if (fromUrl) return fromUrl;
  return localStorage.getItem(SPACE_STORAGE_KEY)?.trim() || DEFAULT_SPACE_ID;
}

function displaySpaceTitle(space?: Pick<Space, "id" | "title"> | null): string {
  if (!space) return "Default";
  const title = space.title?.trim();
  if (title) return title;
  const id = space.id?.trim();
  if (!id || id === DEFAULT_SPACE_ID) return "Default";
  return id.replace(/[-_]+/g, " ");
}

function slugifySpaceId(value: string): string {
  const slug = value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^[-_]+|[-_]+$/g, "")
    .slice(0, 56);
  return slug || "space";
}

function uniqueSpaceId(title: string, spaces: Space[]): string {
  const existing = new Set(spaces.map((space) => space.id));
  const base = slugifySpaceId(title);
  if (!existing.has(base)) return base;
  for (let index = 2; index < 1000; index += 1) {
    const next = `${base}-${index}`;
    if (!existing.has(next)) return next;
  }
  return `${base}-${Date.now().toString(36)}`;
}

function widgetCount(space: Pick<Space, "widget_order" | "widgets">): number {
  return space.widgets?.length ?? space.widget_order?.length ?? 0;
}

function normalizeSpace(space: Partial<Space>, index: number): Space {
  const id = String(space.id || "").trim() || (index === 0 ? DEFAULT_SPACE_ID : `space-${index + 1}`);
  const title = String(space.title || "").trim() || (id === DEFAULT_SPACE_ID ? "Default" : displaySpaceTitle({ id, title: "" }));
  return {
    id,
    title,
    schema: space.schema || "studio.space.v1",
    created_at: space.created_at || "",
    updated_at: space.updated_at || "",
    widget_order: space.widget_order || [],
    widgets: space.widgets,
    background_url: space.background_url,
    background_type: space.background_type,
  };
}

export default function StudioPage() {
  const [spaceId, setSpaceId] = useState(initialSpaceId);
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [spacesOpen, setSpacesOpen] = useState(false);
  const [spacesError, setSpacesError] = useState<string | null>(null);
  const [newSpaceTitle, setNewSpaceTitle] = useState("");
  const [creatingSpace, setCreatingSpace] = useState(false);
  const [templatesOpen, setTemplatesOpen] = useState(false);
  const [optionsOpen, setOptionsOpen] = useState(false);
  const [presets, setPresets] = useState<StudioPreset[]>([]);
  const [presetError, setPresetError] = useState<string | null>(null);
  const [installing, setInstalling] = useState<string | null>(null);
  const [widgets, setWidgets] = useState<Widget[]>([]);
  const [annotationState, setAnnotationState] = useState<{
    spaceId: string;
    items: StudioAnnotation[];
  }>(() => ({
    spaceId,
    items: loadAnnotations(spaceId),
  }));
  const [queuedAnnotations, setQueuedAnnotations] = useState<StudioAnnotation[]>([]);
  const [annotationMode, setAnnotationMode] = useState(false);
  const [annotationDraft, setAnnotationDraft] = useState("");
  const [selectedWidgetId, setSelectedWidgetId] = useState<string | null>(null);
  const annotations = annotationState.spaceId === spaceId ? annotationState.items : [];

  const setAnnotations = useCallback(
    (
      updater:
        | StudioAnnotation[]
        | ((previous: StudioAnnotation[]) => StudioAnnotation[]),
    ) => {
      setAnnotationState((previous) => {
        const currentItems =
          previous.spaceId === spaceId ? previous.items : loadAnnotations(spaceId);
        const items =
          typeof updater === "function"
            ? (updater as (previous: StudioAnnotation[]) => StudioAnnotation[])(currentItems)
            : updater;
        return { spaceId, items };
      });
    },
    [spaceId],
  );

  useEffect(() => {
    let alive = true;
    void studioApi
      .listSpaces()
      .then((data) => {
        if (!alive) return;
        const nextSpaces = (data ?? []).map((space, index) => normalizeSpace(space, index));
        setSpaces(nextSpaces);
        const ids = new Set(nextSpaces.map((space) => space.id));
        setSpaceId((current) => {
          if (ids.has(current)) return current;
          if (ids.has(DEFAULT_SPACE_ID)) return DEFAULT_SPACE_ID;
          return nextSpaces[0]?.id ?? DEFAULT_SPACE_ID;
        });
      })
      .catch((err: Error) => {
        if (alive) setSpacesError(err.message);
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    let alive = true;
    void studioApi
      .listPresets()
      .then((data) => {
        if (alive) setPresets(data.presets ?? []);
      })
      .catch((err: Error) => {
        if (alive) setPresetError(err.message);
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    localStorage.setItem(SPACE_STORAGE_KEY, spaceId);
    const url = new URL(window.location.href);
    url.searchParams.set("space", spaceId);
    window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  }, [spaceId]);

  useEffect(() => {
    setAnnotationState({ spaceId, items: loadAnnotations(spaceId) });
    setQueuedAnnotations([]);
    setAnnotationMode(false);
    setAnnotationDraft("");
    setSelectedWidgetId(null);
    setWidgets([]);
    setTemplatesOpen(false);
    setOptionsOpen(false);
    setSpacesOpen(false);
  }, [spaceId]);

  useEffect(() => {
    if (annotationState.spaceId === spaceId) {
      saveAnnotations(spaceId, annotationState.items);
    }
  }, [annotationState, spaceId]);

  useEffect(() => {
    const widgetIds = new Set(widgets.map((widget) => widget.id));
    if (widgets.length > 0) {
      setAnnotations((prev) => prev.filter((annotation) => widgetIds.has(annotation.widgetId)));
    }
    if (selectedWidgetId && !widgetIds.has(selectedWidgetId)) {
      setSelectedWidgetId(null);
    }
  }, [selectedWidgetId, setAnnotations, widgets]);

  useEffect(() => {
    setSpaces((previous) =>
      previous.map((space) =>
        space.id === spaceId
          ? {
              ...space,
              widget_order: widgets.map((widget) => widget.id),
              widgets,
              updated_at: new Date().toISOString(),
            }
          : space,
      ),
    );
  }, [spaceId, widgets]);

  const unresolvedAnnotations = useMemo(
    () => annotations.filter((annotation) => !annotation.resolvedAt),
    [annotations],
  );
  const activeSpace = useMemo(
    () =>
      spaces.find((space) => space.id === spaceId) ?? {
        id: spaceId,
        title:
          spaceId === DEFAULT_SPACE_ID
            ? "Default"
            : displaySpaceTitle({ id: spaceId, title: "" }),
        schema: "studio.space.v1",
        created_at: "",
        updated_at: "",
        widget_order: widgets.map((widget) => widget.id),
        widgets,
      },
    [spaceId, spaces, widgets],
  );
  const visibleSpaces = useMemo(() => {
    const ordered: Space[] = [];
    const pushSpace = (space?: Space) => {
      if (space && !ordered.some((existing) => existing.id === space.id)) {
        ordered.push(space);
      }
    };
    pushSpace(spaces.find((space) => space.id === DEFAULT_SPACE_ID));
    pushSpace(spaces.find((space) => space.id === spaceId));
    for (const space of spaces) {
      if (ordered.length >= 4) break;
      pushSpace(space);
    }
    return ordered;
  }, [spaceId, spaces]);
  const selectedWidget = useMemo(
    () => widgets.find((widget) => widget.id === selectedWidgetId) ?? null,
    [selectedWidgetId, widgets],
  );
  const selectedWidgetNotes = useMemo(
    () =>
      annotations
        .filter((annotation) => annotation.widgetId === selectedWidgetId)
        .sort((left, right) => right.createdAt.localeCompare(left.createdAt)),
    [annotations, selectedWidgetId],
  );

  const selectSpace = (nextSpaceId: string) => {
    if (nextSpaceId === spaceId) return;
    setSpaceId(nextSpaceId);
  };

  const createSpace = async () => {
    const title = newSpaceTitle.trim();
    if (!title || creatingSpace) return;
    setCreatingSpace(true);
    setSpacesError(null);
    try {
      const space = await studioApi.createSpace(uniqueSpaceId(title, spaces), title);
      setSpaces((previous) => [...previous.filter((item) => item.id !== space.id), space]);
      setNewSpaceTitle("");
      setSpaceId(space.id);
    } catch (err) {
      setSpacesError((err as Error).message);
    } finally {
      setCreatingSpace(false);
    }
  };

  const deleteSpace = async (targetSpace: Space) => {
    if (targetSpace.id === DEFAULT_SPACE_ID) return;
    if (!window.confirm(`Delete "${displaySpaceTitle(targetSpace)}" and its widgets?`)) {
      return;
    }
    setSpacesError(null);
    try {
      await studioApi.deleteSpace(targetSpace.id);
      setSpaces((previous) => previous.filter((space) => space.id !== targetSpace.id));
      if (targetSpace.id === spaceId) {
        setSpaceId(DEFAULT_SPACE_ID);
      }
    } catch (err) {
      setSpacesError((err as Error).message);
    }
  };

  const installPreset = async (preset: StudioPreset) => {
    setInstalling(preset.name);
    setPresetError(null);
    try {
      await studioApi.installPreset(preset.name, { space_id: spaceId });
      setTemplatesOpen(false);
    } catch (err) {
      setPresetError((err as Error).message);
    } finally {
      setInstalling(null);
    }
  };

  const addAnnotation = () => {
    if (!selectedWidget || !annotationDraft.trim()) return;
    const annotation: StudioAnnotation = {
      id: `annotation-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
      spaceId,
      widgetId: selectedWidget.id,
      widgetTitle: selectedWidget.title,
      text: annotationDraft.trim(),
      createdAt: new Date().toISOString(),
      resolvedAt: null,
    };
    setQueuedAnnotations((prev) => [...prev, annotation]);
    setAnnotationDraft("");
  };

  const toggleAnnotationResolved = (annotationId: string) => {
    setAnnotations((prev) =>
      prev.map((annotation) =>
        annotation.id === annotationId
          ? {
              ...annotation,
              resolvedAt: annotation.resolvedAt ? null : new Date().toISOString(),
            }
          : annotation,
      ),
    );
  };

  const deleteAnnotation = (annotationId: string) => {
    setAnnotations((prev) => prev.filter((annotation) => annotation.id !== annotationId));
  };

  return (
    <div className="hermes-studio">
      <StudioCanvas
        key={spaceId}
        spaceId={spaceId}
        annotations={annotations}
        annotationMode={annotationMode}
        selectedWidgetId={selectedWidgetId}
        onSelectWidget={(widgetId) => {
          setAnnotationMode(true);
          setSelectedWidgetId(widgetId);
          setTemplatesOpen(false);
          setOptionsOpen(false);
          setSpacesOpen(false);
        }}
        onWidgetsChange={setWidgets}
      />

      <div className="studio-brand">
        <span className="studio-brand-mark">Hermes Studio</span>
        <span className="studio-brand-sub">{displaySpaceTitle(activeSpace)}</span>
        <div className="studio-space-tabs" role="tablist" aria-label="Studio spaces">
          {visibleSpaces.map((space) => (
            <button
              key={space.id}
              type="button"
              role="tab"
              aria-selected={space.id === spaceId}
              className={`studio-space-tab ${space.id === spaceId ? "is-current" : ""}`}
              onClick={() => selectSpace(space.id)}
              title={`${displaySpaceTitle(space)} space`}
            >
              {displaySpaceTitle(space)}
            </button>
          ))}
          <button
            type="button"
            className={`studio-brand-back studio-space-menu-trigger ${spacesOpen ? "is-live" : ""}`}
            onClick={() => {
              setSpacesOpen((value) => !value);
              setTemplatesOpen(false);
              setOptionsOpen(false);
            }}
          >
            <Layers size={14} aria-hidden="true" />
            Spaces
            <ChevronDown size={14} aria-hidden="true" />
          </button>
        </div>
        <button
          type="button"
          className={`studio-brand-back ${annotationMode ? "is-live" : ""}`}
          onClick={() => {
            setAnnotationMode((value) => !value);
            setTemplatesOpen(false);
            setOptionsOpen(false);
            setSpacesOpen(false);
            if (!annotationMode && !selectedWidgetId && widgets[0]) {
              setSelectedWidgetId(widgets[0].id);
            }
          }}
        >
          Annotate{unresolvedAnnotations.length > 0 ? ` (${unresolvedAnnotations.length})` : ""}
        </button>
        <button
          type="button"
          className="studio-brand-back"
          onClick={() => {
            setTemplatesOpen((value) => !value);
            setOptionsOpen(false);
            setSpacesOpen(false);
          }}
        >
          Templates
        </button>
        <button
          type="button"
          className="studio-brand-back"
          onClick={() => {
            setOptionsOpen((value) => !value);
            setTemplatesOpen(false);
            setSpacesOpen(false);
          }}
        >
          Options
        </button>
      </div>

      {spacesOpen && (
        <section className="studio-spaces-drawer studio-glass-strong">
          <div className="studio-template-head">
            <div>
              <div className="studio-template-eyebrow">Studio spaces</div>
              <h2>{displaySpaceTitle(activeSpace)}</h2>
            </div>
            <button
              type="button"
              className="studio-template-close"
              onClick={() => setSpacesOpen(false)}
              aria-label="Close spaces"
            >
              x
            </button>
          </div>
          <form
            className="studio-space-create"
            onSubmit={(event) => {
              event.preventDefault();
              void createSpace();
            }}
          >
            <input
              value={newSpaceTitle}
              onChange={(event) => setNewSpaceTitle(event.target.value)}
              placeholder="New space name"
              aria-label="New space name"
            />
            <button type="submit" disabled={!newSpaceTitle.trim() || creatingSpace}>
              <Plus size={15} aria-hidden="true" />
              {creatingSpace ? "Creating..." : "Create"}
            </button>
          </form>
          {spacesError && <div className="studio-template-error">{spacesError}</div>}
          <div className="studio-space-list">
            {spaces.length === 0 ? (
              <div className="studio-space-empty">Loading spaces...</div>
            ) : (
              spaces.map((space) => (
                <div
                  key={space.id}
                  className={`studio-space-row ${space.id === spaceId ? "is-current" : ""}`}
                >
                  <button
                    type="button"
                    className="studio-space-main"
                    onClick={() => selectSpace(space.id)}
                  >
                    <span className="studio-space-title">{displaySpaceTitle(space)}</span>
                    <span className="studio-space-meta">
                      {widgetCount(space)} {widgetCount(space) === 1 ? "widget" : "widgets"} - {space.id}
                    </span>
                  </button>
                  {space.id !== DEFAULT_SPACE_ID && (
                    <button
                      type="button"
                      className="studio-space-danger"
                      onClick={() => void deleteSpace(space)}
                      aria-label={`Delete ${displaySpaceTitle(space)}`}
                    >
                      <Trash2 size={15} aria-hidden="true" />
                    </button>
                  )}
                </div>
              ))
            )}
          </div>
        </section>
      )}

      {templatesOpen && (
        <section className="studio-template-drawer studio-glass-strong">
          <div className="studio-template-head">
            <div>
              <div className="studio-template-eyebrow">{displaySpaceTitle(activeSpace)}</div>
              <h2>Templates</h2>
            </div>
            <button
              type="button"
              className="studio-template-close"
              onClick={() => setTemplatesOpen(false)}
              aria-label="Close templates"
            >
              x
            </button>
          </div>
          {presetError && <div className="studio-template-error">{presetError}</div>}
          <div className="studio-template-grid">
            {presets.map((preset) => (
              <article key={preset.name} className="studio-template-card">
                <div className="studio-template-card-top">
                  <h3>{preset.title}</h3>
                  {preset.kind === "bundle" && preset.bundle ? (
                    <span>{preset.bundle.length} pack</span>
                  ) : preset.size ? (
                    <span>
                      {preset.size.w}x{preset.size.h}
                    </span>
                  ) : preset.kind ? (
                    <span>{preset.kind}</span>
                  ) : null}
                </div>
                {(preset.category || preset.source) && (
                  <div className="studio-template-meta">
                    {preset.category || preset.source}
                  </div>
                )}
                <p>{preset.description}</p>
                <button
                  type="button"
                  onClick={() => void installPreset(preset)}
                  disabled={installing === preset.name}
                >
                  {installing === preset.name
                    ? "Installing..."
                    : preset.kind === "bundle"
                      ? "Install Pack"
                      : "Install"}
                </button>
              </article>
            ))}
          </div>
        </section>
      )}

      {annotationMode && (
        <section className="studio-annotation-panel studio-glass-strong">
          <div className="studio-template-head">
            <div>
              <div className="studio-template-eyebrow">Review notes</div>
              <h2>Annotations</h2>
            </div>
            <button
              type="button"
              className="studio-template-close"
              onClick={() => setAnnotationMode(false)}
              aria-label="Close annotations"
            >
              x
            </button>
          </div>
          <div className="studio-annotation-summary">
            {selectedWidget
              ? `Selected: ${selectedWidget.title} (${selectedWidget.id})`
              : "Tap a widget to attach a note."}
          </div>
          <textarea
            className="studio-annotation-input"
            placeholder={
              selectedWidget
                ? "Describe the exact fix, layout issue, or design note for Hermes"
                : "Select a widget first"
            }
            value={annotationDraft}
            onChange={(e) => setAnnotationDraft(e.target.value)}
            disabled={!selectedWidget}
            rows={3}
          />
          <button
            type="button"
            className="studio-annotation-save"
            onClick={addAnnotation}
            disabled={!selectedWidget || !annotationDraft.trim()}
          >
            Send to Hermes
          </button>
          <div className="studio-annotation-list">
            {queuedAnnotations.length > 0 && (
              <article className="studio-annotation-card is-pending">
                <div className="studio-annotation-card-head">
                  <span>Sent to Hermes</span>
                  <span>{queuedAnnotations.length} queued</span>
                </div>
                <p>Notes are one-shot repair requests now; they clear after Hermes receives them.</p>
              </article>
            )}
            {selectedWidgetNotes.length === 0 ? (
              <div className="studio-annotation-empty">
                No notes on this widget yet.
              </div>
            ) : (
              selectedWidgetNotes.map((annotation) => (
                <article
                  key={annotation.id}
                  className={`studio-annotation-card ${annotation.resolvedAt ? "is-resolved" : ""}`}
                >
                  <div className="studio-annotation-card-head">
                    <span>{annotation.widgetTitle}</span>
                    <span>
                      {annotation.resolvedAt ? "Resolved" : "Open"}
                    </span>
                  </div>
                  <p>{annotation.text}</p>
                  <div className="studio-annotation-card-actions">
                    <button
                      type="button"
                      onClick={() => toggleAnnotationResolved(annotation.id)}
                    >
                      {annotation.resolvedAt ? "Reopen" : "Resolve"}
                    </button>
                    <button
                      type="button"
                      onClick={() => deleteAnnotation(annotation.id)}
                    >
                      Delete
                    </button>
                  </div>
                </article>
              ))
            )}
          </div>
        </section>
      )}

      {optionsOpen && (
        <section className="studio-options-menu studio-glass-strong">
          <div className="studio-template-eyebrow">Studio options</div>
          <button type="button" onClick={() => window.location.reload()}>
            Reload Studio
          </button>
          <button
            type="button"
            onClick={() => {
              setTemplatesOpen(true);
              setOptionsOpen(false);
            }}
          >
            Open Templates
          </button>
          <button
            type="button"
            onClick={() => {
              setSpacesOpen(true);
              setOptionsOpen(false);
            }}
          >
            Open Spaces
          </button>
        </section>
      )}

      <StudioChat
        spaceId={spaceId}
        widgets={widgets}
        queuedAnnotation={queuedAnnotations[0] ?? null}
        onQueuedAnnotationHandled={(annotationId) => {
          setQueuedAnnotations((prev) =>
            prev.filter((annotation) => annotation.id !== annotationId),
          );
        }}
      />
    </div>
  );
}
