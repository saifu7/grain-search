# Grain Search — PLAN

> Original design notes, kept for reference. Current plans: [roadmap](../roadmap.md).

> A private, local "search everything" app for Windows, powered by Google's
> **EmbeddingGemma 2** (`google/embeddinggemma-2`, released 6 Oct 2026, Apache 2.0).

---

## 1. Goal

Type anything → get the file.

| You type | You get |
|---|---|
| A sentence (exact or paraphrased): *"the invoice was paid late because of the bank holiday"* | The file containing it, the matching snippet, page/line — **Enter opens it** |
| A description of a picture: *"my dog sleeping on a red sofa at night"* | The matching photos as thumbnails — **Enter opens it** |
| A filename fragment: *"budget 2025 xlsx"* | Classic "Everything"-style filename hits |
| Later: *"the part of the video where the cake falls"*, *"voice note about the meeting"* | Video moment / audio clip |

Everything runs on your PC. No file content leaves the machine.

---

## 2. Why EmbeddingGemma 2 is the right model

EmbeddingGemma **1** was text-only — finding a picture from a description would have
needed a second model (captioner or CLIP/SigLIP). **Version 2 is natively multimodal**:
text, code, images, video and audio all land in **one shared 768-d vector space**, so a
text query can be compared directly against an image embedding. One model does both jobs.

| Need | EmbeddingGemma 2 feature |
|---|---|
| Sentence → document | Strong multilingual text retrieval (100+ languages), 8,192-token context |
| Description → picture | Images embedded directly in the same space as text |
| Runs on a laptop | 740M total, but encoders load selectively: **text + image = 440M** |
| Scanned PDFs / screenshots | Trained on visual-document retrieval (MMEB VisDoc) → page images can be embedded as images |
| Code files | ~14% better code retrieval than v1, has a `CodeRetrieval` task prompt |
| Storage | Matryoshka dims 768 / 512 / 256 / 128 (near-lossless down to 256; avoid 128 for images) |

### Rules from the model card the code MUST follow (enforced in `embedder.py`)

1. **Queries** (text) → `prompt_name="SearchQuery"` → `task: search result | query: {q}`
2. **Text chunks** → `title: {filename} | text: {chunk}` (format manually; `prompt_name="Document"` only gives `title: none`)
3. **Images / video / audio** → passed with **no prefix**: `model.encode({"image": path})`
4. **Never `float16`** (silently gives NaN/garbage). `bfloat16` on GPUs that support it, `float32` on CPU.
5. If truncating dims → `truncate_dim=..., normalize_embeddings=True`, and **queries and documents must use the same dim**.
6. Load only what we need: `config_kwargs={"audio_config": None}` for text+image.
7. **Scores are not calibrated across modalities.** In Google's own example a correct
   text→image match scores ~0.64 — far lower than a typical text→text match. So we
   **rank documents and images separately**, never in one mixed list by raw score.

---

## 3. Architecture

```
                 ┌──────────────────── Indexer (background) ─────────────────────┐
 Your folders →  │ Crawler → Extractors → Chunker → Embedder (EmbeddingGemma 2) │
 (Documents,     │    ↑ watchdog: live file changes                              │
  Pictures, D:\) └───────────────────────────────┬───────────────────────────────┘
                                                 ▼
                                    data/index.db  (one SQLite file)
                                    ├─ files       path, size, mtime, hash, kind, EXIF…
                                    ├─ chunks      file_id, text, page, line, offsets
                                    ├─ chunks_fts  FTS5 → exact words & "phrases"
                                    ├─ files_fts   FTS5 trigram → filename/path search
                                    └─ vectors     float16 blobs (text chunks + images)
                                                   → numpy dot-product search, cached in vectors-*.npz
                                                 ▲
 Search window ──► local API (127.0.0.1) ──► Search (hybrid + per-modality ranking) ──► Opener
 (hotkey)                                                                   (open / reveal / page)
```

One long-running local service keeps the model loaded in memory (loading takes seconds,
a query takes milliseconds). The search window and the CLI are thin clients.

---

## 4. Tech stack

| Layer | Default | Why | Swap later if… |
|---|---|---|---|
| Language | Python 3.11+, managed with **uv** | sentence-transformers ecosystem | — |
| Embeddings | `sentence-transformers` + `google/embeddinggemma-2` | official, documented path | need more speed → ONNX / LiteRT |
| Catalog + exact search | SQLite + FTS5 | ships with Python, single file, phrase queries | — |
| Vector search | numpy brute force over vectors stored in SQLite (+ `.npz` cache) | no native extension, ~25 ms for 200k vectors | > ~1M vectors → sqlite-vec / LanceDB |
| PDF | PyMuPDF | fast text per page + page rendering | — |
| Office | python-docx, python-pptx, openpyxl | | |
| Images | Pillow + pillow-heif (iPhone HEIC) | | |
| File watching | watchdog | | huge drives → Windows USN journal |
| API | FastAPI + uvicorn | | |
| UI | HTML/JS in a `pywebview` window + global hotkey | fast to build, looks good | Tauri for a polished installer |
| CLI | argparse (`grain`) | no extra dependency | |

---

## 5. Indexing pipeline

### 5.1 What gets scanned (`config.yaml`)
- **roots**: e.g. Documents, Desktop, Downloads, Pictures, `D:\work`
- **excludes**: `C:\Windows`, Program Files, `AppData`, `$Recycle.Bin`,
  `System Volume Information`, `node_modules`, `.git`, `venv`/`.venv`, `__pycache__`, build/dist
- size limits (default 50 MB docs / 40 MB images), skip hidden/system files

### 5.2 Change detection (so re-index is fast)
- `files` table keeps `path, size, mtime_ns, qhash`.
- `size`+`mtime` unchanged → skip. Changed → quick hash (xxhash of size + first/last 64 KB).
- Same hash at a new path → it was **moved/renamed** → update path, no re-embedding.
- In DB but gone from disk → delete rows + vectors.

### 5.3 Extractors (file → segments)

| Kind | Extensions | What we keep |
|---|---|---|
| text / code | .txt .md .csv .log .json .yaml .py .js .ts .java .c .cpp .cs .go .rs .html … | text + line numbers |
| pdf | .pdf | text per page. Page with almost no text (scanned) → render page → embed as **image** |
| office | .docx .pptx .xlsx | paragraphs / slide text (slide = page) / sheet rows |
| image | .jpg .jpeg .png .webp .bmp .gif .heic | the image + EXIF (date taken, camera, GPS) + 256 px thumbnail |
| video *(Phase 5)* | .mp4 .mov .mkv | ~30 s windows at 1 fps → 1 vector each, with start time |
| audio *(Phase 5)* | .mp3 .wav .m4a | ~30 s mono 16 kHz windows (25 tokens/s) |

Filename + folder path always go into `files_fts` so plain filename search works for every file.

### 5.4 Chunking (text)
- ~350 tokens per chunk, ~50 overlap, split on paragraph → sentence boundaries.
- Keep `page`, `line`, `char_start/char_end` → open at the right place + highlight snippet.

### 5.5 Embedding
- Batches: ~32 text chunks, ~8 images (tune with the Phase 0 benchmark).
- Vision budget per image: **280 tokens** (default); 140 for faster CPU indexing; 70–1120 allowed.
- Dimension: **768** default; 512 if the index gets large (almost no quality loss).
- Order of work: text files first (cheap, high value) → images → newest files first.
- Resumable: every file has a `status` (pending / indexed / error / skipped).

---

## 6. Search pipeline

1. **Parse query** — optional filters: `type:img`, `type:doc`, `ext:pdf`, `in:D:\work`,
   `after:2025-01-01`, and `"quoted exact phrase"`.
2. **Encode once** with the `SearchQuery` prompt (tens of ms).
3. **Run three lookups:**
   - **A. Exact** — FTS5 phrase / BM25 over chunk text + filenames.
   - **B. Semantic text** — nearest text-chunk vectors (top 200).
   - **C. Semantic images** — nearest image vectors (top 50).
4. **Documents list** = Reciprocal Rank Fusion of A + B (k = 60). An exact phrase hit is
   **pinned to the top** — this is what makes "type a sentence → open that file" reliable.
   Chunks collapse to files (best snippet + up to 2 more).
5. **Images list** = ranked by C's own score, shown above a per-modality threshold
   (tuned on your golden queries, §9).
6. **Top hit** — if one result clearly dominates (exact phrase, or image score far above #2),
   it's highlighted; **Enter opens it immediately**.

**Opening (Windows):** Enter → `os.startfile(path)` (default app).
Ctrl+Enter → `explorer /select,"path"` (show in folder).
Nice-to-have: open at location — VS Code `code -g file:line`, SumatraPDF `-page N`.

---

## 7. UI

Spotlight-style floating window:
- **Ctrl+Alt+Space** shows/hides it from anywhere.
- One search box, live results (debounced ~150 ms), full keyboard navigation.
- **Documents** section: icon · filename · folder · snippet with highlights · page/line.
- **Images** section: thumbnail grid; hover = bigger preview.
- Status bar: `Indexed 48,210 files · 3,120 queued · GPU`.
- Settings: folders to index, excludes, rebuild index.

---

## 8. Roadmap

### Phase 0 — Spike (½ day) — optional check
- `uv sync`, then run `scripts/spike_embeddinggemma2.py` on ~100 of
  your photos and a few text files with 5–10 descriptive queries.
- It prints top-5 matches + timings (model load, images/sec, query latency) and compares the
  query **with vs without** the `SearchQuery` prompt for images.
- **Decide:** CPU float32 vs GPU bf16 · dim 768 vs 512 · vision budget 140 vs 280 · prompt or not for image queries.
- **Exit:** the right photo is top-5 for most queries; you know how long a full index will take.

### Phase 1 — Text MVP (CLI) ✅ built
- `config.py`, `crawler.py`, text/pdf/docx extractors, `chunker.py`, `store.py`,
  `embedder.py`, `indexer.py`, `search.py` (hybrid), `opener.py`, `cli.py`.
- Commands: `grain index`, `grain search "…"`, `grain open 1`, `grain status`.
- **Exit:** exact sentence → right file **top-1 on 100%** of golden queries; paraphrase → top-3 ≥ 80%.

### Phase 2 — Images ✅ built
- Image extractor (EXIF, thumbnails, HEIC), `vec_media`, image section in results.
- **Exit:** description → right photo **top-5 ≥ 80%** on ~30 golden queries over your own photos.

### Phase 3 — Service + window ← **next**
- FastAPI service, watchdog live updates, pywebview window, global hotkey, thumbnails.
- Auto-start with Windows (Startup folder shortcut).

### Phase 4 — Quality
- Golden-query eval in `tests/`, tune chunk size / RRF / thresholds.
- Scanned PDF pages as images, pptx/xlsx, `CodeRetrieval` prompt for code files,
  "find similar" (image → image, file → file).

### Phase 5 — Extras
- Video moments + audio/voice notes (load the audio encoder: full 740M model).
- Duplicate photo finder, optional OCR for screenshots, PyInstaller installer.

---

## 9. Measuring quality

`tests/golden_queries.yaml` — 30–60 real queries you'd actually type, each with the file
you expect. `tests/test_search_quality.py` reports **top-1**, **recall@5** and **MRR** per kind
(doc / image). Re-run after every change to chunking, thresholds, dims or prompts.

---

## 10. Sizing (estimates — confirm in Phase 0)

| Item | Estimate |
|---|---|
| Model RAM, text+image (440M params) | ~0.9 GB bf16 · ~1.8 GB float32 |
| One vector at 768-d float32 | 3 KB (2 KB at 512-d) |
| 200k text chunks | ~600 MB vectors at 768-d |
| 50k photos | ~150 MB vectors + thumbnails |
| Indexing speed | depends heavily on GPU vs CPU — measure with the spike, then extrapolate |

---

## 11. Risks & mitigations

| Risk | Mitigation |
|---|---|
| First full index is slow on CPU | Text first, images in background, newest first, vision budget 140, resumable queue |
| Embeddings alone miss an exact sentence | FTS5 phrase search in the hybrid; exact hits pinned to top |
| Text and image scores not comparable | Separate ranking + separate thresholds per modality |
| Photos of text (receipts, screenshots) | VisDoc-trained image embeddings; optional OCR later |
| float16 NaNs | dtype chosen in one place (`embedder.py`), float16 never used |
| Locked / huge / corrupt files | size limits, per-file try/except, `status=error` + message, skip list |
| Index grows past ~1M vectors | Swap numpy search for sqlite-vec / LanceDB behind `store.vector_matrix()` |
| Privacy | API binds to 127.0.0.1 only; index lives in `data/`; no telemetry |
| Model download | One-time from Hugging Face; if prompted, log in with the HF CLI |

---

## 12. Open decisions (answer these before Phase 1)

1. **Which folders first?** Suggested: Documents, Desktop, Downloads, Pictures, `D:\work`.
2. **Do you have an NVIDIA GPU?** Changes dtype (bf16 vs float32) and indexing speed a lot.
3. **v1 window:** pywebview desktop window (recommended) or just a browser tab?
4. **Video/audio:** in v1, or Phase 5?

---

## 13. Folder map

```
grain-search/
├── PLAN.md · README.md · pyproject.toml (uv) · requirements.txt · config.example.yaml
├── scripts/
│   ├── spike_embeddinggemma2.py   Phase 0: does it work on my PC? how fast?
│   └── eval_golden.py             quality on your own queries (top-1, recall@5, MRR)
├── src/grain_search/
│   ├── config.py               load config.yaml
│   ├── embedder.py             EmbeddingGemma 2 wrapper (all model-card rules live here)
│   ├── crawler.py              walk folders, excludes, hidden files, quick hash
│   ├── extractors/             text · pdf · office (docx/pptx/xlsx) · image (EXIF)
│   ├── chunker.py              ~350-token chunks with page/line
│   ├── store.py                SQLite: files, chunks, FTS5, names (trigram), vectors
│   ├── indexer.py              crawl → changes/moves/deletes → extract → embed → store
│   ├── search.py               hybrid search, RRF, filters, per-modality ranking
│   ├── opener.py               open / reveal in Explorer / PDF page / code line
│   └── cli.py                  `grain index | search | open | status | reset` + interactive
├── tests/                      pipeline tests (fake embedder) + model-contract tests
└── data/                       (runtime, git-ignored) index.db, vectors-*.npz, last results
```

Phase 3 adds `watcher.py`, `api.py` and `ui/`.
