"""The background service, run for real on a local port with the fake embedder:
auth, two-stage SSE search, previews, thumbnails, path safety, open-ranking,
live re-indexing from the file watcher, pause/resume, and index_paths itself."""

from __future__ import annotations

import json
import os
import shutil
import socket
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

from test_pipeline import SENTENCE, make_files  # noqa: E402

from grain_search import winstate  # noqa: E402
from grain_search.config import Config  # noqa: E402
from grain_search.embedder import HashEmbedder  # noqa: E402
from grain_search.indexer import index_paths, run_index  # noqa: E402
from grain_search.store import Store  # noqa: E402

# The indexer pauses on battery power and while a full-screen app runs (Windows only).
# Tests must not depend on whether the laptop is plugged in, so both report "no" here;
# AutoPauseTest below switches them on deliberately.
_REAL_WINSTATE = (winstate.on_battery, winstate.fullscreen_app_running)


def setUpModule():
    winstate.on_battery = lambda: False
    winstate.fullscreen_app_running = lambda: False


def tearDownModule():
    winstate.on_battery, winstate.fullscreen_app_running = _REAL_WINSTATE


try:
    import starlette  # noqa: F401
    import uvicorn  # noqa: F401

    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(cond, timeout=15.0, every=0.2):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(every)
    return cond()


@unittest.skipUnless(HAVE_WEB, "starlette/uvicorn not installed")
class ServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import uvicorn

        from grain_search.service import SearchService

        cls.tmp = Path(tempfile.mkdtemp())
        cls.root = cls.tmp / "files"
        make_files(cls.root)
        cls.cfg = Config(roots=[str(cls.root)], data_dir=cls.tmp / "data")
        cls.cfg.excludes = ["*/node_modules/*"]
        cls.cfg.image_min_score = 0.0
        cls.cfg.text_min_score = -1.0
        cls.svc = SearchService(cls.cfg, HashEmbedder(), background=True, watch=True, token="secret-token")
        cls.svc.startup()
        # wait for the initial background pass to finish
        wait_for(lambda: cls.svc.indexer.last_report is not None, 30)
        cls.port = free_port()
        cls.server = uvicorn.Server(
            uvicorn.Config(cls.svc.app, host="127.0.0.1", port=cls.port, log_level="error", lifespan="off")
        )
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        wait_for(lambda: cls.server.started, 10)
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(5)
        cls.svc.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- http helpers --------------------------------------------------------------
    def req(self, path, method="GET", body=None, token="secret-token", headers=None, raw=False):
        h = {"Content-Type": "application/json", **(headers or {})}
        if token:
            h["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                content = resp.read()
                return resp.status, (content if raw else json.loads(content or b"null")), resp.headers
        except urllib.error.HTTPError as e:
            content = e.read()
            try:
                return e.code, json.loads(content or b"null"), e.headers
            except ValueError:
                return e.code, content, e.headers

    def sse(self, q, limit=10):
        url = f"/search?q={urllib.parse.quote(q)}&limit={limit}"
        status, raw, _ = self.req(url, raw=True)
        self.assertEqual(status, 200)
        events = {}
        for block in raw.decode().split("\n\n"):
            lines = dict(ln.split(": ", 1) for ln in block.splitlines() if ": " in ln)
            if "event" in lines:
                events[lines["event"]] = json.loads(lines.get("data", "{}"))
        return events

    def q(self, path):
        return urllib.parse.quote(path)

    # -- tests ---------------------------------------------------------------------
    def test_health_is_open_everything_else_needs_token(self):
        self.assertEqual(self.req("/health", token=None)[0], 200)
        self.assertEqual(self.req("/status", token=None)[0], 401)
        self.assertEqual(self.req("/status", token="wrong")[0], 401)
        status, body, _ = self.req("/status")
        self.assertEqual(status, 200)
        self.assertGreater(body["index"]["files"], 5)
        self.assertEqual(body["model"], "fake/hash-embedder")

    def test_token_in_query_string_works_for_img_tags(self):
        img = str(self.root / "photos" / "red_car.png")
        status, _, _ = self.req(f"/thumb?path={self.q(img)}&size=64&token=secret-token", token=None, raw=True)
        self.assertEqual(status, 200)

    def test_cors_preflight_for_tauri(self):
        status, _, headers = self.req(
            "/search?q=x",
            method="OPTIONS",
            token=None,
            raw=True,
            headers={
                "Origin": "http://tauri.localhost",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("access-control-allow-origin"), "http://tauri.localhost")
        status, _, headers = self.req(
            "/search?q=x",
            method="OPTIONS",
            token=None,
            raw=True,
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
        )
        self.assertNotEqual(headers.get("access-control-allow-origin"), "https://evil.example")

    def test_two_stage_search(self):
        ev = self.sse(SENTENCE)
        self.assertEqual(list(ev), ["fast", "full", "done"])
        fast, full = ev["fast"], ev["full"]
        self.assertEqual(fast["docs"][0]["name"], "letter.txt")
        self.assertEqual(fast["docs"][0]["match"], "exact")
        self.assertTrue(all(d["contains_text"] for d in fast["docs"]))
        self.assertEqual(fast["images"], [])
        self.assertEqual(full["docs"][0]["name"], "letter.txt")
        self.assertEqual(full["top"], fast["docs"][0]["path"])

    def test_images_come_in_full_stage(self):
        ev = self.sse("red car")
        self.assertEqual(ev["full"]["images"][0]["name"], "red_car.png")
        self.assertEqual(ev["fast"]["images"], [])

    def test_meaning_only_query_skips_fast_stage(self):
        ev = self.sse("~bank holiday")
        self.assertTrue(ev["fast"].get("skipped"))

    def test_plain_json_search(self):
        status, body, _ = self.req(f"/search?q={self.q('volcano safety drill')}&stream=0")
        self.assertEqual(status, 200)
        self.assertEqual(body["docs"][0]["name"], "deck.pptx")
        self.assertEqual(body["docs"][0]["page"], 3)

    def test_preview_highlights_the_match(self):
        path = str(self.root / "notes" / "letter.txt")
        status, body, _ = self.req(f"/preview?path={self.q(path)}&q={self.q('bank holiday')}")
        self.assertEqual(status, 200)
        hit = [ln for ln in body["excerpt"]["lines"] if ln["match"]][0]
        self.assertIn("bank holiday", hit["text"])
        a, b = hit["highlights"][0]
        self.assertEqual(hit["text"][a:b].lower(), "bank holiday")
        self.assertEqual(body["name"], "letter.txt")

    def test_pdf_preview_uses_page(self):
        path = str(self.root / "notes" / "manual.pdf")
        status, body, _ = self.req(f"/preview?path={self.q(path)}&q=toaster&page=2")
        self.assertEqual(status, 200)
        self.assertEqual(body["excerpt"]["page"], 2)
        self.assertTrue(any("toaster" in ln["text"] for ln in body["excerpt"]["lines"]))

    def test_only_indexed_files_are_reachable(self):
        for p in ("/etc/passwd", str(self.root / "notes" / ".hidden.txt"), "C:\\Windows\\win.ini"):
            self.assertEqual(self.req(f"/preview?path={self.q(p)}")[0], 404)
            self.assertEqual(self.req(f"/thumb?path={self.q(p)}", raw=True)[0], 404)
            self.assertEqual(self.req("/open", "POST", {"path": p})[0], 404)

    def test_thumbnails(self):
        img = str(self.root / "photos" / "dog_on_sofa.jpg")
        status, data, headers = self.req(f"/thumb?path={self.q(img)}&size=32", raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "image/jpeg")
        self.assertTrue(data.startswith(b"\xff\xd8"))
        txt = str(self.root / "notes" / "recipe.md")
        self.assertEqual(self.req(f"/thumb?path={self.q(txt)}", raw=True)[0], 404)

    def test_open_records_feedback_and_reranks(self):
        from grain_search import opener

        calls = []
        orig = opener.open_file
        opener.open_file = lambda path, page=None, line=None, at_location=True: calls.append(path) or "test"
        try:
            # "notes" matches every file in the notes folder by name/path -> one "name" group
            before = [d["name"] for d in self.sse("=notes")["full"]["docs"]]
            target = before[-1]
            path = str(self.root / "notes" / target)
            for _ in range(3):
                status, body, _ = self.req("/open", "POST", {"path": path, "query": "notes"})
                self.assertEqual(status, 200, body)
            self.assertEqual(calls, [path] * 3)
            after = [d["name"] for d in self.sse("=notes")["full"]["docs"]]
            self.assertEqual(after[0], target)
        finally:
            opener.open_file = orig

    def test_shutdown_endpoint_needs_token_and_calls_exit(self):
        called = []
        self.svc.request_exit = lambda: called.append(True)
        self.assertEqual(self.req("/shutdown", "POST", token=None)[0], 401)
        self.assertEqual(self.req("/shutdown", "POST")[0], 200)
        self.assertEqual(called, [True])

    def test_pause_resume(self):
        self.assertEqual(self.req("/index/pause", "POST")[0], 200)
        self.assertTrue(self.req("/status")[1]["indexer"]["manual_pause"])
        self.assertEqual(self.req("/index/resume", "POST")[0], 200)
        self.assertFalse(self.req("/status")[1]["indexer"]["manual_pause"])

    def test_live_watcher_add_change_move_delete(self):
        new = self.root / "notes" / "fresh_note.txt"
        new.write_text("the quokka festival starts on tuesday", encoding="utf-8")
        found = wait_for(
            lambda: any(d["name"] == "fresh_note.txt" for d in self.sse("=quokka festival")["full"]["docs"]), 20
        )
        self.assertTrue(found, "new file was not picked up by the watcher")

        new.write_text("the wombat parade starts on friday", encoding="utf-8")
        found = wait_for(
            lambda: any(d["name"] == "fresh_note.txt" for d in self.sse("=wombat parade")["full"]["docs"]), 20
        )
        self.assertTrue(found, "changed file was not re-indexed")

        moved = self.root / "notes" / "renamed_note.txt"
        new.rename(moved)
        found = wait_for(
            lambda: [d["name"] for d in self.sse("=wombat parade")["full"]["docs"]] == ["renamed_note.txt"], 20
        )
        self.assertTrue(found, "rename was not followed")

        moved.unlink()
        gone = wait_for(lambda: self.sse("=wombat parade")["full"]["docs"] == [], 20)
        self.assertTrue(gone, "deleted file is still in the index")


class FlakyGpuEmbedder(HashEmbedder):
    """Fails with CUDA out-of-memory for the first `fail` document batches."""

    def __init__(self, fail: int):
        super().__init__()
        self.fail = fail

    def encode_documents(self, docs):
        if self.fail > 0:
            self.fail -= 1
            raise RuntimeError("CUDA error: out of memory")
        return super().encode_documents(docs)


class GpuFullTest(unittest.TestCase):
    def test_full_pass_waits_and_retries_instead_of_giving_up(self):
        from grain_search import service
        from grain_search.service import BackgroundIndexer, LockedEmbedder

        tmp = Path(tempfile.mkdtemp())
        try:
            make_files(tmp / "files")
            cfg = Config(roots=[str(tmp / "files")], data_dir=tmp / "data")
            cfg.excludes = [
                "*/node_modules/*"
            ]  # temp dirs live under AppData on Windows, which the default excludes skip
            old = service.GPU_FULL_RETRY_S
            service.GPU_FULL_RETRY_S = 1.0
            ix = BackgroundIndexer(cfg, LockedEmbedder(FlakyGpuEmbedder(fail=1)), watch=False)
            seen_pause = []
            ix.start()
            try:
                finished = wait_for(
                    lambda: (seen_pause.append(ix.state.paused_reason) or True) and ix.last_report is not None, 20, 0.05
                )
            finally:
                ix.stop()
                service.GPU_FULL_RETRY_S = old
            self.assertTrue(finished, ix.state.last_error)
            self.assertTrue(any("GPU memory full" in r for r in seen_pause), seen_pause[-5:])
            self.assertGreater(ix.last_report["chunks"], 5)
            self.assertEqual(ix.state.last_error, "")
            store = Store(cfg.db_path)
            self.assertEqual(store.stats()["by_status"].get("pending", 0), 0)
            store.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class NoLockWhilePausedTest(unittest.TestCase):
    def test_other_writers_are_not_blocked_at_a_checkpoint(self):
        import sqlite3

        tmp = Path(tempfile.mkdtemp())
        try:
            make_files(tmp / "files")
            cfg = Config(roots=[str(tmp / "files")], data_dir=tmp / "data")
            cfg.excludes = [
                "*/node_modules/*"
            ]  # temp dirs live under AppData on Windows, which the default excludes skip
            store = Store(cfg.db_path)
            results = []

            def checkpoint():  # simulates a pause: someone else writes meanwhile
                other = sqlite3.connect(str(cfg.db_path), timeout=0.5)
                try:
                    other.execute(
                        "INSERT INTO meta(key,value) VALUES('probe', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(len(results)),),
                    )
                    other.commit()
                    results.append("ok")
                except sqlite3.OperationalError as e:
                    results.append(str(e))
                finally:
                    other.close()

            run_index(cfg, store, HashEmbedder(), checkpoint=checkpoint)
            store.close()
            self.assertGreater(len(results), 5)
            self.assertEqual(set(results), {"ok"}, results[:3])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cli_index_refuses_while_service_runs(self):
        import io
        from contextlib import redirect_stdout

        from grain_search import service
        from grain_search.cli import main

        tmp = Path(tempfile.mkdtemp())
        orig = service.running_service
        try:
            (tmp / "files").mkdir()
            cfgfile = tmp / "c.yaml"
            cfgfile.write_text(
                f"roots: ['{(tmp / 'files').as_posix()}']\ndata_dir: '{(tmp / 'data').as_posix()}'\n", encoding="utf-8"
            )
            service.running_service = lambda cfg: {"url": "http://127.0.0.1:1", "token": "x"}
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--config", str(cfgfile), "--fake", "index"])
            self.assertEqual(code, 1)
            self.assertIn("already keeps the index up to date", out.getvalue())
        finally:
            service.running_service = orig
            shutil.rmtree(tmp, ignore_errors=True)


class IndexPathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "files"
        make_files(self.root)
        self.cfg = Config(roots=[str(self.root)], data_dir=self.tmp / "data")
        self.cfg.excludes = ["*/node_modules/*"]
        self.store = Store(self.cfg.db_path)
        self.emb = HashEmbedder()
        run_index(self.cfg, self.store, self.emb)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_new_file_and_ignored_ones(self):
        (self.root / "notes" / "a.txt").write_text("alpha", encoding="utf-8")
        (self.root / "notes" / ".secret.txt").write_text("hidden", encoding="utf-8")
        (self.root / "node_modules" / "pkg" / "b.js").write_text("beta", encoding="utf-8")
        (self.root / "notes" / "c.exe").write_bytes(b"MZ")
        rep = index_paths(
            self.cfg,
            self.store,
            self.emb,
            [
                str(self.root / "notes" / "a.txt"),
                str(self.root / "notes" / ".secret.txt"),
                str(self.root / "node_modules" / "pkg" / "b.js"),
                str(self.root / "notes" / "c.exe"),
                "/somewhere/else/outside.txt",
            ],
        )
        self.assertEqual(rep.indexed, 1)
        self.assertIsNotNone(self.store.get_file(str(self.root / "notes" / "a.txt")))
        self.assertIsNone(self.store.get_file(str(self.root / "notes" / ".secret.txt")))
        self.assertIsNone(self.store.get_file(str(self.root / "node_modules" / "pkg" / "b.js")))

    def test_unchanged_file_is_skipped(self):
        rep = index_paths(self.cfg, self.store, self.emb, [str(self.root / "notes" / "letter.txt")])
        self.assertEqual((rep.indexed, rep.unchanged), (0, 1))

    def test_deleted_folder_removes_everything_under_it(self):
        shutil.rmtree(self.root / "photos")
        rep = index_paths(self.cfg, self.store, self.emb, [str(self.root / "photos")])
        self.assertGreaterEqual(rep.removed, 3)
        self.assertEqual(self.store.files_under(str(self.root / "photos") + os.sep), {})

    def test_folder_rename_moves_without_reembedding(self):
        src, dst = self.root / "notes", self.root / "notes2"
        n_before = len(self.store.files_under(str(src) + os.sep))
        src.rename(dst)
        rep = index_paths(self.cfg, self.store, self.emb, [], moves=[(str(src), str(dst))])
        self.assertEqual(rep.indexed, 0)
        self.assertEqual(rep.moved, n_before)
        self.assertEqual(self.store.files_under(str(src) + os.sep), {})
        self.assertIsNotNone(self.store.get_file(str(dst / "letter.txt")))

    def test_vector_index_follows_changes_incrementally(self):
        qv = self.emb.encode_query("quokka festival")
        before = self.store.vector_count("text")
        (self.root / "notes" / "q.txt").write_text("quokka festival", encoding="utf-8")
        index_paths(self.cfg, self.store, self.emb, [str(self.root / "notes" / "q.txt")])
        self.assertEqual(self.store.vector_count("text"), before + 1)
        scores, fids, _ = self.store.knn("text", qv, 1)
        self.assertEqual(self.store.get_files(fids)[fids[0]].name, "q.txt")
        (self.root / "notes" / "q.txt").unlink()
        index_paths(self.cfg, self.store, self.emb, [str(self.root / "notes" / "q.txt")])
        self.assertEqual(self.store.vector_count("text"), before)


class AutoPauseTest(unittest.TestCase):
    """Indexing pauses on battery / full-screen, and resumes when that ends."""

    def setUp(self):
        from grain_search.service import BackgroundIndexer

        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(roots=[str(self.tmp)], data_dir=self.tmp / "data")
        self.ix = BackgroundIndexer(self.cfg, HashEmbedder(), watch=False)

    def tearDown(self):
        winstate.on_battery = lambda: False
        winstate.fullscreen_app_running = lambda: False
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reason(self):
        self.ix._auto_checked = float("-inf")  # skip the 'check at most every N seconds' throttle
        return self.ix._pause_reason()

    def test_battery_and_fullscreen_pause(self):
        self.assertEqual(self._reason(), "")
        winstate.on_battery = lambda: True
        self.assertEqual(self._reason(), "on battery")
        winstate.fullscreen_app_running = lambda: True
        self.assertEqual(self._reason(), "full-screen app running")
        winstate.on_battery = lambda: False
        winstate.fullscreen_app_running = lambda: False
        self.assertEqual(self._reason(), "")

    def test_settings_turn_auto_pause_off(self):
        winstate.on_battery = lambda: True
        winstate.fullscreen_app_running = lambda: True
        self.cfg.pause_on_battery = False
        self.cfg.pause_on_fullscreen = False
        self.assertEqual(self._reason(), "")


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_WEB, "starlette/uvicorn not installed")
class ServeTest(unittest.TestCase):
    """`grain serve` end to end: any free port, service.json, refusing a second copy, clean exit."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "files").mkdir()
        self.cfg = Config(roots=[str(self.tmp / "files")], data_dir=self.tmp / "data")
        self.cfg.service_file = self.tmp / "service.json"
        self.logs: list[str] = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _serve(self, port=0):
        from grain_search.service import serve

        result = {}
        t = threading.Thread(
            target=lambda: result.setdefault(
                "code", serve(self.cfg, HashEmbedder(), port=port, watch=False, log=self.logs.append)
            ),
            daemon=True,
        )
        t.start()
        return t, result

    def _info(self):
        try:
            return json.loads(self.cfg.service_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _call(self, info, method, path):
        req = urllib.request.Request(
            info["url"] + path, method=method, headers={"Authorization": f"Bearer {info['token']}"}
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status

    def test_free_port_service_file_and_clean_exit(self):
        t, result = self._serve()
        self.assertTrue(wait_for(lambda: self._info() is not None), self.logs)
        info = self._info()
        port = int(info["url"].rsplit(":", 1)[1])
        self.assertNotEqual(port, 0)
        self.assertNotEqual(port, 8766, "should not fall back to the old fixed port")
        self.assertEqual(info["pid"], os.getpid())
        self.assertTrue(wait_for(lambda: self._call(info, "GET", "/health") == 200))

        # a second copy refuses to start instead of fighting over the index
        from grain_search.service import serve

        code = serve(self.cfg, HashEmbedder(), port=0, watch=False, log=self.logs.append)
        self.assertEqual(code, 1)
        self.assertIn("already running", self.logs[-1])
        self.assertEqual(self._info(), info, "the running service's service.json must be left alone")

        self.assertEqual(self._call(info, "POST", "/shutdown"), 200)
        t.join(20)
        self.assertFalse(t.is_alive())
        self.assertEqual(result["code"], 0)
        self.assertFalse(self.cfg.service_path.exists(), "service.json is removed on exit")

    def test_fixed_port_that_is_taken(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            t, result = self._serve(port)
            t.join(20)
        self.assertEqual(result.get("code"), 1)
        self.assertIn("Cannot listen", self.logs[-1])
        self.assertFalse(self.cfg.service_path.exists())


class BindSocketTest(unittest.TestCase):
    def test_port_zero_picks_a_free_port(self):
        from grain_search.service import bind_socket

        a, b = bind_socket("127.0.0.1", 0), bind_socket("localhost", 0)
        try:
            pa, pb = a.getsockname()[1], b.getsockname()[1]
            self.assertGreater(pa, 0)
            self.assertNotEqual(pa, pb)
            self.assertEqual(b.getsockname()[0], "127.0.0.1")
        finally:
            a.close()
            b.close()

    def test_service_url_brackets_ipv6(self):
        from grain_search.service import service_url

        self.assertEqual(service_url("127.0.0.1", 5), "http://127.0.0.1:5")
        self.assertEqual(service_url("::1", 5), "http://[::1]:5")


class ServiceFileTest(unittest.TestCase):
    def test_only_removes_its_own_service_file(self):
        from grain_search.service import _remove_service_file

        tmp = Path(tempfile.mkdtemp())
        try:
            cfg = Config(roots=[], data_dir=tmp)
            cfg.service_file = tmp / "service.json"
            cfg.service_file.write_text(json.dumps({"pid": os.getpid() + 1}), encoding="utf-8")
            _remove_service_file(cfg)
            self.assertTrue(cfg.service_file.exists(), "another process's file must stay")
            cfg.service_file.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
            _remove_service_file(cfg)
            self.assertFalse(cfg.service_file.exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
