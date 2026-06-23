"""MCP server exposing the hybrid search tools to Kilo CLI / OpenCode.

Tools:
    search_text     - exact / FTS5 full-text search with line numbers
    search_symbol   - find definitions (functions/classes/...) by name
    file_symbols    - list all symbols (outline) of one file
    search_semantic - fuzzy meaning search via Qdrant (if enabled)
    search_hybrid   - merge text + symbol + semantic, deduped & ranked
    list_services   - show indexable services (from the external registry)
    reindex         - rebuild the index on demand
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

import os
import threading
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .config import Settings, load_settings
from .indexer import build_index
from .registry import Service, load_registry
from .semantic import SemanticIndex
from .store import Store

mcp = FastMCP("code-index")

# Default service (CWD/CODE_INDEX_ROOT based) for single-project usage.
_default_settings: Settings = load_settings()

# Per-service caches so one server can serve many microservices.
_stores: dict[str, Store] = {}
_semantics: dict[str, SemanticIndex | None] = {}
_lock = threading.Lock()


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


def _get_store(service: Optional[str] = None) -> Store:
    settings = _resolve_settings(service)
    k = _key(settings)
    with _lock:
        st = _stores.get(k)
        if st is None:
            st = Store(settings.db_path)
            _stores[k] = st
        return st


def _get_semantic(service: Optional[str] = None) -> SemanticIndex | None:
    settings = _resolve_settings(service)
    if not settings.semantic_enabled:
        return None
    k = _key(settings)
    with _lock:
        if k not in _semantics:
            sem = SemanticIndex(settings)
            _semantics[k] = sem if sem.available else None
        return _semantics[k]


@mcp.tool()
def search_text(query: str, limit: int = 30, service: Optional[str] = None) -> str:
    """Exact full-text search across a service (FTS5). Returns path:line: content.

    Use for known strings, identifiers, config keys, error messages. Supports
    FTS5 syntax, e.g. "foo AND bar", "exact phrase", prefix*.
    `service` selects which microservice index to search (name or id); omit for
    the default service.
    """
    hits = _get_store(service).search_text(query, limit=limit)
    if not hits:
        return f"No text matches for: {query}"
    return "\n".join(f"{h.path}:{h.line}: {h.content.strip()}" for h in hits)


@mcp.tool()
def search_symbol(name: str, limit: int = 30, exact: bool = False, service: Optional[str] = None) -> str:
    """Find symbol definitions (function/class/method/record/...) by name.

    Use to jump to where something is DEFINED. Set exact=true for an exact name
    match, otherwise substring matching is used. `service` selects the index.
    """
    hits = _get_store(service).search_symbol(name, limit=limit, exact=exact)
    if not hits:
        return f"No symbols matching: {name}"
    return "\n".join(
        f"{h.kind} {h.name}  ->  {h.path}:{h.start_line}-{h.end_line}" for h in hits
    )


@mcp.tool()
def file_symbols(path: str, service: Optional[str] = None) -> str:
    """List all symbols (outline) of a single file, ordered by line."""
    hits = _get_store(service).file_symbols(path)
    if not hits:
        return f"No symbols indexed for: {path}"
    return "\n".join(f"L{h.start_line}-{h.end_line}\t{h.kind}\t{h.name}" for h in hits)


@mcp.tool()
def search_semantic(query: str, limit: int = 10, service: Optional[str] = None) -> str:
    """Fuzzy meaning-based search (vector similarity via Qdrant).

    Use for conceptual queries like "where do we validate refunds" when you
    don't know exact identifiers. `service` selects the index.
    """
    sem = _get_semantic(service)
    if sem is None:
        return "Semantic search is disabled (Qdrant/fastembed unavailable)."
    hits = sem.search(query, limit=limit)
    if not hits:
        return f"No semantic matches for: {query}"
    out = []
    for h in hits:
        out.append(f"[{h.score:.3f}] {h.path}:{h.start_line}-{h.end_line}\n{h.preview}")
    return "\n\n".join(out)


@mcp.tool()
def search_hybrid(query: str, limit: int = 8, service: Optional[str] = None) -> str:
    """Best-effort combined search: symbols + text + semantic for one service.

    Prefer this when you're not sure which layer fits. `service` selects the
    index (name or id); omit for the default service.
    """
    store = _get_store(service)
    sections: list[str] = []

    syms = store.search_symbol(query, limit=limit)
    if syms:
        sections.append(
            "## Symbols\n"
            + "\n".join(f"{s.kind} {s.name} -> {s.path}:{s.start_line}" for s in syms)
        )

    texts = store.search_text(query, limit=limit)
    if texts:
        sections.append(
            "## Text\n"
            + "\n".join(f"{t.path}:{t.line}: {t.content.strip()}" for t in texts)
        )

    sem = _get_semantic(service)
    if sem is not None:
        sem_hits = sem.search(query, limit=limit)
        if sem_hits:
            sections.append(
                "## Semantic\n"
                + "\n".join(
                    f"[{h.score:.3f}] {h.path}:{h.start_line}-{h.end_line}" for h in sem_hits
                )
            )

    return "\n\n".join(sections) if sections else f"No matches for: {query}"


@mcp.tool()
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
def reindex(full: bool = False, service: Optional[str] = None) -> str:
    """Rebuild the index from disk for one service. Use after large changes.

    full=true forces a complete re-index; otherwise only changed files are updated.
    """
    settings = _resolve_settings(service)
    k = _key(settings)
    with _lock:
        st = _stores.pop(k, None)
        if st is not None:
            st.close()
    report = build_index(settings, full=full)
    return (
        f"indexed={report.indexed} skipped={report.skipped} removed={report.removed} "
        f"symbols={report.symbols} semantic_files={report.semantic_files} "
        f"semantic={'on' if report.semantic_enabled else 'off'}"
    )


@mcp.tool()
def index_stats(service: Optional[str] = None) -> str:
    """Show how many files and symbols are currently indexed for a service."""
    return str(_get_store(service).stats())


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


def main() -> None:
    _startup_reindex()
    # Default stdio transport: Kilo/OpenCode launch this as a local MCP server.
    mcp.run()


if __name__ == "__main__":
    main()
