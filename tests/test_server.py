"""MCP server tools: semantic degradation visibility (D+F).

The tools must let an agent DISTINGUISH "semantic disabled" vs "unavailable"
vs "no matches" instead of all looking the same.
"""

import importlib
import inspect
import sqlite3

import pytest

import code_index.config as config


@pytest.fixture
def server(monkeypatch):
    """Import the server module fresh under the isolated test homes."""
    importlib.reload(config)
    import code_index.server as srv

    importlib.reload(srv)
    return srv


class _FakeSettings:
    def __init__(self, semantic_enabled):
        self.semantic_enabled = semantic_enabled
        self.root = config.Path(".").resolve()
        self.qdrant_url = "http://localhost:6333"
        self.qdrant_api_key = None
        self.qdrant_health_timeout = 0.75
        self.sqlite_read_timeout = 0.75

    def collection_name(self):
        return "code_test"


class _FakeSem:
    def __init__(self, hits=None, failed=False, health=None):
        self._hits = hits or []
        self.last_search_failed = failed
        self.last_error = "BoomError: down" if failed else None
        self._health = health or {"status": "ok", "collection": "c", "points": 7, "error": None}

    def search(self, query, limit=10, path_glob=None, exclude_glob=None):
        return self._hits

    def health(self):
        return self._health


def test_search_semantic_reports_disabled(server, monkeypatch):
    monkeypatch.setattr(server, "_semantic_status", lambda svc: (None, "disabled"))
    out = server.search_semantic("anything")
    assert "DISABLED" in out
    assert "search_text" in out  # points the agent at a working fallback


def test_search_semantic_reports_unavailable(server, monkeypatch):
    monkeypatch.setattr(server, "_semantic_status", lambda svc: (None, "unavailable"))
    out = server.search_semantic("anything")
    assert "UNAVAILABLE" in out
    assert "NOT 'no results'" in out or "degraded" in out.lower()


def test_search_semantic_reports_failed_midrequest(server, monkeypatch):
    sem = _FakeSem(hits=[], failed=True)
    monkeypatch.setattr(server, "_semantic_status", lambda svc: (sem, "ok"))
    out = server.search_semantic("anything")
    assert "FAILED" in out
    assert "down" in out


def test_search_semantic_empty_is_distinct_from_failure(server, monkeypatch):
    sem = _FakeSem(hits=[], failed=False)
    monkeypatch.setattr(server, "_semantic_status", lambda svc: (sem, "ok"))
    out = server.search_semantic("nothing matches")
    assert out.startswith("No semantic matches")


def test_index_stats_shows_semantic_disabled(server, monkeypatch):
    class _Store:
        def stats(self):
            return {"files": 10, "symbols": 20}

    monkeypatch.setattr(server, "_get_store", lambda svc=None: _Store())
    monkeypatch.setattr(server, "_resolve_settings", lambda svc: _FakeSettings(semantic_enabled=False))
    out = server.index_stats()
    assert "files=10" in out and "symbols=20" in out
    assert "semantic: disabled" in out


def test_index_stats_shows_semantic_ok(server, monkeypatch):
    class _Store:
        def stats(self):
            return {"files": 5, "symbols": 9}

    monkeypatch.setattr(server, "_get_store", lambda svc=None: _Store())
    monkeypatch.setattr(server, "_resolve_settings", lambda svc: _FakeSettings(semantic_enabled=True))
    monkeypatch.setattr(
        server,
        "_qdrant_status_fast",
        lambda settings: {"status": "ok", "collection": "c", "points": 7, "error": None},
    )
    out = server.index_stats()
    assert "semantic: ok" in out
    assert "points=7" in out


def test_index_stats_shows_semantic_unavailable(server, monkeypatch):
    class _Store:
        def stats(self):
            return {"files": 1, "symbols": 2}

    monkeypatch.setattr(server, "_get_store", lambda svc=None: _Store())
    monkeypatch.setattr(server, "_resolve_settings", lambda svc: _FakeSettings(semantic_enabled=True))
    monkeypatch.setattr(
        server,
        "_qdrant_status_fast",
        lambda settings: {"status": "unavailable", "collection": "c", "points": None, "error": "down"},
    )
    out = server.index_stats()
    assert "semantic: unavailable" in out


def test_index_stats_does_not_construct_semantic(server, monkeypatch):
    class _Store:
        def stats(self):
            return {"files": 3, "symbols": 4}

    def fail_get_semantic(svc=None):
        raise AssertionError("index_stats must not construct SemanticIndex")

    monkeypatch.setattr(server, "_get_store", lambda svc=None: _Store())
    monkeypatch.setattr(server, "_resolve_settings", lambda svc: _FakeSettings(semantic_enabled=True))
    monkeypatch.setattr(server, "_get_semantic", fail_get_semantic)
    monkeypatch.setattr(
        server,
        "_qdrant_status_fast",
        lambda settings: {"status": "not_checked", "collection": "c", "points": None, "error": None},
    )

    out = server.index_stats()
    assert "files=3" in out and "symbols=4" in out
    assert "health not checked" in out


def test_search_text_reports_busy_database(server, monkeypatch):
    class _Store:
        def search_text(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(server, "_get_store", lambda svc=None: _Store())

    out = server.search_text("anything")
    assert "Index database is busy" in out
    assert "retry" in out.lower()


def test_search_hybrid_reports_busy_database(server, monkeypatch):
    class _Store:
        def search_symbol(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(server, "_get_store", lambda svc=None: _Store())

    out = server.search_hybrid("anything")
    assert "Index database is busy" in out


def test_tool_tracing_logs_start_and_finish(server, tmp_path, monkeypatch):
    log_file = tmp_path / "mcp.log"

    class _FakeLogger:
        def info(self, msg, *args):
            log_file.write_text(log_file.read_text() + (msg % args) + "\n" if log_file.exists() else (msg % args) + "\n")

        def warning(self, msg, *args):
            self.info(msg, *args)

        def exception(self, msg, *args):
            self.info(msg, *args)

        def log(self, level, msg, *args):
            self.info(msg, *args)

    class _Store:
        def stats(self):
            return {"files": 1, "symbols": 2}

    monkeypatch.setattr(server, "_setup_logger", lambda: _FakeLogger())
    monkeypatch.setattr(server, "_get_store", lambda svc=None: _Store())
    monkeypatch.setattr(server, "_resolve_settings", lambda svc: _FakeSettings(semantic_enabled=False))

    out = server.index_stats()
    text = log_file.read_text()
    assert "files=1" in out
    assert "tool_start" in text
    assert "tool_finish" in text
    assert "tool=index_stats" in text


def test_tool_tracing_preserves_signature(server):
    sig = inspect.signature(server.read_span)
    assert list(sig.parameters) == ["path", "start_line", "end_line", "context", "service"]


class _ImmediateThread:
    """Runs target() synchronously on start() so prewarm is testable."""

    daemon = False

    def __init__(self, target=None, daemon=False):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()


def test_prewarm_semantic_builds_index_when_enabled(server, monkeypatch):
    monkeypatch.setattr(server._default_settings, "semantic_enabled", True)
    monkeypatch.setenv("CODE_INDEX_PREWARM", "1")
    monkeypatch.setattr(server, "_thread_factory", _ImmediateThread)

    calls = []
    monkeypatch.setattr(server, "_get_semantic", lambda svc=None: calls.append(svc))

    server._prewarm_semantic()
    assert calls == [None]


def test_prewarm_semantic_noop_when_disabled(server, monkeypatch):
    monkeypatch.setattr(server._default_settings, "semantic_enabled", False)
    monkeypatch.setenv("CODE_INDEX_PREWARM", "1")
    monkeypatch.setattr(server, "_thread_factory", _ImmediateThread)

    def fail(svc=None):
        raise AssertionError("prewarm must not build semantic when disabled")

    monkeypatch.setattr(server, "_get_semantic", fail)
    server._prewarm_semantic()  # must not raise


def test_prewarm_semantic_respects_opt_out(server, monkeypatch):
    monkeypatch.setattr(server._default_settings, "semantic_enabled", True)
    monkeypatch.setenv("CODE_INDEX_PREWARM", "0")
    monkeypatch.setattr(server, "_thread_factory", _ImmediateThread)

    def fail(svc=None):
        raise AssertionError("prewarm must be a no-op when CODE_INDEX_PREWARM=0")

    monkeypatch.setattr(server, "_get_semantic", fail)
    server._prewarm_semantic()  # must not raise


def test_prewarm_semantic_survives_build_failure(server, monkeypatch):
    monkeypatch.setattr(server._default_settings, "semantic_enabled", True)
    monkeypatch.setenv("CODE_INDEX_PREWARM", "1")
    monkeypatch.setattr(server, "_thread_factory", _ImmediateThread)

    def boom(svc=None):
        raise RuntimeError("qdrant import blew up")

    monkeypatch.setattr(server, "_get_semantic", boom)
    server._prewarm_semantic()  # best-effort: must swallow the error


def test_tool_tracing_emits_slow_event(server, monkeypatch):
    events = []

    class _FakeLogger:
        def info(self, msg, *args):
            events.append(msg % args)

        def warning(self, msg, *args):
            events.append(msg % args)

        def exception(self, msg, *args):
            events.append(msg % args)

        def log(self, level, msg, *args):
            events.append(msg % args)

    monkeypatch.setattr(server, "_setup_logger", lambda: _FakeLogger())
    monkeypatch.setattr(server._default_settings, "slow_tool_seconds", 0.01)

    class _ImmediateTimer:
        daemon = False

        def __init__(self, interval, callback):
            self._callback = callback

        def start(self):
            self._callback()

        def cancel(self):
            pass

    monkeypatch.setattr(server, "_timer_factory", _ImmediateTimer)

    server._run_tool("slow_test", None, {}, lambda: None)

    assert any("tool_slow" in e for e in events)
