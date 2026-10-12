"""Split extracted segments into overlapping ~350-token chunks.

Chunks never cross a page boundary (each segment is one page/slide), break on
paragraph -> line -> sentence -> word boundaries, and remember their starting line
so search results can open the file at the right place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .extractors import Segment

_BREAKS = [re.compile(r"\n\s*\n"), re.compile(r"\n"), re.compile(r"(?<=[.!?。！？])\s+"), re.compile(r"\s+")]


@dataclass
class Chunk:
    text: str
    page: int | None
    line: int | None
    char_start: int
    char_end: int


def _cut_point(text: str, start: int, hard_end: int, min_end: int) -> int:
    """Best place to end a chunk inside text[min_end:hard_end]."""
    window = text[min_end:hard_end]
    for pat in _BREAKS:
        last = None
        for m in pat.finditer(window):
            last = m
        if last is not None:
            return min_end + last.end()
    return hard_end


def chunk_segment(seg: Segment, size: int = 1400, overlap: int = 200) -> list[Chunk]:
    text = seg.text
    n = len(text)
    chunks: list[Chunk] = []
    start = 0
    while start < n:
        # skip leading whitespace
        while start < n and text[start].isspace():
            start += 1
        if start >= n:
            break
        hard_end = min(start + size, n)
        end = n if hard_end == n else _cut_point(text, start, hard_end, start + size // 2)
        piece = text[start:end].strip()
        if piece:
            line = None if seg.line is None else seg.line + text.count("\n", 0, start)
            chunks.append(Chunk(piece, seg.page, line, start, end))
        if end >= n:
            break
        nxt = end - overlap
        # restart the overlap on a word boundary
        if nxt > start:
            sp = text.find(" ", nxt, end)
            nxt = sp + 1 if sp != -1 else nxt
        start = max(nxt, start + 1)
    return chunks


def chunk_segments(
    segments: list[Segment], size: int = 1400, overlap: int = 200, limit: int | None = None
) -> list[Chunk]:
    out: list[Chunk] = []
    for seg in segments:
        out.extend(chunk_segment(seg, size, overlap))
        if limit and len(out) >= limit:
            return out[:limit]
    return out
