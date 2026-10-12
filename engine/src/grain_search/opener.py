"""Open a result: default app, reveal in Explorer, or jump to page/line when we can."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

CODE_EXTS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".cs",
    ".go",
    ".rs",
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".html",
    ".css",
    ".sql",
    ".log",
    ".csv",
}
SUMATRA_PATHS = [
    r"%LOCALAPPDATA%\SumatraPDF\SumatraPDF.exe",
    r"C:\Program Files\SumatraPDF\SumatraPDF.exe",
]


def _sumatra() -> str | None:
    for p in SUMATRA_PATHS:
        p = os.path.expandvars(p)
        if os.path.exists(p):
            return p
    return shutil.which("SumatraPDF")


def open_file(path: str, page: int | None = None, line: int | None = None, at_location: bool = True) -> str:
    """Opens the file. Returns a short description of how it was opened."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    ext = os.path.splitext(path)[1].lower()
    if at_location:
        if ext == ".pdf" and page and (s := _sumatra()):
            subprocess.Popen([s, "-reuse-instance", "-page", str(page), path])
            return f"SumatraPDF, page {page}"
        if ext in CODE_EXTS and line and (code := shutil.which("code")):
            subprocess.Popen([code, "-g", f"{path}:{line}"], shell=sys.platform == "win32")
            return f"VS Code, line {line}"
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - default app, like double-click
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])
    return "default app"


def reveal(path: str) -> None:
    """Show the file selected in Explorer / Finder."""
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])
