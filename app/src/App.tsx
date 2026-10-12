import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import * as api from "./api";
import { copyText, ensureService, getSettings, hideWindow, inTauri, onShown, setWindowHeight } from "./platform";
import type { DocHit, Item, Preview, SearchPayload, Section, ServiceInfo, Status, UiSettings } from "./types";

// layout (logical px) — keep in sync with styles.css
const BAR = 64;
const BODY = 430;
const FOOTER = 30;
const MARGIN_ROUNDED = 18; // transparent margin around the rounded panel, for its shadow
const LIMIT = 12;
const DEBOUNCE_MS = 70;
const KEEP_QUERY_MS = 30_000;

type Boot = { state: "starting" } | { state: "ready"; svc: ServiceInfo } | { state: "error"; message: string };

export default function App() {
  const [boot, setBoot] = useState<Boot>({ state: "starting" });
  const [settings, setSettings] = useState<UiSettings>({ glass: false, hotkey: "Alt+Space" });
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<SearchPayload | null>(null);
  const [searching, setSearching] = useState(false);
  const [sel, setSel] = useState(0);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const lastActive = useRef(Date.now());

  const svc = boot.state === "ready" ? boot.svc : null;

  // -- start-up: settings + make sure the search service is running -------------------
  const connect = useCallback(() => {
    setBoot({ state: "starting" });
    ensureService()
      .then((s) => setBoot({ state: "ready", svc: s }))
      .catch((e) => setBoot({ state: "error", message: String(e?.message ?? e) }));
  }, []);
  useEffect(() => {
    getSettings().then(setSettings).catch(() => {});
    connect();
  }, [connect]);

  // -- every time the hotkey shows the window -----------------------------------------
  useEffect(
    () =>
      onShown(() => {
        if (Date.now() - lastActive.current > KEEP_QUERY_MS) {
          setQuery("");
          setResult(null);
        }
        lastActive.current = Date.now();
        requestAnimationFrame(() => {
          input.current?.focus();
          input.current?.select();
        });
        if (boot.state === "error") connect();
      }),
    [boot.state, connect],
  );
  useEffect(() => input.current?.focus(), []);

  // -- search as you type --------------------------------------------------------------
  useEffect(() => {
    lastActive.current = Date.now();
    if (!svc) return;
    const q = query.trim();
    if (!q) {
      setResult(null);
      setSearching(false);
      return;
    }
    let cancel = () => {};
    const timer = setTimeout(() => {
      setSearching(true);
      cancel = api.search(
        svc,
        q,
        LIMIT,
        (p) => {
          setResult((prev) => (p.stage === "fast" && prev?.stage === "full" && prev.query === p.query ? prev : p));
          if (p.stage === "full") setSearching(false);
          if (p.stage === "fast" && !p.skipped) setSel(0);
        },
        (msg) => {
          setSearching(false);
          flash(msg);
        },
      );
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      cancel();
    };
  }, [query, svc]);

  // -- flatten results into selectable rows, grouped like Spotlight ----------------------
  const items = useMemo<Item[]>(() => buildItems(result), [result]);
  useEffect(() => setSel((s) => Math.min(s, Math.max(0, items.length - 1))), [items.length]);
  const current = items[sel] ?? null;

  // -- preview of the selected row --------------------------------------------------------
  useEffect(() => {
    setPreview(null);
    if (!svc || !current) return;
    const hit = current.hit;
    const t = setTimeout(() => {
      const page = current.kind === "doc" ? current.hit.page : null;
      const line = current.kind === "doc" ? current.hit.line : null;
      api
        .preview(svc, hit.path, query.trim(), page, line)
        .then(setPreview)
        .catch(() => {});
    }, 60);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [svc, current?.hit.path]);

  // keep the selected row visible
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-i="${sel}"]`)?.scrollIntoView({ block: "nearest" });
  }, [sel]);

  // -- indexing status (footer) ----------------------------------------------------------
  useEffect(() => {
    if (!svc) return;
    let alive = true;
    const tick = () => api.status(svc).then((s) => alive && setStatus(s)).catch(() => {});
    tick();
    const t = setInterval(tick, 4000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [svc]);

  // -- window height follows content -------------------------------------------------------
  const hasBody = query.trim().length > 0 || boot.state !== "ready";
  const margin = settings.glass ? 0 : MARGIN_ROUNDED;
  useLayoutEffect(() => {
    setWindowHeight(margin * 2 + BAR + (hasBody ? 1 + BODY + FOOTER : 0));
  }, [hasBody, margin]);

  // -- actions -----------------------------------------------------------------------------
  function flash(msg: string) {
    setToast(msg);
    setTimeout(() => setToast((t) => (t === msg ? null : t)), 2500);
  }

  async function open(item: Item | null, mode: "open" | "reveal") {
    if (!svc || !item) return;
    const page = item.kind === "doc" ? item.hit.page : null;
    const line = item.kind === "doc" ? item.hit.line : null;
    try {
      await api.openFile(svc, item.hit.path, mode, query.trim(), page, line);
      hideWindow();
    } catch (e) {
      flash(String((e as Error).message).includes("410") ? "That file no longer exists" : "Could not open the file");
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSel((s) => Math.min(s + 1, items.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSel((s) => Math.max(s - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      open(current, e.ctrlKey || e.metaKey ? "reveal" : "open");
    } else if (e.key === "Escape") {
      e.preventDefault();
      if (query) setQuery("");
      else hideWindow();
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "c") {
      const el = e.currentTarget;
      if (current && el.selectionStart === el.selectionEnd) {
        e.preventDefault();
        copyText(current.hit.path).then(() => flash("Path copied"));
      }
    }
  }

  // -- render ------------------------------------------------------------------------------
  const cls = ["shell", settings.glass ? "glass" : "rounded", inTauri() ? "in-tauri" : "in-browser"].join(" ");
  return (
    <div className={cls} style={{ padding: margin }}>
      <div className="panel">
        <div className="bar" data-tauri-drag-region>
          <SearchIcon />
          <input
            ref={input}
            className="query"
            value={query}
            placeholder="Search files and photos"
            spellCheck={false}
            autoComplete="off"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
          />
          {searching && <span className="spinner" aria-label="searching" />}
        </div>

        {hasBody && (
          <>
            <div className="divider" />
            <div className="body" style={{ height: BODY }}>
              {boot.state === "starting" && <Message title="Starting search…" text="Loading the AI model, this takes a few seconds." />}
              {boot.state === "error" && (
                <Message title="Search service isn't running" text={boot.message}>
                  <button className="btn" onClick={connect}>Try again</button>
                </Message>
              )}
              {boot.state === "ready" && (
                <>
                  <div className="list" ref={listRef}>
                    {items.length === 0 && result?.stage === "full" && <div className="empty">No results for “{query.trim()}”</div>}
                    {items.length === 0 && result?.stage !== "full" && <div className="empty muted">Searching…</div>}
                    <ResultList items={items} sel={sel} svc={boot.svc} onSelect={setSel} onOpen={(i) => open(i, "open")} />
                  </div>
                  <div className="preview">
                    <PreviewPane svc={boot.svc} item={current} preview={preview} />
                  </div>
                </>
              )}
            </div>
            <Footer status={status} toast={toast} settings={settings} />
          </>
        )}
      </div>
    </div>
  );
}

// ======================================================================================
function buildItems(r: SearchPayload | null): Item[] {
  if (!r) return [];
  const out: Item[] = [];
  const top = r.top;
  const topDoc = r.docs.find((d) => d.path === top);
  const topImg = !topDoc ? r.images.find((i) => i.path === top) : undefined;
  if (topDoc) out.push({ kind: "doc", section: "top", hit: topDoc });
  if (topImg) out.push({ kind: "image", section: "top", hit: topImg });
  for (const d of r.docs) if (d.path !== top && d.contains_text) out.push({ kind: "doc", section: "docs", hit: d });
  // photos before "related" documents: describing a photo is a main use, related docs are the weakest hits
  for (const i of r.images) if (i.path !== top) out.push({ kind: "image", section: "images", hit: i });
  for (const d of r.docs) if (d.path !== top && !d.contains_text) out.push({ kind: "doc", section: "related", hit: d });
  return out;
}

const SECTION_TITLE: Record<Section, string> = {
  top: "Top Hit",
  docs: "Documents",
  related: "Related by meaning",
  images: "Images",
};

function ResultList(props: {
  items: Item[];
  sel: number;
  svc: ServiceInfo;
  onSelect: (i: number) => void;
  onOpen: (item: Item) => void;
}) {
  const { items, sel, svc, onSelect, onOpen } = props;
  const blocks: { section: Section; rows: { item: Item; i: number }[] }[] = [];
  items.forEach((item, i) => {
    const last = blocks[blocks.length - 1];
    if (last && last.section === item.section) last.rows.push({ item, i });
    else blocks.push({ section: item.section, rows: [{ item, i }] });
  });
  return (
    <>
      {blocks.map((b) => (
        <section key={b.section}>
          <h3>{SECTION_TITLE[b.section]}</h3>
          {b.section === "images" ? (
            <div className="grid">
              {b.rows.map(({ item, i }) => (
                <button
                  key={item.hit.path}
                  data-i={i}
                  className={"tile" + (i === sel ? " sel" : "")}
                  title={item.hit.name}
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => onSelect(i)}
                  onDoubleClick={() => onOpen(item)}
                >
                  <img src={api.thumbUrl(svc, item.hit.path, 160)} alt="" loading="lazy" />
                </button>
              ))}
            </div>
          ) : (
            b.rows.map(({ item, i }) => (
              <Row key={item.hit.path} item={item} i={i} selected={i === sel} svc={svc} onSelect={onSelect} onOpen={onOpen} />
            ))
          )}
        </section>
      ))}
    </>
  );
}

function Row(props: {
  item: Item;
  i: number;
  selected: boolean;
  svc: ServiceInfo;
  onSelect: (i: number) => void;
  onOpen: (item: Item) => void;
}) {
  const { item, i, selected, svc, onSelect, onOpen } = props;
  const hit = item.hit;
  const doc = item.kind === "doc" ? (hit as DocHit) : null;
  return (
    <div
      data-i={i}
      className={"row" + (selected ? " sel" : "")}
      onMouseDown={(e) => e.preventDefault()}
      onClick={() => onSelect(i)}
      onDoubleClick={() => onOpen(item)}
    >
      {item.kind === "image" ? (
        <img className="row-thumb" src={api.thumbUrl(svc, hit.path, 64)} alt="" />
      ) : (
        <FileIcon ext={doc!.ext} />
      )}
      <div className="row-text">
        <div className="row-name">{hit.name}</div>
        <div className="row-sub">
          {doc && <Badge doc={doc} />}
          <span className="row-folder">{shortFolder(hit.folder)}</span>
        </div>
      </div>
    </div>
  );
}

function Badge({ doc }: { doc: DocHit }) {
  if (doc.match === "exact") return <span className="badge exact">Exact</span>;
  if (doc.match === "words") return <span className="badge words">All words</span>;
  if (doc.match === "name") return <span className="badge name">Name</span>;
  return <span className="badge related">{doc.similarity != null ? `${Math.round(doc.similarity * 100)}% similar` : "Related"}</span>;
}

// ======================================================================================
function PreviewPane({ svc, item, preview }: { svc: ServiceInfo; item: Item | null; preview: Preview | null }) {
  if (!item) return <div className="pv-empty" />;
  const hit = item.hit;
  const doc = item.kind === "doc" ? (item.hit as DocHit) : null;
  const isPdf = doc?.ext === ".pdf";
  const page = preview?.excerpt?.page ?? doc?.page ?? null;
  return (
    <div className="pv">
      {item.kind === "image" ? (
        <img className="pv-image" src={api.thumbUrl(svc, hit.path, 512)} alt="" />
      ) : isPdf && page ? (
        <img className="pv-page" src={api.pageUrl(svc, hit.path, page, 520)} alt="" />
      ) : (
        <FileIcon ext={doc?.ext ?? ""} big />
      )}
      <div className="pv-name">{hit.name}</div>
      <div className="pv-folder" title={hit.folder}>{hit.folder}</div>
      {preview?.excerpt && preview.excerpt.lines.length > 0 && (
        <pre className="pv-excerpt">
          {preview.excerpt.lines.map((ln, k) => (
            <div key={k} className={"pv-line" + (ln.match ? " hit" : "")}>
              {ln.n != null && <span className="pv-ln">{ln.n}</span>}
              <span>{highlight(ln.text, ln.highlights)}</span>
            </div>
          ))}
        </pre>
      )}
      {preview && (
        <dl className="pv-meta">
          {page && (
            <>
              <dt>Page</dt>
              <dd>{page}</dd>
            </>
          )}
          {typeof preview.meta?.taken === "string" && (
            <>
              <dt>Taken</dt>
              <dd>{String(preview.meta.taken)}</dd>
            </>
          )}
          {typeof preview.meta?.camera === "string" && (
            <>
              <dt>Camera</dt>
              <dd>{String(preview.meta.camera)}</dd>
            </>
          )}
          <dt>Modified</dt>
          <dd>{preview.modified.replace("T", " ")}</dd>
          <dt>Size</dt>
          <dd>{formatSize(preview.size)}</dd>
        </dl>
      )}
    </div>
  );
}

function highlight(text: string, ranges: [number, number][]) {
  if (!ranges.length) return text;
  const out: React.ReactNode[] = [];
  let pos = 0;
  ranges.forEach(([a, b], k) => {
    if (a > pos) out.push(text.slice(pos, a));
    out.push(<mark key={k}>{text.slice(a, b)}</mark>);
    pos = b;
  });
  if (pos < text.length) out.push(text.slice(pos));
  return out;
}

// ======================================================================================
function Footer({ status, toast, settings }: { status: Status | null; toast: string | null; settings: UiSettings }) {
  let left = "";
  if (status) {
    const ix = status.indexer;
    const files = status.index.files.toLocaleString();
    const dev = status.device.startsWith("cuda") ? "GPU" : status.device.startsWith("cpu") ? "CPU" : status.device;
    if (ix?.phase === "paused") left = `Indexing paused (${ix.paused_reason}) · ${files} files`;
    else if (ix?.phase === "indexing") left = `Indexing ${ix.done.toLocaleString()} / ${ix.total.toLocaleString()} · ${files} files · ${dev}`;
    else if (ix?.phase === "scanning" || ix?.phase === "loading") left = `Checking for changes… · ${files} files`;
    else left = `${files} files indexed · ${dev}`;
  }
  if (settings.hotkey_error) left = `Hotkey ${settings.hotkey} unavailable: ${settings.hotkey_error}`;
  return (
    <div className="footer" style={{ height: FOOTER }}>
      <span className={"footer-left" + (toast ? " toast" : "")}>{toast ?? left}</span>
      <span className="keys">
        <kbd>↵</kbd> open <kbd>Ctrl</kbd>
        <kbd>↵</kbd> show in folder <kbd>Ctrl</kbd>
        <kbd>C</kbd> copy path
      </span>
    </div>
  );
}

function Message({ title, text, children }: { title: string; text: string; children?: React.ReactNode }) {
  return (
    <div className="message">
      <div className="message-title">{title}</div>
      <div className="message-text">{text}</div>
      {children}
    </div>
  );
}

// ======================================================================================
const EXT_COLORS: Record<string, string> = {
  pdf: "#e5484d", doc: "#2b6cd4", docx: "#2b6cd4", ppt: "#d9622b", pptx: "#d9622b", xls: "#1f8a4c",
  xlsx: "#1f8a4c", csv: "#1f8a4c", md: "#6e56cf", txt: "#6b7280", py: "#3776ab", js: "#c9a400",
  ts: "#3178c6", tsx: "#3178c6", json: "#8b5cf6", html: "#e34c26",
};

function FileIcon({ ext, big }: { ext: string; big?: boolean }) {
  const e = ext.replace(".", "").toLowerCase() || "file";
  const color = EXT_COLORS[e] ?? "#8e8e93";
  return (
    <div className={"file-icon" + (big ? " big" : "")} aria-hidden>
      <svg viewBox="0 0 32 40">
        <path d="M3 2h18l8 8v26a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z" fill="var(--icon-paper)" stroke="var(--icon-edge)" />
        <path d="M21 2v6a2 2 0 0 0 2 2h6" fill="none" stroke="var(--icon-edge)" />
      </svg>
      <span style={{ background: color }}>{e.slice(0, 4).toUpperCase()}</span>
    </div>
  );
}

function SearchIcon() {
  return (
    <svg className="search-icon" viewBox="0 0 24 24" aria-hidden>
      <circle cx="10.5" cy="10.5" r="6.5" fill="none" stroke="currentColor" strokeWidth="2.2" />
      <path d="M15.5 15.5 21 21" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" />
    </svg>
  );
}

function shortFolder(folder: string) {
  const parts = folder.split(/[\\/]/).filter(Boolean);
  return parts.length > 3 ? `${parts[0]}\\…\\${parts.slice(-2).join("\\")}` : folder;
}

function formatSize(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}
