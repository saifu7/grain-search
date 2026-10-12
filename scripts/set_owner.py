"""Replace the OWNER placeholder with your GitHub user or organisation name (run once).

uv run --no-project python scripts/set_owner.py your-github-name
"""

from __future__ import annotations

import re
import sys

from versions import ROOT

FILES = [
    "README.md",
    "CHANGELOG.md",
    ".github/CODEOWNERS",
    ".github/ISSUE_TEMPLATE/config.yml",
    "engine/pyproject.toml",
    "app/src-tauri/Cargo.toml",
]


def main() -> int:
    if len(sys.argv) != 2 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", sys.argv[1]):
        print(__doc__)
        return 2
    owner = sys.argv[1]
    for rel in FILES:
        p = ROOT / rel
        text = p.read_text("utf-8")
        new = text.replace("OWNER/", f"{owner}/").replace("@OWNER", f"@{owner}")
        if new != text:
            p.write_text(new, "utf-8")
            print(f"updated {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
