"""Command-line entrypoint for managing the registry and building indexes.

Single project:
    code-index index [--path DIR] [--full] [--plain]   # index CWD or a dir
    code-index index --background [--path DIR] [--full] # detach (for git hooks)
    code-index stats [--path DIR]

Multi-service (microservices) via the external registry:
    code-index add <path> [--name NAME]      # register one service
    code-index add-workspace <path> [--depth N]   # auto-discover git repos
    code-index list                          # show registered services
    code-index index-all [--full] [--plain]  # (re)index every service
    code-index stats-all
    code-index status [--watch]              # live indexing status dashboard
    code-index web [--host H] [--port P]     # tiny local status web UI
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from .config import load_settings, project_id, settings_for
from .indexer import build_index, is_reindex_active, run_index
from .progress import (
    MultiReporter,
    NullReporter,
    RichReporter,
    StatusFileReporter,
    read_status,
)
from .registry import Service, add_service, add_workspace, load_registry
from .semantic import SemanticIndex
from .store import Store
from .walker import read_span as _read_span


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def _rich_available() -> bool:
    try:
        import rich  # noqa: F401

        return True
    except Exception:
        return False


def _use_rich(plain: bool) -> bool:
    """Use the rich UI only for an interactive TTY with rich installed."""
    if plain:
        return False
    if not _rich_available():
        return False
    try:
        return sys.stderr.isatty()
    except Exception:
        return False


def _settings_and_meta(root: Path):
    """Resolve Settings for a bare path, honoring any registered ignore config."""
    root = root.resolve()
    for s in load_registry():
        if s.path.resolve() == root:
            return s.settings(), s.id, s.name
    return settings_for(root), project_id(root), root.name


def _resolve_settings_for_cli(service: str | None = None, path: str | None = None):
    """Resolve one service for CLI search/read commands."""
    if service and path:
        raise ValueError("Use either --service or --path, not both")
    if service:
        for s in load_registry():
            if service in (s.id, s.name):
                return s.settings(), s.id, s.name
        raise ValueError(f"Unknown service '{service}'. Run `code-index list`.")
    if path:
        return _settings_and_meta(Path(path))
    settings = load_settings()
    return settings, project_id(settings.root), settings.root.name


def _as_glob_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return parts or None


def _cmd_services() -> int:
    services = load_registry()
    if not services:
        print("No services registered. Use `code-index add <path>` or `code-index add-workspace <parent>`.")
        return 0
    for s in services:
        print(f"{s.name}\t(id={s.id})\t{s.path}")
    return 0


def _cmd_search_text(args) -> int:
    settings, _, _ = _resolve_settings_for_cli(args.service, args.path)
    store = Store(settings.db_path, busy_timeout_ms=int(settings.sqlite_read_timeout * 1000))
    try:
        hits = store.search_text(
            args.query,
            limit=args.limit,
            path_glob=_as_glob_list(args.path_glob),
            exclude_glob=_as_glob_list(args.exclude_glob),
        )
    finally:
        store.close()
    if not hits:
        print(f"No text matches for: {args.query}")
        return 0
    for h in hits:
        print(f"{h.path}:{h.line}: {h.content.strip()}")
    return 0


def _cmd_search_symbol(args) -> int:
    settings, _, _ = _resolve_settings_for_cli(args.service, args.path)
    store = Store(settings.db_path, busy_timeout_ms=int(settings.sqlite_read_timeout * 1000))
    try:
        hits = store.search_symbol(
            args.name,
            limit=args.limit,
            exact=args.exact,
            path_glob=_as_glob_list(args.path_glob),
            exclude_glob=_as_glob_list(args.exclude_glob),
        )
    finally:
        store.close()
    if not hits:
        print(f"No symbols matching: {args.name}")
        return 0
    for h in hits:
        print(f"{h.kind} {h.name}  ->  {h.path}:{h.start_line}-{h.end_line}")
    return 0


def _cmd_file_symbols(args) -> int:
    settings, _, _ = _resolve_settings_for_cli(args.service, args.path_root)
    store = Store(settings.db_path, busy_timeout_ms=int(settings.sqlite_read_timeout * 1000))
    try:
        hits = store.file_symbols(args.file)
    finally:
        store.close()
    if not hits:
        print(f"No symbols indexed for: {args.file}")
        return 0
    for h in hits:
        print(f"L{h.start_line}-{h.end_line}\t{h.kind}\t{h.name}")
    return 0


def _cmd_read_span(args) -> int:
    settings, _, _ = _resolve_settings_for_cli(args.service, args.path_root)
    start = max(1, args.start_line)
    end = max(start, args.end_line)
    text = _read_span(settings.root, args.file, start, end, context=args.context)
    if text is not None:
        if text == "":
            print(f"{args.file}:{start}-{end} is empty or out of range.")
        else:
            lo = max(1, start - max(0, args.context))
            print(f"{args.file}:{lo}-{end + max(0, args.context)}")
            print(text)
        return 0

    lo = max(1, start - max(0, args.context))
    hi = end + max(0, args.context)
    store = Store(settings.db_path, busy_timeout_ms=int(settings.sqlite_read_timeout * 1000))
    try:
        rows = store.get_lines(args.file, lo, hi)
    finally:
        store.close()
    if not rows:
        print(f"Could not read {args.file}:{start}-{end} (not on disk or in index).")
        return 0
    print(f"{args.file}:{lo}-{hi} (from index)")
    print("\n".join(r.content for r in rows))
    return 0


def _cmd_search_semantic(args) -> int:
    settings, _, _ = _resolve_settings_for_cli(args.service, args.path)
    if not settings.semantic_enabled:
        print("Semantic search is DISABLED for this service (CODE_INDEX_SEMANTIC=0).")
        return 0
    sem = SemanticIndex(settings)
    if not sem.available:
        print("Semantic search is UNAVAILABLE (embeddings API or Qdrant could not be reached).")
        return 0
    hits = sem.search(
        args.query,
        limit=args.limit,
        path_glob=_as_glob_list(args.path_glob),
        exclude_glob=_as_glob_list(args.exclude_glob),
    )
    if getattr(sem, "last_search_failed", False):
        print(f"Semantic search FAILED ({getattr(sem, 'last_error', 'unknown')}).")
        return 0
    if not hits:
        print(f"No semantic matches for: {args.query}")
        return 0
    for h in hits:
        print(f"[{h.score:.3f}] {h.path}:{h.start_line}-{h.end_line}")
        print(h.preview)
        print()
    return 0


def _cmd_search_hybrid(args) -> int:
    settings, _, _ = _resolve_settings_for_cli(args.service, args.path)
    inc = _as_glob_list(args.path_glob)
    exc = _as_glob_list(args.exclude_glob)
    store = Store(settings.db_path, busy_timeout_ms=int(settings.sqlite_read_timeout * 1000))
    try:
        syms = store.search_symbol(args.query, limit=args.limit, path_glob=inc, exclude_glob=exc)
        texts = store.search_text(args.query, limit=args.limit, path_glob=inc, exclude_glob=exc)
    finally:
        store.close()

    printed = False
    if syms:
        printed = True
        print("## Symbols")
        for s in syms:
            print(f"{s.kind} {s.name} -> {s.path}:{s.start_line}")
    if texts:
        printed = True
        print("\n## Text" if printed else "## Text")
        for t in texts:
            print(f"{t.path}:{t.line}: {t.content.strip()}")
    if settings.semantic_enabled:
        sem = SemanticIndex(settings)
        if sem.available:
            sem_hits = sem.search(args.query, limit=args.limit, path_glob=inc, exclude_glob=exc)
            if sem_hits:
                printed = True
                print("\n## Semantic" if printed else "## Semantic")
                for h in sem_hits:
                    print(f"[{h.score:.3f}] {h.path}:{h.start_line}-{h.end_line}")
        else:
            print("\n## Semantic\n(unavailable — text+symbols above are unaffected)")
    if not printed:
        print(f"No matches for: {args.query}")
    return 0


def _add_search_common(p, default_limit: int = 30) -> None:
    p.add_argument("--service", default=None, help="registered service name or id")
    p.add_argument("--path", default=None, help="project root instead of a registered service")
    p.add_argument("--limit", type=int, default=default_limit, help="max results")
    p.add_argument("--path-glob", default=None, help="include glob(s), comma-separated")
    p.add_argument("--exclude-glob", default=None, help="exclude glob(s), comma-separated")


def _index_plain(settings, sid: str, name: str, full: bool) -> None:
    _eprint(f"root: {settings.root}")
    _eprint(f"db:   {settings.db_path}")
    reporter = StatusFileReporter(sid, name, str(settings.root))
    report = run_index(settings, full=full, reporter=reporter, log=_eprint)
    _eprint(
        f"done: indexed={report.indexed} skipped={report.skipped} removed={report.removed} "
        f"symbols={report.symbols} semantic_files={report.semantic_files} "
        f"semantic={'on' if report.semantic_enabled else 'off'}"
        + (f" semantic_failures={report.semantic_failures}" if report.semantic_failures else "")
    )


def _index_one(root: Path, full: bool, plain: bool) -> None:
    settings, sid, name = _settings_and_meta(root)
    if not _use_rich(plain):
        _index_plain(settings, sid, name, full)
        return
    _index_with_rich([(settings, sid, name)], full)


def _index_background(root: Path, full: bool) -> int:
    """Spawn a detached child that reindexes, and return immediately.

    For git commit/push hooks: a hook must not block while the index rebuilds.
    Unlike the MCP server (a long-lived process that can hold a daemon thread),
    a CLI invocation is short-lived, so the work runs in a fully detached child
    process (`code-index index --plain`) that outlives this one. Idempotent per
    service: if `status.json` shows a reindex already active, we skip spawning.
    """
    settings, sid, name = _settings_and_meta(root)
    phase = (read_status(sid) or {}).get("phase")
    if is_reindex_active(sid, phase):
        _eprint(f"reindex already running (service={name}, phase={phase}); not starting another")
        return 0

    cmd = [sys.executable, "-m", "code_index.cli", "index", "--plain", "--path", str(settings.root)]
    if full:
        cmd.append("--full")

    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        # Detach from the console so the child survives the hook/terminal exit.
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        )
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen(cmd, **kwargs)
    _eprint(f"reindex started in background (service={name}, full={full}); poll `code-index status`")
    return 0


def _index_with_rich(targets: list[tuple], full: bool) -> None:
    """Index a list of (settings, id, name) with a live rich progress bar."""
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    console = Console(stderr=True)
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        refresh_per_second=4,
        transient=False,
    ) as progress:
        for settings, sid, name in targets:
            task = progress.add_task(f"[cyan]{name}[/] waiting", total=None)
            reporter = MultiReporter(
                [
                    RichReporter(progress, task, name),
                    StatusFileReporter(sid, name, str(settings.root)),
                ]
            )
            try:
                run_index(settings, full=full, reporter=reporter, log=lambda m: None)
            except Exception as exc:  # keep going across services
                progress.update(task, description=f"[red]{name}[/] error: {exc}")


def _cmd_status(watch: bool) -> int:
    """Show a live status table for all registered services."""
    if not _rich_available():
        return _status_plain()
    from rich.console import Console
    from rich.live import Live

    console = Console()
    if not watch:
        console.print(_status_table())
        return 0
    try:
        with Live(_status_table(), console=console, refresh_per_second=2, screen=False) as live:
            import time

            while True:
                time.sleep(1.0)
                live.update(_status_table())
    except KeyboardInterrupt:
        return 0


def _status_table():
    from rich.table import Table

    from .progress import read_status
    from .store import Store as _Store

    services = load_registry()
    table = Table(title="code-index status", expand=False)
    table.add_column("service", style="cyan", no_wrap=True)
    table.add_column("phase")
    table.add_column("progress", justify="right")
    table.add_column("files", justify="right")
    table.add_column("symbols", justify="right")
    table.add_column("current", overflow="ellipsis", max_width=48)

    if not services:
        table.add_row("(none)", "-", "-", "-", "-", "register with `code-index add`")
        return table

    for s in services:
        st = read_status(s.id) or {}
        phase = st.get("phase", "idle")
        done, total = st.get("done", 0), st.get("total", 0)
        if total:
            pct = f"{(done / total * 100):.0f}% ({done}/{total})"
        else:
            pct = "-"
        try:
            store = _Store(s.settings().db_path)
            stats = store.stats()
            store.close()
            files, syms = stats.get("files", 0), stats.get("symbols", 0)
        except Exception:
            files, syms = "?", "?"
        phase_style = {
            "indexing": "yellow",
            "scanning": "yellow",
            "removing": "yellow",
            "done": "green",
            "idle": "dim",
            "error": "red",
        }.get(phase, "white")
        lost = (st.get("semantic_embed_failures", 0) or 0) + (st.get("semantic_failures", 0) or 0)
        phase_cell = f"[{phase_style}]{phase}[/]"
        if lost:
            phase_cell += f" [yellow]\u26a0 {lost} sem lost[/]"
        table.add_row(
            s.name,
            phase_cell,
            pct,
            str(files),
            str(syms),
            st.get("current", "") or "",
        )
    return table


def _status_plain() -> int:
    from .progress import read_status

    services = load_registry()
    if not services:
        print("no services registered.")
        return 0
    for s in services:
        st = read_status(s.id) or {}
        phase = st.get("phase", "idle")
        done, total = st.get("done", 0), st.get("total", 0)
        pct = f"{done}/{total}" if total else "-"
        print(f"{s.name}\t{phase}\t{pct}\t{st.get('current', '')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-index", description="Hybrid code index (SQLite + tree-sitter + Qdrant)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="build/refresh the index for one project")
    p_index.add_argument("--path", default=None, help="project root (default: CWD / CODE_INDEX_ROOT)")
    p_index.add_argument("--full", action="store_true", help="force full re-index")
    p_index.add_argument("--plain", action="store_true", help="disable the rich progress bar")
    p_index.add_argument(
        "--background",
        action="store_true",
        help="start the reindex detached and return at once (for git commit/push "
        "hooks); idempotent per service. Poll `code-index status` for progress.",
    )

    p_stats = sub.add_parser("stats", help="show index statistics for one project")
    p_stats.add_argument("--path", default=None, help="project root (default: CWD / CODE_INDEX_ROOT)")

    p_add = sub.add_parser("add", help="register a service in the external registry")
    p_add.add_argument("path", help="service repo path")
    p_add.add_argument("--name", default=None, help="friendly name (default: dir name)")

    p_ws = sub.add_parser("add-workspace", help="register a workspace (auto-discovers git repos)")
    p_ws.add_argument("path", help="parent folder containing service repos")
    p_ws.add_argument("--depth", type=int, default=1, help="scan depth for git repos (default 1)")

    sub.add_parser("list", help="list registered services")

    p_all = sub.add_parser("index-all", help="(re)index every registered service")
    p_all.add_argument("--full", action="store_true", help="force full re-index")
    p_all.add_argument("--plain", action="store_true", help="disable the rich progress bar")

    sub.add_parser("stats-all", help="show stats for every registered service")

    p_status = sub.add_parser("status", help="show live indexing status for all services")
    p_status.add_argument("--watch", action="store_true", help="continuously refresh (Ctrl+C to stop)")

    p_web = sub.add_parser("web", help="serve a tiny local status web UI")
    p_web.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    p_web.add_argument("--port", type=int, default=8765, help="bind port (default 8765)")

    sub.add_parser("services", help="list services in MCP-friendly format")

    p_st = sub.add_parser("search-text", help="CLI fallback for MCP search_text")
    p_st.add_argument("query")
    _add_search_common(p_st)

    p_ss = sub.add_parser("search-symbol", help="CLI fallback for MCP search_symbol")
    p_ss.add_argument("name")
    p_ss.add_argument("--exact", action="store_true", help="exact symbol name match")
    _add_search_common(p_ss)

    p_fs = sub.add_parser("file-symbols", help="CLI fallback for MCP file_symbols")
    p_fs.add_argument("file", help="repo-relative file path")
    p_fs.add_argument("--service", default=None, help="registered service name or id")
    p_fs.add_argument("--path-root", default=None, help="project root instead of a registered service")

    p_rs = sub.add_parser("read-span", help="CLI fallback for MCP read_span")
    p_rs.add_argument("file", help="repo-relative file path")
    p_rs.add_argument("start_line", type=int)
    p_rs.add_argument("end_line", type=int)
    p_rs.add_argument("--context", type=int, default=0)
    p_rs.add_argument("--service", default=None, help="registered service name or id")
    p_rs.add_argument("--path-root", default=None, help="project root instead of a registered service")

    p_sem = sub.add_parser("search-semantic", help="CLI fallback for MCP search_semantic")
    p_sem.add_argument("query")
    _add_search_common(p_sem, default_limit=10)

    p_hybrid = sub.add_parser("search-hybrid", help="CLI fallback for MCP search_hybrid")
    p_hybrid.add_argument("query")
    _add_search_common(p_hybrid, default_limit=8)

    args = parser.parse_args(argv)

    if args.cmd == "index":
        root = Path(args.path).resolve() if args.path else load_settings().root
        if args.background:
            return _index_background(root, full=args.full)
        _index_one(root, full=args.full, plain=args.plain)
        return 0

    if args.cmd == "stats":
        settings = settings_for(args.path) if args.path else load_settings()
        store = Store(settings.db_path)
        print(store.stats())
        store.close()
        return 0

    if args.cmd == "add":
        svc = add_service(args.path, name=args.name)
        _eprint(f"registered: {svc.name} -> {svc.path}  (id={svc.id})")
        return 0

    if args.cmd == "add-workspace":
        p = add_workspace(args.path, depth=args.depth)
        found = load_registry()
        _eprint(f"workspace added: {p} (depth={args.depth}); now {len(found)} service(s) resolve")
        return 0

    if args.cmd == "list":
        services = load_registry()
        if not services:
            _eprint("no services registered. Use: code-index add <path>  or  add-workspace <path>")
            return 0
        for s in services:
            extra = ""
            if s.ignore or s.use_gitignore:
                extra = f"  [ignore={len(s.ignore)} gitignore={'on' if s.use_gitignore else 'off'}]"
            print(f"{s.name}\t{s.path}\t(id={s.id}){extra}")
        return 0

    if args.cmd == "index-all":
        services = load_registry()
        if not services:
            _eprint("no services registered.")
            return 1
        if _use_rich(args.plain):
            targets = [(s.settings(), s.id, s.name) for s in services]
            _index_with_rich(targets, full=args.full)
        else:
            for s in services:
                _eprint(f"=== {s.name} ===")
                _index_plain(s.settings(), s.id, s.name, full=args.full)
        return 0

    if args.cmd == "stats-all":
        for s in load_registry():
            store = Store(s.settings().db_path)
            print(f"{s.name}: {store.stats()}")
            store.close()
        return 0

    if args.cmd == "status":
        return _cmd_status(watch=args.watch)

    if args.cmd == "web":
        from .webui import serve

        return serve(host=args.host, port=args.port)

    try:
        if args.cmd == "services":
            return _cmd_services()
        if args.cmd == "search-text":
            return _cmd_search_text(args)
        if args.cmd == "search-symbol":
            return _cmd_search_symbol(args)
        if args.cmd == "file-symbols":
            return _cmd_file_symbols(args)
        if args.cmd == "read-span":
            return _cmd_read_span(args)
        if args.cmd == "search-semantic":
            return _cmd_search_semantic(args)
        if args.cmd == "search-hybrid":
            return _cmd_search_hybrid(args)
    except ValueError as exc:
        _eprint(str(exc))
        return 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
