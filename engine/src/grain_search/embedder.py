"""EmbeddingGemma 2 wrapper. Every model-card rule lives here and nowhere else:

1. Queries      -> prompt_name="SearchQuery"  ("task: search result | query: ...")
2. Text chunks  -> "title: {filename} | text: {chunk}"  (formatted manually)
3. Images       -> {"image": ...} with NO prefix
4. Never float16 for the model: bfloat16 on capable GPUs, float32 otherwise
5. truncate_dim + normalize_embeddings=True; queries and docs share one dim
6. Only load needed encoders: audio is always off here, vision is optional

`HashEmbedder` is a tiny stand-in with the same interface, used by the test-suite
(and `--fake`) so the whole pipeline can be exercised without downloading the model.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Protocol

import numpy as np

from .config import ModelCfg


class Embedder(Protocol):
    model_id: str
    dim: int

    def encode_query(self, text: str) -> np.ndarray: ...
    def encode_documents(self, docs: Sequence[tuple[str, str]]) -> np.ndarray: ...
    def encode_images(self, paths: Sequence[str]) -> np.ndarray: ...


def is_out_of_memory(e: BaseException) -> bool:
    """CUDA / torch out-of-memory, whatever exception class this torch version uses."""
    return "out of memory" in str(e).lower() or type(e).__name__ == "OutOfMemoryError"


def free_gpu_memory() -> None:
    try:
        import gc

        import torch

        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


KEEP_RESERVED_BYTES = 2 * 1024**3


def release_idle_gpu_memory() -> None:
    """PyTorch keeps freed GPU memory reserved for reuse; hand it back above ~2 GB."""
    try:
        import torch

        if torch.cuda.is_available() and torch.cuda.memory_reserved() > KEEP_RESERVED_BYTES:
            torch.cuda.empty_cache()
    except Exception:
        pass


def _doc_prompt(title: str, text: str) -> str:
    title = (title or "none").replace("|", " ")
    return f"title: {title} | text: {text}"


class GemmaEmbedder:
    """The real thing. Loads lazily so `grain status` etc. stay instant."""

    def __init__(self, cfg: ModelCfg):
        self.cfg = cfg
        self.model_id = cfg.id
        self.dim = cfg.dim
        self._model = None
        self._load_error: Exception | None = None
        self.image_errors: dict[str, str] = {}
        self.device_desc = "not loaded"

    # -- loading ---------------------------------------------------------------
    def _load(self):
        if self._model is not None:
            return self._model
        if self._load_error is not None:  # never retry a failed load per file
            raise self._load_error
        try:
            return self._load_inner()
        except Exception as e:
            self._load_error = RuntimeError(f"Could not load {self.cfg.id}: {type(e).__name__}: {e}")
            raise self._load_error from e

    def _load_inner(self):
        missing = []
        for mod, pkg in (("torchvision", "torchvision"), ("PIL", "pillow")):
            try:
                __import__(mod)
            except ImportError:
                missing.append(pkg)
        if missing:  # the EmbeddingGemma 2 processor imports these even for text-only use
            raise ImportError(f"missing {', '.join(missing)} - run `uv sync` to install it")
        import torch
        from sentence_transformers import SentenceTransformer

        device = self.cfg.device
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        if device == "cuda" and torch.cuda.is_bf16_supported():
            dtype = torch.bfloat16
        else:
            dtype = torch.float32  # never float16 (NaNs / silent garbage)

        config_kwargs = {"audio_config": None}
        if not self.cfg.images:
            config_kwargs["vision_config"] = None

        self._model = SentenceTransformer(
            self.cfg.id,
            device=device,
            model_kwargs={"dtype": dtype},
            config_kwargs=config_kwargs,
        )
        # Bound memory: chunks are ~350 tokens, so 1024 never cuts real text but stops
        # one odd input from needing a huge attention buffer.
        try:
            cur = getattr(self._model, "max_seq_length", None)
            self._model.max_seq_length = min(cur or self.cfg.max_seq_tokens, self.cfg.max_seq_tokens)
        except Exception:
            pass
        if device == "cuda":
            try:  # leave the rest of the GPU to games and other apps
                torch.cuda.set_per_process_memory_fraction(max(0.1, min(1.0, self.cfg.gpu_memory_fraction)))
            except Exception:
                pass
        self.device_desc = f"{device} / {str(dtype).replace('torch.', '')}"
        if device == "cpu" and getattr(torch.version, "cuda", None) is None:
            self.device_desc += " (CPU-only PyTorch installed: run `uv sync` to get the GPU build)"
        return self._model

    def _encode(self, inputs, batch_size: int, **kw) -> np.ndarray:
        """Encode; on GPU out-of-memory, free cached memory and retry with smaller batches."""
        model = self._load()
        bs = max(1, batch_size)
        while True:
            try:
                out = model.encode(
                    inputs,
                    batch_size=bs,
                    truncate_dim=self.dim,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                    **kw,
                )
                release_idle_gpu_memory()
                return np.asarray(out, dtype=np.float32)
            except Exception as e:
                if not is_out_of_memory(e) or bs == 1:
                    raise
                free_gpu_memory()
                bs = max(1, bs // 2)

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def unload(self) -> None:
        """Drop the model and give its GPU memory back (~0.9 GB). The CUDA context stays until
        the process ends. The next encode loads the model again."""
        self._model = None
        self._load_error = None  # a later load may succeed (e.g. the GPU has room again)
        self.device_desc = "not loaded"
        free_gpu_memory()

    # -- public API -------------------------------------------------------------
    def warm_up(self) -> None:
        """Load the model and run one tiny encode, so problems show up immediately."""
        self.encode_query("warm up")
        self.encode_documents([("warm up", "warm up")])

    def encode_query(self, text: str) -> np.ndarray:
        return self._encode([text], 1, prompt_name="SearchQuery")[0]

    def encode_documents(self, docs: Sequence[tuple[str, str]]) -> np.ndarray:
        if not docs:
            return np.zeros((0, self.dim), np.float32)
        prompts = [_doc_prompt(t, x) for t, x in docs]
        return self._encode(prompts, self.cfg.text_batch)

    def encode_images(self, paths: Sequence[str]) -> np.ndarray:
        """One row per path. Unusable images get an all-zero row and a reason in
        `self.image_errors[path]`. GPU out-of-memory, or a model error that hits every
        image, is raised instead: that is a problem with the model, not the files."""
        self.image_errors = {}
        if not paths:
            return np.zeros((0, self.dim), np.float32)
        from .extractors.image import TOO_SMALL, load_for_embedding, too_small

        out = np.zeros((len(paths), self.dim), np.float32)
        items, idx = [], []
        for i, p in enumerate(paths):
            img = load_for_embedding(p)
            if img is None:
                self.image_errors[p] = "cannot open image (corrupt or unsupported format)"
            elif too_small(img):
                self.image_errors[p] = TOO_SMALL
            else:
                items.append({"image": img})
                idx.append(i)
        if not items:
            return out
        kw = {"processing_kwargs": {"image": {"max_soft_tokens": self.cfg.vision_tokens}}}
        try:
            out[idx] = self._encode(items, self.cfg.image_batch, **kw)
            return out
        except Exception as e:
            if is_out_of_memory(e):
                raise
        # the batch failed for another reason: find out which image(s), one by one
        errors: list[BaseException] = []
        for i, item in zip(idx, items):
            try:
                out[i] = self._encode([item], 1, **kw)[0]
            except Exception as e:
                if is_out_of_memory(e):
                    raise
                errors.append(e)
                self.image_errors[paths[i]] = f"{type(e).__name__}: {e}"[:300]
        if len(errors) == len(items) and len(items) > 1:
            raise RuntimeError(f"every image in the batch failed: {type(errors[0]).__name__}: {errors[0]}")
        return out


_WORD = re.compile(r"[a-z0-9]+")


class HashEmbedder:
    """Deterministic bag-of-words hashing. NOT semantic - for tests and dry runs only."""

    model_id = "fake/hash-embedder"

    def __init__(self, dim: int = 256):
        self.dim = dim
        self.device_desc = "fake"
        self.image_errors: dict[str, str] = {}
        self.loaded = False
        self.loads = 0  # how often the "model" was loaded (tests)

    def _ensure_loaded(self) -> None:  # behaves like the real model's lazy loading
        if not self.loaded:
            self.loaded = True
            self.loads += 1

    def unload(self) -> None:
        self.loaded = False

    def warm_up(self) -> None:
        self._ensure_loaded()

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, np.float32)
        for w in _WORD.findall(text.lower()):
            h = int.from_bytes(hashlib.blake2b(w.encode(), digest_size=8).digest(), "little")
            v[h % self.dim] += 1.0 if (h >> 63) == 0 else -1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def encode_query(self, text: str) -> np.ndarray:
        self._ensure_loaded()
        return self._vec(text)

    def encode_documents(self, docs: Sequence[tuple[str, str]]) -> np.ndarray:
        self._ensure_loaded()
        return np.stack([self._vec(f"{t} {x}") for t, x in docs]) if docs else np.zeros((0, self.dim), np.float32)

    def encode_images(self, paths: Sequence[str]) -> np.ndarray:
        # "describes" an image by its file name, e.g. red_car.png -> "red car"
        import os

        self._ensure_loaded()

        from .extractors.image import MIN_SIDE, TOO_SMALL

        self.image_errors = {}
        for p in paths:
            try:
                from PIL import Image

                with Image.open(p) as im:
                    if min(im.size) < MIN_SIDE:
                        self.image_errors[p] = TOO_SMALL
            except Exception:
                pass

        names = [os.path.splitext(os.path.basename(p))[0].replace("_", " ") for p in paths]
        out = np.stack([self._vec(n) for n in names]) if paths else np.zeros((0, self.dim), np.float32)
        for i, p in enumerate(paths):
            if p in self.image_errors:
                out[i] = 0
        return out


def make_embedder(cfg: ModelCfg, fake: bool = False) -> Embedder:
    return HashEmbedder() if fake else GemmaEmbedder(cfg)
