// The Settings window: which folders are searched, what is skipped, and indexing status.
// Every change is sent to the search service, which saves config.json and applies it live.
import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "./api";
import { ensureService, inTauri, pickFolder } from "./platform";
import type { ConfigView, ServiceInfo, Status } from "./types";

type Note = { kind: "ok" | "error"; text: string } | null;

export default function Settings() {
  const [svc, setSvc] = useState<ServiceInfo | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  const [config, setConfig] = useState<ConfigView | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [note, setNote] = useState<Note>(null);
  const [busy, setBusy] = useState<string | null>(null);
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
  }, [svc]);

  // fast refresh while indexing (counts move), slow when idle
  const indexing = status?.indexer?.phase === "indexing" || status?.indexer?.phase === "scanning";
  useEffect(() => {
    if (!svc) return;
    refresh();
    const t = window.setInterval(refresh, indexing ? 1500 : 4000);
    return () => window.clearInterval(t);
  }, [svc, refresh, indexing]);

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
