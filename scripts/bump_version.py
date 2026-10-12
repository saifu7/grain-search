"""Set one version everywhere.

    uv run --no-project python scripts/bump_version.py 0.2.0
    uv run --no-project python scripts/bump_version.py 0.2.0-beta.1

Updates engine/pyproject.toml (PEP 440 spelling, e.g. 0.2.0b1), app/package.json,
app/package-lock.json, app/src-tauri/Cargo.toml, app/src-tauri/Cargo.lock,
app/src-tauri/tauri.conf.json and engine/uv.lock.
"""

from __future__ import annotations

import re
import sys

from versions import CARGO_TOML, PACKAGE_JSON, PYPROJECT, ROOT, SEMVER, TAURI_CONF, to_pep440


def _sub_once(path, pattern: str, repl: str, flags: int = 0) -> None:
    text = path.read_text("utf-8")
    new, n = re.subn(pattern, repl, text, count=1, flags=flags)
    if n != 1:
        raise SystemExit(f"could not find the version in {path.relative_to(ROOT)}")
    path.write_text(new, "utf-8")


def main() -> int:
    if len(sys.argv) != 2 or not SEMVER.match(sys.argv[1].removeprefix("v")):
        print(__doc__)
        return 2
    semver = sys.argv[1].removeprefix("v")
    py = to_pep440(semver)

    _sub_once(PYPROJECT, r'(?m)^(version = )"[^"]+"', rf'\g<1>"{py}"')
    _sub_once(ROOT / "engine" / "uv.lock", r'(name = "grain-search"\nversion = )"[^"]+"', rf'\g<1>"{py}"')
    _sub_once(PACKAGE_JSON, r'("version":\s*)"[^"]+"', rf'\g<1>"{semver}"')
    lock = ROOT / "app" / "package-lock.json"
    for _ in range(2):  # top level and packages[""]
        text = lock.read_text("utf-8")
        text = re.sub(r'("name": "grain-search-app",\s*"version": )"[^"]+"', rf'\g<1>"{semver}"', text)
        lock.write_text(text, "utf-8")
    _sub_once(CARGO_TOML, r'(?m)^(version = )"[^"]+"', rf'\g<1>"{semver}"')
    _sub_once(
        ROOT / "app" / "src-tauri" / "Cargo.lock", r'(name = "grain-search"\nversion = )"[^"]+"', rf'\g<1>"{semver}"'
    )
    _sub_once(TAURI_CONF, r'("version":\s*)"[^"]+"', rf'\g<1>"{semver}"')
    print(f"version set to {semver} (Python: {py})")
    print("Next: move the Unreleased section of CHANGELOG.md under this version, then open a PR.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
