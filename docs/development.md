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
copy config.example.yaml config.yaml      # choose your folders (this file is git-ignored)
uv run grain index                        # first run downloads EmbeddingGemma 2 (~1.5 GB)
uv run grain search "dog on a red sofa"
```
Check that the GPU is used: `uv run python -c "import torch; print(torch.cuda.is_available())"`.
The index lives in `engine\data\` (git-ignored). Details: [engine/README.md](../engine/README.md).

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
The app looks for the engine in `..\engine` during development. Override with
`settings.json` (`project_dir`) or the `GRAIN_SEARCH_ENGINE_DIR` environment variable.

Checks:
```
npm run typecheck
npm run build
cd src-tauri && cargo fmt --check && cargo clippy -- -D warnings && cargo test
```

A local installer (unsigned, for testing): `npm run tauri build`. The output goes to
`app\src-tauri\target\release\bundle\nsis\`.

## Running next to an older copy
The development service uses port **8766**. If another copy of the app is running with the same
hotkey (`Alt+Space`), quit it first or change `hotkey` in `settings.json`.
