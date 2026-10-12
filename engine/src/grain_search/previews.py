"""Quick Look data for the Grain Search window: highlighted text excerpts, file details,
cached thumbnails and rendered PDF pages."""

from __future__ import annotations

import hashlib
import io
import os
import re
from datetime import datetime
from pathlib import Path

from .extractors import Segment, extract
from .extractors.image import load_for_embedding
from .store import FileRow

_TOKEN = re.compile(r"\w+", re.UNICODE)
CONTEXT_LINES = 6
MAX_LINE_CHARS = 400


def _needles(query: str) -> tuple[list[str], list[str]]:
    """(phrases, words) to highlight. Quoted parts are phrases; the whole query is one too."""
    q = query.strip().lstrip("=~")
    q = re.sub(r"\b(type|ext|in|after|before):(\"[^\"]*\"|\S+)", " ", q)
    phrases = [p for p in re.findall(r'"([^"]+)"', q) if p.strip()]
    plain = " ".join(q.replace('"', " ").split())
    if plain and plain not in phrases:
        phrases.append(plain)
    words = sorted({w.lower() for w in _TOKEN.findall(plain) if len(w) > 2}, key=len, reverse=True)
    return phrases, words


def _highlights(line: str, phrases: list[str], words: list[str]) -> list[list[int]]:
    """[[start, end], ...] character ranges to highlight in `line` (phrases first, then words)."""
    low, spans = line.lower(), []
    for needle in [p.lower() for p in phrases] + words:
        start = 0
        while needle and (i := low.find(needle, start)) != -1:
            if not any(a <= i < b or a < i + len(needle) <= b for a, b in spans):
                spans.append([i, i + len(needle)])
            start = i + len(needle)
    return sorted(spans)


def _segments(row: FileRow, page: int | None) -> list[Segment]:
    segs = extract(row.path, row.kind).segments
    if page is not None:
        on_page = [s for s in segs if s.page == page]
        segs = on_page or segs
    return segs


def text_excerpt(row: FileRow, query: str, page: int | None = None, line: int | None = None) -> dict:
    """Lines around the first match (or around `line`), with highlight ranges."""
    phrases, words = _needles(query)
    best = None  # (segment, line index)
    for seg in _segments(row, page):
        lines = seg.text.splitlines() or [""]
        if line is not None and seg.line is not None and seg.page is None:
            idx = max(0, min(len(lines) - 1, line - seg.line))
            best = (seg, lines, idx)
            break
        low = [ln.lower() for ln in lines]
        hit = next((i for i, ln in enumerate(low) for p in phrases if p.lower() in ln), None)
        if hit is None:
            hit = next((i for i, ln in enumerate(low) if any(w in ln for w in words)), None)
        if hit is not None:
            best = (seg, lines, hit)
            break
        if best is None:
            best = (seg, lines, 0)
    if best is None:
        return {"lines": [], "page": page}
    seg, lines, idx = best
    lo, hi = max(0, idx - CONTEXT_LINES), min(len(lines), idx + CONTEXT_LINES + 1)
    out = []
    for i in range(lo, hi):
        txt = lines[i][:MAX_LINE_CHARS]
        out.append(
            {
                "n": (seg.line + i) if seg.line is not None else None,
                "text": txt,
                "highlights": _highlights(txt, phrases, words),
                "match": i == idx,
            }
        )
    return {"lines": out, "page": seg.page}


def details(row: FileRow) -> dict:
    try:
        st = os.stat(row.path)
        size, mtime = st.st_size, st.st_mtime
    except OSError:
        size, mtime = row.size, row.mtime_ns / 1e9
    return {
        "path": row.path,
        "name": row.name,
        "folder": os.path.dirname(row.path),
        "ext": row.ext,
        "kind": row.kind,
        "size": size,
        "modified": datetime.fromtimestamp(mtime).isoformat(timespec="minutes"),
        "meta": row.meta,
    }


def preview(row: FileRow, query: str = "", page: int | None = None, line: int | None = None) -> dict:
    out = details(row)
    if row.kind == "image":
        out["excerpt"] = None
    else:
        try:
            out["excerpt"] = text_excerpt(row, query, page, line)
        except Exception as e:  # unreadable right now (locked, deleted...)
            out["excerpt"] = None
            out["error"] = f"{type(e).__name__}: {e}"
    return out


# -- images -------------------------------------------------------------------------
def _cache_file(cache_dir: Path, row: FileRow, size: int, extra: str = "") -> Path:
    key = f"{row.path}|{row.mtime_ns}|{row.size}|{size}|{extra}".encode()
    return cache_dir / (hashlib.blake2b(key, digest_size=12).hexdigest() + ".jpg")


def _render_pdf_page(path: str, page: int, width: int):
    """PIL image of one PDF page, or None if PyMuPDF isn't available / the page can't render."""
    try:
        import fitz  # PyMuPDF
        from PIL import Image
    except ImportError:
        return None
    try:
        with fitz.open(path) as doc:
            if doc.needs_pass or not (1 <= page <= len(doc)):
                return None
            pg = doc[page - 1]
            zoom = max(0.1, width / max(1.0, pg.rect.width))
            pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    except Exception:
        return None


def thumbnail(row: FileRow, size: int, cache_dir: Path) -> bytes | None:
    """JPEG thumbnail (longest side = size) for images and PDFs; cached on disk."""
    size = max(32, min(int(size), 1024))
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = _cache_file(cache_dir, row, size)
    if cached.exists():
        return cached.read_bytes()
    if row.kind == "image":
        img = load_for_embedding(row.path)
    elif row.kind == "pdf":
        img = _render_pdf_page(row.path, 1, size * 2)
    else:
        img = None
    if img is None:
        return None
    img = img.convert("RGB")
    img.thumbnail((size, size))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82, optimize=True)
    data = buf.getvalue()
    try:
        cached.write_bytes(data)
    except OSError:
        pass
    return data


def pdf_page_png(row: FileRow, page: int, width: int) -> bytes | None:
    img = _render_pdf_page(row.path, page, max(200, min(int(width), 2000)))
    if img is None:
        return None
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
