"""The service stops when the app does, and files it opens stay open (Windows job object)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from grain_search import opener, winstate  # noqa: E402
from grain_search.config import Config  # noqa: E402
from grain_search.embedder import HashEmbedder  # noqa: E402

try:
    import starlette  # noqa: F401
    import uvicorn  # noqa: F401

    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False

_REAL_WINSTATE = (winstate.on_battery, winstate.fullscreen_app_running)


def setUpModule():
    winstate.on_battery = lambda: False
    winstate.fullscreen_app_running = lambda: False


def tearDownModule():
    winstate.on_battery, winstate.fullscreen_app_running = _REAL_WINSTATE


def sleeper() -> subprocess.Popen:
    """Stands in for the Grain Search app process."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def wait_for(cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


class WatchParentTest(unittest.TestCase):
    def test_alive(self):
        from grain_search.service import process_alive

        self.assertTrue(process_alive(os.getpid()))
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.assertFalse(process_alive(p.pid))

    def test_called_when_the_app_ends(self):
        from grain_search.service import watch_parent

        app = sleeper()
        gone = threading.Event()
        try:
            watch_parent(app.pid, gone.set, poll=0.2)
            self.assertFalse(gone.wait(1.0), "must not fire while the app runs")
            app.kill()
            app.wait()
            self.assertTrue(gone.wait(10), "must fire once the app is gone")
        finally:
            if app.poll() is None:
                app.kill()

    def test_called_at_once_if_already_gone(self):
        from grain_search.service import watch_parent

        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        gone = threading.Event()
        watch_parent(p.pid, gone.set, poll=0.2)
        self.assertTrue(gone.wait(5))


@unittest.skipUnless(HAVE_WEB, "starlette/uvicorn not installed")
class ServeStopsWithAppTest(unittest.TestCase):
    def test_serve_exits_cleanly_when_the_app_ends(self):
        from grain_search import service

        tmp = Path(tempfile.mkdtemp())
        (tmp / "files").mkdir()
        cfg = Config(roots=[str(tmp / "files")], data_dir=tmp / "data")
        cfg.service_file = tmp / "service.json"
        app = sleeper()
        old_env = os.environ.get("GRAIN_SEARCH_PARENT_PID")
        os.environ["GRAIN_SEARCH_PARENT_PID"] = str(app.pid)
        logs: list[str] = []
        result: dict = {}
        try:
            t = threading.Thread(
                target=lambda: result.setdefault(
                    "code", service.serve(cfg, HashEmbedder(), port=0, watch=False, log=logs.append)
                ),
                daemon=True,
            )
            t.start()
            self.assertTrue(wait_for(cfg.service_file.exists), logs)
            self.assertEqual(json.loads(cfg.service_file.read_text())["pid"], os.getpid())
            app.kill()
            app.wait()
            t.join(20)
            self.assertFalse(t.is_alive(), "the service should stop when the app is gone")
            self.assertEqual(result.get("code"), 0)
            self.assertIn("app has closed", " ".join(logs))
            self.assertFalse(cfg.service_file.exists())
        finally:
            if app.poll() is None:
                app.kill()
            if old_env is None:
                os.environ.pop("GRAIN_SEARCH_PARENT_PID", None)
            else:
                os.environ["GRAIN_SEARCH_PARENT_PID"] = old_env
            shutil.rmtree(tmp, ignore_errors=True)


class FakeProc:
    def __init__(self, code=0, err=b"", hang=False):
        self.returncode, self.err, self.hang = code, err, hang

    def communicate(self, timeout=None):
        if self.hang:
            raise subprocess.TimeoutExpired("helper", timeout)
        return b"", self.err


class FakePopen:
    """Records how programs are started; optionally refuses breaking away once."""

    def __init__(self, refuse_breakaway=False, proc=None):
        self.calls: list[tuple[list, dict]] = []
        self.refuse_breakaway = refuse_breakaway
        self.proc = proc or FakeProc()

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        if self.refuse_breakaway and kw.get("creationflags", 0) & opener.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError("Access is denied")
        return self.proc


class OpenerBreakawayTest(unittest.TestCase):
    def test_windows_programs_break_away_from_the_job(self):
        fake = FakePopen()
        opener._launch(["SumatraPDF.exe", "x.pdf"], windows=True, popen=fake)
        self.assertEqual(fake.calls[0][1]["creationflags"], opener.CREATE_BREAKAWAY_FROM_JOB)

    def test_extra_flags_are_kept(self):
        fake = FakePopen()
        opener._launch(["code"], windows=True, flags=opener.CREATE_NO_WINDOW, popen=fake, shell=True)
        args, kw = fake.calls[0]
        self.assertEqual(kw["creationflags"], opener.CREATE_NO_WINDOW | opener.CREATE_BREAKAWAY_FROM_JOB)
        self.assertTrue(kw["shell"])

    def test_falls_back_when_breaking_away_is_not_allowed(self):
        fake = FakePopen(refuse_breakaway=True)
        opener._launch(["x"], windows=True, popen=fake)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[1][1]["creationflags"], 0)

    def test_no_windows_flags_elsewhere(self):
        fake = FakePopen()
        opener._launch(["xdg-open", "x"], windows=False, popen=fake)
        self.assertNotIn("creationflags", fake.calls[0][1])

    def test_default_app_opened_from_a_helper(self):
        fake = FakePopen()
        opener._startfile_outside_job(r"C:\docs\a & b.docx", popen=fake)
        args, kw = fake.calls[0]
        self.assertEqual(args[0], sys.executable)
        self.assertEqual(args[-1], r"C:\docs\a & b.docx", "path passed as its own argument, never through a shell")
        self.assertNotIn("shell", kw)
        self.assertTrue(kw["creationflags"] & opener.CREATE_BREAKAWAY_FROM_JOB)

    def test_default_app_error_is_reported(self):
        err = b"Traceback...\nOSError: [WinError 1155] No application is associated with the specified file\n"
        fake = FakePopen(proc=FakeProc(code=1, err=err))
        with self.assertRaises(OSError) as ctx:
            opener._startfile_outside_job("x.abc", popen=fake)
        self.assertIn("No application is associated", str(ctx.exception))

    def test_slow_hand_over_is_not_an_error(self):
        opener._startfile_outside_job("x.docx", popen=FakePopen(proc=FakeProc(hang=True)))

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_helper_really_runs_on_windows(self):
        # the helper's own code: os.startfile on a missing file must surface Windows' error
        with self.assertRaises(OSError):
            opener._startfile_outside_job(str(Path(tempfile.gettempdir()) / "grain-no-such-file.qqq"))


if __name__ == "__main__":
    unittest.main()
