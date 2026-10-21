"""Settings: load config.json (or a YAML/JSON file given with --config) into a typed Config.

Where the file lives is decided in paths.py. A missing file is fine: it means "not set up
yet" - no folders are indexed until the user adds some (`grain folders add ...`).
Nothing machine-specific is built in.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .paths import app_paths

CONFIG_VERSION = 1

DEFAULT_EXCLUDES = [
    "C:\\Windows\\*",
    "C:\\Program Files\\*",
    "C:\\Program Files (x86)\\*",
    "C:\\ProgramData\\*",
    "*\\AppData\\*",
    "*\\$Recycle.Bin\\*",
    "*\\System Volume Information\\*",
    "*\\node_modules\\*",
    "*\\.git\\*",
    "*\\.venv\\*",
    "*\\venv\\*",
    "*\\__pycache__\\*",
    "*\\site-packages\\*",
]


@dataclass
class ModelCfg:
    id: str = "google/embeddinggemma-2"
    dim: int = 768  # 768 | 512 | 256 (128 is poor for images)
    vision_tokens: int = 280  # 70 | 140 | 280 | 560 | 1120
    device: str = "auto"  # auto | cuda | cpu
    text_batch: int = 16
    image_batch: int = 8
    images: bool = True  # load the vision encoder and index pictures
    gpu_memory_fraction: float = 0.5  # never use more than this share of GPU memory (games need the rest)
    max_seq_tokens: int = 1024  # longest text piece the model sees at once (chunks are ~350 tokens)
    dir: str | None = None  # folder the model is downloaded into (None = %LOCALAPPDATA%\GrainSearch\models)
    # Set at run time by model_files.locate(): where to load the model from, and whether loading may
    # download it. The app never downloads silently: the user clicks "Download model" in Settings.
    source: str | None = None
    local_only: bool = False


@dataclass
class Config:
    roots: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    max_doc_mb: float = 50
    max_image_mb: float = 40
    max_chunks_per_file: int = 2000
    skip_hidden: bool = True
    chunk_chars: int = 1400  # ~350 tokens
    overlap_chars: int = 200  # ~50 tokens
    rrf_k: int = 60
    text_top_k: int = 200
    image_top_k: int = 50
    image_min_score: float = 0.20
    text_min_score: float = 0.30  # meaning-only document results below this are hidden
    related_margin: float = 0.08  # ...and when real text matches exist, only keep ones close to the best
    related_limit: int = 3  # ...and at most this many
    port: int = 0  # grain serve, 127.0.0.1 only; 0 = any free port (the window reads it from service.json)
    watch: bool = True  # live updates while the service runs
    pause_on_fullscreen: bool = True
    pause_on_battery: bool = True
    data_dir: Path = field(default_factory=lambda: app_paths().index_dir)  # the index folder
    model: ModelCfg = field(default_factory=ModelCfg)
    config_path: Path | None = None  # the file these settings came from (None = built-in defaults)
    service_file: Path | None = None  # where the running service announces itself

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.db"

    @property
    def service_path(self) -> Path:
        return self.service_file or self.data_dir / "service.json"

    @property
    def own_excludes(self) -> list[str]:
        """The app's own folders (index, AI model files): never indexed, whatever the user's settings say."""
        own = [self.data_dir, app_paths().models_dir] + ([Path(self.model.dir)] if self.model.dir else [])
        return [str(p).rstrip("\\/").replace("[", "[[]") + os.sep + "*" for p in own]


_PCT_VAR = re.compile(r"%([^%]+)%")


def expand(p: str) -> str:
    """Expand %USERPROFILE%, $HOME and ~ on any OS, then normalise."""
    p = _PCT_VAR.sub(lambda m: os.environ.get(m.group(1), m.group(0)), p)
    p = os.path.expanduser(os.path.expandvars(p))
    return os.path.normpath(p)


def default_config_path() -> Path:
    return app_paths().config_file


def read_raw(path: str | os.PathLike) -> dict:
    """The settings file as a plain dict ({} if it does not exist). JSON, or YAML by extension."""
    p = Path(path)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8-sig")
    if p.suffix.lower() in (".yaml", ".yml"):
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError(f"{p}: expected an object with settings, got {type(data).__name__}")
    return data


def save_raw(path: str | os.PathLike, raw: dict) -> Path:
    """Write settings atomically (a crash never leaves a half-written file)."""
    p = Path(path)
    if p.suffix.lower() in (".yaml", ".yml"):
        raise ValueError("Settings are saved as JSON; use a .json file")
    p.parent.mkdir(parents=True, exist_ok=True)
    out = {"version": CONFIG_VERSION, **{k: v for k, v in raw.items() if k != "version"}}
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def load_config(path: str | os.PathLike | None = None) -> Config:
    paths = app_paths()
    cfg_path = Path(path) if path else paths.config_file
    raw = read_raw(cfg_path)

    cfg = Config()
    cfg.config_path = cfg_path
    cfg.service_file = paths.service_file
    cfg.roots = [expand(r) for r in raw.get("roots") or []]  # nothing built in: the user picks folders
    if raw.get("excludes"):
        cfg.excludes = [expand(e) if not e.startswith("*") else e for e in raw["excludes"]]

    limits = raw.get("limits") or {}
    cfg.max_doc_mb = float(limits.get("max_doc_mb", cfg.max_doc_mb))
    cfg.max_image_mb = float(limits.get("max_image_mb", cfg.max_image_mb))
    cfg.skip_hidden = bool(limits.get("skip_hidden", cfg.skip_hidden))

    chunking = raw.get("chunking") or {}
    # config is in tokens; we work in characters (~4 chars per token)
    if "target_tokens" in chunking:
        cfg.chunk_chars = int(chunking["target_tokens"]) * 4
    if "overlap_tokens" in chunking:
        cfg.overlap_chars = int(chunking["overlap_tokens"]) * 4

    search = raw.get("search") or {}
    cfg.rrf_k = int(search.get("rrf_k", cfg.rrf_k))
    cfg.text_top_k = int(search.get("text_top_k", cfg.text_top_k))
    cfg.image_top_k = int(search.get("image_top_k", cfg.image_top_k))
    cfg.image_min_score = float(search.get("image_min_score", cfg.image_min_score))
    cfg.text_min_score = float(search.get("text_min_score", cfg.text_min_score))
    cfg.related_margin = float(search.get("related_margin", cfg.related_margin))
    cfg.related_limit = int(search.get("related_limit", cfg.related_limit))

    service = raw.get("service") or {}
    cfg.port = int(service.get("port", cfg.port))
    cfg.watch = bool(service.get("watch", cfg.watch))
    cfg.pause_on_fullscreen = bool(service.get("pause_on_fullscreen", cfg.pause_on_fullscreen))
    cfg.pause_on_battery = bool(service.get("pause_on_battery", cfg.pause_on_battery))

    m = raw.get("model") or {}
    cfg.model = ModelCfg(
        id=m.get("id", cfg.model.id),
        dim=int(m.get("dim", cfg.model.dim)),
        vision_tokens=int(m.get("vision_tokens", cfg.model.vision_tokens)),
        device=str(m.get("device", cfg.model.device)),
        text_batch=int(m.get("text_batch", cfg.model.text_batch)),
        image_batch=int(m.get("image_batch", cfg.model.image_batch)),
        images="image" in (m.get("modalities") or ["text", "image"]),
        gpu_memory_fraction=float(m.get("gpu_memory_fraction", 0.5)),
        max_seq_tokens=int(m.get("max_seq_tokens", 1024)),
        dir=expand(m["dir"]) if m.get("dir") else None,
    )
    if cfg.model.dim not in (768, 512, 256, 128):
        raise ValueError("model.dim must be 768, 512, 256 or 128")

    index_dir = raw.get("index_dir") or raw.get("data_dir")  # data_dir = older name
    if index_dir:
        d = Path(expand(str(index_dir)))
        cfg.data_dir = d if d.is_absolute() else (cfg_path.parent / d).resolve()
    else:
        cfg.data_dir = paths.index_dir
    return cfg


# -- changing folders and exclusions (used by the CLI and the app's Settings window) ---------
def same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def as_exclude_pattern(given: str) -> str:
    """`*.log` stays a pattern; a folder path becomes `<folder>\\*` (the folder and everything in it)."""
    given = given.strip()
    if "*" in given or "?" in given:
        return given
    return os.path.abspath(expand(given)).rstrip("\\/") + os.sep + "*"


def change_folders(raw: dict, action: str, given: str) -> tuple[bool, str, str]:
    """Add or remove one folder in the raw settings dict, in place.
    Returns (changed, message, normalised path). Raises ValueError for bad input."""
    if action not in ("add", "remove"):
        raise ValueError(f"unknown action {action!r}")
    if not given or not given.strip():
        raise ValueError("No folder given")
    path = os.path.abspath(expand(given.strip()))
    roots: list[str] = list(raw.get("roots") or [])
    if action == "add":
        if not os.path.isdir(path):
            raise ValueError(f"Not a folder: {path}")
        if any(same_path(expand(r), path) for r in roots):
            return False, f"Already indexed: {path}", path
        raw["roots"] = [*roots, path]
        return True, f"Added {path}", path
    keep = [r for r in roots if not same_path(expand(r), path)]
    if len(keep) == len(roots):
        return False, f"Not in the list: {path}", path
    raw["roots"] = keep
    return True, f"Removed {path}", path


def change_excludes(raw: dict, action: str, given: str) -> tuple[bool, str, str]:
    """Add or remove one exclusion (a folder or a pattern) in the raw settings dict, in place.
    The first change copies the built-in list, so built-ins stay unless removed explicitly."""
    if action not in ("add", "remove"):
        raise ValueError(f"unknown action {action!r}")
    if not given or not given.strip():
        raise ValueError("No folder or pattern given")
    pat = as_exclude_pattern(given)
    pats: list[str] = list(raw.get("excludes") or DEFAULT_EXCLUDES)
    if action == "add":
        if any(same_path(p, pat) for p in pats):
            return False, f"Already excluded: {pat}", pat
        raw["excludes"] = [*pats, pat]
        return True, f"Excluded {pat}", pat
    keep = [p for p in pats if not same_path(p, pat)]
    if len(keep) == len(pats):
        return False, f"Not in the list: {pat}", pat
    raw["excludes"] = keep
    return True, f"No longer excluded: {pat}", pat
