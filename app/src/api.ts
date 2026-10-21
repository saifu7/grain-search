// Talks to `grain serve` (127.0.0.1 only, bearer token).
import type {
  ConfigChange,
  ConfigView,
  FolderSuggestion,
  ModelState,
  Preview,
  SearchPayload,
  ServiceInfo,
  Status,
} from "./types";

const enc = encodeURIComponent;

/** Two-stage search over Server-Sent Events. Returns a function that cancels it. */
export function search(
  svc: ServiceInfo,
  q: string,
  limit: number,
  onStage: (p: SearchPayload) => void,
  onError: (msg: string) => void,
): () => void {
  // EventSource can't send headers, so the token goes in the query string (the service allows that)
  const es = new EventSource(`${svc.url}/search?q=${enc(q)}&limit=${limit}&token=${enc(svc.token)}`);
  let finished = false;
  const close = () => {
    finished = true;
    es.close();
  };
  const handle = (e: MessageEvent) => {
    try {
      onStage(JSON.parse(e.data));
    } catch {
      /* ignore malformed */
    }
  };
  es.addEventListener("fast", handle as EventListener);
  es.addEventListener("full", handle as EventListener);
  es.addEventListener("done", () => close());
  es.addEventListener("error", (e) => {
    const data = (e as MessageEvent).data;
    if (typeof data === "string" && data) {
      try {
        onError(JSON.parse(data).error ?? "search failed");
      } catch {
        onError("search failed");
      }
    } else if (!finished) {
      onError("Search service is not reachable");
    }
    close();
  });
  return close;
}

async function call<T>(svc: ServiceInfo, path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(svc.url + path, {
    ...init,
    headers: { Authorization: `Bearer ${svc.token}`, "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!r.ok) {
    const text = await r.text();
    let msg = text.slice(0, 200);
    try {
      msg = JSON.parse(text).error ?? msg; // the service explains what was wrong
    } catch {
      /* not JSON */
    }
    throw new Error(msg || `HTTP ${r.status}`);
  }
  return r.json() as Promise<T>;
}

export const preview = (svc: ServiceInfo, path: string, q: string, page?: number | null, line?: number | null) =>
  call<Preview>(
    svc,
    `/preview?path=${enc(path)}&q=${enc(q)}${page ? `&page=${page}` : ""}${line ? `&line=${line}` : ""}`,
  );

export const status = (svc: ServiceInfo) => call<Status>(svc, "/status");

export const openFile = (
  svc: ServiceInfo,
  path: string,
  mode: "open" | "reveal",
  query: string,
  page?: number | null,
  line?: number | null,
) => call<{ ok: boolean }>(svc, "/open", { method: "POST", body: JSON.stringify({ path, mode, query, page, line }) });

export const thumbUrl = (svc: ServiceInfo, path: string, size: number) =>
  `${svc.url}/thumb?path=${enc(path)}&size=${size}&token=${enc(svc.token)}`;

export const pageUrl = (svc: ServiceInfo, path: string, page: number, width: number) =>
  `${svc.url}/page?path=${enc(path)}&page=${page}&width=${width}&token=${enc(svc.token)}`;

// -- settings: folders and exclusions (applied live by the service) ---------------------
export const getConfig = (svc: ServiceInfo) => call<ConfigView>(svc, "/config");

export const getSuggestions = (svc: ServiceInfo) =>
  call<{ suggestions: FolderSuggestion[] }>(svc, "/config/suggestions");

export const changeFolder = (svc: ServiceInfo, action: "add" | "remove", path: string) =>
  call<ConfigChange>(svc, "/config/folders", { method: "POST", body: JSON.stringify({ action, path }) });

export const changeExclude = (svc: ServiceInfo, action: "add" | "remove", value: string) =>
  call<ConfigChange>(svc, "/config/excludes", { method: "POST", body: JSON.stringify({ action, value }) });

/** Bring the model back after tray -> Unload model (loads in the background; returns at once). */
export const loadModel = (svc: ServiceInfo) =>
  call<{ ok: boolean; model_state: ModelState }>(svc, "/model/load", { method: "POST" });

export const indexControl = (svc: ServiceInfo, action: "pause" | "resume" | "rescan") =>
  call<{ ok: boolean }>(svc, `/index/${action}`, { method: "POST" });
