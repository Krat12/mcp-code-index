"""Background auto-reindex daemon for all registered services.

Two complementary triggers, both running OUTSIDE the service repos (no git
hooks, no committed files):

1. Filesystem watcher (watchdog): on any relevant file change, the owning
   service is scheduled for an incremental re-index after a short debounce.
2. Periodic timer: every N seconds every service is incrementally re-indexed,
   as a safety net for events the watcher might miss (network drives, etc.).

Run it once and leave it running:

    code-index-watch                 # uses ~/.config/code-index/projects.toml
    code-index-watch --interval 600  # periodic sweep every 10 minutes
    code-index-watch --no-periodic   # rely on filesystem events only
"""

from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

from .config import DEFAULT_IGNORE_DIRS, DEFAULT_TEXT_EXTS
from .indexer import run_index
from .progress import StatusFileReporter
from .registry import Service, load_registry


def _log(msg: str) -> None:
    print(f"[watch] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def _relevant(path: str) -> bool:
    """Only react to files we'd actually index, and skip ignored dirs."""
    p = Path(path)
    parts = set(p.parts)
    if parts & DEFAULT_IGNORE_DIRS:
        return False
    ext = p.suffix.lower()
    name = p.name.lower()
    return ext in DEFAULT_TEXT_EXTS or name in {"dockerfile", "makefile"}


class _Debouncer:
    """Coalesce bursts of FS events per service into a single re-index call."""

    def __init__(self, delay: float, run_fn) -> None:
        self._delay = delay
        self._run_fn = run_fn
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def schedule(self, service: Service) -> None:
        with self._lock:
            old = self._timers.get(service.id)
            if old is not None:
                old.cancel()
            t = threading.Timer(self._delay, self._fire, args=(service,))
            t.daemon = True
            self._timers[service.id] = t
            t.start()

    def _fire(self, service: Service) -> None:
        with self._lock:
            self._timers.pop(service.id, None)
        self._run_fn(service)


def _reindex_service(service: Service, lock: threading.Lock) -> None:
    """Incrementally re-index one service. Serialized to avoid Qdrant/SQLite races."""
    with lock:
        try:
            settings = service.settings()
            reporter = StatusFileReporter(service.id, service.name, str(service.path))
            report = run_index(settings, full=False, reporter=reporter)
            _log(
                f"reindexed '{service.name}': indexed={report.indexed} "
                f"removed={report.removed} (semantic={'on' if report.semantic_enabled else 'off'})"
            )
        except Exception as exc:  # keep the daemon alive on any single failure
            _log(f"ERROR reindexing '{service.name}': {exc!r}")


def _build_handler(service: Service, debouncer: "_Debouncer"):
    from watchdog.events import FileSystemEventHandler

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            if getattr(event, "is_directory", False):
                return
            src = getattr(event, "src_path", "") or ""
            dest = getattr(event, "dest_path", "") or ""
            if _relevant(src) or (dest and _relevant(dest)):
                debouncer.schedule(service)

    return _Handler()


def run_watch(interval: float, debounce: float, periodic: bool, registry_path: Path | None = None) -> int:
    from watchdog.observers import Observer

    services = load_registry(registry_path)
    if not services:
        _log("no services in registry. Add some with: code-index add <path>  (or add a [[workspace]])")
        return 1

    reindex_lock = threading.Lock()
    debouncer = _Debouncer(debounce, lambda svc: _reindex_service(svc, reindex_lock))

    _log(f"watching {len(services)} service(s):")
    for s in services:
        _log(f"  - {s.name}  ({s.path})")

    # Initial incremental index so the watcher starts from a fresh baseline.
    for s in services:
        _reindex_service(s, reindex_lock)

    observer = Observer()
    for s in services:
        if s.path.exists():
            observer.schedule(_build_handler(s, debouncer), str(s.path), recursive=True)
    observer.start()
    _log("filesystem watcher started")

    stop = threading.Event()

    def _periodic_loop() -> None:
        while not stop.wait(interval):
            for s in services:
                _reindex_service(s, reindex_lock)

    if periodic:
        t = threading.Thread(target=_periodic_loop, daemon=True)
        t.start()
        _log(f"periodic sweep every {interval:.0f}s enabled")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        _log("stopping...")
        stop.set()
        observer.stop()
        observer.join(timeout=5)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="code-index-watch",
        description="Background auto re-index daemon for all registered services",
    )
    parser.add_argument("--interval", type=float, default=600.0, help="periodic sweep seconds (default 600)")
    parser.add_argument("--debounce", type=float, default=3.0, help="debounce seconds after a change (default 3)")
    parser.add_argument("--no-periodic", action="store_true", help="disable the periodic sweep, FS events only")
    args = parser.parse_args(argv)
    return run_watch(interval=args.interval, debounce=args.debounce, periodic=not args.no_periodic)


if __name__ == "__main__":
    raise SystemExit(main())
