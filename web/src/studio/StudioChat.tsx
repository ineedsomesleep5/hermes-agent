/** Floating glass chat panel — streams to/from /api/studio/chat.
 *
 * Renders a list of message bubbles plus tool-call indicators. Tool
 * side-effects (widget mutations) fan out separately over the canvas's
 * own SSE stream, so the canvas updates alongside this chat.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronUp, Mic, Paperclip, X } from "lucide-react";
import { studioApi } from "./api";
import { studioFetch } from "./auth";
import type { StudioAnnotation, Widget } from "./types";

// Minimal Web Speech API type declarations. TypeScript's lib.dom.d.ts
// doesn't ship these reliably; we declare just what we use here so the
// dictation handler typechecks without pulling a polyfill.
interface SpeechRecognitionAlternative {
  transcript: string;
  confidence: number;
}
interface SpeechRecognitionResult {
  isFinal: boolean;
  length: number;
  [index: number]: SpeechRecognitionAlternative;
  item(index: number): SpeechRecognitionAlternative;
}
interface SpeechRecognitionResultList {
  length: number;
  [index: number]: SpeechRecognitionResult;
  item(index: number): SpeechRecognitionResult;
}
interface SpeechRecognitionEvent extends Event {
  resultIndex: number;
  results: SpeechRecognitionResultList;
}
interface SpeechRecognitionErrorEvent extends Event {
  error: string;
  message: string;
}
interface SpeechRecognition extends EventTarget {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((event: SpeechRecognitionEvent) => void) | null;
  onerror: ((event: SpeechRecognitionErrorEvent) => void) | null;
  onend: ((event: Event) => void) | null;
}
type SpeechRecognitionConstructor = new () => SpeechRecognition;

const SESSION_KEY = "hermes-studio-chat-session";
const HISTORY_INDEX_KEY = "hermes-studio-chat-history-index";
const HISTORY_MESSAGE_PREFIX = "hermes-studio-chat-history:";
const HISTORY_LIMIT = 30;
const CHAT_SIZE_KEY = "hermes-studio-chat-size";
const COMPANION_POSITION_KEY = "hermes-studio-companion-position";
const CHAT_MIN_WIDTH = 300;
const CHAT_MIN_HEIGHT = 220;
const CHAT_DEFAULT_WIDTH = 420;
const CHAT_DEFAULT_HEIGHT = 340;
const COMPANION_DEFAULT_POSITION = { x: 24, y: 86 };
const GLANCE_FALLBACK =
  "Ready to work on widgets, spaces, browser flows, and Studio itself.";

interface ToolCall {
  id: string;
  name: string;
  args?: unknown;
  result?: unknown;
  state: "running" | "done" | "error";
}

interface Message {
  role: "user" | "assistant";
  text: string;
  toolCalls: ToolCall[];
  streaming?: boolean;
}

interface HistoryRecord {
  id: string;
  title: string;
  updatedAt: string;
  spaceId: string;
}

interface PanelSize {
  width: number;
  height: number;
}

interface CompanionPosition {
  x: number;
  y: number;
}

interface ViewportSize {
  width: number;
  height: number;
}

interface PointerStart {
  x: number;
  y: number;
}

interface CompanionDrag {
  pointerId: number;
  startX: number;
  startY: number;
  origin: CompanionPosition;
  last: CompanionPosition;
  moved: boolean;
}

type CompanionCssVars = React.CSSProperties & {
  "--studio-companion-x": string;
  "--studio-companion-y": string;
};

type ChatCssVars = React.CSSProperties & {
  "--studio-chat-left": string;
  "--studio-chat-bottom": string;
  "--studio-chat-width": string;
  "--studio-chat-height": string;
};

function getViewportSize(): ViewportSize {
  if (typeof window === "undefined") return { width: 1440, height: 900 };
  return { width: window.innerWidth, height: window.innerHeight };
}

function sameCompanionPosition(a: CompanionPosition, b: CompanionPosition): boolean {
  return a.x === b.x && a.y === b.y;
}

function clampCompanionPosition(
  position: CompanionPosition,
  viewport = getViewportSize(),
): CompanionPosition {
  const pad = 12;
  const maxX = Math.max(pad, viewport.width - 154);
  const maxY = Math.max(24, viewport.height - 156);
  return {
    x: Math.max(pad, Math.min(Math.round(position.x), maxX)),
    y: Math.max(18, Math.min(Math.round(position.y), maxY)),
  };
}

function loadCompanionPosition(): CompanionPosition {
  try {
    const raw = localStorage.getItem(COMPANION_POSITION_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<CompanionPosition>;
      if (typeof parsed.x === "number" && typeof parsed.y === "number") {
        return clampCompanionPosition({ x: parsed.x, y: parsed.y });
      }
    }
  } catch {
    // ignore
  }
  return clampCompanionPosition(COMPANION_DEFAULT_POSITION);
}

function saveCompanionPosition(position: CompanionPosition): void {
  try {
    localStorage.setItem(COMPANION_POSITION_KEY, JSON.stringify(clampCompanionPosition(position)));
  } catch {
    // ignore
  }
}

function getChatPlacement(
  companion: CompanionPosition,
  extended: boolean,
  savedSize: PanelSize,
  viewport: ViewportSize,
): { left: number; bottom: number; width: number; height: number } {
  const pad = 12;
  const compactWidth = Math.min(CHAT_DEFAULT_WIDTH, Math.max(CHAT_MIN_WIDTH, viewport.width - 168));
  const compactHeight = Math.min(318, Math.max(CHAT_MIN_HEIGHT, viewport.height - 128));
  const extendedWidth = Math.min(Math.max(savedSize.width, 500), Math.max(CHAT_MIN_WIDTH, viewport.width - 32), 560);
  const extendedHeight = Math.min(Math.max(savedSize.height, 520), Math.max(CHAT_MIN_HEIGHT, viewport.height - 92), 640);
  const width = extended ? extendedWidth : compactWidth;
  const height = extended ? extendedHeight : compactHeight;

  return {
    left: Math.max(pad, Math.min(companion.x + 120, viewport.width - width - pad)),
    bottom: Math.max(pad, Math.min(companion.y + 22, viewport.height - height - pad)),
    width,
    height,
  };
}

function clampPanelSize(size: PanelSize, maxWidth = Number.POSITIVE_INFINITY, maxHeight = Number.POSITIVE_INFINITY): PanelSize {
  return {
    width: Math.max(CHAT_MIN_WIDTH, Math.min(Math.round(size.width), maxWidth)),
    height: Math.max(CHAT_MIN_HEIGHT, Math.min(Math.round(size.height), maxHeight)),
  };
}

function loadChatSize(): PanelSize {
  try {
    const raw = localStorage.getItem(CHAT_SIZE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<PanelSize>;
      if (typeof parsed.width === "number" && typeof parsed.height === "number") {
        return clampPanelSize({ width: parsed.width, height: parsed.height });
      }
    }
  } catch {
    // ignore
  }
  return { width: CHAT_DEFAULT_WIDTH, height: CHAT_DEFAULT_HEIGHT };
}

function saveChatSize(size: PanelSize): void {
  try {
    localStorage.setItem(CHAT_SIZE_KEY, JSON.stringify(clampPanelSize(size)));
  } catch {
    // ignore
  }
}

const WELCOME_MESSAGE: Message = {
  role: "assistant",
  text: "I'm Hermes. I can build live widgets on the canvas. What do you want to see?",
  toolCalls: [],
};

interface Attachment {
  id: string;
  name: string;
  mime: string;
  /** "image" -> dataUrl is image data URL; "text" -> dataUrl is the file text. */
  kind: "image" | "text";
  dataUrl: string; // for images: data URL; for text: the actual text content
  size: number;
}

type ServerEvent =
  | { type: "user_echo"; content: string }
  | { type: "text"; delta: string }
  | { type: "tool_call_start"; id: string; name: string; args: unknown }
  | { type: "tool_call_result"; id: string; name: string; result: unknown }
  | { type: "done" }
  | { type: "ping" }
  | { type: "error"; message: string };

function getSessionId(): string {
  let id = sessionStorage.getItem(SESSION_KEY);
  if (!id) {
    id = newSessionId();
    sessionStorage.setItem(SESSION_KEY, id);
  }
  return id;
}

function newSessionId(): string {
  return `studio-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function chatStorageKey(sessionId: string): string {
  return `${HISTORY_MESSAGE_PREFIX}${sessionId}`;
}

function loadHistoryIndex(): HistoryRecord[] {
  try {
    const raw = localStorage.getItem(HISTORY_INDEX_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function writeHistoryIndex(records: HistoryRecord[]): void {
  try {
    localStorage.setItem(
      HISTORY_INDEX_KEY,
      JSON.stringify(records.slice(0, HISTORY_LIMIT)),
    );
  } catch {
    // localStorage may be unavailable in private or locked-down browsers.
  }
}

function safeMessages(value: unknown): Message[] {
  if (!Array.isArray(value)) return [WELCOME_MESSAGE];
  const messages = value
    .filter((item): item is Message => {
      if (!item || typeof item !== "object") return false;
      const candidate = item as Partial<Message>;
      return (
        (candidate.role === "user" || candidate.role === "assistant") &&
        typeof candidate.text === "string"
      );
    })
    .map((item) => ({
      role: item.role,
      text: item.text,
      toolCalls: Array.isArray(item.toolCalls) ? item.toolCalls : [],
      streaming: false,
    }));
  return messages.length > 0 ? messages : [WELCOME_MESSAGE];
}

function loadStoredMessages(sessionId: string): Message[] {
  try {
    const raw = localStorage.getItem(chatStorageKey(sessionId));
    return raw ? safeMessages(JSON.parse(raw)) : [WELCOME_MESSAGE];
  } catch {
    return [WELCOME_MESSAGE];
  }
}

function titleForMessages(messages: Message[]): string {
  const firstUser = messages.find((message) => message.role === "user" && message.text.trim());
  if (!firstUser) return "New Studio chat";
  const oneLine = firstUser.text.replace(/\s+/g, " ").trim();
  return oneLine.length > 52 ? `${oneLine.slice(0, 52)}...` : oneLine;
}

function compactPreview(text: string, fallback = GLANCE_FALLBACK): string {
  const normalized = text.replace(/\s+/g, " ").trim() || fallback;
  return normalized.length > 156 ? `${normalized.slice(0, 153)}...` : normalized;
}

function isGratitudeOnly(text: string): boolean {
  const normalized = text
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s']/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (!normalized) return false;
  return /^(perfect|great|awesome|amazing|nice|cool|sweet|good|looks good|that looks good|thank you|thanks|thx|ty|appreciate it|love it|beautiful|excellent)(\s+(thank you|thanks|thx|ty|appreciate it|perfect|great|awesome|nice|cool|sweet|good|love it))*$/.test(
    normalized,
  );
}

function buildAnnotationRequest(annotation: StudioAnnotation, widgets: Widget[]): string {
  const widgetTitle =
    widgets.find((widget) => widget.id === annotation.widgetId)?.title ||
    annotation.widgetTitle ||
    annotation.widgetId;
  return [
    "[Studio annotation]",
    `widget=${widgetTitle} (${annotation.widgetId})`,
    `note=${annotation.text}`,
    "",
    "[User request]",
    "Fix this annotated issue on the widget now. Read the widget, make the requested edit, verify it, then stop.",
  ].join("\n");
}

function normalizeWidgetText(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function resolveLocalDelete(text: string, widgets: Widget[]): Widget | null {
  const normalized = normalizeWidgetText(text);
  if (!/\b(close|delete|remove|dismiss|hide|clear|x)\b/.test(normalized)) {
    return null;
  }
  const aliases = new Map<string, string>([
    ["welcome", "welcome"],
    ["hermes studio", "welcome"],
    ["youtube", "youtube-window"],
    ["you tube", "youtube-window"],
    ["logs", "logs"],
    ["agent logs", "logs"],
    ["skills", "skills"],
    ["memory", "memory-graph"],
    ["memory graph", "memory-graph"],
    ["tetris", "tetris"],
  ]);
  for (const [phrase, id] of aliases) {
    if (normalized.includes(phrase)) {
      const match = widgets.find((widget) => widget.id === id);
      if (match) return match;
    }
  }
  return (
    widgets.find((widget) => {
      const id = normalizeWidgetText(widget.id);
      const title = normalizeWidgetText(widget.title || "");
      return (id && normalized.includes(id)) || (title && normalized.includes(title));
    }) || null
  );
}

type ContentPart =
  | { type: "text"; text: string }
  | { type: "image_url"; image_url: { url: string } };

async function* streamChat(
  sessionId: string,
  message: string | ContentPart[],
  spaceId: string,
  signal: AbortSignal,
): AsyncGenerator<ServerEvent> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  };

  const res = await studioFetch("/api/studio/chat", {
    method: "POST",
    headers,
    body: JSON.stringify({ session_id: sessionId, message, space_id: spaceId }),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(`chat ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const raw = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
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
        yield JSON.parse(data) as ServerEvent;
      } catch {
        // skip
      }
    }
  }
}

function isMobileViewport(): boolean {
  if (typeof window === "undefined") return false;
  return window.matchMedia("(max-width: 767px)").matches;
}

export function StudioChat({
  spaceId = "default",
  annotations = [],
  widgets = [],
  queuedAnnotation = null,
  onQueuedAnnotationHandled,
}: {
  spaceId?: string;
  annotations?: StudioAnnotation[];
  widgets?: Widget[];
  queuedAnnotation?: StudioAnnotation | null;
  onQueuedAnnotationHandled?: (annotationId: string) => void;
}) {
  const [sessionId, setSessionId] = useState(() => getSessionId());
  const [messages, setMessages] = useState<Message[]>(() =>
    loadStoredMessages(getSessionId()),
  );
  const [history, setHistory] = useState<HistoryRecord[]>(() => loadHistoryIndex());
  const [historyOpen, setHistoryOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Default to the Space Agent / Codex Pets style glance shell so the canvas
  // stays primary. Clicking the bubble opens the full working chat.
  const [collapsed, setCollapsed] = useState<boolean>(() => true);
  const [extended, setExtended] = useState<boolean>(() => false);
  const [panelSize, setPanelSize] = useState<PanelSize>(() => loadChatSize());
  const [viewportSize, setViewportSize] = useState<ViewportSize>(() => getViewportSize());
  const [companionPosition, setCompanionPosition] = useState<CompanionPosition>(() =>
    loadCompanionPosition(),
  );
  const abortRef = useRef<AbortController | null>(null);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const companionDragRef = useRef<CompanionDrag | null>(null);
  const suppressCompanionClickRef = useRef(false);
  const handledQueuedAnnotationRef = useRef<string | null>(null);
  const queuedAnnotationPromptRef = useRef<{ id: string; prompt: string } | null>(null);
  const annotationSummary = useMemo(() => {
    if (annotations.length === 0) return "";
    const widgetTitleById = new Map(widgets.map((widget) => [widget.id, widget.title]));
    const lines = annotations.map((annotation, index) => {
      const title = widgetTitleById.get(annotation.widgetId) || annotation.widgetTitle || annotation.widgetId;
      return `${index + 1}. widget=${title} (${annotation.widgetId}) note=${annotation.text}`;
    });
    return [
      "[Studio annotations]",
      "Use these as precise edit notes for the current canvas before replying.",
      ...lines,
      "",
      "[User request]",
    ].join("\n");
  }, [annotations, widgets]);
  const latestAssistantMessage = useMemo(
    () => [...messages].reverse().find((message) => message.role === "assistant"),
    [messages],
  );
  const glanceText = useMemo(
    () => compactPreview(latestAssistantMessage?.text || (streaming ? "Working..." : "")),
    [latestAssistantMessage?.text, streaming],
  );
  const glanceTitle = streaming
    ? "Hermes is working"
    : latestAssistantMessage?.toolCalls.some((tool) => tool.state === "error")
      ? "Hermes needs attention"
      : "Hermes Studio";
  const chatPlacement = useMemo(
    () => getChatPlacement(companionPosition, extended, panelSize, viewportSize),
    [companionPosition, extended, panelSize, viewportSize],
  );
  const companionStyle = useMemo<CompanionCssVars>(
    () => ({
      "--studio-companion-x": `${companionPosition.x}px`,
      "--studio-companion-y": `${companionPosition.y}px`,
    }),
    [companionPosition],
  );

  const addFiles = useCallback(async (files: FileList | File[]) => {
    const next: Attachment[] = [];
    for (const file of Array.from(files)) {
      const id = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
      const isImage = file.type.startsWith("image/");
      // Cap individual attachment size: 8MB images, 256KB text files.
      const cap = isImage ? 8 * 1024 * 1024 : 256 * 1024;
      if (file.size > cap) {
        setError(
          `${file.name} is too large (${Math.round(file.size / 1024)}KB > ${cap / 1024}KB)`,
        );
        continue;
      }
      try {
        if (isImage) {
          const dataUrl = await new Promise<string>((resolve, reject) => {
            const r = new FileReader();
            r.onload = () => resolve(String(r.result));
            r.onerror = () => reject(r.error);
            r.readAsDataURL(file);
          });
          next.push({
            id,
            name: file.name,
            mime: file.type,
            kind: "image",
            dataUrl,
            size: file.size,
          });
        } else {
          // Treat as text — works for code, json, md, txt, etc.
          const text = await file.text();
          next.push({
            id,
            name: file.name,
            mime: file.type || "text/plain",
            kind: "text",
            dataUrl: text,
            size: file.size,
          });
        }
      } catch (e) {
        console.warn("[studio] attachment read failed", e);
      }
    }
    setAttachments((prev) => [...prev, ...next]);
  }, []);

  const removeAttachment = useCallback((id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  }, []);

  // ---------------------------------------------------------------------
  // Voice dictation (Web Speech API — Chrome/Edge native, Safari/iOS via
  // webkitSpeechRecognition). Tap mic to toggle; results stream into the
  // textarea draft, picking up where the typed text left off.
  // ---------------------------------------------------------------------

  const [recording, setRecording] = useState(false);
  const recognitionRef = useRef<SpeechRecognition | null>(null);
  const dictationBaseRef = useRef("");
  const speechSupported = typeof window !== "undefined" && (
    "SpeechRecognition" in window ||
    "webkitSpeechRecognition" in window
  );

  const stopDictation = useCallback(() => {
    const r = recognitionRef.current;
    if (r) {
      try {
        r.stop();
      } catch {
        /* ignore */
      }
    }
  }, []);

  const startDictation = useCallback(() => {
    if (recording) {
      stopDictation();
      return;
    }
    const w = window as unknown as {
      SpeechRecognition?: SpeechRecognitionConstructor;
      webkitSpeechRecognition?: SpeechRecognitionConstructor;
    };
    const Ctor = w.SpeechRecognition ?? w.webkitSpeechRecognition;
    if (!Ctor) {
      setError("Voice dictation isn't supported in this browser.");
      return;
    }
    const r: SpeechRecognition = new Ctor();
    r.continuous = true;
    r.interimResults = true;
    r.lang = navigator.language || "en-US";

    // Capture whatever's in the draft right now so dictation appends to it.
    dictationBaseRef.current = draft.length > 0 && !draft.endsWith(" ") ? draft + " " : draft;

    r.onresult = (event: SpeechRecognitionEvent) => {
      let finalText = "";
      let interim = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        const transcript = result[0]?.transcript ?? "";
        if (result.isFinal) {
          finalText += transcript;
        } else {
          interim += transcript;
        }
      }
      if (finalText) {
        dictationBaseRef.current = dictationBaseRef.current + finalText + " ";
      }
      setDraft(dictationBaseRef.current + interim);
    };

    r.onerror = (e: SpeechRecognitionErrorEvent) => {
      console.warn("[studio] speech recognition error", e.error);
      if (e.error !== "no-speech" && e.error !== "aborted") {
        setError(`voice: ${e.error}`);
      }
      setRecording(false);
    };

    r.onend = () => {
      setRecording(false);
      recognitionRef.current = null;
    };

    try {
      r.start();
      recognitionRef.current = r;
      setRecording(true);
      setError(null);
    } catch (err) {
      setError(`voice: ${(err as Error).message}`);
      setRecording(false);
    }
  }, [draft, recording, stopDictation]);

  // Stop dictation on unmount.
  useEffect(() => () => stopDictation(), [stopDictation]);

  // Allow pasting images directly from the clipboard.
  const onPaste = useCallback(
    (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
      const files: File[] = [];
      for (const item of Array.from(e.clipboardData.items)) {
        if (item.kind === "file") {
          const f = item.getAsFile();
          if (f) files.push(f);
        }
      }
      if (files.length > 0) {
        e.preventDefault();
        void addFiles(files);
      }
    },
    [addFiles],
  );

  // Re-evaluate collapse state when the viewport crosses the breakpoint.
  useEffect(() => {
    const mql = window.matchMedia("(max-width: 767px)");
    const onChange = (e: MediaQueryListEvent) => {
      // Only adjust if the user hasn't manually expanded — when the user
      // is on desktop, never auto-collapse; when crossing into mobile,
      // collapse if currently expanded.
      if (e.matches) setCollapsed(true);
      else setCollapsed(false);
    };
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    saveChatSize(panelSize);
  }, [panelSize]);

  useEffect(() => {
    const clampToViewport = () => {
      const maxWidth = Math.max(CHAT_MIN_WIDTH, window.innerWidth - 32);
      const maxHeight = Math.max(CHAT_MIN_HEIGHT, window.innerHeight - 32);
      const viewport = getViewportSize();
      setViewportSize(viewport);
      setPanelSize((prev) => {
        const next = clampPanelSize(prev, maxWidth, maxHeight);
        if (next.width === prev.width && next.height === prev.height) return prev;
        return next;
      });
      setCompanionPosition((prev) => {
        const next = clampCompanionPosition(prev, viewport);
        if (sameCompanionPosition(prev, next)) return prev;
        saveCompanionPosition(next);
        return next;
      });
    };
    clampToViewport();
    window.addEventListener("resize", clampToViewport);
    return () => window.removeEventListener("resize", clampToViewport);
  }, []);

  // Auto-scroll to the bottom on new content.
  useEffect(() => {
    if (bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [messages]);

  useEffect(() => {
    const settledMessages = messages.map((message) => ({
      ...message,
      streaming: false,
      toolCalls: message.toolCalls.map((tool) => ({
        ...tool,
        state: tool.state === "running" ? "done" : tool.state,
      })),
    }));
    try {
      localStorage.setItem(chatStorageKey(sessionId), JSON.stringify(settledMessages));
    } catch {
      // Best-effort browser history; chat still works without storage.
    }
    const record: HistoryRecord = {
      id: sessionId,
      title: titleForMessages(settledMessages),
      updatedAt: new Date().toISOString(),
      spaceId,
    };
    setHistory((prev) => {
      const next = [record, ...prev.filter((item) => item.id !== sessionId)]
        .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))
        .slice(0, HISTORY_LIMIT);
      writeHistoryIndex(next);
      return next;
    });
  }, [messages, sessionId, spaceId]);

  const send = useCallback(async () => {
    const text = draft.trim();
    if ((!text && attachments.length === 0) || streaming) return;
    setError(null);

    const localDeleteTarget =
      attachments.length === 0 ? resolveLocalDelete(text, widgets) : null;
    if (localDeleteTarget) {
      setDraft("");
      setMessages((prev) => [
        ...prev,
        { role: "user", text, toolCalls: [] },
        {
          role: "assistant",
          text: `Closing ${localDeleteTarget.title || localDeleteTarget.id} now.`,
          toolCalls: [
            {
              id: `local-delete-${localDeleteTarget.id}`,
              name: "studio_widget_delete",
              args: { id: localDeleteTarget.id, fast_path: true },
              state: "running",
            },
          ],
          streaming: true,
        },
      ]);
      try {
        await studioApi.deleteWidget(spaceId, localDeleteTarget.id);
        setMessages((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last?.role !== "assistant") return next;
          next[next.length - 1] = {
            ...last,
            text: `Closed ${localDeleteTarget.title || localDeleteTarget.id}.`,
            streaming: false,
            toolCalls: last.toolCalls.map((tool) => ({
              ...tool,
              state: "done",
              result: { ok: true },
            })),
          };
          return next;
        });
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        setMessages((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last?.role !== "assistant") return next;
          next[next.length - 1] = {
            ...last,
            text: `I could not close ${localDeleteTarget.title || localDeleteTarget.id}: ${message}`,
            streaming: false,
            toolCalls: last.toolCalls.map((tool) => ({
              ...tool,
              state: "error",
              result: { error: message },
            })),
          };
          return next;
        });
      }
      return;
    }

    const shouldAttachAnnotations =
      !!annotationSummary && !(isGratitudeOnly(text) && attachments.length === 0);
    const promptText = shouldAttachAnnotations
      ? `${annotationSummary}\n${text || "(see attachments)"}`
      : text;

    // Build the message — multimodal if there are attachments, plain
    // string otherwise. Text-file attachments are inlined as fenced code
    // blocks at the end of the text part; images go as image_url parts
    // (data URLs — the gateway forwards to the underlying model).
    let outgoing: string | ContentPart[];
    let echoText: string;
    if (attachments.length === 0) {
      outgoing = promptText;
      echoText = text;
    } else {
      const textPart = promptText;
      const fileBlocks: string[] = [];
      const imageParts: ContentPart[] = [];
      const labels: string[] = [];
      for (const a of attachments) {
        if (a.kind === "text") {
          // Pick a fence language hint from the extension if we can.
          const ext = a.name.split(".").pop() || "";
          fileBlocks.push(
            `\n\n[file: ${a.name}]\n\`\`\`${ext}\n${a.dataUrl}\n\`\`\``,
          );
          labels.push(a.name);
        } else {
          imageParts.push({ type: "image_url", image_url: { url: a.dataUrl } });
          labels.push(a.name);
        }
      }
      const fullText = (textPart + fileBlocks.join("")).trim();
      outgoing = [{ type: "text", text: fullText || "(see attachments)" } as ContentPart, ...imageParts];
      echoText =
        text +
        (labels.length
          ? `\n\n📎 ${labels.join(", ")}`
          : "");
    }

    setDraft("");
    setAttachments([]);
    setMessages((prev) => [
      ...prev,
      { role: "user", text: echoText, toolCalls: [] },
      { role: "assistant", text: "", toolCalls: [], streaming: true },
    ]);
    setStreaming(true);

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      for await (const evt of streamChat(sessionId, outgoing, spaceId, ctrl.signal)) {
        setMessages((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (!last || last.role !== "assistant") return next;
          const updated: Message = { ...last, toolCalls: [...last.toolCalls] };

          if (evt.type === "text") {
            updated.text = updated.text + evt.delta;
          } else if (evt.type === "tool_call_start") {
            updated.toolCalls.push({
              id: evt.id,
              name: evt.name,
              args: evt.args,
              state: "running",
            });
          } else if (evt.type === "tool_call_result") {
            const idx = updated.toolCalls.findIndex((t) => t.id === evt.id);
            const result = evt.result as Record<string, unknown> | undefined;
            const isError = !!(result && "error" in result);
            const patch: Partial<ToolCall> = {
              result: evt.result,
              state: isError ? "error" : "done",
            };
            if (idx >= 0) {
              updated.toolCalls[idx] = { ...updated.toolCalls[idx], ...patch };
            } else {
              updated.toolCalls.push({
                id: evt.id,
                name: evt.name,
                ...patch,
                state: patch.state ?? "done",
              } as ToolCall);
            }
          } else if (evt.type === "done") {
            updated.streaming = false;
          } else if (evt.type === "error") {
            updated.streaming = false;
            setError(evt.message);
          }

          next[next.length - 1] = updated;
          return next;
        });
      }
    } catch (err) {
      const e = err as Error;
      if (e.name !== "AbortError") {
        // "Failed to fetch" / "NetworkError" mid-stream usually means the
        // proxy dropped an idle SSE connection. Hint that retrying should
        // pick up where it left off (the gateway keeps history).
        const msg = /network|fetch|failed/i.test(e.message)
          ? `${e.message} — connection dropped mid-stream. Try again; your session history is preserved.`
          : e.message;
        setError(msg);
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
      setMessages((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last && last.streaming) {
          next[next.length - 1] = { ...last, streaming: false };
        }
        return next;
      });
    }
  }, [annotationSummary, attachments, draft, sessionId, spaceId, streaming, widgets]);

  useEffect(() => {
    if (!queuedAnnotation || streaming) return;
    if (handledQueuedAnnotationRef.current === queuedAnnotation.id) return;
    const prompt = buildAnnotationRequest(queuedAnnotation, widgets);
    handledQueuedAnnotationRef.current = queuedAnnotation.id;
    queuedAnnotationPromptRef.current = { id: queuedAnnotation.id, prompt };
    setCollapsed(false);
    setAttachments([]);
    setDraft(prompt);
  }, [queuedAnnotation, streaming, widgets]);

  useEffect(() => {
    const queued = queuedAnnotationPromptRef.current;
    if (!queued || streaming || draft !== queued.prompt || attachments.length > 0) return;
    queuedAnnotationPromptRef.current = null;
    void send().finally(() => {
      onQueuedAnnotationHandled?.(queued.id);
    });
  }, [attachments.length, draft, onQueuedAnnotationHandled, send, streaming]);

  const onKey = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        void send();
      }
    },
    [send],
  );

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const openCompactChat = useCallback(() => {
    setCollapsed(false);
    setExtended(false);
    setHistoryOpen(false);
    setTimeout(() => inputRef.current?.focus(), 180);
  }, []);

  const minimizeCompanion = useCallback(() => {
    setCollapsed(true);
    setExtended(false);
    setHistoryOpen(false);
  }, []);

  const toggleExtended = useCallback(() => {
    if (collapsed) {
      openCompactChat();
      return;
    }
    setExtended((value) => !value);
    setTimeout(() => inputRef.current?.focus(), 120);
  }, [collapsed, openCompactChat]);

  const onHeaderClick = useCallback(() => {
    if (!isMobileViewport()) return;
    toggleExtended();
  }, [toggleExtended]);

  const onCompanionPointerDown = useCallback(
    (e: React.PointerEvent<HTMLButtonElement>) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      e.currentTarget.setPointerCapture(e.pointerId);
      companionDragRef.current = {
        pointerId: e.pointerId,
        startX: e.clientX,
        startY: e.clientY,
        origin: companionPosition,
        last: companionPosition,
        moved: false,
      };
      suppressCompanionClickRef.current = false;
    },
    [companionPosition],
  );

  const onCompanionPointerMove = useCallback(
    (e: React.PointerEvent<HTMLButtonElement>) => {
      const drag = companionDragRef.current;
      if (!drag || drag.pointerId !== e.pointerId) return;
      const dx = e.clientX - drag.startX;
      const dy = e.clientY - drag.startY;
      if (!drag.moved && Math.hypot(dx, dy) < 4) return;
      e.preventDefault();
      const next = clampCompanionPosition(
        {
          x: drag.origin.x + dx,
          y: drag.origin.y - dy,
        },
        viewportSize,
      );
      drag.moved = true;
      drag.last = next;
      suppressCompanionClickRef.current = true;
      setCompanionPosition(next);
    },
    [viewportSize],
  );

  const onCompanionPointerEnd = useCallback((e: React.PointerEvent<HTMLButtonElement>) => {
    const drag = companionDragRef.current;
    if (!drag || drag.pointerId !== e.pointerId) return;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
    companionDragRef.current = null;
    if (drag.moved) saveCompanionPosition(drag.last);
    window.setTimeout(() => {
      suppressCompanionClickRef.current = false;
    }, 0);
  }, []);

  const onPetClick = useCallback(() => {
    if (suppressCompanionClickRef.current) return;
    if (collapsed) {
      openCompactChat();
      return;
    }
    inputRef.current?.focus();
  }, [collapsed, openCompactChat]);

  const beginResize = useCallback(
    (start: PointerStart) => {
      const panel = panelRef.current;
      if (!panel) return null;
      const rect = panel.getBoundingClientRect();
      const startSize = clampPanelSize({ width: rect.width, height: rect.height });

      const onMove = (clientX: number, clientY: number) => {
        const maxWidth = Math.max(CHAT_MIN_WIDTH, window.innerWidth - 32);
        const maxHeight = Math.max(CHAT_MIN_HEIGHT, window.innerHeight - 32);
        const next = clampPanelSize(
          {
            width: startSize.width + (clientX - start.x),
            height: startSize.height + (clientY - start.y),
          },
          maxWidth,
          maxHeight,
        );
        panel.style.setProperty("--studio-chat-width", `${next.width}px`);
        panel.style.setProperty("--studio-chat-height", `${next.height}px`);
      };

      const onEnd = (clientX: number, clientY: number) => {
        const maxWidth = Math.max(CHAT_MIN_WIDTH, window.innerWidth - 32);
        const maxHeight = Math.max(CHAT_MIN_HEIGHT, window.innerHeight - 32);
        const next = clampPanelSize(
          {
            width: startSize.width + (clientX - start.x),
            height: startSize.height + (clientY - start.y),
          },
          maxWidth,
          maxHeight,
        );
        panel.style.setProperty("--studio-chat-width", `${next.width}px`);
        panel.style.setProperty("--studio-chat-height", `${next.height}px`);
        setPanelSize(next);
        saveChatSize(next);
      };

      return { onMove, onEnd };
    },
    [],
  );

  const onResizeMouseDown = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
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

  const onResizeTouchStart = useCallback(
    (e: React.TouchEvent<HTMLDivElement>) => {
      if (e.touches.length !== 1) return;
      e.stopPropagation();
      const touch = e.touches[0];
      const handlers = beginResize({ x: touch.clientX, y: touch.clientY });
      if (!handlers) return;
      const onMove = (tev: TouchEvent) => {
        if (tev.touches.length === 0) return;
        tev.preventDefault();
        const nextTouch = tev.touches[0];
        handlers.onMove(nextTouch.clientX, nextTouch.clientY);
      };
      const onEnd = (tev: TouchEvent) => {
        document.removeEventListener("touchmove", onMove);
        document.removeEventListener("touchend", onEnd);
        document.removeEventListener("touchcancel", onEnd);
        const finalTouch =
          tev.changedTouches.length > 0
            ? tev.changedTouches[0]
            : tev.touches.length > 0
              ? tev.touches[0]
              : null;
        if (!finalTouch) return;
        handlers.onEnd(finalTouch.clientX, finalTouch.clientY);
      };
      document.addEventListener("touchmove", onMove, { passive: false });
      document.addEventListener("touchend", onEnd);
      document.addEventListener("touchcancel", onEnd);
    },
    [beginResize],
  );

  const loadConversation = useCallback((id: string) => {
    sessionStorage.setItem(SESSION_KEY, id);
    setSessionId(id);
    setMessages(loadStoredMessages(id));
    setHistoryOpen(false);
    setError(null);
  }, []);

  const startNewConversation = useCallback(() => {
    abortRef.current?.abort();
    const nextId = newSessionId();
    sessionStorage.setItem(SESSION_KEY, nextId);
    setSessionId(nextId);
    setMessages([WELCOME_MESSAGE]);
    setDraft("");
    setAttachments([]);
    setHistoryOpen(false);
    setError(null);
  }, []);

  const deleteConversation = useCallback((id: string) => {
    try {
      localStorage.removeItem(chatStorageKey(id));
    } catch {
      // ignore
    }
    setHistory((prev) => {
      const next = prev.filter((item) => item.id !== id);
      writeHistoryIndex(next);
      return next;
    });
    if (id === sessionId) startNewConversation();
  }, [sessionId, startNewConversation]);

  return (
    <>
      <div
        className={`studio-agent-pet-shell ${collapsed ? "is-glance" : "is-chat"} ${
          extended ? "is-extended" : "is-compact"
        }`}
        style={companionStyle}
      >
        {collapsed && (
          <button
            type="button"
            className="studio-agent-glance"
            aria-label="Open Hermes chat"
            onClick={openCompactChat}
          >
            <span className="studio-agent-glance-title">
              {glanceTitle}
              {streaming && <span className="studio-agent-glance-spinner" aria-hidden />}
            </span>
            <span className="studio-agent-glance-text">{glanceText}</span>
          </button>
        )}
        <div className="studio-agent-pet-row">
          <button
            type="button"
            className="studio-agent-pet"
            aria-label={collapsed ? "Open Hermes chat" : "Focus Hermes chat"}
            title={collapsed ? "Open Hermes chat" : "Focus Hermes chat"}
            draggable={false}
            onPointerDown={onCompanionPointerDown}
            onPointerMove={onCompanionPointerMove}
            onPointerUp={onCompanionPointerEnd}
            onPointerCancel={onCompanionPointerEnd}
            onClick={onPetClick}
          >
            <span className="studio-agent-pet-sprite" aria-hidden />
          </button>
          <button
            type="button"
            className="studio-agent-expand"
            aria-label={
              collapsed ? "Open Hermes chat" : extended ? "Compact Hermes chat" : "Extend Hermes chat"
            }
            title={collapsed ? "Open Hermes chat" : extended ? "Compact Hermes chat" : "Extend Hermes chat"}
            onClick={toggleExtended}
          >
            {extended ? (
              <ChevronUp aria-hidden size={18} strokeWidth={2.2} />
            ) : (
              <ChevronDown aria-hidden size={18} strokeWidth={2.2} />
            )}
          </button>
        </div>
        </div>
      <div
        ref={panelRef}
        className={`studio-chat studio-glass-strong ${collapsed ? "is-collapsed" : ""} ${
          extended ? "is-extended" : "is-compact"
        }`}
        style={{
          "--studio-chat-left": `${chatPlacement.left}px`,
          "--studio-chat-bottom": `${chatPlacement.bottom}px`,
          "--studio-chat-width": `${chatPlacement.width}px`,
          "--studio-chat-height": `${chatPlacement.height}px`,
        } as ChatCssVars}
      >
        <div className="studio-chat-header" onClick={onHeaderClick}>
          <span
            className="studio-chat-status-dot"
            aria-hidden
            style={{
              animationPlayState: streaming ? "running" : "paused",
              opacity: streaming ? 1 : 0.5,
            }}
          />
          <span style={{ flex: 1 }}>Hermes</span>
          {streaming && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                stop();
              }}
              style={{
                background: "transparent",
                border: "none",
                color: "var(--studio-signal)",
                cursor: "pointer",
                fontSize: "0.65rem",
                letterSpacing: "0.1em",
                textTransform: "uppercase",
              }}
            >
              Stop
            </button>
          )}
          <button
            type="button"
            className={`studio-chat-history-button ${historyOpen ? "is-active" : ""}`}
            aria-label="Conversation history"
            onClick={(e) => {
              e.stopPropagation();
              setHistoryOpen((open) => !open);
            }}
          >
            History
          </button>
          <button
            type="button"
            className="studio-chat-toggle"
            aria-label="Minimize Hermes chat"
            title="Minimize Hermes chat"
            onClick={(e) => {
              e.stopPropagation();
              minimizeCompanion();
            }}
          >
            <X aria-hidden size={16} strokeWidth={2.2} />
          </button>
        </div>

        {historyOpen && !collapsed && (
          <div className="studio-chat-history">
            <div className="studio-chat-history-top">
              <span>Conversation History</span>
              <button type="button" onClick={startNewConversation}>
                New
              </button>
            </div>
            <div className="studio-chat-history-list">
              {history.length === 0 ? (
                <div className="studio-chat-history-empty">No saved chats yet.</div>
              ) : (
                history.map((item) => (
                  <article
                    key={item.id}
                    className={`studio-chat-history-item ${item.id === sessionId ? "is-current" : ""}`}
                  >
                    <button type="button" onClick={() => loadConversation(item.id)}>
                      <span>{item.title}</span>
                      <small>
                        {new Date(item.updatedAt).toLocaleString([], {
                          month: "short",
                          day: "numeric",
                          hour: "numeric",
                          minute: "2-digit",
                        })}
                      </small>
                    </button>
                    <button
                      type="button"
                      className="studio-chat-history-delete"
                      aria-label={`Delete ${item.title}`}
                      onClick={() => deleteConversation(item.id)}
                    >
                      x
                    </button>
                  </article>
                ))
              )}
            </div>
          </div>
        )}

      <div ref={bodyRef} className="studio-chat-body">
        {messages.map((m, i) => (
          <ChatBubble key={i} message={m} />
        ))}
        {error && (
          <div
            style={{
              marginTop: 10,
              fontSize: 0.75 + "rem",
              color: "var(--studio-signal)",
              textShadow: "var(--studio-signal-glow-soft)",
              fontFamily: "ui-monospace, monospace",
            }}
          >
            {error}
          </div>
        )}
      </div>

        {attachments.length > 0 && (
          <div className="studio-chat-attachments">
            {attachments.map((a) => (
              <span key={a.id} className="studio-chat-attachment" title={a.name}>
                {a.kind === "image" ? (
                  <img
                    className="studio-chat-attachment-thumb"
                    src={a.dataUrl}
                    alt=""
                  />
                ) : (
                  <span style={{ fontSize: "0.85rem" }}>📄</span>
                )}
                <span className="studio-chat-attachment-name">{a.name}</span>
                <button
                  type="button"
                  className="studio-chat-attachment-remove"
                  aria-label={`Remove ${a.name}`}
                  onClick={() => removeAttachment(a.id)}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        )}

        <div className="studio-chat-input">
          <div className="studio-chat-input-row">
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*,text/*,.md,.json,.yaml,.yml,.csv,.tsv,.log,.py,.js,.ts,.tsx,.jsx,.html,.css,.sh"
              multiple
              style={{ display: "none" }}
              onChange={(e) => {
                if (e.target.files) void addFiles(e.target.files);
                e.target.value = ""; // allow re-picking the same file
              }}
            />
            <button
              type="button"
              className="studio-chat-attach-btn"
              aria-label="Attach file or image"
              title="Attach file or image"
              onClick={() => fileInputRef.current?.click()}
              disabled={streaming}
            >
              <Paperclip aria-hidden size={18} strokeWidth={1.9} />
            </button>
            {speechSupported && (
              <button
                type="button"
                className="studio-chat-attach-btn"
                aria-label={recording ? "Stop dictation" : "Voice dictation"}
                title={recording ? "Stop dictation" : "Voice dictation"}
                onClick={startDictation}
                disabled={streaming}
                style={
                  recording
                    ? {
                        color: "var(--studio-signal)",
                        borderColor: "rgba(255,32,32,0.5)",
                        boxShadow: "var(--studio-signal-glow-soft)",
                      }
                    : undefined
                }
              >
                <Mic aria-hidden size={17} strokeWidth={recording ? 2.4 : 1.9} />
              </button>
            )}
            <textarea
              ref={inputRef}
              className="studio-chat-input-field"
              placeholder={
                streaming
                  ? "Hermes is working…"
                  : recording
                    ? "Listening…"
                    : "Ask Hermes — paste images or attach files"
              }
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={onKey}
              onPaste={onPaste}
              disabled={streaming}
              rows={extended ? 4 : 2}
              style={{ resize: "none", flex: 1 }}
            />
          </div>
        </div>
        <div
          className="studio-chat-resize"
          aria-label="Resize chat"
          onMouseDown={onResizeMouseDown}
          onTouchStart={onResizeTouchStart}
        />
      </div>

      {/* Mobile-only floating button — surfaced via CSS when chat is collapsed. */}
      <button
        type="button"
        className="studio-chat-fab"
        aria-label="Open chat"
        onClick={openCompactChat}
      >
        <span style={{ fontSize: "1.2rem", lineHeight: 1 }}>💬</span>
      </button>
    </>
  );
}

function ChatBubble({ message }: { message: Message }) {
  const isUser = message.role === "user";
  return (
    <div
      style={{
        marginTop: 12,
        display: "flex",
        flexDirection: "column",
        alignItems: isUser ? "flex-end" : "flex-start",
      }}
    >
      <div
        style={{
          maxWidth: "85%",
          padding: "8px 12px",
          border: "1px solid rgba(17, 24, 39, 0.08)",
          borderRadius: 14,
          background: isUser ? "rgba(58, 126, 255, 0.14)" : "rgba(255,255,255,0.58)",
          boxShadow: "inset 0 1px 0 rgba(255,255,255,0.6)",
          color: "var(--studio-text)",
          fontSize: "0.85rem",
          lineHeight: 1.4,
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
        }}
      >
        {message.text || (message.streaming ? <em style={{ opacity: 0.5 }}>…</em> : null)}
      </div>
      {message.toolCalls.map((tc) => (
        <ToolCallChip key={tc.id} tc={tc} />
      ))}
    </div>
  );
}

function ToolCallChip({ tc }: { tc: ToolCall }) {
  const color =
    tc.state === "error"
      ? "var(--studio-signal)"
      : tc.state === "running"
        ? "var(--studio-signal)"
        : "var(--studio-text-fade)";
  const glow =
    tc.state === "running" ? "var(--studio-signal-glow-soft)" : undefined;
  const label = (() => {
    const args = (tc.args ?? {}) as Record<string, unknown>;
    if (tc.name === "create_widget" || tc.name === "update_widget") {
      const id = args["id"] ?? "?";
      return `${tc.name} ${id}`;
    }
    if (tc.name === "delete_widget") return `delete ${args["id"] ?? "?"}`;
    if (tc.name === "read_widget") return `read ${args["id"] ?? "?"}`;
    return tc.name;
  })();
  return (
    <div
      style={{
        marginTop: 6,
        fontSize: "0.65rem",
        letterSpacing: "0.08em",
        textTransform: "uppercase",
        color,
        textShadow: glow,
        fontFamily: "ui-monospace, monospace",
        opacity: tc.state === "running" ? 1 : 0.7,
      }}
    >
      {tc.state === "running" ? "▸ " : tc.state === "error" ? "✕ " : "✓ "}
      {label}
    </div>
  );
}
