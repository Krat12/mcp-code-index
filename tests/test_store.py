"""Store: FTS5 text search, symbol storage/search, incremental metadata."""

import pytest

from code_index.store import Store, StoreError
from code_index.symbols import Symbol


def _store(tmp_path):
    return Store(tmp_path / "idx.sqlite3")


def test_text_search_returns_line_numbers(tmp_path):
    s = _store(tmp_path)
    s.upsert_file(
        "Foo.java", 1.0, 10, "java",
        "class Foo {\n  void chargeCustomer() {}\n}\n",
        [],
    )
    s.commit()
    hits = s.search_text("chargeCustomer")
    assert len(hits) == 1
    assert hits[0].path == "Foo.java"
    assert hits[0].line == 2
    s.close()


def test_symbol_search_exact_and_substring(tmp_path):
    s = _store(tmp_path)
    syms = [
        Symbol(name="chargeCustomer", kind="method", start_line=2, end_line=2),
        Symbol(name="Foo", kind="class", start_line=1, end_line=3),
    ]
    s.upsert_file("Foo.java", 1.0, 10, "java", "class Foo {}\n", syms)
    s.commit()

    exact = s.search_symbol("Foo", exact=True)
    assert [h.name for h in exact] == ["Foo"]

    sub = s.search_symbol("charge", exact=False)
    assert any(h.name == "chargeCustomer" for h in sub)
    s.close()


def test_upsert_replaces_previous_data(tmp_path):
    s = _store(tmp_path)
    s.upsert_file("A.java", 1.0, 5, "java", "old content here\n", [])
    s.commit()
    assert s.search_text("old")

    s.upsert_file("A.java", 2.0, 6, "java", "new content here\n", [])
    s.commit()
    assert not s.search_text("old")
    assert s.search_text("new")
    s.close()


def test_file_is_current_tracks_mtime_size(tmp_path):
    s = _store(tmp_path)
    s.upsert_file("A.java", 100.0, 50, "java", "x\n", [])
    s.commit()
    assert s.file_is_current("A.java", 100.0, 50) is True
    assert s.file_is_current("A.java", 101.0, 50) is False  # changed mtime
    assert s.file_is_current("A.java", 100.0, 51) is False  # changed size
    assert s.file_is_current("B.java", 100.0, 50) is False  # unknown
    s.close()


def test_delete_file_removes_everything(tmp_path):
    s = _store(tmp_path)
    s.upsert_file("A.java", 1.0, 5, "java", "hello world\n",
                  [Symbol("A", "class", 1, 1)])
    s.commit()
    s.delete_file("A.java")
    s.commit()
    assert s.search_text("hello") == []
    assert s.search_symbol("A") == []
    assert "A.java" not in s.known_paths()
    s.close()


def test_delete_files_batches_and_dedupes(tmp_path):
    s = _store(tmp_path)
    # More files than one batch so the IN-chunking path is exercised.
    n = 450
    for i in range(n):
        s.upsert_file(f"f{i}.java", 1.0, 5, "java", f"token{i} body\n",
                      [Symbol(f"Sym{i}", "class", 1, 1)])
    s.commit()
    assert s.stats()["files"] == n

    # Delete most of them in a single batched call (with a duplicate + an
    # unknown path mixed in, which must be tolerated).
    to_delete = [f"f{i}.java" for i in range(400)]
    to_delete += ["f0.java", "does-not-exist.java"]  # duplicate + unknown
    deleted = s.delete_files(to_delete, batch=200)
    s.commit()
    assert deleted == 401  # 400 distinct files + 1 unknown, de-duped

    assert s.stats()["files"] == 50  # f400..f449 remain
    assert s.search_text("token0") == []      # deleted file's text gone
    assert s.search_symbol("Sym0") == []       # deleted file's symbols gone
    assert s.search_text("token449")           # survivor still searchable
    assert "f400.java" in s.known_paths()
    assert "f399.java" not in s.known_paths()
    s.close()


def test_delete_files_empty_is_noop(tmp_path):
    s = _store(tmp_path)
    s.upsert_file("A.java", 1.0, 5, "java", "hello\n", [])
    s.commit()
    assert s.delete_files([]) == 0
    assert s.delete_files(()) == 0
    s.commit()
    assert "A.java" in s.known_paths()
    s.close()


def test_stats_counts(tmp_path):
    s = _store(tmp_path)
    s.upsert_file("A.java", 1.0, 5, "java", "a\n", [Symbol("A", "class", 1, 1)])
    s.upsert_file("B.java", 1.0, 5, "java", "b\n", [])
    s.commit()
    st = s.stats()
    assert st["files"] == 2
    assert st["symbols"] == 1
    s.close()


def test_search_text_path_glob_filters(tmp_path):
    s = _store(tmp_path)
    s.upsert_file("backend/svc.py", 1.0, 5, "python", "def dedup():\n    pass\n", [])
    s.upsert_file("backend/tests/test_svc.py", 1.0, 5, "python", "def test_dedup():\n    pass\n", [])
    s.commit()

    only_backend = s.search_text("dedup", path_glob=["backend/**"])
    assert {h.path for h in only_backend} == {"backend/svc.py", "backend/tests/test_svc.py"}

    no_tests = s.search_text("dedup", exclude_glob=["**/tests/**"])
    assert [h.path for h in no_tests] == ["backend/svc.py"]

    only_tests = s.search_text("dedup", path_glob=["**/tests/**"])
    assert [h.path for h in only_tests] == ["backend/tests/test_svc.py"]
    s.close()


def test_search_symbol_path_glob_filters(tmp_path):
    s = _store(tmp_path)
    s.upsert_file("backend/a.py", 1.0, 5, "python", "x\n", [Symbol("dedup", "function", 1, 2)])
    s.upsert_file("backend/tests/b.py", 1.0, 5, "python", "y\n", [Symbol("dedup_test", "function", 1, 2)])
    s.commit()

    hits = s.search_symbol("dedup", exclude_glob=["**/tests/**"])
    assert [h.path for h in hits] == ["backend/a.py"]
    s.close()


def test_search_text_robust_to_bad_fts_query(tmp_path):
    s = _store(tmp_path)
    s.upsert_file("a.py", 1.0, 5, "python", "value = compute(x)\n", [])
    s.commit()
    # Unbalanced paren / quote would normally raise OperationalError in FTS5.
    assert s.search_text("compute(x") != []  # falls back to a phrase query
    assert isinstance(s.search_text('"oops'), list)  # no crash
    assert isinstance(s.search_text("AND OR NOT"), list)  # dangling operators
    s.close()


def test_get_lines_returns_stored_span(tmp_path):
    s = _store(tmp_path)
    s.upsert_file("a.py", 1.0, 5, "python", "l1\nl2\nl3\nl4\nl5\n", [])
    s.commit()
    rows = s.get_lines("a.py", 2, 4)
    assert [r.line for r in rows] == [2, 3, 4]
    assert [r.content for r in rows] == ["l2", "l3", "l4"]
    assert s.get_lines("missing.py", 1, 3) == []
    s.close()


def test_busy_timeout_pragma_set(tmp_path):
    s = _store(tmp_path)
    val = s.conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert val >= 1000  # a generous wait so two writers don't instantly error
    s.close()


def test_corrupt_db_raises_store_error(tmp_path):
    bad = tmp_path / "corrupt.sqlite3"
    # A file that is NOT a valid SQLite database.
    bad.write_bytes(b"this is definitely not sqlite" * 100)
    with pytest.raises(StoreError):
        Store(bad)
