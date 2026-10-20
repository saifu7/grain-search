# Roadmap

Where Grain Search is heading. Each phase is a GitHub milestone; progress is tracked in issues.

## Releases

| Release | Goal | What you'll find on the Releases page |
|---|---|---|
| **v0.1.0-alpha** (pre-release) | Repository, CI and release pipeline in place; portable foundation; smooth search window | Developer-preview installer (expects the engine set up from source), engine wheel, checksums |
| **v0.2.0-beta** (pre-release) | First build anyone can install: settings window, first-run wizard, runtime and model downloaded by the app | Installer, update files, engine wheel, checksums |
| **v1.0.0** | Signed, auto-updating, tested on a wide range of PCs | Signed installer, update files, engine wheel, checksums |

## Phases

### Phase 0: Open-source repository ✅
Monorepo (`engine/`, `app/`, `docs/`), MIT licence, contributing guide, code of conduct, security
policy, issue and PR templates, CI (lint, tests, builds), tag-driven release workflow with
checksums and build attestations, Dependabot, CodeQL.

### Phase 1: Portable foundation
- One place that resolves app folders (`%APPDATA%\GrainSearch`, `%LOCALAPPDATA%\GrainSearch`).
- Settings in `config.json`. **No folders indexed until the user picks them.**
- ✅ Nothing machine-specific compiled in (engine folder and uv found at run time); free port chosen at start.
- ✅ The service always stops with the app (Windows job object, plus the engine watching the app).
- The model unloads from the GPU when idle.
- Crash restart with back-off; rotating logs; "Open logs" in the tray.

### Phase 2: Settings window and first-run wizard
- **Wizard:** choose folders, exclusions, hardware check (GPU / disk space, where to store the
  index), download and set-up progress, hotkey, start indexing.
- **Settings tabs:** Folders · Exclusions (with live "would exclude N files" preview) · File types ·
  Search strictness · AI & performance · Shortcut & look · Index (errors with Retry, move to
  another drive) · About.
- Changes apply live. Removing a folder removes its files from the index at once.
- ✅ Done early: the Settings window with folders, exclusions, indexing status, pause/resume and
  re-scan, applied live, and opened automatically on first run.

### Phase 3: Smooth search window
Rows that don't jump (stable merge of fast and full results), selection follows the file, preview
never flashes empty, window resized once before rendering, reserved space for images.
A Playwright test measures layout shift (target CLS < 0.02) on every pull request.

### Phase 4: Installer and first-run runtime
~15 MB per-user NSIS installer (no admin rights). On first run it sets up Python and PyTorch with
the bundled `uv`: the CUDA build for NVIDIA GPUs, the CPU build otherwise. It then downloads
EmbeddingGemma 2, resumably. The uninstaller offers to remove the index and models too.

### Phase 5: Release quality
Code signing (no SmartScreen "unknown publisher"), signed auto-updates from GitHub Releases with a
beta channel, test matrix (Windows 10/11, NVIDIA / other / no GPU, unusual usernames, full disks,
OneDrive, multi-monitor), privacy statement, third-party notices.

## Later
- Inference without Python (ONNX / LiteRT) for a much smaller app.
- winget and Scoop packages.
- App launcher, calculator, Windows settings search.
- Scanned PDFs (OCR), video and audio search.
- AMD/Intel GPU acceleration (DirectML); Windows on ARM.

Background: [architecture](architecture.md) · [tech debt](tech-debt.md) · [engine design](design/engine.md) ·
[search window design](design/search-window.md).
