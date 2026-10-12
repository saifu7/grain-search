"""File -> list[Segment]. Each extractor returns plain text segments with the
page (PDF page / slide number) and starting line where they came from."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

TEXT_EXTS = {
    ".txt",
    ".md",
    ".markdown",
    ".rst",
    ".csv",
    ".tsv",
    ".log",
    ".ini",
    ".cfg",
    ".conf",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".sql",
    ".tex",
    ".srt",
    ".vtt",
    ".py",
    ".ipynb",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".kt",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".cs",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".swift",
    ".m",
    ".r",
    ".lua",
    ".dart",
    ".scala",
    ".sh",
    ".bat",
    ".ps1",
    ".vue",
    ".svelte",
}
PDF_EXTS = {".pdf"}
OFFICE_EXTS = {".docx", ".pptx", ".xlsx"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic", ".heif"}


def kind_of(path: str) -> str | None:
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXT_EXTS:
        return "text"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in OFFICE_EXTS:
        return "office"
    if ext in IMAGE_EXTS:
        return "image"
    return None


@dataclass
class Segment:
    text: str
    page: int | None = None  # 1-based PDF page / slide; None for plain text
    line: int | None = None  # 1-based starting line in the file, when known


@dataclass
class Extracted:
    segments: list[Segment] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


def extract(path: str, kind: str) -> Extracted:
    if kind == "text":
        from .text import extract_text

        return extract_text(path)
    if kind == "pdf":
        from .pdf import extract_pdf

        return extract_pdf(path)
    if kind == "office":
        from .office import extract_office

        return extract_office(path)
    if kind == "image":
        from .image import extract_image

        return extract_image(path)
    return Extracted()
