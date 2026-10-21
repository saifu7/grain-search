"""Where this user's standard folders really are: Documents, Pictures, Desktop, Downloads.

Never build `C:\\Users\\<name>\\Documents` by hand: OneDrive backup moves these folders, users can
move them to another drive, and the names on disk can differ from what Explorer shows.
Windows: ask the Known Folders API. Elsewhere: the XDG user folders (~/.config/user-dirs.dirs).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# (key, label shown in Settings)
SUGGESTED: list[tuple[str, str]] = [
    ("documents", "Documents"),
    ("pictures", "Pictures"),
    ("desktop", "Desktop"),
    ("downloads", "Downloads"),
]

# https://learn.microsoft.com/windows/win32/shell/knownfolderid
_FOLDER_IDS = {
    "documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "downloads": "374DE290-123F-4565-9164-39C4925E467B",
}

# XDG names, and the usual English fallback folder
_XDG = {
    "documents": ("XDG_DOCUMENTS_DIR", "Documents"),
    "pictures": ("XDG_PICTURES_DIR", "Pictures"),
    "desktop": ("XDG_DESKTOP_DIR", "Desktop"),
    "downloads": ("XDG_DOWNLOAD_DIR", "Downloads"),
}


def _windows_known_folder(key: str) -> str | None:
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    guid = GUID.from_buffer_copy(uuid.UUID(_FOLDER_IDS[key]).bytes_le)
    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    shell32.SHGetKnownFolderPath.argtypes = (
        ctypes.POINTER(GUID),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_void_p),
    )
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long  # HRESULT
    ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
    ole32.CoTaskMemFree.restype = None

    buf = ctypes.c_void_p()
    hr = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(buf))
    try:
        return ctypes.wstring_at(buf.value) if hr == 0 and buf.value else None
    finally:
        ole32.CoTaskMemFree(buf)  # Windows allocated the string; must be freed even on failure


def _xdg_folder(key: str, home: Path, environ) -> str:
    var, fallback = _XDG[key]
    config_home = Path(environ.get("XDG_CONFIG_HOME") or home / ".config")
    try:
        text = (config_home / "user-dirs.dirs").read_text(encoding="utf-8")
        m = re.search(rf'^{var}="(.*)"\s*$', text, re.MULTILINE)
        if m:
            return m.group(1).replace("$HOME", str(home))
    except OSError:
        pass
    return str(home / fallback)


def known_folders(*, windows: bool | None = None, home: Path | None = None, environ=None) -> list[dict]:
    """The suggested folders that exist on this PC: [{key, label, path}]. Missing ones are skipped."""
    windows = sys.platform == "win32" if windows is None else windows
    environ = os.environ if environ is None else environ
    home = home or Path.home()
    out = []
    for key, label in SUGGESTED:
        try:
            path = _windows_known_folder(key) if windows else _xdg_folder(key, home, environ)
        except Exception:
            path = None
        if path and os.path.isdir(path):
            out.append({"key": key, "label": label, "path": os.path.normpath(path)})
    return out
