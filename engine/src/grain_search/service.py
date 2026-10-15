"""`grain serve` - the background search service the Grain Search window talks to.

* Loads EmbeddingGemma 2 once and keeps it warm (GPU if available).
* Answers searches in two stages over Server-Sent Events:
      event "fast"  ~10-30 ms   file names + files containing the text
      event "full"  +50-150 ms  adds "related by meaning" documents and photos
* Keeps the index up to date in a background thread: one incremental pass at start,
  then live updates from a file watcher. Pauses automatically while a full-screen
  app/game is running or on battery.
* Listens on 127.0.0.1 only; every request needs the secret token from service.json
  (in the app data folder, see paths.py).
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import socket
import threading
import time
from dataclasses import asdict
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import __version__, previews, ranking, winstate
from .config import DEFAULT_EXCLUDES, Config, change_excludes, change_folders, read_raw, save_raw
from .embedder import Embedder, free_gpu_memory, is_out_of_memory
from .indexer import IndexState, Stopped, index_paths, run_index
from .search import Results, Searcher
from .store import FileRow, Store

ALLOWED_ORIGINS = [
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",  # Tauri 2 (macOS/Linux, Windows)
    "http://localhost:1420",
    "http://127.0.0.1:1420",  # Vite dev server
]
DEBOUNCE_S = 2.0
AUTO_PAUSE_CHECK_S = 5.0
GPU_FULL_RETRY_S = 60.0


# -- shared model ---------------------------------------------------------------------
class LockedEmbedder:
    """One model, two threads (searches + background indexing): serialise model calls.
    Indexing encodes one file at a time, so a query waits at most one small batch."""

    def __init__(self, inner: Embedder):
        self.inner = inner
        self.lock = threading.Lock()
        self.model_id, self.dim = inner.model_id, inner.dim

    @property
    def device_desc(self) -> str:
        return getattr(self.inner, "device_desc", "?")

    def _call(self, name, *a):
        with self.lock:
            return getattr(self.inner, name)(*a)

    def warm_up(self):
        return self._call("warm_up")

    def encode_query(self, text):
        return self._call("encode_query", text)

    def encode_documents(self, docs):
        return self._call("encode_documents", docs)

    def encode_images(self, paths):
        return self._call("encode_images", paths)

    @property
    def image_errors(self) -> dict:
        return getattr(self.inner, "image_errors", {})


# -- background indexing ----------------------------------------------------------------
class BackgroundIndexer:
    def __init__(self, cfg: Config, embedder: LockedEmbedder, watch: bool = True, initial_scan: bool = True):
        self.cfg, self.embedder = cfg, embedder
        self.watch, self.initial_scan = watch, initial_scan
        self.state = IndexState()
        self.events: queue.Queue[tuple[str, str, str | None]] = queue.Queue()
        self.stop_flag = threading.Event()
        self.manual_pause = threading.Event()
        self.rescan_flag = threading.Event()
        self.thread = threading.Thread(target=self._run, name="indexer", daemon=True)
        self.observer = None
        self._auto_reason = ""
        self._auto_checked = 0.0
        self._watch_lock = threading.Lock()
        self.last_report: dict | None = None

    # control
    def start(self):
        self.thread.start()

    def stop(self, timeout: float = 10.0):
        self.stop_flag.set()
        if self.observer is not None:
            try:
                self.observer.stop()
                self.observer.join(timeout=3)
            except Exception:
                pass
        self.thread.join(timeout=timeout)

    def pause(self):
        self.manual_pause.set()

    def resume(self):
        self.manual_pause.clear()

    def rescan(self):
        self.rescan_flag.set()

    def reconfigure(self):
        """Folders or exclusions changed: watch the new folder list and run a pass now
        (new folders get scanned, removed/excluded files leave the index)."""
        with self._watch_lock:
            old, self.observer = self.observer, None
            if old is not None:
                try:
                    old.stop()
                    old.join(timeout=3)
                except Exception:
                    pass
            if not self.stop_flag.is_set():
                self._start_watcher()
        self.rescan()

    # pause logic, called between files
    def _pause_reason(self) -> str:
        if self.manual_pause.is_set():
            return "paused by you"
        now = time.monotonic()
        if now - self._auto_checked > AUTO_PAUSE_CHECK_S:
            self._auto_checked = now
            reason = ""
            if self.cfg.pause_on_fullscreen and winstate.fullscreen_app_running():
                reason = "full-screen app running"
            elif self.cfg.pause_on_battery and winstate.on_battery():
                reason = "on battery"
            self._auto_reason = reason
        return self._auto_reason

    def checkpoint(self):
        if self.stop_flag.is_set():
            raise Stopped()
        reason = self._pause_reason()
        if not reason:
            return
        before = self.state.phase
        while reason and not self.stop_flag.is_set():
            self.state.phase, self.state.paused_reason = "paused", reason
            time.sleep(1.0)
            reason = self._pause_reason()
        if self.stop_flag.is_set():
            raise Stopped()
        self.state.phase, self.state.paused_reason = before, ""

    # watcher
    def _start_watcher(self):
        if not self.watch:
            return
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            self.state.last_error = "watchdog not installed - live updates are off"
            return
        events = self.events

        class Handler(FileSystemEventHandler):
            def on_any_event(self, ev):
                kind = ev.event_type
                if kind in ("opened", "closed_no_write"):
                    return
                if kind == "moved":
                    events.put(("moved", ev.src_path, getattr(ev, "dest_path", None)))
                else:
                    events.put(("changed", ev.src_path, None))

        obs = Observer()
        handler = Handler()
        for root in self.cfg.roots:
            if os.path.isdir(root):
                try:
                    obs.schedule(handler, root, recursive=True)
                except Exception as e:
                    self.state.last_error = f"cannot watch {root}: {e}"
        obs.daemon = True
        obs.start()
        self.observer = obs

    def _drain(self) -> tuple[set[str], list[tuple[str, str]]]:
        """Wait until the file system has been quiet for DEBOUNCE_S, then return the batch."""
        changed: set[str] = set()
        moves: list[tuple[str, str]] = []
        deadline = None
        while not self.stop_flag.is_set():
            try:
                kind, src, dst = self.events.get(timeout=0.5)
            except queue.Empty:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if self.rescan_flag.is_set():
                    break
                continue
            if kind == "moved" and dst:
                moves.append((src, dst))
            else:
                changed.add(src)
            deadline = time.monotonic() + DEBOUNCE_S
        data_dir = os.path.normcase(str(self.cfg.data_dir))
        changed = {p for p in changed if not os.path.normcase(p).startswith(data_dir)}
        return changed, moves

    # thread body
    def _run(self):
        winstate.lower_current_thread_priority()
        store = Store(self.cfg.db_path)
        try:
            with self._watch_lock:
                self._start_watcher()
            if self.initial_scan:
                self._full_pass(store)
            while not self.stop_flag.is_set():
                if self.rescan_flag.is_set():
                    self.rescan_flag.clear()
                    self._full_pass(store)
                    continue
                changed, moves = self._drain()
                if (changed or moves) and not self.stop_flag.is_set():
                    try:
                        store.commit()
                        self.checkpoint()
                        rep = index_paths(
                            self.cfg, store, self.embedder, changed, moves, checkpoint=self.checkpoint, state=self.state
                        )
                        self.last_report = {"kind": "live", **asdict(rep)}
                    except Stopped:
                        break
                    except Exception as e:  # never let the indexer thread die
                        self.state.last_error = f"{type(e).__name__}: {e}"[:2000]
                        if is_out_of_memory(e) and self._wait_for_gpu():
                            self.rescan_flag.set()  # files left half-done are 'pending': a full pass redoes them
                    finally:
                        if self.state.phase != "paused":
                            self.state.phase = "idle"
        finally:
            try:
                store.commit()
                store.close()
            except Exception:
                pass

    def _full_pass(self, store: Store):
        while not self.stop_flag.is_set():
            try:
                rep = run_index(self.cfg, store, self.embedder, checkpoint=self.checkpoint, state=self.state)
                self.last_report = {"kind": "full", **asdict(rep)}
                self.state.last_error = ""
                return
            except Stopped:
                return
            except Exception as e:
                self.state.last_error = f"{type(e).__name__}: {e}"[:2000]
                if not (is_out_of_memory(e) and self._wait_for_gpu()):
                    return
                # GPU had room again: run the pass again (finished files are skipped quickly)
            finally:
                self.state.phase, self.state.current = "idle", ""

    def _wait_for_gpu(self) -> bool:
        """The GPU is full (a game or another app is using it): pause, then try again.
        Returns False if the service is stopping."""
        free_gpu_memory()
        self.state.phase = "paused"
        self.state.paused_reason = "GPU memory full - retrying in a minute"
        end = time.monotonic() + GPU_FULL_RETRY_S
        while time.monotonic() < end:
            if self.stop_flag.wait(1.0):
                return False
        self.state.paused_reason = ""
        return True


# -- the web app ------------------------------------------------------------------------
def _doc_json(d) -> dict:
    out = asdict(d)
    out["name"] = os.path.basename(d.path)
    out["folder"] = os.path.dirname(d.path)
    out["ext"] = os.path.splitext(d.path)[1].lower()
    out["contains_text"] = d.contains_text
    return out


def _image_json(i) -> dict:
    out = asdict(i)
    out["name"] = os.path.basename(i.path)
    out["folder"] = os.path.dirname(i.path)
    return out


class SearchService:
    def __init__(
        self,
        cfg: Config,
        embedder: Embedder,
        *,
        background: bool = True,
        watch: bool = True,
        initial_scan: bool = True,
        token: str | None = None,
    ):
        self.cfg = cfg
        self.embedder = LockedEmbedder(embedder)
        self.store = Store(cfg.db_path, check_same_thread=False)
        self.store.check_model(embedder.model_id, embedder.dim)
        ranking.ensure(self.store)
        self.store.commit()
        self.db_lock = threading.RLock()
        self.searcher = Searcher(cfg, self.store, self.embedder)
        self.token = token or secrets.token_urlsafe(32)
        self.started = time.time()
        self.indexer = BackgroundIndexer(cfg, self.embedder, watch, initial_scan) if background else None
        self.thumb_dir = cfg.data_dir / "thumbs"
        self._config_lock = threading.Lock()
        self.request_exit = lambda: None  # set by serve(): asks the web server to stop
        self.app = self._build_app()

    # lifecycle
    def startup(self):
        self.embedder.warm_up()
        if self.indexer:
            self.indexer.start()

    def shutdown(self):
        if self.indexer:
            self.indexer.stop()
        with self.db_lock:
            try:
                self.store.save_vector_cache()
            except Exception:
                pass
            try:
                self.store.close()  # release the file (Windows keeps open files locked)
            except Exception:
                pass

    # helpers
    def _authorised(self, request: Request) -> bool:
        h = request.headers.get("authorization", "")
        tok = h[7:] if h.lower().startswith("bearer ") else request.query_params.get("token", "")
        return secrets.compare_digest(tok, self.token)

    # settings (folders + exclusions), changed live from the app's Settings window
    def _config_view(self) -> dict:
        cfg = self.cfg
        with self.db_lock:
            counts = {r: self.store.count_under(r) for r in cfg.roots}
        builtin = {p.lower() for p in DEFAULT_EXCLUDES}
        return {
            "config_file": str(cfg.config_path) if cfg.config_path else None,
            "index_dir": str(cfg.data_dir),
            "setup_needed": not cfg.roots,
            "folders": [
                {
                    "path": r,
                    "exists": os.path.isdir(r),
                    "indexed": counts[r].get("indexed", 0),
                    "pending": counts[r].get("pending", 0),
                    "errors": counts[r].get("error", 0),
                    "total": sum(counts[r].values()),
                }
                for r in cfg.roots
            ],
            "excludes": [{"pattern": p, "builtin": p.lower() in builtin} for p in cfg.excludes],
        }

    def _change_config(self, what: str, action: str, value: str) -> dict:
        """Apply one change, save config.json, update the live settings, re-scan."""
        if self.cfg.config_path is None:
            raise ValueError("This service was started without a settings file")
        with self._config_lock:
            raw = read_raw(self.cfg.config_path)
            change = change_folders if what == "folders" else change_excludes
            changed, message, value = change(raw, action, value)
            if changed:
                save_raw(self.cfg.config_path, raw)
                # swap in new lists (never mutate: the indexer/searcher may be reading the old ones)
                if what == "folders":
                    self.cfg.roots = list(raw["roots"])
                else:
                    self.cfg.excludes = list(raw["excludes"])
        if changed and self.indexer:
            self.indexer.reconfigure()
        return {"ok": True, "changed": changed, "message": message, "value": value, **self._config_view()}

    def _row(self, path: str | None) -> FileRow | None:
        """Only files that are in the index can be previewed/opened - never arbitrary paths."""
        if not path:
            return None
        with self.db_lock:
            return self.store.get_file(path)

    def _search(self, q: str, limit: int, stage: str) -> dict:
        query = q
        if stage == "fast":
            if q.lstrip().startswith("~"):
                return {"stage": "fast", "docs": [], "images": [], "ms": 0.0, "skipped": True}
            query = "=" + q.lstrip().lstrip("=")
        with self.db_lock:
            res: Results = self.searcher.search(query, limit=limit)
            b = ranking.boosts(self.store, [d.path for d in res.docs], q)
        docs = ranking.rerank(res.docs, b)
        top = None
        if docs and (docs[0].contains_text or not res.images):
            top = docs[0].path
        elif res.images:
            top = res.images[0].path
        return {
            "stage": stage,
            "query": q,
            "mode": res.query.mode,
            "docs": [_doc_json(d) for d in docs],
            "images": [_image_json(i) for i in res.images],
            "top": top,
            "ms": round(res.ms, 1),
        }

    # routes
    def _build_app(self) -> Starlette:
        svc = self

        async def health(request: Request):
            return JSONResponse({"ok": True, "version": __version__})

        async def status(request: Request):
            def build():
                with svc.db_lock:
                    st = svc.store.stats()
                ix = svc.indexer
                return {
                    "version": __version__,
                    "uptime_s": round(time.time() - svc.started),
                    "device": svc.embedder.device_desc,
                    "model": svc.embedder.model_id,
                    "setup_needed": not svc.cfg.roots,
                    "folders": list(svc.cfg.roots),
                    "config_file": str(svc.cfg.config_path) if svc.cfg.config_path else None,
                    "index": st,
                    "indexer": None
                    if ix is None
                    else {
                        **asdict(ix.state),
                        "manual_pause": ix.manual_pause.is_set(),
                        "queued_events": ix.events.qsize(),
                        "last_report": ix.last_report,
                    },
                }

            return JSONResponse(await run_in_threadpool(build))

        async def search(request: Request):
            q = request.query_params.get("q", "").strip()
            try:
                limit = max(1, min(int(request.query_params.get("limit", "10")), 50))
            except ValueError:
                limit = 10
            if request.query_params.get("stream", "1") in ("0", "false", "no"):
                return JSONResponse(await run_in_threadpool(svc._search, q, limit, "full"))

            async def events():
                for stage in ("fast", "full"):
                    if await request.is_disconnected():  # a newer keystroke replaced this query
                        return
                    try:
                        data = await run_in_threadpool(svc._search, q, limit, stage)
                        yield f"event: {stage}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                    except Exception as e:
                        yield f"event: error\ndata: {json.dumps({'error': f'{type(e).__name__}: {e}'})}\n\n"
                        return
                yield "event: done\ndata: {}\n\n"

            return StreamingResponse(
                events(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        async def preview(request: Request):
            p = request.query_params
            row = svc._row(p.get("path"))
            if row is None:
                return JSONResponse({"error": "not in index"}, status_code=404)
            page = int(p["page"]) if p.get("page", "").isdigit() else None
            line = int(p["line"]) if p.get("line", "").isdigit() else None
            return JSONResponse(await run_in_threadpool(previews.preview, row, p.get("q", ""), page, line))

        async def thumb(request: Request):
            p = request.query_params
            row = svc._row(p.get("path"))
            if row is None:
                return Response(status_code=404)
            size = int(p["size"]) if p.get("size", "").isdigit() else 256
            data = await run_in_threadpool(previews.thumbnail, row, size, svc.thumb_dir)
            if data is None:
                return Response(status_code=404)
            return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

        async def page(request: Request):
            p = request.query_params
            row = svc._row(p.get("path"))
            if row is None or row.kind != "pdf":
                return Response(status_code=404)
            n = int(p["page"]) if p.get("page", "").isdigit() else 1
            width = int(p["width"]) if p.get("width", "").isdigit() else 900
            data = await run_in_threadpool(previews.pdf_page_png, row, n, width)
            if data is None:
                return Response(status_code=404)
            return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

        async def open_(request: Request):
            from .opener import open_file, reveal

            body = await _json(request)
            row = svc._row(body.get("path"))
            if row is None:
                return JSONResponse({"error": "not in index"}, status_code=404)
            mode = body.get("mode", "open")
            try:
                if mode == "reveal":
                    await run_in_threadpool(reveal, row.path)
                    how = "explorer"
                else:
                    how = await run_in_threadpool(open_file, row.path, body.get("page"), body.get("line"))
            except FileNotFoundError:
                return JSONResponse({"error": "file no longer exists"}, status_code=410)
            if body.get("query") is not None:
                with svc.db_lock:
                    ranking.record_open(svc.store, str(body["query"]), row.path)
            return JSONResponse({"ok": True, "how": how})

        async def feedback(request: Request):
            body = await _json(request)
            row = svc._row(body.get("path"))
            if row is None:
                return JSONResponse({"error": "not in index"}, status_code=404)
            with svc.db_lock:
                ranking.record_open(svc.store, str(body.get("query", "")), row.path)
            return JSONResponse({"ok": True})

        def _control(action):
            async def handler(request: Request):
                if svc.indexer is None:
                    return JSONResponse({"error": "background indexing is off"}, status_code=409)
                getattr(svc.indexer, action)()
                return JSONResponse({"ok": True, "action": action})

            return handler

        async def shutdown(request: Request):
            svc.request_exit()
            return JSONResponse({"ok": True})

        async def config_get(request: Request):
            return JSONResponse(await run_in_threadpool(svc._config_view))

        def _config_change(what: str):
            async def handler(request: Request):
                body = await _json(request)
                action = str(body.get("action", ""))
                value = str(body.get("path") or body.get("value") or "")
                try:
                    return JSONResponse(await run_in_threadpool(svc._change_config, what, action, value))
                except ValueError as e:
                    return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

            return handler

        routes = [
            Route("/shutdown", shutdown, methods=["POST"]),
            Route("/health", health),
            Route("/status", status),
            Route("/search", search),
            Route("/preview", preview),
            Route("/thumb", thumb),
            Route("/page", page),
            Route("/open", open_, methods=["POST"]),
            Route("/feedback", feedback, methods=["POST"]),
            Route("/index/pause", _control("pause"), methods=["POST"]),
            Route("/index/resume", _control("resume"), methods=["POST"]),
            Route("/index/rescan", _control("rescan"), methods=["POST"]),
            Route("/config", config_get),
            Route("/config/folders", _config_change("folders"), methods=["POST"]),
            Route("/config/excludes", _config_change("excludes"), methods=["POST"]),
        ]

        class Auth:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["type"] == "http" and scope["path"] != "/health" and scope["method"] != "OPTIONS":
                    if not svc._authorised(Request(scope)):
                        await JSONResponse({"error": "unauthorised"}, status_code=401)(scope, receive, send)
                        return
                await self.app(scope, receive, send)

        return Starlette(
            routes=routes,
            middleware=[
                Middleware(
                    CORSMiddleware,
                    allow_origins=ALLOWED_ORIGINS,
                    allow_methods=["GET", "POST"],
                    allow_headers=["Authorization", "Content-Type"],
                ),
                Middleware(Auth),
            ],
        )


async def _json(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# -- running it -------------------------------------------------------------------------
def running_service(cfg: Config) -> dict | None:
    """The service.json of a service that is actually up (answers /health), else None."""
    import urllib.request

    try:
        info = json.loads(cfg.service_path.read_text(encoding="utf-8"))
        with urllib.request.urlopen(info["url"] + "/health", timeout=1.5) as r:
            return info if r.status == 200 else None
    except Exception:
        return None


def bind_socket(host: str, port: int = 0) -> socket.socket:
    """Bind the listening socket up front. Port 0 lets the OS pick a free port, so two apps can
    never fight over a fixed number, and nothing can take the port between choosing and using it.
    Raises OSError if a fixed port is taken."""
    host = "127.0.0.1" if host == "localhost" else host
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: no other program may share the port
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    return sock


def service_url(host: str, port: int) -> str:
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def _remove_service_file(cfg: Config) -> None:
    """Remove service.json, but only if it is ours (a newer service may have replaced it)."""
    try:
        info = json.loads(cfg.service_path.read_text(encoding="utf-8"))
        if info.get("pid") == os.getpid():
            cfg.service_path.unlink()
    except (OSError, ValueError):
        pass


def write_service_file(cfg: Config, host: str, port: int, token: str) -> Path:
    """service.json tells the Grain Search window where the service is and how to authenticate."""
    path = cfg.service_path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"url": service_url(host, port), "token": token, "pid": os.getpid(), "version": __version__}),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


def _make_graceful_server():
    import contextlib
    import signal

    import uvicorn

    class GracefulServer(uvicorn.Server):
        """uvicorn re-raises Ctrl+C / SIGTERM after stopping, which would kill the process
        before our cleanup runs. Here a signal just asks the server to stop."""

        def _handlers(self):
            if threading.current_thread() is not threading.main_thread():
                return {}  # signals can only be handled on the main thread (tests run it in a thread)

            def ask_to_stop(sig, frame):
                self.should_exit = True

            sigs = [signal.SIGINT, signal.SIGTERM] + ([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])
            return {s: signal.signal(s, ask_to_stop) for s in sigs}

        @contextlib.contextmanager
        def capture_signals(self):  # uvicorn >= 0.29
            old = self._handlers()
            try:
                yield
            finally:
                for s, h in old.items():
                    signal.signal(s, h)

        def install_signal_handlers(self):  # older uvicorn
            self._handlers()

    return GracefulServer


def _GracefulServer(config):
    return _make_graceful_server()(config)


def serve(
    cfg: Config,
    embedder: Embedder,
    host: str = "127.0.0.1",
    port: int = 0,
    background: bool = True,
    watch: bool = True,
    log=print,
) -> int:
    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError("The service only listens on this PC (127.0.0.1).")
    other = running_service(cfg)
    if other:
        log(f"Grain Search is already running at {other.get('url')} - use that one (`grain status`, tray icon).")
        return 1
    try:
        sock = bind_socket(host, port)
    except OSError as e:
        log(
            f"Cannot listen on {host}:{port} ({e}). Another program is using that port; pick another or use 0 (any free port)."
        )
        return 1
    host, port = sock.getsockname()[:2]
    svc = None
    try:
        svc = SearchService(cfg, embedder, background=background, watch=watch)
        log(f"Loading {embedder.model_id} ...")
        svc.startup()
        log(f"Model ready on {svc.embedder.device_desc}")
        info = write_service_file(cfg, host, port, svc.token)
        log(f"Search service on {service_url(host, port)}  (token in {info})")
        if svc.indexer:
            log("Background indexing: on" + (" + live file watching" if watch else ""))
        server = _GracefulServer(
            uvicorn.Config(svc.app, host=host, port=port, log_level="warning", access_log=False, lifespan="off")
        )
        svc.request_exit = lambda: setattr(server, "should_exit", True)
        server.run(sockets=[sock])
        log("Stopping...")
    finally:
        if svc is not None:
            svc.shutdown()
        sock.close()
        _remove_service_file(cfg)
    return 0
