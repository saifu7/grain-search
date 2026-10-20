# Grain Search app (Windows)

Press **Alt+Space** anywhere → search your files and photos → **Enter** opens.
A Tauri 2 + React window on top of the [engine](../engine/README.md) service (`grain serve`).

## Build & run (first time ~5–10 min, later builds are fast)

```powershell
cd app
npm ci
npm run tauri dev        # development: opens the bar, hot-reloads the UI
```

The bar starts the search service for you (`uv run grain serve` in `engine\`, hidden window,
log in `%LOCALAPPDATA%\GrainSearch\logs\service.log`). If it is already running, it just connects to it.

Make a real app + installer:
```powershell
npm run tauri build
```
→ `src-tauri\target\release\grain-search.exe` and an installer in
`src-tauri\target\release\bundle\nsis\`. Install it, tick **Start with Windows** in the tray menu, done.

## Using it

| Key | Action |
|---|---|
| **Alt+Space** | show / hide |
| type | file names + text instantly, meaning + photos a moment later |
| **↑ ↓** | move through results (preview on the right) |
| **Enter** | open (PDFs at the matching page, code at the line, when SumatraPDF / VS Code are installed) |
| **Ctrl+Enter** | show in Explorer |
| **Ctrl+C** | copy the file path |
| **Esc** | clear the search; again to hide |

`=text` only files that contain the text · `~idea` only by meaning · `type:img`, `ext:pdf`, `in:D:\work`, `after:2025-01-01`

Tray icon: Open · Settings… · Pause / Resume indexing · Re-scan · Start with Windows · Quit (also
stops the service if the bar started it).

**Settings window** (⚙ in the search bar, or tray → Settings…): add/remove folders with the folder
picker or by pasting a path, add exclusions (a folder or a pattern like `*.log`), see per-folder
counts, pause/resume, re-scan. Changes apply immediately. In a browser (UI development) the page
is at `#settings`, with a text box instead of the folder picker.

## Settings (optional)

Copy `settings.example.json` to `%APPDATA%\GrainSearch\settings.json`:

| Key | Default | |
|---|---|---|
| `hotkey` | `Alt+Space` | e.g. `Ctrl+Space`, `Alt+Shift+Space` |
| `glass` | `false` | `true` = real Windows acrylic blur (Windows draws small square-ish corners); `false` = macOS-style rounded frosted bar |
| `engine_dir` | found automatically | the engine folder (where its `pyproject.toml` is). Normally not needed: the app looks for an `engine` folder next to itself and in the folders above it, so a development build finds `<repo>\engine` (the old name `project_dir` still works) |
| `uv` | found automatically | full path to `uv.exe`; normally found on PATH or in `%USERPROFILE%\.local\bin` |

**Alt+Space taken?** PowerToys Run uses it too: PowerToys → PowerToys Run → change or disable its
shortcut, or set another `hotkey` here. If the hotkey can't be registered, the bar's footer says so.

## UI development in a browser

```powershell
uv run grain serve                           # in engine\
npm run dev                                  # in app\
```
Open `http://localhost:1420/?url=<url>&token=<token>`, with both values copied from
`%LOCALAPPDATA%\GrainSearch\service.json` (the port changes every time the service starts).
