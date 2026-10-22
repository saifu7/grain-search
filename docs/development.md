# Development

How to build and run Grain Search from source on Windows.

## Tools
| Tool | Version | Get it |
|---|---|---|
| uv (Python manager) | 0.9+ | https://docs.astral.sh/uv/ (it installs Python 3.12 for you) |
| Node.js | 22 LTS | https://nodejs.org or nvm-windows |
| Rust | stable | https://rustup.rs |
| Visual Studio Build Tools | 2022, "Desktop development with C++" | needed by Rust on Windows |
| Git | any | https://git-scm.com |

Short on space on C:? Put the caches on another drive first (new terminals pick these up):
```
setx UV_CACHE_DIR D:\cache\uv
setx CARGO_HOME D:\cache\cargo
setx RUSTUP_HOME D:\cache\rustup
```

## Engine (Python)
```
cd engine
uv sync --locked                          # installs everything, incl. the CUDA build of PyTorch
uv run grain folders add D:\some\test\folder   # nothing is indexed until you add folders
uv run grain index                        # first run downloads EmbeddingGemma 2 (~1.5 GB)
uv run grain search "dog on a red sofa"
```
Check that the GPU is used: `uv run python -c "import torch; print(torch.cuda.is_available())"`.
Settings go to `%APPDATA%\GrainSearch\config.json`, the index, logs and `service.json` to
`%LOCALAPPDATA%\GrainSearch\` (`uv run grain config` shows the paths). To keep a separate
development profile, set `GRAIN_SEARCH_HOME` to any folder first, e.g.
`set GRAIN_SEARCH_HOME=D:\grain-dev`. Details: [engine/README.md](../engine/README.md).

Tests use a tiny fake embedder, so no model download is needed:
```
uv run pytest                        # or: uv run python -m unittest discover -s tests
uvx ruff check . && uvx ruff format --check .
```

## App (Tauri + React)
```
cd app
npm ci
npm run tauri dev       # opens the search window (Alt+Space); starts `grain serve` in ..\engine
```
Nothing about your PC is compiled into the app. When it starts, it looks for an `engine`
folder next to the program and in the folders above it, so a development build in
`app\src-tauri\target\debug\` finds `..\..\..\..\engine` wherever the repository is. To use a
different engine folder, set `engine_dir` in `%APPDATA%\GrainSearch\settings.json` or the
`GRAIN_SEARCH_ENGINE_DIR` environment variable. `uv` is looked up on PATH and in
`%USERPROFILE%\.local\bin`; set `uv` in settings.json if yours is elsewhere. The first line of
`%LOCALAPPDATA%\GrainSearch\logs\service.log` says which engine folder and uv were used.

Checks:
```
npm run typecheck
npm run build
cd src-tauri && cargo fmt --check && cargo clippy -- -D warnings && cargo test
```

A local installer (unsigned, for testing): `npm run tauri build`. It first runs
`npm run build:runtime` (`scripts/runtime_bundle.py`: the engine wheel, pinned `requirements.txt`,
`runtime.json` and your `uv` into `app\src-tauri\resources\engine\`, git-ignored), then builds the
installer into `app\src-tauri\target\release\bundle\nsis\`. Needs `uv` and internet.

**Uninstalling** (`app\src-tauri\windows\hooks.nsh`, run by the uninstaller): always deletes
`%LOCALAPPDATA%\GrainSearch\runtime`; with "Delete the application data" ticked also the model (its
folder is read from `%LOCALAPPDATA%\GrainSearch\model-location.txt`, written by the engine),
`%LOCALAPPDATA%\GrainSearch` and `%APPDATA%\GrainSearch`. Automatic updates (`/UPDATE`) keep all of it.
Installing a new version with "uninstall the old version first" counts as an uninstall: the setup
window then downloads the runtime again.

**Trying the first-run setup:** install the build, or run `tauri dev` with
`GRAIN_SEARCH_USE_RUNTIME=1` after `npm run build:runtime`. To see the setup window again, rename
`%LOCALAPPDATA%\GrainSearch\runtime` (deleting it also works; uv's downloads are cached inside it,
so a rename keeps them for later).

## Running next to an older copy
The service listens on a free port chosen at start (written to `service.json`), so it never
clashes with another program. Only one service runs per profile: a second `grain serve` says
"already running" and exits. To run a completely separate copy (e.g. for testing), give it its own
profile with `GRAIN_SEARCH_HOME=<folder>`. If another copy of the app is running with the same
hotkey (`Alt+Space`), quit it first or change `hotkey` in `settings.json`.
