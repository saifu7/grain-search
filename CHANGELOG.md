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

[Unreleased]: https://github.com/saifu7/grain-search/commits/main
