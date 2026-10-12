"""Images: EXIF metadata at index time, and a safe loader for the embedder."""

from __future__ import annotations

from . import Extracted

try:  # iPhone .heic support, optional
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:
    pass

MAX_SIDE = 1536  # downscale huge photos before embedding: faster, same result
MIN_SIDE = 32  # icons, 1-px spacers and line strips are useless to search (and confuse the processor)
TOO_SMALL = f"too small to index (under {MIN_SIDE} px)"


def too_small(img) -> bool:
    return min(img.size) < MIN_SIDE


EXIF_DATETIME_ORIGINAL = 36867
EXIF_DATETIME = 306
EXIF_MODEL = 272


def extract_image(path: str) -> Extracted:
    from PIL import Image

    meta: dict = {}
    try:
        with Image.open(path) as im:
            meta["width"], meta["height"] = im.size
            exif = im.getexif()
            if exif:
                sub = exif.get_ifd(0x8769) if hasattr(exif, "get_ifd") else {}
                taken = sub.get(EXIF_DATETIME_ORIGINAL) or exif.get(EXIF_DATETIME)
                if taken:
                    meta["taken"] = str(taken).replace(":", "-", 2)
                if exif.get(EXIF_MODEL):
                    meta["camera"] = str(exif.get(EXIF_MODEL)).strip()
    except Exception as e:  # corrupt / unsupported
        meta["error"] = str(e)[:200]
    return Extracted([], meta)


def load_for_embedding(path: str):
    """Open, fix orientation, convert to RGB, downscale. None if unreadable."""
    from PIL import Image, ImageOps

    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            if getattr(im, "is_animated", False):
                im.seek(0)
            im = im.convert("RGB")
            im.thumbnail((MAX_SIDE, MAX_SIDE))
            return im.copy()
    except Exception:
        return None
