# Grain Search engine

The Python half of [Grain Search](../README.md): crawler, text and image extractors,
EmbeddingGemma 2, the SQLite index, hybrid search, the `grain` command line and the background
service (`grain serve`) that the Windows app talks to. Everything runs locally.

## Setup (uv)

```powershell
cd engine
setx UV_CACHE_DIR "D:\cache\uv"       # optional: keep uv's downloads off a full C: drive
uv sync --locked                        # installs everything, incl. the CUDA build of PyTorch
uv run grain folders add "%USERPROFILE%\Documents"   # choose what to index (nothing is built in)
uv run grain folders add D:\Photos
uv run grain config                     # shows where settings and the index are kept
```

Settings live in `%APPDATA%\GrainSearch\config.json` and the index in
`%LOCALAPPDATA%\GrainSearch\index\`. Manage folders and exclusions with
`grain folders [add|remove] FOLDER` and `grain exclude [add|remove] FOLDER_OR_PATTERN`
(e.g. `*.log`). Every option is described in [config.example.yaml](config.example.yaml); the same
keys go into `config.json`. Set `GRAIN_SEARCH_HOME=D:\some\folder` to keep everything in one
folder instead (a portable or test profile).

PyTorch is installed from the CUDA 12.8 build: it uses your NVIDIA GPU when there is one and
runs on the CPU otherwise. Check with:
`uv run python -c "import torch; print(torch.__version__, torch.cuda.is_available())"`

The first run downloads EmbeddingGemma 2 from Hugging Face (about 1.5 GB, once).
If it asks you to log in: `uvx hf auth login`.

## Use

```powershell
uv run grain index                         # first run indexes everything; later runs only changes
uv run grain                               # interactive: type, see results, type a number to open
uv run grain search "the invoice was paid late because of the bank holiday"
uv run grain search "dog sleeping on a red sofa" --open    # open the top hit straight away
uv run grain open 2                        # open result 2 of the last search
uv run grain open 2 -r                     # show it in Explorer instead
uv run grain status
```

Results come in two groups: **Contains your text** (exact phrase, all words, or file name) and
**Related by meaning** (similar topic, but your words are *not* in the file). Related results are
hidden when they're weak; tune `text_min_score` (under `search` in `config.json`) to see more or fewer.

**Search tips**
| Type | Meaning |
|---|---|
| `=some text` | **only** files that contain the text (no "related" results) |
| `~some idea` | **only** search by meaning (files about this, whatever the wording) |
| `"exact phrase"` | must contain these words in this order |
| `type:img` / `type:doc` | only pictures / only documents |
| `ext:pdf,docx` | only these file types |
| `in:D:\work` | only inside this folder |
| `after:2025-01-01` `before:2025-06-30` | by date (photo date taken, else modified) |

Opening a PDF at the right page needs [SumatraPDF](https://www.sumatrapdfreader.org/);
opening code/text at the right line needs VS Code's `code` command. Otherwise the default app opens.

## Background service (for the Windows app)

```powershell
uv run grain serve
```

Keeps EmbeddingGemma 2 loaded (GPU), indexes in the background and re-indexes files the moment
they change (pauses automatically while a game / full-screen app runs or on battery).
Listens on `127.0.0.1:8766` only; the address and a secret token are written to
`%LOCALAPPDATA%\GrainSearch\service.json`, the log to `%LOCALAPPDATA%\GrainSearch\logs\`.
Stop it with Ctrl+C.

Try it from another terminal:
```powershell
$s = Get-Content "$env:LOCALAPPDATA\GrainSearch\service.json" | ConvertFrom-Json
curl.exe -N -H "Authorization: Bearer $($s.token)" "$($s.url)/search?q=bank%20holiday"
curl.exe -H "Authorization: Bearer $($s.token)" "$($s.url)/status"
```

| Endpoint | What it does |
|---|---|
| `GET /search?q=&limit=` | Server-Sent Events: `fast` (names + text matches) then `full` (+ meaning, photos); `&stream=0` for plain JSON |
| `GET /preview?path=&q=&page=&line=` | file details + highlighted lines around the match |
| `GET /thumb?path=&size=` | JPEG thumbnail (photos, PDFs), cached |
| `GET /page?path=&page=&width=` | PNG of a PDF page |
| `POST /open {path, mode: open/reveal, query}` | opens the file; files you open rise in future results |
| `GET /status` · `POST /index/pause` · `/index/resume` · `/index/rescan` | indexing control |

Only files that are in the index can be previewed or opened.

## Try the model on your own photos (optional)

```powershell
uv run python scripts\spike_embeddinggemma2.py --images "$env:USERPROFILE\Pictures" --limit 100 `
  --query "a dog sleeping on a sofa" --query "sunset at the beach"
```
Shows speed (images/sec) and whether describing a photo finds it.

## Measure quality

Copy `tests\golden_queries.example.yaml` → `tests\golden_queries.yaml`, add real queries, then
`uv run python scripts\eval_golden.py --show-misses`.

## Tests

```powershell
uv run python -m unittest discover -s tests -v
```
(Uses a tiny fake embedder, so no model download is needed.)
