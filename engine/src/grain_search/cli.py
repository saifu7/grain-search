"""Command line:  grain index | grain search "..." | grain open N | grain status | grain reset
| grain folders [add|remove] | grain exclude [add|remove] | grain config | grain (interactive)"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .config import DEFAULT_EXCLUDES, Config, expand, load_config, read_raw, save_raw


# -- console helpers ------------------------------------------------------------
def _setup_console():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if sys.platform == "win32":
        os.system("")  # turns on ANSI colours in the Windows console


_TTY = sys.stdout.isatty()


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _TTY else s


bold = lambda s: _c("1", s)  # noqa: E731
dim = lambda s: _c("2", s)  # noqa: E731
green = lambda s: _c("32", s)  # noqa: E731
cyan = lambda s: _c("36", s)  # noqa: E731
yellow = lambda s: _c("33", s)  # noqa: E731


# -- wiring -----------------------------------------------------------------------
def _open_store(cfg: Config, fake: bool):
    from .store import Store

    if fake:  # keep the fake test index away from the real one
        cfg.data_dir = cfg.data_dir.with_name(cfg.data_dir.name + "-fake")
    return Store(cfg.db_path)


def _embedder(cfg: Config, fake: bool):
    from .embedder import make_embedder

    return make_embedder(cfg.model, fake=fake)


def _last_path(cfg: Config) -> Path:
    return cfg.data_dir / "last_results.json"


def _save_last(cfg: Config, items: list[dict]):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    _last_path(cfg).write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")


def _load_last(cfg: Config) -> list[dict]:
    p = _last_path(cfg)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


# -- printing ---------------------------------------------------------------------
def _print_results(res, cfg: Config) -> list[dict]:
    items: list[dict] = []
    n = 0

    def doc_line(d):
        nonlocal n
        n += 1
        where = f"p.{d.page}" if d.page else (f"line {d.line}" if d.line else "")
        tag = {"exact": green(" EXACT"), "words": green(" ALL WORDS"), "name": cyan(" FILE NAME")}.get(d.match, "")
        sim = dim(f"  similarity {d.similarity:.2f}") if d.match == "meaning" and d.similarity is not None else ""
        print(f"{yellow(f'{n:>2}')}  {bold(os.path.basename(d.path))}{tag}  {dim(where)}{sim}")
        print(f"    {dim(os.path.dirname(d.path))}")
        if d.snippet:
            print(f"    {d.snippet}")
        items.append({"n": n, "path": d.path, "page": d.page, "line": d.line})

    found = [d for d in res.docs if d.contains_text]
    related = [d for d in res.docs if not d.contains_text]
    if found:
        print(bold("Contains your text"))
        for d in found:
            doc_line(d)
    if related:
        print(bold("Related by meaning") + dim("  (your exact words are NOT in these files)"))
        for d in related:
            doc_line(d)
    if res.docs and not found and res.query.mode != "meaning":
        print(dim("No file contains this text exactly - the files above are only similar in meaning."))
    if res.images:
        print(bold("Images"))
        for im in res.images:
            n += 1
            extra = im.meta.get("taken", "")
            print(f"{yellow(f'{n:>2}')}  {bold(os.path.basename(im.path))}  {dim(f'{im.score:.3f}')}  {dim(extra)}")
            print(f"    {dim(os.path.dirname(im.path))}")
            items.append({"n": n, "path": im.path, "page": None, "line": None})
    if not items:
        print(dim("No results."))
    print(dim(f"({res.ms:.0f} ms)"))
    return items


def _open_item(items: list[dict], n: int, reveal_only: bool = False):
    from .opener import open_file, reveal

    hit = next((i for i in items if i["n"] == n), None)
    if not hit:
        print(f"No result #{n}.")
        return
    if reveal_only:
        reveal(hit["path"])
        print(f"Showing in folder: {hit['path']}")
    else:
        how = open_file(hit["path"], hit["page"], hit["line"])
        print(f"Opened ({how}): {hit['path']}")


# -- commands ---------------------------------------------------------------------
def cmd_index(args, cfg: Config) -> int:
    from .indexer import run_index

    if not cfg.roots:
        _no_folders_hint(cfg)
        return 1
    store = _open_store(cfg, args.fake)
    try:
        from .service import running_service

        live = running_service(cfg)
    except ImportError:  # starlette not installed: no service either
        live = None
    if live:
        store.close()
        print(yellow("The background search service is running and already keeps the index up to date."))
        print("Use the Grain Search tray icon (Re-scan all folders), or quit Grain Search first and run this again.")
        return 1
    emb = _embedder(cfg, args.fake)
    kinds = {"text": {"text", "pdf", "office"}, "images": {"image"}}.get(args.only)
    print(bold("Indexing:"))
    for r in cfg.roots:
        print(f"  {r}" + ("" if os.path.isdir(r) else dim("  (not found, skipped)")))
    try:
        rep = run_index(cfg, store, emb, progress=lambda s: print(dim(s)), kinds=kinds, retry_errors=args.retry_errors)
    except KeyboardInterrupt:
        store.commit()
        print("\nStopped. Progress is saved - run `grain index` again to continue.")
        return 130
    finally:
        store.close()
    print(
        green("Done") + f" in {rep.seconds:.0f}s on {getattr(emb, 'device_desc', '?')}: "
        f"{rep.indexed:,} indexed ({rep.chunks:,} text chunks, {rep.images:,} images), "
        f"{rep.unchanged:,} unchanged, {rep.moved:,} moved, {rep.removed:,} removed, "
        f"{rep.empty:,} empty, {rep.errors:,} errors."
    )
    for e in rep.error_samples[:5]:
        print(dim("  ! " + e))
    return 0


def cmd_search(args, cfg: Config) -> int:
    from .search import Searcher

    store = _open_store(cfg, args.fake)
    emb = _embedder(cfg, args.fake)
    store.check_model(emb.model_id, emb.dim)
    query = " ".join(args.query)
    if args.exact and not query.startswith("="):
        query = "=" + query
    res = Searcher(cfg, store, emb).search(query, limit=args.n)
    if args.json:
        print(
            json.dumps(
                {"docs": [asdict(d) for d in res.docs], "images": [asdict(i) for i in res.images], "ms": res.ms},
                ensure_ascii=False,
                indent=2,
            )
        )
        items = [
            {"n": i + 1, "path": h.path, "page": getattr(h, "page", None), "line": getattr(h, "line", None)}
            for i, h in enumerate(res.docs + res.images)
        ]
    else:
        items = _print_results(res, cfg)
    _save_last(cfg, items)
    if args.open and res.top is not None:
        n = next(i["n"] for i in items if i["path"] == res.top.path)
        _open_item(items, n)
    store.close()
    return 0


def cmd_open(args, cfg: Config) -> int:
    if args.fake:
        cfg.data_dir = cfg.data_dir.with_name(cfg.data_dir.name + "-fake")
    items = _load_last(cfg)
    if not items:
        print("Search first, then `grain open N`.")
        return 1
    _open_item(items, args.n, reveal_only=args.reveal)
    return 0


def cmd_status(args, cfg: Config) -> int:
    store = _open_store(cfg, args.fake)
    s = store.stats()
    store.close()
    print(bold("Index: ") + str(cfg.db_path) + dim(f"  ({s['db_mb']} MB)"))
    print(f"Model:   {s['model'] or '-'}")
    print(f"Files:   {s['files']:,}   " + "  ".join(f"{k}: {v:,}" for k, v in sorted(s["by_status"].items())))
    print("Indexed: " + ("  ".join(f"{k}: {v:,}" for k, v in sorted(s["indexed_by_kind"].items())) or "-"))
    print(f"Vectors: {s['text_vectors']:,} text chunks, {s['image_vectors']:,} images")
    print(bold("Folders:"))
    for r in cfg.roots:
        print(f"  {r}" + ("" if os.path.isdir(r) else dim("  (not found)")))
    return 0


def cmd_reset(args, cfg: Config) -> int:
    if args.fake:
        cfg.data_dir = cfg.data_dir.with_name(cfg.data_dir.name + "-fake")
    if not cfg.data_dir.exists():
        print("Nothing to reset.")
        return 0
    if not args.yes:
        ans = input(f"Delete the index in {cfg.data_dir}? Your files are NOT touched. [y/N] ")
        if ans.strip().lower() != "y":
            return 1
    shutil.rmtree(cfg.data_dir)
    print("Index deleted. Run `grain index` to rebuild.")
    return 0


def cmd_interactive(args, cfg: Config) -> int:
    from .search import Searcher

    store = _open_store(cfg, args.fake)
    emb = _embedder(cfg, args.fake)
    store.check_model(emb.model_id, emb.dim)
    searcher = Searcher(cfg, store, emb)
    print(bold("Grain Search") + dim("  - type to search · number = open · r<number> = show in folder · q = quit"))
    print(dim('  =text  only files containing the text   ·   ~text  only by meaning   ·   "phrase"  exact phrase'))
    if not args.fake:
        t0 = time.perf_counter()
        print(dim("Loading EmbeddingGemma 2..."), end="", flush=True)
        emb.encode_query("warm up")
        print(dim(f" ready on {emb.device_desc} ({time.perf_counter() - t0:.0f}s)"))
    items: list[dict] = []
    while True:
        try:
            line = input(cyan("\nsearch> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.lower() in ("q", "quit", "exit"):
            break
        try:
            if line.isdigit() and items:
                _open_item(items, int(line))
            elif line[:1].lower() == "r" and line[1:].strip().isdigit() and items:
                _open_item(items, int(line[1:]), reveal_only=True)
            else:
                items = _print_results(searcher.search(line, limit=args.n), cfg)
                _save_last(cfg, items)
        except Exception as e:  # keep the session alive
            print(f"Error: {e}")
    store.close()
    return 0


def cmd_serve(args, cfg: Config) -> int:
    from .service import serve

    if args.fake:
        cfg.data_dir = cfg.data_dir.with_name(cfg.data_dir.name + "-fake")
    if not cfg.roots:
        print(yellow("No folders set up yet: the service starts, but there is nothing to search."))
        _no_folders_hint(cfg)
    emb = _embedder(cfg, args.fake)
    return serve(
        cfg,
        emb,
        port=args.port or cfg.port,
        background=not args.no_index,
        watch=cfg.watch and not args.no_watch,
        log=lambda s: print(dim(s)),
    )


def _no_folders_hint(cfg: Config):
    print("No folders to index yet. Add one, for example:")
    example = '"%USERPROFILE%\\Documents"' if os.name == "nt" else "~/Documents"
    print(cyan(f"  grain folders add {example}"))
    print(dim(f"Settings file: {cfg.config_path}"))


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _restart_note():
    print(dim("If Grain Search is running, restart it (tray -> Quit, then start it again) to apply this."))


def _settings_file(cfg: Config):
    if cfg.config_path is None:
        raise RuntimeError("No settings file in use")
    return cfg.config_path


def cmd_config(args, cfg: Config) -> int:
    exists = cfg.config_path is not None and cfg.config_path.exists()
    print(bold("Settings file: ") + str(cfg.config_path) + ("" if exists else dim("  (not created yet)")))
    print(bold("Index folder:  ") + str(cfg.data_dir))
    print(bold("Service file:  ") + str(cfg.service_path))
    print(bold("Folders:"))
    for r in cfg.roots:
        print(f"  {r}" + ("" if os.path.isdir(r) else dim("  (not found)")))
    if not cfg.roots:
        print(dim("  (none yet)"))
    return 0


def cmd_folders(args, cfg: Config) -> int:
    action = args.action or "list"
    if action == "list":
        if not cfg.roots:
            _no_folders_hint(cfg)
            return 0
        for r in cfg.roots:
            print(r + ("" if os.path.isdir(r) else dim("  (not found)")))
        return 0
    if not args.paths:
        print(yellow(f"Which folder? e.g.  grain folders {action} D:\\Documents"))
        return 1
    path_file = _settings_file(cfg)
    raw = read_raw(path_file)
    roots: list[str] = list(raw.get("roots") or [])
    changed = False
    for given in args.paths:
        path = os.path.abspath(expand(given))
        if action == "add":
            if not os.path.isdir(path):
                print(yellow(f"Not a folder: {path}"))
                return 1
            if any(_same_path(expand(r), path) for r in roots):
                print(dim(f"Already indexed: {path}"))
                continue
            roots.append(path)
            print(green("Added ") + path)
        else:
            keep = [r for r in roots if not _same_path(expand(r), path)]
            if len(keep) == len(roots):
                print(yellow(f"Not in the list: {path}"))
                continue
            roots = keep
            print(green("Removed ") + path + dim("  (its files leave the index on the next scan)"))
        changed = True
    if changed:
        raw["roots"] = roots
        save_raw(path_file, raw)
        _restart_note()
    return 0


def _as_exclude_pattern(given: str) -> str:
    if "*" in given or "?" in given:
        return given  # already a pattern, e.g. *\\node_modules\\*  or  *.log
    return os.path.abspath(expand(given)).rstrip("\\/") + os.sep + "*"  # a folder and everything in it


def cmd_exclude(args, cfg: Config) -> int:
    action = args.action or "list"
    path_file = _settings_file(cfg)
    raw = read_raw(path_file)
    pats: list[str] = list(raw.get("excludes") or DEFAULT_EXCLUDES)
    if action == "list":
        for p in pats:
            print(p)
        return 0
    if not args.items:
        print(yellow(f"Which folder or pattern? e.g.  grain exclude {action} D:\\Documents\\old  or  *.log"))
        return 1
    changed = False
    for given in args.items:
        pat = _as_exclude_pattern(given)
        if action == "add":
            if any(_same_path(p, pat) for p in pats):
                print(dim(f"Already excluded: {pat}"))
                continue
            pats.append(pat)
            print(green("Excluded ") + pat)
        else:
            keep = [p for p in pats if not _same_path(p, pat)]
            if len(keep) == len(pats):
                print(yellow(f"Not in the list: {pat}"))
                continue
            pats = keep
            print(green("No longer excluded ") + pat)
        changed = True
    if changed:
        raw["excludes"] = pats
        save_raw(path_file, raw)
        _restart_note()
    return 0


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    ap = argparse.ArgumentParser(prog="grain", description="Search everything on your PC with EmbeddingGemma 2.")
    ap.add_argument(
        "--config", help="settings file to use (default: config.json in the app folder; see `grain config`)"
    )
    ap.add_argument("--fake", action="store_true", help="use a tiny non-AI test embedder (separate index)")
    ap.add_argument("-n", type=int, default=10, help="results per section (default 10)")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("index", help="index / update your folders")
    p.add_argument("--only", choices=["text", "images"], help="index only documents or only images")
    p.add_argument("--retry-errors", action="store_true", help="try files that failed last time again")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("search", aliases=["s"], help='search, e.g. grain search "dog on a red sofa"')
    p.add_argument("query", nargs="+")
    p.add_argument("-n", type=int, default=10, help="results per section")
    p.add_argument("--open", action="store_true", help="open the top hit straight away")
    p.add_argument("--exact", action="store_true", help="only files that contain the text (same as starting with =)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("open", aliases=["o"], help="open result N from the last search")
    p.add_argument("n", type=int)
    p.add_argument("--reveal", "-r", action="store_true", help="show in Explorer instead of opening")
    p.set_defaults(func=cmd_open)

    sub.add_parser("status", help="what is indexed").set_defaults(func=cmd_status)

    p = sub.add_parser("serve", help="run the background search service for the Grain Search window")
    p.add_argument("--port", type=int, help="default from the settings (8766)")
    p.add_argument("--no-index", action="store_true", help="search only, no background indexing")
    p.add_argument("--no-watch", action="store_true", help="no live file watching")
    p.set_defaults(func=cmd_serve)

    sub.add_parser("config", help="show where settings and the index are kept").set_defaults(func=cmd_config)

    p = sub.add_parser("folders", help="list, add or remove the folders to index")
    p.add_argument("action", nargs="?", choices=["list", "add", "remove"])
    p.add_argument("paths", nargs="*", metavar="FOLDER")
    p.set_defaults(func=cmd_folders)

    p = sub.add_parser("exclude", help="list, add or remove exclusions (a folder, or a pattern like *.log)")
    p.add_argument("action", nargs="?", choices=["list", "add", "remove"])
    p.add_argument("items", nargs="*", metavar="FOLDER_OR_PATTERN")
    p.set_defaults(func=cmd_exclude)

    p = sub.add_parser("reset", help="delete the index (not your files)")
    p.add_argument("--yes", "-y", action="store_true")
    p.set_defaults(func=cmd_reset)

    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as e:  # ValueError includes JSON syntax errors
        where = args.config or "config.json"
        print(f"Error: cannot read the settings file ({where}): {e}")
        print(dim("Fix the file, or delete it to start again with no folders."))
        return 2
    func = getattr(args, "func", cmd_interactive)
    try:
        return func(args, cfg)
    except RuntimeError as e:
        if os.environ.get("ES_DEBUG"):
            raise
        print(f"\nError: {e}")
        if e.__cause__ is not None:
            print(dim(f"Caused by: {type(e.__cause__).__name__}: {e.__cause__}"))
        print(dim("Set ES_DEBUG=1 to see the full traceback."))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
