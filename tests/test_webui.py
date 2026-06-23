"""Web UI helpers: snapshot busy flag, reindex job guard, service lookup."""

import importlib
import time

import code_index.config as config
import code_index.webui as webui
from code_index.registry import Service


def _reload():
    importlib.reload(config)
    importlib.reload(webui)


def _register(tmp_path, monkeypatch, name="svc"):
    """Create a tiny repo and register it via a temp projects.toml."""
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    (repo / "main.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    reg = tmp_path / "projects.toml"
    reg.write_text(
        f'[[service]]\nname = "{name}"\npath = "{repo.as_posix()}"\n', encoding="utf-8"
    )
    monkeypatch.setattr("code_index.registry.REGISTRY_PATH", reg)
    return repo, reg


def test_service_by_id_matches_name_and_id(tmp_path, monkeypatch):
    _reload()
    repo, _ = _register(tmp_path, monkeypatch, "billing")
    monkeypatch.setattr("code_index.registry.REGISTRY_PATH", tmp_path / "projects.toml")

    svc = webui._service_by_id("billing")
    assert svc is not None
    assert webui._service_by_id(svc.id) is not None
    assert webui._service_by_id("nope") is None


def test_start_reindex_is_idempotent_and_runs(tmp_path, monkeypatch):
    _reload()
    repo, _ = _register(tmp_path, monkeypatch, "payments")
    svc = webui._service_by_id("payments")
    assert svc is not None

    started, msg = webui.start_reindex(svc)
    assert started is True
    # The job thread should be tracked.
    assert svc.id in webui._jobs
    # Wait for the background index to complete.
    for _ in range(100):
        if not webui._job_alive(svc.id):
            break
        time.sleep(0.05)
    assert not webui._job_alive(svc.id)


def test_snapshot_reports_busy_flag(tmp_path, monkeypatch):
    _reload()
    _register(tmp_path, monkeypatch, "shipping")
    svc = webui._service_by_id("shipping")

    # Not busy initially.
    snap = webui._snapshot()
    row = next(r for r in snap["services"] if r["id"] == svc.id)
    assert row["busy"] is False

    # An active phase in the status file should mark it busy.
    from code_index.progress import StatusFileReporter

    rep = StatusFileReporter(svc.id, svc.name, str(svc.path))
    rep.start()
    rep(1, 10, "x.py", "indexing")
    snap2 = webui._snapshot()
    row2 = next(r for r in snap2["services"] if r["id"] == svc.id)
    assert row2["busy"] is True


def test_is_busy_phase_detection(tmp_path):
    _reload()
    assert webui._is_busy("nobody", "indexing") is True
    assert webui._is_busy("nobody", "scanning") is True
    assert webui._is_busy("nobody", "idle") is False
    assert webui._is_busy("nobody", "done") is False


def test_start_reindex_passes_full_flag(tmp_path, monkeypatch):
    _reload()
    _register(tmp_path, monkeypatch, "ledger")
    svc = webui._service_by_id("ledger")

    captured = {}

    def fake_run_index(settings, full=False, reporter=None):
        captured["full"] = full
        return None

    monkeypatch.setattr(webui, "run_index", fake_run_index)
    started, msg = webui.start_reindex(svc, full=True)
    assert started is True
    for _ in range(100):
        if not webui._job_alive(svc.id):
            break
        time.sleep(0.05)
    assert captured.get("full") is True
