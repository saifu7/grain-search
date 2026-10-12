"""Repository hygiene, run by pre-commit and CI.

1. No personal data or secrets are tracked: indexes, config.yaml, settings.json, service.json,
   model weights, keys, or real Windows user-profile paths in text files.
2. Every version file carries the same version (and matches the tag, with --tag vX.Y.Z).

Usage:  uv run --no-project python scripts/check_repo.py [--tag v0.2.0]
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys

from versions import ROOT, SEMVER, current_versions, to_pep440

FORBIDDEN = [
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "*.npz",
    "*/data/*",
    "data/*",
    "config.yaml",
    "*/config.yaml",
    "settings.json",
    "*/settings.json",
    "*service.json",
    "*.env",
    "*.safetensors",
    "*.bin",
    "*.onnx",
    "*.gguf",
    "*.pt",
    "*.pth",
    "*.key",
    "*.pem",
    "*.pfx",
    "*.p12",
    "*.tgz",
]
ALLOWED = ["*.example.yaml", "*.example.json"]

# A real user-profile path (C:\Users\alice\...). Placeholders like <me>, %USERPROFILE%, Public are fine.
USER_PATH = re.compile(r"[A-Za-z]:\\{1,2}Users\\{1,2}(?!<|%|\{|Public\b|Default\b|you\b|me\b|USERNAME\b)[\w.-]+", re.I)
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".rs", ".json", ".toml", ".yaml", ".yml", ".md", ".html", ".css", ".txt"}
SKIP_TEXT = {"scripts/check_repo.py"}


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    return [p for p in out.stdout.decode("utf-8").split("\0") if p]


def check_files(files: list[str]) -> list[str]:
    errors = []
    for f in files:
        if any(fnmatch.fnmatch(f, a) for a in ALLOWED):
            continue
        if any(fnmatch.fnmatch(f, pat) for pat in FORBIDDEN):
            errors.append(f"personal/secret file must not be committed: {f}")
    for f in files:
        p = ROOT / f
        if f in SKIP_TEXT or p.suffix.lower() not in TEXT_SUFFIXES or not p.is_file():
            continue
        if p.name in {"package-lock.json", "Cargo.lock", "uv.lock"}:
            continue
        for n, line in enumerate(p.read_text("utf-8", errors="replace").splitlines(), 1):
            if USER_PATH.search(line):
                errors.append(f"{f}:{n}: looks like a real user-profile path; use %USERPROFILE% or <me>")
    return errors


def check_versions(tag: str | None) -> list[str]:
    errors = []
    v = current_versions()
    semver_values = {k: val for k, val in v.items() if k != "engine/pyproject.toml"}
    distinct = set(semver_values.values())
    if len(distinct) != 1:
        return [f"versions differ: {v} - run scripts/bump_version.py"]
    semver = distinct.pop()
    if not SEMVER.match(semver):
        errors.append(f"version {semver!r} is not X.Y.Z or X.Y.Z-alpha|beta|rc.N")
        return errors
    if v["engine/pyproject.toml"] != to_pep440(semver):
        errors.append(f"engine/pyproject.toml has {v['engine/pyproject.toml']}, expected {to_pep440(semver)}")
    if tag is not None and tag.removeprefix("v") != semver:
        errors.append(f"tag {tag} does not match version {semver} in the files")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", help="release tag to compare against, e.g. v0.2.0")
    args = ap.parse_args()
    errors = check_files(tracked_files()) + check_versions(args.tag)
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    if not errors:
        print("repository check: ok")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
