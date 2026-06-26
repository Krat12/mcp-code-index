---
name: code-search
description: Search and navigate a codebase through the local code-index CLI (FTS5 text + tree-sitter symbols + semantic vectors). Use for "where is symbol X defined", "where does this string/identifier/error appear", "where do we do X", a file outline, or reading a span by path:line — instead of the built-in grep when the repo is indexed by code-index. Works for sub-agents that have NO code-index MCP session.
---

# code-search — search the codebase via the CLI

This workspace is indexed by **code-index** (text + symbols + semantic). Sub-agents
usually have **no MCP access** to code-index, so search through the **CLI**. It is
faster and more precise than the built-in grep: exact `path:line` hits, symbol
lookup, and meaning-based search.

## How to run it (PATH-robust)

Always invoke via the module — this does not depend on whether `code-index`(.exe)
is on PATH:

```
py -m code_index.cli <command> [args] [options]
```

(On macOS/Linux use `python3 -m code_index.cli ...`.) The short form
`code-index <command> ...` works only if the console script is on PATH; when in
doubt use the module form.

## Choosing a service (`--service`)

By default `--service` is **not needed**: the CLI resolves the service from the
current working directory. Pass `--service <name|id>` only to search a
**different** service (cross-repo). List registered services:

```
py -m code_index.cli services
```

## Commands

- `py -m code_index.cli search-symbol <name>` — where a symbol is **defined**
  (function/class/method/record). `--exact` for an exact name, otherwise substring.
- `py -m code_index.cli search-text "<string>"` — where a string **appears**:
  literal, identifier, error message, config key. FTS5 syntax: `foo AND bar`,
  `"exact phrase"`, `prefix*`.
- `py -m code_index.cli search-semantic "<question>"` — "where do we **do** X" by
  meaning, when you don't know the exact identifier.
- `py -m code_index.cli search-hybrid "<question>"` — symbols + text + semantic at
  once; use when you're not sure which layer fits.
- `py -m code_index.cli file-symbols <path>` — a file outline (symbols by line)
  before reading the whole file.
- `py -m code_index.cli read-span <path> <start> <end>` — read a span at the
  `path:line` you found (targeted, instead of reading the whole file).

For `file-symbols`/`read-span` use `--path-root` instead of `--service`.

## Options

- `--limit N` — number of results.
- `--path-glob "<glob>"` — narrow by path (e.g. `src/**`, `tests/**`). Use it
  aggressively.
- `--exclude-glob "<glob>"` — exclude `node_modules`, `.next`, generated, logs.

Globs can be comma-separated: `--path-glob "src/**,packages/**"`.

## Workflow

1. Find `path:line` via `search-symbol` / `search-text` / `search-semantic` /
   `search-hybrid`.
2. Read the relevant span via `read-span <path> <start> <end>` (not the whole file).
3. Narrow with `--path-glob` / `--exclude-glob` so you don't drown in noise.

## Degradation and fallback

- The semantic layer (`search-semantic`/`search-hybrid`) calls an external
  embeddings API and may answer slowly or degrade — that's **expected**: text and
  symbol layers still work, and `search-hybrid` still returns its symbols+text
  sections. Don't block on semantic.
- If the CLI is unavailable, errors, or returns empty when a result clearly should
  exist — fall back to the built-in Grep / Glob / Read. Don't get stuck.
