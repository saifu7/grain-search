<div align="center">

<img src="app/src-tauri/icons/128x128.png" width="96" alt="">

# Grain Search

**Search your files and photos by what they mean, not just their names.**
Type a sentence and open the document that contains it. Describe a picture and find the photo.
Everything runs on your own PC.

[![CI](https://github.com/saifu7/grain-search/actions/workflows/ci.yml/badge.svg)](https://github.com/saifu7/grain-search/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/saifu7/grain-search?include_prereleases&sort=semver)](https://github.com/saifu7/grain-search/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

</div>

> **Status: early development (alpha).** The search engine and the search window work, but there is
> no one-click installer yet. That arrives with `v0.2.0-beta`; see the [roadmap](docs/roadmap.md).
> Until then, [build from source](docs/development.md).

---

## What it does

| You type | You get |
|---|---|
| `the invoice from the plumber last spring` | the PDF that says so, even if those words aren't in its name |
| `"quarterly targets"` | only documents containing that exact phrase |
| `dog asleep on a red sofa` | photos of that, from your Pictures folder |
| `budget xlsx` | file names, instantly |

- **Press `Alt+Space` anywhere** and a search bar like macOS Spotlight appears. Results show as you type, with a preview.
- **Results are labelled honestly:**
  - **Contains your text**: the words really are in the file.
  - **Related by meaning**: similar, but the exact words aren't in it.
- **Understands documents**: PDF, Word, PowerPoint, Excel, text, Markdown, code.
- **Understands images**: JPEG, PNG, WebP, GIF, BMP, TIFF (HEIC optional).
- **Stays up to date**: new and changed files are picked up in the background. Indexing pauses on battery and during full-screen games.
- **Private by design**: no account, no telemetry, no cloud. Your files and the index never leave your PC.

## How it works

Grain Search uses Google's [EmbeddingGemma 2](https://huggingface.co/google/embeddinggemma-2), a
multimodal embedding model that maps text *and* images into the same "meaning space".

1. Files in the folders you choose are split into passages and turned into vectors once.
2. Those vectors are stored in a local SQLite index.
3. Each search combines three signals:
   - exact text matching (SQLite FTS5);
   - file-name matching;
   - meaning (vector similarity, fused with Reciprocal Rank Fusion).

```
 app/      Windows app (Tauri 2 + React): hotkey, search window, tray
   │  HTTP on 127.0.0.1 with a per-session token
 engine/   Python service: crawler · extractors · EmbeddingGemma 2 · SQLite index · hybrid search
```

Full picture: **[architecture](docs/architecture.md)**. Background: [engine design](docs/design/engine.md) · [search window design](docs/design/search-window.md).

## Requirements

| | Minimum | Recommended |
|---|---|---|
| OS | Windows 10 (64-bit) | Windows 11 |
| RAM | 8 GB | 16 GB |
| GPU | none (CPU works; indexing is slower) | NVIDIA with 6 GB+ VRAM |
| Disk | ~5 GB for the runtime and model, plus the index | SSD |

## Install

Download the latest `GrainSearch-x.y.z-x64-setup.exe` from
**[Releases](https://github.com/saifu7/grain-search/releases)**. The first one-click installer is planned for `v0.2.0-beta`.

Verify a download against `SHA256SUMS.txt`, attached to the same release:

```powershell
Get-FileHash .\GrainSearch-0.2.0-x64-setup.exe -Algorithm SHA256
```

You can also check that a file was built by this repository's CI:
`gh attestation verify GrainSearch-0.2.0-x64-setup.exe --repo saifu7/grain-search`.

## Build from source

See **[docs/development.md](docs/development.md)**. In short: install uv, Node 22 and Rust, then run:

```
cd engine && uv sync --locked && uv run grain index
cd ../app && npm ci && npm run tauri dev
```

## Contributing

Bug reports, ideas and pull requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) and
the [Code of Conduct](CODE_OF_CONDUCT.md). Security issues: see [SECURITY.md](SECURITY.md); please
don't open public issues for them.

## Licence

- **Grain Search code**: [MIT](LICENSE).
- **EmbeddingGemma 2 model**: not part of this repository. It is downloaded from Hugging Face under
  Google's own terms, which you accept when you download it. See the
  [model card](https://huggingface.co/google/embeddinggemma-2).
- **Third-party components**: keep their own licences.

Grain Search is an independent project, not affiliated with Google, Apple or voidtools.
