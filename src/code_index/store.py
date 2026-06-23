"""SQLite storage: FTS5 full-text index + a symbols table.

The DB is the source of truth for the text and symbol layers and also tracks
file mtimes so re-indexing is incremental.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path   TEXT PRIMARY KEY,
    mtime  REAL NOT NULL,
    size   INTEGER NOT NULL,
    lang   TEXT
);

-- One row per line for precise grep-like results with line numbers.
CREATE VIRTUAL TABLE IF NOT EXISTS lines_fts USING fts5(
    path UNINDEXED,
    line UNINDEXED,
    content,
    tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS symbols (
    path        TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);
"""


@dataclass
class TextHit:
    path: str
    line: int
    content: str


@dataclass
class SymbolHit:
    path: str
    name: str
    kind: str
    start_line: int
    end_line: int


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._apply_pragmas()
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _apply_pragmas(self) -> None:
        """Speed up bulk inserts dramatically vs. SQLite defaults.

        Default SQLite uses a rollback journal with synchronous=FULL, which
        fsyncs on every commit and makes large incremental indexing crawl.
        WAL + synchronous=NORMAL is safe (durable across app crashes; only an
        OS-level crash mid-write could lose the last txn — fine for a rebuildable
        code index) and far faster. The rest are pure in-memory speedups.
        """
        for pragma in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA temp_store=MEMORY",
            "PRAGMA cache_size=-65536",   # ~64MB page cache
            "PRAGMA mmap_size=268435456",  # 256MB memory-mapped I/O
        ):
            try:
                self.conn.execute(pragma)
            except sqlite3.Error:
                pass

    def close(self) -> None:
        self.conn.close()

    # ---- indexing ---------------------------------------------------------

    def file_is_current(self, path: str, mtime: float, size: int) -> bool:
        row = self.conn.execute(
            "SELECT mtime, size FROM files WHERE path = ?", (path,)
        ).fetchone()
        return bool(row) and row["mtime"] == mtime and row["size"] == size

    def known_paths(self) -> set[str]:
        return {r["path"] for r in self.conn.execute("SELECT path FROM files")}

    def delete_file(self, path: str) -> None:
        self.conn.execute("DELETE FROM lines_fts WHERE path = ?", (path,))
        self.conn.execute("DELETE FROM symbols WHERE path = ?", (path,))
        self.conn.execute("DELETE FROM files WHERE path = ?", (path,))

    def upsert_file(
        self,
        path: str,
        mtime: float,
        size: int,
        lang: str | None,
        text: str,
        symbols: list,
    ) -> None:
        """Replace all data for one file (text lines + symbols + metadata)."""
        cur = self.conn
        # Clear previous data for this path.
        cur.execute("DELETE FROM lines_fts WHERE path = ?", (path,))
        cur.execute("DELETE FROM symbols WHERE path = ?", (path,))

        cur.executemany(
            "INSERT INTO lines_fts(path, line, content) VALUES (?, ?, ?)",
            ((path, i + 1, ln) for i, ln in enumerate(text.splitlines())),
        )
        if symbols:
            cur.executemany(
                "INSERT INTO symbols(path, name, kind, start_line, end_line) VALUES (?, ?, ?, ?, ?)",
                ((path, s.name, s.kind, s.start_line, s.end_line) for s in symbols),
            )
        cur.execute(
            "INSERT INTO files(path, mtime, size, lang) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size, lang=excluded.lang",
            (path, mtime, size, lang),
        )

    def commit(self) -> None:
        self.conn.commit()

    # ---- querying ---------------------------------------------------------

    def search_text(self, query: str, limit: int = 30) -> list[TextHit]:
        rows = self.conn.execute(
            "SELECT path, line, content FROM lines_fts WHERE lines_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()
        return [TextHit(r["path"], r["line"], r["content"]) for r in rows]

    def search_symbol(self, name: str, limit: int = 30, exact: bool = False) -> list[SymbolHit]:
        if exact:
            rows = self.conn.execute(
                "SELECT path, name, kind, start_line, end_line FROM symbols "
                "WHERE name = ? LIMIT ?",
                (name, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT path, name, kind, start_line, end_line FROM symbols "
                "WHERE name LIKE ? ORDER BY length(name) LIMIT ?",
                (f"%{name}%", limit),
            ).fetchall()
        return [
            SymbolHit(r["path"], r["name"], r["kind"], r["start_line"], r["end_line"])
            for r in rows
        ]

    def file_symbols(self, path: str) -> list[SymbolHit]:
        rows = self.conn.execute(
            "SELECT path, name, kind, start_line, end_line FROM symbols "
            "WHERE path = ? ORDER BY start_line",
            (path,),
        ).fetchall()
        return [
            SymbolHit(r["path"], r["name"], r["kind"], r["start_line"], r["end_line"])
            for r in rows
        ]

    def stats(self) -> dict:
        files = self.conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
        syms = self.conn.execute("SELECT COUNT(*) c FROM symbols").fetchone()["c"]
        return {"files": files, "symbols": syms}
