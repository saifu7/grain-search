"""Folder suggestions in Settings: Documents / Pictures / Desktop / Downloads that exist and
aren't added yet, with how many files Grain Search would index in each."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_service import HAVE_WEB, free_port, wait_for  # noqa: E402

from grain_search import winstate  # noqa: E402
from grain_search.config import Config  # noqa: E402
from grain_search.crawler import count_indexable  # noqa: E402
from grain_search.embedder import HashEmbedder  # noqa: E402
from grain_search.known_folders import known_folders  # noqa: E402

_REAL = (winstate.on_battery, winstate.fullscreen_app_running)


def setUpModule():
    winstate.on_battery = lambda: False
    winstate.fullscreen_app_running = lambda: False


def tearDownModule():
    winstate.on_battery, winstate.fullscreen_app_running = _REAL


def write(p: Path, text: str = "some words") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


class KnownFoldersTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_existing_folders_only_with_xdg_overrides(self):
        (self.home / "Docs").mkdir()  # Documents was moved, as user-dirs.dirs says
        (self.home / "Pictures").mkdir()
        (self.home / "Downloads").mkdir()
        write(self.home / ".config" / "user-dirs.dirs", 'XDG_DOCUMENTS_DIR="$HOME/Docs"\n# comment\n')
        found = known_folders(windows=False, home=self.home, environ={})
        self.assertEqual([f["key"] for f in found], ["documents", "pictures", "downloads"])  # no Desktop
        self.assertEqual(found[0]["path"], str(self.home / "Docs"))
        self.assertEqual(found[0]["label"], "Documents")

    def test_nothing_exists(self):
        self.assertEqual(known_folders(windows=False, home=self.home, environ={}), [])

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_windows_asks_known_folders(self):
        found = {f["key"]: f["path"] for f in known_folders()}
        self.assertIn("documents", found)
        self.assertTrue(Path(found["documents"]).is_dir())


class CountTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        write(self.root / "notes.txt", "hello there")
        write(self.root / "sub" / "plan.md", "a plan")
        write(self.root / "photo.png", "not really a png but named like one")
        write(self.root / "movie.mp4", "video is never indexed")
        write(self.root / "node_modules" / "lib.txt", "excluded by a default rule")
        write(self.root / "empty.txt", "")  # empty files are skipped

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def cfg(self, images=True) -> Config:
        cfg = Config(roots=[], data_dir=self.root / "_index")
        cfg.excludes = ["*/node_modules/*", "*\\node_modules\\*"]
        cfg.model.images = images
        return cfg

    def test_counts_what_a_scan_would_index(self):
        files, size = count_indexable(self.cfg(), str(self.root))
        self.assertEqual(files, 3)  # notes.txt, plan.md, photo.png
        expected = sum((self.root / n).stat().st_size for n in ("notes.txt", "sub/plan.md", "photo.png"))
        self.assertEqual(size, expected)

    def test_images_off_means_images_not_counted(self):
        self.assertEqual(count_indexable(self.cfg(images=False), str(self.root))[0], 2)


class FolderSuggestionsTest(unittest.TestCase):
    def setUp(self):
        from grain_search.service import FolderSuggestions

        self.tmp = Path(tempfile.mkdtemp())
        self.docs = self.tmp / "Documents"
        self.pics = self.tmp / "Pictures"
        self.empty = self.tmp / "Desktop"
        write(self.docs / "a.txt")
        write(self.docs / "b.txt")
        write(self.pics / "c.png")
        self.empty.mkdir()
        self.found = [
            {"key": "documents", "label": "Documents", "path": str(self.docs)},
            {"key": "pictures", "label": "Pictures", "path": str(self.pics)},
            {"key": "desktop", "label": "Desktop", "path": str(self.empty)},
        ]
        self.cfg = Config(roots=[], data_dir=self.tmp / "_index")
        self.sug = FolderSuggestions(self.cfg, find=lambda: self.found)

    def tearDown(self):
        self.sug.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def settled(self):
        return wait_for(lambda: (v := self.sug.view()) and not any(s["counting"] for s in v) and v, 10)

    def test_counting_first_then_numbers_and_empty_hidden(self):
        first = self.sug.view()
        self.assertEqual(len(first), 3)
        self.assertTrue(all(s["counting"] and s["files"] is None for s in first))
        done = self.settled()
        self.assertEqual({s["key"]: s["files"] for s in done}, {"documents": 2, "pictures": 1})  # Desktop: empty

    def test_added_folders_are_not_suggested(self):
        self.cfg.roots = [str(self.docs)]  # added itself
        self.assertNotIn("documents", {s["key"] for s in self.sug.view()})
        self.cfg.roots = [str(self.pics / "Holiday")]  # a folder inside Pictures is added
        self.assertNotIn("pictures", {s["key"] for s in self.sug.view()})
        self.cfg.roots = [str(self.tmp)]  # their parent is added: everything is covered
        self.assertEqual(self.sug.view(), [])

    def test_exclusion_change_recounts(self):
        self.settled()
        self.cfg.excludes = ["*.png"]
        self.sug.invalidate()
        self.assertTrue(all(s["counting"] for s in self.sug.view()))
        done = self.settled()
        self.assertEqual({s["key"] for s in done}, {"documents"})  # Pictures now has nothing to index


@unittest.skipUnless(HAVE_WEB, "starlette/uvicorn not installed")
class SuggestionsApiTest(unittest.TestCase):
    def test_route(self):
        import uvicorn

        from grain_search.service import SearchService

        tmp = Path(tempfile.mkdtemp())
        try:
            write(tmp / "Documents" / "a.txt")
            cfg = Config(roots=[], data_dir=tmp / "_index")
            svc = SearchService(cfg, HashEmbedder(), background=False, token="t")
            docs = {"key": "documents", "label": "Documents", "path": str(tmp / "Documents")}
            svc.suggestions.find = lambda: [docs]
            server = uvicorn.Server(
                uvicorn.Config(svc.app, host="127.0.0.1", port=free_port(), log_level="error", lifespan="off")
            )
            t = threading.Thread(target=server.run, daemon=True)
            t.start()
            wait_for(lambda: server.started, 10)
            url = f"http://127.0.0.1:{server.config.port}/config/suggestions"

            def get():
                r = urllib.request.Request(url, headers={"Authorization": "Bearer t"})
                with urllib.request.urlopen(r, timeout=10) as resp:
                    return json.loads(resp.read())["suggestions"]

            self.assertTrue(wait_for(lambda: get() and get()[0]["files"] == 1, 10), get())
            self.assertEqual(get()[0]["path"], str(tmp / "Documents"))
            server.should_exit = True
            t.join(5)
            svc.shutdown()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
