# Changelog

All notable changes to Grain Search are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Before 1.0, minor versions may change the config or
index format; the app migrates or rebuilds automatically and the notes say so.

## [Unreleased]

### Added
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
- New Grain Search logo: a lowercase "g" with a golden grain in its bowl. App, tray and installer
  icons are generated from `docs/assets/logo.svg`.

### Changed
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
- The search service no longer keeps running (holding ~1.4 GB of GPU memory) when the app is
  closed from Task Manager, crashes, or is stopped with Ctrl+C: Windows ends it together with the
  app, and the service also stops by itself once the app is gone. Files opened from Grain Search
  stay open when it quits.

[Unreleased]: https://github.com/saifu7/grain-search/commits/main
