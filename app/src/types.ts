export interface ServiceInfo {
  url: string;
  token: string;
}

/** First-run setup of the installed app (setup.rs): progress shown in the setup window. */
export interface SetupStep {
  id: "gpu" | "disk" | "python" | "libraries" | "pytorch" | "engine";
  label: string;
  state: "pending" | "running" | "done" | "failed" | "skipped";
}

export interface SetupStatus {
  running: boolean;
  done: boolean;
  steps: SetupStep[];
  gpu: string | null;
  build: "cuda" | "cpu" | null;
  downloaded_mb: number;
  error: string | null;
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

/** Whether the AI model is in (GPU) memory; tray -> Unload model frees it; "missing" = not downloaded. */
export type ModelState = "loaded" | "loading" | "unloaded" | "missing";

/** GET /model: the model's files on disk and the download. */
export interface ModelFiles {
  state: "missing" | "downloading" | "ready";
  /** folder = downloaded by Grain Search; cache = already in the Hugging Face cache */
  source: "folder" | "cache" | "missing";
  path: string | null;
  folder: string;
  models_folder: string;
  model: string;
  error: string;
  total_bytes?: number | null;
  done_bytes?: number;
  model_state?: ModelState;
}

/** Where the AI model runs (empty while it isn't loaded). */
export interface DeviceInfo {
  kind?: "gpu" | "cpu";
  name?: string;
  memory_mb?: number;
  /** on the CPU: why (no NVIDIA GPU, CPU-only PyTorch, set to CPU) */
  reason?: string;
}

export interface Status {
  device: string;
  device_info?: DeviceInfo;
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

/** GET /config/suggestions: a standard folder (Documents, Pictures…) not added yet. */
export interface FolderSuggestion {
  key: "documents" | "pictures" | "desktop" | "downloads";
  label: string;
  path: string;
  /** files Grain Search would index there; null until the first count finishes */
  files: number | null;
  bytes: number | null;
  counting: boolean;
}

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
