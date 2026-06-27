"""MCP server exposing the hybrid search tools to Kilo CLI / OpenCode.

Tools:
    search_text     - exact / FTS5 full-text search with line numbers
    search_symbol   - find definitions (functions/classes/...) by name
    file_symbols    - list all symbols (outline) of one file
    search_semantic - fuzzy meaning search via Qdrant (if enabled)
    search_hybrid   - merge text + symbol + semantic, deduped & ranked
    list_services   - show indexable services (from the external registry)
    reindex         - rebuild the index on demand (blocks until done)
    reindex_background - start a reindex and return at once (fire-and-forget)
    index_stats     - counts

Multi-service (microservices):
    Every tool accepts an optional `service` argument (a registered service
    name or id). With no `service`, the "default" service is used:
      - CODE_INDEX_ROOT / CWD if it is itself indexed, else
      - the first service in the registry.
    This lets ONE MCP server serve all microservices; the agent picks the
    service per call (use list_services to discover them).

Runs over stdio (default MCP transport) so Kilo launches it as a local server.
"""

import json
import functools
import inspect
import logging
from logging.handlers import RotatingFileHandler
import os
import sqlite3
import threading
import time
from typing import Optional
import urllib.error
import urllib.request
from urllib.parse import quote
from uuid import uuid4

from mcp.server.fastmcp import FastMCP

from .config import LOG_DIR, Settings, load_settings
from .indexer import build_index, is_reindex_active, start_background_reindex
from .progress import read_status
from .registry import Service, load_registry
from .semantic import SemanticIndex, _port_is_open
from .store import Store
from .walker import read_span as _read_span

mcp = FastMCP("code-index")

# Default service (CWD/CODE_INDEX_ROOT based) for single-project usage.
_default_settings: Settings = load_settings()

# Per-service caches so one server can serve many microservices.
_stores: dict[tuple[str, int], Store] = {}
_semantics: dict[str, SemanticIndex | None] = {}
_lock = threading.Lock()
_log_lock = threading.Lock()
_logger: logging.Logger | None = None
_timer_factory = threading.Timer
_thread_factory = threading.Thread


def _setup_logger() -> logging.Logger:
    global _logger
    with _log_lock:
        if _logger is not None:
            return _logger
        logger = logging.getLogger("code_index.mcp")
        logger.propagate = False
        level_name = (_default_settings.log_level or "INFO").upper()
        logger.setLevel(getattr(logging, level_name, logging.INFO))
        for old in list(logger.handlers):
            logger.removeHandler(old)
            old.close()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            LOG_DIR / "mcp.log",
            maxBytes=_default_settings.log_max_bytes,
            backupCount=_default_settings.log_backups,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(threadName)s %(message)s"
        ))
        logger.addHandler(handler)
        _logger = logger
        return logger


def _short(value, limit: int = 200) -> str:
    text = repr(value)
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _log_tool_start(tool: str, service: Optional[str], **fields) -> tuple[str, float, threading.Timer | None]:
    call_id = uuid4().hex[:12]
    started = time.perf_counter()
    logger = _setup_logger()
    compact = " ".join(f"{k}={_short(v)}" for k, v in fields.items() if v is not None)
    logger.info("tool_start id=%s tool=%s service=%s %s", call_id, tool, service or "<default>", compact)

    settings = _resolve_settings(service)
    slow_after = float(getattr(settings, "slow_tool_seconds", 0) or 0)
    timer = None
    if slow_after > 0:
        def _slow() -> None:
            logger.warning(
                "tool_slow id=%s tool=%s service=%s elapsed_ms=%d thread=%s",
                call_id,
                tool,
                service or "<default>",
                int((time.perf_counter() - started) * 1000),
                threading.get_ident(),
            )
        timer = _timer_factory(slow_after, _slow)
        timer.daemon = True
        timer.start()
    return call_id, started, timer


def _log_tool_finish(call_id: str, tool: str, service: Optional[str], started: float, timer: threading.Timer | None) -> None:
    if timer is not None:
        timer.cancel()
    _setup_logger().info(
        "tool_finish id=%s tool=%s service=%s elapsed_ms=%d",
        call_id,
        tool,
        service or "<default>",
        int((time.perf_counter() - started) * 1000),
    )


def _log_tool_error(call_id: str, tool: str, service: Optional[str], started: float, timer: threading.Timer | None, exc: Exception) -> None:
    if timer is not None:
        timer.cancel()
    _setup_logger().exception(
        "tool_error id=%s tool=%s service=%s elapsed_ms=%d error=%s",
        call_id,
        tool,
        service or "<default>",
        int((time.perf_counter() - started) * 1000),
        f"{type(exc).__name__}: {exc}",
    )


def _run_tool(tool: str, service: Optional[str], fields: dict, fn):
    call_id, started, timer = _log_tool_start(tool, service, **fields)
    try:
        result = fn()
    except Exception as exc:
        _log_tool_error(call_id, tool, service, started, timer, exc)
        raise
    _log_tool_finish(call_id, tool, service, started, timer)
    return result


def _trace_tool(tool: str, fields: tuple[str, ...] = ()):
    """Decorator for MCP tool diagnostics while preserving function signatures."""
    def _decorator(func):
        sig = inspect.signature(func)

        @functools.wraps(func)
        def _wrapper(*args, **kwargs):
            bound = sig.bind_partial(*args, **kwargs)
            service = bound.arguments.get("service")
            log_fields = {name: bound.arguments.get(name) for name in fields}
            return _run_tool(tool, service, log_fields, lambda: func(*args, **kwargs))

        return _wrapper

    return _decorator


def _log_event(level: int, message: str, **fields) -> None:
    compact = " ".join(f"{k}={_short(v)}" for k, v in fields.items())
    _setup_logger().log(level, "%s %s", message, compact)


def _resolve_settings(service: str | None) -> Settings:
    """Map an optional service name/id to its Settings.

    None -> default (CWD). Otherwise match a registered service by id or name.
    """
    if not service:
        return _default_settings
    for s in load_registry():
        if service in (s.id, s.name):
            return s.settings()
    raise ValueError(f"Unknown service '{service}'. Use list_services to see options.")


def _key(settings: Settings) -> str:
    return str(settings.db_path)


def _service_identity(service: str | None) -> tuple[str, str]:
    """Resolve an optional service arg to its (id, display name).

    Mirrors `_resolve_settings`: None -> the default (CWD) service. The id keys
    the status file and the in-flight job guard; the name is for display.
    """
    if service:
        for s in load_registry():
            if service in (s.id, s.name):
                return s.id, s.name
        raise ValueError(f"Unknown service '{service}'. Use list_services to see options.")
    from .config import project_id
    root = _default_settings.root
    return project_id(root), root.name


def _get_store(service: Optional[str] = None) -> Store:
    settings = _resolve_settings(service)
    # One sqlite3 connection must not be shared by parallel MCP worker threads.
    # Cache per (service DB, thread) and keep a short read timeout so a concurrent
    # reindex/watcher returns a visible busy message instead of an MCP timeout.
    k = (_key(settings), threading.get_ident())
    with _lock:
        st = _stores.get(k)
        if st is None:
            st = Store(
                settings.db_path,
                busy_timeout_ms=int(float(getattr(settings, "sqlite_read_timeout", 0.75)) * 1000),
            )
            _stores[k] = st
        return st


def _is_sqlite_busy(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "database is locked" in msg or "database table is locked" in msg or "database is busy" in msg


def _busy_response() -> str:
    _log_event(logging.WARNING, "sqlite_busy")
    return (
        "Index database is busy (likely a concurrent reindex/watch update). "
        "This is transient; retry this tool shortly."
    )


def _get_semantic(service: Optional[str] = None) -> SemanticIndex | None:
    settings = _resolve_settings(service)
    if not settings.semantic_enabled:
        return None
    k = _key(settings)
    # Fast path: return a cached entry under the lock.
    with _lock:
        if k in _semantics:
            return _semantics[k]
    # Slow path: build the SemanticIndex OUTSIDE the lock. Its constructor opens
    # a Qdrant client (a network round-trip that can stall on a cold start);
    # holding the global lock here would block every other tool — even
    # SQLite-only ones like search_text/read_span — turning one slow semantic
    # init into a whole-server hang. Build first, then store under the lock.
    sem = SemanticIndex(settings)
    with _lock:
        # Another thread may have built it while we were constructing; if so,
        # keep the existing entry and drop ours.
        if k not in _semantics:
            _semantics[k] = sem if sem.available else None
        return _semantics[k]


def _semantic_status(service: Optional[str]) -> tuple[Optional[SemanticIndex], str]:
    """Resolve the semantic layer AND a human/agent-facing reason for its state.

    Returns (index_or_None, reason) where reason is one of:
      "ok"          - usable
      "disabled"    - turned off via CODE_INDEX_SEMANTIC=0 for this service
      "unavailable" - enabled but the embedder/Qdrant could not be reached
    This lets the search tools tell the agent "semantic is down" (degraded)
    instead of silently implying "no matches".
    """
    settings = _resolve_settings(service)
    if not settings.semantic_enabled:
        return None, "disabled"
    sem = _get_semantic(service)
    if sem is None:
        return None, "unavailable"
    return sem, "ok"


def _as_glob_list(value) -> list[str] | None:
    """Accept a comma-separated string or a list of globs; normalize to a list."""
    if value is None:
        return None
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
        return parts or None
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value if str(v).strip()]
        return parts or None
    return None


def _qdrant_status_fast(settings: Settings) -> dict:
    """Best-effort Qdrant collection count without constructing SemanticIndex.

    `index_stats` must be safe in agent startup paths. Creating SemanticIndex can
    initialize an embedder and a qdrant-client instance; this tiny stdlib HTTP
    probe has its own sub-second timeout and never raises.
    """
    timeout = float(getattr(settings, "qdrant_health_timeout", 0) or 0)
    collection = settings.collection_name()
    if timeout <= 0:
        return {"status": "not_checked", "collection": collection, "points": None, "error": None}

    url = settings.qdrant_url.rstrip("/") + "/collections/" + quote(collection, safe="") + "/points/count"
    payload = json.dumps({"exact": False}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if settings.qdrant_api_key:
        headers["api-key"] = settings.qdrant_api_key
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        points = ((body.get("result") or {}).get("count") if isinstance(body, dict) else None)
        return {"status": "ok", "collection": collection, "points": points, "error": None}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            _log_event(logging.INFO, "qdrant_health_missing_collection", collection=collection)
            return {"status": "unavailable", "collection": collection,
                    "points": 0, "error": "collection does not exist yet"}
        err = f"HTTPError {exc.code}: {exc.reason}"
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"

    if _port_is_open(settings.qdrant_url, timeout=min(0.2, timeout)):
        _log_event(logging.WARNING, "qdrant_health_warming_up", collection=collection, error=err)
        return {"status": "warming_up", "collection": collection, "points": None,
                "error": f"Qdrant not ready yet ({err})"}
    _log_event(logging.WARNING, "qdrant_health_unavailable", collection=collection, error=err)
    return {"status": "unavailable", "collection": collection, "points": None, "error": err}


@mcp.tool()
@_trace_tool("search_text", ("query", "limit", "path_glob", "exclude_glob"))
def search_text(
    query: str,
    limit: int = 30,
    service: Optional[str] = None,
    path_glob: Optional[str] = None,
    exclude_glob: Optional[str] = None,
) -> str:
    """Exact full-text search across a service (FTS5). Returns path:line: content.

    Use for known strings, identifiers, config keys, error messages. Supports
    FTS5 syntax, e.g. "foo AND bar", "exact phrase", prefix*.
    `service` selects which microservice index to search (name or id); omit for
    the default service.
    `path_glob`/`exclude_glob` narrow results by repo-relative path (globs;
    comma-separated or a list), e.g. path_glob="backend/**", exclude_glob="**/tests/**".
    """
    try:
        hits = _get_store(service).search_text(
            query, limit=limit,
            path_glob=_as_glob_list(path_glob), exclude_glob=_as_glob_list(exclude_glob),
        )
    except sqlite3.OperationalError as exc:
        if _is_sqlite_busy(exc):
            return _busy_response()
        raise
    if not hits:
        return f"No text matches for: {query}"
    return "\n".join(f"{h.path}:{h.line}: {h.content.strip()}" for h in hits)


@mcp.tool()
@_trace_tool("search_symbol", ("name", "limit", "exact", "path_glob", "exclude_glob"))
def search_symbol(
    name: str,
    limit: int = 30,
    exact: bool = False,
    service: Optional[str] = None,
    path_glob: Optional[str] = None,
    exclude_glob: Optional[str] = None,
) -> str:
    """Find symbol definitions (function/class/method/record/...) by name.

    Use to jump to where something is DEFINED. Set exact=true for an exact name
    match, otherwise substring matching is used. `service` selects the index.
    `path_glob`/`exclude_glob` narrow results by repo-relative path (globs).
    """
    try:
        hits = _get_store(service).search_symbol(
            name, limit=limit, exact=exact,
            path_glob=_as_glob_list(path_glob), exclude_glob=_as_glob_list(exclude_glob),
        )
    except sqlite3.OperationalError as exc:
        if _is_sqlite_busy(exc):
            return _busy_response()
        raise
    if not hits:
        return f"No symbols matching: {name}"
    return "\n".join(
        f"{h.kind} {h.name}  ->  {h.path}:{h.start_line}-{h.end_line}" for h in hits
    )


@mcp.tool()
@_trace_tool("file_symbols", ("path",))
def file_symbols(path: str, service: Optional[str] = None) -> str:
    """List all symbols (outline) of a single file, ordered by line."""
    try:
        hits = _get_store(service).file_symbols(path)
    except sqlite3.OperationalError as exc:
        if _is_sqlite_busy(exc):
            return _busy_response()
        raise
    if not hits:
        return f"No symbols indexed for: {path}"
    return "\n".join(f"L{h.start_line}-{h.end_line}\t{h.kind}\t{h.name}" for h in hits)


@mcp.tool()
@_trace_tool("read_span", ("path", "start_line", "end_line", "context"))
def read_span(
    path: str,
    start_line: int,
    end_line: int,
    context: int = 0,
    service: Optional[str] = None,
) -> str:
    """Read source lines [start_line, end_line] of an indexed file (1-based).

    The natural follow-up to the search tools: they return `path:line` locations,
    this returns the actual code there so you can read it without an external
    file tool. `context` adds N lines on each side. Reads the live file on disk
    and falls back to the stored index if the file is gone/changed. `path` must
    be a repo-relative path from a search result (it is confined to the repo).
    """
    if start_line < 1:
        start_line = 1
    if end_line < start_line:
        end_line = start_line
    settings = _resolve_settings(service)
    text = _read_span(settings.root, path, start_line, end_line, context=context)
    if text is None:
        # File unreadable/missing on disk -> serve the indexed copy instead.
        lo = max(1, start_line - max(0, context))
        hi = end_line + max(0, context)
        try:
            rows = _get_store(service).get_lines(path, lo, hi)
        except sqlite3.OperationalError as exc:
            if _is_sqlite_busy(exc):
                return _busy_response()
            raise
        if not rows:
            return f"Could not read {path}:{start_line}-{end_line} (not on disk or in index)."
        body = "\n".join(r.content for r in rows)
        return f"{path}:{lo}-{hi} (from index)\n{body}"
    if text == "":
        return f"{path}:{start_line}-{end_line} is empty or out of range."
    lo = max(1, start_line - max(0, context))
    return f"{path}:{lo}-{end_line + max(0, context)}\n{text}"


@mcp.tool()
@_trace_tool("search_semantic", ("query", "limit", "path_glob", "exclude_glob"))
def search_semantic(
    query: str,
    limit: int = 10,
    service: Optional[str] = None,
    path_glob: Optional[str] = None,
    exclude_glob: Optional[str] = None,
) -> str:
    """Fuzzy meaning-based search (vector similarity via Qdrant).

    Use for conceptual queries like "where do we validate refunds" when you
    don't know exact identifiers. `service` selects the index.
    `path_glob`/`exclude_glob` narrow results by repo-relative path (globs).
    """
    sem, reason = _semantic_status(service)
    if reason == "disabled":
        return (
            "Semantic search is DISABLED for this service (CODE_INDEX_SEMANTIC=0). "
            "Use search_text/search_symbol instead — they are unaffected."
        )
    if reason == "unavailable":
        return (
            "Semantic search is UNAVAILABLE (the embeddings API or Qdrant could not "
            "be reached). This is a degraded state, NOT 'no results' — fall back to "
            "search_text/search_symbol, which still work."
        )
    hits = sem.search(
        query, limit=limit,
        path_glob=_as_glob_list(path_glob), exclude_glob=_as_glob_list(exclude_glob),
    )
    if getattr(sem, "last_search_failed", False):
        return (
            "Semantic search FAILED mid-request (embeddings API/Qdrant error: "
            f"{getattr(sem, 'last_error', 'unknown')}). Degraded, not empty — "
            "use search_text/search_symbol as a fallback."
        )
    if not hits:
        return f"No semantic matches for: {query}"
    out = []
    for h in hits:
        out.append(f"[{h.score:.3f}] {h.path}:{h.start_line}-{h.end_line}\n{h.preview}")
    return "\n\n".join(out)


@mcp.tool()
@_trace_tool("search_hybrid", ("query", "limit", "path_glob", "exclude_glob"))
def search_hybrid(
    query: str,
    limit: int = 8,
    service: Optional[str] = None,
    path_glob: Optional[str] = None,
    exclude_glob: Optional[str] = None,
) -> str:
    """Best-effort combined search: symbols + text + semantic for one service.

    Prefer this when you're not sure which layer fits. `service` selects the
    index (name or id); omit for the default service.
    `path_glob`/`exclude_glob` narrow results by repo-relative path (globs).
    """
    store = _get_store(service)
    inc = _as_glob_list(path_glob)
    exc = _as_glob_list(exclude_glob)
    sections: list[str] = []

    try:
        syms = store.search_symbol(query, limit=limit, path_glob=inc, exclude_glob=exc)
    except sqlite3.OperationalError as exc:
        if _is_sqlite_busy(exc):
            return _busy_response()
        raise
    if syms:
        sections.append(
            "## Symbols\n"
            + "\n".join(f"{s.kind} {s.name} -> {s.path}:{s.start_line}" for s in syms)
        )

    try:
        texts = store.search_text(query, limit=limit, path_glob=inc, exclude_glob=exc)
    except sqlite3.OperationalError as exc:
        if _is_sqlite_busy(exc):
            return _busy_response()
        raise
    if texts:
        sections.append(
            "## Text\n"
            + "\n".join(f"{t.path}:{t.line}: {t.content.strip()}" for t in texts)
        )

    sem, reason = _semantic_status(service)
    if reason == "unavailable":
        # Tell the agent the merged result is incomplete (don't hide degradation).
        sections.append("## Semantic\n(unavailable — embeddings API/Qdrant unreachable; "
                        "text+symbols above are unaffected)")
    elif sem is not None:
        sem_hits = sem.search(query, limit=limit, path_glob=inc, exclude_glob=exc)
        if getattr(sem, "last_search_failed", False):
            sections.append("## Semantic\n(failed mid-request; degraded, not empty)")
        elif sem_hits:
            sections.append(
                "## Semantic\n"
                + "\n".join(
                    f"[{h.score:.3f}] {h.path}:{h.start_line}-{h.end_line}" for h in sem_hits
                )
            )

    return "\n\n".join(sections) if sections else f"No matches for: {query}"


@mcp.tool()
@_trace_tool("list_services")
def list_services() -> str:
    """List indexable microservices from the external registry.

    Returns name, id and path for each. Pass a name or id as the `service`
    argument of the search tools to target a specific microservice.
    """
    services = load_registry()
    if not services:
        return (
            "No services registered. The default (CWD) service is used.\n"
            "Register services with the CLI: `code-index add <path>` or "
            "`code-index add-workspace <parent>`."
        )
    return "\n".join(f"{s.name}\t(id={s.id})\t{s.path}" for s in services)


@mcp.tool()
@_trace_tool("reindex", ("full",))
def reindex(full: bool = False, service: Optional[str] = None) -> str:
    """Rebuild the index from disk for one service. Use after large changes.

    full=true forces a complete re-index; otherwise only changed files are updated.
    """
    settings = _resolve_settings(service)
    k = _key(settings)
    with _lock:
        stale = [store_key for store_key in _stores if store_key[0] == k]
        for store_key in stale:
            st = _stores.pop(store_key, None)
            if st is not None:
                st.close()
    report = build_index(settings, full=full)
    return (
        f"indexed={report.indexed} skipped={report.skipped} removed={report.removed} "
        f"symbols={report.symbols} semantic_files={report.semantic_files} "
        f"semantic={'on' if report.semantic_enabled else 'off'}"
    )


@mcp.tool()
@_trace_tool("reindex_background", ("full",))
def reindex_background(full: bool = False, service: Optional[str] = None) -> str:
    """Start a reindex in the background and return immediately (don't wait).

    Fire-and-forget: kicks off an incremental (or full=true) rebuild in a daemon
    thread and returns at once, so a git commit/push hook or an agent never
    blocks on indexing. Poll `index_stats` (or the `status` CLI / web UI) to see
    progress; the result reflects in search a few seconds later.

    Idempotent per service: if a reindex is already running for this service
    (this server's own background job, a `code-index-watch` daemon, or a CLI
    `reindex --background` in another process), it does NOT start a second one
    and reports the current phase instead. Use plain `reindex` if you need to
    wait for completion and get the final counts.
    """
    settings = _resolve_settings(service)
    sid, name = _service_identity(service)
    phase = (read_status(sid) or {}).get("phase")

    # Drop cached read connections so the next search reopens after the writer.
    k = _key(settings)
    with _lock:
        stale = [store_key for store_key in _stores if store_key[0] == k]
        for store_key in stale:
            st = _stores.pop(store_key, None)
            if st is not None:
                st.close()

    started, reason = start_background_reindex(
        sid, name, settings, full=full, status_phase=phase,
        thread_factory=_thread_factory,
    )
    if started:
        return (
            f"reindex started in background (service={name}, full={full}). "
            f"Poll index_stats for progress; results land in search shortly."
        )
    return (
        f"reindex already running (service={name}, phase={phase or 'unknown'}); "
        f"not starting a second one. Poll index_stats for progress."
    )


@mcp.tool()
@_trace_tool("index_stats")
def index_stats(service: Optional[str] = None) -> str:
    """Show index status: file/symbol counts + the semantic layer's health.

    The semantic line tells you whether vector search is usable right now:
    ok (with point count), disabled, warming up (Qdrant still starting — retry
    shortly), or unavailable (degraded). The file/symbol counts come from SQLite
    and are returned immediately even when the semantic layer is slow/down, so
    this tool never hangs on a cold start.
    """
    try:
        stats = _get_store(service).stats()
    except sqlite3.OperationalError as exc:
        if _is_sqlite_busy(exc):
            return _busy_response()
        raise
    lines = [f"files={stats.get('files')} symbols={stats.get('symbols')}"]

    settings = _resolve_settings(service)
    if not settings.semantic_enabled:
        lines.append("semantic: disabled (CODE_INDEX_SEMANTIC=0)")
    else:
        h = _qdrant_status_fast(settings)
        status = h["status"]
        if status == "ok":
            lines.append(f"semantic: ok (collection={h['collection']}, points={h['points']})")
        elif status == "warming_up":
            lines.append(
                "semantic: warming up (Qdrant is starting — text/symbol search "
                "work now; retry semantic in a few seconds)"
            )
        elif status == "not_checked":
            lines.append("semantic: enabled (health not checked; CODE_INDEX_QDRANT_HEALTH_TIMEOUT=0)")
        else:
            lines.append(f"semantic: unavailable ({h['error']})")
    return "\n".join(lines)


def _startup_reindex() -> None:
    """Optional incremental re-index at startup (off by default).

    Enable with CODE_INDEX_REINDEX_ON_START=1. Runs in a background thread for
    the default service so the server stays responsive.
    """
    if os.environ.get("CODE_INDEX_REINDEX_ON_START", "0") in ("0", "false", "no"):
        return

    def _run() -> None:
        try:
            build_index(_default_settings, full=False)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


def _prewarm_semantic() -> None:
    """Build the default service's SemanticIndex in the background at startup.

    The first semantic call otherwise pays a one-time cost that NO timeout
    guards: `SemanticIndex.__init__` lazily does `import qdrant_client`, which
    drags in fastembed -> onnxruntime. On a true cold start (fresh boot / evicted
    file cache / AV scanning the native onnxruntime DLLs) that import alone can
    take ~60s and push the first `search_semantic`/`search_hybrid` past the MCP
    client's tool timeout (observed: first call 65s, the next identical one
    4.8s). Doing it here, off the request path, means the import+client are warm
    before any real query. Best-effort: never raises, never blocks startup, and
    is a no-op when semantic is disabled. Disable with CODE_INDEX_PREWARM=0.
    """
    if os.environ.get("CODE_INDEX_PREWARM", "1") in ("0", "false", "no"):
        return
    if not _default_settings.semantic_enabled:
        return

    def _run() -> None:
        started = time.perf_counter()
        try:
            _get_semantic(None)
        except Exception as exc:
            _log_event(logging.WARNING, "prewarm_semantic_failed",
                       error=f"{type(exc).__name__}: {exc}")
            return
        _log_event(logging.INFO, "prewarm_semantic_done",
                   elapsed_ms=int((time.perf_counter() - started) * 1000))

    _thread_factory(target=_run, daemon=True).start()


def main() -> None:
    _startup_reindex()
    _prewarm_semantic()
    # Default stdio transport: Kilo/OpenCode launch this as a local MCP server.
    mcp.run()


if __name__ == "__main__":
    main()
