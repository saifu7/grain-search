"""Hybrid search.

Documents = Reciprocal Rank Fusion of
    A. exact phrase (FTS5)         -> pinned to the top: "type a sentence, get that file"
    B. keywords (FTS5 BM25)
    C. meaning (EmbeddingGemma 2 vectors over text chunks)
    D. file name (trigram)
Images = their own list, ranked by text->image similarity.

Text and image scores are NOT comparable (EmbeddingGemma 2 scores are not
calibrated across modalities), so the two lists are never merged by raw score.

Query syntax:  =text  -> only files that contain the text      ~text -> only by meaning
               type:doc|img  ext:pdf  in:D:\\work  after:2025-01-01  before:2025-12-31
               "exact phrase"
"""

from __future__ import annotations

import os
import re
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from .config import Config
from .embedder import Embedder
from .store import FileRow, Store

FILTER_KEYS = {"type", "ext", "in", "after", "before"}
_TOKEN = re.compile(r"\w+", re.UNICODE)


@dataclass
class Query:
    text: str = ""  # free text sent to the model
    phrases: list[str] = field(default_factory=list)
    type: str | None = None  # doc | img
    exts: set[str] = field(default_factory=set)
    within: list[str] = field(default_factory=list)
    after: float | None = None  # unix seconds
    before: float | None = None
    mode: str = "smart"  # smart | exact ("=" prefix) | meaning ("~" prefix)


def _date(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def parse_query(raw: str) -> Query:
    q = Query()
    raw = raw.strip()
    if raw[:1] == "=":
        q.mode, raw = "exact", raw[1:]
    elif raw[:1] == "~":
        q.mode, raw = "meaning", raw[1:]
    try:
        parts = shlex.split(raw, posix=False)
    except ValueError:
        parts = raw.split()
    words = []
    for p in parts:
        key, sep, val = p.partition(":")
        k = key.lower()
        if sep and k in FILTER_KEYS and val:
            val = val.strip('"')
            if k == "type":
                v = val.lower()
                q.type = "img" if v.startswith(("img", "image", "pic", "photo")) else "doc"
            elif k == "ext":
                q.exts |= {"." + e.lower().lstrip(".") for e in val.split(",") if e}
            elif k == "in":
                q.within.append(os.path.normcase(os.path.abspath(val)))
            elif k in ("after", "before"):
                try:
                    setattr(q, k, _date(val))
                except ValueError:
                    words.append(p)
            continue
        if len(p) >= 2 and p.startswith('"') and p.endswith('"'):
            phrase = p[1:-1].strip()
            if phrase:
                q.phrases.append(phrase)
                words.append(phrase)
            continue
        words.append(p.strip('"'))
    q.text = " ".join(w for w in words if w).strip()
    return q


@dataclass
class DocHit:
    path: str
    score: float
    snippet: str
    page: int | None
    line: int | None
    why: list[str]
    exact: bool = False
    match: str = "meaning"  # exact | words | name | meaning  (meaning = text NOT in file)
    similarity: float | None = None  # EmbeddingGemma score of the best chunk

    @property
    def contains_text(self) -> bool:
        return self.match != "meaning"


@dataclass
class ImageHit:
    path: str
    score: float
    meta: dict


@dataclass
class Results:
    query: Query
    docs: list[DocHit]
    images: list[ImageHit]
    ms: float

    @property
    def top(self) -> DocHit | ImageHit | None:
        """The obvious answer, if there is one (used by `grain open-top` / Enter)."""
        if self.docs and self.docs[0].exact:
            return self.docs[0]
        if self.query.type == "img" and self.images:
            return self.images[0]
        if self.docs:
            return self.docs[0]
        return self.images[0] if self.images else None


def _fts_phrase(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def _fts_words(s: str, op: str) -> str:
    toks = [t for t in _TOKEN.findall(s) if len(t) > 1]
    return f" {op} ".join(_fts_phrase(t) for t in toks)


class Searcher:
    def __init__(self, cfg: Config, store: Store, embedder: Embedder):
        self.cfg, self.store, self.embedder = cfg, store, embedder

    # -- filters ----------------------------------------------------------------
    def _ok(self, f: FileRow, q: Query) -> bool:
        if q.exts and f.ext not in q.exts:
            return False
        if q.within:
            p = os.path.normcase(f.path)
            if not any(p == w or p.startswith(w.rstrip("\\/") + os.sep) for w in q.within):
                return False
        if q.after or q.before:
            t = f.mtime_ns / 1e9
            taken = f.meta.get("taken")
            if taken:
                try:
                    t = datetime.strptime(taken[:19], "%Y-%m-%d %H:%M:%S").timestamp()
                except ValueError:
                    pass
            if q.after and t < q.after:
                return False
            if q.before and t > q.before:
                return False
        return True

    # -- lookups ----------------------------------------------------------------
    def _fts(self, match: str, limit: int) -> list[int]:
        if not match:
            return []
        try:
            return [
                r[0]
                for r in self.store.db.execute(
                    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?", (match, limit)
                )
            ]
        except Exception:
            return []

    def _names(self, text: str, limit: int) -> list[int]:
        toks = [t for t in _TOKEN.findall(text) if len(t) >= 3]
        if not toks:
            return []
        match = " AND ".join(_fts_phrase(t) for t in toks)
        try:
            return [
                r[0]
                for r in self.store.db.execute(
                    "SELECT rowid FROM names_fts WHERE names_fts MATCH ? ORDER BY rank LIMIT ?", (match, limit)
                )
            ]
        except Exception:
            return []

    def _knn(self, qv: np.ndarray, modality: str, k: int):
        return self.store.knn(modality, qv, k)

    # -- documents ----------------------------------------------------------------
    def _search_docs(self, q: Query, qv, limit: int) -> list[DocHit]:
        """Two groups:
        1. files that CONTAIN the text  (exact phrase, all words, or file name)
        2. files that are only related by MEANING (text not present) - kept only if
           the similarity is high enough, and capped when group 1 is not empty.
        """
        K = self.cfg.rrf_k
        n_words = len(_TOKEN.findall(q.text))

        exact_ids: list[int] = []
        words_ids: list[int] = []
        name_files: list[int] = []
        if q.mode != "meaning" and q.text:
            phrases = q.phrases or ([q.text] if n_words >= 2 else [])
            for ph in phrases:
                exact_ids += self._fts(_fts_phrase(ph), 300)
            words_ids = self._fts(_fts_words(q.text, "AND"), 300)
            name_files = self._names(q.text, 100)

        sem_ids: list[int] = []
        sem_score: dict[int, float] = {}
        if q.mode != "exact" and qv is not None:
            scores, _, sem_ids = self._knn(qv, "text", self.cfg.text_top_k)
            sem_score = dict(zip(sem_ids, scores))

        chunks = self.store.get_chunks([*exact_ids, *words_ids, *sem_ids])
        agg: dict[int, dict] = {}

        def add(why: str, ids: list[int], weight: float):
            seen: dict[int, int] = {}
            for cid in ids:
                c = chunks.get(cid)
                if c is None or c["file_id"] in seen:
                    continue
                fid = c["file_id"]
                r = seen[fid] = len(seen)
                a = agg.setdefault(fid, {"score": 0.0, "chunk": None, "why": [], "sim": None})
                if a["chunk"] is None:  # snippet priority: exact > words > meaning
                    a["chunk"] = cid
                a["score"] += weight / (K + r + 1)
                a["why"].append(why)
                if why == "meaning" and a["sim"] is None:
                    a["sim"] = float(sem_score.get(cid, 0.0))

        add("exact", exact_ids, 3.0)
        add("words", words_ids, 1.5)
        add("meaning", sem_ids, 1.0)
        for rank, fid in enumerate(name_files):
            a = agg.setdefault(fid, {"score": 0.0, "chunk": None, "why": [], "sim": None})
            a["score"] += 1.0 / (K + rank + 1)
            a["why"].append("name")

        def kind(a) -> str:
            for k in ("exact", "words", "name"):
                if k in a["why"]:
                    return k
            return "meaning"

        files = self.store.get_files(agg)
        ok = {
            fid
            for fid in agg
            if (f := files.get(fid)) and f.kind != "image" and self._ok(f, q) and os.path.exists(f.path)
        }

        order = {"exact": 0, "words": 1, "name": 2, "meaning": 3}
        text_hits = sorted(
            (fid for fid in ok if kind(agg[fid]) != "meaning"),
            key=lambda fid: (order[kind(agg[fid])], -agg[fid]["score"]),
        )
        related = sorted((fid for fid in ok if kind(agg[fid]) == "meaning"), key=lambda fid: -(agg[fid]["sim"] or 0.0))

        # meaning-only results: absolute floor, plus a tighter cut when real text matches exist
        floor = self.cfg.text_min_score
        if related:
            best = agg[related[0]]["sim"] or 0.0
            if text_hits and q.mode == "smart":
                floor = max(floor, best - self.cfg.related_margin)
            related = [fid for fid in related if (agg[fid]["sim"] or 0.0) >= floor]
            if text_hits and q.mode == "smart":
                related = related[: self.cfg.related_limit]

        picked = text_hits[:limit] + related[:limit]
        need = [agg[f]["chunk"] for f in picked if agg[f]["chunk"] and agg[f]["chunk"] not in chunks]
        chunks.update(self.store.get_chunks(need))

        needles = q.phrases or [q.text]
        out: list[DocHit] = []
        for fid in picked:
            a, f = agg[fid], files[fid]
            c = chunks.get(a["chunk"]) if a["chunk"] else None
            out.append(
                DocHit(
                    path=f.path,
                    score=round(a["score"], 4),
                    snippet=make_snippet(c["text"] if c else "", needles),
                    page=c["page"] if c else None,
                    line=_match_line(c, needles) if c else None,
                    why=sorted(set(a["why"]), key=["exact", "words", "name", "meaning"].index),
                    exact=kind(a) == "exact",
                    match=kind(a),
                    similarity=None if a["sim"] is None else round(a["sim"], 3),
                )
            )
        return out

    # -- main -------------------------------------------------------------------
    def search(self, raw: str, limit: int = 10) -> Results:
        t0 = time.perf_counter()
        q = parse_query(raw)
        if not q.text and not (q.exts or q.within or q.after or q.before):
            return Results(q, [], [], 0.0)

        want_docs = q.type != "img"
        want_imgs = q.type != "doc" and self.cfg.model.images and q.mode != "exact"
        qv = self.embedder.encode_query(q.text) if q.text and q.mode != "exact" else None

        docs: list[DocHit] = []
        if want_docs:
            docs = self._search_docs(q, qv, limit)

        images: list[ImageHit] = []
        if want_imgs and qv is not None:
            scores, fids, _ = self._knn(qv, "image", max(self.cfg.image_top_k, limit * 5))
            files = self.store.get_files(fids)
            for s, fid in zip(scores, fids):
                f = files.get(fid)
                if f is None or s < self.cfg.image_min_score or not self._ok(f, q) or not os.path.exists(f.path):
                    continue
                images.append(ImageHit(f.path, round(float(s), 4), f.meta))
                if len(images) >= limit:
                    break
        elif want_imgs and qv is None:  # filters only, e.g. "type:img after:2025-06-01"
            rows = self.store.db.execute(
                "SELECT * FROM files WHERE kind='image' AND status='indexed' ORDER BY mtime_ns DESC LIMIT 2000"
            ).fetchall()
            for r in rows:
                f = FileRow.from_row(r)
                if self._ok(f, q):
                    images.append(ImageHit(f.path, 0.0, f.meta))
                    if len(images) >= limit:
                        break

        return Results(q, docs, images, (time.perf_counter() - t0) * 1000)


def make_snippet(text: str, needles: list[str], width: int = 220) -> str:
    flat = " ".join(text.split())
    if not flat:
        return ""
    low = flat.lower()
    pos = -1
    for n in needles:
        n = " ".join(n.split()).lower()
        if n and (pos := low.find(n)) != -1:
            break
    if pos == -1:  # fall back to the first query word that appears
        for w in sorted({t.lower() for n in needles for t in _TOKEN.findall(n) if len(t) > 3}, key=len, reverse=True):
            if (pos := low.find(w)) != -1:
                break
    if pos == -1:
        return flat[:width] + ("…" if len(flat) > width else "")
    start = max(0, pos - width // 3)
    end = min(len(flat), start + width)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


def _match_line(chunk, needles: list[str]) -> int | None:
    """Line of the first exact match inside the chunk (falls back to the chunk's first line)."""
    line = chunk["line"]
    if line is None:
        return None
    text = chunk["text"]
    low = text.lower()
    for n in needles:
        n = n.strip().lower()
        if n and (pos := low.find(n)) != -1:
            return line + text.count("\n", 0, pos)
    return line
