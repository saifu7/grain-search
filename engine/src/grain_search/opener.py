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
# Windows: the search service runs inside the app's job object, which ends every process in it
# when the app quits. Programs we open for the user must outlive the app, so they break away.
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000

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


def _launch(args, *, windows: bool | None = None, flags: int = 0, popen=subprocess.Popen, **kw):
    """Start a program that must stay open after Grain Search quits."""
    if windows if windows is not None else sys.platform == "win32":
        try:
            return popen(args, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kw)
        except OSError:
            pass  # in a job that forbids breaking away: start it normally
        return popen(args, creationflags=flags, **kw)
    return popen(args, **kw)


def _startfile_outside_job(path: str, popen=subprocess.Popen) -> None:
    """os.startfile (open with the default app, like a double-click) from a short-lived helper
    that breaks away from the job: the app it starts is the helper's child, outside the job.
    Raises OSError with Windows' message if the file cannot be opened."""
    helper = _launch(
        [sys.executable, "-I", "-S", "-c", "import os, sys; os.startfile(sys.argv[1])", path],
        windows=True,
        flags=CREATE_NO_WINDOW,
        popen=popen,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _, err = helper.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        return  # still busy handing over to the app (e.g. a slow network drive): fine
    if helper.returncode:
        lines = (err or b"").decode("utf-8", "replace").strip().splitlines()
        raise OSError(lines[-1] if lines else "Windows could not open this file")


def open_file(path: str, page: int | None = None, line: int | None = None, at_location: bool = True) -> str:
    """Opens the file. Returns a short description of how it was opened."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    ext = os.path.splitext(path)[1].lower()
    if at_location:
        if ext == ".pdf" and page and (s := _sumatra()):
            _launch([s, "-reuse-instance", "-page", str(page), path])
            return f"SumatraPDF, page {page}"
        if ext in CODE_EXTS and line and (code := shutil.which("code")):
            _launch([code, "-g", f"{path}:{line}"], shell=sys.platform == "win32", flags=CREATE_NO_WINDOW)
            return f"VS Code, line {line}"
    if sys.platform == "win32":
        _startfile_outside_job(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])
    return "default app"


def reveal(path: str) -> None:
    """Show the file selected in Explorer / Finder."""
    if sys.platform == "win32":
        _launch(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])
