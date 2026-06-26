# code-index-mcp

Self-hosted **hybrid code search** as an MCP server for **Kilo CLI / OpenCode**.
Everything runs locally on your machine — no third-party MCP servers, no LSP,
no SaaS.

Three search layers, one server:

| Layer | Engine | Good for |
|---|---|---|
| **text** | SQLite **FTS5** | exact strings, identifiers, config keys, errors |
| **symbols** | **tree-sitter** | "where is `X` defined", file outline |
| **semantic** | **fastembed** (local) → **Qdrant** | fuzzy "where do we do Y" |

## Don't want to configure this yourself? Let your agent do it

You don't need to learn the env vars, registry file, or Qdrant. Just tell your
coding agent (Kilo CLI / OpenCode) to set it up from the runbook:

> **"Set up code-index for me, following the instructions at
> https://github.com/Krat12/mcp-code-index/blob/main/docs/AGENT_SETUP.md"**

The agent reads [`docs/AGENT_SETUP.md`](docs/AGENT_SETUP.md), checks your
environment, asks you only the few things it can't decide (which repos, your
embeddings API key, whether to honor each repo's `.gitignore`, what else to
ignore), wires everything up, builds the index, optionally installs the
[`code-search` skill](skills/code-search/SKILL.md) so sub-agents can use the CLI
fallback, and tells you where everything lives — without writing anything into
your repos. The rest of this README is the manual reference for those who prefer
to do it by hand.

## Requirements

- Python 3.10+
- Qdrant running locally (you already have it) at `http://localhost:6333`
- The rest is pip-installed (tree-sitter grammars + fastembed are local; the
  embedding model downloads once and then runs offline).

## Install

```powershell
# from the project folder
uv venv
uv pip install -e .
# or: pip install -e .
```

## Nothing is written into your repos

All state lives **outside** the indexed repositories:

- SQLite index → `~/.cache/code-index/<service-id>.sqlite3`
- Qdrant collection → `code_<service-id>` (derived from the repo's absolute path)
- Project registry → `~/.config/code-index/projects.toml`

So you never commit anything, never edit `.gitignore`, and never add git hooks
inside the service repos — no PR noise.

## Single project

```powershell
code-index index            # incremental index of CWD (or --path DIR)
code-index index --full     # full rebuild
code-index stats
```

## Microservices (many repos)

Register services in the **external** registry (no files in the repos):

```powershell
# Register each service explicitly...
code-index add C:\work\billing  --name billing
code-index add C:\work\payments --name payments

# ...or point at a parent folder and auto-discover every git repo inside:
code-index add-workspace C:\work --depth 1

code-index list             # show resolved services
code-index index-all        # (re)index every service (per-service index)
code-index stats-all
```

The registry file (`~/.config/code-index/projects.toml`) looks like:

```toml
[[service]]
name = "billing"
path = "C:/work/billing"

[[workspace]]
path = "C:/work"
depth = 1
```

## Per-project ignore (no files in the repo)

Each `[[service]]` (and `[[workspace]]`) can declare what to skip — all in the
**external** registry, so nothing is written into the service repo:

```toml
[[service]]
name = "billing"
path = "C:/work/billing"
ignore = ["**/generated/**", "*.pb.go", "docs/legacy/**"]  # extra ignore globs
use_gitignore = true   # ALSO skip whatever the repo-root .gitignore lists
```

- `ignore` is a list of glob patterns matched against repo-relative POSIX paths.
  Supported glob/.gitignore-like syntax: `*` (no `/`), `**` (any depth), `?`,
  a trailing `/` (directories only), and a leading/embedded `/` (anchored to the
  repo root). These are layered **on top of** the built-in ignores (build dirs,
  lock files, minified bundles, etc.).
- `use_gitignore = true` additionally reads the repo's **top-level** `.gitignore`
  (read-only — never written). Negation rules (`!pattern`) and nested
  per-directory `.gitignore` files are intentionally not supported, to keep the
  matcher tiny and predictable.
- For a `[[workspace]]`, `ignore`/`use_gitignore` are inherited by every
  auto-discovered repo under it.

## Watch indexing progress

```powershell
code-index index            # shows a live rich progress bar (TTY)
code-index index --plain    # plain stderr logging instead
code-index status           # one-shot table: phase / progress / files / symbols
code-index status --watch   # live dashboard, refreshes ~1/s (Ctrl+C to stop)
code-index web              # tiny local web dashboard at http://127.0.0.1:8765
code-index web --port 9000  # pick another port
```

Progress is shared **across processes** via tiny JSON files in
`~/.cache/code-index/status/<id>.json` (atomic writes). So you can run
`code-index status --watch` (or open the web page) in one terminal and watch the
background `code-index-watch` daemon — or an `index-all` running elsewhere —
make progress in real time. The web UI is opt-in (started only by `code-index
web`), single-threaded, and does work only when the browser polls — deliberately
light for a low-power machine.

## Auto re-index (no git hooks)

A background daemon keeps every registered service fresh using **filesystem
events (watchdog)** plus a **periodic safety sweep** — all outside the repos:

```powershell
code-index-watch                 # FS events + sweep every 600s
code-index-watch --interval 300  # sweep every 5 min
code-index-watch --no-periodic   # FS events only
```

Leave it running (e.g. as a startup task / Windows service). It does an initial
incremental index of each service, then re-indexes only what changes.

## Wire it into Kilo CLI / OpenCode

Add a **local** MCP server to your config
(`~/.config/kilo/opencode.json` for Kilo CLI, or `~/.config/opencode/opencode.json`
for OpenCode; a per-project `opencode.json` works too):

One MCP server can serve **all** your microservices — the agent picks the
service per call (or uses the default CWD service). Put this in the **global**
config (`~/.config/kilo/opencode.json` for Kilo, `~/.config/opencode/opencode.json`
for OpenCode):

```jsonc
{
  "$schema": "https://app.kilo.ai/config.json",
  "mcp": {
    "code-index": {
      "type": "local",
      "command": ["code-index-mcp"],
      "enabled": true,
      "environment": {
        "CODE_INDEX_ROOT": "${cwd}",
        "QDRANT_URL": "http://localhost:6333",
        "CODE_INDEX_EMBED_MODEL": "BAAI/bge-small-en-v1.5",
        "CODE_INDEX_SEMANTIC": "1"
      }
    }
  }
}
```

> If `code-index-mcp` isn't on PATH, use the absolute path to the venv script,
> e.g. `["C:/Users/you/PycharmProjects/code-index-mcp/.venv/Scripts/code-index-mcp.exe"]`,
> or `["python", "-m", "code_index.server"]` with the venv's python.

Restart the CLI. The agent now sees tools:
`search_text`, `search_symbol`, `file_symbols`, `read_span`, `search_semantic`,
`search_hybrid`, `list_services`, `reindex`, `index_stats`. Every search tool
takes an optional `service` (name or id from `list_services`).

- `read_span(path, start_line, end_line, context=0, service=...)` returns the
  actual source at a location — the natural follow-up to a search hit, so the
  agent can read code without a separate file tool (path is confined to the
  repo; falls back to the index if the file changed/vanished on disk).
- `search_text`/`search_symbol`/`search_semantic`/`search_hybrid` accept
  `path_glob` and `exclude_glob` (globs, comma-separated or a list) to narrow
  results by repo-relative path, e.g. `path_glob="backend/**"`,
  `exclude_glob="**/tests/**"`. `search_text` also degrades gracefully on
  malformed FTS5 input (it retries the query as a literal phrase).

## Keeping the index fresh

- Background daemon (recommended): `code-index-watch` (see above).
- On demand from the agent: it can call the `reindex` tool.
- Manual: `code-index index` / `code-index index-all`.
- At server start: set `CODE_INDEX_REINDEX_ON_START=1` for a quick background
  incremental of the default service when the MCP server launches.

## Environment variables

| Var | Default | Meaning |
|---|---|---|
| `CODE_INDEX_ROOT` | `.` | default project root (single-project / fallback) |
| `CODE_INDEX_CONFIG_HOME` | `~/.config/code-index` | where `projects.toml` lives |
| `CODE_INDEX_CACHE_HOME` | `~/.cache/code-index` | where SQLite indexes live |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant endpoint |
| `QDRANT_API_KEY` | – | optional Qdrant key |
| `CODE_INDEX_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | fastembed model |
| `CODE_INDEX_SEMANTIC` | `1` | set `0` to disable semantic layer |
| `CODE_INDEX_REINDEX_ON_START` | `0` | `1` = background reindex on server start |

> Live indexing status is written to `~/.cache/code-index/status/<id>.json`
> (under `CODE_INDEX_CACHE_HOME`). Inspect it with `code-index status` /
> `code-index status --watch`, or the `code-index web` dashboard.

## Graceful degradation

- No tree-sitter? → symbols layer off, text + semantic still work.
- Qdrant down / fastembed missing? → semantic off, text + symbols still work.
- The text (FTS5) layer always works as long as the SQLite index exists.

Degradation is **visible, not silent**:

- `index_stats` reports the semantic layer's health: `ok (points=N)`,
  `disabled` (turned off), or `unavailable` (API/Qdrant unreachable).
- `search_semantic` / `search_hybrid` distinguish **disabled vs unavailable vs
  "no matches"**, so the agent knows to fall back to text/symbols instead of
  treating a down layer as an empty result.
- Indexing counts what it couldn't store — chunks that failed to embed and
  vectors that failed to upsert — and surfaces the totals in the run log, in
  `status.json`, and in the `status` / web dashboards (a ⚠ marker).
