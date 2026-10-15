# Spotlight for Windows — PLAN

> Original design notes, kept for reference. Current plans: [roadmap](../roadmap.md).

> Press a hotkey anywhere → a floating search bar appears → type → files, photos, apps,
> calculations and settings show up instantly → Enter opens. Built on top of the existing
> `grain-search` engine (EmbeddingGemma 2 + SQLite).

---

## 1. What it should feel like

| Moment | Target |
|---|---|
| Press **Alt+Space** | Bar appears in **< 100 ms**, centred, blurred glass background, cursor in the box |
| Type `budg` | File-name hits within **~50 ms** of each keystroke |
| Type a sentence / describe a photo | Meaning results stream in **~150 ms** later (GPU) |
| ↑ ↓ | Move through results; preview updates |
| **Enter** | Open · **Ctrl+Enter** reveal in Explorer · **Ctrl+C** copy path · **Space** Quick Look preview |
| **Esc** / click away | Hides instantly (stays loaded in memory for the next time) |

Results are grouped like macOS Spotlight:

```
┌──────────────────────────────────────────────────────────────┐
│ 🔍  invoice paid late bank holiday                            │
├───────────────────────────────┬──────────────────────────────┤
│ TOP HIT                       │                              │
│ ▸ letter_to_sam.txt   EXACT   │   PREVIEW                    │
│ APPLICATIONS                  │   letter_to_sam.txt          │
│   Notepad                     │   D:\Documents\letters       │
│ DOCUMENTS · contains text     │   …the invoice was paid late │
│   payments.xlsx  ALL WORDS    │   because of the **bank      │
│ DOCUMENTS · related           │   holiday** in March…        │
│   finance_notes.md  0.54      │                              │
│ IMAGES                        │   Modified 3 Mar 2025 · 4 KB │
│   🖼 🖼 🖼 🖼                    │   [Open] [Show in folder]    │
└───────────────────────────────┴──────────────────────────────┘
```

---

## 2. Architecture

Two processes. The UI stays tiny and instant; the heavy AI part stays warm in the background.

```
 ┌──────────────── Spotlight UI (Tauri app) ────────────────┐
 │  global hotkey · hidden-but-preloaded window · tray icon │
 │  React UI: search box, sections, preview, keyboard nav   │
 │  local providers: apps launcher, calculator, settings    │
 └───────────────┬──────────────────────────────────────────┘
                 │ HTTP + Server-Sent Events, 127.0.0.1 only, secret token
 ┌───────────────▼──────────── Search service (Python) ─────┐
 │  `grain serve` — FastAPI                                     │
 │  EmbeddingGemma 2 loaded once on the GPU (bf16)      │
 │  grain_search: store · search · indexer (existing)   │
 │  watcher: live re-index on file changes (watchdog)        │
 │  thumbnails + previews (PIL, PyMuPDF)                     │
 └───────────────────────────────────────────────────────────┘
```

**Why two processes:** loading the model takes seconds and ~1 GB of GPU memory; a Spotlight
bar must appear in milliseconds. The service starts once at login and keeps the model warm; the
UI only talks to it. If the service is down, the UI still works for apps, calculator and
file-name search (via a direct read-only SQLite connection).

---

## 3. Tech choices

| Part | Choice | Why | Alternative |
|---|---|---|---|
| UI shell | **Tauri 2** (Rust + WebView2) | ~10 MB app, ~40 MB RAM, native window effects, official global-shortcut / tray / autostart / single-instance plugins | Electron (you know it from `kanban-electron`, but ~150 MB RAM, slower to show) · PySide6 (one language, harder to make look good) |
| UI code | React + TypeScript + Vite | fast to iterate, keyboard-heavy UI is easy | Svelte |
| Glass look | `window-vibrancy` crate: **Mica / Acrylic** on Windows 11, solid fallback on 10 | looks native | CSS blur only |
| Backend | Existing Python package + **FastAPI** + uvicorn | reuse everything already built | — |
| Live updates | `watchdog` (ReadDirectoryChangesW) | real-time, cheap | NTFS USN journal later for whole drives |
| Streaming results | Server-Sent Events | simple, one-way, works with fetch | WebSocket |

---

## 4. Result providers (what can be found)

Each provider returns results fast and independently; the UI merges them by section.

| Provider | Where | Source | Speed |
|---|---|---|---|
| **Apps** | UI (Rust) | Start Menu `.lnk` files (`%ProgramData%` + `%AppData%\Microsoft\Windows\Start Menu`) + Store apps (`shell:AppsFolder`) | instant, cached |
| **Calculator** | UI | `2*(3+4)`, `15% of 240`, `sqrt 2` → math parser | instant |
| **Units / currency** | UI | `5 km in miles`, `100 usd in inr` (currency rates cached daily) | instant |
| **Windows settings** | UI | `wifi`, `bluetooth`, `display` → `ms-settings:` URIs | instant |
| **System commands** | UI | `lock`, `sleep`, `restart`, `empty recycle bin` (confirm first) | instant |
| **File names** | service | `names_fts` trigram | ~10 ms |
| **Text in files** | service | FTS5 exact phrase / all words | ~20 ms |
| **Meaning (docs)** | service | EmbeddingGemma 2 vectors | ~50–150 ms |
| **Photos** | service | EmbeddingGemma 2 image vectors | ~50–150 ms |
| **Web fallback** | UI | last row: “Search the web for …” | instant |

**Ranking “Top Hit”:** exact text match > app/file-name prefix match > frequently opened >
strong meaning match. Every Enter is logged (locally) so things you open often rise to the top.

---

## 5. Search service API

All on `127.0.0.1`, on a port the OS picks at start. The address and a random token (new on every
start) are in `service.json`; every request carries `Authorization: Bearer <token>`.

| Endpoint | Purpose |
|---|---|
| `GET /search?q=…&limit=…` → **SSE stream** | event `fast` (names + text matches, ~20 ms) then event `full` (meaning docs + photos) |
| `GET /preview?path=…` | snippet with highlights, metadata, PDF page text |
| `GET /thumb?path=…&size=256` | cached JPEG thumbnail (photos, PDF first page) |
| `POST /open` `{path, page, line, mode: open|reveal|copy}` | open via default app / Explorer |
| `POST /feedback` `{query, path}` | record what was opened (for ranking) |
| `GET /status` | indexed counts, queue, device (cuda/cpu), last error |
| `POST /index/pause` · `/index/resume` | pause indexing (e.g. while gaming) |

Requests for a stale query are cancelled when a newer keystroke arrives.

---

## 6. Window behaviour (the details that make it feel like Spotlight)

- Window is **created at startup and kept hidden**; the hotkey only shows/focuses it → instant.
- Frameless, always-on-top, no taskbar button, rounded corners, centred on the **monitor with the mouse**.
- Hides on **Esc**, on focus loss, and after opening a result.
- Remembers the last query for 30 s (reopen → same results), then clears.
- Height grows with results (max ~60% of screen); preview pane only when there are results.
- Tray icon: *Open · Pause indexing · Settings · Quit*.
- Single instance; starts at login (Tauri autostart) and starts the Python service if it isn't running.

**Hotkey:** default **Alt+Space**, configurable. Note: PowerToys Run also uses Alt+Space and
PowerToys Command Palette uses Win+Alt+Space — if PowerToys is installed, pick another
(e.g. **Ctrl+Space**) or disable theirs. Registration failure is shown in the tray, not silently ignored.

---

## 7. Indexing in the background

- Service starts → loads model → starts **watcher** on all roots → runs an incremental
  `run_index` pass (only new/changed files).
- File events are **debounced** (2 s) and batched; renames use the existing move detection.
- **Low priority**: indexing thread runs at below-normal priority and **pauses automatically**
  when a full-screen app/game is in the foreground or on battery, so it never hurts gaming.
- Status bar in the UI: `Indexed 48,210 · 120 queued · GPU`.

---

## 8. Quick Look preview (Space)

| Type | Preview |
|---|---|
| Image | full-size image, EXIF date/camera |
| PDF | rendered matching page (PyMuPDF → PNG) with text snippet |
| Text / code | highlighted lines around the match, monospace for code |
| Office | extracted text around the match |
| App | icon, path, “Run as administrator” action |

---

## 9. Settings window

Folders to index · excludes · hotkey · theme (system/light/dark) · which sections to show ·
result count · “related by meaning” strictness (`text_min_score`) · GPU/CPU · start with
Windows · rebuild index · open logs.

---

## 10. Roadmap

### Phase A — Service ✅ built (12 Oct 2026)
Built on Starlette (the framework FastAPI is made of) — fewer dependencies, same speed.
Vectors are kept in memory incrementally, so live re-indexing never triggers a full reload.
- `grain serve` (FastAPI) wrapping `Searcher`, token auth, SSE `fast`/`full` stages, `/open`, `/status`.
- Watcher + background incremental indexing + pause/resume.
- **Exit:** `curl` search returns in < 30 ms (fast) / < 200 ms (full) with the model warm.

### Phase B — Bar MVP ✅ built (12 Oct 2026)
UI tested in Chromium against a live `grain serve`; the Rust shell is compiled on Windows.
Includes a first version of the preview pane and thumbnails (originally Phase C).
- Tauri 2 + React: hidden preloaded window, Alt+Space toggle, search box, sections,
  keyboard navigation, Enter / Ctrl+Enter / Esc, tray icon, autostart, single instance.
- **Exit:** hotkey → typing → Enter opens the right file, every time, without touching the mouse.

### Phase C — Spotlight polish (1–2 days)
- Mica/Acrylic glass, thumbnails grid for photos, preview pane, Quick Look (Space),
  multi-monitor placement, dark/light theme, smooth height animation.

### Phase D — More providers (1–2 days)
- Apps launcher (Start Menu + Store apps, icons), calculator, units, Windows settings,
  system commands, web fallback, “Top Hit” ranking with open-frequency.

### Phase E — Ship (1 day)
- Settings window, first-run onboarding (choose folders, hotkey), logs.
- Packaging: Tauri `.msi`/NSIS installer for the UI; the service runs from the project's uv
  environment (`uv run grain serve`) — bundling CUDA PyTorch into one installer (~3 GB) is not
  worth it for personal use.

---

## 11. Risks

| Risk | Mitigation |
|---|---|
| Hotkey already taken (PowerToys, games) | configurable, clear error in tray |
| Window steals focus badly / doesn't come to front | Tauri `set_focus` + Win32 `AllowSetForegroundWindow` workaround; test with fullscreen apps |
| Indexing slows down games | auto-pause on fullscreen/battery, low thread priority, GPU batch limits |
| Service crashed / not started | UI shows apps + calculator + file names via read-only SQLite; tray “Restart service” |
| SQLite busy while indexer writes | WAL mode (already on), short write transactions |
| Another local program calls the API | 127.0.0.1 only + random bearer token + strict CORS (Tauri origin only) |
| 8 GB VRAM shared with games | text+image model only (~1 GB), unload model after N minutes idle (optional) |

---

## 12. Decisions (12 Oct 2026)

| Question | Decision |
|---|---|
| UI tech | **Tauri 2 + React + TypeScript** |
| Hotkey | **Alt+Space** (configurable; if PowerToys Run is installed, its Alt+Space must be turned off) |
| v1 scope | **Files + photos first** — Phases A, B, C. Apps/calculator/settings (Phase D) come after. |
| Look | **macOS Spotlight style** — light frosted glass bar, large rounded corners, big search text, soft shadow; dark variant for dark mode |

macOS look details: 680 px wide bar centred in the top third of the screen, 18 px corner radius,
22 px search text, Acrylic blur with a light tint (`rgba(246,246,246,0.72)`), 1 px hairline border,
large soft shadow; selected row = system-blue pill; preview pane on the right only when there
are results; Segoe UI Variable (closest Windows match to SF Pro).

## 13. Folder layout (added to the project)

```
grain-search/
├── src/grain_search/
│   ├── service.py         # FastAPI app: /search (SSE) /preview /thumb /open /status
│   ├── watcher.py         # watchdog → debounced incremental indexing
│   ├── previews.py        # thumbnails, PDF page renders, highlighted snippets
│   └── ranking.py         # open-frequency boost, Top Hit selection
└── app/             # Tauri 2 app
    ├── src-tauri/         # Rust: hotkey, window, tray, autostart, apps scanner, opener
    └── src/               # React + TS: SearchBox, ResultList, Preview, Settings
```
