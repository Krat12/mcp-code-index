"""Progress reporting for indexing + a cross-process status channel.

Two concerns, both deliberately light on CPU/RAM:

1. Live progress callbacks for `indexer.build_index(..., on_progress=...)`.
   Reporters implement ``__call__(done, total, current, phase)`` plus a small
   lifecycle (``start`` / ``finish`` / ``error``).

2. A status channel between processes: each indexer writes a tiny JSON file per
   service into ``CACHE_HOME/status/<id>.json`` (atomic replace). The ``status``
   command and the optional web UI read these files. This is how you can watch
   the background watcher's progress from another terminal without any sockets.

Everything lives OUTSIDE the indexed repos, like all other state.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Iterable, Protocol

from .config import STATUS_DIR


# ---------------------------------------------------------------------------
# Status file channel (one JSON per service id).
# ---------------------------------------------------------------------------


def _status_path(service_id: str) -> Path:
    return STATUS_DIR / f"{service_id}.json"


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically (temp file + os.replace) so readers never tear."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def read_status(service_id: str) -> dict | None:
    """Read one service's live status, or None if absent/unreadable."""
    p = _status_path(service_id)
    try:
        if not p.is_file():
            return None
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_all_status() -> dict[str, dict]:
    """Read every status file, keyed by service id (best effort)."""
    out: dict[str, dict] = {}
    try:
        entries = list(STATUS_DIR.glob("*.json"))
    except OSError:
        return out
    for p in entries:
        try:
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        out[p.stem] = data
    return out


def clear_status(service_id: str) -> None:
    try:
        _status_path(service_id).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Reporters.
# ---------------------------------------------------------------------------


class Reporter(Protocol):
    def start(self) -> None: ...
    def __call__(self, done: int, total: int, current: str, phase: str) -> None: ...
    def finish(self, report) -> None: ...
    def error(self, exc: BaseException) -> None: ...


class NullReporter:
    """Does nothing. Default for tests / the MCP server."""

    def start(self) -> None:
        pass

    def __call__(self, done: int, total: int, current: str, phase: str) -> None:
        pass

    def finish(self, report) -> None:
        pass

    def error(self, exc: BaseException) -> None:
        pass


class StatusFileReporter:
    """Persist live progress to ``CACHE_HOME/status/<id>.json`` (throttled)."""

    def __init__(self, service_id: str, name: str, path: str, min_interval: float = 0.3) -> None:
        self._id = service_id
        self._name = name
        self._path = path
        self._min_interval = min_interval
        self._last_write = 0.0
        self._started = time.time()
        self._state: dict = {
            "id": service_id,
            "name": name,
            "path": path,
            "phase": "idle",
            "done": 0,
            "total": 0,
            "current": "",
            "indexed": 0,
            "skipped": 0,
            "removed": 0,
            "symbols": 0,
            "semantic": None,
            "semantic_failures": 0,
            "semantic_embed_failures": 0,
            "pid": os.getpid(),
            "started_at": self._started,
            "updated_at": self._started,
            "finished_at": None,
            "error": None,
        }

    def _flush(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last_write) < self._min_interval:
            return
        self._state["updated_at"] = now
        _atomic_write_json(_status_path(self._id), self._state)
        self._last_write = now

    def start(self) -> None:
        self._started = time.time()
        self._state.update(
            phase="scanning",
            started_at=self._started,
            finished_at=None,
            error=None,
            done=0,
            total=0,
            current="",
        )
        self._flush(force=True)

    def __call__(self, done: int, total: int, current: str, phase: str) -> None:
        # Always persist phase transitions immediately; throttle within a phase.
        phase_changed = phase != self._state.get("phase")
        self._state.update(done=done, total=total, current=current, phase=phase)
        self._flush(force=phase_changed or phase in ("scanning", "removing", "done"))

    def finish(self, report) -> None:
        self._state.update(
            phase="done",
            done=self._state.get("total", 0),
            current="",
            indexed=getattr(report, "indexed", 0),
            skipped=getattr(report, "skipped", 0),
            removed=getattr(report, "removed", 0),
            symbols=getattr(report, "symbols", 0),
            semantic=getattr(report, "semantic_enabled", None),
            semantic_failures=getattr(report, "semantic_failures", 0),
            semantic_embed_failures=getattr(report, "semantic_embed_failures", 0),
            finished_at=time.time(),
        )
        self._flush(force=True)

    def error(self, exc: BaseException) -> None:
        self._state.update(
            phase="error",
            error=f"{type(exc).__name__}: {exc}",
            finished_at=time.time(),
        )
        self._flush(force=True)


class RichReporter:
    """Update one task of an externally-managed ``rich.progress.Progress``.

    The CLI owns the Progress (so it can show several services), creating a task
    per service and handing its id here. CPU cost is negligible (rich refreshes
    a few times per second), which suits the low-power machine.
    """

    def __init__(self, progress, task_id, name: str) -> None:
        self._progress = progress
        self._task = task_id
        self._name = name

    def start(self) -> None:
        self._progress.update(self._task, description=f"[cyan]{self._name}[/] scanning")

    def __call__(self, done: int, total: int, current: str, phase: str) -> None:
        if total and self._progress.tasks:
            self._progress.update(self._task, total=total, completed=done)
        desc = f"[cyan]{self._name}[/] {phase}"
        self._progress.update(self._task, description=desc)

    def finish(self, report) -> None:
        total = getattr(report, "indexed", 0) + getattr(report, "skipped", 0)
        self._progress.update(
            self._task,
            description=f"[green]{self._name}[/] done "
            f"(indexed={getattr(report, 'indexed', 0)}, removed={getattr(report, 'removed', 0)})",
        )

    def error(self, exc: BaseException) -> None:
        self._progress.update(self._task, description=f"[red]{self._name}[/] error: {exc}")


class MultiReporter:
    """Fan-out a single ``on_progress`` callback to several reporters."""

    def __init__(self, reporters: Iterable[Reporter]) -> None:
        self._reporters = [r for r in reporters if r is not None]

    def start(self) -> None:
        for r in self._reporters:
            try:
                r.start()
            except Exception:
                pass

    def __call__(self, done: int, total: int, current: str, phase: str) -> None:
        for r in self._reporters:
            try:
                r(done, total, current, phase)
            except Exception:
                pass

    def finish(self, report) -> None:
        for r in self._reporters:
            try:
                r.finish(report)
            except Exception:
                pass

    def error(self, exc: BaseException) -> None:
        for r in self._reporters:
            try:
                r.error(exc)
            except Exception:
                pass
