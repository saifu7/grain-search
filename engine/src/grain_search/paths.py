"""Where Grain Search keeps its files - the one place that decides it.

Windows (the target platform):
    %APPDATA%\\GrainSearch\\config.json          settings (folders, exclusions, ...)
    %LOCALAPPDATA%\\GrainSearch\\index\\          index.db, vector cache, thumbnails
    %LOCALAPPDATA%\\GrainSearch\\logs\\           service.log
    %LOCALAPPDATA%\\GrainSearch\\service.json     address + token of the running service

Linux/macOS (development and CI) follow the XDG convention:
    ~/.config/grain-search/config.json  and  ~/.local/share/grain-search/...

GRAIN_SEARCH_HOME=<folder> puts everything in that one folder instead (a portable copy, a
second test profile, or the test-suite).

The Rust app (app/src-tauri/src/lib.rs, `app_dirs`) resolves the same folders with the same
rules - keep the two in sync.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

APP_DIR_WINDOWS = "GrainSearch"
APP_DIR_POSIX = "grain-search"
HOME_ENV = "GRAIN_SEARCH_HOME"


@dataclass(frozen=True)
class AppPaths:
    config_dir: Path  # small, per-user settings (roams with the Windows profile)
    data_dir: Path  # large, machine-local data

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def index_dir(self) -> Path:
        """Default index location (can be moved with `index_dir` in config.json)."""
        return self.data_dir / "index"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def service_file(self) -> Path:
        return self.data_dir / "service.json"


def app_paths(environ: Mapping[str, str] | None = None, windows: bool | None = None) -> AppPaths:
    """Resolve the app folders. `environ` / `windows` exist so tests can check every case."""
    env = os.environ if environ is None else environ
    win = (os.name == "nt") if windows is None else windows

    home = env.get(HOME_ENV, "").strip()
    if home:
        p = Path(home).expanduser()
        return AppPaths(p, p)

    if win:
        user = env.get("USERPROFILE") or str(Path.home())
        roaming = env.get("APPDATA") or os.path.join(user, "AppData", "Roaming")
        local = env.get("LOCALAPPDATA") or os.path.join(user, "AppData", "Local")
        return AppPaths(Path(roaming) / APP_DIR_WINDOWS, Path(local) / APP_DIR_WINDOWS)

    user = env.get("HOME") or str(Path.home())
    config = env.get("XDG_CONFIG_HOME") or os.path.join(user, ".config")
    data = env.get("XDG_DATA_HOME") or os.path.join(user, ".local", "share")
    return AppPaths(Path(config) / APP_DIR_POSIX, Path(data) / APP_DIR_POSIX)
