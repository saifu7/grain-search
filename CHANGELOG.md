# Changelog

All notable changes to Grain Search are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Before 1.0, minor versions may change the config or
index format; the app migrates or rebuilds automatically and the notes say so.

## [Unreleased]

### Added
- **The installer works on any Windows PC**: on first start a small setup window checks for an
  NVIDIA GPU and free disk space, then downloads Python, the libraries and PyTorch (CUDA or CPU
  build) at exactly the versions Grain Search is tested with, into
  `%LOCALAPPDATA%\GrainSearch\runtime`. Progress, Cancel and Retry (finished downloads are kept);
  when it's done, Settings opens to download the AI model. Updates only download again when the
  libraries changed.
- Uninstalling removes the downloaded runtime (Python, libraries, PyTorch). Ticking **Delete the
  application data** removes every trace: index, AI model (also from a folder you chose), settings
  and logs. The Hugging Face cache, shared with other tools, is left alone.
- Search engine: documents (PDF, Word, PowerPoint, Excel, text, code) and images, indexed with
  Google's EmbeddingGemma 2; hybrid search (exact text, all words, file name, meaning) with results
  labelled "Contains your text" / "Related by meaning".
- Query syntax: `=exact only`, `~meaning only`, `"phrases"`, `type:`, `ext:`, `in:`, `after:`, `before:`.
- `grain` command line: `index`, `search`, `open`, `status`, `reset`, `serve`, interactive mode.
- Background service: live re-indexing on file changes; pauses on battery and full-screen apps;
  GPU memory cap with automatic retry on out-of-memory; previews, thumbnails and PDF pages.
- Windows app (Tauri 2 + React): `Alt+Space` search window with preview, tray menu, start with Windows.
- Open-source project setup: MIT licence, contributing guide, CI, release workflow.
- `grain folders [add|remove]`, `grain exclude [add|remove]` and `grain config` to manage what is
  indexed without editing files.

- **Settings window** (⚙ in the search bar, or tray → Settings…): add and remove folders with the
  Windows folder picker, add exclusions (a folder, or a pattern like `*.log`), see how many files
  each folder has indexed, pause/resume and re-scan. Changes apply immediately, no restart; it
  opens by itself on first run when no folders are set up.
- Engine API for the Settings window: `GET /config`, `POST /config/folders`, `POST /config/excludes`.
- Settings suggests **Documents, Pictures, Desktop and Downloads** (their real locations, also when
  OneDrive or you moved them) with how many files and how much data Grain Search would index in
  each; tick the ones you want and add them in one go. Engine API: `GET /config/suggestions`.
- Settings → **AI model**: download EmbeddingGemma 2 (~1.4 GB) with a button, into a folder you
  choose (saved as `model.dir`), with progress and Cancel; an interrupted download resumes. If the
  model is already in the Hugging Face cache it is used as is. `grain model [status|download]` does
  the same from the command line. Engine API: `GET /model`, `POST /model/download`,
  `POST /model/download/cancel`.
- Settings is split into tabs: **Folders · Exclusions · AI model · Shortcut**. It opens on AI model
  while the model still has to be downloaded.
- Settings shows where the AI model runs: the GPU's name and memory in use, or **CPU** with a
  warning that indexing is slower and why (no NVIDIA GPU, CPU-only PyTorch, or set to CPU).
  `/status` has a new `device_info`.
- Settings → **Keyboard shortcut**: see the shortcut that opens the search (Alt+Space by default),
  change it by pressing the new keys, or reset it. It applies at once, no restart. If another
  program already uses the new shortcut, the box turns red, says so, and the old one keeps working;
  the same warning shows when the app couldn't get its shortcut at start-up.
- Tray → **Unload model** frees the AI model's GPU memory (~0.9 GB) and **Stop engine** stops the
  search engine entirely (all of it); the next Alt+Space brings back whichever you unloaded.
  While the model is unloaded, changed files wait and are indexed once it is back. Engine API:
  `POST /model/unload`, `POST /model/load`, and `model_state` in `/status`.
- New Grain Search logo: a lowercase "g" with a golden grain in its bowl. App, tray and installer
  icons are generated from `docs/assets/logo.svg`.

### Changed
- The engine no longer downloads the AI model by itself: until it is downloaded (Settings → AI model)
  nothing is indexed and searches find file names and exact text only. The model folder is never
  indexed.
- The search service listens on a free port chosen at start instead of the fixed 8766, so it no
  longer clashes with other programs; a second copy refuses to start instead of fighting over the
  index. The address is in `service.json`, as before.
- The app no longer has the build machine's engine path compiled in: it finds the engine folder
  and `uv` when it starts (`engine_dir` replaces `project_dir` in settings.json; the old name still
  works). The log's first line says which ones it used.
- Removing a folder or adding an exclusion now removes those files from the index (before, only
  files deleted from disk were removed), and hides them from results straight away.
- The search service closes its database on shutdown (no locked index file afterwards).
- Settings now live in `%APPDATA%\GrainSearch\config.json` and the index, logs and service file
  in `%LOCALAPPDATA%\GrainSearch\` (XDG folders on Linux/macOS; `GRAIN_SEARCH_HOME` overrides),
  instead of inside the source folder. The index folder can be moved with `index_dir`.
- No folders are indexed until you add some; the Documents/Desktop/Downloads/Pictures default is gone.
- The app's own index folder is always excluded from indexing.
- A folder you add explicitly is indexed even when a default exclusion covers it (for example a
  notes folder under `AppData`); exclusions that match things inside it still apply.

### Fixed
- The search bar no longer looks thin or shifted for a moment after you clear the text: the panel
  now has its own height and no longer waits for the window to resize.
- The search service no longer keeps running (holding ~1.4 GB of GPU memory) when the app is
  closed from Task Manager, crashes, or is stopped with Ctrl+C: Windows ends it together with the
  app, and the service also stops by itself once the app is gone. Files opened from Grain Search
  stay open when it quits.

[Unreleased]: https://github.com/saifu7/grain-search/commits/main
