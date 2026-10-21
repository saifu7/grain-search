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
  if (!url || !token) throw new Error("Open with ?url=…&token=… copied from service.json in %LOCALAPPDATA%\\GrainSearch");
  return { url, token };
}

export async function getSettings(): Promise<UiSettings> {
  const t = tauri();
  if (t) return t.core.invoke<UiSettings>("get_settings");
  const glass = new URLSearchParams(location.search).get("glass") === "1";
  return { glass, hotkey: "Alt+Space" };
}

/** Change the global shortcut live. Resolves to the shortcut now in use; rejects with the reason
 *  (e.g. another program already uses it), in which case the old shortcut keeps working. */
export async function setHotkey(hotkey: string): Promise<string> {
  const t = tauri();
  if (!t) throw new Error("Only the Grain Search app can change the shortcut");
  return t.core.invoke<string>("set_hotkey", { hotkey });
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

/** Opens the Settings window (in a browser: the settings page in this tab). */
export function openSettings(): void {
  const t = tauri();
  if (t) {
    t.core.invoke("open_settings").catch(() => {});
    return;
  }
  location.hash = "#settings";
}

/** The native folder picker. Resolves to null when cancelled, or when there is no picker (browser). */
export async function pickFolder(title?: string): Promise<string | null> {
  const t = tauri();
  if (!t) return null;
  return t.core.invoke<string | null>("pick_folder", { title });
}
