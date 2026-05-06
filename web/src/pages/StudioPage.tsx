/** Hermes Studio — full-screen workspace.
 *
 * Lives outside the dashboard chrome (App.tsx short-circuits when the
 * pathname starts with /studio). Renders the canvas + the floating chat
 * panel + a small brand strip with a back button to /sessions.
 */

import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { loadAnnotations, saveAnnotations } from "@/studio/annotations";
import { studioApi } from "@/studio/api";
import { StudioCanvas } from "@/studio/StudioCanvas";
import { StudioChat } from "@/studio/StudioChat";
import type { StudioAnnotation, StudioPreset, Widget } from "@/studio/types";
import "@/studio/styles.css";

export default function StudioPage() {
  const navigate = useNavigate();
  const spaceId = "default";
  const [templatesOpen, setTemplatesOpen] = useState(false);
  const [optionsOpen, setOptionsOpen] = useState(false);
  const [presets, setPresets] = useState<StudioPreset[]>([]);
  const [presetError, setPresetError] = useState<string | null>(null);
  const [installing, setInstalling] = useState<string | null>(null);
  const [widgets, setWidgets] = useState<Widget[]>([]);
  const [annotations, setAnnotations] = useState<StudioAnnotation[]>(() =>
    loadAnnotations(spaceId),
  );
  const [queuedAnnotations, setQueuedAnnotations] = useState<StudioAnnotation[]>([]);
  const [annotationMode, setAnnotationMode] = useState(false);
  const [annotationDraft, setAnnotationDraft] = useState("");
  const [selectedWidgetId, setSelectedWidgetId] = useState<string | null>(null);

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
    saveAnnotations(spaceId, annotations);
  }, [annotations, spaceId]);

  useEffect(() => {
    const widgetIds = new Set(widgets.map((widget) => widget.id));
    setAnnotations((prev) => prev.filter((annotation) => widgetIds.has(annotation.widgetId)));
    if (selectedWidgetId && !widgetIds.has(selectedWidgetId)) {
      setSelectedWidgetId(null);
    }
  }, [selectedWidgetId, widgets]);

  const unresolvedAnnotations = useMemo(
    () => annotations.filter((annotation) => !annotation.resolvedAt),
    [annotations],
  );
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
        spaceId={spaceId}
        annotations={annotations}
        annotationMode={annotationMode}
        selectedWidgetId={selectedWidgetId}
        onSelectWidget={(widgetId) => {
          setAnnotationMode(true);
          setSelectedWidgetId(widgetId);
          setTemplatesOpen(false);
          setOptionsOpen(false);
        }}
        onWidgetsChange={setWidgets}
      />

      <div className="studio-brand">
        <button
          type="button"
          className="studio-brand-back"
          onClick={() => navigate("/sessions")}
        >
          ← Dashboard
        </button>
        <span className="studio-brand-mark">Hermes Studio</span>
        <span className="studio-brand-sub">{spaceId}</span>
        <button
          type="button"
          className={`studio-brand-back ${annotationMode ? "is-live" : ""}`}
          onClick={() => {
            setAnnotationMode((value) => !value);
            setTemplatesOpen(false);
            setOptionsOpen(false);
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
          }}
        >
          Options
        </button>
      </div>

      {templatesOpen && (
        <section className="studio-template-drawer studio-glass-strong">
          <div className="studio-template-head">
            <div>
              <div className="studio-template-eyebrow">Space Agent style</div>
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
          <p>
            More space actions next: duplicate, export, import, repair layout,
            and per-space instructions.
          </p>
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
