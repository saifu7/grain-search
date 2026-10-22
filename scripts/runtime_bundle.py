"""Build the `engine` folder the installer ships next to the app (Phase 4).

The installer stays small (~15 MB): it carries our own code and `uv`, and the app's setup window
downloads Python, the libraries and PyTorch on first start, at exactly the versions in uv.lock.

    uv run --no-project python scripts/runtime_bundle.py            # -> app/src-tauri/resources/engine
    uv run --no-project python scripts/runtime_bundle.py --out DIR --uv PATH

Writes into the output folder:
  grain_search-<version>-py3-none-any.whl   the engine
  requirements.txt                          every library pinned from uv.lock, except PyTorch's family
  runtime.json                              Python version, PyTorch versions and download sites, fingerprint
  uv.exe (uv on Linux/macOS)                the installer tool
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlparse

from versions import ROOT

ENGINE = ROOT / "engine"
DEFAULT_OUT = ROOT / "app" / "src-tauri" / "resources" / "engine"
PYTHON = "3.12"  # same as CI
PYTORCH_HOST = "download.pytorch.org"
CPU_INDEX = f"https://{PYTORCH_HOST}/whl/cpu"

# Installed separately, from PyTorch's own download site (CUDA or CPU build). These packages only
# exist for PyTorch's sake (its GPU libraries on Linux), so they are left to that step too.
GENERATED = {"requirements.txt", "runtime.json", "uv", "uv.exe"}
TORCH_FAMILY = {"torch", "torchvision"}
TORCH_ONLY = re.compile(r"^(nvidia-|cuda-|triton\b)")


def is_pytorch_site(url: str) -> bool:
    """True only for https://download.pytorch.org/... (the host itself, not a look-alike)."""
    u = urlparse(url)
    return u.scheme == "https" and u.hostname == PYTORCH_HOST


def torch_pins(lock_text: str) -> dict:
    """PyTorch and torchvision as locked for Windows (the CUDA build), without the +cu128 suffix."""
    lock = tomllib.loads(lock_text)
    out: dict = {}
    for pkg in lock.get("package", []):
        name = pkg.get("name")
        registry = (pkg.get("source") or {}).get("registry", "")
        if name in TORCH_FAMILY and is_pytorch_site(registry):
            out[name] = pkg["version"].split("+", 1)[0]
            out["cuda_index"] = registry.rstrip("/")
    missing = TORCH_FAMILY - out.keys()
    if missing:
        raise SystemExit(f"uv.lock has no PyTorch-site entry for {', '.join(sorted(missing))}")
    return out


def requirements(export_text: str) -> str:
    """`uv export` output -> the shared libraries (PyTorch's family removed), one pin per line."""
    keep = []
    for line in export_text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        name = re.split(r"[=<>!~ ;\[]", line, maxsplit=1)[0].lower()
        if name in TORCH_FAMILY or TORCH_ONLY.match(name):
            continue
        keep.append(line)
    return "\n".join(keep) + "\n"


def fingerprint(reqs: str, pins: dict) -> str:
    """Changes only when what has to be downloaded changes (not for engine-code-only updates)."""
    h = hashlib.sha256()
    h.update(reqs.encode())
    h.update(json.dumps({"python": PYTHON, "torch": pins["torch"], "torchvision": pins["torchvision"]}).encode())
    return h.hexdigest()[:16]


def run(cmd: list[str], **kw) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout


def build_bundle(out: Path, uv: str, runner=run) -> dict:
    """Write the bundle into `out` (emptied first). `runner(cmd, cwd=...)` runs uv (tests swap it)."""
    out.mkdir(parents=True, exist_ok=True)
    for old in out.iterdir():  # only what this script writes; the folder's README stays
        if old.name.startswith("grain_search-") or old.name in GENERATED:
            old.unlink()

    runner([uv, "build", "--wheel", "--out-dir", str(out)], cwd=ENGINE)
    wheels = sorted(out.glob("grain_search-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected one engine wheel, found {len(wheels)}")

    export = runner(
        [uv, "export", "--frozen", "--no-dev", "--no-hashes", "--no-emit-project", "--no-header", "--no-annotate"],
        cwd=ENGINE,
    )
    reqs = requirements(export)
    (out / "requirements.txt").write_text(reqs, encoding="utf-8")

    pins = torch_pins((ENGINE / "uv.lock").read_text("utf-8"))
    version = tomllib.loads((ENGINE / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    info = {
        "version": version,
        "python": PYTHON,
        "torch": pins["torch"],
        "torchvision": pins["torchvision"],
        "torch_index": {"cuda": pins["cuda_index"], "cpu": CPU_INDEX},
        "wheel": wheels[0].name,
        "requirements": "requirements.txt",
        "fingerprint": fingerprint(reqs, pins),
    }
    (out / "runtime.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")

    uv_name = "uv.exe" if uv.lower().endswith(".exe") else "uv"
    shutil.copy2(uv, out / uv_name)
    return info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--uv", help="uv executable to bundle (default: the one on PATH)")
    args = ap.parse_args(argv)
    uv = args.uv or shutil.which("uv")
    if not uv:
        raise SystemExit("uv not found on PATH: install it or pass --uv")
    info = build_bundle(args.out, uv)
    print(f"runtime bundle in {args.out}: {info['wheel']}, torch {info['torch']}, fingerprint {info['fingerprint']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
