// Thin bridge to the Tauri shell. Uses the global `window.__TAURI__` (withGlobalTauri: true),
// so the UI also runs in a normal browser for development: pass ?url=...&token=... there.
import type { ServiceInfo, UiSettings } from "./types";

type TauriGlobal = {
  core: { invoke: <T>(cmd: string, args?: Record<string, unknown>) => Promise<T> };
  event: { listen: (name: string, cb: (e: { payload: unknown }) => void) => Promise<() => void> };
};

declare global {
  interface Window {
    __TAURI__?: TauriGlobal;
  }
}

const tauri = () => window.__TAURI__;
export const inTauri = () => !!tauri();

export async function ensureService(): Promise<ServiceInfo> {
  const t = tauri();
  if (t) return t.core.invoke<ServiceInfo>("ensure_service");
  const p = new URLSearchParams(location.search);
  const url = p.get("url"), token = p.get("token");
  if (!url || !token) throw new Error("Open with ?url=http://127.0.0.1:8766&token=… (see engine/data/service.json)");
  return { url, token };
}

export async function getSettings(): Promise<UiSettings> {
  const t = tauri();
  if (t) return t.core.invoke<UiSettings>("get_settings");
  const glass = new URLSearchParams(location.search).get("glass") === "1";
  return { glass, hotkey: "Alt+Space" };
}

export function hideWindow(): void {
  tauri()?.core.invoke("hide_window").catch(() => {});
}

export function setWindowHeight(height: number): void {
  tauri()?.core.invoke("set_height", { height }).catch(() => {});
}

/** Called every time the hotkey shows the window. */
export function onShown(cb: () => void): () => void {
  const t = tauri();
  if (!t) return () => {};
  let un: (() => void) | undefined;
  t.event.listen("grain://shown", () => cb()).then((f) => (un = f));
  return () => un?.();
}

export async function copyText(text: string): Promise<void> {
  await navigator.clipboard.writeText(text);
}
