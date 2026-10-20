# Tech debt

Known issues to fix later, most important first. Each item: what happens, why it matters,
how to fix it, rough effort. Each one is tracked as a GitHub issue (label `tech-debt`); this file
is the long-form background. Last updated 15 Oct 2026.

---

## 1. ~~Search service is left running when the app dies~~ · ✅ Fixed (Phase 1)

The app puts the service in a **Windows job object** with "kill on close"; Windows ends it
(and `uv`) whenever the app's process ends, however it ends. As a second line, the service
watches the app's process (`GRAIN_SEARCH_PARENT_PID`) and shuts down cleanly when it is gone,
which also covers macOS/Linux. Programs the engine opens for the user break away from the job,
so they stay open. A service started by hand (`uv run grain serve`) is not tied to anything and
the app leaves it alone, as before.

---

## 2. Model stays on the GPU even when nobody is searching  · **Medium** · ~3 h

**What happens:** the service keeps EmbeddingGemma 2 loaded all the time (~0.9 GB weights +
~0.5 GB CUDA context ≈ 1.4 GB). Needed for instant searches, but wasteful when the PC is
used for games or other GPU work for hours.

**Fix:** idle unload — after N minutes with no search and no indexing work, delete the model,
`gc.collect()`, `torch.cuda.empty_cache()` (GPU memory → ~0). Reload on the next search
(~5–10 s; the UI already shows "Starting search…"-style states, add a "Waking up…" one).
Config: `model.idle_unload_minutes` (default 10, `0` = never). Needs care around the
`LockedEmbedder` lock so a search and an unload can't race.

---

## 3. `grain status` can't see the running service  · **Medium** · ~1 h

**What happens:** `grain status` reads the database only. It doesn't say whether the service is
running, on which device, whether indexing is paused/stuck, the last error (e.g. the CUDA
out-of-memory that silently stalled indexing for hours), or GPU memory use. Diagnosing this
took several manual one-liners.

**Fix:** if `data/service.json` answers `/health`, also print the service's `/status`:
running / PID / device / indexer phase + progress / paused reason / last error. Add
`torch.cuda.memory_allocated()` / `memory_reserved()` / `max_memory_reserved()` to `/status`.
Also add `grain errors` — top error messages with counts (replaces the long one-liner) — and
`grain retry [--match TEXT]` to reset failed files to pending (replaces the SQL one-liners).

---

## 4. Images are indexed only after *all* documents  · **Medium** · ~1 h

**What happens:** a full pass sorts documents first, images last. With ~10,000 documents,
photo search stays empty for a long time on a fresh install (we had to run
`grain index --only images` by hand).

**Fix:** interleave — e.g. process new/changed images in small batches between document
batches, or do a quick images-first pass when the image index is empty. Show both counts in
the footer.

---

## 5. Stale `data/service.json` after a crash  · **Low** · ~30 min

**What happens:** if the service is killed hard, `service.json` stays behind. the app and
`grain index` check `/health`, so nothing breaks, but the file misleads anyone reading it.

**Fix:** on start-up, if `service.json` exists but its `pid` isn't alive or `/health` fails,
delete/overwrite it (already overwritten on next start — just document/clean it in `grain status`).

---

## 6. Rust shell has no tests  · **Low** · ongoing

The Tauri/Rust code (`app/src-tauri/src/lib.rs`) builds and runs, but has no tests. Add a few `#[cfg(test)]` tests for `place()` maths, `read_info`,
and the tiny HTTP client; `cargo clippy` now runs in CI.

---

## 7. Smaller items

| Item | Notes |
|---|---|
| Hugging Face "unauthenticated requests" warning on every model load | `uvx hf auth login` once, or set `HF_HUB_OFFLINE=1` in the service environment since the model is already cached |
| Scanned PDF pages are not searchable | pages with no text are detected (`meta.scanned_pages`) but not yet embedded as images (plan Phase 4) |
| `.heic` (iPhone) photos | need `pillow-heif` (`uv sync --extra heic`); not installed by default |
| `grain index` and the service share one GPU | `grain index` now refuses while the service runs; long term, everything should go through the service (`/index/rescan`) |
| Thresholds (`text_min_score`, `image_min_score`) are guesses | tune with `scripts/eval_golden.py` on real queries |
| Quick Look on Space, animations | search-window Phase C |
| Apps launcher, calculator, settings search | search-window Phase D |
