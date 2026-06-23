"""Walker: directory filtering, ignored dirs, binary detection, chunking."""

from code_index.walker import (
    IgnoreSpec,
    build_ignore_spec,
    chunk_lines,
    iter_files,
    read_gitignore,
    read_text,
    rel,
    _keep_dir,
)


def test_ignores_build_and_git_dirs():
    assert _keep_dir("build") is False
    assert _keep_dir("target") is False
    assert _keep_dir(".git") is False
    assert _keep_dir(".gradle") is False
    assert _keep_dir("node_modules") is False


def test_keeps_normal_and_whitelisted_hidden_dirs():
    assert _keep_dir("src") is True
    assert _keep_dir("com") is True
    assert _keep_dir(".github") is True  # whitelisted hidden
    assert _keep_dir(".mvn") is True
    assert _keep_dir(".secret") is False  # other hidden dirs pruned


def test_iter_files_skips_build_output(sample_repo):
    files = {rel(sample_repo, p) for p in iter_files(sample_repo)}
    assert "src/main/java/com/acme/BillingController.java" in files
    assert "src/main/resources/application.properties" in files
    assert "helper.py" in files
    # Gradle build output and .git must be excluded.
    assert not any("build/" in f for f in files)
    assert not any(".git" in f for f in files)


def test_read_text_handles_binary(tmp_path):
    binp = tmp_path / "bin.dat"
    binp.write_bytes(b"\x00\x01\x02binary")
    assert read_text(binp) is None

    txt = tmp_path / "ok.txt"
    txt.write_text("hello", encoding="utf-8")
    assert read_text(txt) == "hello"


def test_chunk_lines_overlap_and_bounds():
    text = "\n".join(str(i) for i in range(1, 201))  # 200 lines
    chunks = list(chunk_lines(text, window=60, overlap=15))
    assert chunks[0][0] == 1
    assert chunks[0][1] == 60
    # step = window - overlap = 45 -> second chunk starts at line 46
    assert chunks[1][0] == 46
    # last chunk must reach the final line
    assert chunks[-1][1] == 200


# --- per-project ignore globs (IgnoreSpec) ---------------------------------


def test_ignore_spec_doublestar_and_basename():
    spec = IgnoreSpec(["**/generated/**", "*.pb.go", "docs/legacy/**"])
    assert spec.match("src/generated/a.go")
    assert spec.match("a/b/generated/x.py")
    assert spec.match("x.pb.go")
    assert spec.match("api/v1/service.pb.go")  # basename match at any depth
    assert spec.match("docs/legacy/old.md")
    assert not spec.match("src/main.py")
    assert not spec.match("docs/current/new.md")


def test_ignore_spec_anchored_and_dir_only():
    spec = IgnoreSpec(["/secret.txt", "build/"])
    assert spec.match("secret.txt")
    assert not spec.match("sub/secret.txt")  # anchored to root
    assert spec.match("build", is_dir=True)
    assert spec.match("nested/build", is_dir=True)  # dir name at any depth
    # a dir-only pattern should not match a same-named file
    assert not spec.match("build", is_dir=False)


def test_ignore_spec_ignores_comments_negation_blanks():
    spec = IgnoreSpec(["# comment", "", "!keep.txt", "drop.txt"])
    assert spec.match("drop.txt")
    assert not spec.match("keep.txt")  # negation not supported -> not ignored
    assert bool(spec) is True


def test_empty_spec_is_falsey_and_matches_nothing():
    spec = IgnoreSpec([])
    assert bool(spec) is False
    assert not spec.match("anything.py")


def test_iter_files_honors_ignore_spec(sample_repo):
    spec = IgnoreSpec(["**/*.properties"])
    files = {rel(sample_repo, p) for p in iter_files(sample_repo, spec)}
    assert "helper.py" in files
    assert not any(f.endswith(".properties") for f in files)


def test_read_gitignore_and_build_spec(tmp_path):
    (tmp_path / ".gitignore").write_text(
        "# comment\n\nnode_modules/\n*.log\n!keep.log\n", encoding="utf-8"
    )
    patterns = read_gitignore(tmp_path)
    assert "node_modules/" in patterns
    assert "*.log" in patterns
    assert "!keep.log" not in patterns  # negations dropped on read

    spec = build_ignore_spec(["extra/**"], use_gitignore=True, root=tmp_path)
    assert spec.match("app.log")
    assert spec.match("node_modules", is_dir=True)
    assert spec.match("extra/foo.py")


def test_build_spec_without_gitignore_skips_file(tmp_path):
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    spec = build_ignore_spec(["keep_me_out/**"], use_gitignore=False, root=tmp_path)
    assert not spec.match("app.log")  # gitignore not consulted
    assert spec.match("keep_me_out/x.py")
