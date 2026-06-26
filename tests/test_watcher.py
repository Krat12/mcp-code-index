"""Watcher relevance filter (decides which FS events trigger a re-index)."""

import threading
import time
import types

import code_index.semantic as sem
from code_index.watcher import _keep_warm_loop, _relevant


def test_relevant_source_files():
    assert _relevant(r"C:\work\svc\src\main\java\Foo.java") is True
    assert _relevant(r"C:\work\svc\src\resources\application.properties") is True
    assert _relevant(r"C:\work\svc\service.py") is True
    assert _relevant(r"C:\work\svc\Dockerfile") is True


def test_irrelevant_build_and_binary():
    assert _relevant(r"C:\work\svc\build\classes\Gen.java") is False
    assert _relevant(r"C:\work\svc\target\app.jar") is False
    assert _relevant(r"C:\work\svc\.git\index") is False
    assert _relevant(r"C:\work\svc\logo.png") is False
    assert _relevant(r"C:\work\svc\node_modules\x\index.js") is False


def _run_keep_warm(settings, monkeypatch, interval=0.02, run_for=0.07):
    """Run the keep-warm loop briefly with a fake embedder; return its calls."""
    calls: list[list[str]] = []

    class _FakeEmbedder:
        def embed(self, texts):
            calls.append(list(texts))
            return [[0.0]]

    monkeypatch.setattr(sem, "_make_embedder", lambda s: _FakeEmbedder())
    stop = threading.Event()
    th = threading.Thread(target=_keep_warm_loop, args=(settings, interval, stop), daemon=True)
    th.start()
    time.sleep(run_for)
    stop.set()
    th.join(timeout=1)
    return calls


def test_keep_warm_pings_api_backend(monkeypatch):
    s = types.SimpleNamespace(embed_backend="api", semantic_enabled=True)
    calls = _run_keep_warm(s, monkeypatch)
    assert len(calls) >= 2  # repeated pings, not a one-shot
    # Must use embed() (the cache-less indexing path), not embed_query(); the
    # query LRU would serve a repeated string from memory and never warm the API.
    assert all(c == ["keep warm"] for c in calls)


def test_keep_warm_skips_fastembed_backend(monkeypatch):
    s = types.SimpleNamespace(embed_backend="fastembed", semantic_enabled=True)
    assert _run_keep_warm(s, monkeypatch) == []


def test_keep_warm_skips_when_semantic_disabled(monkeypatch):
    s = types.SimpleNamespace(embed_backend="api", semantic_enabled=False)
    assert _run_keep_warm(s, monkeypatch) == []
