"""The live settings API behind the app's Settings window: GET /config, POST /config/folders,
POST /config/excludes. Changes are saved to config.json and applied without a restart:
new folders get indexed, removed or excluded files leave the index and the results."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_service import HAVE_WEB, free_port, wait_for  # noqa: E402

from grain_search import winstate  # noqa: E402
from grain_search.config import DEFAULT_EXCLUDES, Config, read_raw, save_raw  # noqa: E402
from grain_search.embedder import HashEmbedder  # noqa: E402

_REAL = (winstate.on_battery, winstate.fullscreen_app_running)


def setUpModule():  # tests must not depend on the laptop's power state
    winstate.on_battery = lambda: False
    winstate.fullscreen_app_running = lambda: False


def tearDownModule():
    winstate.on_battery, winstate.fullscreen_app_running = _REAL


@unittest.skipUnless(HAVE_WEB, "starlette/uvicorn not installed")
class SettingsApiTest(unittest.TestCase):
    def setUp(self):
        import uvicorn

        from grain_search.service import SearchService

        self.tmp = Path(tempfile.mkdtemp())
        self.docs = self.tmp / "docs"
        self.notes = self.tmp / "notes"
        (self.docs / "old").mkdir(parents=True)
        self.notes.mkdir()
        (self.docs / "plan.txt").write_text("the quarterly zebra migration plan", encoding="utf-8")
        (self.docs / "old" / "draft.txt").write_text("an early zebra draft", encoding="utf-8")
        (self.notes / "trip.txt").write_text("zebra spotted on the safari trip", encoding="utf-8")

        self.config_file = self.tmp / "profile" / "config.json"
        save_raw(self.config_file, {"roots": [str(self.docs)], "excludes": ["*/node_modules/*"]})
        self.cfg = Config(roots=[str(self.docs)], data_dir=self.tmp / "profile" / "index")
        self.cfg.excludes = ["*/node_modules/*"]
        self.cfg.config_path = self.config_file
        self.cfg.text_min_score = -1.0

        self.svc = SearchService(self.cfg, HashEmbedder(), background=True, watch=True, token="t")
        self.svc.startup()
        self.assertTrue(wait_for(lambda: self.svc.indexer.last_report is not None, 30))
        self.server = uvicorn.Server(
            uvicorn.Config(self.svc.app, host="127.0.0.1", port=free_port(), log_level="error", lifespan="off")
        )
        self.base = f"http://127.0.0.1:{self.server.config.port}"
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        wait_for(lambda: self.server.started, 10)

    def tearDown(self):
        self.server.should_exit = True
        self.thread.join(5)
        self.svc.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------------------
    def req(self, path, body=None, token="t"):
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method="POST" if body is not None else "GET", headers=h)
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def found(self, q="zebra") -> set[str]:
        _, data = self.req(f"/search?stream=0&q={urllib.parse.quote(q)}&limit=20")
        return {Path(d["path"]).name for d in data["docs"]}

    def wait_until_found(self, expected: set[str], timeout=20) -> set[str]:
        return wait_for(lambda: self.found() if self.found() == expected else None, timeout) or self.found()

    # -- tests -------------------------------------------------------------------------
    def test_needs_the_token(self):
        self.assertEqual(self.req("/config", token=None)[0], 401)
        self.assertEqual(self.req("/config/folders", {"action": "add", "path": str(self.notes)}, token="bad")[0], 401)
        self.assertEqual(read_raw(self.config_file)["roots"], [str(self.docs)])  # nothing changed

    def test_view_lists_folders_with_counts_and_exclusions(self):
        status, data = self.req("/config")
        self.assertEqual(status, 200)
        self.assertFalse(data["setup_needed"])
        self.assertEqual(data["config_file"], str(self.config_file))
        (folder,) = data["folders"]
        self.assertEqual(folder["path"], str(self.docs))
        self.assertTrue(folder["exists"])
        self.assertEqual(folder["indexed"], 2)
        self.assertEqual([e["pattern"] for e in data["excludes"]], ["*/node_modules/*"])

    def test_add_folder_is_saved_and_indexed_live(self):
        self.assertEqual(self.found(), {"plan.txt", "draft.txt"})
        status, data = self.req("/config/folders", {"action": "add", "path": str(self.notes)})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["changed"])
        self.assertEqual(len(data["folders"]), 2)
        self.assertEqual(read_raw(self.config_file)["roots"], [str(self.docs), str(self.notes)])
        self.assertEqual(
            self.wait_until_found({"plan.txt", "draft.txt", "trip.txt"}), {"plan.txt", "draft.txt", "trip.txt"}
        )

    def test_remove_folder_hides_results_at_once_and_drops_them(self):
        self.req("/config/folders", {"action": "add", "path": str(self.notes)})
        self.wait_until_found({"plan.txt", "draft.txt", "trip.txt"})
        status, data = self.req("/config/folders", {"action": "remove", "path": str(self.docs)})
        self.assertEqual(status, 200)
        self.assertEqual(self.found(), {"trip.txt"})  # hidden immediately, before any re-scan
        self.assertTrue(
            wait_for(lambda: self.svc.store.get_file(str(self.docs / "plan.txt")) is None, 20),
            "removed folder's files should leave the index",
        )

    def test_exclude_folder_and_pattern(self):
        status, data = self.req("/config/excludes", {"action": "add", "value": str(self.docs / "old")})
        self.assertEqual(status, 200, data)
        self.assertEqual(self.found(), {"plan.txt"})  # hidden at once
        self.assertTrue(wait_for(lambda: self.svc.store.get_file(str(self.docs / "old" / "draft.txt")) is None, 20))
        status, data = self.req("/config/excludes", {"action": "add", "value": "*.txt"})
        self.assertEqual(self.found(), set())
        patterns = [e["pattern"] for e in data["excludes"]]
        self.assertIn("*.txt", patterns)
        # removing the exclusion brings the files back
        self.req("/config/excludes", {"action": "remove", "value": "*.txt"})
        self.req("/config/excludes", {"action": "remove", "value": str(self.docs / "old")})
        self.assertEqual(self.wait_until_found({"plan.txt", "draft.txt"}), {"plan.txt", "draft.txt"})

    def test_bad_input_is_rejected_and_nothing_saved(self):
        before = self.config_file.read_text(encoding="utf-8")
        status, data = self.req("/config/folders", {"action": "add", "path": str(self.tmp / "missing")})
        self.assertEqual(status, 400)
        self.assertIn("Not a folder", data["error"])
        self.assertEqual(self.req("/config/folders", {"action": "explode", "path": str(self.notes)})[0], 400)
        self.assertEqual(self.req("/config/excludes", {"action": "add", "value": "  "})[0], 400)
        self.assertEqual(self.config_file.read_text(encoding="utf-8"), before)

    def test_adding_twice_is_harmless(self):
        _, data = self.req("/config/folders", {"action": "add", "path": str(self.docs)})
        self.assertFalse(data["changed"])
        self.assertIn("Already", data["message"])

    def test_builtin_exclusions_are_marked(self):
        save_raw(self.config_file, {"roots": [str(self.docs)]})
        self.cfg.excludes = list(DEFAULT_EXCLUDES)
        _, data = self.req("/config/excludes", {"action": "add", "value": "*.log"})
        marks = {e["pattern"]: e["builtin"] for e in data["excludes"]}
        self.assertTrue(marks["*\\node_modules\\*"])
        self.assertFalse(marks["*.log"])


if __name__ == "__main__":
    unittest.main()
