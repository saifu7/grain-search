"""The AI model's files on disk: is it here, where, and downloading it (Settings -> Download model).

The app never downloads the model silently. It is used from:
1. the model folder (`model.dir` in config.json, default %LOCALAPPDATA%\\GrainSearch\\models),
   once a download there has finished; or
2. the Hugging Face cache, if the model is already there (e.g. from development with `uv run`),
   so nobody has to download 1.4 GB twice.
Otherwise it is "missing": indexing waits and searches find file names and exact text only.

The download runs in a separate process (`python -m grain_search.model_files <repo> <folder>`), so
Cancel can stop it at once; huggingface_hub resumes the unfinished files next time.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from .config import Config, ModelCfg
from .paths import app_paths

DONE_MARKER = ".grain-complete.json"  # written after every file has arrived
INFO_FILE = ".grain-download.json"  # total size, written when a download starts
SPACE_MARGIN = 200 * 2**20  # keep this much free on top of the model


class ModelMissing(RuntimeError):
    """The model isn't on this PC yet (download it in Settings)."""


@dataclass
class Location:
    state: str  # "folder" | "cache" | "missing"
    path: str | None  # what to load: a folder with the model files
    folder: str  # where a download goes / went


def models_folder(model: ModelCfg) -> Path:
    return Path(model.dir) if model.dir else app_paths().models_dir


def model_folder(model: ModelCfg) -> Path:
    """<models folder>/<model name>, e.g. ...\\models\\embeddinggemma-2"""
    return models_folder(model) / model.id.split("/")[-1]


def is_complete(folder: Path) -> bool:
    return (folder / DONE_MARKER).is_file() and (folder / "config.json").is_file()


def cached_copy(repo_id: str) -> str | None:
    """The model's folder in the Hugging Face cache, if it is fully there."""
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return None
    try:
        found = [try_to_load_from_cache(repo_id, f) for f in ("config.json", "model.safetensors", "modules.json")]
    except Exception:
        return None
    if all(isinstance(f, str) and os.path.isfile(f) for f in found):
        return str(Path(found[0]).parent)
    return None


def locate(model: ModelCfg, cache_lookup=cached_copy) -> Location:
    folder = model_folder(model)
    if is_complete(folder):
        return Location("folder", str(folder), str(folder))
    cached = cache_lookup(model.id)
    if cached:
        return Location("cache", cached, str(folder))
    return Location("missing", None, str(folder))


def use_location(model: ModelCfg, loc: Location) -> None:
    """Point the embedder at the files found; never let it download on its own."""
    model.source = loc.path
    model.local_only = True


def folder_bytes(folder: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(folder):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _read_info(folder: Path) -> dict:
    try:
        return json.loads((folder / INFO_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class ModelDownloader:
    """Runs one download at a time in a child process and reports progress from the bytes on disk."""

    def __init__(self, cfg: Config, on_done=lambda: None, command=None, cache_lookup=cached_copy):
        self.cfg = cfg
        self.cache_lookup = cache_lookup  # tests swap this: the developer's own cache may hold the model
        self.on_done = on_done
        self.command = command or self._python_command
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._cancelled = False
        self.error = ""

    @staticmethod
    def _python_command(repo_id: str, folder: str) -> list[str]:
        return [sys.executable, "-m", "grain_search.model_files", repo_id, folder]

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def locate(self) -> Location:
        return locate(self.cfg.model, cache_lookup=self.cache_lookup)

    def status(self) -> dict:
        loc = self.locate()
        folder = Path(loc.folder)
        state = "downloading" if self.running else ("ready" if loc.state != "missing" else "missing")
        out = {
            "state": state,
            "source": loc.state,  # folder | cache | missing
            "path": loc.path,
            "folder": loc.folder,
            "models_folder": str(models_folder(self.cfg.model)),
            "model": self.cfg.model.id,
            "error": "" if state == "ready" else self.error,
        }
        if state == "downloading" or (state == "missing" and folder.is_dir()):
            info = _read_info(folder)
            out["total_bytes"] = info.get("total")
            out["done_bytes"] = folder_bytes(folder)
        return out

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            if self.locate().state != "missing":
                return
            folder = model_folder(self.cfg.model)
            folder.mkdir(parents=True, exist_ok=True)
            self.error, self._cancelled = "", False
            kw: dict = {}
            if sys.platform == "win32":
                kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            env = {**os.environ, "HF_HUB_DISABLE_PROGRESS_BARS": "1", "PYTHONUNBUFFERED": "1"}
            self._proc = subprocess.Popen(
                self.command(self.cfg.model.id, str(folder)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=env,
                **kw,
            )
            threading.Thread(target=self._wait, args=(self._proc,), name="model-download", daemon=True).start()

    def _wait(self, proc: subprocess.Popen) -> None:
        _, err = proc.communicate()
        if self._cancelled:
            self.error = ""
            return
        if proc.returncode == 0 and is_complete(model_folder(self.cfg.model)):
            self.error = ""
            self.on_done()
            return
        lines = (err or b"").decode("utf-8", "replace").strip().splitlines()
        self.error = lines[-1] if lines else f"The download stopped (code {proc.returncode})."
        self.error = self.error.removeprefix("DownloadError: ")

    def cancel(self) -> None:
        with self._lock:
            if self.running:
                self._cancelled = True
                self._proc.kill()

    def close(self) -> None:
        self.cancel()


# -- the child process ------------------------------------------------------------------
class DownloadError(Exception):
    pass


def _gb(n: int) -> str:
    return f"{n / 2**30:.1f} GB"


def download(repo_id: str, folder: str) -> None:
    """Download every file of the model into `folder`, resuming unfinished files."""
    from huggingface_hub import HfApi, snapshot_download

    dest = Path(folder)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        info = HfApi().model_info(repo_id, files_metadata=True)
    except Exception as e:
        raise DownloadError(f"Can't reach Hugging Face to download the model ({type(e).__name__}).") from e
    total = sum((s.size or 0) for s in info.siblings or [])
    (dest / INFO_FILE).write_text(json.dumps({"total": total, "revision": info.sha}), encoding="utf-8")
    need = total - folder_bytes(dest) + SPACE_MARGIN
    free = shutil.disk_usage(dest).free
    if free < need:
        raise DownloadError(f"Not enough space in {dest}: needs {_gb(need)}, {_gb(free)} free.")
    try:
        snapshot_download(repo_id, revision=info.sha, local_dir=str(dest))
    except Exception as e:
        raise DownloadError(f"The download stopped: {type(e).__name__}: {e}"[:400]) from e
    (dest / DONE_MARKER).write_text(json.dumps({"model": repo_id, "revision": info.sha}), encoding="utf-8")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m grain_search.model_files <repo id> <folder>", file=sys.stderr)
        return 2
    try:
        download(argv[0], argv[1])
    except DownloadError as e:
        print(f"DownloadError: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
