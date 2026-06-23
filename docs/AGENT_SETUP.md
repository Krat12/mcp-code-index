# AGENT_SETUP — runbook for the coding agent

**You are an AI coding agent. The user pointed you here and asked you to "set up
code-index". Read this whole file before doing anything, then drive the setup
yourself: detect the environment, ask the user the few decisions only they can
make, do the work, and finish with a short "where things live" report.**

The goal: the user should NOT have to learn env vars, registry files, or Qdrant.
You handle all of that. Ask short, concrete questions; never dump raw
configuration at them unless they ask.

> Talk to the user in **their** language (match the language they wrote to you
> in). Keep commands, paths, and identifiers verbatim.

---

## 0. Hard invariants (never violate)

These are load-bearing design choices. Breaking them defeats the whole point of
the tool.

- **Never write anything into a target/service repository.** No index files, no
  `.gitignore` edits, no git hooks, no config dropped in the repo. All state
  lives OUTSIDE the indexed repos:
  - registry → `~/.config/code-index/projects.toml`
  - SQLite indexes → `~/.cache/code-index/<service-id>.sqlite3`
  - live status → `~/.cache/code-index/status/<service-id>.json`
  - Qdrant vectors → collection `code_<service-id>`
- **One Qdrant instance is shared**; isolation is per-service collection names
  (`code_<id>`), where `<id> = <dirname>_<sha1(abspath)[:12]>`. Don't create
  extra Qdrant containers.
- **Degradation is visible, not silent.** If semantic search can't run, the
  tools say so ("disabled" vs "unavailable") — don't "fix" that by hiding it.
- **Ask before anything destructive.** A *full* reindex wipes and rebuilds the
  index and re-embeds everything via the API (slow, uses quota). The default
  incremental index is safe.

---

## 1. Detect the environment

Run these checks and adapt; report blockers to the user instead of guessing.

1. **Python interpreter.** Need 3.10+.
   - On Windows, `python`/`python.exe` may be the broken Microsoft Store stub
     (it prints `Python` and exits). If so, use the launcher `py` for
     everything (`py -3`, `py -m pip ...`, `py -m pytest`). Verify with
     `py -3 --version`.
   - On macOS/Linux use `python3`.
2. **The package.** From the repo root, install the console scripts:
   `py -m pip install -e .` (or `pip install -e .`). This gives `code-index`,
   `code-index-mcp`, `code-index-watch`. If you can't/don't want to install,
   every script also runs as a module:
   - CLI: `py -m code_index.cli ...`
   - MCP server: `py -m code_index.server`
   - watcher: `py -m code_index.watcher`
3. **Qdrant.** The semantic layer needs Qdrant reachable (default
   `http://localhost:6333`). Check `GET http://localhost:6333/collections`. If
   it's down, semantic indexing/search degrade gracefully (text + symbols still
   work) — tell the user, don't block setup on it.
4. **Optional deps already present?** `tree-sitter*` (symbols), `qdrant-client`,
   `watchdog`, `rich` come from `pip install -e .`. Missing tree-sitter → the
   symbol layer turns itself off; that's fine for a first run.

---

## 2. The embeddings API key (the part users get wrong)

The semantic layer's **default backend is an OpenAI-compatible `/embeddings`
API** (see `src/code_index/config.py`, this is authoritative — the README's
"fastembed default" line is stale). Defaults:

- `CODE_INDEX_EMBED_BACKEND = api`
- `CODE_INDEX_EMBED_API_BASE = https://routerai.ru/api/v1`
- `CODE_INDEX_EMBED_API_MODEL = qwen/qwen3-embedding-8b` (4096-dim)
- **`CODE_INDEX_EMBED_API_KEY` is required** — without it the API embedder
  raises and the semantic layer disables itself (text + symbols still work).

Ask the user for the key (and, if they use a different provider, the base URL
and model). Then set it. **There are two ways, and the order matters because of
a real gotcha:**

### 2a. Best: put the key in the MCP client's `env` block (recommended)

This is the most reliable because it doesn't depend on OS environment
inheritance at all — the MCP client injects it straight into the server
process. See §5; you'll add `CODE_INDEX_EMBED_API_KEY` there. **Prefer this.**

### 2b. Or: a persistent user environment variable

- **Windows (PowerShell), persistent for the user:**
  ```powershell
  [Environment]::SetEnvironmentVariable("CODE_INDEX_EMBED_API_KEY", "<KEY>", "User")
  ```
- **macOS/Linux:** add `export CODE_INDEX_EMBED_API_KEY=<KEY>` to the shell rc
  (`~/.bashrc`, `~/.zshrc`) or a systemd/user-service environment file.

> ⚠ **Inheritance gotcha (caused real confusion):** a variable added to the
> environment is only inherited by processes started *afterwards* from a parent
> that re-read the environment. Already-running terminals / IDEs / MCP clients
> keep their old environment. So after setting it persistently you must
> **restart the terminal / IDE / MCP client** for it to take effect — or just
> use method 2a, which sidesteps this entirely.

**Verify** a *freshly started* process sees it (don't trust the current shell):
- Windows: `py -3 -c "import os;print(bool(os.environ.get('CODE_INDEX_EMBED_API_KEY')))"`

This key is set **once per machine/process**, **not per project**. One value
covers every registered service.

---

## 3. Ask the user the decisions only they can make

Keep it to a few concrete questions:

1. **Which repositories?** Either:
   - a list of individual repo paths, or
   - one parent folder to auto-discover every git repo inside (a "workspace").
2. **Semantic search on?** Default yes (needs the key + Qdrant). If they say no,
   or there's no key/Qdrant, set `CODE_INDEX_SEMANTIC=0` and proceed with
   text + symbols only.
3. **What should be ignored per repo?** Beyond the built-in ignores (build dirs,
   `node_modules`, `.venv`, lock files, minified bundles), ask if a repo has:
   - generated/vendored code (e.g. `**/generated/**`, `*.pb.go`, `vendor/**`),
   - large data/asset dirs,
   - whether to also honor the repo's top-level `.gitignore`.
   These become `ignore = [...]` / `use_gitignore = true` in the registry — see
   §4. Nothing is written into the repo.

Don't over-ask. If they say "just index everything", the built-in ignores are a
sane default — skip the ignore questions.

---

## 4. Register the services (external registry only)

Use the CLI; it appends to `~/.config/code-index/projects.toml` (never touches
the repos).

```powershell
# one repo at a time
code-index add C:\work\billing  --name billing
code-index add C:\work\payments --name payments

# or auto-discover every git repo under a parent folder
code-index add-workspace C:\work --depth 1

code-index list      # confirm what resolved
```

(Module form if not installed: `py -m code_index.cli add ...`.)

### Per-project ignore (edit the registry file, not the repo)

If the user wanted extra ignores, open `~/.config/code-index/projects.toml` and
add fields to the relevant `[[service]]` (or `[[workspace]]`, inherited by all
discovered repos):

```toml
[[service]]
name = "billing"
path = "C:/work/billing"
ignore = ["**/generated/**", "*.pb.go", "docs/legacy/**"]
use_gitignore = true
```

Glob syntax (self-contained matcher, no `pathspec`): `*` (no `/`), `**` (any
depth), `?`, trailing `/` (dirs only), leading/embedded `/` (anchored to repo
root). **Not** supported on purpose: `!` negation and nested per-directory
`.gitignore` files. `use_gitignore = true` reads only the repo's **top-level**
`.gitignore`, read-only.

---

## 5. Wire it into the MCP client (Kilo CLI / OpenCode)

Add a **local** MCP server to the user's config. Locations:
- Kilo CLI: `~/.config/kilo/opencode.json`
- OpenCode: `~/.config/opencode/opencode.json` (a per-project `opencode.json`
  also works)

One server serves **all** registered microservices; the agent picks the service
per call. Put the API key right here (method 2a) so it doesn't depend on shell
inheritance:

```jsonc
{
  "$schema": "https://app.kilo.ai/config.json",
  "mcp": {
    "code-index": {
      "type": "local",
      "command": ["code-index-mcp"],
      "enabled": true,
      "environment": {
        "CODE_INDEX_EMBED_API_KEY": "<KEY>",
        "CODE_INDEX_EMBED_BACKEND": "api",
        "CODE_INDEX_EMBED_API_BASE": "https://routerai.ru/api/v1",
        "CODE_INDEX_EMBED_API_MODEL": "qwen/qwen3-embedding-8b",
        "CODE_INDEX_SEMANTIC": "1",
        "QDRANT_URL": "http://localhost:6333",
        "CODE_INDEX_ROOT": "${cwd}"
      }
    }
  }
}
```

- If `code-index-mcp` isn't on PATH, use the absolute path to the script, or
  `["py", "-m", "code_index.server"]` (Windows) / `["python3", "-m",
  "code_index.server"]`. Make sure that interpreter is the one with the package
  installed.
- To run with the **local** embeddings backend instead of the API, set
  `"CODE_INDEX_EMBED_BACKEND": "fastembed"` and drop the API_* vars (and the
  key requirement).
- **Restart the MCP client** after editing the config.

After restart the agent gets these tools: `search_text`, `search_symbol`,
`file_symbols`, `read_span`, `search_semantic`, `search_hybrid`,
`list_services`, `reindex`, `index_stats`. Every search tool takes an optional
`service` (name or id from `list_services`) plus `path_glob` / `exclude_glob`.

### 5a. Teach the agent to actually USE code-index (per-repo `AGENTS.md` rule)

Wiring the server in isn't enough: by default an agent reaches for its built-in
`grep`/file-reading and the MCP index sits idle. Add a short rule so the agent
**prefers code-index for search/navigation**, with a fallback.

**Where to put it — follow this decision tree per service repo:**

1. **Does an `AGENTS.md` already exist at the repo root?**
   - **No → do nothing.** Do NOT create one. (We don't add files to repos.)
   - **Yes → continue.**
2. **Is that `AGENTS.md` tracked by git?** Check from the repo:
   `git ls-files --error-unmatch AGENTS.md` (exit 0 = tracked).
   - **Untracked (the expected case here) → append our rule to it.** This is
     fine: it's the user's local, non-committed agent context, so there's no PR
     noise and no invariant broken.
   - **Tracked → STOP and ask the user.** Do not edit it silently and do NOT add
     it to `.gitignore` (editing the repo's `.gitignore` is forbidden by §0).
     Offer instead to put the rule in the **global** agent rules (see below).
3. **Always available alternative:** the user's global agent rules file (e.g.
   `~/.config/opencode/AGENTS.md` or the Kilo/OpenCode global rules). Putting the
   rule there covers every project at once and touches no repo. Use this when
   there's no per-repo `AGENTS.md`, when it's git-tracked, or whenever the user
   prefers one global place.

**The rule text to insert** (idempotent — wrap in markers and skip if the
`code-index:begin` marker is already present):

```markdown
<!-- code-index:begin -->
## Code search: prefer the code-index MCP server

This workspace is indexed by the **code-index** MCP server (hybrid text +
symbols + semantic). For searching and navigating the codebase, prefer its
tools over the built-in grep/file scan:

- Finding where something is **defined** → `search_symbol` (then `read_span` to
  read it). Use `file_symbols` for a file outline.
- Exact strings / identifiers / config keys / error messages → `search_text`.
- Conceptual "where do we do X" when you don't know the identifier →
  `search_semantic` (or `search_hybrid` to combine all layers).
- After a hit, read the code with `read_span(path, start_line, end_line)`
  instead of opening the file separately.
- In a multi-repo setup, pass the `service` argument (see `list_services`);
  narrow with `path_glob` / `exclude_glob`.

**Fallback:** if `index_stats` reports the index is empty/unavailable, or a tool
errors, fall back to the built-in search/read tools — don't get stuck. A
"semantic disabled/unavailable" reply is a degraded state, not "no results":
use `search_text`/`search_symbol`, which still work.
<!-- code-index:end -->
```

Keep it short and additive; never rewrite or reorder the user's existing
`AGENTS.md` content — just append the marked block (or replace the existing
marked block on re-runs).

---

## 6. Build the first index

```powershell
code-index index-all          # incremental index of every registered service
# watch progress in another terminal or a browser:
code-index status --watch
code-index web                # http://127.0.0.1:8765 (or --port N)
```

- With semantic on, the first build calls the embeddings API per chunk, so it
  takes minutes per repo (a Qdrant collection `code_<id>` appears as it runs).
  Without it, indexing is near-instant.
- A **fast finish with `indexed=0 skipped=N`** just means files were unchanged
  (incremental). To force a rebuild: `code-index index-all --full` (slow,
  re-embeds — confirm with the user first).

### Optional: keep it fresh automatically

```powershell
code-index-watch                 # FS events + periodic safety sweep (every 600s)
code-index-watch --interval 300
```

Leave it running (e.g. as a startup task). It re-indexes only what changes. No
git hooks involved.

---

## 7. Verify, then report to the user

Verify:
- `code-index list` — services resolve as expected.
- `code-index stats-all` — file/symbol counts are non-zero.
- For semantic: in the MCP client, `index_stats` should say
  `semantic: ok (collection=..., points=N)`. From the shell you can also check
  `GET http://localhost:6333/collections` for `code_<id>`.

Then give the user a short **"where things live"** summary, e.g.:

> Done. code-index is set up for N service(s): <names>.
> - Registry (add/remove services here): `~/.config/code-index/projects.toml`
> - Indexes: `~/.cache/code-index/<id>.sqlite3`
> - Live status: `~/.cache/code-index/status/<id>.json` — view with
>   `code-index status --watch` or `code-index web`.
> - Qdrant collections: `code_<id>` on `http://localhost:6333`.
> - Embeddings API key is in your MCP client config (`<path>`).
> - MCP server wired into `<config path>`; tools are available after restart.
> - Search rule added to `<AGENTS.md path or "global agent rules">` so the agent
>   prefers code-index for search.
> Nothing was committed/written into your tracked repositories.

---

## 8. Adding a new service or machine later (mini-runbook)

When the user later says "add repo X" or sets this up on a new machine:

**New repo on an existing setup:**
1. `code-index add <path> --name <name>` (or `add-workspace <parent>`).
2. Optionally add `ignore`/`use_gitignore` for it in
   `~/.config/code-index/projects.toml`.
3. `code-index index --path <path>` (or `code-index index-all`). With the
   watcher running, it'll also pick it up on the next sweep.
4. No MCP config change needed — the one server already serves all services;
   the agent targets it via the `service` argument (`list_services` to see ids).
5. If that repo has an untracked `AGENTS.md`, append the search rule per §5a
   (skip if the `code-index:begin` marker is already there). If not, the global
   rule from §5a already covers it.

**New machine / fresh clone:**
- Repeat §1 (install), §2 (key — reuse method 2a in the MCP config), §4
  (register repos), §5 (MCP config), §6 (index). The registry and indexes are
  per-machine under `~/.config` / `~/.cache`; copy `projects.toml` over if you
  want the same service list, then re-index (indexes/Qdrant are not portable —
  rebuild them).

---

## 9. Troubleshooting (quick map)

- **`index_stats` says `semantic: unavailable`** → key missing in the *server's*
  environment (inheritance gotcha — use method 2a and restart the client), or
  Qdrant down, or the API rejected the request. Text/symbols still work.
- **`semantic: disabled`** → `CODE_INDEX_SEMANTIC=0` is set for that process.
- **Reindex finishes in ~1s with `indexed=0`** → nothing changed (incremental).
  Use `--full` only if you truly need a rebuild.
- **`⚠ N semantic lost`** in `status`/web → some chunks failed to embed or
  upsert (transient API/Qdrant errors). Re-run the index; counts are surfaced on
  purpose, not hidden.
- **Port already in use** for `code-index web` → pick another: `--port N`.
- **`code-index` not found** → not installed; use the `py -m code_index.cli`
  module form, or `pip install -e .` from the repo root.
- **`python` prints `Python` and exits (Windows)** → it's the Store stub; use
  `py`.
- **The agent keeps using built-in grep instead of code-index** → the §5a search
  rule isn't in context. Add it to the repo's untracked `AGENTS.md`, or to the
  global agent rules, and reload the agent.

---

## 10. Authoritative references in this repo

- `src/code_index/config.py` — all tunables and **the real defaults** (trust
  this over the README for embedding behavior).
- `AGENTS.md` — architecture and the invariants you must preserve when editing.
- `README.md` — user-facing overview and the full env-var table.
- `src/code_index/registry.py` — registry format and resolution rules.
