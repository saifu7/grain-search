"""Tray -> Unload model: the model leaves memory, background indexing waits (changed files stay
queued), and Alt+Space (POST /model/load) or a search brings it back."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_service import HAVE_WEB, free_port, wait_for  # noqa: E402

from grain_search import winstate  # noqa: E402
from grain_search.config import Config  # noqa: E402
from grain_search.embedder import HashEmbedder  # noqa: E402

_REAL = (winstate.on_battery, winstate.fullscreen_app_running)


def setUpModule():  # tests must not depend on the laptop's power state
    winstate.on_battery = lambda: False
    winstate.fullscreen_app_running = lambda: False


def tearDownModule():
    winstate.on_battery, winstate.fullscreen_app_running = _REAL


@unittest.skipUnless(HAVE_WEB, "starlette/uvicorn not installed")
class ModelUnloadTest(unittest.TestCase):
    def setUp(self):
        import uvicorn

        from grain_search.service import SearchService

        self.tmp = Path(tempfile.mkdtemp())
        self.docs = self.tmp / "docs"
        self.docs.mkdir()
        (self.docs / "plan.txt").write_text("the quarterly zebra migration plan", encoding="utf-8")
        self.cfg = Config(roots=[str(self.docs)], data_dir=self.tmp / "index")
        self.cfg.text_min_score = -1.0
        self.model = HashEmbedder()
        self.svc = SearchService(self.cfg, self.model, background=True, watch=False, token="t")
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

    def req(self, path, body=None):
        h = {"Content-Type": "application/json", "Authorization": "Bearer t"}
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method="POST" if body is not None else "GET", headers=h)
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def status(self) -> dict:
        return self.req("/status")[1]

    def indexed(self, name: str) -> bool:
        with self.svc.db_lock:
            row = self.svc.store.get_file(str(self.docs / name))
        return row is not None and row.status == "indexed"

    def unload(self):
        code, data = self.req("/model/unload", {})
        self.assertEqual(code, 200)
        self.assertEqual(data["model_state"], "unloaded")
        self.assertFalse(self.model.loaded)

    # -- tests -------------------------------------------------------------------------
    def test_status_reports_the_model(self):
        self.assertEqual(self.status()["model_state"], "loaded")
        self.unload()
        self.assertEqual(self.status()["model_state"], "unloaded")

    def test_status_reports_where_the_model_runs(self):
        info = self.status()["device_info"]
        self.assertEqual(info["kind"], "cpu")
        self.assertIn("name", info)
        self.unload()
        self.assertEqual(self.status()["device_info"], {}, "nothing to report while unloaded")

    def test_indexing_waits_while_unloaded_and_resumes_on_load(self):
        self.unload()
        loads = self.model.loads
        (self.docs / "new.txt").write_text("a brand new zebra note", encoding="utf-8")
        self.svc.indexer.rescan()
        # the indexer notices it has to wait and says why, without loading the model
        self.assertTrue(
            wait_for(lambda: "model unloaded" in (self.status()["indexer"]["paused_reason"] or ""), 10),
            self.status()["indexer"],
        )
        time.sleep(1.5)
        self.assertFalse(self.indexed("new.txt"), "nothing may be indexed while the model is unloaded")
        self.assertFalse(self.model.loaded)
        self.assertEqual(self.model.loads, loads)

        code, data = self.req("/model/load", {})  # what Alt+Space sends
        self.assertEqual(code, 200)
        self.assertTrue(wait_for(lambda: self.status()["model_state"] == "loaded", 10))
        self.assertTrue(wait_for(lambda: self.indexed("new.txt"), 20), "queued work continues after loading")
        self.assertEqual(self.status()["indexer"]["paused_reason"], "")

    def test_load_returns_at_once_and_twice_is_harmless(self):
        self.unload()
        t0 = time.perf_counter()
        self.req("/model/load", {})
        self.req("/model/load", {})
        self.assertLess(time.perf_counter() - t0, 5)
        self.assertTrue(wait_for(lambda: self.model.loaded, 10))
        self.req("/model/load", {})  # already loaded: nothing happens
        self.assertEqual(self.status()["model_state"], "loaded")

    def test_search_while_unloaded_loads_the_model_and_answers(self):
        self.unload()
        code, data = self.req(f"/search?stream=0&q={urllib.parse.quote('migration of zebras')}&limit=5")
        self.assertEqual(code, 200)
        self.assertIn("plan.txt", {Path(d["path"]).name for d in data["docs"]})
        self.assertTrue(self.model.loaded)
        self.assertEqual(self.status()["model_state"], "loaded")

    def test_exact_text_search_does_not_load_the_model(self):
        self.unload()
        code, data = self.req(f"/search?stream=0&q={urllib.parse.quote('=zebra')}&limit=5")
        self.assertEqual(code, 200)
        self.assertIn("plan.txt", {Path(d["path"]).name for d in data["docs"]})
        self.assertFalse(self.model.loaded, "exact search needs no model")

    def test_unload_twice_and_shutdown_while_waiting(self):
        self.unload()
        self.unload()
        self.svc.indexer.rescan()
        wait_for(lambda: "model unloaded" in (self.status()["indexer"]["paused_reason"] or ""), 10)
        t0 = time.perf_counter()
        self.server.should_exit = True
        self.thread.join(5)
        self.svc.shutdown()
        self.assertLess(time.perf_counter() - t0, 8, "a waiting indexer must not hold up shutdown")
        self.assertFalse(self.svc.indexer.thread.is_alive())


class CpuReasonTest(unittest.TestCase):
    def test_reasons(self):
        from grain_search.embedder import cpu_reason

        self.assertIn("Set to CPU", cpu_reason("cpu", True, True))
        self.assertIn("NVIDIA GPU, but the CPU-only build", cpu_reason("auto", False, True))
        self.assertEqual(cpu_reason("auto", False, False), "The CPU-only build of PyTorch is installed.")
        self.assertIn("No NVIDIA GPU found", cpu_reason("auto", True, False))


if __name__ == "__main__":
    unittest.main()
