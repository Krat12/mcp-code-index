# AGENTS.md

Self-hosted hybrid code-search MCP server (SQLite FTS5 + tree-sitter symbols +
Qdrant semantic) for Kilo CLI / OpenCode. Pure Python, `src/` layout, no CI,
no lint/format/typecheck config — `pytest` is the only quality gate.

## Environment (Windows gotcha)

- `python` / `python.exe` on this machine is the **broken Windows Store stub**
  (`...\WindowsApps\python.exe`) — it prints `Python` and exits. **Do not use it.**
- Use the launcher `py` (Python 3.12.3) for everything: `py`, `py -m pytest`,
  `py -m pip ...`.
- There is **no `.venv`**; dependencies are installed into the global 3.12
  interpreter and `mcp`, `tree-sitter*`, `qdrant-client`, `watchdog`, `pytest`
  are already present. Editable install (`py -m pip install -e .`) is only
  needed for the `code-index*` console scripts, not to run tests.

## Commands

- Run tests: `py -m pytest`  (29 tests, ~1.5s; `pythonpath=["src"]` and
  `testpaths=["tests"]` come from `pyproject.toml`, so no install needed).
- Single test: `py -m pytest tests/test_symbols.py::test_python_symbols`
- Console scripts (after `py -m pip install -e .`): `code-index` (CLI),
  `code-index-mcp` (MCP server, stdio), `code-index-watch` (auto-reindex daemon).
- Progress UI: `code-index index`/`index-all` show a `rich` progress bar on a
  TTY (`--plain` to disable); `code-index status [--watch]` is a dashboard table;
  `code-index web [--port N]` is an opt-in stdlib `http.server` status page.

## Architecture (read before editing)

Pipeline lives in `src/code_index/`:
`walker` (file discovery/read) → `symbols` (tree-sitter) + `store` (SQLite FTS5)
+ `semantic` (embeddings→Qdrant), orchestrated by `indexer.build_index`.
Three frontends: `cli.py`, `server.py` (FastMCP tools), `watcher.py` (watchdog
daemon). `config.py` holds all tunables; `registry.py` reads the external
`projects.toml`. `progress.py` = progress reporters + a cross-process status
channel (`CACHE_HOME/status/<id>.json`, atomic writes); `webui.py` = optional
stdlib status page.

- **Progress is reported via callbacks, not prints.** `build_index(...,
  on_progress=cb)` calls `cb(done, total, current, phase)`; `run_index(...,
  reporter=r)` wraps it with the reporter lifecycle (`start`/`finish`/`error`).
  Default is no-op (tests/MCP). Keep `build_index` materializing the file list
  once (`list(iter_files(...))`) so `total` exists — don't revert to a bare
  generator. Reporters are best-effort and must never break indexing.
- **Per-project ignore lives in `projects.toml`, never in the repo.** A
  `[[service]]` may set `ignore = [globs...]` and `use_gitignore = true`.
  `walker.IgnoreSpec` is a self-contained glob→regex matcher (no `pathspec`
  dependency) over POSIX-relative paths; `build_ignore_spec()` merges explicit
  globs with the repo-root `.gitignore` (no `!` negation, no nested files).
  `iter_files(root, spec=None)` keeps its old behavior when `spec` is None.
  Resolve a service's `Settings` via `service.settings()` so the ignore config
  flows through (CLI/watcher/server already do this).
- **Search-result path filtering reuses the ignore matcher.** `walker.PathFilter`
  wraps `IgnoreSpec` for include/exclude globs; `store.search_text`/`search_symbol`
  and `SemanticIndex.search` take `path_glob`/`exclude_glob` and post-filter
  (over-fetching `limit*8` first). The MCP search tools expose these via
  `_as_glob_list` (comma-string or list). Don't add a second glob engine.
- **`read_span` closes the search→read loop inside the server.** `walker.read_span`
  reads live-disk lines with a path-traversal guard (the relpath must stay under
  the repo root); the `read_span` MCP tool falls back to `store.get_lines` (the
  stored FTS copy) when the file changed/vanished. Keep the guard.
- **`store.search_text` must not crash on bad FTS5 syntax.** `_fts_query` catches
  `sqlite3.OperationalError` and retries the input as a quoted phrase — preserve
  this when touching text search.

- **All index state lives OUTSIDE indexed repos** (deliberate): SQLite in
  `~/.cache/code-index/<id>.sqlite3`, registry in
  `~/.config/code-index/projects.toml`, Qdrant collection `code_<id>`. Never
  write index files, `.gitignore` entries, or git hooks into target repos.
- **Per-service isolation**: `config.project_id()` = `<dirname>_<sha1(abspath)[:12]>`
  keys both the SQLite filename and the Qdrant collection. One MCP server serves
  many microservices; every search tool takes an optional `service` (name/id).
- **Graceful degradation is a hard requirement**: missing tree-sitter → symbols
  off; Qdrant/embedder unreachable → semantic off; FTS5 text layer always works.
  Preserve the broad `try/except` fallbacks in `semantic.py` and `symbols.py`.
- `symbols.py` has an adapter layer (`_node_kind`, `_node_children`, etc.)
  normalizing differing tree-sitter binding APIs — keep it when touching symbols.
- Stored paths are POSIX-relative to the repo root (`walker.rel`) for OS stability.

## Embeddings: README is out of date — trust `config.py`

`README.md` describes fastembed/local as the default semantic backend. The code
defaults to **`embed_backend="api"`** (OpenAI-compatible `/embeddings`), base
`https://routerai.ru/api/v1`, model `qwen/qwen3-embedding-8b`, and **requires
`CODE_INDEX_EMBED_API_KEY`** or the api embedder raises and semantic disables
itself. Set `CODE_INDEX_EMBED_BACKEND=fastembed` for the local path. Env vars in
the README table are incomplete vs `config.Settings`: also exist
`CODE_INDEX_EMBED_BACKEND`, `CODE_INDEX_EMBED_API_BASE`, `_API_MODEL`,
`_API_KEY`, `CODE_INDEX_EMBED_DIM`, `CODE_INDEX_DB`, `CODE_INDEX_MAX_DOC_BYTES`,
`CODE_INDEX_IGNORE_PATHS`. If you change embedding behavior, update both files.

## Testing conventions

- `tests/conftest.py` is `autouse`: it redirects `CONFIG_HOME`/`CACHE_HOME` to a
  tmp dir, sets `CODE_INDEX_SEMANTIC=0`, and **reloads `code_index.config`**
  because that module reads env into module-level constants at import time. If
  you add import-time env reads to `config.py`, the reload in conftest must still
  pick them up.
- Symbol tests `pytest.skip` when tree-sitter is absent; semantic tests mock
  `urllib.request.urlopen` and never hit the network or need an API key.
