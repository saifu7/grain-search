"""crawl -> detect changes -> extract -> chunk -> embed -> store.

* Unchanged files (same size + mtime) are skipped instantly.
* Same content at a new path (quick hash match) is treated as a move/rename:
  the row is updated, nothing is re-embedded.
* Files that disappeared from disk are removed from the index.
* Text-like files are indexed first, then images. Within each, newest first.
* Commits every few files, so Ctrl+C loses at most a moment of work and the
  next run carries on where it stopped.

`run_index` does a full (incremental) pass over all folders; `index_paths` updates
just the given paths - the file watcher uses it for live updates.
Both accept a `checkpoint` callback that is called between files: the background
service uses it to pause (games, battery) and to stop cleanly.
"""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .chunker import chunk_segments
from .config import Config
from .crawler import Excluder, FileInfo, crawl, excluder_for, quick_hash
from .embedder import Embedder
from .extractors import extract, kind_of
from .extractors.image import TOO_SMALL
from .store import Store

COMMIT_EVERY_S = 5.0


class ModelError(RuntimeError):
    """The embedding model failed (as opposed to one unreadable file)."""


class Stopped(Exception):
    """Raised by a checkpoint to end indexing early (service shutting down)."""


@dataclass
class IndexReport:
    scanned: int = 0
    unchanged: int = 0
    moved: int = 0
    removed: int = 0
    indexed: int = 0
    empty: int = 0
    errors: int = 0
    chunks: int = 0
    images: int = 0
    seconds: float = 0.0
    error_samples: list[str] = field(default_factory=list)


@dataclass
class IndexState:
    """Live progress, read by the service's /status endpoint."""

    phase: str = "idle"  # idle | loading | scanning | indexing | paused
    done: int = 0
    total: int = 0
    current: str = ""
    paused_reason: str = ""
    last_error: str = ""
    last_run: float = 0.0


def _noop(*_a, **_k):
    return None


def _is_under(path: str, roots: list[str]) -> bool:
    p = os.path.normcase(os.path.abspath(path))
    for r in roots:
        r = os.path.normcase(os.path.abspath(r))
        if p == r or p.startswith(r.rstrip("\\/") + os.sep):
            return True
    return False


def _split(path: str) -> tuple[str, str]:
    name = os.path.basename(path)
    return name, os.path.splitext(name)[1].lower()


# -- one file at a time -------------------------------------------------------------
def _index_text_file(
    cfg: Config, store: Store, embedder: Embedder, fi: FileInfo, qh: str | None, rep: IndexReport
) -> None:
    name, ext = _split(fi.path)
    fid = store.upsert_file(fi.path, name, ext, fi.kind, fi.size, fi.mtime_ns, qh)
    try:
        ex = extract(fi.path, fi.kind)
        chunks = chunk_segments(ex.segments, cfg.chunk_chars, cfg.overlap_chars, limit=cfg.max_chunks_per_file)
    except Exception as e:  # corrupt / locked / weird file: record and move on
        store.set_status(fid, "error", f"{type(e).__name__}: {e}"[:500])
        rep.errors += 1
        if len(rep.error_samples) < 20:
            rep.error_samples.append(f"{fi.path}: {type(e).__name__}: {e}")
        return
    if not chunks:
        store.set_status(fid, "empty", None, ex.meta)
        rep.empty += 1
        return
    try:
        vecs = embedder.encode_documents([(name, c.text) for c in chunks])
    except Exception as e:  # the MODEL failed, not the file: stop, don't skip
        store.commit()
        raise ModelError(f"Embedding failed on {fi.path}: {type(e).__name__}: {e}") from e
    ids = store.add_chunks(fid, chunks)
    store.add_vectors(fid, "text", vecs, ids)
    store.set_status(fid, "indexed", None, ex.meta)
    rep.indexed += 1
    rep.chunks += len(chunks)


def _index_images(store: Store, embedder: Embedder, batch: list[tuple[FileInfo, str | None]], rep: IndexReport) -> None:
    if not batch:
        return
    try:
        vecs = embedder.encode_images([fi.path for fi, _ in batch])
    except Exception as e:
        store.commit()
        raise ModelError(f"Image embedding failed: {type(e).__name__}: {e}") from e
    for (fi, qh), v in zip(batch, vecs):
        name, ext = _split(fi.path)
        fid = store.upsert_file(fi.path, name, ext, fi.kind, fi.size, fi.mtime_ns, qh)
        meta = extract(fi.path, "image").meta
        if "error" in meta or not v.any():
            why = (
                (getattr(embedder, "image_errors", {}) or {}).get(fi.path)
                or meta.get("error")
                or "cannot open image (corrupt or unsupported format)"
            )
            if why == TOO_SMALL:  # not an error: just nothing worth searching
                store.set_status(fid, "empty", why, meta)
                rep.empty += 1
                continue
            store.set_status(fid, "error", f"image: {why}"[:500], meta)
            rep.errors += 1
            continue
        store.add_vectors(fid, "image", v[None, :])
        store.set_status(fid, "indexed", None, meta)
        rep.indexed += 1
        rep.images += 1


def _process(
    cfg: Config,
    store: Store,
    embedder: Embedder,
    todo: list[tuple[FileInfo, str | None]],
    rep: IndexReport,
    state: IndexState,
    progress,
    checkpoint,
) -> None:
    """Index a list of (file, quick-hash): documents first, then images in batches."""
    order = {"text": 0, "office": 0, "pdf": 0, "image": 1}
    todo.sort(key=lambda t: (order[t[0].kind], -t[0].mtime_ns))
    state.phase, state.done, state.total = "indexing", 0, len(todo)
    images: list[tuple[FileInfo, str | None]] = []
    last_commit = time.perf_counter()

    for fi, qh in todo:
        # Commit before every checkpoint: a checkpoint may pause for minutes (game running,
        # GPU full) and an open write transaction would lock the database for everyone else.
        store.commit()
        checkpoint()
        state.current = fi.path
        if fi.kind == "image":
            images.append((fi, qh))
            if len(images) >= cfg.model.image_batch:
                _index_images(store, embedder, images, rep)
                state.done += len(images)
                images.clear()
        else:
            _index_text_file(cfg, store, embedder, fi, qh, rep)
            state.done += 1
        if time.perf_counter() - last_commit > COMMIT_EVERY_S:
            store.commit()
            last_commit = time.perf_counter()
            progress(f"  {state.done:,}/{state.total:,} files  ({rep.chunks:,} text chunks, {rep.images:,} images)")

    _index_images(store, embedder, images, rep)
    state.done += len(images)
    store.commit()


# -- full pass --------------------------------------------------------------------------
def run_index(
    cfg: Config,
    store: Store,
    embedder: Embedder,
    progress: Callable[[str], None] = lambda s: None,
    kinds: set[str] | None = None,
    retry_errors: bool = False,
    checkpoint: Callable[[], None] = _noop,
    state: IndexState | None = None,
) -> IndexReport:
    t0 = time.perf_counter()
    rep = IndexReport()
    state = state or IndexState()
    store.check_model(embedder.model_id, embedder.dim)
    # Load the model before touching any file: a model problem must stop the run
    # with its real error, not be recorded as an error on every single file.
    state.phase = "loading"
    progress(f"Loading {embedder.model_id} ...")
    embedder.warm_up()
    progress(f"Model ready on {getattr(embedder, 'device_desc', '?')}")
    if not cfg.model.images:
        kinds = (kinds or {"text", "pdf", "office", "image"}) - {"image"}

    # 1) crawl + change detection --------------------------------------------
    state.phase = "scanning"
    known = store.all_files()
    seen: set[str] = set()
    todo: list[FileInfo] = []
    progress("Scanning folders...")
    for fi in crawl(cfg):
        if kinds and fi.kind not in kinds:
            continue
        rep.scanned += 1
        seen.add(fi.path)
        row = known.get(fi.path)
        if (
            row
            and row.size == fi.size
            and row.mtime_ns == fi.mtime_ns
            and row.status != "pending"
            and not (retry_errors and row.status == "error")
        ):
            rep.unchanged += 1
            continue
        todo.append(fi)
        if rep.scanned % 2000 == 0:
            progress(f"Scanning... {rep.scanned:,} files")
            store.commit()
            checkpoint()

    # 2) moves/renames + deletions -------------------------------------------
    missing = {p: r for p, r in known.items() if p not in seen and _is_under(p, cfg.roots) and not os.path.exists(p)}
    by_hash = {(r.qhash, r.size): r for r in missing.values() if r.qhash and r.status != "pending"}
    really_todo: list[tuple[FileInfo, str | None]] = []
    for fi in todo:
        try:
            qh = quick_hash(fi.path, fi.size)
        except OSError as e:
            rep.errors += 1
            rep.error_samples.append(f"{fi.path}: {e}")
            continue
        row = known.get(fi.path)
        if row and row.qhash == qh and row.status not in ("pending", "error"):
            store.touch_file(row.id, fi.size, fi.mtime_ns)  # touched but not changed
            rep.unchanged += 1
            continue
        old = by_hash.pop((qh, fi.size), None) if not row else None
        if old:
            name, ext = _split(fi.path)
            store.move_file(old.id, fi.path, name, ext, fi.mtime_ns)
            missing.pop(old.path, None)
            rep.moved += 1
            continue
        really_todo.append((fi, qh))
    for r in missing.values():
        store.delete_file(r.id)
        rep.removed += 1
    store.commit()

    # 3) extract + embed ------------------------------------------------------
    if really_todo:
        progress(f"Indexing {len(really_todo):,} new/changed files with {embedder.model_id} ...")
    _process(cfg, store, embedder, really_todo, rep, state, progress, checkpoint)
    rep.seconds = time.perf_counter() - t0
    state.phase, state.current, state.last_run = "idle", "", time.time()
    return rep


# -- just these paths (file watcher) ----------------------------------------------------
def _file_info(cfg: Config, path: str, excluded: Excluder) -> FileInfo | None:
    """FileInfo if `path` is an indexable file under the configured rules, else None."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or not _is_under(path, cfg.roots) or excluded(path):
        return None
    name = os.path.basename(path)
    if cfg.skip_hidden and (name.startswith(".") or getattr(st, "st_file_attributes", 0) & 0x6):
        return None
    # skip anything inside a hidden/excluded folder too
    parent = os.path.dirname(path)
    while _is_under(parent, cfg.roots):
        if excluded(parent, is_dir=True) or (cfg.skip_hidden and os.path.basename(parent).startswith(".")):
            return None
        nxt = os.path.dirname(parent)
        if nxt == parent:
            break
        parent = nxt
    kind = kind_of(name)
    if kind is None or (kind == "image" and not cfg.model.images):
        return None
    limit = (cfg.max_image_mb if kind == "image" else cfg.max_doc_mb) * 1024 * 1024
    if st.st_size == 0 or st.st_size > limit:
        return None
    return FileInfo(path, st.st_size, st.st_mtime_ns, kind)


def index_paths(
    cfg: Config,
    store: Store,
    embedder: Embedder,
    paths: Iterable[str],
    moves: Iterable[tuple[str, str]] = (),
    checkpoint: Callable[[], None] = _noop,
    state: IndexState | None = None,
    progress: Callable[[str], None] = lambda s: None,
) -> IndexReport:
    """Bring the index up to date for specific paths (files or folders) that changed."""
    t0 = time.perf_counter()
    rep = IndexReport()
    state = state or IndexState()
    excluded = excluder_for(cfg)
    paths = list(paths)

    # 1) renames/moves reported by the watcher: update rows, no re-embedding
    for src, dst in moves:
        row = store.get_file(src)
        if row is not None and os.path.isfile(dst) and _file_info(cfg, dst, excluded):
            if store.get_file(dst) is not None:  # destination was already indexed: replace it
                store.delete_file(store.get_file(dst).id)
            name, ext = _split(dst)
            store.move_file(row.id, dst, name, ext, os.stat(dst).st_mtime_ns)
            rep.moved += 1
        else:
            paths += [src, dst]
        # folder renamed: move every file under it
        if row is None and os.path.isdir(dst):
            prefix = src.rstrip("\\/") + os.sep
            for old_path, r in list(store.files_under(prefix).items()):
                new_path = os.path.join(dst, old_path[len(prefix) :])
                if os.path.isfile(new_path):
                    name, ext = _split(new_path)
                    store.move_file(r.id, new_path, name, ext, os.stat(new_path).st_mtime_ns)
                    rep.moved += 1
                else:
                    store.delete_file(r.id)
                    rep.removed += 1

    # 2) expand folders, classify files
    todo: list[tuple[FileInfo, str | None]] = []
    seen: set[str] = set()

    def consider(p: str):
        if p in seen:
            return
        seen.add(p)
        rep.scanned += 1
        fi = _file_info(cfg, p, excluded)
        row = store.get_file(p)
        if fi is None:
            if row is not None and not os.path.exists(p):
                store.delete_file(row.id)
                rep.removed += 1
            return
        if row and row.size == fi.size and row.mtime_ns == fi.mtime_ns and row.status != "pending":
            rep.unchanged += 1
            return
        try:
            qh = quick_hash(p, fi.size)
        except OSError:
            return  # still being written / locked: the next event will retry
        if row and row.qhash == qh and row.status not in ("pending", "error"):
            store.touch_file(row.id, fi.size, fi.mtime_ns)
            rep.unchanged += 1
            return
        todo.append((fi, qh))

    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            sub = Config(**{**cfg.__dict__, "roots": [p]})
            if _is_under(p, cfg.roots) and not excluded(p, is_dir=True):
                for fi in crawl(sub):
                    consider(fi.path)
        elif os.path.exists(p):
            consider(p)
        else:  # deleted file or folder
            row = store.get_file(p)
            if row is not None:
                store.delete_file(row.id)
                rep.removed += 1
            for r in store.files_under(p.rstrip("\\/") + os.sep).values():
                store.delete_file(r.id)
                rep.removed += 1
    store.commit()

    if todo:
        store.check_model(embedder.model_id, embedder.dim)
        _process(cfg, store, embedder, todo, rep, state, progress, checkpoint)
        state.phase, state.current = "idle", ""
    rep.seconds = time.perf_counter() - t0
    return rep
