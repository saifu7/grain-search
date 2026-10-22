# Installer's engine folder (generated)

`npm run tauri build` fills this folder with `scripts/runtime_bundle.py`: the engine's wheel,
`requirements.txt`, `runtime.json` and `uv`. The installer puts it next to the app, and the
first-run setup window uses it to download Python, the libraries and PyTorch.

Only this README is committed; everything else here is generated and git-ignored.
