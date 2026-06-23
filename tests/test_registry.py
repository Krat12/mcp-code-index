"""Registry: explicit services, workspace auto-discovery, de-dup, add helpers."""

from pathlib import Path

from code_index.registry import add_service, add_workspace, load_registry
from code_index.config import project_id


def _make_repo(parent: Path, name: str) -> Path:
    repo = parent / name
    (repo / ".git").mkdir(parents=True)
    (repo / "README.md").write_text("x", encoding="utf-8")
    return repo


def test_add_service_and_load(tmp_path, monkeypatch):
    reg = tmp_path / "projects.toml"
    monkeypatch.setattr("code_index.registry.REGISTRY_PATH", reg)
    svc_dir = _make_repo(tmp_path, "billing")

    add_service(svc_dir, name="billing", registry_path=reg)
    services = load_registry(reg)

    assert len(services) == 1
    assert services[0].name == "billing"
    assert services[0].path == svc_dir.resolve()
    assert services[0].id == project_id(svc_dir)


def test_workspace_autodiscovers_git_repos(tmp_path):
    reg = tmp_path / "projects.toml"
    work = tmp_path / "work"
    work.mkdir()
    _make_repo(work, "billing")
    _make_repo(work, "payments")
    (work / "not-a-repo").mkdir()  # no .git -> ignored

    add_workspace(work, depth=1, registry_path=reg)
    services = load_registry(reg)

    names = sorted(s.name for s in services)
    assert names == ["billing", "payments"]


def test_dedup_between_service_and_workspace(tmp_path):
    reg = tmp_path / "projects.toml"
    work = tmp_path / "work"
    work.mkdir()
    repo = _make_repo(work, "billing")

    add_service(repo, name="billing", registry_path=reg)
    add_workspace(work, depth=1, registry_path=reg)

    services = load_registry(reg)
    # Same path registered twice -> single entry.
    assert len([s for s in services if s.path == repo.resolve()]) == 1


def test_project_id_is_stable_and_unique(tmp_path):
    a = tmp_path / "a" / "billing"
    b = tmp_path / "b" / "billing"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    # Same dir name, different paths -> different ids.
    assert project_id(a) != project_id(b)
    # Stable across calls.
    assert project_id(a) == project_id(a)


def test_load_missing_registry_returns_empty(tmp_path):
    assert load_registry(tmp_path / "nope.toml") == []


def test_service_ignore_fields_parsed(tmp_path):
    reg = tmp_path / "projects.toml"
    repo = _make_repo(tmp_path, "billing")
    reg.write_text(
        "[[service]]\n"
        f'name = "billing"\n'
        f'path = "{repo.as_posix()}"\n'
        'ignore = ["**/generated/**", "*.pb.go"]\n'
        "use_gitignore = true\n",
        encoding="utf-8",
    )
    services = load_registry(reg)
    assert len(services) == 1
    s = services[0]
    assert s.ignore == ["**/generated/**", "*.pb.go"]
    assert s.use_gitignore is True
    # Settings carry the ignore config through.
    settings = s.settings()
    assert settings.ignore_globs == ["**/generated/**", "*.pb.go"]
    assert settings.use_gitignore is True


def test_service_defaults_when_no_ignore(tmp_path):
    reg = tmp_path / "projects.toml"
    repo = _make_repo(tmp_path, "payments")
    add_service(repo, name="payments", registry_path=reg)
    s = load_registry(reg)[0]
    assert s.ignore == []
    assert s.use_gitignore is False


def test_workspace_ignore_inherited(tmp_path):
    reg = tmp_path / "projects.toml"
    work = tmp_path / "work"
    work.mkdir()
    _make_repo(work, "billing")
    reg.write_text(
        "[[workspace]]\n"
        f'path = "{work.as_posix()}"\n'
        "depth = 1\n"
        'ignore = ["dist/**"]\n'
        "use_gitignore = true\n",
        encoding="utf-8",
    )
    services = load_registry(reg)
    assert services
    for s in services:
        assert s.ignore == ["dist/**"]
        assert s.use_gitignore is True
