"""CLI search/read fallbacks for agents that do not have an MCP session."""

from code_index import cli
from code_index.store import Store


def test_cli_search_text_by_path(tmp_path, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    settings, _, _ = cli._settings_and_meta(root)
    store = Store(settings.db_path)
    store.upsert_file("a.py", 1.0, 5, "python", "def target():\n    pass\n", [])
    store.commit()
    store.close()

    rc = cli.main(["search-text", "target", "--path", str(root)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "a.py:1: def target():" in out


def test_cli_read_span_by_path(tmp_path, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("l1\nl2\nl3\n", encoding="utf-8")

    rc = cli.main(["read-span", "a.py", "2", "2", "--path-root", str(root)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "a.py:2-2" in out
    assert "l2" in out


def test_cli_search_text_unknown_service(capsys):
    rc = cli.main(["search-text", "target", "--service", "missing"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "Unknown service 'missing'" in err
