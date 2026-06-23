"""Watcher relevance filter (decides which FS events trigger a re-index)."""

from code_index.watcher import _relevant


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
