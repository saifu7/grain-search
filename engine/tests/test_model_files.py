"""Settings -> AI model -> Download: where the model is, the download (progress, errors, cancel,
resume), and the engine waiting for it (no indexing, exact search only) until it's there."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_service import HAVE_WEB, free_port, wait_for  # noqa: E402

from grain_search import model_files, winstate  # noqa: E402
from grain_search.config import Config, ModelCfg, read_raw, save_raw  # noqa: E402
from grain_search.embedder import GemmaEmbedder, HashEmbedder  # noqa: E402
from grain_search.model_files import (  # noqa: E402
    DONE_MARKER,
    ModelDownloader,
    ModelMissing,
    locate,
    model_folder,
    use_location,
)

_REAL = (winstate.on_battery, winstate.fullscreen_app_running)


def setUpModule():
    winstate.on_battery = lambda: False
    winstate.fullscreen_app_running = lambda: False


def tearDownModule():
    winstate.on_battery, winstate.fullscreen_app_running = _REAL


# A stand-in for `python -m grain_search.model_files <repo> <folder>`: writes the files slowly,
# or fails, or hangs - depending on the repo id it is given.
FAKE_DOWNLOAD = textwrap.dedent(
    """
    import json, sys, time
    from pathlib import Path
    repo, folder = sys.argv[1], Path(sys.argv[2])
    folder.mkdir(parents=True, exist_ok=True)
    if repo.endswith("-fails"):
        print("Traceback...", file=sys.stderr)
        print("DownloadError: Not enough space in X: needs 1.6 GB, 0.4 GB free.", file=sys.stderr)
        sys.exit(1)
    (folder / ".grain-download.json").write_text(json.dumps({"total": 400000}))
    for i in range(4):
        (folder / f"part{i}.bin").write_bytes(b"x" * 100000)
        time.sleep(0.4 if not repo.endswith("-slow") else 30)
    (folder / "config.json").write_text("{}")
    (folder / ".grain-complete.json").write_text("{}")
    """
)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.script = self.tmp / "fake_download.py"
        self.script.write_text(FAKE_DOWNLOAD, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def command(self, repo, folder):
        return [sys.executable, str(self.script), repo, folder]


class LocateTest(Tmp):
    def test_missing_folder_or_cache(self):
        m = ModelCfg(dir=str(self.tmp / "models"))
        self.assertEqual(locate(m, cache_lookup=lambda r: None).state, "missing")
        self.assertEqual(locate(m, cache_lookup=lambda r: "/hf/cache/snap").path, "/hf/cache/snap")
        folder = model_folder(m)
        self.assertEqual(folder.name, "embeddinggemma-2")
        folder.mkdir(parents=True)
        (folder / "config.json").write_text("{}")
        self.assertEqual(locate(m, cache_lookup=lambda r: None).state, "missing", "no marker: unfinished")
        (folder / DONE_MARKER).write_text("{}")
        loc = locate(m, cache_lookup=lambda r: "/hf/cache/snap")
        self.assertEqual((loc.state, loc.path), ("folder", str(folder)), "the downloaded copy wins")

    def test_use_location_never_downloads(self):
        m = ModelCfg()
        use_location(m, model_files.Location("missing", None, "x"))
        self.assertTrue(m.local_only)
        self.assertIsNone(m.source)
        emb = GemmaEmbedder(m)
        self.assertFalse(emb.available)
        with self.assertRaises(ModelMissing):
            emb.encode_query("hello")
        self.assertIsNone(emb._load_error, "not remembered: works once the files arrive")

    def test_model_folder_is_never_indexed(self):
        cfg = Config(roots=[], data_dir=self.tmp / "index")
        cfg.model.dir = str(self.tmp / "my-models")
        joined = " ".join(cfg.own_excludes)
        self.assertIn("my-models", joined)
        self.assertIn("models", joined)


class DownloaderTest(Tmp):
    def make(self, repo="google/embeddinggemma-2"):
        cfg = Config(roots=[], data_dir=self.tmp / "index")
        cfg.model = ModelCfg(id=repo, dir=str(self.tmp / "models"))
        done = threading.Event()
        d = ModelDownloader(cfg, on_done=done.set, command=self.command, cache_lookup=lambda r: None)
        return d, done

    def test_progress_then_ready(self):
        d, done = self.make()
        self.assertEqual(d.status()["state"], "missing")
        d.start()
        self.assertTrue(wait_for(lambda: d.status().get("total_bytes") == 400000, 10))
        st = d.status()
        self.assertEqual(st["state"], "downloading")
        self.assertLessEqual(st["done_bytes"], 400000 + 200)
        d.start()  # a second click does nothing
        self.assertTrue(done.wait(15), "on_done runs when every file is there")
        self.assertEqual(d.status()["state"], "ready")
        self.assertEqual(d.status()["source"], "folder")

    def test_error_is_reported(self):
        d, done = self.make("google/x-fails")
        d.start()
        self.assertTrue(wait_for(lambda: not d.running, 10))
        time.sleep(0.2)
        st = d.status()
        self.assertEqual(st["state"], "missing")
        self.assertEqual(st["error"], "Not enough space in X: needs 1.6 GB, 0.4 GB free.")
        self.assertFalse(done.is_set())

    def test_cancel(self):
        d, done = self.make("google/x-slow")
        d.start()
        self.assertTrue(wait_for(lambda: d.status().get("done_bytes", 0) > 0, 10))
        d.cancel()
        self.assertTrue(wait_for(lambda: not d.running, 5))
        time.sleep(0.2)
        st = d.status()
        self.assertEqual((st["state"], st["error"]), ("missing", ""), "cancelled is not an error")
        self.assertGreater(st["done_bytes"], 0, "what arrived stays, for resuming")


class DownloadFunctionTest(Tmp):
    """model_files.download() with a stand-in huggingface_hub (no network in tests)."""

    def fake_hub(self, total, write=True):
        hub = types.ModuleType("huggingface_hub")

        class Api:
            def model_info(self, repo, files_metadata=False):
                assert files_metadata
                return types.SimpleNamespace(sha="abc123", siblings=[types.SimpleNamespace(size=total)])

        def snapshot_download(repo, revision, local_dir):
            assert revision == "abc123"
            if write:
                Path(local_dir, "config.json").write_text("{}")

        hub.HfApi = Api
        hub.snapshot_download = snapshot_download
        return hub

    def run_with(self, hub):
        old = sys.modules.get("huggingface_hub")
        sys.modules["huggingface_hub"] = hub
        try:
            model_files.download("google/embeddinggemma-2", str(self.tmp / "m"))
        finally:
            if old is None:
                sys.modules.pop("huggingface_hub", None)
            else:
                sys.modules["huggingface_hub"] = old

    def test_writes_marker_and_info(self):
        self.run_with(self.fake_hub(1000))
        folder = self.tmp / "m"
        self.assertTrue(model_files.is_complete(folder))
        self.assertEqual(json.loads((folder / ".grain-download.json").read_text())["total"], 1000)

    def test_not_enough_space(self):
        with self.assertRaises(model_files.DownloadError) as ctx:
            self.run_with(self.fake_hub(10**18))  # an exabyte
        self.assertIn("Not enough space", str(ctx.exception))
        self.assertFalse(model_files.is_complete(self.tmp / "m"))


@unittest.skipUnless(HAVE_WEB, "starlette/uvicorn not installed")
class ServiceWithoutModelTest(Tmp):
    def setUp(self):
        super().setUp()
        import uvicorn

        from grain_search.service import SearchService

        self.docs = self.tmp / "docs"
        self.docs.mkdir()
        (self.docs / "plan.txt").write_text("the quarterly zebra migration plan", encoding="utf-8")
        self.config_file = self.tmp / "profile" / "config.json"
        save_raw(self.config_file, {"roots": [str(self.docs)]})
        self.cfg = Config(roots=[str(self.docs)], data_dir=self.tmp / "index")
        self.cfg.config_path = self.config_file
        self.cfg.text_min_score = -1.0
        self.cfg.model.dir = str(self.tmp / "models")
        self.model = HashEmbedder()
        self.model.available = False  # as on a fresh install
        self.svc = SearchService(self.cfg, self.model, background=True, watch=False, token="t")
        self.svc.model_files.command = self.command
        self.svc.model_files.cache_lookup = lambda r: None  # ignore a real cached copy on this PC

        def downloaded():  # the fake model "arrives" when the fake download finishes
            self.model.available = True
            self.svc._model_downloaded()

        self.svc.model_files.on_done = downloaded
        self.svc.startup()
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
        super().tearDown()

    def req(self, path, body=None):
        h = {"Content-Type": "application/json", "Authorization": "Bearer t"}
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method="POST" if body is not None else "GET", headers=h)
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def indexed(self) -> bool:
        with self.svc.db_lock:
            row = self.svc.store.get_file(str(self.docs / "plan.txt"))
        return row is not None and row.status == "indexed"

    def test_waits_for_the_download_then_indexes(self):
        st = self.req("/status")[1]
        self.assertEqual(st["model_state"], "missing")
        self.assertTrue(wait_for(lambda: "not downloaded" in self.req("/status")[1]["indexer"]["paused_reason"], 10))
        self.assertEqual(self.req("/model")[1]["state"], "missing")
        self.assertFalse(self.indexed())
        self.req("/model/load", {})  # Alt+Space: nothing to load yet, no error
        self.assertFalse(self.model.loaded)

        code, data = self.req("/model/download", {})
        self.assertEqual(code, 200)
        self.assertEqual(data["state"], "downloading")
        self.assertTrue(wait_for(lambda: self.req("/model")[1]["state"] == "ready", 15))
        self.assertTrue(wait_for(self.indexed, 20), "indexing starts once the model is there")
        self.assertEqual(self.req("/status")[1]["model_state"], "loaded")

    def test_search_without_model_is_exact_only(self):
        code, data = self.req("/search?stream=0&q=migration%20plan&limit=5")
        self.assertEqual(code, 200)  # works, but finds nothing: nothing is indexed yet
        self.assertEqual(data["docs"], [])
        self.assertFalse(self.model.loaded)

    def test_choosing_a_folder_saves_it(self):
        other = self.tmp / "D-drive" / "Models"
        code, data = self.req("/model/download", {"folder": str(other)})
        self.assertEqual(code, 200)
        self.assertEqual(read_raw(self.config_file)["model"]["dir"], str(other))
        self.assertEqual(read_raw(self.config_file)["roots"], [str(self.docs)], "other settings kept")
        self.assertTrue(data["folder"].startswith(str(other)))
        self.assertEqual(self.req("/model/download", {"folder": "relative/path"})[0], 200, "ignored while running")
        self.svc.model_files.cancel()
        self.assertTrue(wait_for(lambda: not self.svc.model_files.running, 5))
        self.assertEqual(self.req("/model/download", {"folder": "relative/path"})[0], 400)


if __name__ == "__main__":
    unittest.main()
