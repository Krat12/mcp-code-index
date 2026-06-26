"""Filesystem walking, .gitignore-lite filtering, and file reading."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterator

from .config import (
    DEFAULT_IGNORE_DIRS,
    DEFAULT_IGNORE_FILES,
    DEFAULT_TEXT_EXTS,
    DOC_EXTS,
    MAX_DOC_BYTES,
    MAX_FILE_BYTES,
    MINIFIED_MARKERS,
    ignore_path_parts,
)


# Hidden directories that are still worth indexing despite starting with a dot.
_KEEP_HIDDEN: set[str] = {".github", ".mvn"}


# ---------------------------------------------------------------------------
# Per-project ignore patterns (glob / simplified .gitignore).
#
# Matching is done against POSIX-relative paths (see `rel`) so it is stable
# across operating systems. We deliberately implement a small, self-contained
# matcher (stdlib `re`/`fnmatch`-style) instead of pulling in pathspec: it is
# light and predictable. Supported, gitignore-like semantics:
#   *   matches anything except "/"
#   **  matches anything including "/"
#   ?   matches a single char except "/"
#   trailing "/"  -> matches directories only
#   a leading "/" or any embedded "/" anchors the pattern to the repo root;
#   a pattern with no "/" matches at any depth (by basename or directory name)
# Negation ("!") and per-subdirectory .gitignore files are NOT supported by
# design (keeps the matcher tiny and the behavior obvious).
# ---------------------------------------------------------------------------


def _glob_to_regex(pat: str) -> str:
    """Translate the glob body (no anchoring) into a regex fragment."""
    out: list[str] = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if i + 1 < n and pat[i + 1] == "*":
                # "**/" -> zero or more path segments; bare "**" -> anything
                if i + 2 < n and pat[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
                continue
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _translate(pattern: str) -> tuple[re.Pattern[str], bool] | None:
    """Compile one gitignore-style pattern. Returns (regex, dir_only) or None."""
    pat = pattern.rstrip()
    if not pat or pat.startswith("#") or pat.startswith("!"):
        return None  # comments, blanks and negations are ignored by design

    dir_only = pat.endswith("/")
    if dir_only:
        pat = pat[:-1]
    anchored = pat.startswith("/")
    if anchored:
        pat = pat[1:]
    if not pat:
        return None
    # A slash anywhere (after the leading one) also anchors to the root.
    if "/" in pat:
        anchored = True

    body = _glob_to_regex(pat)
    if anchored:
        full = "^" + body + r"(?:/.*)?$"
    else:
        # Unanchored patterns match at any depth (by basename / dir name),
        # mirroring .gitignore semantics for slash-less patterns.
        full = "(?:^|.*/)" + body + r"(?:/.*)?$"
    try:
        return re.compile(full), dir_only
    except re.error:
        return None


class IgnoreSpec:
    """A compiled set of ignore patterns matched against POSIX-relative paths."""

    __slots__ = ("_patterns",)

    def __init__(self, patterns: list[str] | None = None) -> None:
        self._patterns: list[tuple[re.Pattern[str], bool]] = []
        for p in patterns or []:
            compiled = _translate(p)
            if compiled is not None:
                self._patterns.append(compiled)

    def __bool__(self) -> bool:
        return bool(self._patterns)

    def match(self, relposix: str, is_dir: bool = False) -> bool:
        """True if the (relative, POSIX) path is ignored.

        For directories a trailing slash is appended so that patterns like
        `docs/legacy/**` prune the directory itself (and thus its contents).
        """
        if not self._patterns:
            return False
        target = relposix + "/" if is_dir else relposix
        for rx, dir_only in self._patterns:
            if dir_only and not is_dir:
                continue
            if rx.match(target):
                return True
        return False


class PathFilter:
    """Include/exclude glob filter over POSIX-relative paths (search results).

    Reuses IgnoreSpec's matcher so glob semantics are identical everywhere:
      - include: if any include glob is given, a path must match at least one.
      - exclude: a path matching any exclude glob is dropped.
    Empty/None on both sides => matches everything (a no-op filter).
    """

    __slots__ = ("_inc", "_exc")

    def __init__(self, include: list[str] | None = None, exclude: list[str] | None = None) -> None:
        self._inc = IgnoreSpec(include) if include else None
        self._exc = IgnoreSpec(exclude) if exclude else None

    def __bool__(self) -> bool:
        return self._inc is not None or self._exc is not None

    def match(self, relposix: str) -> bool:
        if self._inc is not None and not self._inc.match(relposix):
            return False
        if self._exc is not None and self._exc.match(relposix):
            return False
        return True


def read_span(root: Path, relpath: str, start: int, end: int, context: int = 0) -> str | None:
    """Read lines [start, end] (1-based, inclusive) of a repo file, +/- context.

    Returns the slice as text, or None if the file can't be read. `relpath` is
    confined to `root` (path-traversal guard) so the tool can't read outside the
    indexed repo.
    """
    root = root.resolve()
    target = (root / relpath).resolve()
    try:
        target.relative_to(root)  # guard: stay inside the repo
    except ValueError:
        return None
    try:
        if target.stat().st_size > MAX_FILE_BYTES:
            return None
    except OSError:
        return None
    text = read_text(target)
    if text is None:
        return None
    lines = text.splitlines()
    n = len(lines)
    if n == 0:
        return ""
    lo = max(1, start - max(0, context))
    hi = min(n, end + max(0, context))
    if lo > n:
        return ""
    return "\n".join(lines[lo - 1 : hi])


def read_gitignore(root: Path) -> list[str]:
    """Read the repo-root .gitignore into a list of raw patterns (best effort).

    Only the top-level .gitignore is read; nested .gitignore files and negation
    rules are intentionally not supported. We never write to the repo.
    """
    f = root / ".gitignore"
    try:
        if not f.is_file():
            return []
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("!"):
            continue
        out.append(s)
    return out


def build_ignore_spec(
    globs: list[str] | None = None,
    use_gitignore: bool = False,
    root: Path | None = None,
) -> IgnoreSpec:
    """Combine explicit ignore globs with optional .gitignore rules."""
    patterns: list[str] = list(globs or [])
    if use_gitignore and root is not None:
        patterns.extend(read_gitignore(root))
    return IgnoreSpec(patterns)


def _keep_dir(name: str) -> bool:
    """Decide whether to descend into a directory."""
    if name in DEFAULT_IGNORE_DIRS:
        return False
    if name.startswith(".") and name not in _KEEP_HIDDEN:
        return False
    return True


def _keep_file(name: str) -> bool:
    """Decide whether a filename is worth considering (before reading it)."""
    low = name.lower()
    if low in DEFAULT_IGNORE_FILES:
        return False
    if any(marker in low for marker in MINIFIED_MARKERS):
        return False
    return True


def iter_files(root: Path, spec: IgnoreSpec | None = None) -> Iterator[Path]:
    """Yield candidate text files under root, skipping ignored dirs and big/binary files.

    `spec` adds per-project ignore globs (and optional .gitignore rules) on top
    of the built-in defaults; pass None to keep the default behavior.
    """
    root = root.resolve()
    ignore_parts = ignore_path_parts()
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune ignored directories in-place so os.walk doesn't descend into them.
        kept: list[str] = []
        for d in dirnames:
            if not _keep_dir(d):
                continue
            if spec is not None:
                drel = rel(root, Path(dirpath) / d)
                if spec.match(drel, is_dir=True):
                    continue
            kept.append(d)
        dirnames[:] = kept
        for name in filenames:
            if not _keep_file(name):
                continue
            p = Path(dirpath) / name
            ext = p.suffix.lower()
            # Allow known text extensions, plus a few extension-less names.
            if ext not in DEFAULT_TEXT_EXTS and name.lower() not in {"dockerfile", "makefile"}:
                continue
            relposix = rel(root, p)
            # Skip generated/translated docs by relative path (e.g. docs/i18n/).
            if any(part in relposix.lower() for part in ignore_parts):
                continue
            # Per-project ignore globs / .gitignore rules.
            if spec is not None and spec.match(relposix, is_dir=False):
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            # Prose/docs get a tighter cap; everything else the general cap.
            cap = MAX_DOC_BYTES if ext in DOC_EXTS else MAX_FILE_BYTES
            if size > cap:
                continue
            yield p


def read_text(path: Path) -> str | None:
    """Read a file as UTF-8 text.

    Returns None for binary/undecodable files AND for minified/generated files
    (detected by an extremely long line), which would otherwise bloat the FTS
    index and slow indexing to a crawl.
    """
    from .config import MAX_LINE_CHARS

    try:
        data = path.read_bytes()
    except OSError:
        return None
    # Quick binary sniff: NUL byte in first chunk => treat as binary.
    if b"\x00" in data[:4096]:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return None
    # Minified / single-line generated file guard.
    if any(len(line) > MAX_LINE_CHARS for line in text.splitlines()):
        return None
    return text


def rel(root: Path, path: Path) -> str:
    """POSIX-style path relative to root (stable across OSes for storage)."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def chunk_lines(text: str, window: int = 60, overlap: int = 15) -> Iterator[tuple[int, int, str]]:
    """Yield (start_line, end_line, chunk_text) windows for semantic embedding.

    1-based, inclusive line numbers. Overlap keeps context across chunk borders.
    """
    lines = text.splitlines()
    n = len(lines)
    if n == 0:
        return
    step = max(1, window - overlap)
    start = 0
    while start < n:
        end = min(n, start + window)
        chunk = "\n".join(lines[start:end])
        if chunk.strip():
            yield (start + 1, end, chunk)
        if end >= n:
            break
        start += step
