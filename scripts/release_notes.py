"""Print the CHANGELOG.md section for one version (used as the GitHub Release notes).

uv run --no-project python scripts/release_notes.py 0.2.0
"""

from __future__ import annotations

import re
import sys

from versions import ROOT

FOOTER = """
---
**Download:** `GrainSearch-{v}-x64-setup.exe` below. Check it against `SHA256SUMS.txt`, or verify it
was built by this repository: `gh attestation verify GrainSearch-{v}-x64-setup.exe --repo <owner>/grain-search`.
"""


def section(changelog: str, version: str) -> str | None:
    m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]: |\Z)", changelog, re.S | re.M)
    return m.group(1).strip() if m else None


def main() -> int:
    version = sys.argv[1].removeprefix("v")
    text = (ROOT / "CHANGELOG.md").read_text("utf-8")
    body = section(text, version)
    if body is None:
        print(f"warning: CHANGELOG.md has no section for {version}", file=sys.stderr)
        body = "See [CHANGELOG.md](CHANGELOG.md)."
    print(body + "\n" + FOOTER.format(v=version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
