"""Learn from what you open: files you open often (and recently) rise within their group,
and a file you opened for the same query before becomes the top hit.

Stored locally in the index database (table `opens`); nothing leaves the PC.
"""

from __future__ import annotations

import math
import time

from .store import Store

HALF_LIFE_DAYS = 30.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS opens (
    path  TEXT NOT NULL,
    query TEXT NOT NULL,
    ts    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS opens_path ON opens(path);
"""


def _norm_q(q: str) -> str:
    return " ".join(q.lower().lstrip("=~").split())


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


def record_open(store: Store, query: str, path: str, now: float | None = None) -> None:
    ensure(store)
    store.db.execute("INSERT INTO opens(path, query, ts) VALUES(?,?,?)", (path, _norm_q(query), now or time.time()))
    store.db.commit()


def boosts(store: Store, paths: list[str], query: str, now: float | None = None) -> dict[str, float]:
    """path -> boost (0 = never opened). Frequency with a 30-day half-life, plus a bonus
    when the same (or a longer version of the same) query led to this file before."""
    if not paths:
        return {}
    ensure(store)
    now = now or time.time()
    q = _norm_q(query)
    out: dict[str, float] = {}
    for i in range(0, len(paths), 900):
        part = paths[i : i + 900]
        rows = store.db.execute(f"SELECT path, query, ts FROM opens WHERE path IN ({','.join('?' * len(part))})", part)
        for path, oq, ts in rows:
            age_days = max(0.0, (now - ts) / 86400)
            w = 0.5 ** (age_days / HALF_LIFE_DAYS)
            same = q and (oq == q or oq.startswith(q) or q.startswith(oq))
            out[path] = out.get(path, 0.0) + w * (3.0 if same else 1.0)
    return {p: math.log1p(v) for p, v in out.items()}


def rerank(docs: list, b: dict[str, float]) -> list:
    """Re-order inside each match group (exact / words / name / meaning) - never across
    groups, so a real text match always stays above a 'related by meaning' file."""
    if not b:
        return docs
    out, i = [], 0
    while i < len(docs):
        j = i
        while j < len(docs) and docs[j].match == docs[i].match:
            j += 1
        group = docs[i:j]
        keyed = sorted(range(len(group)), key=lambda k: -(1.0 / (k + 1) + b.get(group[k].path, 0.0)))
        out += [group[k] for k in keyed]
        i = j
    return out
