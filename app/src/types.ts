export interface ServiceInfo {
  url: string;
  token: string;
}

export interface UiSettings {
  glass: boolean; // Windows acrylic blur (square-ish corners) instead of macOS-style rounded bar
  hotkey: string;
  hotkey_error?: string | null;
}

export interface DocHit {
  path: string;
  name: string;
  folder: string;
  ext: string;
  snippet: string;
  page: number | null;
  line: number | null;
  match: "exact" | "words" | "name" | "meaning";
  contains_text: boolean;
  similarity: number | null;
}

export interface ImageHit {
  path: string;
  name: string;
  folder: string;
  score: number;
  meta: Record<string, unknown>;
}

export interface SearchPayload {
  stage: "fast" | "full";
  query: string;
  docs: DocHit[];
  images: ImageHit[];
  top: string | null;
  ms: number;
  skipped?: boolean;
}

export interface ExcerptLine {
  n: number | null;
  text: string;
  highlights: [number, number][];
  match: boolean;
}

export interface Preview {
  path: string;
  name: string;
  folder: string;
  ext: string;
  kind: string;
  size: number;
  modified: string;
  meta: Record<string, unknown>;
  excerpt: { lines: ExcerptLine[]; page: number | null } | null;
  error?: string;
}

/** Whether the AI model is in (GPU) memory; tray -> Unload model frees it. */
export type ModelState = "loaded" | "loading" | "unloaded";

export interface Status {
  device: string;
  model_state?: ModelState;
  /** why the last attempt to load the model failed, if it did */
  model_error?: string;
  /** true when no folders are set up yet (nothing to index or search) */
  setup_needed?: boolean;
  folders?: string[];
  index: { files: number; text_vectors: number; image_vectors: number };
  indexer: null | {
    phase: string;
    done: number;
    total: number;
    paused_reason: string;
    manual_pause: boolean;
    last_error: string;
  };
}

/** One selectable row in the result list. */
export type Item =
  | { kind: "doc"; section: Section; hit: DocHit }
  | { kind: "image"; section: Section; hit: ImageHit };

export type Section = "top" | "docs" | "related" | "images";

/** GET /config: what is indexed and what is skipped. */
export interface ConfigView {
  config_file: string | null;
  index_dir: string;
  setup_needed: boolean;
  folders: { path: string; exists: boolean; indexed: number; pending: number; errors: number; total: number }[];
  excludes: { pattern: string; builtin: boolean }[];
}

/** Reply to a folder/exclusion change: the result plus the new settings. */
export interface ConfigChange extends ConfigView {
  ok: boolean;
  changed: boolean;
  message: string;
  value: string;
}
