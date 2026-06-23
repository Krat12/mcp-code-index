"""Incremental indexer that fills all three layers from the filesystem."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .semantic import SemanticIndex
from .store import Store
from .symbols import SymbolExtractor, lang_for_ext
from .walker import build_ignore_spec, chunk_lines, iter_files, read_text, rel


@dataclass
class IndexReport:
    indexed: int = 0
    skipped: int = 0
    removed: int = 0
    symbols: int = 0
    semantic_files: int = 0
    semantic_enabled: bool = False


def build_index(
    settings: Settings,
    full: bool = False,
    log=lambda m: None,
    on_progress=None,
) -> IndexReport:
    """Walk the project root and (re)index changed files into all layers.

    full=True forces a complete re-index regardless of mtimes.

    on_progress: optional callable(done, total, current, phase) reporting live
    progress. `phase` is one of "scanning" | "indexing" | "removing" | "done".
    It is best-effort (wrapped so a misbehaving reporter never breaks indexing).
    """

    def _emit(done: int, total: int, current: str, phase: str) -> None:
        if on_progress is None:
            return
        try:
            on_progress(done, total, current, phase)
        except Exception:
            pass

    store = Store(settings.db_path)
    extractor = SymbolExtractor()
    report = IndexReport(semantic_enabled=settings.semantic_enabled)

    semantic: SemanticIndex | None = None
    if settings.semantic_enabled:
        semantic = SemanticIndex(settings)
        if semantic.available:
            semantic.ensure_collection()
        if semantic.available:
            log(f"semantic: ON (backend={settings.embed_backend}, collection={settings.collection_name()})")
        else:
            log("semantic: OFF (embedder/qdrant unavailable)")
            report.semantic_enabled = False
    if not extractor.available:
        log("symbols: OFF (tree-sitter unavailable) -> text layer only")

    # Per-project ignore globs (+ optional .gitignore) on top of the defaults.
    spec = build_ignore_spec(
        getattr(settings, "ignore_globs", None),
        getattr(settings, "use_gitignore", False),
        settings.root,
    )

    # Materialize the file list once so we have a total for progress reporting.
    # One filesystem pass, same as before; the Path list is cheap even for
    # thousands of files.
    _emit(0, 0, "", "scanning")
    files = list(iter_files(settings.root, spec))
    total = len(files)
    _emit(0, total, "", "indexing")

    seen: set[str] = set()
    for i, path in enumerate(files, 1):
        relpath = rel(settings.root, path)
        seen.add(relpath)
        try:
            st = path.stat()
        except OSError:
            _emit(i, total, relpath, "indexing")
            continue

        if not full and store.file_is_current(relpath, st.st_mtime, st.st_size):
            report.skipped += 1
            _emit(i, total, relpath, "indexing")
            continue

        text = read_text(path)
        if text is None:
            report.skipped += 1
            _emit(i, total, relpath, "indexing")
            continue

        lang = lang_for_ext(path.suffix)
        syms = extractor.extract(lang, text) if lang else []

        store.upsert_file(relpath, st.st_mtime, st.st_size, lang, text, syms)
        report.indexed += 1
        report.symbols += len(syms)

        if semantic and semantic.available:
            semantic.delete_path(relpath)
            chunks = list(chunk_lines(text))
            semantic.index_chunks(relpath, chunks)
            if chunks:
                report.semantic_files += 1

        if report.indexed % 200 == 0:
            store.commit()
            log(f"  indexed {report.indexed} files...")

        _emit(i, total, relpath, "indexing")

    # Remove files that vanished from disk.
    _emit(total, total, "", "removing")
    for gone in store.known_paths() - seen:
        store.delete_file(gone)
        if semantic and semantic.available:
            semantic.delete_path(gone)
        report.removed += 1

    store.commit()
    store.close()
    _emit(total, total, "", "done")
    return report


def run_index(settings: Settings, full: bool = False, reporter=None, log=lambda m: None) -> IndexReport:
    """Index one service, driving a progress reporter's full lifecycle.

    `reporter` implements start()/__call__(done,total,current,phase)/finish()/
    error() (see progress.py). Pass None to skip reporting. The reporter's
    start/finish/error are always called so status files stay consistent even
    when indexing raises.
    """
    if reporter is not None:
        try:
            reporter.start()
        except Exception:
            pass
    try:
        report = build_index(settings, full=full, log=log, on_progress=reporter)
    except BaseException as exc:
        if reporter is not None:
            try:
                reporter.error(exc)
            except Exception:
                pass
        raise
    if reporter is not None:
        try:
            reporter.finish(report)
        except Exception:
            pass
    return report
