/** Shared types for the Studio canvas — mirrors tools/studio_widgets.py YAML. */

export interface Position {
  x: number;
  y: number;
}

export interface Size {
  w: number;
  h: number;
}

export interface Widget {
  id: string;
  title: string;
  schema: string;
  created_at: string;
  updated_at: string;
  position: Position;
  size: Size;
  renderer: string;
}

export interface Space {
  id: string;
  title: string;
  schema: string;
  created_at: string;
  updated_at: string;
  widget_order: string[];
  widgets?: Widget[];
  background_url?: string;
  background_type?: "video" | "image" | "color" | string;
}

export interface StudioPreset {
  name: string;
  title: string;
  description: string;
  size?: Size;
  kind?: "widget" | "bundle" | "prompt" | "guide" | string;
  category?: string;
  bundle?: string[];
  source?: string;
}

export interface StudioAnnotation {
  id: string;
  spaceId: string;
  widgetId: string;
  widgetTitle: string;
  text: string;
  createdAt: string;
  resolvedAt?: string | null;
}

export type StudioEvent =
  | { type: "widget.upserted"; space_id: string; widget: Widget }
  | { type: "widget.deleted"; space_id: string; id: string }
  | {
      type: "widget.position_changed";
      space_id: string;
      id: string;
      position: Position;
      size: Size;
    }
  | { type: "space.updated"; space: Space }
  | { type: "space.deleted"; id: string };
