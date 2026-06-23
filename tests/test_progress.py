"""Progress reporters and the cross-process status-file channel."""

import importlib

import code_index.config as config
import code_index.progress as progress
from code_index.indexer import IndexReport


def _reload():
    """conftest reloads config; progress caches STATUS_DIR at import -> reload it."""
    importlib.reload(config)
    importlib.reload(progress)


def test_status_file_roundtrip(tmp_path):
    _reload()
    rep = progress.StatusFileReporter("svc_abc", "billing", "C:/work/billing")
    rep.start()
    rep(3, 10, "src/a.py", "indexing")

    data = progress.read_status("svc_abc")
    assert data is not None
    assert data["name"] == "billing"
    assert data["phase"] == "indexing"
    assert data["done"] == 3
    assert data["total"] == 10
    assert data["current"] == "src/a.py"


def test_status_file_finish_and_error(tmp_path):
    _reload()
    rep = progress.StatusFileReporter("svc_fin", "payments", "/p")
    rep.start()
    report = IndexReport(indexed=5, skipped=2, removed=1, symbols=42, semantic_enabled=True)
    rep.finish(report)
    data = progress.read_status("svc_fin")
    assert data["phase"] == "done"
    assert data["indexed"] == 5
    assert data["symbols"] == 42
    assert data["finished_at"] is not None

    rep2 = progress.StatusFileReporter("svc_err", "broken", "/b")
    rep2.start()
    rep2.error(RuntimeError("boom"))
    err = progress.read_status("svc_err")
    assert err["phase"] == "error"
    assert "boom" in err["error"]


def test_read_all_status_and_clear(tmp_path):
    _reload()
    a = progress.StatusFileReporter("svc_a", "a", "/a")
    b = progress.StatusFileReporter("svc_b", "b", "/b")
    a.start()
    b.start()
    allst = progress.read_all_status()
    assert set(allst) >= {"svc_a", "svc_b"}

    progress.clear_status("svc_a")
    assert progress.read_status("svc_a") is None


def test_read_missing_status_returns_none(tmp_path):
    _reload()
    assert progress.read_status("does_not_exist") is None


def test_multi_reporter_fanout_is_resilient(tmp_path):
    _reload()

    class _Boom:
        def start(self):
            raise ValueError("x")

        def __call__(self, *a):
            raise ValueError("x")

        def finish(self, r):
            raise ValueError("x")

        def error(self, e):
            raise ValueError("x")

    calls = []

    class _Good:
        def start(self):
            calls.append("start")

        def __call__(self, *a):
            calls.append("call")

        def finish(self, r):
            calls.append("finish")

        def error(self, e):
            calls.append("error")

    multi = progress.MultiReporter([_Boom(), _Good()])
    # A misbehaving reporter must never break the fan-out.
    multi.start()
    multi(1, 2, "x", "indexing")
    multi.finish(IndexReport())
    multi.error(RuntimeError("e"))
    assert calls == ["start", "call", "finish", "error"]


def test_null_reporter_noop(tmp_path):
    _reload()
    r = progress.NullReporter()
    r.start()
    r(1, 2, "x", "indexing")
    r.finish(IndexReport())
    r.error(RuntimeError("x"))  # must not raise
