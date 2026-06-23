"""Indexer end-to-end against the filesystem (text + symbols layers)."""

from code_index.config import settings_for
from code_index.indexer import build_index, run_index
from code_index.store import Store


def test_full_index_and_incremental(sample_repo):
    settings = settings_for(sample_repo)
    report = build_index(settings, full=True)

    # 4 indexable files: 2 java + 1 properties + 1 py (build/ excluded).
    assert report.indexed == 4
    assert report.removed == 0

    store = Store(settings.db_path)
    # build output must not have leaked in
    assert store.search_text("GENERATED_SHOULD_BE_IGNORED") == []
    # text search works across languages
    assert any(h.path.endswith("BillingController.java") for h in store.search_text("chargeCustomer"))
    assert any(h.path.endswith("application.properties") for h in store.search_text("server*"))
    store.close()

    # Second incremental run: nothing changed -> everything skipped.
    report2 = build_index(settings, full=False)
    assert report2.indexed == 0
    assert report2.skipped >= 4


def test_incremental_picks_up_change_and_deletion(sample_repo):
    settings = settings_for(sample_repo)
    build_index(settings, full=True)

    # modify one file
    f = sample_repo / "helper.py"
    f.write_text("def calculate_total(items):\n    return 999\n", encoding="utf-8")
    report = build_index(settings, full=False)
    assert report.indexed == 1

    store = Store(settings.db_path)
    assert store.search_text("999")
    store.close()

    # delete it -> removed from index on next run
    f.unlink()
    report2 = build_index(settings, full=False)
    assert report2.removed == 1
    store = Store(settings.db_path)
    assert store.search_text("calculate_total") == []
    store.close()


def test_per_service_ignore_globs(sample_repo):
    settings = settings_for(sample_repo, ignore_globs=["**/*.properties"])
    report = build_index(settings, full=True)
    # The .properties file is excluded -> 3 files instead of 4.
    assert report.indexed == 3
    store = Store(settings.db_path)
    assert store.search_text("server*") == []  # application.properties skipped
    assert any(h.path.endswith("helper.py") for h in store.search_text("calculate_total"))
    store.close()


def test_on_progress_callback_invoked(sample_repo):
    settings = settings_for(sample_repo)
    events = []

    def on_progress(done, total, current, phase):
        events.append((done, total, current, phase))

    report = build_index(settings, full=True, on_progress=on_progress)
    phases = {e[3] for e in events}
    assert "scanning" in phases
    assert "indexing" in phases
    assert "done" in phases
    # final indexing event reaches the total
    indexing = [e for e in events if e[3] == "indexing"]
    assert indexing[-1][0] == indexing[-1][1] == report.indexed + report.skipped


def test_run_index_drives_reporter_lifecycle(sample_repo):
    settings = settings_for(sample_repo)

    class _Rec:
        def __init__(self):
            self.started = False
            self.finished = None
            self.errored = None
            self.calls = 0

        def start(self):
            self.started = True

        def __call__(self, done, total, current, phase):
            self.calls += 1

        def finish(self, report):
            self.finished = report

        def error(self, exc):
            self.errored = exc

    rec = _Rec()
    report = run_index(settings, full=True, reporter=rec)
    assert rec.started is True
    assert rec.finished is report
    assert rec.errored is None
    assert rec.calls > 0
