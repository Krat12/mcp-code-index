"""MCP server tools: semantic degradation visibility (D+F).

The tools must let an agent DISTINGUISH "semantic disabled" vs "unavailable"
vs "no matches" instead of all looking the same.
"""

import importlib

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
    monkeypatch.setattr(server, "_get_semantic", lambda svc=None: _FakeSem())
    out = server.index_stats()
    assert "semantic: ok" in out
    assert "points=7" in out


def test_index_stats_shows_semantic_unavailable(server, monkeypatch):
    class _Store:
        def stats(self):
            return {"files": 1, "symbols": 2}

    monkeypatch.setattr(server, "_get_store", lambda svc=None: _Store())
    monkeypatch.setattr(server, "_resolve_settings", lambda svc: _FakeSettings(semantic_enabled=True))
    monkeypatch.setattr(server, "_get_semantic", lambda svc=None: None)
    out = server.index_stats()
    assert "semantic: unavailable" in out
