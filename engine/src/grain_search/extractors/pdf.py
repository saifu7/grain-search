"""PDF text per page. Prefers PyMuPDF (fast); falls back to pypdf."""

from __future__ import annotations

from . import Extracted, Segment

SCANNED_MIN_CHARS = 25  # fewer chars than this on a page -> probably a scan


def extract_pdf(path: str) -> Extracted:
    pages: list[str] = []
    try:
        import fitz  # PyMuPDF

        with fitz.open(path) as doc:
            if doc.needs_pass:
                return Extracted(meta={"note": "password protected"})
            pages = [p.get_text("text") for p in doc]
    except ImportError:
        from pypdf import PdfReader

        reader = PdfReader(path)
        if reader.is_encrypted:
            return Extracted(meta={"note": "password protected"})
        pages = [(p.extract_text() or "") for p in reader.pages]

    segs = [Segment(text=t, page=i + 1) for i, t in enumerate(pages) if t.strip()]
    scanned = [i + 1 for i, t in enumerate(pages) if len(t.strip()) < SCANNED_MIN_CHARS]
    meta = {"pages": len(pages)}
    if scanned:
        meta["scanned_pages"] = scanned[:500]  # Phase 4: embed these as images
    return Extracted(segs, meta)
