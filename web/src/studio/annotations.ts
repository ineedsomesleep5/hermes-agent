import type { StudioAnnotation } from "./types";

const STORAGE_PREFIX = "__hermes_studio_annotations__:";

function storageKey(spaceId: string): string {
  return `${STORAGE_PREFIX}${spaceId}`;
}

export function loadAnnotations(spaceId: string): StudioAnnotation[] {
  try {
    const raw = localStorage.getItem(storageKey(spaceId));
    if (!raw) return [];
    const data = JSON.parse(raw);
    return Array.isArray(data) ? (data as StudioAnnotation[]) : [];
  } catch {
    return [];
  }
}

export function saveAnnotations(spaceId: string, annotations: StudioAnnotation[]): void {
  localStorage.setItem(storageKey(spaceId), JSON.stringify(annotations));
}
