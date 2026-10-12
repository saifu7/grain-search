"""Load config.yaml (or sensible defaults) into a typed Config object."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]

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
    "*\\grain-search\\engine\\data\\*",
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
    port: int = 8766  # grain serve (127.0.0.1 only)
    watch: bool = True  # live updates while the service runs
    pause_on_fullscreen: bool = True
    pause_on_battery: bool = True
    data_dir: Path = PROJECT_ROOT / "data"
    model: ModelCfg = field(default_factory=ModelCfg)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.db"


_PCT_VAR = re.compile(r"%([^%]+)%")


def expand(p: str) -> str:
    """Expand %USERPROFILE%, $HOME and ~ on any OS, then normalise."""
    p = _PCT_VAR.sub(lambda m: os.environ.get(m.group(1), m.group(0)), p)
    p = os.path.expanduser(os.path.expandvars(p))
    return os.path.normpath(p)


def default_roots() -> list[str]:
    home = Path.home()
    roots = [home / n for n in ("Documents", "Desktop", "Downloads", "Pictures")]
    return [str(r) for r in roots if r.exists()]


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg_path = Path(path) if path else PROJECT_ROOT / "config.yaml"
    raw: dict = {}
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    cfg = Config()
    cfg.roots = [expand(r) for r in raw.get("roots") or default_roots()]
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
    )
    if cfg.model.dim not in (768, 512, 256, 128):
        raise ValueError("model.dim must be 768, 512, 256 or 128")

    if raw.get("data_dir"):
        d = Path(expand(raw["data_dir"]))
        cfg.data_dir = d if d.is_absolute() else (PROJECT_ROOT / d).resolve()
    return cfg
