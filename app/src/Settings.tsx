// The Settings window: which folders are searched, what is skipped, and indexing status.
// Every change is sent to the search service, which saves config.json and applies it live.
import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "./api";
import { ensureService, getSettings, inTauri, pickFolder, setHotkey } from "./platform";
import type { ConfigChange, ConfigView, FolderSuggestion, ServiceInfo, Status } from "./types";

type Note = { kind: "ok" | "error"; text: string } | null;

export default function Settings() {
  const [svc, setSvc] = useState<ServiceInfo | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  const [config, setConfig] = useState<ConfigView | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [note, setNote] = useState<Note>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [suggestions, setSuggestions] = useState<FolderSuggestion[]>([]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const noteTimer = useRef<number | undefined>(undefined);

  useEffect(() => {
    document.title = "Grain Search Settings";
    ensureService()
      .then(setSvc)
      .catch((e) => setBootError(String(e?.message ?? e)));
  }, []);

  const refresh = useCallback(() => {
    if (!svc) return;
    api.getConfig(svc).then(setConfig).catch(() => {});
    api.status(svc).then(setStatus).catch(() => {});
    api
      .getSuggestions(svc)
      .then((r) => setSuggestions(r.suggestions))
      .catch(() => {});
  }, [svc]);

  // fast refresh while indexing (counts move), slow when idle
  const indexing = status?.indexer?.phase === "indexing" || status?.indexer?.phase === "scanning";
  const counting = suggestions.some((s) => s.counting);
  useEffect(() => {
    if (!svc) return;
    refresh();
    const t = window.setInterval(refresh, indexing || counting ? 1500 : 4000);
    return () => window.clearInterval(t);
  }, [svc, refresh, indexing, counting]);

  const show = (n: Note) => {
    setNote(n);
    window.clearTimeout(noteTimer.current);
    if (n?.kind === "ok") noteTimer.current = window.setTimeout(() => setNote(null), 4000);
  };

  async function run(key: string, fn: () => Promise<unknown>) {
    setBusy(key);
    try {
      const res = (await fn()) as { message?: string } & Partial<ConfigView>;
      if (res.folders) setConfig(res as ConfigView);
      if (res.message) show({ kind: "ok", text: res.message });
      refresh();
    } catch (e) {
      show({ kind: "error", text: String((e as Error)?.message ?? e) });
    } finally {
      setBusy(null);
    }
  }

  const addFolder = (path: string) => svc && run("add-folder", () => api.changeFolder(svc, "add", path));
  const removeFolder = (path: string) => svc && run(`rm:${path}`, () => api.changeFolder(svc, "remove", path));
  const addExclude = (value: string) => svc && run("add-exclude", () => api.changeExclude(svc, "add", value));
  const removeExclude = (value: string) => svc && run(`rx:${value}`, () => api.changeExclude(svc, "remove", value));

  const togglePicked = (path: string) =>
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  // add the ticked suggestions one by one, exactly like the Add button does
  const addSuggested = () => {
    const paths = suggestions.filter((s) => picked.has(s.path)).map((s) => s.path);
    if (!svc || paths.length === 0) return;
    run("add-suggested", async () => {
      let last: ConfigChange | undefined;
      for (const p of paths) last = await api.changeFolder(svc, "add", p);
      setPicked(new Set());
      return { ...last, message: `Added ${plural(paths.length, "folder")}` };
    });
  };

  async function browse(kind: "folder" | "exclude") {
    const chosen = await pickFolder(kind === "folder" ? "Choose a folder to search" : "Choose a folder to skip");
    if (chosen) (kind === "folder" ? addFolder : addExclude)(chosen);
  }

  if (bootError)
    return (
      <Page>
        <div className="card">
          <h2>Search service isn't running</h2>
          <p className="muted">{bootError}</p>
        </div>
      </Page>
    );
  if (!svc || !config) return <Page><p className="muted pad">Loading settings…</p></Page>;

  const ix = status?.indexer;
  return (
    <Page>
      <header className="s-header">
        <div>
          <h1>Settings</h1>
          <p className="muted">Choose what Grain Search looks through. Everything stays on this PC.</p>
        </div>
      </header>

      <div className={"note" + (note ? ` ${note.kind}` : "")} role="status" aria-live="polite">
        {note?.text ?? ""}
      </div>

      <IndexingCard status={status} busy={busy} onAction={(a) => run(a, () => api.indexControl(svc, a))} />

      <section className="card" aria-labelledby="folders-h">
        <div className="card-head">
          <h2 id="folders-h">Folders to search</h2>
          {inTauri() && (
            <button className="btn primary" disabled={busy === "add-folder"} onClick={() => browse("folder")}>
              Add folder…
            </button>
          )}
        </div>
        {suggestions.length > 0 && (
          <SuggestedFolders
            items={suggestions}
            picked={picked}
            busy={busy === "add-suggested"}
            onToggle={togglePicked}
            onAdd={addSuggested}
          />
        )}
        {config.folders.length === 0 ? (
          <p className="empty-state">No folders yet. Add the folders you want to search, such as Documents or Pictures.</p>
        ) : (
          <ul className="rows">
            {config.folders.map((f) => (
              <li key={f.path} className="row">
                <FolderIcon />
                <div className="row-main">
                  <div className="row-title" title={f.path}>{baseName(f.path)}</div>
                  <div className="row-sub" title={f.path}>{f.path}</div>
                </div>
                <div className={"row-meta" + (f.exists ? "" : " warn")}>{folderMeta(f, ix?.phase)}</div>
                <button className="btn ghost" disabled={busy === `rm:${f.path}`} onClick={() => removeFolder(f.path)}>
                  Remove
                </button>
              </li>
            ))}
          </ul>
        )}
        <PathInput
          label="Add a folder by path"
          placeholder={inTauri() ? "…or paste a folder path, e.g. D:\\Photos" : "Folder path, e.g. D:\\Photos"}
          button="Add"
          busy={busy === "add-folder"}
          onSubmit={addFolder}
        />
      </section>

      {inTauri() && <HotkeyCard />}

      <section className="card" aria-labelledby="skip-h">
        <div className="card-head">
          <h2 id="skip-h">Skip these</h2>
          {inTauri() && (
            <button className="btn" disabled={busy === "add-exclude"} onClick={() => browse("exclude")}>
              Exclude folder…
            </button>
          )}
        </div>
        <p className="muted small">
          A folder you add above is always searched, even if a built-in rule covers it. Rules still apply to
          what's inside it.
        </p>
        <ExcludeList excludes={config.excludes} busy={busy} onRemove={removeExclude} />
        <PathInput
          label="Add a rule"
          placeholder="A folder path, or a pattern like *.log or *\build\*"
          button="Add rule"
          busy={busy === "add-exclude"}
          onSubmit={addExclude}
        />
      </section>

      <footer className="s-footer muted small">
        Settings file: <span className="selectable">{config.config_file}</span>
        <br />
        Index: <span className="selectable">{config.index_dir}</span>
      </footer>
    </Page>
  );
}

// ======================================================================================
function ExcludeList({
  excludes,
  busy,
  onRemove,
}: {
  excludes: ConfigView["excludes"];
  busy: string | null;
  onRemove: (pattern: string) => void;
}) {
  const [showBuiltin, setShowBuiltin] = useState(false);
  const mine = excludes.filter((e) => !e.builtin);
  const builtin = excludes.filter((e) => e.builtin);
  const row = (e: ConfigView["excludes"][number]) => (
    <li key={e.pattern} className="row">
      <code className="row-main pattern" title={e.pattern}>{e.pattern}</code>
      {e.builtin && <span className="tag">Built-in</span>}
      <button className="btn ghost" disabled={busy === `rx:${e.pattern}`} onClick={() => onRemove(e.pattern)}>
        Remove
      </button>
    </li>
  );
  return (
    <div>
      <ul className="rows compact">
        {mine.length === 0 && <li className="empty-state">No rules of your own yet.</li>}
        {mine.map(row)}
      </ul>
      {builtin.length > 0 && (
        <>
          <button className="builtin-toggle" aria-expanded={showBuiltin} onClick={() => setShowBuiltin((v) => !v)}>
            <span className={"chev" + (showBuiltin ? " open" : "")} aria-hidden="true">›</span>
            Built-in rules ({builtin.length}): system folders, app data, code dependencies
            <span style={{ marginLeft: "auto" }}>{showBuiltin ? "Hide" : "Show"}</span>
          </button>
          {showBuiltin && <ul className="rows compact">{builtin.map(row)}</ul>}
        </>
      )}
    </div>
  );
}

function SuggestedFolders({
  items,
  picked,
  busy,
  onToggle,
  onAdd,
}: {
  items: FolderSuggestion[];
  picked: Set<string>;
  busy: boolean;
  onToggle: (path: string) => void;
  onAdd: () => void;
}) {
  const chosen = items.filter((s) => picked.has(s.path)).length;
  return (
    <div className="suggest" aria-labelledby="suggest-h">
      <div className="suggest-head">
        <span id="suggest-h" className="suggest-title">Suggested folders</span>
        <button className="btn primary" disabled={busy || chosen === 0} onClick={onAdd}>
          {chosen > 1 ? `Add ${chosen} folders` : "Add selected"}
        </button>
      </div>
      <ul className="rows compact">
        {items.map((s) => (
          <li key={s.path} className="row">
            <label className="suggest-row">
              <input type="checkbox" checked={picked.has(s.path)} onChange={() => onToggle(s.path)} />
              <FolderIcon />
              <span className="row-main">
                <span className="row-title">{s.label}</span>
                <span className="row-sub" title={s.path}>{s.path}</span>
              </span>
              <span className="row-meta">
                {s.files === null || s.bytes === null
                  ? "Counting…"
                  : `${plural(s.files, "file")} · ${formatBytes(s.bytes)}`}
              </span>
            </label>
          </li>
        ))}
      </ul>
    </div>
  );
}

const DEFAULT_HOTKEY = "Alt+Space";

/** KeyboardEvent.code -> the key name the app accepts (letters, digits, Space, F1-F12). */
function keyName(code: string): string | null {
  if (/^Key[A-Z]$/.test(code)) return code.slice(3);
  if (/^Digit[0-9]$/.test(code)) return code.slice(5);
  if (code === "Space") return "Space";
  if (/^F([1-9]|1[0-2])$/.test(code)) return code;
  return null;
}

function Keys({ combo }: { combo: string }) {
  return (
    <span className="keycaps">
      {combo.split("+").map((k, i) => (
        <kbd key={i}>{k}</kbd>
      ))}
    </span>
  );
}

/** Settings -> Keyboard shortcut: shows the shortcut, records a new one, applies it at once.
 *  If another program already owns it, the old shortcut keeps working and the box turns red. */
function HotkeyCard() {
  const [hotkey, setHk] = useState<string | null>(null);
  const [attempt, setAttempt] = useState<string | null>(null); // the shortcut that didn't work
  const [problem, setProblem] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [recording, setRecording] = useState(false);
  const [held, setHeld] = useState(""); // modifiers held so far while recording
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getSettings()
      .then((s) => {
        setHk(s.hotkey);
        if (s.hotkey_error) setProblem(s.hotkey_error); // couldn't get it when the app started
      })
      .catch(() => {});
  }, []);

  const apply = useCallback(async (combo: string) => {
    setBusy(true);
    setAttempt(combo);
    setDone(null);
    try {
      const now = await setHotkey(combo);
      setHk(now);
      setAttempt(null);
      setProblem(null);
      setDone(`Shortcut changed to ${now}.`);
    } catch (e) {
      setProblem(String((e as Error)?.message ?? e));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    if (!recording) return;
    const down = (e: KeyboardEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const mods = [e.ctrlKey && "Ctrl", e.altKey && "Alt", e.shiftKey && "Shift"].filter(Boolean) as string[];
      if (e.key === "Escape" && mods.length === 0) {
        setRecording(false);
        setHeld("");
        return;
      }
      if (e.metaKey) {
        setProblem("The Windows key can't be used in the shortcut.");
        return;
      }
      const key = keyName(e.code);
      if (!key) {
        setHeld(mods.join("+")); // only modifiers so far (or a key we don't allow)
        return;
      }
      if (mods.length === 0) {
        setAttempt(key);
        setProblem("Use at least one of Ctrl, Alt or Shift.");
        return;
      }
      setRecording(false);
      setHeld("");
      apply([...mods, key].join("+"));
    };
    window.addEventListener("keydown", down, true);
    return () => window.removeEventListener("keydown", down, true);
  }, [recording, apply]);

  const start = () => {
    setRecording(true);
    setHeld("");
    setAttempt(null);
    setProblem(null);
    setDone(null);
  };

  return (
    <section className="card" aria-labelledby="hotkey-h">
      <div className="card-head">
        <h2 id="hotkey-h">Keyboard shortcut</h2>
        <span className="status-actions">
          {recording ? (
            <button className="btn" onClick={() => setRecording(false)}>Cancel</button>
          ) : (
            <button className="btn" disabled={busy || !hotkey} onClick={start}>Change</button>
          )}
          {hotkey && hotkey !== DEFAULT_HOTKEY && !recording && (
            <button className="btn" disabled={busy} onClick={() => apply(DEFAULT_HOTKEY)}>
              Reset to {DEFAULT_HOTKEY}
            </button>
          )}
        </span>
      </div>
      <p className="muted small">Press it anywhere in Windows to open the search.</p>
      <div className={"hotkey-box" + (recording ? " rec" : problem ? " bad" : "")} aria-live="polite">
        {recording ? (
          held ? (
            <Keys combo={held + "+…"} />
          ) : (
            <span className="muted">Press the new shortcut… (Esc to cancel)</span>
          )
        ) : (
          <Keys combo={attempt ?? hotkey ?? "…"} />
        )}
      </div>
      {problem && !recording && (
        <p className="hotkey-problem" role="alert">
          {problem}
        </p>
      )}
      {done && !problem && <p className="muted small">{done}</p>}
    </section>
  );
}

function formatBytes(n: number): string {
  const units = ["bytes", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return i === 0 ? `${n} ${units[0]}` : `${n.toFixed(n < 10 ? 1 : 0)} ${units[i]}`;
}

function Page({ children }: { children: React.ReactNode }) {
  return (
    <div className="settings-page">
      <div className="settings-inner">{children}</div>
    </div>
  );
}

function IndexingCard({
  status,
  busy,
  onAction,
}: {
  status: Status | null;
  busy: string | null;
  onAction: (a: "pause" | "resume" | "rescan") => void;
}) {
  const ix = status?.indexer;
  let text = "Connecting…";
  let tone = "idle";
  if (status && !ix) text = "Background indexing is off";
  else if (ix) {
    const files = plural(status!.index.files, "file");
    if (status!.setup_needed) [text, tone] = ["Add a folder to start", "idle"];
    else if (ix.manual_pause) [text, tone] = [`Paused by you · ${files} indexed`, "paused"];
    else if (ix.phase === "paused") [text, tone] = [`Paused: ${ix.paused_reason} · ${files}`, "paused"];
    else if (ix.phase === "indexing")
      [text, tone] = [`Indexing ${ix.done.toLocaleString()} of ${plural(ix.total, "file")}…`, "busy"];
    else if (ix.phase === "scanning" || ix.phase === "loading") [text, tone] = ["Looking for new and changed files…", "busy"];
    else [text, tone] = [`Up to date · ${files} indexed`, "ok"];
    if (ix.last_error) text += ` · Last problem: ${ix.last_error.slice(0, 120)}`;
  }
  return (
    <section className="card status-card" aria-label="Indexing status">
      <span className={`dot ${tone}`} aria-hidden="true" />
      <span className="status-text">{text}</span>
      {ix && !status?.setup_needed && (
        <span className="status-actions">
          {ix.manual_pause ? (
            <button className="btn" disabled={busy === "resume"} onClick={() => onAction("resume")}>Resume</button>
          ) : (
            <button className="btn" disabled={busy === "pause"} onClick={() => onAction("pause")}>Pause</button>
          )}
          <button className="btn" disabled={busy === "rescan"} onClick={() => onAction("rescan")}>Re-scan</button>
        </span>
      )}
    </section>
  );
}

function PathInput(props: {
  label: string;
  placeholder: string;
  button: string;
  busy: boolean;
  onSubmit: (v: string) => void;
}) {
  const [value, setValue] = useState("");
  return (
    <form
      className="path-input"
      onSubmit={(e) => {
        e.preventDefault();
        const v = value.trim().replace(/^"(.*)"$/, "$1"); // Explorer's "Copy as path" adds quotes
        if (!v) return;
        props.onSubmit(v);
        setValue("");
      }}
    >
      <input
        aria-label={props.label}
        value={value}
        placeholder={props.placeholder}
        spellCheck={false}
        onChange={(e) => setValue(e.target.value)}
      />
      <button className="btn" type="submit" disabled={props.busy || !value.trim()}>
        {props.button}
      </button>
    </form>
  );
}

function folderMeta(f: ConfigView["folders"][number], phase?: string): string {
  if (!f.exists) return "Folder not found";
  const parts = [plural(f.indexed, "file")];
  if (f.pending) parts.push(`${f.pending.toLocaleString()} waiting`);
  if (f.errors) parts.push(`${f.errors.toLocaleString()} unreadable`);
  if (f.total === 0 && (phase === "scanning" || phase === "indexing" || phase === "loading")) return "Scanning…";
  return parts.join(" · ");
}

function plural(n: number, word: string): string {
  return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
}

function baseName(p: string): string {
  const parts = p.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || p;
}

function FolderIcon() {
  return (
    <svg className="folder-icon" viewBox="0 0 24 24" aria-hidden="true">
      <path
        d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4.38a1.5 1.5 0 0 1 1.06.44L11.5 7h8A1.5 1.5 0 0 1 21 8.5v9a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5z"
        fill="currentColor"
      />
    </svg>
  );
}
