"""Everything lives in one SQLite file (data/index.db):

files       one row per file (path, size, mtime, quick hash, status, metadata)
chunks      text chunks with page / line
chunks_fts  FTS5 full-text over chunk text      -> exact words and "phrases"
names_fts   FTS5 trigram over file name + path  -> filename search
vectors     embeddings (text chunks and images) as float16 blobs

Vectors are searched with numpy (brute-force dot product). On a personal PC
(up to a few hundred thousand vectors) that takes milliseconds and needs no
extra native extension. Storage is float16 to halve disk/RAM; the *model*
itself never runs in float16.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files (
    id         INTEGER PRIMARY KEY,
    path       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    ext        TEXT NOT NULL,
    kind       TEXT NOT NULL,
    size       INTEGER NOT NULL,
    mtime_ns   INTEGER NOT NULL,
    qhash      TEXT,
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending|indexed|empty|error
    error      TEXT,
    meta       TEXT,
    indexed_at REAL
);
CREATE INDEX IF NOT EXISTS files_qhash ON files(qhash);
CREATE INDEX IF NOT EXISTS files_status ON files(status);
CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ord        INTEGER NOT NULL,
    text       TEXT NOT NULL,
    page       INTEGER,
    line       INTEGER,
    char_start INTEGER,
    char_end   INTEGER
);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
CREATE TABLE IF NOT EXISTS vectors (
    id       INTEGER PRIMARY KEY,
    file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    chunk_id INTEGER,
    modality TEXT NOT NULL,                       -- text|image
    vec      BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS vectors_file ON vectors(file_id);
CREATE INDEX IF NOT EXISTS vectors_modality ON vectors(modality);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE IF NOT EXISTS names_fts USING fts5(name, path, tokenize='trigram');
"""


@dataclass
class FileRow:
    id: int
    path: str
    name: str
    ext: str
    kind: str
    size: int
    mtime_ns: int
    qhash: str | None
    status: str
    error: str | None
    meta: dict

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> FileRow:
        return cls(
            r["id"],
            r["path"],
            r["name"],
            r["ext"],
            r["kind"],
            r["size"],
            r["mtime_ns"],
            r["qhash"],
            r["status"],
            r["error"],
            json.loads(r["meta"] or "{}"),
        )


class Store:
    def __init__(self, db_path: Path, check_same_thread: bool = True):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        self.db = sqlite3.connect(str(db_path), check_same_thread=check_same_thread, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        self._vec: dict[str, VecIndex] = {}

    def close(self):
        self.db.close()

    # -- meta -------------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_meta(self, key: str, value: str):
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
        )
        self.db.commit()

    def check_model(self, model_id: str, dim: int):
        """The index must be built by the same model at the same dimension as the queries."""
        stored = self.get_meta("model")
        want = f"{model_id}@{dim}"
        if stored is None:
            if self.db.execute("SELECT 1 FROM vectors LIMIT 1").fetchone() is None:
                self.set_meta("model", want)
                return
            stored = "unknown"
        if stored != want:
            raise RuntimeError(
                f"The index was built with {stored} but the current setting is {want}.\n"
                f"Run `grain reset` and re-index, or change config.yaml back."
            )

    def _bump_vectors(self):
        v = int(self.get_meta("vec_version") or 0) + 1
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES('vec_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(v),),
        )

    # -- files ------------------------------------------------------------------
    def all_files(self) -> dict[str, FileRow]:
        return {r["path"]: FileRow.from_row(r) for r in self.db.execute("SELECT * FROM files")}

    def get_file(self, path: str) -> FileRow | None:
        r = self.db.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        return FileRow.from_row(r) if r else None

    def files_under(self, prefix: str) -> dict[str, FileRow]:
        """All indexed files whose path starts with `prefix` (a folder path ending in a separator)."""
        like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self.db.execute("SELECT * FROM files WHERE path LIKE ? ESCAPE '\\'", (like,))
        return {r["path"]: FileRow.from_row(r) for r in rows}

    def get_files(self, ids) -> dict[int, FileRow]:
        ids = list(set(ids))
        out: dict[int, FileRow] = {}
        for i in range(0, len(ids), 900):
            part = ids[i : i + 900]
            q = f"SELECT * FROM files WHERE id IN ({','.join('?' * len(part))})"
            out.update({r["id"]: FileRow.from_row(r) for r in self.db.execute(q, part)})
        return out

    def upsert_file(
        self, path: str, name: str, ext: str, kind: str, size: int, mtime_ns: int, qhash: str | None
    ) -> int:
        """Insert or reset a file row (clearing its old chunks/vectors). Returns file id."""
        row = self.get_file(path)
        if row:
            self._clear_content(row.id)
            self.db.execute(
                "UPDATE files SET size=?, mtime_ns=?, qhash=?, status='pending', error=NULL, "
                "meta=NULL, indexed_at=NULL, kind=? WHERE id=?",
                (size, mtime_ns, qhash, kind, row.id),
            )
            return row.id
        cur = self.db.execute(
            "INSERT INTO files(path,name,ext,kind,size,mtime_ns,qhash) VALUES(?,?,?,?,?,?,?)",
            (path, name, ext, kind, size, mtime_ns, qhash),
        )
        fid = cur.lastrowid
        self.db.execute("INSERT INTO names_fts(rowid,name,path) VALUES(?,?,?)", (fid, name, path))
        return fid

    def touch_file(self, file_id: int, size: int, mtime_ns: int):
        self.db.execute("UPDATE files SET size=?, mtime_ns=? WHERE id=?", (size, mtime_ns, file_id))

    def move_file(self, file_id: int, new_path: str, name: str, ext: str, mtime_ns: int):
        self.db.execute(
            "UPDATE files SET path=?, name=?, ext=?, mtime_ns=? WHERE id=?", (new_path, name, ext, mtime_ns, file_id)
        )
        self.db.execute("DELETE FROM names_fts WHERE rowid=?", (file_id,))
        self.db.execute("INSERT INTO names_fts(rowid,name,path) VALUES(?,?,?)", (file_id, name, new_path))

    def set_status(self, file_id: int, status: str, error: str | None = None, meta: dict | None = None):
        self.db.execute(
            "UPDATE files SET status=?, error=?, meta=?, indexed_at=? WHERE id=?",
            (status, error, json.dumps(meta) if meta else None, time.time(), file_id),
        )

    def _clear_content(self, file_id: int):
        ids = [r[0] for r in self.db.execute("SELECT id FROM chunks WHERE file_id=?", (file_id,))]
        for i in range(0, len(ids), 900):
            part = ids[i : i + 900]
            self.db.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({','.join('?' * len(part))})", part)
        self.db.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
        if self.db.execute("DELETE FROM vectors WHERE file_id=?", (file_id,)).rowcount:
            self._bump_vectors()

    def delete_file(self, file_id: int):
        self._clear_content(file_id)
        self.db.execute("DELETE FROM names_fts WHERE rowid=?", (file_id,))
        self.db.execute("DELETE FROM files WHERE id=?", (file_id,))

    # -- content ----------------------------------------------------------------
    def add_chunks(self, file_id: int, chunks) -> list[int]:
        ids = []
        for i, c in enumerate(chunks):
            cur = self.db.execute(
                "INSERT INTO chunks(file_id,ord,text,page,line,char_start,char_end) VALUES(?,?,?,?,?,?,?)",
                (file_id, i, c.text, c.page, c.line, c.char_start, c.char_end),
            )
            self.db.execute("INSERT INTO chunks_fts(rowid,text) VALUES(?,?)", (cur.lastrowid, c.text))
            ids.append(cur.lastrowid)
        return ids

    def add_vectors(self, file_id: int, modality: str, vecs: np.ndarray, chunk_ids=None):
        vecs = np.asarray(vecs, dtype=np.float16)
        # ids only ever grow, so an in-memory index can tell new rows from old ones
        nxt = self.get_meta("vec_next_id")
        if nxt is None:
            nxt = (self.db.execute("SELECT MAX(id) FROM vectors").fetchone()[0] or 0) + 1
        nxt = int(nxt)
        rows = [
            (nxt + i, file_id, (chunk_ids[i] if chunk_ids else None), modality, vecs[i].tobytes())
            for i in range(len(vecs))
        ]
        self.db.executemany("INSERT INTO vectors(id,file_id,chunk_id,modality,vec) VALUES(?,?,?,?,?)", rows)
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES('vec_next_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(nxt + len(rows)),),
        )
        self._bump_vectors()

    def commit(self):
        self.db.commit()

    def get_chunks(self, ids) -> dict[int, sqlite3.Row]:
        ids = list(set(ids))
        out = {}
        for i in range(0, len(ids), 900):
            part = ids[i : i + 900]
            q = f"SELECT * FROM chunks WHERE id IN ({','.join('?' * len(part))})"
            out.update({r["id"]: r for r in self.db.execute(q, part)})
        return out

    # -- vectors in memory ---------------------------------------------------------
    def _vec_index(self, modality: str) -> VecIndex:
        """In-memory vectors for one modality, kept in sync with the table incrementally."""
        version = int(self.get_meta("vec_version") or 0)
        vi = self._vec.get(modality)
        if vi is None:
            vi = VecIndex.load(self.path.parent / f"vectors-{modality}.npz")
            self._vec[modality] = vi
        if vi.version != version:
            fetched = vi.sync(self.db, modality)
            vi.version = version
            if fetched > 5000:  # big change: refresh the fast-start file for next time
                vi.save(self.path.parent / f"vectors-{modality}.npz")
        return vi

    def knn(self, modality: str, qv: np.ndarray, k: int):
        """Top-k (scores, file_ids, chunk_ids) by dot product (= cosine, vectors are normalised)."""
        return self._vec_index(modality).search(qv, k)

    def vector_count(self, modality: str) -> int:
        return self._vec_index(modality).count()

    def save_vector_cache(self):
        for modality, vi in self._vec.items():
            vi.save(self.path.parent / f"vectors-{modality}.npz")

    # -- stats ------------------------------------------------------------------
    def stats(self) -> dict:
        q = lambda sql: self.db.execute(sql).fetchone()[0]  # noqa: E731
        by_status = dict(self.db.execute("SELECT status, COUNT(*) FROM files GROUP BY status").fetchall())
        by_kind = dict(
            self.db.execute("SELECT kind, COUNT(*) FROM files WHERE status='indexed' GROUP BY kind").fetchall()
        )
        return {
            "files": q("SELECT COUNT(*) FROM files"),
            "by_status": by_status,
            "indexed_by_kind": by_kind,
            "chunks": q("SELECT COUNT(*) FROM chunks"),
            "text_vectors": q("SELECT COUNT(*) FROM vectors WHERE modality='text'"),
            "image_vectors": q("SELECT COUNT(*) FROM vectors WHERE modality='image'"),
            "model": self.get_meta("model"),
            "db_mb": round(self.path.stat().st_size / 1e6, 1) if self.path.exists() else 0,
        }


class VecIndex:
    """Vectors kept in a few float32 blocks: appends add a small block, deletions only
    flip an `alive` flag, and blocks are merged once enough has changed. Searching is a
    dot product per block - a few ms for hundreds of thousands of vectors."""

    def __init__(self):
        self.blocks: list[dict] = []
        self.version = -1

    # -- persistence ----------------------------------------------------------------
    @classmethod
    def load(cls, path: Path) -> VecIndex:
        vi = cls()
        try:
            with np.load(path) as z:
                if "ids" in z.files and len(z["ids"]):
                    vi._append(z["ids"], z["f"], z["c"], z["m"])
        except Exception:
            pass
        return vi

    def save(self, path: Path):
        ids, f, c, m = self._alive_arrays(as_f16=True)
        try:
            tmp = path.with_suffix(".tmp.npz")
            np.savez(tmp, ids=ids, f=f, c=c, m=m)
            os.replace(tmp, path)
        except OSError:
            pass

    # -- maintenance ----------------------------------------------------------------
    def _append(self, ids, f, c, m):
        self.blocks.append(
            {
                "ids": np.asarray(ids, np.int64),
                "f": np.asarray(f, np.int64),
                "c": np.asarray(c, np.int64),
                "m": np.asarray(m, np.float32),
                "alive": np.ones(len(ids), bool),
            }
        )

    def _alive_arrays(self, as_f16=False):
        if not self.blocks:
            z = np.zeros(0, np.int64)
            return z, z, z, np.zeros((0, 1), np.float16 if as_f16 else np.float32)
        parts = [
            (b["ids"][b["alive"]], b["f"][b["alive"]], b["c"][b["alive"]], b["m"][b["alive"]]) for b in self.blocks
        ]
        m = np.concatenate([p[3] for p in parts])
        return (
            np.concatenate([p[0] for p in parts]),
            np.concatenate([p[1] for p in parts]),
            np.concatenate([p[2] for p in parts]),
            m.astype(np.float16) if as_f16 else m,
        )

    def max_id(self) -> int:
        return max((int(b["ids"].max()) for b in self.blocks if len(b["ids"])), default=-1)

    def count(self) -> int:
        return int(sum(b["alive"].sum() for b in self.blocks))

    def sync(self, db: sqlite3.Connection, modality: str) -> int:
        """Drop rows deleted from the table, add rows inserted since. Returns rows fetched."""
        db_ids = np.fromiter(
            (r[0] for r in db.execute("SELECT id FROM vectors WHERE modality=?", (modality,))), np.int64
        )
        for b in self.blocks:
            b["alive"] &= np.isin(b["ids"], db_ids)
        rows = db.execute(
            "SELECT id, file_id, COALESCE(chunk_id,-1), vec FROM vectors WHERE modality=? AND id>? ORDER BY id",
            (modality, self.max_id()),
        ).fetchall()
        if rows:
            m = np.frombuffer(b"".join(r[3] for r in rows), dtype=np.float16).reshape(len(rows), -1)
            self._append([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], m)
        total = sum(len(b["ids"]) for b in self.blocks)
        dead = total - self.count()
        if len(self.blocks) > 16 or (total and dead / total > 0.2):
            ids, f, c, m = self._alive_arrays()
            self.blocks = []
            if len(ids):
                self._append(ids, f, c, m)
        return len(rows)

    # -- search -----------------------------------------------------------------------
    def search(self, qv: np.ndarray, k: int):
        qv = np.asarray(qv, np.float32)
        all_s, all_f, all_c = [], [], []
        for b in self.blocks:
            if not b["alive"].any():
                continue
            if b["m"].shape[1] != qv.shape[0]:
                raise RuntimeError("Index and query dimensions differ - run `grain reset` and re-index.")
            s = b["m"] @ qv
            s[~b["alive"]] = -np.inf
            all_s.append(s)
            all_f.append(b["f"])
            all_c.append(b["c"])
        if not all_s:
            return [], [], []
        s, f, c = np.concatenate(all_s), np.concatenate(all_f), np.concatenate(all_c)
        k = min(k, int(np.isfinite(s).sum()))
        if k <= 0:
            return [], [], []
        top = np.argpartition(-s, k - 1)[:k]
        top = top[np.argsort(-s[top])]
        return s[top].tolist(), f[top].tolist(), c[top].tolist()
