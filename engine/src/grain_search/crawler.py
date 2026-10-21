"""Walk the configured folders and yield indexable files."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass, replace

from .config import Config
from .extractors import kind_of

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4


@dataclass
class FileInfo:
    path: str
    size: int
    mtime_ns: int
    kind: str


def _norm(p: str) -> str:
    return p.replace("\\", "/").lower()


class Excluder:
    """Decides whether a path is skipped.

    A folder the user added explicitly wins over exclusions that cover that whole folder:
    adding C:\\Users\\me\\AppData\\Roaming\\Notes indexes it, even though *\\AppData\\* is a
    default exclusion. Exclusions that match things *inside* the folder still apply, and
    `always` patterns (the app's own index folder) apply everywhere.
    """

    def __init__(self, patterns: list[str], roots: list[str] = (), always: list[str] = ()):
        self.patterns = [_norm(p) for p in patterns]
        self.always = [_norm(p) for p in always]
        rules = []
        for r in roots:
            root = _norm(os.path.abspath(r)).rstrip("/")
            active = [pat for pat in self.patterns if not self._match(root, pat, is_dir=True)]
            rules.append((root + "/", active))
        self.root_rules = sorted(rules, key=lambda x: -len(x[0]))  # most specific root first

    @staticmethod
    def _match(p: str, pat: str, is_dir: bool) -> bool:
        probe = p + "/" if is_dir else p  # so "*/node_modules/*" matches the dir itself
        return fnmatch.fnmatchcase(probe, pat) or fnmatch.fnmatchcase(p, pat)

    def _patterns_for(self, p: str) -> list[str]:
        for prefix, active in self.root_rules:
            if (p + "/").startswith(prefix):
                return active
        return self.patterns

    def __call__(self, path: str, is_dir: bool = False) -> bool:
        p = _norm(path)
        return any(self._match(p, pat, is_dir) for pat in self.always) or any(
            self._match(p, pat, is_dir) for pat in self._patterns_for(p)
        )


def is_under(path: str, roots: list[str]) -> bool:
    """True if `path` is one of `roots` or inside one (case-insensitive on Windows)."""
    p = os.path.normcase(os.path.abspath(path))
    for r in roots:
        r = os.path.normcase(os.path.abspath(r))
        if p == r or p.startswith(r.rstrip("\\/") + os.sep):
            return True
    return False


def count_indexable(cfg: Config, folder: str, stop=lambda: False) -> tuple[int, int]:
    """How many files in `folder`, and how many bytes, Grain Search would index if it were added:
    the same rules as a real scan (exclusions, file types, size limits, hidden files)."""
    trial = replace(cfg, roots=[folder])
    files = size = 0
    for fi in crawl(trial):
        if fi.kind == "image" and not cfg.model.images:
            continue
        files += 1
        size += fi.size
        if files % 500 == 0 and stop():
            break
    return files, size


def excluder_for(cfg: Config) -> Excluder:
    return Excluder(cfg.excludes, roots=cfg.roots, always=cfg.own_excludes)


def _hidden(entry: os.DirEntry, st: os.stat_result) -> bool:
    if entry.name.startswith("."):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))


def crawl(cfg: Config, on_error=None) -> Iterator[FileInfo]:
    excluded = excluder_for(cfg)
    max_doc = cfg.max_doc_mb * 1024 * 1024
    max_img = cfg.max_image_mb * 1024 * 1024
    seen_dirs: set[str] = set()

    stack = [os.path.abspath(r) for r in cfg.roots if os.path.isdir(r)]
    while stack:
        d = stack.pop()
        key = os.path.normcase(os.path.realpath(d))
        if key in seen_dirs:  # overlapping roots / junctions
            continue
        seen_dirs.add(key)
        try:
            it = os.scandir(d)
        except OSError as e:
            if on_error:
                on_error(d, e)
            continue
        with it:
            for entry in it:
                try:
                    if entry.is_symlink():
                        continue
                    st = entry.stat(follow_symlinks=False)
                    if cfg.skip_hidden and _hidden(entry, st):
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        if not excluded(entry.path, is_dir=True):
                            stack.append(entry.path)
                        continue
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    kind = kind_of(entry.name)
                    if kind is None or excluded(entry.path):
                        continue
                    limit = max_img if kind == "image" else max_doc
                    if st.st_size == 0 or st.st_size > limit:
                        continue
                    yield FileInfo(entry.path, st.st_size, st.st_mtime_ns, kind)
                except OSError as e:
                    if on_error:
                        on_error(entry.path, e)


def quick_hash(path: str, size: int, block: int = 64 * 1024) -> str:
    """Hash of size + first and last 64 KB: cheap, good enough to spot moves/renames."""
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(block))
        if size > 2 * block:
            f.seek(-block, os.SEEK_END)
            h.update(f.read(block))
    return h.hexdigest()
