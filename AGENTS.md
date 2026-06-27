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
- CLI search/read fallback exists for agents or sub-agents that do not inherit
  the MCP session: `code-index search-text`, `search-symbol`, `read-span`,
  `file-symbols`, `search-semantic`, `search-hybrid`, `services` (or module
  form `py -m code_index.cli ...`). Keep this parity when adding MCP tools.
- Progress UI: `code-index index`/`index-all` show a `rich` progress bar on a
  TTY (`--plain` to disable); `code-index status [--watch]` is a dashboard table;
  `code-index web [--port N]` is an opt-in stdlib `http.server` status page.
- Background reindex (for git commit/push hooks, fire-and-forget): MCP tool
  `reindex_background` + CLI `code-index index --background`. Both return at once
  and are idempotent per service (won't start a 2nd run if one is already
  active). Poll `index_stats` / `code-index status` for progress.

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
- **Deleting stale files MUST stay batched (`store.delete_files`), never
  per-file in a loop.** `lines_fts.path` is an FTS5 `UNINDEXED` column (no
  btree), so every `DELETE ... WHERE path = ?` scans the WHOLE FTS table.
  Per-file deletion is O(files × table) and hangs for hours when a repo shrinks
  a lot (e.g. an ignore rule cutting 19k → 5k files makes ~14k full scans). The
  indexer deletes in chunks of 200 via `WHERE path IN (...)`, commits per chunk,
  and emits live `removing N/total` progress; `SemanticIndex.delete_paths` is
  likewise chunked (no single giant `MatchAny`). `delete_file` is just a
  one-path wrapper — don't reintroduce a per-file delete loop.
- **Background reindex has ONE shared helper; the job guard is non-reentrant.**
  `indexer.start_background_reindex(id, name, settings, full, status_phase,
  thread_factory)` is the single fire-and-forget entry point reused by the MCP
  `reindex_background` tool, the CLI `index --background`, and `webui.start_reindex`
  — don't fork a second copy of the thread/job machinery. Idempotency is per
  service id: it skips if THIS process has a live job (`_bg_jobs`) OR
  `status_phase` (from `read_status`) is in `_ACTIVE_PHASES` (catches another
  process's watcher/CLI run). The guard lock `_bg_jobs_lock` is a plain
  `threading.Lock` (NOT reentrant): the busy check inside the locked section
  must call `_job_alive_locked` directly, never `background_job_alive`/
  `is_reindex_active` (those re-acquire the lock and deadlock). The CLI path is
  special: a CLI process is short-lived, so `--background` does NOT use a daemon
  thread (it would die on exit) — it spawns a fully detached child
  (`code-index index --plain`, `start_new_session`/`DETACHED_PROCESS`) that
  outlives the hook, after the same `read_status` idempotency check. All paths
  drive a `StatusFileReporter`, so progress shows up in `index_stats`/`status`.
- **Cold start must fail fast, never hang, and never hold the global lock.**
  Only `search_semantic`/`search_hybrid` may build the semantic client; the
  SQLite tools (`search_text`/`search_symbol`/`file_symbols`/`read_span`) must
  stay instant, and `index_stats` must remain SQLite-first: it must not
  construct `SemanticIndex`/`QdrantClient` on its hot path. It may do only a
  tiny stdlib Qdrant count probe bounded by `CODE_INDEX_QDRANT_HEALTH_TIMEOUT`
  (default 0.75s) after returning SQLite counts.
  MCP read/search tools cache SQLite `Store` connections per worker thread and
  use `CODE_INDEX_SQLITE_READ_TIMEOUT` (default 0.75s) so a concurrent
  reindex/watcher lock becomes a visible "database is busy; retry" response
  rather than a full MCP `Request timed out`. Indexer/writer paths keep the
  longer Store default busy timeout.
  MCP server diagnostics are file-only (never stdout/stderr because stdio is the
  protocol): `~/.cache/code-index/logs/mcp.log` with rotation. Every tool logs
  `tool_start`/`tool_finish`/`tool_error` plus `tool_slow` after
  `CODE_INDEX_SLOW_TOOL_SECONDS` (default 2s); use this to diagnose external MCP
  `Request timed out` errors. Tunables: `CODE_INDEX_LOG_LEVEL`,
  `CODE_INDEX_LOG_MAX_BYTES`, `CODE_INDEX_LOG_BACKUPS`.
  The Qdrant client is built with a hard per-request `timeout`
  (`Settings.qdrant_timeout`, env `CODE_INDEX_QDRANT_TIMEOUT`, default 5s) and
  `check_compatibility=False` — the REST default timeout is huge, so a
  warming-up Qdrant (Docker up, service not ready) would otherwise hang past the
  MCP client's tool timeout. `server._get_semantic` builds the `SemanticIndex`
  OUTSIDE `_lock` (double-checked insert): its constructor opens a network
  client, and holding `_lock` there would block every other tool. `health()`
  returns `warming_up` (port open but not ready — transient) vs `unavailable`
  (nothing there) vs `ok`; `index_stats` reports the same states from its fast
  probe. Don't reintroduce semantic construction/network calls under `_lock`,
  or a full-timeout Qdrant client call inside `index_stats`.
  The FIRST semantic call also pays a one-time cost NO timeout guards:
  `SemanticIndex.__init__` lazily does `import qdrant_client`, which pulls in
  fastembed -> onnxruntime; on a true cold start that import alone can take ~60s
  (observed: first `search_semantic` 65s, the next identical one 4.8s; the embed
  HTTP call itself was fast — the time was the import, not the network). So
  `server._prewarm_semantic()` builds the default service's `SemanticIndex` in a
  background daemon thread at startup (`main()`), off the request path,
  best-effort (never raises/blocks), no-op when semantic is disabled, opt-out via
  `CODE_INDEX_PREWARM=0`. It logs `prewarm_semantic_done`/`_failed` to `mcp.log`.
  Keep this background — don't move the heavy import onto the first request, and
  don't make startup block on it.
- **Semantic degradation must stay VISIBLE, never silent.** `flush()` counts
  lost chunks (`embed_failures`) / points (`upsert_failures`) instead of dropping
  them; `search()` sets `last_search_failed` so callers tell "down" from "empty";
  `health()` is a cheap liveness probe. The MCP tools (`search_semantic`,
  `search_hybrid`, `index_stats`) report disabled vs unavailable vs empty
  distinctly, and the indexer surfaces counts via `IndexReport` →
  `status.json` → `status`/web UI. Don't revert these to bare `except: return []`.
- **The SEARCH path must never inherit the INDEXING embed timeout/retries.** The
  api embeddings provider (qwen3-embedding-8b on routerai.ru) has bimodal
  latency: ~1s warm but 11-21s on a cold start (the 8B model gets paged back in),
  so embedding a query for `search_semantic`/`search_hybrid` was hanging the MCP
  tool past the client's ~60s timeout (the old `ApiEmbedder` timeout was a
  hardcoded 60s × up to 3 retries, and `_make_embedder` never even plumbed it
  through). Now `ApiEmbedder.embed_query` is a SEPARATE path with its own budget
  (`embed_search_timeout`, default 45s — 15s was cutting ~25% of real queries on
  cold starts), ZERO retries (`embed_search_retries`),
  and a small in-memory LRU (`embed_query_cache`); the indexing `embed()` path
  keeps the longer `embed_timeout`/`embed_max_retries`. `SemanticIndex.search`
  calls `_embed_query` (which prefers `embedder.embed_query` when present, else
  falls back to `embed`) so a slow/down provider sets `last_search_failed` fast
  and `search_hybrid` still returns its symbols+text sections. A local fallback
  embedder is NOT an option for search: the collection is qwen-4096-dim, a local
  model (~384-dim) lives in a different vector space and can't query it. Don't
  reintroduce a 60s hardcoded timeout or retries on the query-embedding path.
  The real fix for cold starts is keeping the model warm, not switching provider
  (benchmarked: routerai vs OpenRouter default/`:nitro` on the same model are all
  ~1-2s warm; the 11-21s spikes are idle unloads, identical across providers).
  `watcher._keep_warm_loop` pings `embed(["keep warm"])` every
  `CODE_INDEX_WARM_INTERVAL` seconds (default 120; 0 disables) so interactive
  search stays on the warm path. It uses `embed()` NOT `embed_query()` on purpose
  — the query LRU would serve a repeated string from cache and never warm the
  remote model. Best-effort: api backend only (a local fastembed model is always
  resident), never raises, keeps the daemon alive.

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
`CODE_INDEX_IGNORE_PATHS`, and the embed-timeout knobs `CODE_INDEX_EMBED_TIMEOUT`
(indexing path, default 120s), `CODE_INDEX_EMBED_SEARCH_TIMEOUT` (query path,
default 45s), `CODE_INDEX_EMBED_SEARCH_RETRIES` (default 0), `CODE_INDEX_EMBED_QUERY_CACHE`
(LRU size, default 256). If you change embedding behavior, update both files.

## Testing conventions

- `tests/conftest.py` is `autouse`: it redirects `CONFIG_HOME`/`CACHE_HOME` to a
  tmp dir, sets `CODE_INDEX_SEMANTIC=0`, and **reloads `code_index.config`**
  because that module reads env into module-level constants at import time. If
  you add import-time env reads to `config.py`, the reload in conftest must still
  pick them up.
- Symbol tests `pytest.skip` when tree-sitter is absent; semantic tests mock
  `urllib.request.urlopen` and never hit the network or need an API key.
