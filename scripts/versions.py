"""Shared helpers: where the version lives, and SemVer <-> Python (PEP 440) conversion."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "engine" / "pyproject.toml"
PACKAGE_JSON = ROOT / "app" / "package.json"
CARGO_TOML = ROOT / "app" / "src-tauri" / "Cargo.toml"
TAURI_CONF = ROOT / "app" / "src-tauri" / "tauri.conf.json"

SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?$")
_PEP440_SUFFIX = {"alpha": "a", "beta": "b", "rc": "rc"}


def to_pep440(semver: str) -> str:
    """0.2.0-beta.1 -> 0.2.0b1 (Python packages use PEP 440 spelling)."""
    m = SEMVER.match(semver)
    if not m:
        raise ValueError(f"not a supported version: {semver!r} (use X.Y.Z or X.Y.Z-alpha|beta|rc.N)")
    base = ".".join(m.group(1, 2, 3))
    return base + (f"{_PEP440_SUFFIX[m.group(4)]}{m.group(5)}" if m.group(4) else "")


def current_versions() -> dict[str, str]:
    """Version string in each file, as written there."""
    return {
        "engine/pyproject.toml": tomllib.loads(PYPROJECT.read_text("utf-8"))["project"]["version"],
        "app/package.json": json.loads(PACKAGE_JSON.read_text("utf-8"))["version"],
        "app/src-tauri/Cargo.toml": tomllib.loads(CARGO_TOML.read_text("utf-8"))["package"]["version"],
        "app/src-tauri/tauri.conf.json": json.loads(TAURI_CONF.read_text("utf-8"))["version"],
    }
