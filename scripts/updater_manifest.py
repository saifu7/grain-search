"""Write latest.json for tauri-plugin-updater.

uv run --no-project python scripts/updater_manifest.py VERSION SIG_FILE DOWNLOAD_URL > latest.json
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    version, sig_file, url = sys.argv[1:4]
    manifest = {
        "version": version,
        "notes": f"Grain Search {version}",
        "pub_date": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platforms": {
            "windows-x86_64": {"signature": Path(sig_file).read_text("utf-8").strip(), "url": url},
        },
    }
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
