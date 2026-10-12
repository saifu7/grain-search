from __future__ import annotations

import json
import os

from . import Extracted, Segment

MAX_BYTES = 20 * 1024 * 1024


def read_text_file(path: str) -> str | None:
    """Decode a text file; returns None if it looks binary."""
    with open(path, "rb") as f:
        data = f.read(MAX_BYTES)
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data[:8192]:
        return None
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _notebook_text(raw: str) -> str:
    try:
        nb = json.loads(raw)
        cells = nb.get("cells", [])
        return "\n\n".join("".join(c.get("source", [])) for c in cells)
    except Exception:
        return raw


def extract_text(path: str) -> Extracted:
    text = read_text_file(path)
    if not text or not text.strip():
        return Extracted()
    if os.path.splitext(path)[1].lower() == ".ipynb":
        text = _notebook_text(text)
    return Extracted([Segment(text=text, line=1)])
