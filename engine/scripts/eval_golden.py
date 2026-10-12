"""Measure search quality on YOUR files with YOUR queries.

    uv run python scripts/eval_golden.py              # uses tests/golden_queries.yaml
    uv run python scripts/eval_golden.py --show-misses

Prints top-1, recall@5 and MRR for documents and images. Re-run after changing
config.yaml (dim, vision_tokens, chunking, image_min_score) to see what helped.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from grain_search.config import load_config  # noqa: E402
from grain_search.embedder import make_embedder  # noqa: E402
from grain_search.search import Searcher  # noqa: E402
from grain_search.store import Store  # noqa: E402


def norm(p: str) -> str:
    return os.path.normcase(p.replace("/", "\\")).rstrip("\\")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(ROOT / "tests" / "golden_queries.yaml"))
    ap.add_argument("--show-misses", action="store_true")
    args = ap.parse_args()

    golden = yaml.safe_load(Path(args.file).read_text(encoding="utf-8"))
    cfg = load_config()
    store = Store(cfg.db_path)
    emb = make_embedder(cfg.model)
    store.check_model(emb.model_id, emb.dim)
    s = Searcher(cfg, store, emb)

    stats = {}
    for g in golden:
        kind = g.get("kind", "doc")
        res = s.search(g["query"], limit=10)
        hits = [h.path for h in (res.images if kind == "img" else res.docs)]
        want = norm(g["expect"])
        rank = next((i + 1 for i, h in enumerate(hits) if norm(h).endswith(want)), None)
        st = stats.setdefault(kind, {"n": 0, "top1": 0, "r5": 0, "mrr": 0.0})
        st["n"] += 1
        st["top1"] += rank == 1
        st["r5"] += bool(rank and rank <= 5)
        st["mrr"] += 1 / rank if rank else 0
        if args.show_misses and rank != 1:
            print(f"[{kind}] rank={rank}  {g['query']!r}\n      want {g['expect']}\n      got  {hits[:3]}")

    for kind, st in stats.items():
        n = st["n"]
        print(f"{kind:>4}: n={n}  top-1 {st['top1'] / n:.0%}  recall@5 {st['r5'] / n:.0%}  MRR {st['mrr'] / n:.3f}")


if __name__ == "__main__":
    main()
