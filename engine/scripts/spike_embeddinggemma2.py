"""Phase 0 spike: does EmbeddingGemma 2 work on this PC, how fast is it, and does
"describe a picture -> find the picture" actually find YOUR photos?

Usage (PowerShell):
    python scripts\\spike_embeddinggemma2.py --images "$env:USERPROFILE\\Pictures" --limit 100 `
        --texts "$env:USERPROFILE\\Documents" `
        --query "a dog sleeping on a sofa" `
        --query "sunset over the sea"

What it prints:
    * device / dtype and model load time
    * images per second and text files per second (to estimate a full index)
    * for every query: top-5 images WITH and WITHOUT the SearchQuery prompt,
      and top-5 text files, plus query latency
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import torch
from sentence_transformers import SentenceTransformer

MODEL_ID = "google/embeddinggemma-2"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
TEXT_EXTS = {".txt", ".md", ".csv", ".log", ".json", ".py", ".js", ".html"}


def pick_dtype() -> torch.dtype:
    # Model card: never float16. bf16 on GPUs that support it, else float32.
    if torch.cuda.is_available() and torch.cuda.is_bf16_supported():
        return torch.bfloat16
    return torch.float32


def find_files(folder: str | None, exts: set[str], limit: int) -> list[Path]:
    if not folder:
        return []
    root = Path(folder)
    if not root.exists():
        raise SystemExit(f"Folder not found: {root}")
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in exts]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)  # newest first
    return files[:limit]


def show(title: str, scores: torch.Tensor, paths: list[Path], k: int = 5) -> None:
    top = torch.topk(scores, k=min(k, len(paths)))
    print(f"  {title}")
    for score, idx in zip(top.values.tolist(), top.indices.tolist()):
        print(f"    {score:.3f}  {paths[idx]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--images", help="folder with photos to test text->image search")
    ap.add_argument("--texts", help="folder with .txt/.md/... files to test sentence->file search")
    ap.add_argument("--query", action="append", required=True, help="repeat for several queries")
    ap.add_argument("--limit", type=int, default=100, help="max files per kind (newest first)")
    ap.add_argument("--dim", type=int, default=768, choices=[768, 512, 256, 128])
    ap.add_argument("--vision-tokens", type=int, default=280, choices=[70, 140, 280, 560, 1120])
    ap.add_argument("--batch", type=int, default=8)
    args = ap.parse_args()

    if not args.images and not args.texts:
        raise SystemExit("Give --images and/or --texts")

    def enc(inputs, **kw):
        return model.encode(inputs, truncate_dim=args.dim, normalize_embeddings=True, **kw)

    # 1) Load model: text + image only (audio encoder not loaded -> ~440M params)
    dtype = pick_dtype()
    t0 = time.perf_counter()
    model = SentenceTransformer(
        MODEL_ID,
        model_kwargs={"dtype": dtype},
        config_kwargs={"audio_config": None},
    )
    print(f"Loaded {MODEL_ID} on {model.device} ({dtype}) in {time.perf_counter() - t0:.1f}s")

    # 2) Embed images (no prefix for media)
    img_paths = find_files(args.images, IMAGE_EXTS, args.limit)
    img_emb = None
    if img_paths:
        t0 = time.perf_counter()
        img_emb = enc(
            [{"image": str(p)} for p in img_paths],
            batch_size=args.batch,
            processing_kwargs={"image": {"max_soft_tokens": args.vision_tokens}},
            show_progress_bar=True,
        )
        dt = time.perf_counter() - t0
        print(
            f"Images: {len(img_paths)} in {dt:.1f}s -> {len(img_paths) / dt:.2f} img/s "
            f"(vision budget {args.vision_tokens})"
        )

    # 3) Embed text files as documents: "title: {name} | text: {content}"
    txt_paths = find_files(args.texts, TEXT_EXTS, args.limit)
    txt_emb = None
    if txt_paths:
        docs = []
        for p in txt_paths:
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")[:4000]
            except OSError:
                content = ""
            docs.append(f"title: {p.name} | text: {content}")
        t0 = time.perf_counter()
        txt_emb = enc(docs, batch_size=32, show_progress_bar=True)
        dt = time.perf_counter() - t0
        print(f"Texts:  {len(txt_paths)} in {dt:.1f}s -> {len(txt_paths) / dt:.2f} files/s")

    # 4) Queries
    for q in args.query:
        t0 = time.perf_counter()
        q_prompt = enc(q, prompt_name="SearchQuery")
        latency_ms = (time.perf_counter() - t0) * 1000
        print(f"\nQuery: {q!r}   (encode {latency_ms:.0f} ms)")
        if img_emb is not None:
            q_plain = enc(q)
            show("images - with SearchQuery prompt", model.similarity(q_prompt, img_emb)[0], img_paths)
            show("images - no prompt", model.similarity(q_plain, img_emb)[0], img_paths)
        if txt_emb is not None:
            show("text files - SearchQuery prompt", model.similarity(q_prompt, txt_emb)[0], txt_paths)

    print(
        "\nNote: text->image scores are naturally lower than text->text scores "
        "(not calibrated across modalities). Compare rankings, not raw numbers."
    )


if __name__ == "__main__":
    main()
