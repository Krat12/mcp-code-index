"""Store: FTS5 text search, symbol storage/search, incremental metadata."""

from code_index.store import Store
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


def test_stats_counts(tmp_path):
    s = _store(tmp_path)
    s.upsert_file("A.java", 1.0, 5, "java", "a\n", [Symbol("A", "class", 1, 1)])
    s.upsert_file("B.java", 1.0, 5, "java", "b\n", [])
    s.commit()
    st = s.stats()
    assert st["files"] == 2
    assert st["symbols"] == 1
    s.close()
